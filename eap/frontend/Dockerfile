# EAP 控制台镜像（Next.js 独立部署，与后端分开构建/发布）
# 构建：docker build -t eap-console:latest --build-arg NEXT_PUBLIC_API_BASE_URL=http://<后端地址>:8300 .
# 运行：docker run -p 3000:3000 eap-console:latest

FROM node:22-alpine AS builder
WORKDIR /app
RUN corepack enable

COPY package.json pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY . .
# NEXT_PUBLIC_* 在构建期注入 bundle
ARG NEXT_PUBLIC_API_BASE_URL=""
ARG NEXT_PUBLIC_EAP_TOKEN=""
ENV NEXT_OUTPUT=standalone \
    NEXT_PUBLIC_API_BASE_URL=$NEXT_PUBLIC_API_BASE_URL \
    NEXT_PUBLIC_EAP_TOKEN=$NEXT_PUBLIC_EAP_TOKEN
RUN pnpm build

FROM node:22-alpine AS runner
WORKDIR /app
ENV NODE_ENV=production HOSTNAME=0.0.0.0 PORT=3000
COPY --from=builder /app/.next/standalone ./
COPY --from=builder /app/.next/static ./.next/static
USER node
EXPOSE 3000
CMD ["node", "server.js"]
