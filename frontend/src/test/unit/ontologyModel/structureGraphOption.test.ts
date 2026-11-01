import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  buildStructureGraphOption,
  structureCircularPositions,
  structureNodeColor,
  structureNodeLabel,
} from '../../../pages/ontology-model/network/structureGraphOption.ts'
import type { StructureGraphData } from '../../../api/ontologyNetwork.ts'

const graph: StructureGraphData = {
  release: { releaseId: 'rel-1', version: 'v2', snapshotHash: 'hash-1' },
  status: 'ready',
  nodes: [
    { id: 'n1', kind: 'object_type', localId: 't1', name: 'customer', displayName: '客户' },
    { id: 'n2', kind: 'property', localId: 'p1', name: 'tax_id' },
    { id: 'n3', kind: 'link_type', localId: 'l1', name: 'places' },
    { id: 'n4', kind: 'mystery_kind', localId: 'm1', name: 'unknown-node' },
  ],
  edges: [{ id: 'e1', type: 'has_property', source: 'n1', target: 'n2' }],
  overlays: { joinAssertions: [] },
}

describe('structureNodeColor / structureNodeLabel', () => {
  it('maps known kinds to theme constants and falls back neutrally for unknown kinds', () => {
    assert.equal(structureNodeColor('object_type'), structureNodeColor('object_type'))
    assert.notEqual(structureNodeColor('object_type'), structureNodeColor('link_type'))
    assert.equal(structureNodeColor('mystery_kind'), structureNodeColor('another_unknown'))
  })

  it('prefers displayName over technical name', () => {
    assert.equal(structureNodeLabel(graph.nodes[0]), '客户')
    assert.equal(structureNodeLabel(graph.nodes[1]), 'tax_id')
  })
})

describe('structureCircularPositions (deterministic layout for stable E2E assertions)', () => {
  it('starts at the top and distributes evenly; single node sits at origin', () => {
    const one = structureCircularPositions(1)
    assert.deepEqual(one, [{ x: 0, y: 0 }])
    const four = structureCircularPositions(4)
    assert.equal(four.length, 4)
    assert.equal(four[0].x, 0)
    assert.ok(four[0].y < 0, 'first node is at the top (negative y)')
    // 对角节点坐标互为相反数（确定性圆环；加法比较规避 -0 严格相等陷阱）
    assert.equal(four[1].x + four[3].x, 0)
    assert.equal(four[1].y + four[3].y, 0)
  })

  it('returns empty for zero nodes', () => {
    assert.deepEqual(structureCircularPositions(0), [])
  })
})

describe('buildStructureGraphOption', () => {
  it('builds a none-layout graph series with nodes and links', () => {
    const option = buildStructureGraphOption(graph) as {
      animation: boolean
      series: { type: string; layout: string; data: { name: string }[]; links: { source: string; target: string }[] }[]
    }
    assert.equal(option.animation, false)
    assert.equal(option.series.length, 1)
    const series = option.series[0]
    assert.equal(series.type, 'graph')
    assert.equal(series.layout, 'none')
    assert.deepEqual(series.data.map(node => node.name), ['客户', 'tax_id', 'places', 'unknown-node'])
    assert.deepEqual(series.links.map(link => ({ source: link.source, target: link.target })), [{ source: 'n1', target: 'n2' }])
  })

  it('handles an empty graph without throwing', () => {
    const option = buildStructureGraphOption({ ...graph, nodes: [], edges: [] }) as {
      series: { data: unknown[]; links: unknown[] }[]
    }
    assert.deepEqual(option.series[0].data, [])
    assert.deepEqual(option.series[0].links, [])
  })
})
