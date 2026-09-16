'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Button, Input, Label } from '@/components/ui'
import { api, getToken, setToken } from '@/lib/api'

/**
 * 登录页（M6）：
 * - 企业账号登录：直接跳后端 /auth/oidc/login（307 → IdP 授权页），
 *   回调换发 API Key 后由 IdP/回调页带 token 回控制台
 * - 开发模式：直接粘贴 API Key（dev-key-1）
 */
export default function LoginPage() {
  const router = useRouter()
  const [ssoReady, setSsoReady] = useState(false)
  const [token, setTokenState] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    // 已登录直接进控制台
    if (getToken()) {
      router.replace('/agents')
      return
    }
    // 探测 OIDC 是否已配置（501 = 未配置 → 隐藏 SSO 入口）
    fetch(`${process.env.NEXT_PUBLIC_API_BASE_URL ?? ''}/api/v1/auth/oidc/login`, {
      method: 'GET', redirect: 'manual',
    })
      .then(r => setSsoReady(r.status === 307 || r.status === 302 || r.ok))
      .catch(() => setSsoReady(false))
  }, [router])

  const devLogin = async () => {
    setLoading(true)
    setError('')
    try {
      // 验证凭证有效性
      await api('GET', '/api/v1/conversations', undefined)
      setToken(token.trim())
      router.replace('/agents')
    } catch {
      setError('凭证无效或后端不可达')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-bg p-6">
      <div className="w-full max-w-sm rounded-[--radius-card] border border-line bg-surface p-8 shadow-(--shadow-card)">
        <div className="mb-6 flex items-center gap-3">
          <div className="flex size-11 items-center justify-center rounded-xl bg-gradient-to-br from-brand-500 to-violet-500 text-[15px] font-bold text-white">
            EA
          </div>
          <div>
            <h1 className="text-lg font-semibold text-ink">EAP 控制台</h1>
            <p className="text-xs text-ink-3">企业级 Agent 智能体平台</p>
          </div>
        </div>

        {ssoReady && (
          <a href="/api/v1/auth/oidc/login" className="block">
            <Button variant="primary" size="md" className="w-full">使用企业账号登录（SSO）</Button>
          </a>
        )}

        {ssoReady && <div className="my-5 flex items-center gap-3 text-[11px] text-ink-3">
          <span className="h-px flex-1 bg-line" />或使用 API Key<span className="h-px flex-1 bg-line" />
        </div>}

        <div className="space-y-3">
          <div>
            <Label>API Key</Label>
            <Input type="password" value={token} placeholder="dev-key-1 / eap_u_…"
              onChange={e => setTokenState(e.target.value)}
              onKeyDown={e => e.key === 'Enter' && devLogin()} />
          </div>
          {error && <p className="text-xs text-red-500">{error}</p>}
          <Button variant="secondary" size="md" className="w-full" onClick={devLogin} loading={loading} disabled={!token.trim()}>
            进入控制台
          </Button>
        </div>
      </div>
    </div>
  )
}
