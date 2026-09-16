'use client'

// xyflow 画布依赖浏览器测量，跳过 SSR 动态加载
import dynamic from 'next/dynamic'

const Canvas = dynamic(() => import('@/views/WorkflowCanvas'), { ssr: false })

export default function CanvasPage() {
  return <Canvas />
}
