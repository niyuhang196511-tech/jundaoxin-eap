'use client'

import { createContext, useContext } from 'react'

/**
 * i18n 基建（M13）：轻量词典方案（无运行时依赖）。
 *
 * 用法：
 *   const { t } = useI18n()
 *   t('sidebar.agents')          // 缺失 key 原样返回（增量翻译不阻断）
 *
 * 增量策略：布局壳与登录页已接入；各页面文案迁移按需进行——
 * 词典里加 `en` 条目 + 页面字符串替换为 t('key') 即可。
 */

export type Locale = 'zh' | 'en'

const dict: Record<Locale, Record<string, string>> = {
  zh: {
    'app.name': 'EAP 控制台',
    'app.subtitle': '企业级 Agent 智能体平台',
    'nav.studio': '工作室',
    'nav.agents': '智能体',
    'nav.canvas': 'Workflow 画布',
    'nav.assets': '技能 · Prompt',
    'nav.extensions': '扩展中心',
    'nav.knowledge': '知识',
    'nav.kb': '知识库',
    'nav.ops': '运营',
    'nav.tasks': '任务 · 审批',
    'nav.evals': '评测',
    'nav.gov': '治理 · 成本',
    'nav.audit': '审计日志',
    'nav.models': '模型中心',
    'nav.conn': '连接器 · IM',
    'login.title': '进入控制台',
    'login.apiKey': 'API Key',
    'login.sso': '使用企业账号登录（SSO）',
  },
  en: {
    'app.name': 'EAP Console',
    'app.subtitle': 'Enterprise Agent Platform',
    'nav.studio': 'Studio',
    'nav.agents': 'Agents',
    'nav.canvas': 'Workflow Canvas',
    'nav.assets': 'Skills · Prompts',
    'nav.extensions': 'Extensions',
    'nav.knowledge': 'Knowledge',
    'nav.kb': 'Knowledge Bases',
    'nav.ops': 'Operations',
    'nav.tasks': 'Tasks · Approvals',
    'nav.evals': 'Evaluations',
    'nav.gov': 'Governance · Cost',
    'nav.audit': 'Audit Log',
    'nav.models': 'Model Hub',
    'nav.conn': 'Connectors · IM',
    'login.title': 'Enter Console',
    'login.apiKey': 'API Key',
    'login.sso': 'Sign in with SSO',
  },
}

interface I18nContextValue {
  locale: Locale
  t: (key: string) => string
}

const I18nContext = createContext<I18nContextValue>({ locale: 'zh', t: k => dict.zh[k] ?? k })

export function I18nProvider({ locale, children }: { locale: Locale; children: React.ReactNode }) {
  const value: I18nContextValue = {
    locale,
    t: key => dict[locale]?.[key] ?? dict.zh[key] ?? key,
  }
  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n() {
  return useContext(I18nContext)
}
