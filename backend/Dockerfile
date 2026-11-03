FROM python:3.12-slim
# 部署机到 deb.debian.org / pypi.org 的连接不稳定；改用国内镜像并强制 IPv4。
RUN sed -i 's|http://deb.debian.org|https://mirrors.ustc.edu.cn|g; s|http://security.debian.org|https://mirrors.ustc.edu.cn|g' /etc/apt/sources.list.d/debian.sources \
    && printf '%s\n' 'Acquire::ForceIPv4 "true"; Acquire::Retries "5";' > /etc/apt/apt.conf.d/99tubeflow \
    && apt-get update \
    && apt-get install --no-install-recommends -y ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/pyproject.toml backend/README.md ./
COPY backend/src ./src
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY index.html app.js styles.css ./frontend/
COPY frontend ./frontend/frontend
ENV TUBEFLOW_FRONTEND_DIR=/app/frontend
ENV PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=5
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple .
RUN ffmpeg -version >/dev/null && ffprobe -version >/dev/null
EXPOSE 8000
CMD ["tubeflow-test-api"]
