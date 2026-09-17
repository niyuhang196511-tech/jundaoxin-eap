# EAP 平台镜像（纯 API 后端；控制台前端独立构建，见 frontend/Dockerfile + docker-compose frontend 服务）
# 构建：docker build -t eap:latest .
# 运行：docker run -p 8300:8300 --env-file .env eap:latest
# 或用 docker-compose.yml（含 PostgreSQL）

FROM python:3.12-slim AS runtime
WORKDIR /app

# uv 安装依赖（锁定的可复现环境；postgres extra 供生产库使用）
# 两段 sync：先装依赖层（源码不变即命中缓存），拷入源码后再安装 eap 包本身
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --extra postgres --extra redis --no-dev

# 源码
COPY src/ ./src/
RUN uv sync --frozen --extra postgres --extra redis --no-dev

# 非 root 运行
RUN useradd -r eap && chown -R eap:eap /app
USER eap

EXPOSE 8300
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8300/health')"]

# 生产：多副本 + Gunicorn/UVicorn worker（docs/09 §部署拓扑）；单机先用单进程
CMD [".venv/bin/python", "-m", "eap"]
