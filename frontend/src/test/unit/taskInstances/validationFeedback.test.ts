import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  buildCanvasNames, buildOkFeedback, buildValidationFeedback,
  parseStructuralErrors,
} from '../../../components/task-instances/validationFeedback.ts'
import type { WorkflowNode } from '../../../components/task-instances/workflowYaml.ts'

const node = (id: string, data: Record<string, unknown>): WorkflowNode =>
  ({ id, type: 'workflow', position: { x: 0, y: 0 }, data }) as WorkflowNode

// 与 WorkflowCanvas 预置图同构：start(agent) → gate(approval) → end(terminal)
const presetGraph = {
  nodes: [
    node('start', { kind: 'agent', system: '执行任务并产出结果' }),
    node('gate', { kind: 'approval', approvers: 'admin' }),
    node('end', { kind: 'terminal' }),
  ],
  edges: [
    { id: 'e1', source: 'start', target: 'gate' },
    { id: 'e2', source: 'gate', target: 'end' },
  ],
}

describe('validationFeedback 画布命名', () => {
  it('序列化 id／edges[N] 映射为画布可读名（同类型带序号）', () => {
    const names = buildCanvasNames(presetGraph)
    assert.equal(names.node.get('n1'), 'Agent 1')
    assert.equal(names.node.get('approval_2'), '审批 1')
    assert.equal(names.node.get('terminal_3'), '终点 1')
    assert.equal(names.edge.get(2), '连线「审批 1 → 终点 1」')
    assert.equal(names.canvasEdgeId.get(2), 'e2')
    assert.equal(names.canvasNodeId.get('approval_2'), 'gate')
  })
})

describe('validationFeedback 结构层（pydantic 英文原文）', () => {
  it('拆出 pydantic 2.13 多错误原文的 loc／type（loc 独占一行）', () => {
    const raw = [
      '2 validation errors for WorkflowSpec',
      'metadata.name',
      "  String should have at least 5 characters [type=string_too_short, input_value='我的流程', input_type=str]",
      '    For further information visit https://errors.pydantic.dev/2.13/v/string_too_short',
      'edges',
      '  Input should be a valid list [type=list_type, input_value=None, input_type=NoneType]',
      '    For further information visit https://errors.pydantic.dev/2.13/v/list_type',
    ].join('\n')
    assert.deepEqual(parseStructuralErrors(raw), [
      { loc: 'metadata.name', type: 'string_too_short', message: 'String should have at least 5 characters' },
      { loc: 'edges', type: 'list_type', message: 'Input should be a valid list' },
    ])
  })

  it('兼容内联形态（loc 与消息同行）', () => {
    const raw = "结构不合法: 1 validation error for WorkflowSpec\nmetadata.name String should have at least 5 characters [type=string_too_short, input_value='我的流程', input_type=str]"
    assert.deepEqual(parseStructuralErrors(raw), [
      { loc: 'metadata.name', type: 'string_too_short', message: 'String should have at least 5 characters' },
    ])
  })

  it('名称过短＋连线索表缺失 → 中文问题与修复动作，原文折叠进技术详情', () => {
    const error = {
      detail: {
        code: 'TEMPLATE_INVALID',
        message: '结构不合法: 2 validation errors for WorkflowSpec\n'
          + 'metadata.name\n'
          + "  String should have at least 5 characters [type=string_too_short, input_value='我的流程', input_type=str]\n"
          + '    For further information visit https://errors.pydantic.dev/2.13/v/string_too_short\n'
          + 'edges\n'
          + '  Input should be a valid list [type=list_type, input_value=None, input_type=NoneType]',
      },
    }
    const feedback = buildValidationFeedback(error, presetGraph)
    assert.equal(feedback.status, 'problems')
    assert.equal(feedback.summary, '发现 2 个问题')
    assert.equal(feedback.items[0].message, '模板名称至少 5 个字符')
    assert.match(feedback.items[0].action ?? '', /保存模板/)
    assert.equal(feedback.items[1].message, '流程连线列表缺失或格式不正确（edges 必须是列表）')
    assert.match(feedback.technicalDetail ?? '', /string_too_short/)
    // 主清单不出现英文原文／schema 路径／pydantic 外链（edges 是用户可见的 YAML 字段名，允许）
    for (const item of feedback.items) {
      assert.doesNotMatch(item.message, /[A-Za-z]{6,}/)
      assert.doesNotMatch(item.message, /pydantic|nodes\.|metadata\./)
    }
  })

  it('Agent 系统提示词为空 → 中文指明节点并可定位', () => {
    const error = {
      detail: {
        code: 'TEMPLATE_INVALID',
        message: '结构不合法: 1 validation error for WorkflowSpec\n'
          + 'nodes.n1.agent.system\n'
          + "  String should have at least 1 character [type=string_too_short, input_value='', input_type=str]",
      },
    }
    const feedback = buildValidationFeedback(error, presetGraph)
    assert.equal(feedback.items[0].message, 'Agent 节点「Agent 1」缺少系统提示词（至少 1 个字符）')
    assert.equal(feedback.items[0].nodeId, 'start')
  })

  it('未知结构错误 → 主提示只给安全区域摘要，schema 路径只留在技术详情', () => {
    const error = {
      detail: {
        code: 'TEMPLATE_INVALID',
        message: '结构不合法: 1 validation error for WorkflowSpec\n'
          + 'policies.max_parallelism Input should be a valid integer [type=int_type, input_value=\'x\', input_type=str]',
      },
    }
    const feedback = buildValidationFeedback(error, null)
    assert.equal(feedback.items.length, 1)
    assert.equal(feedback.items[0].message, '模板执行策略不合规')
    assert.doesNotMatch(feedback.items[0].message, /policies|max_parallelism|_/)
    assert.match(feedback.items[0].action ?? '', /技术详情/)
    assert.match(feedback.technicalDetail ?? '', /policies\.max_parallelism/)
    assert.match(feedback.technicalDetail ?? '', /int_type/)
  })

  it('无法归入已知区域的结构错误 → 通用中文摘要，不裸透路径', () => {
    const error = {
      detail: {
        code: 'TEMPLATE_INVALID',
        message: '结构不合法: 1 validation error for WorkflowSpec\n'
          + 'unknown_thing.bad_field Input should be a valid string [type=string_type]',
      },
    }
    const feedback = buildValidationFeedback(error, null)
    assert.equal(feedback.items.length, 1)
    assert.equal(feedback.items[0].message, '模板结构不合规')
    assert.doesNotMatch(feedback.items[0].message, /unknown_thing|bad_field/)
    assert.match(feedback.technicalDetail ?? '', /unknown_thing\.bad_field/)
  })
})

describe('validationFeedback 语义层（后端中文 + 内部索引）', () => {
  it('edges[N]／序列化 id 映射为画布名，补修复动作并可点击定位', () => {
    const error = {
      detail: {
        code: 'TEMPLATE_INVALID',
        message: '语义校验未通过',
        errors: [
          { code: 'PORT_REQUIRED', message: "edges[2]: approval 节点的出边必须显式端口（approved/rejected）", ref: 'edges[2]' },
          { code: 'TERMINAL_UNREACHABLE', message: "没有任何 terminal 节点从源点可达（源点: ['n1']）" },
        ],
      },
    }
    const feedback = buildValidationFeedback(error, presetGraph)
    assert.equal(feedback.status, 'problems')
    assert.equal(feedback.summary, '发现 2 个问题')
    assert.equal(feedback.items[0].message, '连线「审批 1 → 终点 1」: 审批节点的出边必须显式端口（通过／驳回）')
    assert.match(feedback.items[0].action ?? '', /通过.*驳回/)
    assert.equal(feedback.items[0].edgeId, 'e2')
    assert.equal(feedback.items[1].message, '没有任何终点节点从源点可达（源点: [「Agent 1」]）')
    assert.match(feedback.items[1].action ?? '', /连线/)
  })

  it('同因重复错误收敛为一条，独立问题各自可见（M7）', () => {
    const error = {
      detail: {
        code: 'TEMPLATE_INVALID',
        message: '语义校验未通过',
        errors: [
          { code: 'CYCLE', message: '检测到环: a -> b -> a', ref: 'edges[1]' },
          { code: 'CYCLE', message: '检测到环: a -> b -> a', ref: 'edges[1]' },
          { code: 'NO_TERMINAL', message: '流程必须至少包含一个 terminal 节点' },
        ],
      },
    }
    const feedback = buildValidationFeedback(error, null)
    assert.equal(feedback.summary, '发现 2 个问题')
    assert.equal(feedback.items.filter(item => item.message.includes('终点节点')).length, 1)
    // 主清单无内部索引／英文类型；原文（含 terminal）折叠进技术详情
    for (const item of feedback.items) {
      assert.doesNotMatch(item.message, /edges\[|terminal|approval/)
    }
    assert.match(feedback.technicalDetail ?? '', /terminal/)
  })

  it('YAML 模式语义错误同样人化：索引改序号、类型换中文，动作可执行，原文留技术详情', () => {
    const error = {
      detail: {
        code: 'TEMPLATE_INVALID',
        message: '语义校验未通过',
        errors: [
          { code: 'PORT_REQUIRED', message: 'edges[2]: approval 节点的出边必须显式端口（approved/rejected）', ref: 'edges[2]' },
          { code: 'PORT_REF', message: "edges[4]: 节点 n1 不存在端口 'bogus'（合法: ['done']）", ref: 'edges[4]' },
          { code: 'TERMINAL_UNREACHABLE', message: "没有任何 terminal 节点从源点可达（源点: ['n1']）" },
          { code: 'CONDITION_DEFAULT', message: '节点 n1: condition 必须配置 default 兜底路由', ref: 'n1' },
          { code: 'SOME_UNKNOWN_CODE', message: "edges[3]: from 引用了不存在的节点 'n9'" },
        ],
      },
    }
    const feedback = buildValidationFeedback(error, null)
    assert.equal(feedback.summary, '发现 5 个问题')
    assert.equal(feedback.items[0].message, '第 2 条连线: 审批节点的出边必须显式端口（通过／驳回）')
    assert.match(feedback.items[0].action ?? '', /\.approved（通过）.*\.rejected（驳回）/)
    assert.equal(feedback.items[1].message, '第 4 条连线: 节点 n1 不存在端口「bogus」（合法: [「done」]）')
    assert.match(feedback.items[1].action ?? '', /实际存在的输出端口/)
    assert.equal(feedback.items[2].message, '没有任何终点节点从源点可达（源点: [「n1」]）')
    assert.equal(feedback.items[3].message, '节点 n1: 条件必须配置 default 兜底路由')
    assert.match(feedback.items[3].action ?? '', /default 兜底路由/)
    assert.equal(feedback.items[4].message, '第 3 条连线: from 引用了不存在的节点「n9」')
    assert.match(feedback.items[4].action ?? '', /技术详情/)
    // 主清单无内部索引／英文类型／英文端口值；原文折叠进技术详情
    for (const item of feedback.items) {
      assert.doesNotMatch(item.message, /edges\[|terminal|approval|condition|approved|rejected/)
    }
    assert.match(feedback.technicalDetail ?? '', /PORT_REQUIRED \| edges\[2\]/)
    assert.match(feedback.technicalDetail ?? '', /PORT_REF/)
    assert.match(feedback.technicalDetail ?? '', /SOME_UNKNOWN_CODE/)
    assert.match(feedback.technicalDetail ?? '', /terminal/)
  })

  it('YAML 模式名称过短 → 指引直接改 metadata.name 并重验（F5），画布仍走保存弹窗', () => {
    const error = {
      detail: {
        code: 'TEMPLATE_INVALID',
        message: '结构不合法: 1 validation error for WorkflowSpec\n'
          + 'metadata.name\n'
          + "  String should have at least 5 characters [type=string_too_short, input_value='abc', input_type=str]",
      },
    }
    const feedback = buildValidationFeedback(error, null)
    assert.equal(feedback.items[0].message, '模板名称至少 5 个字符')
    assert.match(feedback.items[0].action ?? '', /metadata\.name/)
    assert.match(feedback.items[0].action ?? '', /重新校验/)
    assert.match(buildValidationFeedback(error, presetGraph).items[0].action ?? '', /保存模板/)
  })
})

describe('validationFeedback 服务异常与成功', () => {
  it('网络失败（无 detail）→ 服务文案，与配置错误可分辨（M10）', () => {
    const feedback = buildValidationFeedback({ message: 'Network Error' }, null)
    assert.equal(feedback.status, 'service')
    assert.match(feedback.summary, /这不是流程配置问题/)
  })

  it('5xx（detail 字符串）与 422（detail 数组）同样归为服务异常', () => {
    assert.equal(buildValidationFeedback({ detail: 'Internal Server Error', status: 500 }, null).status, 'service')
    const unprocessable = buildValidationFeedback({
      detail: [{ loc: ['body', 'spec_yaml'], msg: 'Field required' }], status: 422,
    }, null)
    assert.equal(unprocessable.status, 'service')
    assert.match(unprocessable.technicalDetail ?? '', /spec_yaml/)
  })

  it('成功反馈带节点／连线计数（画布）或简洁文案（YAML）', () => {
    assert.equal(
      buildOkFeedback(presetGraph).summary,
      '校验通过：3 个节点、2 条连线，可以保存模板')
    assert.equal(buildOkFeedback(null).summary, '校验通过，可以保存模板')
  })
})
