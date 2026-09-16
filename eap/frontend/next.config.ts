import type { NextConfig } from 'next'

// 独立部署：容器内以 NEXT_OUTPUT=standalone 构建，产物可经 node server.js 直接运行（见 Dockerfile）；
// Windows 本机构建不开 standalone（symlink 权限受限），本地用 pnpm start 即可
const nextConfig: NextConfig = {
  output: process.env.NEXT_OUTPUT === 'standalone' ? 'standalone' : undefined,
}

export default nextConfig
