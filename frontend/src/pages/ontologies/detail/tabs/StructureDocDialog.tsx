import { Children, isValidElement, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { toast } from 'sonner'
import { AlertCircle, Download, FileText, Loader2, Pencil } from 'lucide-react'
import MermaidBlock from '@/components/MermaidBlock'
import ZoomableImage from '@/components/ZoomableImage'
import { CenterMorphModal, CenterMorphModalContent } from '@/components/motion-ui/center-morph-modal'
import { Alert } from '@/components/ui/Alert'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { ontologyVersionApi, type SaveOntologyDocumentBody } from '@/api/v2/ontology-versions'
import { splitMarkdownSections, tocSections, type StructureDocSection } from './structureDocSections'
import {
  buildDocumentSaveBody,
  classifyDocumentSaveError,
  documentByteLabel,
  DOCUMENT_MAX_BYTES,
  isBlankDocument,
  isDocumentDirty,
  isDocumentTooLarge,
  type DocumentSaveError,
} from './structureDocEdit'
import StructureDocEditor, { type StructureDocEditorHandle } from './StructureDocEditor'
import './ontology-dialogs.css'

const isMermaidEl = (child: unknown) =>
  isValidElement(child) && String((child.props as any)?.className || '').includes('language-mermaid')

/**
 * 需求文档正文排版，与本体建模页需求文档弹窗（pages/explore/Md.tsx）同一套
 * 视觉口径；因页面域之间禁止相互导入，这里按 ontologies 域内渲染器维护。
 * 文字取 odg-body-text（14px / 1.85 行距 / 深墨色）保证清晰易读。
 * renderDiagrams=false 时（编辑态实时预览）mermaid 不渲染：半成品图表随
 * 每次按键异步重绘代价高且必然失败，按普通代码块展示。
 */
function DocMarkdown({ text, renderDiagrams = true }: { text: string; renderDiagrams?: boolean }) {
  const components = useMemo<Components>(() => ({
    p: p => <p className="odg-body-text mb-2 last:mb-0" {...p} />,
    strong: p => <strong className="odg-body-text font-semibold" {...p} />,
    h1: p => <h2 className="text-base font-semibold mt-4 mb-2" {...p} />,
    h2: p => <h3 className="text-sm font-semibold mt-3 mb-1.5" {...p} />,
    h3: p => <h4 className="text-sm font-semibold mt-2 mb-1" {...p} />,
    ul: p => <ul className="list-disc pl-5 mb-2 space-y-1" {...p} />,
    ol: p => <ol className="list-decimal pl-5 mb-2 space-y-1" {...p} />,
    li: p => <li className="odg-body-text leading-relaxed" {...p} />,
    code: ({ className, children, ...p }) => {
      if (renderDiagrams && String(className || '').includes('language-mermaid')) {
        return <MermaidBlock chart={String(children).trim()} />
      }
      return <code className={`px-1 py-0.5 rounded bg-[var(--color-bg-overlay)] text-[12px] font-mono ${className || ''}`} {...p}>{children}</code>
    },
    pre: ({ children, ...p }) => {
      // mermaid 代码块由 MermaidBlock 接管，去掉 pre 包裹
      if (renderDiagrams && Children.toArray(children).some(isMermaidEl)) return <>{children}</>
      return <pre className="p-3 my-2 rounded-lg bg-[var(--color-bg-overlay)] text-[12px] font-mono overflow-x-auto" {...p}>{children}</pre>
    },
    table: p => (
      <div className="overflow-x-auto my-2 rounded-lg border border-[var(--color-border)]">
        <table className="w-full text-xs border-collapse" {...p} />
      </div>
    ),
    thead: p => <thead className="bg-[var(--color-bg-base)]" {...p} />,
    th: p => <th className="px-3 py-1.5 text-left font-medium text-[var(--color-text-secondary)] border-b border-[var(--color-border)] whitespace-nowrap" {...p} />,
    td: p => <td className="px-3 py-1.5 border-b border-[var(--color-border)]" {...p} />,
    a: p => <a className="text-[var(--color-primary)] underline-offset-2 hover:underline" {...p} />,
    blockquote: p => <blockquote className="border-l-2 border-[var(--color-border)] pl-3 my-2 text-[var(--color-text-secondary)]" {...p} />,
    img: ({ src, alt }) => <ZoomableImage src={src} alt={alt} />,
  }), [renderDiagrams])

  return (
    <div className="structure-doc-md">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>{text}</ReactMarkdown>
    </div>
  )
}

function CenterHint({ icon, title, hint, testid, retry }: {
  icon: React.ReactNode
  title: string
  hint: string
  testid: string
  retry?: () => void
}) {
  return (
    <div data-testid={testid} className="flex h-full flex-col items-center justify-center gap-2 px-8 text-center">
      <span className="flex h-12 w-12 items-center justify-center rounded-2xl bg-muted text-[var(--color-text-tertiary)]">{icon}</span>
      <p className="text-sm font-medium text-muted-foreground">{title}</p>
      <p className="max-w-sm text-xs leading-relaxed text-[var(--color-text-tertiary)]">{hint}</p>
      {retry && (
        <button
          type="button"
          onClick={retry}
          className="mt-1 rounded-lg border border-border bg-card px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
        >
          重新加载
        </button>
      )}
    </div>
  )
}

type EditMode = 'view' | 'edit'
type DiscardIntent = 'close' | 'cancel-edit' | null

/** 一次业务文档编辑会话的标识：每次挂载/打开生成、不可复用的令牌 + 上下文。 */
interface DocDialogSessionContext {
  /**
   * 每个打开会话生成、不可复用（随 mutation variables 携带）。
   * 用模块级自增序号对象 identity 作令牌：不依赖 secure context 的
   * crypto.randomUUID（非安全 origin 下不可用），也不引入任何 fallback。
   */
  token: { sessionNo: number }
  oid: string
  vid: string
}

// 模块级自增会话序号：对象引用天然唯一，跨实例比较即身份比较。
let docDialogSessionCounter = 0

/** 纯谓词：该次请求的会话上下文是否与实例当前活跃会话全等（token+oid+vid）。 */
function isActiveSession(
  active: DocDialogSessionContext | null,
  context: DocDialogSessionContext | undefined,
): boolean {
  return active !== null
    && context !== undefined
    && active.token === context.token
    && active.oid === context.oid
    && active.vid === context.vid
}

/**
 * 本体结构的「业务文档」弹窗：查询并展示当前发布版本语义层的需求文档，
 * 并支持当前发布正文的受控修订（快照口径，与建模页 DocumentsDrawer 同源
 * 同风格）。
 * 弹窗壳走 beUI CenterMorphModal（morph 展开、焦点圈定、Esc/遮罩关闭、
 * 滚动锁），内置关闭按钮经 closeButtonLabel 中文化；目录导航为中性画布
 * 浅底（与超级助手工作台导航画布同值）；顶栏展示本体名称与发布版本徽章，
 * 查看态操作为「编辑 | 下载」，编辑态为「取消 | 保存」（下载隐藏，避免
 * 下载到未保存草稿）。
 *
 * 编辑契约：入口只在服务端 canEdit 为真（写权限 × 当前发布 × released ×
 * 正文非空）时渲染；保存 PUT 携带 semanticRevision/documentFingerprint/
 * expectedCurrentReleaseId 三基线，服务端锁内 CAS 复核；保存成功以服务端
 * 持久化为准并失效 semantic/version-tree 查询。目录与预览由草稿实时计算
 * （fence 长度感知），目录点击同时定位编辑器与预览。脏态关闭/取消需经
 * ConfirmDialog 确认；保存中禁止关闭与重复提交。
 */
export default function StructureDocDialog({ open, ontologyId, ontologyName, versionId, versionLabel, onClose }: {
  open: boolean
  ontologyId: string
  /** 顶栏标题：本体名称（替代旧版文档标题）。 */
  ontologyName?: string
  /** 当前发布版本 id（语义层挂载点）。 */
  versionId: string
  versionLabel?: string
  onClose: () => void
}) {
  const queryClient = useQueryClient()
  const queryKey = useMemo(() => ['ontology-structure-doc', ontologyId, versionId] as const, [ontologyId, versionId])
  const { data, isLoading, isError, refetch } = useQuery({
    queryKey,
    queryFn: () => ontologyVersionApi.versionSemantic(ontologyId, versionId),
    enabled: open && Boolean(ontologyId && versionId),
  })

  const semantic = data?.semantic && typeof data.semantic === 'object' ? data.semantic as Record<string, unknown> : null
  const documentMd = typeof semantic?.documentMd === 'string' ? semantic.documentMd : ''
  const documentTitle = typeof semantic?.documentTitle === 'string' && semantic.documentTitle.trim()
    ? semantic.documentTitle.trim()
    : '需求文档'
  const hasContent = documentMd.trim().length > 0

  const [mode, setMode] = useState<EditMode>('view')
  const [editBaseMd, setEditBaseMd] = useState('')
  const [draftMd, setDraftMd] = useState('')
  const [baseline, setBaseline] = useState<{ semanticRevision: number; fingerprint: string } | null>(null)
  const [pane, setPane] = useState<'edit' | 'preview'>('edit')
  const [saveError, setSaveError] = useState<DocumentSaveError | null>(null)
  const [confirmDiscard, setConfirmDiscard] = useState<DiscardIntent>(null)
  const [previewMd, setPreviewMd] = useState('')

  const editing = mode === 'edit'
  const dirty = editing && baseline !== null && isDocumentDirty(editBaseMd, draftMd)
  const draftBlank = editing && isBlankDocument(draftMd)
  const draftTooLarge = editing && isDocumentTooLarge(draftMd)

  const scrollToLineRef = useRef<StructureDocEditorHandle | null>(null)
  const previewScrollRef = useRef<HTMLDivElement>(null)
  // 嵌套确认层（Radix）与外层 CenterMorphModal 都会响应同一次 Esc：在
  // window capture 阶段（早于 Radix 的 document capture）记录「本次 Esc
  // 发生时确认层是否打开」。确认层打开时的 Esc 关闭权归确认层，外层在
  // bubble 回调里按该标记精确吞掉同一次按键——不依赖时间窗。
  const confirmDiscardRef = useRef<DiscardIntent>(null)
  confirmDiscardRef.current = confirmDiscard
  const escapeOwnedByConfirmRef = useRef(false)
  useEffect(() => {
    if (!open) return
    const onKeydown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return
      escapeOwnedByConfirmRef.current = confirmDiscardRef.current !== null
    }
    window.addEventListener('keydown', onKeydown, { capture: true })
    return () => {
      escapeOwnedByConfirmRef.current = false
      window.removeEventListener('keydown', onKeydown, { capture: true })
    }
  }, [open])

  // 目录与正文使用同一份切分：查看态用服务端正文，编辑态用草稿实时重算。
  const viewSections = useMemo(() => splitMarkdownSections(documentMd), [documentMd])
  const editSections = useMemo(() => splitMarkdownSections(draftMd), [draftMd])
  const sections = editing ? editSections : viewSections
  const toc = useMemo(() => tocSections(sections), [sections])

  const [activeId, setActiveId] = useState('')

  // 查看态：目录变化（保存/重载）时回到首节；编辑态：按键会连续改变目录，
  // 按 id 保持当前项，仅在当前节消失时回落到首节，避免每次输入跳回顶部。
  useEffect(() => {
    if (editing) return
    setActiveId(toc[0]?.id || '')
  }, [toc, editing])

  useEffect(() => {
    if (!editing) return
    setActiveId(current => (toc.some(section => section.id === current)
      ? current
      : (toc[0]?.id || '')))
  }, [toc, editing])

  // 编辑预览去抖重渲染：保证「实时」体感且不阻塞输入。
  useEffect(() => {
    if (!editing) return
    const timer = window.setTimeout(() => setPreviewMd(draftMd), 180)
    return () => window.clearTimeout(timer)
  }, [draftMd, editing])

  const previewSections = useMemo(
    () => (editing ? splitMarkdownSections(previewMd) : []),
    [editing, previewMd])

  const exitEdit = useCallback(() => {
    setMode('view')
    setEditBaseMd('')
    setDraftMd('')
    setBaseline(null)
    setPane('edit')
    setPreviewMd('')
    setSaveError(null)
    setConfirmDiscard(null)
  }, [])

  // 编辑上下文以 (ontologyId, versionId) 双键绑定：弹窗关闭、本体或版本
  // 指针变化（含非空→非空的发布指针切换）都立即丢弃编辑态，旧会话草稿
  // 与 in-flight 保存响应不得落到新版本正文上。
  const previousContextRef = useRef({ ontologyId, versionId })
  useEffect(() => {
    const previous = previousContextRef.current
    previousContextRef.current = { ontologyId, versionId }
    if (!open || !versionId) {
      exitEdit()
      return
    }
    if (previous.ontologyId !== ontologyId || previous.versionId !== versionId) {
      exitEdit()
    }
  }, [open, ontologyId, versionId, exitEdit])

  // 会话令牌与活跃会话均由 effect 管理的实例 ref 承载（非模块单例）：
  // - 每个打开会话（open 变 true）生成新令牌（自增序号对象，不依赖安全
  //   上下文），同 oid/vid 重挂载/重开都不可复用；
  // - open 期间按 (token, oid, vid) 登记活跃会话；open=false、上下文变化或
  //   卸载时清理为 null——旧 mutation 回调读到的即不再是活跃会话（lifecycle
  //   guard 与请求上下文各自正确，T21-01）。
  const sessionTokenRef = useRef<{ sessionNo: number } | null>(null)
  const activeSessionRef = useRef<DocDialogSessionContext | null>(null)
  useEffect(() => {
    if (!open) return
    docDialogSessionCounter += 1
    sessionTokenRef.current = { sessionNo: docDialogSessionCounter }
    activeSessionRef.current = {
      token: sessionTokenRef.current, oid: ontologyId, vid: versionId,
    }
    return () => {
      activeSessionRef.current = null
    }
  }, [open, ontologyId, versionId])

  const beginEdit = () => {
    const revision = semantic?.semanticRevision
    setEditBaseMd(documentMd)
    setDraftMd(documentMd)
    setPreviewMd(documentMd)
    setBaseline({
      semanticRevision: typeof revision === 'number' && Number.isFinite(revision) ? revision : 0,
      fingerprint: typeof data?.documentFingerprint === 'string' ? data.documentFingerprint : '',
    })
    setPane('edit')
    setSaveError(null)
    setMode('edit')
  }

  // 每次保存的请求上下文（会话令牌 + oid/vid）随该次 mutation 的 variables
  // 走：mutationFn 与 onSuccess/onError 都只读这一次 variables.context，并由
  // 它构造该次请求的 requestQueryKey/requestVersionTreeKey——不使用 render
  // 闭包 queryKey 或当前 props 推导旧请求目标；后续新的 mutate 不可能覆盖
  // 旧请求的归属（T19-01/T21-01/T21-03）。
  const saveMutation = useMutation({
    mutationFn: (variables: {
      body: SaveOntologyDocumentBody
      context: DocDialogSessionContext
    }) => ontologyVersionApi.saveDocument(
      variables.context.oid, variables.context.vid, variables.body),
    onSuccess: (result, variables) => {
      const context = variables.context
      const requestQueryKey = ['ontology-structure-doc', context.oid, context.vid] as const
      const requestVersionTreeKey = ['version-tree', context.oid] as const
      if (!isActiveSession(activeSessionRef.current, context)) {
        // 旧响应（会话令牌或上下文已不是当前活跃会话）：只允许失效该次
        // 保存自己的旧 queryKey（旧版本确实已持久化），其余全部 UI 副作用
        // （mode/draft/baseline/saveError/confirmDiscard/toast/新缓存写入）
        // 静默，绝不影响新会话（含同 oid/vid 的新实例）。
        void queryClient.invalidateQueries({ queryKey: requestQueryKey })
        void queryClient.invalidateQueries({ queryKey: requestVersionTreeKey })
        return
      }
      // 以服务端持久化为准：写缓存、回查看态并失效 semantic/version-tree。
      queryClient.setQueryData(requestQueryKey, result)
      void queryClient.invalidateQueries({ queryKey: requestQueryKey })
      void queryClient.invalidateQueries({ queryKey: requestVersionTreeKey })
      setSaveError(null)
      setConfirmDiscard(null)
      setMode('view')
      toast.success('业务文档已保存', {
        description: '发布正文与版本树已更新；知识图谱镜像是异步同步，可能有短暂延迟。',
      })
    },
    onError: (error, variables) => {
      const context = variables.context
      // 旧错误（该次保存的会话/上下文已不是当前活跃会话）完全静默：不写
      // saveError、不动确认层、不弹提示。
      if (!isActiveSession(activeSessionRef.current, context)) return
      setSaveError(classifyDocumentSaveError(error))
    },
  })
  // 旧会话在途的 pending 不阻塞新上下文：saving 只在「最新一次保存的
  // variables.context == 当前活跃上下文」时为真（saveMutation.variables 即
  // 最近一次 mutate 的 variables，observer 只跟踪最新 mutation，旧请求落定
  // 不会把它置假）；上下文切换后旧 PUT 继续在后台完成，其响应由上面的
  // 活跃上下文守卫静默消化（T5-01/T19-01）。
  const saving = (() => {
    if (!saveMutation.isPending) return false
    return isActiveSession(activeSessionRef.current, saveMutation.variables?.context)
  })()

  const submitSave = () => {
    if (saving || !baseline) return
    if (draftBlank) {
      setSaveError({
        kind: 'document-empty',
        message: '正文不能为空：请填写业务文档内容后再保存。',
        detail: '正文为空',
      })
      return
    }
    if (draftTooLarge) {
      setSaveError({
        kind: 'document-too-large',
        message: `正文超过 ${DOCUMENT_MAX_BYTES} 字节上限，请精简后再保存。`,
        detail: '正文超限',
        maxBytes: DOCUMENT_MAX_BYTES,
      })
      return
    }
    saveMutation.mutate({
      body: buildDocumentSaveBody(draftMd, {
        semanticRevision: baseline.semanticRevision,
        fingerprint: baseline.fingerprint,
        expectedCurrentReleaseId: versionId,
      }),
      context: {
        token: sessionTokenRef.current ?? { sessionNo: -1 },
        oid: ontologyId,
        vid: versionId,
      },
    })
  }

  /** 冲突/发布版变化后重取服务端基线；草稿保留由用户决定去留。 */
  const reloadBaselineKeepingDraft = async () => {
    const result = await refetch()
    const fresh = result.data
    const freshSemantic = fresh?.semantic && typeof fresh.semantic === 'object'
      ? fresh.semantic as Record<string, unknown> : null
    const freshMd = typeof freshSemantic?.documentMd === 'string' ? freshSemantic.documentMd : ''
    const freshRevision = freshSemantic?.semanticRevision
    setBaseline({
      semanticRevision: typeof freshRevision === 'number' && Number.isFinite(freshRevision) ? freshRevision : 0,
      fingerprint: typeof fresh?.documentFingerprint === 'string' ? fresh.documentFingerprint : '',
    })
    setEditBaseMd(freshMd)
    if (saveError?.kind === 'release-context-changed') {
      void queryClient.invalidateQueries({ queryKey: ['current-release-workspace', ontologyId] })
      void queryClient.invalidateQueries({ queryKey: ['version-tree', ontologyId] })
    }
    setSaveError(null)
  }

  // 关闭请求：确认弹窗打开时由 ConfirmDialog 自行处理 Esc/遮罩；保存中
  // 忽略关闭；脏编辑先确认再关闭。
  const requestClose = () => {
    // 本次 Esc 在 capture 时确认层还开着 → 关闭权归确认层（Radix 会/已
    // 消化），外层精确吞掉同一次按键；下一次真实的 Esc（确认层已关，
    // capture 标记为 false）不受影响。
    if (escapeOwnedByConfirmRef.current) {
      escapeOwnedByConfirmRef.current = false
      return
    }
    if (confirmDiscard !== null) return
    if (saving) return
    if (editing && dirty) {
      setConfirmDiscard('close')
      return
    }
    exitEdit()
    onClose()
  }

  const requestCancelEdit = () => {
    if (saving) return
    if (dirty) {
      setConfirmDiscard('cancel-edit')
      return
    }
    exitEdit()
  }

  const jumpTo = (section: StructureDocSection) => {
    setActiveId(section.id)
    if (editing) {
      scrollToLineRef.current?.scrollToLine(section.startLine + 1)
      document.getElementById(section.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      return
    }
    document.getElementById(section.id)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  // 滚动跟随：以「最后一个滚过内容区顶部阈值的小节」作为当前目录项。
  const handleContentScroll = () => {
    if (editing) return
    const container = previewScrollRef.current
    if (!container) return
    const containerTop = container.getBoundingClientRect().top
    let current = ''
    for (const section of sections) {
      const element = document.getElementById(section.id)
      if (!element) continue
      if (element.getBoundingClientRect().top - containerTop <= 96) current = section.id
      else break
    }
    if (current) setActiveId(current)
  }

  const download = () => {
    if (!hasContent) return
    const blob = new Blob([documentMd], { type: 'text/markdown;charset=utf-8' })
    const anchor = document.createElement('a')
    anchor.href = URL.createObjectURL(blob)
    anchor.download = `${documentTitle}.md`
    anchor.click()
    URL.revokeObjectURL(anchor.href)
  }

  const canEnterEdit = !editing && hasContent && !isLoading && !isError
    && Boolean(semantic) && data?.canEdit === true

  return (
    <CenterMorphModal open={open} onOpenChange={next => { if (!next) requestClose() }}>
      {/* odg-scope：beUI 语义令牌在本域钉为浅色（固定浅色作用域，见 ontology-dialogs.css）。
          尺寸/圆角以 !important 覆盖组件默认的 max-w-[26rem]/rounded-[30px]；
          编辑态放宽到 1360px 容纳「编辑器 + 实时预览」双栏。 */}
      <CenterMorphModalContent
        ariaLabel="业务文档"
        closeButtonLabel="关闭业务文档"
        className={`odg-scope flex h-[78vh] min-h-[520px] !rounded-[14px] shadow-[0_24px_80px_rgba(15,23,42,0.22)] ${editing
          ? '!max-w-[min(96vw,1360px)]'
          : '!max-w-[min(94vw,1040px)]'}`}
      >
        {/* 文档目录：中性画布浅底导航，细滚动条，点击跳转到正文具体位置；
            编辑态目录由草稿实时重算（同源纯函数）。 */}
        <aside className="flex w-60 shrink-0 flex-col border-r border-border bg-[var(--odg-nav-canvas)]">
          <div className="flex h-16 shrink-0 flex-col justify-center border-b border-border px-4">
            <div className="text-sm font-semibold text-[var(--color-text-primary)]">业务文档</div>
            <div className="mt-0.5 text-xs text-[var(--color-text-tertiary)]">
              {editing ? '实时目录 · 共 ' : '需求文档目录 · 共 '}{toc.length} 节
            </div>
          </div>
          <nav data-testid="structure-doc-toc" aria-label="需求文档目录" className="odg-scroll flex-1 overflow-y-auto p-2">
            {isLoading && <div className="px-2 py-1 text-xs text-[var(--color-text-tertiary)]">加载中…</div>}
            {!isLoading && toc.length === 0 && (
              <div className="px-2 py-1 text-xs leading-relaxed text-[var(--color-text-tertiary)]">
                {editing
                  ? '本文档没有小节标题；输入「## 标题」后会实时出现在这里。'
                  : hasContent ? '本文档没有小节标题，可直接阅读右侧全文。' : '暂无可展示的文档目录。'}
              </div>
            )}
            {toc.map(section => (
              <button
                key={section.id}
                type="button"
                data-testid="structure-doc-toc-item"
                data-section-id={section.id}
                aria-current={activeId === section.id ? 'true' : undefined}
                onClick={() => jumpTo(section)}
                style={{ paddingLeft: 10 + (Math.min(section.level, 4) - 1) * 12 }}
                className={`block w-full truncate rounded-md py-2 pr-2.5 text-left text-[13px] transition-colors ${activeId === section.id
                  ? 'odg-toc-active bg-card font-medium text-[var(--color-text-primary)]'
                  : 'text-[var(--color-text-secondary)] hover:bg-card'}`}
              >
                {section.title}
              </button>
            ))}
          </nav>
        </aside>

        {/* 正文 + 顶部操作（查看态：编辑在下载左侧；编辑态：取消/保存并隐藏下载；
            右上角为弹窗内置关闭按钮，留出让位） */}
        <div className="flex min-w-0 flex-1 flex-col">
          <div className="flex h-16 shrink-0 items-center justify-between gap-3 border-b border-border py-0 pl-5 pr-14">
            <div className="flex min-w-0 items-center gap-2">
              <span
                data-testid="structure-doc-ontology-name"
                className="truncate text-sm font-semibold text-[var(--color-text-primary)]"
                title={ontologyName || undefined}
              >
                {ontologyName || '本体'}
              </span>
              {versionLabel && (
                <span
                  className="shrink-0 rounded border border-border bg-muted px-1.5 py-px text-[10px] font-medium text-muted-foreground"
                  title="需求文档随版本快照沉淀；当前发布版正文可在弹窗内受控修订，历史版本保持生成时内容"
                >
                  发布版本 {versionLabel}
                </span>
              )}
            </div>
            <div className="flex shrink-0 items-center gap-1.5" data-testid="structure-doc-actions">
              {editing ? (<>
                <span className="hidden text-[11px] text-muted-foreground sm:inline" title="正文 UTF-8 字节数 / 上限">
                  {documentByteLabel(draftMd)}
                </span>
                <button
                  type="button"
                  data-testid="structure-doc-edit-cancel"
                  onClick={requestCancelEdit}
                  disabled={saving}
                  className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-card px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted disabled:cursor-not-allowed disabled:opacity-60"
                >
                  取消
                </button>
                <button
                  type="button"
                  data-testid="structure-doc-edit-save"
                  onClick={submitSave}
                  disabled={saving || draftBlank || draftTooLarge}
                  title={draftBlank ? '正文为空，不能保存' : draftTooLarge ? `正文超过 ${DOCUMENT_MAX_BYTES} 字节上限` : '保存当前正文到当前发布版本'}
                  className="inline-flex shrink-0 items-center gap-1 rounded-md bg-[var(--ring)] px-2.5 py-1.5 text-xs font-semibold text-white transition-colors hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-60"
                >
                  {saving
                    ? <><Loader2 size={12} className="animate-spin" /> 正在保存</>
                    : <><Pencil size={12} /> 保存</>}
                </button>
              </>) : (<>
                {canEnterEdit && (
                  <button
                    type="button"
                    data-testid="structure-doc-edit"
                    onClick={beginEdit}
                    title="修订当前发布版本的业务文档正文"
                    className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-card px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
                  >
                    <Pencil size={12} /> 编辑
                  </button>
                )}
                {hasContent && (
                  <button
                    type="button"
                    data-testid="structure-doc-download"
                    onClick={download}
                    className="inline-flex shrink-0 items-center gap-1 rounded-md border border-border bg-card px-2.5 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
                  >
                    <Download size={12} /> 下载 .md
                  </button>
                )}
              </>)}
            </div>
          </div>

          {editing && saveError && (
            <div className="shrink-0 px-5 pt-3" data-testid="structure-doc-edit-error-bar">
              <Alert variant={saveError.kind === 'conflict' || saveError.kind === 'release-context-changed' || saveError.kind === 'document-too-large' || saveError.kind === 'document-empty' ? 'warning' : 'danger'} role="alert" data-testid="structure-doc-save-error" title={saveError.detail}>
                <div className="flex flex-wrap items-center gap-2">
                  <span>{saveError.message}</span>
                  {(saveError.kind === 'conflict' || saveError.kind === 'release-context-changed') && (
                    <button
                      type="button"
                      data-testid="structure-doc-reload"
                      onClick={() => { void reloadBaselineKeepingDraft() }}
                      className="rounded-md border border-border bg-card px-2 py-0.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted"
                    >
                      重新加载
                    </button>
                  )}
                  {(saveError.kind === 'network' || saveError.kind === 'unknown') && (
                    <button
                      type="button"
                      data-testid="structure-doc-retry-save"
                      onClick={submitSave}
                      disabled={saving}
                      className="rounded-md border border-border bg-card px-2 py-0.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-muted disabled:opacity-60"
                    >
                      重试
                    </button>
                  )}
                </div>
              </Alert>
            </div>
          )}

          {editing ? (
            <>
              {/* 窄视口（<xl）降级：编辑/预览双 Tab + 实时目录；宽视口双栏并排。 */}
              <div className="flex shrink-0 items-center gap-1 border-b border-border px-5 py-1.5 xl:hidden" data-testid="structure-doc-pane-tabs">
                <button
                  type="button"
                  data-testid="structure-doc-pane-edit"
                  onClick={() => setPane('edit')}
                  aria-pressed={pane === 'edit'}
                  className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${pane === 'edit' ? 'bg-card text-[var(--color-text-primary)] shadow-sm' : 'text-muted-foreground hover:bg-muted'}`}
                >
                  编辑
                </button>
                <button
                  type="button"
                  data-testid="structure-doc-pane-preview"
                  onClick={() => setPane('preview')}
                  aria-pressed={pane === 'preview'}
                  className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${pane === 'preview' ? 'bg-card text-[var(--color-text-primary)] shadow-sm' : 'text-muted-foreground hover:bg-muted'}`}
                >
                  预览
                </button>
              </div>
              <div className="flex min-h-0 flex-1 flex-col xl:flex-row">
                <div
                  data-testid="structure-doc-edit-pane"
                  className={`${pane === 'edit' ? 'flex' : 'hidden'} min-h-0 min-w-0 flex-1 flex-col border-b border-border xl:flex xl:border-b-0 xl:border-r`}
                >
                  <StructureDocEditor
                    value={draftMd}
                    onChange={next => { setDraftMd(next); if (saveError?.kind === 'document-empty') setSaveError(null) }}
                    ariaLabel="业务文档 Markdown 编辑区"
                    scrollToLineRef={scrollToLineRef}
                  />
                </div>
                <div
                  ref={previewScrollRef}
                  data-testid="structure-doc-preview-pane"
                  className={`${pane === 'preview' ? 'flex' : 'hidden'} odg-scroll min-h-0 min-w-0 flex-1 flex-col overflow-y-auto xl:flex`}
                >
                  <div className="mx-auto w-full max-w-[760px] px-6 py-4">
                    {previewSections.map(section => (
                      <div key={section.id} id={section.id}>
                        <DocMarkdown text={section.markdown} renderDiagrams={false} />
                      </div>
                    ))}
                  </div>
                </div>
              </div>
            </>
          ) : (
            <div
              ref={previewScrollRef}
              onScroll={handleContentScroll}
              data-testid="structure-doc-content"
              className="odg-scroll min-h-0 flex-1 overflow-y-auto px-6 py-4"
            >
              {isLoading && (
                <CenterHint
                  testid="structure-doc-loading"
                  icon={<Loader2 size={20} className="animate-spin" />}
                  title="正在查询当前版本关联的需求文档…"
                  hint="文档读取自版本语义层快照。"
                />
              )}
              {isError && (
                <CenterHint
                  testid="structure-doc-error"
                  icon={<AlertCircle size={20} />}
                  title="需求文档读取失败"
                  hint="网络或服务异常导致读取失败，请重试。"
                  retry={() => void refetch()}
                />
              )}
              {!isLoading && !isError && !semantic && (
                <CenterHint
                  testid="structure-doc-empty"
                  icon={<FileText size={20} />}
                  title="当前版本没有关联的需求文档"
                  hint="该版本未经「本体建模」的需求文档生成，语义层尚未沉淀需求文档；这不影响结构浏览，也可回到本体建模补齐后发布新版本。"
                />
              )}
              {!isLoading && !isError && semantic && !hasContent && (
                <CenterHint
                  testid="structure-doc-empty"
                  icon={<FileText size={20} />}
                  title="需求文档内容为空"
                  hint="该版本关联的需求文档没有正文内容；特殊情况下允许为空。"
                />
              )}
              {!isLoading && !isError && hasContent && (
                <div className="mx-auto max-w-[760px]">
                  {sections.map(section => (
                    <div key={section.id} id={section.id}>
                      <DocMarkdown text={section.markdown} />
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </CenterMorphModalContent>

      <ConfirmDialog
        open={confirmDiscard !== null}
        onClose={() => {
          setConfirmDiscard(null)
        }}
        onConfirm={() => {
          const intent = confirmDiscard
          exitEdit()
          if (intent === 'close') onClose()
        }}
        title="放弃未保存的修改？"
        description="当前编辑内容尚未保存，放弃后将恢复为服务端已保存的正文。"
        variant="warning"
        confirmText="放弃修改"
        cancelText="继续编辑"
      />
    </CenterMorphModal>
  )
}
