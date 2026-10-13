/**
 * 画布 ↔ WorkflowSpec YAML 序列化（纯函数，node:test 直接单测）。
 * 从 WorkflowCanvas 抽出：保存弹窗收集的名称/描述经此写入
 * metadata（修复「画布创建的模板全部叫『我的流程』」）；
 * YAML 模式的保存同样经名称门禁（parseYamlName 预填 +
 * withMetadataName 回写），杜绝默认名直出。
 */
import type { Edge, Node } from '@xyflow/react'

export interface WorkflowNodeData {
  [key: string]: unknown
  kind: 'agent' | 'human' | 'approval' | 'condition' | 'join' | 'terminal'
  system?: string
  role?: string
  approvers?: string
  branches?: string
  defaultTarget?: string
}

export type WorkflowNode = Node<WorkflowNodeData & Record<string, unknown>>

export interface FlowMeta {
  name: string
  description?: string
}

/** 标量字段折叠换行：多行输入（提示词 textarea）直插会生成非法 YAML */
function scalar(value: string | undefined, fallback: string): string {
  const text = (value ?? '').replace(/\r?\n/g, ' ').trim()
  return text || fallback
}

/** 图 → YAML 文本；meta.name 必填（保存弹单已校验），描述可选。 */
export function graphToYaml(nodes: WorkflowNode[], edges: Edge[], meta: FlowMeta): string {
  const nodeEntries: string[] = []
  const edgeEntries: string[] = []
  let counter = 1
  const idMap = new Map<string, string>()

  for (const node of nodes) {
    const kind = (node.data as WorkflowNodeData).kind
    const id = kind === 'agent' || kind === 'human'
      ? `n${counter}`
      : `${kind}_${counter}`
    idMap.set(node.id, id)
    counter += 1
  }

  for (const node of nodes) {
    const id = idMap.get(node.id) ?? node.id
    const d = node.data as WorkflowNodeData
    switch (d.kind) {
      case 'agent':
        nodeEntries.push(`  ${id}:\n    kind: agent\n    system: ${scalar(d.system, '执行任务')}`)
        break
      case 'human':
        nodeEntries.push(`  ${id}:\n    kind: human\n    role: ${scalar(d.role, '评审员')}`)
        break
      case 'approval':
        nodeEntries.push(`  ${id}:\n    kind: approval\n    approvers: [${scalar(d.approvers, 'admin')}]`)
        break
      case 'condition': {
        // 默认路由兜底：优先作者填写且存在于图中的目标，其次该条件节点的
        // 直连后继，最后图中首个非自身节点——避免指向不存在的字面量 id
        const authored = (d.defaultTarget ?? '').trim()
        const authoredId = [...idMap.values()].find(candidate => candidate === authored)
        const successor = edges
          .filter(edge => edge.source === node.id)
          .map(edge => idMap.get(edge.target))
          .find(Boolean)
        const fallback = authoredId
          ?? successor
          ?? [...idMap.values()].find(candidate => candidate !== id) ?? id
        nodeEntries.push(`  ${id}:\n    kind: condition\n    on: ${scalar(d.branches, 'n1.done')}\n    branches: []\n    default: [${fallback}]`)
        break
      }
      case 'join':
        nodeEntries.push(`  ${id}:\n    kind: join\n    mode: all`)
        break
      case 'terminal':
        nodeEntries.push(`  ${id}: { kind: terminal, outcome: success }`)
        break
    }
  }

  for (const edge of edges) {
    const src = idMap.get(edge.source) ?? edge.source
    const dst = idMap.get(edge.target) ?? edge.target
    edgeEntries.push(`  - { from: ${src}.done, to: ${dst} }`)
  }

  const descriptionLine = meta.description?.trim()
    ? `\n  description: ${scalar(meta.description, '')}` : ''
  return `api_version: openontology.task/v1
kind: Workflow
metadata:
  name: ${meta.name}${descriptionLine}
nodes:
${nodeEntries.join('\n')}
edges:
${edgeEntries.join('\n')}
`
}

/** 从 YAML 文本解析 metadata.name（2 空格缩进的 name 行；无则空串）。 */
export function parseYamlName(yaml: string): string {
  const match = yaml.match(/^ {2}name:[ \t]*(.*)$/m)
  return (match?.[1] ?? '').trim()
}

/**
 * 把名称写回 YAML 文本：替换既有 metadata.name 行；缺失时插到
 * metadata: 行后；仍无则在文末追加 metadata 块（YAML 映射键序无关）。
 */
export function withMetadataName(yaml: string, name: string): string {
  const trimmed = name.trim()
  if (!trimmed) return yaml
  if (/^ {2}name:[ \t]*.*$/m.test(yaml)) {
    return yaml.replace(/^ {2}name:[ \t]*.*$/m, `  name: ${trimmed}`)
  }
  if (/^metadata:[ \t]*$/m.test(yaml)) {
    return yaml.replace(/^metadata:[ \t]*$/m, `metadata:\n  name: ${trimmed}`)
  }
  return `${yaml.replace(/\n*$/, '')}\nmetadata:\n  name: ${trimmed}\n`
}
