'use client'

import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { BookOpen, FileText, Plus, Search, ShieldCheck, Trash2, Upload } from 'lucide-react'
import {
  Badge, Button, Card, CardBody, ConfirmDialog, DialogContent, EmptyState, Input, Label, PageHeader, Select,
  Skeleton, Textarea, toast,
} from '@/components/ui'
import { api } from '@/lib/api'
import { cn } from '@/lib/cn'

// ACL 角色主体候选（M50-B2）：平台约定角色——API Key 通道 = admin、嵌入会话 = embed
// （api/deps.py），JWT 角色来自 IdP claim（member 为常规只读角色）。角色无目录端点，
// datalist 仅作提示，仍支持自由输入任意角色名；'*' = 通配所有主体。
const ACL_ROLE_SUGGESTIONS = ['*', 'admin', 'member', 'embed']

interface KbItem {
  id: number
  name: string
  title: string
  template: string
  embedding_provider: string
}

interface DocItem {
  id: number
  title: string
  source: string
  meta: Record<string, unknown>
}

interface AclRule {
  id: number
  effect: 'allow' | 'deny'
  subject_type: 'role' | 'user'
  subject: string
  document_id: number | null
  document: string
  note: string
}

interface Hit {
  content: string
  score: number
  citation: { kb?: string; document?: string; chunk_index?: number; chunk_id?: number }
}

/** 知识库页（RAGFlow/MaxKB 体验）：库卡片 → 库详情（文档 + 摄入 + 召回测试） */
export default function KnowledgePage() {
  const [kbs, setKbs] = useState<KbItem[] | null>(null)
  const [active, setActive] = useState<string | null>(null)
  const [createOpen, setCreateOpen] = useState(false)
  const [form, setForm] = useState({ name: '', title: '', template: 'doc' })

  const loadKbs = useCallback(async () => {
    try {
      const list = await api<KbItem[]>('GET', '/api/v1/kb')
      setKbs(list)
    } catch (e) {
      toast.error(`加载知识库失败：${(e as Error).message}`)
      setKbs([])
    }
  }, [])
  useEffect(() => { loadKbs() }, [loadKbs])

  const createKb = async () => {
    try {
      await api('POST', '/api/v1/kb', form)
      toast.success(`知识库 ${form.name} 已创建`)
      setCreateOpen(false)
      setForm({ name: '', title: '', template: 'doc' })
      loadKbs()
    } catch (e) {
      toast.error(`创建失败：${(e as Error).message}`)
    }
  }

  if (active) {
    return <KbDetail name={active} onBack={() => { setActive(null); loadKbs() }} />
  }

  return (
    <div>
      <PageHeader title="知识库" description="文档摄入 / 三路混合检索 / 引用溯源"
        actions={<Button variant="primary" onClick={() => setCreateOpen(true)}><Plus className="size-3.5" />新建知识库</Button>} />
      {!kbs ? (
        <div className="grid grid-cols-3 gap-4">
          <Skeleton className="h-28" /><Skeleton className="h-28" /><Skeleton className="h-28" />
        </div>
      ) : kbs.length === 0 ? (
        <Card><EmptyState title="还没有知识库" description="创建第一个知识库并摄入文档" icon={<BookOpen className="size-10" />} /></Card>
      ) : (
        <div className="grid grid-cols-3 gap-4">
          {kbs.map(kb => (
            <Card key={kb.name} className="cursor-pointer transition-shadow hover:shadow-md"
              onClick={() => setActive(kb.name)}>
              <CardBody>
                <div className="flex items-center gap-2.5">
                  <span className="flex size-9 items-center justify-center rounded-lg bg-emerald-50 text-emerald-600 dark:bg-emerald-900/40 dark:text-emerald-300">
                    <BookOpen className="size-4.5" />
                  </span>
                  <div className="min-w-0">
                    <p className="truncate text-sm font-semibold text-ink">{kb.title || kb.name}</p>
                    <p className="text-[11px] text-ink-3">{kb.name}</p>
                  </div>
                </div>
                <div className="mt-3 flex items-center gap-1.5">
                  <Badge tone={kb.template === 'faq' ? 'amber' : 'green'}>{kb.template}</Badge>
                  <Badge tone="gray">{kb.embedding_provider}</Badge>
                </div>
              </CardBody>
            </Card>
          ))}
        </div>
      )}

      <DialogContent open={createOpen} onOpenChange={setCreateOpen}
        title="新建知识库" description="模板决定摄入形态：doc 按分块索引 / faq 问答对整条成块"
        footer={<>
          <Button variant="ghost" onClick={() => setCreateOpen(false)}>取消</Button>
          <Button variant="primary" onClick={createKb} disabled={!form.name}>创建</Button>
        </>}>
        <div className="space-y-3">
          <div>
            <Label>名称（小写字母/数字/连字符）</Label>
            <Input value={form.name} placeholder="product-docs"
              onChange={e => setForm({ ...form, name: e.target.value })} />
          </div>
          <div>
            <Label>显示名</Label>
            <Input value={form.title} placeholder="产品文档库"
              onChange={e => setForm({ ...form, title: e.target.value })} />
          </div>
          <div>
            <Label>模板</Label>
            <Select value={form.template} onChange={e => setForm({ ...form, template: e.target.value })}>
              <option value="doc">doc（文档分块）</option>
              <option value="faq">faq（问答对）</option>
            </Select>
          </div>
        </div>
      </DialogContent>
    </div>
  )
}

function KbDetail({ name, onBack }: { name: string; onBack: () => void }) {
  const subjectListId = useId()
  const [docs, setDocs] = useState<DocItem[]>([])
  const [loading, setLoading] = useState(true)
  const [ingestOpen, setIngestOpen] = useState(false)
  const [ingestTitle, setIngestTitle] = useState('')
  const [ingestText, setIngestText] = useState('')
  const [ingesting, setIngesting] = useState(false)
  const [uploading, setUploading] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)
  const [parser, setParser] = useState('local')
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<Hit[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [previewDoc, setPreviewDoc] = useState<{ id: number; title: string } | null>(null)
  const [aclOpen, setAclOpen] = useState(false)
  const [acls, setAcls] = useState<AclRule[]>([])
  const [aclForm, setAclForm] = useState<{ effect: string; subject_type: string; subject: string; document_id: string }>(
    { effect: 'deny', subject_type: 'role', subject: '', document_id: '' })
  const [labels, setLabels] = useState<Record<number, boolean>>({})
  const [savingDataset, setSavingDataset] = useState(false)

  const loadDocs = useCallback(async () => {
    try {
      setDocs(await api<DocItem[]>('GET', `/api/v1/kb/${encodeURIComponent(name)}/documents`))
    } catch (e) {
      toast.error(`加载文档失败：${(e as Error).message}`)
    } finally {
      setLoading(false)
    }
  }, [name])
  useEffect(() => { loadDocs() }, [loadDocs])

  const ingest = async () => {
    if (!ingestTitle.trim() || !ingestText.trim()) {
      toast.error('标题与正文均必填')
      return
    }
    setIngesting(true)
    try {
      await api('POST', `/api/v1/kb/${encodeURIComponent(name)}/documents`,
        { title: ingestTitle, text: ingestText })
      toast.success('文档已摄入（分块 → 嵌入 → 图谱索引）')
      setIngestOpen(false)
      setIngestTitle('')
      setIngestText('')
      loadDocs()
    } catch (e) {
      toast.error(`摄入失败：${(e as Error).message}`)
    } finally {
      setIngesting(false)
    }
  }

  // 破坏性删除需确认（M51-B）：删除按钮只置目标，ConfirmDialog 确认后执行
  const [removeDocTarget, setRemoveDocTarget] = useState<DocItem | null>(null)
  const [removingDoc, setRemovingDoc] = useState(false)
  const removeDoc = async () => {
    if (!removeDocTarget) return
    setRemovingDoc(true)
    try {
      await api('DELETE', `/api/v1/kb/${encodeURIComponent(name)}/documents/${removeDocTarget.id}`)
      toast.success('文档已删除（chunk/图谱/向量级联清理）')
      setRemoveDocTarget(null)
      loadDocs()
    } catch (e) {
      toast.error(`删除失败：${(e as Error).message}`)
    } finally {
      setRemovingDoc(false)
    }
  }

  // ACL 规则删除同样过确认（M51-B）
  const [removeAclTarget, setRemoveAclTarget] = useState<AclRule | null>(null)
  const [removingAcl, setRemovingAcl] = useState(false)
  const removeAcl = async () => {
    if (!removeAclTarget) return
    setRemovingAcl(true)
    try {
      await api('DELETE', `/api/v1/kb/acls/${removeAclTarget.id}`)
      setAcls(await api<AclRule[]>('GET', `/api/v1/kb/${encodeURIComponent(name)}/acls`))
      toast.success('ACL 规则已删除')
      setRemoveAclTarget(null)
    } catch (e) {
      toast.error(`删除失败：${(e as Error).message}`)
    } finally {
      setRemovingAcl(false)
    }
  }

  const uploadFile = async (file: File) => {
    setUploading(true)
    try {
      const body = new FormData()
      body.append('file', file)
      if (parser !== 'local') body.append('parser', parser)
      const r = await fetch(`/api/v1/kb/${encodeURIComponent(name)}/upload`, {
        method: 'POST',
        headers: { Authorization: `Bearer ${localStorage.getItem('eap-token') ?? ''}` },
        body,
      })
      const d = await r.json().catch(() => ({}))
      if (!r.ok) throw new Error(d.detail ?? `HTTP ${r.status}`)
      toast.success(`${file.name} 已上传（${parser} 后端），解析与摄入异步执行中`)
      // 轮询摄入任务完成后刷新文档列表
      const poll = setInterval(async () => {
        try {
          const task = await api<{ state: string }>('GET', `/api/v1/tasks/${d.task_id}`)
          if (task.state === 'COMPLETED') {
            clearInterval(poll)
            toast.success('摄入完成')
            loadDocs()
          } else if (task.state === 'FAILED') {
            clearInterval(poll)
            toast.error('摄入失败（解析错误或格式不支持）')
            loadDocs()
          }
        } catch { clearInterval(poll) }
      }, 1000)
      loadDocs()
    } catch (e) {
      toast.error(`上传失败：${(e as Error).message}`)
    } finally {
      setUploading(false)
    }
  }

  const search = async () => {
    if (!query.trim()) return
    setSearching(true)
    try {
      const r = await api<{ hits: Hit[] }>('POST', `/api/v1/kb/${encodeURIComponent(name)}/retrieve`,
        { query, top_k: 5 })
      setHits(r.hits)
      setLabels({})
    } catch (e) {
      toast.error(`检索失败：${(e as Error).message}`)
    } finally {
      setSearching(false)
    }
  }

  // RAG 标注（v0.6）：勾选相关 chunk → 存为 RAG 评测数据集
  const saveLabeledDataset = async () => {
    const relevant = Object.entries(labels).filter(([, v]) => v).map(([k]) => Number(k))
    if (!relevant.length || !query.trim()) {
      toast.error('请先检索并勾选相关 chunk')
      return
    }
    setSavingDataset(true)
    try {
      const stamp = new Date().toISOString().slice(5, 16).replace(/[-:T]/g, '').toLowerCase()
      await api('POST', '/api/v1/evals/datasets', {
        name: `rag-${name.slice(0, 16)}-${stamp}`,
        kind: 'rag',
        description: `标注来源：${name} · 查询「${query.slice(0, 40)}」`,
        cases: [{ query, relevant_chunk_ids: relevant }],
      })
      toast.success(`已存为 RAG 数据集（${relevant.length} 个相关 chunk）`)
    } catch (e) {
      toast.error(`保存失败：${(e as Error).message}`)
    } finally {
      setSavingDataset(false)
    }
  }

  return (
    <div>
      <PageHeader title={name} description="文档管理 / 召回测试"
        actions={<Button variant="secondary" onClick={onBack}>返回列表</Button>} />

      <div className="grid grid-cols-[1fr_400px] items-start gap-4">
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <p className="flex items-center gap-1.5 text-sm font-semibold text-ink">
              <FileText className="size-4 text-ink-3" />文档（{docs.length}）
            </p>
            <div className="flex items-center gap-2">
              <Select value={parser} onChange={e => setParser(e.target.value)}
                title="解析后端" className="w-40">
                <option value="local">local（内置）</option>
                <option value="mineru_cloud">MinerU 云端</option>
                <option value="mineru_selfhosted">MinerU 自托管</option>
              </Select>
              <input ref={fileRef} type="file" accept=".pdf,.docx,.txt,.md" className="hidden"
                onChange={e => {
                  const f = e.target.files?.[0]
                  if (f) void uploadFile(f)
                  e.target.value = ''
                }} />
              <Button variant="secondary" onClick={() => fileRef.current?.click()} loading={uploading}>
                <Upload className="size-3.5" />上传文件
              </Button>
              <Button variant="primary" onClick={() => setIngestOpen(true)}>
                <Plus className="size-3.5" />粘贴文本
              </Button>
              <Button variant="secondary" onClick={async () => {
                setAclOpen(true)
                setAcls(await api<AclRule[]>('GET', `/api/v1/kb/${encodeURIComponent(name)}/acls`))
              }}>
                <ShieldCheck className="size-3.5" />访问控制
              </Button>
            </div>
          </div>
          <div className="rounded-[--radius-card] border border-line bg-surface">
            {loading ? (
              <div className="space-y-2 p-4"><Skeleton className="h-10" /><Skeleton className="h-10" /></div>
            ) : docs.length === 0 ? (
              <EmptyState title="暂无文档" description="摄入纯文本后自动分块、嵌入并建立图谱索引" />
            ) : (
              <div className="divide-y divide-line">
                {docs.map(d => (
                  <div key={d.id} className="flex items-center gap-3 px-4 py-3">
                    <button className="min-w-0 flex-1 cursor-pointer text-left"
                      onClick={() => setPreviewDoc({ id: d.id, title: d.title })}>
                      <FileText className="mr-2 inline size-4 text-ink-3" />
                      <span className="truncate text-[13px] font-medium text-ink">{d.title}</span>
                      <p className="text-[11px] text-ink-3">
                        {d.source || 'text'}{(d.meta as { type?: string })?.type ? ` · ${(d.meta as { type?: string }).type}` : ''} · 点击查看分块
                      </p>
                    </button>
                    <Badge tone={(d.meta as { type?: string })?.type === 'faq' ? 'amber' : 'green'}>
                      {(d.meta as { type?: string })?.type === 'faq' ? 'FAQ' : '已索引'}
                    </Badge>
                    <Button size="xs" variant="ghost" onClick={() => setRemoveDocTarget(d)} title="删除">
                      <Trash2 className="size-3.5" />
                    </Button>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>

        <Card>
          <CardBody className="space-y-3">
            <p className="flex items-center gap-1.5 text-sm font-semibold text-ink">
              <Search className="size-4 text-brand-500" />召回测试
            </p>
            <Textarea rows={2} value={query} placeholder="输入查询，验证三路混合检索…"
              onChange={e => setQuery(e.target.value)} />
            <Button variant="primary" className="w-full" onClick={search} loading={searching}>
              检索 top 5
            </Button>
            {hits !== null && (
              <div className="space-y-2">
                {hits.length === 0 && <p className="py-2 text-center text-xs text-ink-3">无命中</p>}
                {hits.map((h, i) => (
                  <div key={i} className="rounded-lg border border-line bg-surface-2 p-2.5">
                    <div className="mb-1 flex items-center justify-between">
                      <Badge tone="brand">[{i + 1}] {(h.citation.document ?? '').slice(0, 24)}</Badge>
                      <span className="text-[11px] text-ink-3">score {h.score.toFixed(3)}</span>
                    </div>
                    <p className="line-clamp-3 text-[12px] text-ink-2">{h.content}</p>
                    <div className="mt-1.5 flex items-center gap-1.5">
                      <span className="text-[10px] text-ink-3">标注：</span>
                      <button onClick={() => setLabels(l => ({ ...l, [h.citation.chunk_id ?? -1]: true }))}
                        className={cn('cursor-pointer rounded px-1.5 py-0.5 text-[10px] border transition-colors',
                          labels[h.citation.chunk_id ?? -1] === true
                            ? 'border-emerald-500 bg-emerald-50 text-emerald-600 dark:bg-emerald-900/30 dark:text-emerald-300'
                            : 'border-line text-ink-3 hover:text-ink')}>相关</button>
                      <button onClick={() => setLabels(l => ({ ...l, [h.citation.chunk_id ?? -1]: false }))}
                        className={cn('cursor-pointer rounded px-1.5 py-0.5 text-[10px] border transition-colors',
                          labels[h.citation.chunk_id ?? -1] === false
                            ? 'border-red-400 bg-red-50 text-red-500 dark:bg-red-900/30'
                            : 'border-line text-ink-3 hover:text-ink')}>不相关</button>
                      <span className="text-[10px] text-ink-3">chunk_id: {h.citation.chunk_id}</span>
                    </div>
                  </div>
                ))}
                {hits.length > 0 && Object.keys(labels).length > 0 && (
                  <Button variant="secondary" className="w-full" loading={savingDataset}
                    onClick={saveLabeledDataset}>
                    存为 RAG 评测数据集（标注 {Object.values(labels).filter(Boolean).length} 条相关）
                  </Button>
                )}
              </div>
            )}
          </CardBody>
        </Card>
      </div>

      {/* 分块预览抽屉（M14） */}
      {previewDoc && (
        <ChunksDrawer kb={name} doc={previewDoc}
          onClose={() => setPreviewDoc(null)} />
      )}

      <DialogContent open={ingestOpen} onOpenChange={setIngestOpen}
        title={`摄入文档 · ${name}`} description="纯文本摄入：自动分块 → 嵌入 → 三路索引"
        footer={<>
          <Button variant="ghost" onClick={() => setIngestOpen(false)}>取消</Button>
          <Button variant="primary" onClick={ingest} loading={ingesting}>摄入</Button>
        </>}>
        <div className="space-y-3">
          <div>
            <Label>文档标题</Label>
            <Input value={ingestTitle} onChange={e => setIngestTitle(e.target.value)} placeholder="产品手册第一章" />
          </div>
          <div>
            <Label>正文（空行分段有助于分块质量）</Label>
            <Textarea rows={10} value={ingestText} onChange={e => setIngestText(e.target.value)} />
          </div>
        </div>
      </DialogContent>

      <DialogContent open={aclOpen} onOpenChange={setAclOpen}
        title={`访问控制 · ${name}`} description="deny 优先 → allow → 默认可见（无规则对租户内全部可见）"
        footer={<Button variant="ghost" onClick={() => setAclOpen(false)}>关闭</Button>}>
        <div className="space-y-3">
          <div className="grid grid-cols-[80px_90px_1fr_1fr_auto] items-end gap-2">
            <div>
              <Label>效果</Label>
              <Select value={aclForm.effect} onChange={e => setAclForm({ ...aclForm, effect: e.target.value })}>
                <option value="deny">deny</option>
                <option value="allow">allow</option>
              </Select>
            </div>
            <div>
              <Label>主体类型</Label>
              <Select value={aclForm.subject_type}
                onChange={e => setAclForm({ ...aclForm, subject_type: e.target.value })}>
                <option value="role">角色</option>
                <option value="user">用户</option>
              </Select>
            </div>
            <div>
              <Label>主体（* = 所有）</Label>
              {/* role：datalist 给平台约定角色 + '*'（仍自由输入）；user：无目录端点，如实提示 */}
              <Input value={aclForm.subject}
                list={aclForm.subject_type === 'role' ? subjectListId : undefined}
                onChange={e => setAclForm({ ...aclForm, subject: e.target.value })}
                placeholder={aclForm.subject_type === 'role' ? 'admin / member / *' : 'u-123（用户 ID）'} />
              <datalist id={subjectListId}>
                {ACL_ROLE_SUGGESTIONS.map(r => <option key={r} value={r} />)}
              </datalist>
              {aclForm.subject_type === 'user' && (
                <p className="mt-1 text-[10px] leading-tight text-ink-3">
                  用户 ID 来自 IdP 的 sub 声明，平台无用户目录端点，需手动填写精确值
                </p>
              )}
            </div>
            <div>
              <Label>文档（空=整库默认）</Label>
              <Select value={aclForm.document_id}
                onChange={e => setAclForm({ ...aclForm, document_id: e.target.value })}>
                <option value="">整库默认</option>
                {docs.map(d => <option key={d.id} value={d.id}>{d.title}</option>)}
              </Select>
            </div>
            <Button variant="primary" disabled={!aclForm.subject}
              onClick={async () => {
                try {
                  await api('POST', `/api/v1/kb/${encodeURIComponent(name)}/acls`, {
                    effect: aclForm.effect, subject_type: aclForm.subject_type,
                    subject: aclForm.subject,
                    document_id: aclForm.document_id ? Number(aclForm.document_id) : null,
                  })
                  setAcls(await api<AclRule[]>('GET', `/api/v1/kb/${encodeURIComponent(name)}/acls`))
                  setAclForm({ ...aclForm, subject: '' })
                  toast.success('ACL 规则已创建')
                } catch (e) { toast.error(`创建失败：${(e as Error).message}`) }
              }}>添加</Button>
          </div>
          <div className="divide-y divide-line rounded-lg border border-line">
            {acls.length === 0 ? (
              <p className="p-3 text-[12px] text-ink-3">暂无规则（默认可见）</p>
            ) : acls.map(a => (
              <div key={a.id} className="flex items-center gap-2 px-3 py-2 text-[12px]">
                <Badge tone={a.effect === 'deny' ? 'red' : 'green'}>{a.effect}</Badge>
                <span className="text-ink">{a.subject_type}:{a.subject}</span>
                <span className="text-ink-3">→ {a.document}</span>
                <Button size="xs" variant="ghost" className="ml-auto" title="删除规则"
                  onClick={() => setRemoveAclTarget(a)}><Trash2 className="size-3.5" /></Button>
              </div>
            ))}
          </div>
        </div>
      </DialogContent>

      <ConfirmDialog open={!!removeDocTarget} onCancel={() => setRemoveDocTarget(null)}
        title={`删除文档「${removeDocTarget?.title ?? ''}」？`}
        description="将级联清理该文档的全部分块、向量索引与图谱关系，删除后不可恢复；引用该文档的检索结果随即消失。"
        confirmLabel="删除" busy={removingDoc} onConfirm={removeDoc} />

      <ConfirmDialog open={!!removeAclTarget} onCancel={() => setRemoveAclTarget(null)}
        title="删除该 ACL 规则？"
        description={removeAclTarget
          ? `规则「${removeAclTarget.effect} ${removeAclTarget.subject_type}:${removeAclTarget.subject}」删除后立即按剩余规则生效（无规则时租户内默认可见）。`
          : undefined}
        confirmLabel="删除" busy={removingAcl} onConfirm={removeAcl} />
    </div>
  )
}

/** 分块预览抽屉：文档 chunk 列表（内容/索引），调参验证的分块效果直接可见 */
function ChunksDrawer({ kb, doc, onClose }: {
  kb: string
  doc: { id: number; title: string }
  onClose: () => void
}) {
  const [chunks, setChunks] = useState<{ id: number; idx: number; content: string }[]>([])
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    api<{ id: number; idx: number; content: string }[]>(
      'GET', `/api/v1/kb/${encodeURIComponent(kb)}/documents/${doc.id}/chunks`)
      .then(setChunks)
      .catch(e => toast.error(`加载分块失败：${(e as Error).message}`))
      .finally(() => setLoading(false))
  }, [kb, doc.id])

  return (
    <DialogContent open onOpenChange={o => !o && onClose()}
      title={`分块预览 · ${doc.title}`} wide
      description={`${kb} / 文档 #${doc.id} · ${chunks.length} 块`}>
      {loading ? (
        <div className="space-y-2"><Skeleton className="h-12" /><Skeleton className="h-12" /></div>
      ) : chunks.length === 0 ? (
        <p className="py-6 text-center text-xs text-ink-3">该文档没有分块</p>
      ) : (
        <div className="space-y-2">
          {chunks.map(c => (
            <div key={c.id} className="rounded-lg border border-line bg-surface-2 p-2.5">
              <p className="mb-1 flex items-center justify-between text-[11px] text-ink-3">
                <Badge tone="gray">#{c.idx}</Badge>
                <span>{c.content.length} 字符</span>
              </p>
              <p className="whitespace-pre-wrap text-[12px] text-ink-2">{c.content}</p>
            </div>
          ))}
        </div>
      )}
    </DialogContent>
  )
}
