#!/bin/sh
# mihomo 代理容器入口（ElevenLabs 线路专用）：
# 拉取订阅 YAML -> 后台启动 mihomo -> 经 external-controller 锁定匹配节点 -> 前台等待。
# 仅依赖 alpine busybox 自带工具（wget/grep/sed/awk/nc）。
set -eu

# 节点名含中文/emoji，统一按字节处理，保证子串与长度运算一致
LC_ALL=C
export LC_ALL=C

CONFIG_DIR=/config
CONFIG_FILE=$CONFIG_DIR/config.yaml
CONTROLLER=http://127.0.0.1:9090
PROXIES_TMP=/tmp/proxies.json
SUB_URL=${MIHOMO_SUBSCRIPTION_URL:-}
NODE_FILTER=${MIHOMO_NODE_FILTER:-新加坡|Singapore|🇸🇬|SG}

log() {
  echo "[mihomo-init] $*"
}

if [ -z "$SUB_URL" ]; then
  log "MIHOMO_SUBSCRIPTION_URL 未配置（订阅链接属凭证，写入 .env），容器退出"
  exit 1
fi

# (a) 拉取订阅（1 次 + 失败重试 3 次）
mkdir -p "$CONFIG_DIR"
tries=0
while :; do
  tries=$((tries + 1))
  log "拉取订阅（第 $tries 次）"
  if wget -q -U clash-meta -O "$CONFIG_FILE" "$SUB_URL"; then
    break
  fi
  if [ "$tries" -ge 4 ]; then
    log "订阅拉取连续失败，退出"
    exit 1
  fi
  sleep 3
done

# (a2) 强制 allow-lan：订阅配置默认 allow-lan: false 会把代理端口绑到 127.0.0.1，
#      同一 docker 网络内的 web/worker 容器无法访问 http://mihomo:7890
#      （2026-10-13 号池"添加"422 的根因）。本容器不向宿主机发布端口，
#      allow-lan 仅对同一网络内的栈内容器可见。
if grep -qE '^allow-lan:' "$CONFIG_FILE"; then
  sed -i 's/^allow-lan:.*/allow-lan: true/' "$CONFIG_FILE"
else
  printf '\nallow-lan: true\n' >> "$CONFIG_FILE"
fi
if ! grep -qE '^allow-lan: true' "$CONFIG_FILE"; then
  log "allow-lan 强制失败，退出（代理端口对栈内容器不可达）"
  exit 1
fi
log "allow-lan 已强制为 true（代理端口对栈内容器可见）"

# (a3) 补齐地理数据文件：mihomo 只在 -d 工作目录（$CONFIG_DIR）查找 geo 文件，
#      找不到就去 GitHub 下载；境内直连 GitHub 不稳定，超时会导致 mihomo
#      fatal 退出（2026-10-13 实测：geoip.metadb 下载超时 → 容器无法启动）。
#      镜像内置了这些文件（/root/.config/mihomo），优先本地补齐，避免外网依赖。
for f in geoip.dat geoip.metadb geosite.dat; do
  if [ ! -s "$CONFIG_DIR/$f" ] && [ -s "/root/.config/mihomo/$f" ]; then
    cp "/root/.config/mihomo/$f" "$CONFIG_DIR/$f"
    log "地理数据已补齐：$f"
  fi
done

# (b) 后台启动 mihomo
MIHOMO_BIN=$(command -v mihomo || true)
if [ -z "$MIHOMO_BIN" ]; then
  for candidate in /usr/local/bin/mihomo /bin/mihomo /mihomo; do
    if [ -x "$candidate" ]; then
      MIHOMO_BIN=$candidate
      break
    fi
  done
fi
if [ -z "$MIHOMO_BIN" ]; then
  log "镜像内找不到 mihomo 二进制，退出"
  exit 1
fi
log "启动 mihomo：$MIHOMO_BIN -d $CONFIG_DIR -f $CONFIG_FILE"
"$MIHOMO_BIN" -d "$CONFIG_DIR" -f "$CONFIG_FILE" &
MIHOMO_PID=$!

# (c) 轮询 external-controller 直至 /proxies 可用
ready=0
elapsed=0
while [ "$elapsed" -lt 60 ]; do
  if wget -q -O "$PROXIES_TMP" "$CONTROLLER/proxies"; then
    ready=1
    break
  fi
  if ! kill -0 "$MIHOMO_PID" 2>/dev/null; then
    log "mihomo 进程已退出（订阅配置可能无效）"
    wait "$MIHOMO_PID"
    exit 1
  fi
  elapsed=$((elapsed + 1))
  sleep 1
done
if [ "$ready" != "1" ]; then
  log "external-controller 60 秒内未就绪，保持订阅默认节点选择"
  wait "$MIHOMO_PID"
  exit 0
fi

# 字节级 URL 编码（仅保留 unreserved 字符）
urlencode() {
  _str=$1
  _out=""
  while [ -n "$_str" ]; do
    _c=${_str%"${_str#?}"}
    _str=${_str#?}
    case $_c in
      [a-zA-Z0-9._~-]) _out="$_out$_c" ;;
      *) _out="$_out$(printf '%%%02X' "'$_c")" ;;
    esac
  done
  printf '%s' "$_out"
}

# 用 nc 发 PUT（busybox wget 不支持自定义 method）；$1=编码后路径 $2=JSON body
http_put() {
  { printf 'PUT %s HTTP/1.1\r\n' "$1"
    printf 'Host: 127.0.0.1:9090\r\nContent-Type: application/json\r\nContent-Length: %s\r\nConnection: close\r\n\r\n' "${#2}"
    printf '%s' "$2"
  } | nc -w 5 127.0.0.1 9090
}

# (d) 找出全部 Selector 组，逐组锁定第一个匹配 MIHOMO_NODE_FILTER 的节点
awk '
{
  rest = $0
  all = ""
  gname = ""
  while (match(rest, /"all":\[("[^"]*",?)*\]|"name":"[^"]*"|"type":"[A-Za-z]+"/)) {
    tok = substr(rest, RSTART, RLENGTH)
    rest = substr(rest, RSTART + RLENGTH)
    if (tok ~ /^"all"/) {
      all = tok
      gname = ""
    } else if (tok ~ /^"name"/) {
      if (all != "" && gname == "") {
        gname = substr(tok, 9, length(tok) - 9)
      }
    } else {
      if (all != "" && gname != "" && tok == "\"type\":\"Selector\"") {
        print gname "\t" all
      }
      all = ""
      gname = ""
    }
  }
}
' "$PROXIES_TMP" | while IFS="$(printf '\t')" read -r group alltok; do
  names=$(printf '%s' "$alltok" | sed -e 's/^"all":\["//' -e 's/"\]$//' -e 's/","/\n/g')
  match=$(printf '%s\n' "$names" | grep -E -m 1 -e "$NODE_FILTER" || true)
  if [ -z "$match" ]; then
    log "代理组 [$group] 无匹配 [$NODE_FILTER] 的节点，保持默认"
    continue
  fi
  resp=$(http_put "/proxies/$(urlencode "$group")" "{\"name\":\"$match\"}" || true)
  status=$(printf '%s\n' "$resp" | head -1)
  case $status in
    *" 204"*) log "代理组 [$group] 已锁定节点 [$match]" ;;
    *) log "代理组 [$group] 锁定节点 [$match] 失败：$status（保持原节点）" ;;
  esac
done

# (e) 前台等待 mihomo
wait "$MIHOMO_PID"
