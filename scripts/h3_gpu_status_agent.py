#!/usr/bin/env python3
"""TubeFlow H3 node GPU status agent (read-only, stdlib only).

Deploy on each H3 GPU node alongside ComfyUI. It proxies ComfyUI's local
read-only endpoints into a single LAN-facing endpoint used by the TubeFlow
backend to make stall decisions (busy vs idle/dead).

Configuration via environment variables:
  H3_GPU_AGENT_BIND       listen address (default "0.0.0.0")
  H3_GPU_AGENT_PORT       listen port (default "8399")
  H3_GPU_AGENT_TOKEN      required Bearer token; requests without it get 401
  H3_GPU_AGENT_COMFY_URL  ComfyUI base URL (default "http://127.0.0.1:8188")

Endpoints:
  GET /health      -> {"status": "live"} (no auth, for local checks)
  GET /gpu-status  -> auth required; 200 with JSON body:
    {
      "comfyui_reachable": true,
      "queue_running": 1,
      "queue_pending": 0,
      "devices": [
        {"name": "...", "vram_total": 0, "vram_used": 0, "vram_free": 0}
      ]
    }
    If ComfyUI is unreachable: 200 with {"comfyui_reachable": false,
    "queue_running": null, "queue_pending": null, "devices": []}.
    The backend treats HTTP failure/timeout as probe failure and
    comfyui_reachable=false as "node process dead".
"""

import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BIND = os.environ.get("H3_GPU_AGENT_BIND", "0.0.0.0")
PORT = int(os.environ.get("H3_GPU_AGENT_PORT", "8399"))
TOKEN = os.environ.get("H3_GPU_AGENT_TOKEN", "")
COMFY_URL = os.environ.get("H3_GPU_AGENT_COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
COMFY_TIMEOUT_SECONDS = 5.0


def _fetch_json(path: str) -> dict:
    with urllib.request.urlopen(COMFY_URL + path, timeout=COMFY_TIMEOUT_SECONDS) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _gpu_status() -> dict:
    try:
        queue = _fetch_json("/queue")
        stats = _fetch_json("/system_stats")
    except Exception:
        return {
            "comfyui_reachable": False,
            "queue_running": None,
            "queue_pending": None,
            "devices": [],
        }
    devices = [
        {
            "name": dev.get("name", ""),
            "vram_total": dev.get("vram_total", 0),
            "vram_used": dev.get("vram_used", 0),
            "vram_free": dev.get("vram_free", 0),
        }
        for dev in stats.get("devices", [])
    ]
    return {
        "comfyui_reachable": True,
        "queue_running": len(queue.get("queue_running", [])),
        "queue_pending": len(queue.get("queue_pending", [])),
        "devices": devices,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "TubeFlowH3GpuAgent/1.0"

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 (stdlib handler API)
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, {"status": "live"})
            return
        if path != "/gpu-status":
            self._send(404, {"error": "not_found"})
            return
        if not TOKEN or self.headers.get("Authorization") != f"Bearer {TOKEN}":
            self._send(401, {"error": "unauthorized"})
            return
        self._send(200, _gpu_status())

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    if not TOKEN:
        raise SystemExit("H3_GPU_AGENT_TOKEN must be set")
    server = ThreadingHTTPServer((BIND, PORT), Handler)
    print(f"gpu-status agent listening on {BIND}:{PORT}, proxying {COMFY_URL}")
    server.serve_forever()


if __name__ == "__main__":
    main()
