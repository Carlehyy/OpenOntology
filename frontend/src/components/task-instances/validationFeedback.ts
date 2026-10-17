/**
 * 校验反馈构建（纯函数，node:test 直接单测）：把后端两道校验闸的原始
 * 报错翻译成可执行的中文问题清单，并区分服务异常与配置错误。
 *
 * - 结构层（pydantic 英文原文）：已知错误（名称过短／连线列表缺失／
 *   Agent 提示词为空）映射为「问题 + 修复动作」；未知错误只给安全的中文
 *   区域摘要（主提示不出现 schema／内部字段路径），原文折叠进技术详情。
 * - 语义层（后端已中文，但引用序列化 id／edges[N] 内部索引）：画布模式
 *   借助 graphToYaml 同一套 stableNodeIds 把内部 id 映射回画布对象名
 *   （如「审批 1」「连线「Agent 1 → 终点 1」」）；YAML 模式同样人化——
 *   索引改序号说法、英文类型换中文标签，两种模式都按错误码补修复动作。
 * - 服务层（网络／5xx／422）：统一「非流程配置问题」文案，与配置错误
 *   可分辨；技术详情折叠可查。
 */
import type { Edge } from '@xyflow/react'

import { stableNodeIds, WORKFLOW_KIND_LABELS, type WorkflowNode, type WorkflowNodeData } from './workflowYaml.ts'
import { apiError } from '../../api/taskInstances.ts'

export interface ValidationItem {
  key: string
  message: string
  action?: string
  nodeId?: string
  edgeId?: string
}

export interface ValidationFeedback {
  status: 'ok' | 'problems' | 'service'
  summary: string
  items: ValidationItem[]
  /** 服务端原始报错（结构层英文原文／服务错误信息），默认折叠展示 */
  technicalDetail?: string
}

export interface CanvasGraphContext {
  nodes: WorkflowNode[]
  edges: Edge[]
}

const SERVICE_SUMMARY = '校验服务暂时不可用，请稍后重试（这不是流程配置问题）'

/** 语义错误码 → 修复动作（动作里不出现内部 id，节点由 nodeId 定位） */
const SEMANTIC_ACTIONS: Record<string, string> = {
  NODE_COUNT: '调整节点数量到 1~200 个之间',
  EDGE_COUNT: '从节点右侧的圆点拖到下一个节点，把节点串起来（流程至少需要 1 条连线）',
  EDGE_REF: '删除这条无效连线，再按节点实际存在的方向重新连接',
  EDGE_FROM_FORBIDDEN: '终点／条件节点不能向外连线：删除这条连线，改由其他节点发出',
  EDGE_TO_CONDITION: '条件节点不接受普通连线：删除这条连线，在其属性面板用「条件表达式」声明上游',
  PORT_REQUIRED: '审批节点需要明确去向：删除这条连线，从审批节点的「通过」或「驳回」输出端口重新拖出',
  PORT_REF: '删除这条连线，从节点实际存在的输出端口重新连接',
  REWORK_TARGET: '打回边只能指向可重做的 Agent／人工节点：重新连接打回目标',
  CYCLE: '删除构成环的一条连线，打断循环后重新校验',
  REWORK_CYCLE: '删除构成环的一条打回连线，打断循环后重新校验',
  NO_TERMINAL: '从左侧节点面板添加一个「终点」节点',
  TERMINAL_UNREACHABLE: '检查连线，确保从起始节点沿着连线能够到达终点节点',
  CONDITION_ON: '在条件节点属性面板把「条件表达式」改为引用存在的节点输出（如 n1.done）',
  CONDITION_DEFAULT: '在条件节点属性面板填写「默认路由」，作为所有分支都不命中时的兜底去向',
  BRANCH_REF: '在条件节点属性面板修正分支目标，指向图中存在的节点',
  CONTRACT_REF: '先在 contracts 中声明该契约，或移除端口上的契约引用',
  CONTRACT_INVALID: '按契约白名单修正契约声明',
  CONTRACT_COUNT: '精简契约数量（最多 128 个）',
  NODE_ID: '节点 id 只允许字母／数字／下划线／连字符，请修正后重试',
}

/** YAML 模式的修复动作（全错误码分表）：画布的拖拽／属性面板动作在文本
 * 编辑场景不可执行，每条都给可直接落笔的 YAML 改法；端口值 .approved／
 * .rejected 只作为修复示例出现，由用户按意图选择。 */
const SEMANTIC_ACTIONS_YAML: Record<string, string> = {
  NODE_COUNT: '调整 nodes 数量到 1~200 个之间',
  EDGE_COUNT: '在 edges 列表补一条连线（如 { from: n1.done, to: end }）',
  EDGE_REF: '修正或删除对应的 edges 条目：from／to 必须写 nodes 中存在的节点 id',
  EDGE_FROM_FORBIDDEN: '删除这条出边：终点／条件节点不能向外连线（条件节点的去向在 branches／default 声明）',
  EDGE_TO_CONDITION: '删除这条入边：条件节点的上游在其 on 字段声明',
  PORT_REQUIRED: '为这条审批出边的 from 写明端口：节点 id 后加 .approved（通过）或 .rejected（驳回），按实际意图二选一',
  PORT_REF: '把该出边 from 引用的端口改为实际存在的输出端口；审批节点的端口是 .approved（通过）或 .rejected（驳回）',
  REWORK_TARGET: '打回边目标须为可重做的 agent／human 节点：修正 to，或去掉 rework 标记',
  CYCLE: '删除构成环的一条 edges 条目，打断循环后重新校验',
  REWORK_CYCLE: '删除构成环的一条打回边（rework: true），打断循环后重新校验',
  NO_TERMINAL: '在 nodes 中添加 kind 为 terminal 的终点节点',
  TERMINAL_UNREACHABLE: '检查 edges 与条件分支目标：从起始节点沿连线必须能到达终点节点',
  CONDITION_ON: '把条件节点的 on 字段改为引用存在的节点输出（如 n1.done）',
  CONDITION_DEFAULT: '为条件节点填写 default 兜底路由，作为所有分支都不命中时的去向',
  BRANCH_REF: '修正条件节点 branches／default 引用的目标节点 id，指向 nodes 中存在的节点',
  CONTRACT_REF: '在 contracts 中先声明该契约，或删除端口对它的引用',
  CONTRACT_INVALID: '按契约白名单修正 contracts 中的声明',
  CONTRACT_COUNT: '精简 contracts 数量（最多 128 个）',
  NODE_ID: '修正 nodes 里的节点 id：只允许字母／数字／下划线／连字符',
}

/** 结构错误已知映射之外的 loc 首段 → 安全的中文区域名（主提示不出现 schema 路径） */
const STRUCTURAL_AREA_LABELS: Record<string, string> = {
  metadata: '基本信息',
  nodes: '节点配置',
  edges: '连线配置',
  policies: '执行策略',
  contracts: '契约声明',
  entry: '入口配置',
}

// ─── 画布对象命名 ───

interface CanvasNames {
  /** 序列化 id（n1／approval_2）→ 画布可读名（「Agent 1」「审批 2」） */
  node: Map<string, string>
  /** 序列化 id → 画布节点 id（点击定位用） */
  canvasNodeId: Map<string, string>
  /** edges[N]（1 基）→ 连线可读名 */
  edge: Map<number, string>
  /** edges[N]（1 基）→ 画布连线 id */
  canvasEdgeId: Map<number, string>
}

/**
 * 画布对象名：类型标签 + 同类型序号（画布顺序）。画布节点没有用户命名，
 * 序号保证多个同类节点可区分，且与画布从上到下的排列一致。
 */
export function buildCanvasNames(graph: CanvasGraphContext): CanvasNames {
  const idMap = stableNodeIds(graph.nodes)
  const kindCount = new Map<string, number>()
  const node = new Map<string, string>()
  const canvasNodeId = new Map<string, string>()
  for (const canvasNode of graph.nodes) {
    const kind = (canvasNode.data as WorkflowNodeData).kind
    const serialId = idMap.get(canvasNode.id)
    if (!serialId) continue
    const ordinal = (kindCount.get(kind) ?? 0) + 1
    kindCount.set(kind, ordinal)
    node.set(serialId, `${WORKFLOW_KIND_LABELS[kind] ?? kind} ${ordinal}`)
    canvasNodeId.set(serialId, canvasNode.id)
  }
  const edge = new Map<number, string>()
  const canvasEdgeId = new Map<number, string>()
  graph.edges.forEach((flowEdge, index) => {
    const source = idMap.get(flowEdge.source) ?? flowEdge.source
    const target = idMap.get(flowEdge.target) ?? flowEdge.target
    edge.set(index + 1, `连线「${node.get(source) ?? source} → ${node.get(target) ?? target}」`)
    canvasEdgeId.set(index + 1, flowEdge.id)
  })
  return { node, canvasNodeId, edge, canvasEdgeId }
}

/**
 * 语义文案中文化（画布／YAML 共用）：英文节点类型换中文标签、端口值用
 * 「通过／驳回」说法（具体 .approved／.rejected 只出现在修复示例）、repr
 * 引号统一「」，并收敛替换后残留的中文字间空格。节点 id 是可见字段值，
 * 保留即可定位；服务端原文进 technicalDetail。
 */
function localizeSemanticText(text: string): string {
  return text
    .replace(/\b(agent|human|approval|condition|terminal|join)\b/g,
      kind => WORKFLOW_KIND_LABELS[kind as keyof typeof WORKFLOW_KIND_LABELS])
    .replace(/\bapproved\b/g, '通过')
    .replace(/\brejected\b/g, '驳回')
    .replace(/通过\/驳回/g, '通过／驳回')
    .replace(/'([^']*)'/g, '「$1」')
    // 英文词换成中文标签／「」引号后残留的空格收敛（中文与中文、中文与「之间）
    .replace(/([\u4e00-\u9fff」]) +(?=[\u4e00-\u9fff「])/g, '$1')
}

/** 把语义错误文案里的序列化 id／edges[N] 替换为画布可读名。 */
function humanizeSemanticText(text: string, names: CanvasNames): string {
  let result = text.replace(/edges\[(\d+)\]/g, (whole, index: string) =>
    names.edge.get(Number(index)) ?? whole)
  for (const [serialId, label] of names.node) {
    result = result.replace(new RegExp(`\\b${serialId.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'g'), label)
  }
  return localizeSemanticText(result)
}

/** YAML 模式：`edges[N]` 内部索引改为序号说法后再做同套中文化。 */
function humanizeYamlSemanticText(text: string): string {
  return localizeSemanticText(
    text.replace(/edges\[(\d+)\]/g, (_whole, index: string) => `第 ${index} 条连线`))
}

/** 语义条目的定位：ref（edges[N]／节点 id）→ 画布 node／edge id */
function locateRef(ref: string | undefined, names: CanvasNames): { nodeId?: string, edgeId?: string } {
  if (!ref) return {}
  const edgeRef = ref.match(/^edges\[(\d+)\]$/)
  if (edgeRef) {
    const edgeId = names.canvasEdgeId.get(Number(edgeRef[1]))
    return edgeId ? { edgeId } : {}
  }
  const nodeId = names.canvasNodeId.get(ref)
  return nodeId ? { nodeId } : {}
}

// ─── 结构层（pydantic 英文原文）解析 ───

interface StructuralEntry {
  loc: string
  message: string
  type: string
}

/**
 * 拆 pydantic ValidationError 文案：剥离统计行／外链行，逐条取 loc＋msg＋type。
 * pydantic 2.13 实际格式（本地 backend venv 实测）为 loc 独占一行、
 * 消息缩进下一行尾随 [type=...]；兼容旧版 loc+消息同行的内联形态。
 */
export function parseStructuralErrors(raw: string): StructuralEntry[] {
  const lines = raw.split('\n')
    .map(line => line.trim())
    .filter(line => line
      && !/(?:^|\s)\d+ validation errors? for /.test(line)
      && !/^For further information visit /i.test(line))
  const entries: StructuralEntry[] = []
  for (let index = 0; index < lines.length; index += 1) {
    const match = lines[index].match(/^(.*?)\s*\[type=([^,\]]+)[^\]]*\]$/)
    if (!match) continue
    const head = match[1]
    const previous = index > 0 ? lines[index - 1] : ''
    if (previous && !previous.includes('[type=')) {
      // loc 独占上一行（pydantic 2.13）
      entries.push({ loc: previous, message: head, type: match[2] })
      continue
    }
    // 内联形态：loc 是首个不含空白的 token（metadata.name／edges）
    const separator = head.indexOf(' ')
    if (separator === -1) {
      entries.push({ loc: head, message: '', type: match[2] })
    } else {
      entries.push({ loc: head.slice(0, separator), message: head.slice(separator + 1), type: match[2] })
    }
  }
  return entries
}

/** 单条结构错误 → 中文问题＋动作；未知返回 null（调用方走通用条目）。 */
function structuralItem(entry: StructuralEntry, names: CanvasNames | null,
): ValidationItem | null {
  if (entry.type === 'string_too_short' && entry.loc === 'metadata.name') {
    return {
      key: `struct:${entry.loc}:${entry.type}`,
      message: '模板名称至少 5 个字符',
      action: names
        ? '点「保存模板」，在名称栏填写至少 5 个字符的名称（名称会成为模板的唯一标识）'
        : '在 metadata.name 处填写至少 5 个字符的模板名称，然后重新校验（名称会成为模板的唯一标识）',
    }
  }
  if (entry.type === 'list_type' && entry.loc === 'edges') {
    return {
      key: `struct:${entry.loc}:${entry.type}`,
      message: '流程连线列表缺失或格式不正确（edges 必须是列表）',
      action: '画布模式检查连线即可；YAML 模式请确认 edges 为列表（没有连线时写 edges: []）',
    }
  }
  const agentSystem = entry.loc.match(/^nodes\.([^.]+)\.agent\.system$/)
  if (entry.type === 'string_too_short' && agentSystem) {
    const label = names?.node.get(agentSystem[1]) ?? `Agent（${agentSystem[1]}）`
    const nodeId = names?.canvasNodeId.get(agentSystem[1])
    return {
      key: `struct:${entry.loc}:${entry.type}`,
      message: `Agent 节点「${label}」缺少系统提示词（至少 1 个字符）`,
      action: names
        ? '点击该节点，在右侧属性面板填写「系统提示词」'
        : '为该节点的 system 字段填写系统提示词（至少 1 个字符）后重新校验',
      ...(nodeId ? { nodeId } : {}),
    }
  }
  return null
}

// ─── 对外构建入口 ───

/** 校验通过反馈；有画布上下文时附节点／连线计数。 */
export function buildOkFeedback(graph: CanvasGraphContext | null): ValidationFeedback {
  return {
    status: 'ok',
    summary: graph
      ? `校验通过：${graph.nodes.length} 个节点、${graph.edges.length} 条连线，可以保存模板`
      : '校验通过，可以保存模板',
    items: [],
  }
}

/**
 * 失败反馈：区分语义／结构／服务三层。graph 仅画布模式传入（YAML 模式
 * 的 id 就是文本里可见的 id，无需映射）。
 */
export function buildValidationFeedback(
  error: unknown,
  graph: CanvasGraphContext | null,
): ValidationFeedback {
  const detail = (error as { detail?: unknown } | null | undefined)?.detail
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    const parsed = detail as {
      code?: string
      message?: string
      errors?: Array<{ code?: string, message?: string, ref?: string }>
    }
    if (parsed.code === 'TEMPLATE_INVALID' && Array.isArray(parsed.errors)) {
      return semanticFeedback(parsed.errors, graph)
    }
    if (parsed.code === 'TEMPLATE_INVALID' && typeof parsed.message === 'string') {
      return structuralFeedback(parsed.message, graph)
    }
  }
  return serviceFeedback(error)
}

function semanticFeedback(
  errors: Array<{ code?: string, message?: string, ref?: string }>,
  graph: CanvasGraphContext | null,
): ValidationFeedback {
  const names = graph ? buildCanvasNames(graph) : null
  const items: ValidationItem[] = []
  const seen = new Set<string>()
  for (const issue of errors) {
    const rawMessage = issue.message ?? '未知校验错误'
    const key = `${issue.code ?? ''}|${rawMessage}|${issue.ref ?? ''}`
    if (seen.has(key)) continue
    seen.add(key)
    const located = names ? locateRef(issue.ref, names) : {}
    const action = names
      ? issue.code ? SEMANTIC_ACTIONS[issue.code] : undefined
      // YAML 全错误码分表；表外新码不回退画布措辞，给对照修正并重验的兜底
      : (issue.code && SEMANTIC_ACTIONS_YAML[issue.code])
        ?? '对照下方「技术详情」中的原始报错修正 YAML 后重新校验'
    items.push({
      key: `semantic:${key}:${items.length}`,
      message: names ? humanizeSemanticText(rawMessage, names)
        : humanizeYamlSemanticText(rawMessage),
      ...(action ? { action } : {}),
      ...located,
    })
  }
  const feedback: ValidationFeedback = {
    status: 'problems',
    summary: items.length === 1 ? '发现 1 个问题' : `发现 ${items.length} 个问题`,
    items,
  }
  if (!names) {
    // YAML 模式：人化后的主清单不出现内部索引／英文类型，服务端原文折叠进详情
    feedback.technicalDetail = errors
      .map(issue => [issue.code, issue.message, issue.ref].filter(Boolean).join(' | '))
      .join('\n')
  }
  return feedback
}

function structuralFeedback(rawMessage: string, graph: CanvasGraphContext | null): ValidationFeedback {
  const names = graph ? buildCanvasNames(graph) : null
  const text = rawMessage.replace(/^结构不合法:\s*/, '')
  const fallbackAction = names
    ? '对照「技术详情」中的原始报错修正画布配置后重新校验'
    : '对照「技术详情」中的原始报错修正 YAML 后重新校验'
  const items: ValidationItem[] = []
  for (const entry of parseStructuralErrors(text)) {
    const known = structuralItem(entry, names)
    if (known) {
      items.push(known)
      continue
    }
    const area = STRUCTURAL_AREA_LABELS[entry.loc.split('.')[0]]
    items.push({
      key: `struct-generic:${entry.loc}:${items.length}`,
      message: area ? `模板${area}不合规` : '模板结构不合规',
      action: fallbackAction,
    })
  }
  // 解析不出任何条目时保底一条，保证用户始终能看到问题存在
  if (items.length === 0) {
    items.push({
      key: 'struct-unknown:0',
      message: '模板基本信息或结构不合规',
      action: fallbackAction,
    })
  }
  return {
    status: 'problems',
    summary: items.length === 1 ? '发现 1 个问题' : `发现 ${items.length} 个问题`,
    items,
    technicalDetail: text,
  }
}

function serviceFeedback(error: unknown): ValidationFeedback {
  const detail = (error as { detail?: unknown } | null | undefined)?.detail
  const technical = Array.isArray(detail)
    ? JSON.stringify(detail)
    : typeof detail === 'string' && detail ? detail : apiError(error)
  return {
    status: 'service',
    summary: SERVICE_SUMMARY,
    items: [],
    technicalDetail: technical,
  }
}
