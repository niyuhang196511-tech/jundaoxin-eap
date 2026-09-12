# EAP 平台镜像（多阶段：前端构建 → Python 运行时）
# 构建：docker build -t eap:latest .
# 运行：docker run -p 8300:8300 --env-file .env eap:latest
# 或用 docker-compose.yml（含 PostgreSQL）

# ---- 阶段 1：前端控制台构建 ----
FROM node:22-alpine AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build   # 产物输出到 /app/src/eap/static/console（vite outDir）

# ---- 阶段 2：Python 运行时 ----
FROM python:3.12-slim AS runtime
WORKDIR /app

# uv 安装依赖（锁定的可复现环境；postgres extra 供生产库使用）
# 两段 sync：先装依赖层（源码不变即命中缓存），拷入源码后再安装 eap 包本身
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --extra postgres --no-dev

# 源码与前端产物
COPY src/ ./src/
RUN uv sync --frozen --extra postgres --no-dev
COPY --from=frontend /app/src/eap/static/console ./src/eap/static/console

# 非 root 运行
RUN useradd -r eap && chown -R eap:eap /app
USER eap

EXPOSE 8300
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s \
  CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8300/health')"]

# 生产：多副本 + Gunicorn/UVicorn worker（docs/09 §部署拓扑）；单机先用单进程
CMD [".venv/bin/python", "-m", "eap"]
