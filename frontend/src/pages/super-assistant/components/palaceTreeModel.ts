/**
 * 记忆宫殿文件树构建（纯函数，单测见 test/unit/palaceFileTree.test.ts）。
 *
 * 目录是后端一等公民（palace_folders 行，支持空目录），文件行只带归属路径
 * folder_path：先按目录行建树（携带目录 id，供拖拽/重命名），再由文件路径
 * 兜底补齐缺失层级（迁移前的存量目录、并发窗口）。目录排前、文件排后，
 * 各自按名称 zh locale 排序。
 * 「本体文档」是固定在根级首位的只读虚拟目录（不落 palace_folders 行）：
 * 子项来自「本体侧权威清单 ∪ 宫殿镜像状态」的合并（见 mergeOntologyDocRows），
 * 空清单时整个目录不出现。节点 id：目录 dir:<path>、文件 file:<id>、
 * 本体文档 ontodoc:<镜像行 id>，供 headless-tree dataLoader 使用。
 */
import type {
  PalaceFile,
  PalaceFolder,
  PalaceOntologyDocument,
} from '../../../api/superAssistant'
import type { OntologyPublishedDocument } from '../../../api/ontologies'

export const PALACE_TREE_ROOT = 'palace-root'

/** 本体文档虚拟目录的节点 id（非 dir: 前缀，杜绝与用户目录行碰撞） */
export const PALACE_ONTOLOGY_DOCS_DIR_ID = 'ontology-docs-dir'
/**
 * 本体文档目录的选中路由哨兵：仅用于 selectedDirPath 通信与工具栏门控，
 * 不参与 palace_folders / folder_path 语义。用户若恰好建有同名目录，
 * 该目录的选中态会被一并按只读目录处理（极端边角，见上）。
 */
export const PALACE_ONTOLOGY_DOCS_DIR_PATH = '__ontology_docs__'

export interface PalaceTreeItemData {
  kind: 'dir' | 'file' | 'ontology-doc'
  name: string
  /** 目录归一路径（根哨兵为空串）；本体文档虚拟目录携带路由哨兵 */
  path?: string
  /** 目录行 id（palace_folders）；仅由文件路径派生的中间目录无行时缺省 */
  dirId?: string
  /** 本体文档虚拟目录标记（只读：禁建/禁删/禁拖入） */
  ontologyDocsDir?: boolean
  file?: PalaceFile
  ontologyDoc?: PalaceOntologyDocRow
}

export interface PalaceTreeModel {
  /** 节点 id → 数据（含 PALACE_TREE_ROOT 根哨兵） */
  items: Record<string, PalaceTreeItemData>
  /** 节点 id → 有序子节点 id */
  children: Record<string, string[]>
}

/** 树中展示的本体文档行：镜像状态与权威清单合并后的统一形状 */
export interface PalaceOntologyDocRow {
  /** 宫殿镜像行 id；事件未达（待同步）时为 pending:<ontologyId>，预览不可用 */
  id: string
  ontologyId: string
  ontologyName: string
  title: string
  versionNumber: string
  fingerprint: string
  status: 'pending' | 'building' | 'built' | 'failed' | 'pending-sync'
  error: string | null
  entityCount: number
  relationCount: number
  extractedChars: number
  size: number
  /** 是否已有宫殿镜像（false=发布事件未达，稍后自动同步） */
  synced: boolean
}

export const palaceDirId = (path: string): string => `dir:${path}`
export const palaceFileId = (fileId: string): string => `file:${fileId}`
export const palaceOntologyDocId = (docId: string): string => `ontodoc:${docId}`

/**
 * 本体侧权威清单（/ontologies/published-documents）与宫殿镜像
 * （/super-assistant/palace/ontology-documents）按 ontologyId 合并：
 * - 指纹一致 → 以镜像状态为准（正常态）；
 * - 指纹不一致 → 镜像建图中保持「抽取中」，否则标记「待同步」（事件在途/丢失，
 *   由发布事件或每日对账收敛）；
 * - 无镜像 → 「待同步」占位行（发布刚发生，事件秒级可达）；
 * - 镜像有而权威清单没有（发布回滚幻影等）→ 不展示（权威清单是唯一事实源）。
 */
export function mergeOntologyDocRows(
  published: OntologyPublishedDocument[],
  mirrored: PalaceOntologyDocument[],
): PalaceOntologyDocRow[] {
  const mirrorByOntology = new Map(mirrored.map(doc => [doc.ontologyId, doc]))
  const rows: PalaceOntologyDocRow[] = []
  for (const entry of published) {
    const mirror = mirrorByOntology.get(entry.ontologyId)
    if (!mirror) {
      rows.push({
        id: `pending:${entry.ontologyId}`,
        ontologyId: entry.ontologyId,
        ontologyName: entry.ontologyName,
        title: entry.title,
        versionNumber: entry.versionNumber,
        fingerprint: entry.fingerprint,
        status: 'pending-sync',
        error: null,
        entityCount: 0,
        relationCount: 0,
        extractedChars: 0,
        size: 0,
        synced: false,
      })
      continue
    }
    const sameVersion = mirror.fingerprint === entry.fingerprint
    const status: PalaceOntologyDocRow['status'] = sameVersion
      ? (mirror.status as PalaceOntologyDocRow['status'])
      : (mirror.status === 'building' ? 'building' : 'pending-sync')
    rows.push({
      id: mirror.id,
      ontologyId: entry.ontologyId,
      ontologyName: entry.ontologyName || mirror.ontologyName,
      title: entry.title || mirror.title,
      versionNumber: entry.versionNumber || mirror.versionNumber,
      fingerprint: entry.fingerprint,
      status,
      error: mirror.error,
      entityCount: sameVersion ? mirror.entityCount : 0,
      relationCount: sameVersion ? mirror.relationCount : 0,
      extractedChars: sameVersion ? mirror.extractedChars : 0,
      size: sameVersion ? mirror.size : 0,
      synced: true,
    })
  }
  return rows
}

const collator = new Intl.Collator('zh', { numeric: true, sensitivity: 'base' })

/** 规整 path：斜杠统一、去空段与首尾空白，容忍后端缺省/异常值 */
export const normalizePalacePath = (path: string | null | undefined): string =>
  String(path ?? '')
    .replace(/\\/g, '/')
    .split('/')
    .map(segment => segment.trim())
    .filter(Boolean)
    .join('/')

/** 目录路径 + 新子目录名 → 子目录归一路径 */
export const joinPalacePath = (dirPath: string, name: string): string =>
  [normalizePalacePath(dirPath), name.trim()].filter(Boolean).join('/')

export function buildPalaceTree(
  files: PalaceFile[],
  folders: PalaceFolder[] = [],
  ontologyDocs: PalaceOntologyDocRow[] = [],
): PalaceTreeModel {
  const items: Record<string, PalaceTreeItemData> = {
    [PALACE_TREE_ROOT]: { kind: 'dir', name: '', path: '' },
  }
  const children: Record<string, string[]> = { [PALACE_TREE_ROOT]: [] }

  const ensureDir = (path: string, dirId?: string): string => {
    if (!path) return PALACE_TREE_ROOT
    const id = palaceDirId(path)
    if (!items[id]) {
      items[id] = { kind: 'dir', name: path.slice(path.lastIndexOf('/') + 1), path, dirId }
      children[id] = []
      const parentPath = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : ''
      children[ensureDir(parentPath)].push(id)
    } else if (dirId) {
      // 文件路径与目录行都指向该目录：补上行 id
      items[id] = { ...items[id], dirId }
    }
    return id
  }

  // 「本体文档」只读虚拟目录：固定根级首位，空清单不出现
  if (ontologyDocs.length > 0) {
    items[PALACE_ONTOLOGY_DOCS_DIR_ID] = {
      kind: 'dir', name: '本体文档', path: PALACE_ONTOLOGY_DOCS_DIR_PATH, ontologyDocsDir: true,
    }
    children[PALACE_ONTOLOGY_DOCS_DIR_ID] = []
    children[PALACE_TREE_ROOT].push(PALACE_ONTOLOGY_DOCS_DIR_ID)
    for (const doc of ontologyDocs) {
      const id = palaceOntologyDocId(doc.id)
      items[id] = { kind: 'ontology-doc', name: doc.title, ontologyDoc: doc }
      children[PALACE_ONTOLOGY_DOCS_DIR_ID].push(id)
    }
  }

  // 目录行先行（一等公民，空目录也成立），文件路径随后兜底补缺失层级
  for (const folder of folders) {
    const path = normalizePalacePath(folder.path)
    if (path) ensureDir(path, folder.id)
  }
  for (const file of files) {
    const dirId = ensureDir(normalizePalacePath(file.path))
    const id = palaceFileId(file.id)
    items[id] = { kind: 'file', name: file.filename, file }
    children[dirId].push(id)
  }

  // 深度优先排序：目录在前、文件在后，同组按名称排序；本体文档目录钉在根级首位
  const byName = (a: string, b: string) => collator.compare(items[a].name, items[b].name)
  const walk = (id: string) => {
    const list = children[id] ?? []
    let dirs = list.filter(child => items[child]?.kind === 'dir').sort(byName)
    if (id === PALACE_TREE_ROOT && dirs.includes(PALACE_ONTOLOGY_DOCS_DIR_ID)) {
      dirs = [
        PALACE_ONTOLOGY_DOCS_DIR_ID,
        ...dirs.filter(child => child !== PALACE_ONTOLOGY_DOCS_DIR_ID),
      ]
    }
    const fileLikes = list
      .filter(child => items[child]?.kind !== 'dir')
      .sort(byName)
    children[id] = [...dirs, ...fileLikes]
    for (const dir of dirs) walk(dir)
  }
  walk(PALACE_TREE_ROOT)

  return { items, children }
}

/** 全部目录节点 id（不含根哨兵），用于初始展开与新增目录检测 */
export function palaceTreeDirIds(model: PalaceTreeModel): string[] {
  return Object.keys(model.items).filter(
    id => id !== PALACE_TREE_ROOT && model.items[id].kind === 'dir',
  )
}
