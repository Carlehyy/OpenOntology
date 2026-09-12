/**
 * 记忆宫殿左侧文件树（ReUI Tree + @headless-tree，含拖拽与内联输入行）。
 *
 * 目录是后端一等公民（空目录可常驻）：文件按 path 折叠进目录树，目录行
 * 携带 palace_folders 行 id，可整目录拖拽/重命名。拖拽用 @headless-tree
 * dragAndDropFeature（HTML5 DnD，容器级 drop 命中根目录）：canDrop 禁止
 * 把目录拖进自身或其子目录、目录必须有行才能拖；落下后由父组件调 API
 * 持久化，本组件不做乐观移动。内联输入行（新建目录/新建笔记/目录重命名）
 * 的开闭由父组件（工具栏按钮）驱动，提交/取消上抛。
 */
import { useEffect, useLayoutEffect, useMemo, useRef } from 'react'
import type { CSSProperties, ReactNode } from 'react'
import {
  dragAndDropFeature, hotkeysCoreFeature, selectionFeature, syncDataLoaderFeature,
} from '@headless-tree/core'
import type { ItemInstance } from '@headless-tree/core'
import { useTree } from '@headless-tree/react'
import {
  AlertCircle, BookMarked, CircleDashed, Clock, FileJson, FileSpreadsheet, FileText, Folder, FolderOpen,
  Image as ImageIcon, Loader2, type LucideIcon,
} from 'lucide-react'

import type { PalaceFile, PalaceFolder } from '@/api/superAssistant'
import { Tree, TreeItem, TreeItemLabel } from '@/components/ui/tree'
import {
  buildPalaceTree,
  normalizePalacePath,
  PALACE_ONTOLOGY_DOCS_DIR_ID,
  PALACE_TREE_ROOT,
  palaceDirId,
  palaceFileId,
  palaceOntologyDocId,
  palaceTreeDirIds,
  type PalaceOntologyDocRow,
  type PalaceTreeItemData,
} from './palaceTreeModel'

/** 内联输入行动作（由弹窗工具栏驱动）：落点目录路径 + 重命名时的目录行 id */
export interface PalaceInlineAction {
  kind: 'new-folder' | 'new-note' | 'rename'
  /** new-* 为落点目录路径；rename 为被重命名目录的原路径 */
  targetPath: string
  dirId?: string
}

interface PalaceFileTreeProps {
  files: PalaceFile[]
  folders: PalaceFolder[]
  /** 「本体文档」目录子项（权威清单 ∪ 镜像状态合并后的行） */
  ontologyDocs: PalaceOntologyDocRow[]
  selectedFileId: string | null
  /** 当前选中的本体文档行 id（pending:<ontologyId> 表示待同步占位） */
  selectedOntologyDocId: string | null
  /** 当前选中目录路径（根为空串）：新建/上传的落点，行内高亮 */
  selectedDirPath: string
  onSelectFile: (file: PalaceFile) => void
  onSelectOntologyDoc: (doc: PalaceOntologyDocRow) => void
  onSelectDir: (path: string) => void
  /** 拖拽落定：移动文件 / 移动整目录（目标路径空串表示根目录） */
  onMoveFile: (fileId: string, targetPath: string) => void
  onMoveFolder: (folderId: string, targetPath: string) => void
  /** 内联输入行的当前动作与提交/取消 */
  inline: PalaceInlineAction | null
  onInlineSubmit: (value: string) => void
  onInlineCancel: () => void
}

const FILE_ICON_SIZE = 14

/** 与截图样式同构的按扩展名着色图标（沿用页面既有 tailwind 色板口径） */
const FILE_ICON_BY_EXT: Record<string, { Icon: LucideIcon; className: string }> = {
  md: { Icon: FileText, className: 'text-slate-400' },
  txt: { Icon: FileText, className: 'text-slate-400' },
  pdf: { Icon: FileText, className: 'text-red-500' },
  doc: { Icon: FileText, className: 'text-blue-500' },
  docx: { Icon: FileText, className: 'text-blue-500' },
  ppt: { Icon: FileText, className: 'text-orange-500' },
  pptx: { Icon: FileText, className: 'text-orange-500' },
  xls: { Icon: FileSpreadsheet, className: 'text-brand-ink' },
  xlsx: { Icon: FileSpreadsheet, className: 'text-brand-ink' },
  csv: { Icon: FileSpreadsheet, className: 'text-brand-ink' },
  json: { Icon: FileJson, className: 'text-amber-500' },
  xml: { Icon: FileJson, className: 'text-amber-500' },
  png: { Icon: ImageIcon, className: 'text-violet-500' },
  jpg: { Icon: ImageIcon, className: 'text-violet-500' },
  jpeg: { Icon: ImageIcon, className: 'text-violet-500' },
  gif: { Icon: ImageIcon, className: 'text-violet-500' },
  webp: { Icon: ImageIcon, className: 'text-violet-500' },
}

const DEFAULT_FILE_ICON = { Icon: FileText, className: 'text-[var(--color-text-tertiary)]' }

const fileIconFor = (file: PalaceFile | undefined): { Icon: LucideIcon; className: string } => {
  if (!file) return DEFAULT_FILE_ICON
  const ext = file.filename.toLowerCase().split('.').pop() ?? ''
  return FILE_ICON_BY_EXT[ext] ?? DEFAULT_FILE_ICON
}

export default function PalaceFileTree({
  files, folders, ontologyDocs, selectedFileId, selectedOntologyDocId, selectedDirPath,
  onSelectFile, onSelectOntologyDoc, onSelectDir,
  onMoveFile, onMoveFolder, inline, onInlineSubmit, onInlineCancel,
}: PalaceFileTreeProps) {
  const model = useMemo(() => buildPalaceTree(files, folders, ontologyDocs), [files, folders, ontologyDocs])
  const modelRef = useRef(model)
  modelRef.current = model
  const onSelectFileRef = useRef(onSelectFile)
  onSelectFileRef.current = onSelectFile
  const onSelectOntologyDocRef = useRef(onSelectOntologyDoc)
  onSelectOntologyDocRef.current = onSelectOntologyDoc
  const onSelectDirRef = useRef(onSelectDir)
  onSelectDirRef.current = onSelectDir
  const onMoveFileRef = useRef(onMoveFile)
  onMoveFileRef.current = onMoveFile
  const onMoveFolderRef = useRef(onMoveFolder)
  onMoveFolderRef.current = onMoveFolder
  const onInlineSubmitRef = useRef(onInlineSubmit)
  onInlineSubmitRef.current = onInlineSubmit
  // 刷新与 rebuildTree 之间有一帧“结构缓存仍含已删 id”的窗口：sync
  // dataLoader 的 getItem 返回 undefined/null 会整树抛错（生产白屏），
  // 这里用“见过的节点”兜底，占位行在绘制前的 layout rebuild 中消失。
  const seenItemsRef = useRef<Record<string, PalaceTreeItemData>>({})
  const placeholderRef = useRef<PalaceTreeItemData>({ kind: 'dir', name: '…' })

  const tree = useTree<PalaceTreeItemData>({
    rootItemId: PALACE_TREE_ROOT,
    getItemName: item => item.getItemData()?.name ?? '',
    isItemFolder: item => item.getItemData()?.kind === 'dir',
    dataLoader: {
      getItem: id => modelRef.current.items[id] ?? seenItemsRef.current[id] ?? placeholderRef.current,
      getChildren: id => modelRef.current.children[id] ?? [],
    },
    features: [syncDataLoaderFeature, hotkeysCoreFeature, selectionFeature, dragAndDropFeature],
    // 我们的树按名称排序展示，不提供拖拽排序语义：目标只有「落入目录/根」。
    // draggedItemOverwritesSelection 保持默认 true：未选中直接拖拽时拖的就是
    // 被拖项（若为 false，空选中会让拖拽变成拖「零个条目」，onDrop 不生效）
    canReorder: false,
    // 本体文档行与「本体文档」虚拟目录均不可拖拽（只读，随发布事件演化）
    canDrag: items => items.every(item => {
      const data = item.getItemData()
      if (data?.kind === 'file') return true
      if (data?.kind === 'dir' && !data.ontologyDocsDir) return !!data.dirId
      return false
    }),
    canDrop: (items, target) => {
      if (!target.item.isFolder()) return false
      // 「本体文档」目录只读：禁止任何拖入
      if (target.item.getId() === PALACE_ONTOLOGY_DOCS_DIR_ID) return false
      const targetPath = target.item.getId() === PALACE_TREE_ROOT
        ? '' : target.item.getItemData()?.path ?? ''
      return items.every(item => {
        const data = item.getItemData()
        if (data?.kind === 'file') return true
        if (data?.kind === 'dir' && data.path != null) {
          return data.path !== targetPath && !targetPath.startsWith(`${data.path}/`)
        }
        return false
      })
    },
    onDrop: (items, target) => {
      const moved = target.item.getId() === PALACE_TREE_ROOT
        ? '' : target.item.getItemData()?.path
      if (moved == null) return
      for (const item of items) {
        const data = item.getItemData()
        if (data?.kind === 'dir' && data.dirId && data.path != null) {
          if (data.path !== moved) onMoveFolderRef.current(data.dirId, moved)
        } else if (data?.kind === 'file' && data.file) {
          if (data.file.path !== moved) onMoveFileRef.current(data.file.id, moved)
        }
      }
    },
    initialState: { expandedItems: palaceTreeDirIds(model), selectedItems: [] },
    onPrimaryAction: item => {
      const data = item.getItemData()
      if (data?.kind === 'file' && data.file) onSelectFileRef.current(data.file)
      else if (data?.kind === 'ontology-doc' && data.ontologyDoc) {
        onSelectOntologyDocRef.current(data.ontologyDoc)
      }
    },
  })
  const treeRef = useRef(tree)
  treeRef.current = tree

  // 已知目录集合：刷新后自动展开新增目录，既有目录维持用户展开状态
  const knownDirIdsRef = useRef<Set<string>>(new Set(palaceTreeDirIds(model)))

  useLayoutEffect(() => {
    const dirIds = palaceTreeDirIds(model)
    const known = knownDirIdsRef.current
    const fresh = dirIds.filter(id => !known.has(id))
    knownDirIdsRef.current = new Set(dirIds)
    for (const id of fresh) treeRef.current.getItemInstance(id).expand()
    seenItemsRef.current = { ...seenItemsRef.current, ...model.items }
    // 布局阶段重建结构缓存：绘制前收敛到最新 files，避免渲染已删条目
    treeRef.current.rebuildTree()
  }, [tree, model])

  // 选中态同步进树（data-selected 样式）；文件被删除时清掉悬挂选中；
  // 选中文件同时展开其所在目录（新建笔记/上传归位后的 reveal，空目录
  // 在 rebuildTree 时会被 headless-tree 丢掉展开态，不能只靠 fresh-dir 展开）
  useEffect(() => {
    const instance = treeRef.current
    const id = selectedFileId
      ? palaceFileId(selectedFileId)
      : selectedOntologyDocId ? palaceOntologyDocId(selectedOntologyDocId) : ''
    const current = instance.getSelectedItems().map(item => item.getId())
    const needsUpdate = id ? !current.includes(id) : current.length > 0
    if (needsUpdate) {
      instance.setSelectedItems(id ? [id] : [])
    }
    if (!selectedFileId) return
    const dirPath = normalizePalacePath(files.find(file => file.id === selectedFileId)?.path)
    if (!dirPath) return
    try {
      instance.getItemInstance(palaceDirId(dirPath)).expand()
    } catch {
      // 结构缓存尚未收敛（layout rebuild 前），跳过本次 reveal
    }
  }, [tree, selectedFileId, selectedOntologyDocId, files])

  const handleSelect = (item: ItemInstance<PalaceTreeItemData>) => {
    const data = item.getItemData()
    if (data?.kind === 'file' && data.file) onSelectFileRef.current(data.file)
    else if (data?.kind === 'ontology-doc' && data.ontologyDoc) {
      onSelectOntologyDocRef.current(data.ontologyDoc)
    } else if (data?.kind === 'dir' && data.path != null) onSelectDirRef.current(data.path)
  }

  // 内联输入行落位：根级出现在树顶；目录级（新建/重命名）紧随该目录行之后。
  // 输入行是目录行的兄弟 DOM，不受目录折叠影响， collapsing 也不丢焦点。
  const inlineDirItemId = inline?.targetPath ? palaceDirId(inline.targetPath) : PALACE_TREE_ROOT

  const renderInlineRow = (level: number): ReactNode => (
    <div
      key="palace-inline-row"
      data-testid="palace-inline-row"
      role="group"
      aria-label={inline?.kind === 'rename' ? '重命名目录' : inline?.kind === 'new-folder' ? '新建子目录' : '新建笔记'}
      style={{ '--tree-padding': `${level * 16}px` } as CSSProperties}
      className="flex items-center gap-1.5 py-1 pe-2 ps-[var(--tree-padding)]"
    >
      {inline?.kind === 'new-folder' && (
        <Folder size={FILE_ICON_SIZE} className="shrink-0 text-[var(--color-primary)]" />
      )}
      {inline?.kind === 'new-note' && (
        <FileText size={FILE_ICON_SIZE} className="shrink-0 text-slate-400" />
      )}
      <input
        data-testid="palace-inline-input"
        autoFocus
        defaultValue={inline?.kind === 'rename'
          ? inline.targetPath.slice(inline.targetPath.lastIndexOf('/') + 1)
          : ''}
        placeholder={inline?.kind === 'new-note' ? '笔记名（自动存为 .md）' : '目录名'}
        aria-label={inline?.kind === 'rename' ? '目录名称' : inline?.kind === 'new-folder' ? '新目录名称' : '新笔记文件名'}
        className="h-6 w-full min-w-0 rounded-md border border-[var(--color-primary)] bg-white px-1.5 text-[13px] text-[var(--color-text-primary)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        onKeyDown={event => {
          if (event.key === 'Enter') {
            event.preventDefault()
            onInlineSubmitRef.current((event.target as HTMLInputElement).value)
          } else if (event.key === 'Escape') {
            event.preventDefault()
            onInlineCancel()
          }
        }}
        onBlur={event => {
          const value = event.target.value.trim()
          if (value) onInlineSubmitRef.current(value)
          else onInlineCancel()
        }}
      />
    </div>
  )

  const rows: ReactNode[] = []
  if (inline && inlineDirItemId === PALACE_TREE_ROOT) rows.push(renderInlineRow(0))
  for (const item of tree.getItems()) {
    const data = item.getItemData()
    const file = data?.kind === 'file' ? data.file : undefined
    const ontologyDoc = data?.kind === 'ontology-doc' ? data.ontologyDoc : undefined
    const extracting = file?.status === 'pending' || file?.status === 'building'
      || ontologyDoc?.status === 'pending' || ontologyDoc?.status === 'building'
    const dragTarget = item.isFolder() && item.isDragTarget()
    const dirPath = data?.kind === 'dir' && item.getId() !== PALACE_TREE_ROOT ? data.path : undefined
    const { Icon: FileIcon, className: iconClass } = fileIconFor(file)
    const selectedDir = dirPath != null && dirPath === selectedDirPath && !selectedFileId
    // 目录直接子节点数（含子目录与文件；本体文档目录同理），右侧徽章展示
    const childCount = data?.kind === 'dir' ? (model.children[item.getId()]?.length ?? 0) : 0
    rows.push(
      <div key={`row-${item.getId()}`}>
        <TreeItem
          item={item}
          onItemSelect={handleSelect}
          data-palace-file={file ? file.id : undefined}
          data-palace-dir={dirPath}
          data-selected-dir={selectedDir || undefined}
          className={`py-0.5 ${dragTarget ? 'rounded-lg ring-2 ring-inset ring-[var(--color-primary)]' : ''}`}
        >
          <TreeItemLabel item={item} className={selectedDir ? 'bg-[var(--color-bg-hover)]' : undefined}>
            {data?.kind === 'dir' ? (
              <>
                {item.isExpanded() ? (
                  <FolderOpen size={FILE_ICON_SIZE} className="shrink-0 text-[var(--color-primary)]" />
                ) : (
                  <Folder size={FILE_ICON_SIZE} className="shrink-0 text-[var(--color-primary)]" />
                )}
                <span className="truncate text-[13px]" title={dirPath || undefined}>{data.name}</span>
                <span
                  aria-label={`${data.name} 下共 ${childCount} 项`}
                  className="ml-auto shrink-0 rounded-full bg-[var(--color-bg-hover)] px-1.5 text-[10px] tabular-nums leading-4 text-[var(--color-text-tertiary)]"
                >
                  {childCount}
                </span>
              </>
            ) : data?.kind === 'ontology-doc' && ontologyDoc ? (
              <>
                <BookMarked size={FILE_ICON_SIZE} className="shrink-0 text-brand-ink" />
                <span
                  className="truncate text-[13px]"
                  title={`${ontologyDoc.ontologyName} · ${ontologyDoc.versionNumber}${ontologyDoc.error ? ` · ${ontologyDoc.error}` : ''}`}
                >
                  {ontologyDoc.title}
                </span>
                {ontologyDoc.status === 'pending-sync' && (
                  <Clock size={11} className="ml-auto shrink-0 text-slate-300" aria-label="待同步" />
                )}
                {ontologyDoc.status === 'failed' && (
                  <AlertCircle size={11} className="ml-auto shrink-0 text-red-500" aria-label="抽取失败" />
                )}
                {extracting && (
                  <Loader2 size={11} className="ml-auto shrink-0 animate-spin text-amber-500" aria-label="抽取中" />
                )}
              </>
            ) : (
              <>
                <FileIcon
                  size={FILE_ICON_SIZE}
                  className={`shrink-0 ${file?.status === 'draft' ? 'opacity-50' : ''} ${iconClass}`}
                />
                <span
                  className={`truncate text-[13px] ${file?.status === 'draft' ? 'text-[var(--color-text-tertiary)]' : ''}`}
                  title={file?.error || file?.filename}
                >
                  {file?.filename}
                </span>
                {file?.status === 'draft' && (
                  <CircleDashed size={11} className="ml-auto shrink-0 text-slate-300" aria-label="草稿" />
                )}
                {file?.status === 'failed' && (
                  <AlertCircle size={11} className="ml-auto shrink-0 text-red-500" aria-label="抽取失败" />
                )}
                {extracting && (
                  <Loader2 size={11} className="ml-auto shrink-0 animate-spin text-amber-500" aria-label="抽取中" />
                )}
              </>
            )}
          </TreeItemLabel>
        </TreeItem>
        {inline && inlineDirItemId === item.getId() && renderInlineRow(item.getItemMeta().level + 1)}
      </div>,
    )
  }

  return (
    <Tree
      tree={tree}
      label="知识图谱文件树"
      indent={16}
      data-testid="palace-file-tree"
      className="min-h-0 flex-1 overflow-y-auto pe-1"
    >
      {rows}
    </Tree>
  )
}
