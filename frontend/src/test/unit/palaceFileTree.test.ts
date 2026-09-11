import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  buildPalaceTree,
  mergeOntologyDocRows,
  normalizePalacePath,
  PALACE_ONTOLOGY_DOCS_DIR_ID,
  PALACE_TREE_ROOT,
  palaceDirId,
  palaceFileId,
  palaceOntologyDocId,
  palaceTreeDirIds,
} from '../../pages/super-assistant/components/palaceTreeModel.ts'
import type { PalaceFile, PalaceOntologyDocument } from '../../api/superAssistant'
import type { OntologyPublishedDocument } from '../../api/ontologies'

let seq = 0

function makeFile(partial: Partial<PalaceFile> & { id?: string; filename: string }): PalaceFile {
  seq += 1
  return {
    id: partial.id ?? `f-${seq}`,
    filename: partial.filename,
    path: partial.path ?? '',
    mimeType: partial.mimeType ?? 'text/markdown',
    size: partial.size ?? 10,
    sha256: 'x',
    extractedChars: 0,
    status: partial.status ?? 'built',
    error: null,
    entityCount: 0,
    relationCount: 0,
    editable: partial.editable ?? true,
    isImage: partial.isImage ?? false,
    createdAt: '2026-09-04T00:00:00Z',
    updatedAt: '2026-09-04T00:00:00Z',
  }
}

describe('normalizePalacePath', () => {
  it('容忍反斜杠、空白段与 null/undefined', () => {
    assert.equal(normalizePalacePath('a/b/c'), 'a/b/c')
    assert.equal(normalizePalacePath('a\\b\\c'), 'a/b/c')
    assert.equal(normalizePalacePath(' /a/ /b/ '), 'a/b')
    assert.equal(normalizePalacePath(null), '')
    assert.equal(normalizePalacePath(undefined), '')
  })
})

describe('buildPalaceTree', () => {
  it('空库只有根哨兵', () => {
    const model = buildPalaceTree([])
    assert.deepEqual(Object.keys(model.items), [PALACE_TREE_ROOT])
    assert.deepEqual(model.children[PALACE_TREE_ROOT], [])
  })

  it('根目录文件直接挂在根下；目录排前、文件排后，同组按名称排序', () => {
    const model = buildPalaceTree([
      makeFile({ filename: 'b.md' }),
      makeFile({ filename: 'a.md', path: '' }),
      makeFile({ filename: 'doc.md', path: '资料包' }),
      makeFile({ filename: 'nested.txt', path: '资料包/sub' }),
    ])
    // 目录在前，文件按名称排序：a.md(f-2)、b.md(f-1)
    assert.deepEqual(model.children[PALACE_TREE_ROOT], [
      palaceDirId('资料包'),
      palaceFileId('f-2'),
      palaceFileId('f-1'),
    ])
    assert.deepEqual(model.children[palaceDirId('资料包')], [
      palaceDirId('资料包/sub'),
      palaceFileId('f-3'),
    ])
    assert.equal(model.items[palaceFileId('f-4')].file?.filename, 'nested.txt')
    assert.equal(model.items[palaceDirId('资料包/sub')].name, 'sub')
  })

  it('同名目录跨文件合并；不同目录下同名文件互不冲突', () => {
    const model = buildPalaceTree([
      makeFile({ id: 'fa', filename: '共享.md', path: 'a' }),
      makeFile({ id: 'fb', filename: '共享.md', path: 'b' }),
      makeFile({ id: 'fd', filename: '深.txt', path: 'a/deep' }),
    ])
    assert.deepEqual(model.children[palaceDirId('a')], [
      palaceDirId('a/deep'),
      palaceFileId('fa'),
    ])
    assert.deepEqual(model.children[palaceDirId('b')], [palaceFileId('fb')])
    assert.deepEqual(model.children[palaceDirId('a/deep')], [palaceFileId('fd')])
    // 目录节点只创建一次（a 与 a/deep 各一）
    assert.deepEqual(palaceTreeDirIds(model).sort(), ['dir:a', 'dir:a/deep', 'dir:b'])
  })

  it('path 反斜杠与空白段被规整后再建树', () => {
    const model = buildPalaceTree([
      makeFile({ id: 'fx', filename: 'x.md', path: '\\导入\\ 子目录\\' }),
    ])
    assert.deepEqual(palaceTreeDirIds(model).sort(), ['dir:导入', 'dir:导入/子目录'])
    assert.deepEqual(model.children[palaceDirId('导入/子目录')], [palaceFileId('fx')])
  })
})

// ----- 目录一等公民：目录行建树 + 路径纯函数 ---------------------------------

import { joinPalacePath } from '../../pages/super-assistant/components/palaceTreeModel.ts'
import type { PalaceFolder } from '../../api/superAssistant'

function makeFolder(partial: Partial<PalaceFolder> & { path: string }): PalaceFolder {
  seq += 1
  return {
    id: partial.id ?? `fd-${seq}`,
    path: partial.path,
    createdAt: '2026-09-05T00:00:00Z',
    updatedAt: '2026-09-05T00:00:00Z',
  }
}

describe('buildPalaceTree（目录行）', () => {
  it('空目录（无文件）也是树节点，携带目录行 id', () => {
    const model = buildPalaceTree([], [makeFolder({ id: 'fd-1', path: '空目录' })])
    assert.deepEqual(model.children[PALACE_TREE_ROOT], [palaceDirId('空目录')])
    assert.equal(model.items[palaceDirId('空目录')].dirId, 'fd-1')
    assert.equal(model.items[palaceDirId('空目录')].path, '空目录')
    assert.deepEqual(palaceTreeDirIds(model), ['dir:空目录'])
  })

  it('目录行与文件路径同时存在时合并，目录行 id 保留；祖先目录补齐', () => {
    const model = buildPalaceTree(
      [makeFile({ id: 'fa', filename: 'a.md', path: 'x/y' })],
      [makeFolder({ id: 'fd-y', path: 'x/y' })],
    )
    assert.equal(model.items[palaceDirId('x/y')].dirId, 'fd-y')
    // 文件路径的祖先 x 没有行：树里仍补齐（path 有值、dirId 缺省）
    assert.equal(model.items[palaceDirId('x')].path, 'x')
    assert.equal(model.items[palaceDirId('x')].dirId, undefined)
    assert.deepEqual(model.children[palaceDirId('x/y')], [palaceFileId('fa')])
  })

  it('文件路径派生的目录不含 dirId', () => {
    const model = buildPalaceTree([makeFile({ id: 'fb', filename: 'b.md', path: 'derived' })])
    assert.equal(model.items[palaceDirId('derived')].dirId, undefined)
  })
})

describe('路径纯函数', () => {
  it('joinPalacePath 拼接落点目录与新名称', () => {
    assert.equal(joinPalacePath('a', 'b'), 'a/b')
    assert.equal(joinPalacePath('', 'b'), 'b')
    assert.equal(joinPalacePath(' a / ', ' b '), 'a/b')
  })
})

// ----- 本体文档：权威清单 ∪ 镜像状态合并 + 只读虚拟目录 ---------------------

function makePublished(partial: Partial<OntologyPublishedDocument> & { ontologyId: string }): OntologyPublishedDocument {
  return {
    ontologyId: partial.ontologyId,
    ontologyName: partial.ontologyName ?? '本体A',
    versionId: partial.versionId ?? 'v-id',
    versionNumber: partial.versionNumber ?? 'v2',
    title: partial.title ?? '本体A 业务文档',
    fingerprint: partial.fingerprint ?? 'fp-a',
    documentChars: partial.documentChars ?? 100,
    publishedAt: partial.publishedAt ?? null,
  }
}

function makeMirror(partial: Partial<PalaceOntologyDocument> & { id: string; ontologyId: string }): PalaceOntologyDocument {
  return {
    id: partial.id,
    ontologyId: partial.ontologyId,
    ontologyName: partial.ontologyName ?? '本体A',
    versionId: partial.versionId ?? 'v-id',
    versionNumber: partial.versionNumber ?? 'v2',
    title: partial.title ?? '本体A 业务文档',
    fingerprint: partial.fingerprint ?? 'fp-a',
    status: partial.status ?? 'built',
    error: partial.error ?? null,
    entityCount: partial.entityCount ?? 3,
    relationCount: partial.relationCount ?? 2,
    extractedChars: partial.extractedChars ?? 100,
    size: partial.size ?? 120,
    updatedAt: partial.updatedAt ?? '2026-09-11T00:00:00Z',
  }
}

describe('mergeOntologyDocRows', () => {
  it('指纹一致：以镜像状态为准，计数透传', () => {
    const rows = mergeOntologyDocRows(
      [makePublished({ ontologyId: 'o1' })],
      [makeMirror({ id: 'd1', ontologyId: 'o1', status: 'built', entityCount: 7 })],
    )
    assert.equal(rows.length, 1)
    assert.equal(rows[0].id, 'd1')
    assert.equal(rows[0].status, 'built')
    assert.equal(rows[0].entityCount, 7)
    assert.equal(rows[0].synced, true)
  })

  it('指纹漂移：非建图中标记待同步并清零计数；建图中保持抽取中', () => {
    const rows = mergeOntologyDocRows(
      [makePublished({ ontologyId: 'o1', fingerprint: 'fp-new' })],
      [
        makeMirror({ id: 'd1', ontologyId: 'o1', fingerprint: 'fp-old', status: 'built' }),
        // o2 镜像没有权威清单条目 → 不展示
        makeMirror({ id: 'd2', ontologyId: 'o2', fingerprint: 'fp-old', status: 'built' }),
      ],
    )
    assert.equal(rows.length, 1)
    assert.equal(rows[0].status, 'pending-sync')
    assert.equal(rows[0].entityCount, 0)

    const buildingRows = mergeOntologyDocRows(
      [makePublished({ ontologyId: 'o1', fingerprint: 'fp-new' })],
      [makeMirror({ id: 'd1', ontologyId: 'o1', fingerprint: 'fp-old', status: 'building' })],
    )
    assert.equal(buildingRows[0].status, 'building')
  })

  it('无镜像：占位行待同步、id 带 pending 前缀、不可预览', () => {
    const rows = mergeOntologyDocRows(
      [makePublished({ ontologyId: 'o9' })],
      [],
    )
    assert.equal(rows[0].id, 'pending:o9')
    assert.equal(rows[0].status, 'pending-sync')
    assert.equal(rows[0].synced, false)
  })

  it('镜像有而权威清单没有：不展示（权威清单是唯一事实源）', () => {
    const rows = mergeOntologyDocRows(
      [],
      [makeMirror({ id: 'd1', ontologyId: 'ghost' })],
    )
    assert.equal(rows.length, 0)
  })
})

describe('buildPalaceTree（本体文档目录）', () => {
  it('空清单时目录不出现；有行时固定根级首位且只读标记齐全', () => {
    const empty = buildPalaceTree([], [], [])
    assert.equal(empty.items[PALACE_ONTOLOGY_DOCS_DIR_ID], undefined)

    const rows = mergeOntologyDocRows(
      [makePublished({ ontologyId: 'o1', title: '供应文档' })],
      [makeMirror({ id: 'd1', ontologyId: 'o1' })],
    )
    const model = buildPalaceTree(
      [makeFile({ id: 'fa', filename: 'a.md' })],
      [makeFolder({ id: 'fd-1', path: '资料' })],
      rows,
    )
    const rootChildren = model.children[PALACE_TREE_ROOT]
    assert.equal(rootChildren[0], PALACE_ONTOLOGY_DOCS_DIR_ID, '本体文档目录钉在根级首位')
    assert.equal(model.items[PALACE_ONTOLOGY_DOCS_DIR_ID].ontologyDocsDir, true)
    assert.equal(model.items[PALACE_ONTOLOGY_DOCS_DIR_ID].dirId, undefined, '虚拟目录无目录行，不可拖拽/重命名')
    assert.deepEqual(model.children[PALACE_ONTOLOGY_DOCS_DIR_ID], [palaceOntologyDocId('d1')])
    assert.equal(model.items[palaceOntologyDocId('d1')].kind, 'ontology-doc')
    assert.equal(model.items[palaceOntologyDocId('d1')].name, '供应文档')
    // 计数徽章的数据基础：直接子节点数
    assert.equal(model.children[PALACE_ONTOLOGY_DOCS_DIR_ID].length, 1)
    assert.equal(model.children[palaceDirId('资料')].length, 0)
  })

  it('本体文档行与用户目录同名不冲突（节点 id 命名空间隔离）', () => {
    const rows = mergeOntologyDocRows(
      [makePublished({ ontologyId: 'o1', title: 'T' })],
      [makeMirror({ id: 'd1', ontologyId: 'o1' })],
    )
    const model = buildPalaceTree([], [makeFolder({ id: 'fd-x', path: '__ontology_docs__' })], rows)
    assert.notEqual(PALACE_ONTOLOGY_DOCS_DIR_ID, palaceDirId('__ontology_docs__'))
    assert.equal(model.items[palaceDirId('__ontology_docs__')].dirId, 'fd-x')
    assert.equal(model.items[PALACE_ONTOLOGY_DOCS_DIR_ID].ontologyDocsDir, true)
  })
})
