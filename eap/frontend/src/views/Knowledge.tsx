'use client'

import { useCallback, useEffect, useState } from 'react'
import { BookOpen, FileText, Plus, Search, Trash2 } from 'lucide-react'
import {
  Badge, Button, Card, CardBody, DialogContent, EmptyState, Input, Label, PageHeader, Skeleton,
  Textarea, toast,
} from '@/components/ui'
import { api } from '@/lib/api'

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

interface Hit {
  content: string
  score: number
  citation: { kb?: string; document?: string; chunk_index?: number }
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
            <select value={form.template} onChange={e => setForm({ ...form, template: e.target.value })}
              className="h-9 w-full rounded-lg border border-line bg-surface px-3 text-[13px] text-ink">
              <option value="doc">doc（文档分块）</option>
              <option value="faq">faq（问答对）</option>
            </select>
          </div>
        </div>
      </DialogContent>
    </div>
  )
}

function KbDetail({ name, onBack }: { name: string; onBack: () => void }) {
  const [docs, setDocs] = useState<DocItem[]>([])
  const [loading, setLoading] = useState(true)
  const [ingestOpen, setIngestOpen] = useState(false)
  const [ingestTitle, setIngestTitle] = useState('')
  const [ingestText, setIngestText] = useState('')
  const [ingesting, setIngesting] = useState(false)
  const [query, setQuery] = useState('')
  const [hits, setHits] = useState<Hit[] | null>(null)
  const [searching, setSearching] = useState(false)

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

  const removeDoc = async (docId: number) => {
    try {
      await api('DELETE', `/api/v1/kb/${encodeURIComponent(name)}/documents/${docId}`)
      toast.success('文档已删除（chunk/图谱/向量级联清理）')
      loadDocs()
    } catch (e) {
      toast.error(`删除失败：${(e as Error).message}`)
    }
  }

  const search = async () => {
    if (!query.trim()) return
    setSearching(true)
    try {
      const r = await api<{ hits: Hit[] }>('POST', `/api/v1/kb/${encodeURIComponent(name)}/retrieve`,
        { query, top_k: 5 })
      setHits(r.hits)
    } catch (e) {
      toast.error(`检索失败：${(e as Error).message}`)
    } finally {
      setSearching(false)
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
            <Button variant="primary" onClick={() => setIngestOpen(true)}>
              <Plus className="size-3.5" />摄入文档
            </Button>
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
                    <FileText className="size-4 shrink-0 text-ink-3" />
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-[13px] font-medium text-ink">{d.title}</p>
                      <p className="text-[11px] text-ink-3">
                        {d.source || 'text'}{(d.meta as { type?: string })?.type ? ` · ${(d.meta as { type?: string }).type}` : ''}
                      </p>
                    </div>
                    <Badge tone={(d.meta as { type?: string })?.type === 'faq' ? 'amber' : 'green'}>
                      {(d.meta as { type?: string })?.type === 'faq' ? 'FAQ' : '已索引'}
                    </Badge>
                    <Button size="xs" variant="ghost" onClick={() => removeDoc(d.id)} title="删除">
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
                  </div>
                ))}
              </div>
            )}
          </CardBody>
        </Card>
      </div>

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
    </div>
  )
}
