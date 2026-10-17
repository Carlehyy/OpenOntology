import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  graphToYaml, parseYamlName, withMetadataName,
  type WorkflowNode,
} from '../../../components/task-instances/workflowYaml.ts'

const node = (id: string, data: Record<string, unknown>): WorkflowNode =>
  ({ id, type: 'workflow', position: { x: 0, y: 0 }, data }) as WorkflowNode

describe('task instances workflowYaml', () => {
  const nodes = [
    node('a', { kind: 'agent', system: '写发布说明' }),
    node('b', { kind: 'approval', approvers: 'admin,reviewer' }),
    node('c', { kind: 'terminal' }),
  ]
  const edges = [
    { id: 'e1', source: 'a', target: 'b' },
    { id: 'e2', source: 'b', target: 'c' },
  ]

  describe('graphToYaml', () => {
    it('把保存表单收集的名称/描述写入 metadata（P0-2）', () => {
      const yaml = graphToYaml(nodes, edges, { name: '发布说明生成', description: '生成发布说明' })
      assert.match(yaml, /^ {2}name: 发布说明生成$/m)
      assert.match(yaml, /^ {2}description: 生成发布说明$/m)
    })
    it('无描述时不输出 description 行', () => {
      const yaml = graphToYaml(nodes, edges, { name: 'X' })
      assert.doesNotMatch(yaml, /description:/)
    })
    it('节点与边按类型编号序列化', () => {
      const yaml = graphToYaml(nodes, edges, { name: 'X' })
      assert.match(yaml, /n1:\n {4}kind: agent\n {4}system: 写发布说明/)
      assert.match(yaml, /approval_2:\n {4}kind: approval\n {4}approvers: \[admin,reviewer\]/)
      assert.match(yaml, /terminal_3: \{ kind: terminal, outcome: success \}/)
      assert.match(yaml, /- \{ from: n1\.done, to: approval_2 \}/)
      assert.match(yaml, /- \{ from: approval_2, to: terminal_3 \}/) // 审批边无 sourceHandle → 不带端口
    })
    it('描述中的换行被折叠为空格，避免破坏 YAML 结构', () => {
      const yaml = graphToYaml(nodes, edges, { name: 'X', description: '第一行\n第二行' })
      assert.match(yaml, / {2}description: 第一行 第二行/)
    })
    it('零连线序列化为合法空数组（edges: []），不再是 YAML null（YIYA-26）', () => {
      const yaml = graphToYaml([node('a', { kind: 'agent', system: '写发布说明' }), node('c', { kind: 'terminal' })], [], { name: 'X' })
      assert.match(yaml, /^edges: \[\]$/m)
      assert.doesNotMatch(yaml, /^edges:\s*$/m)
    })
    it('审批出边带明确 sourceHandle 时原样保留，approved 与 rejected 互不串线（YIYA-26/M11）', () => {
      const yaml = graphToYaml(nodes, [
        { id: 'e1', source: 'a', target: 'b', sourceHandle: null },
        { id: 'e2', source: 'b', target: 'c', sourceHandle: 'approved' },
        { id: 'e3', source: 'b', target: 'a', sourceHandle: 'rejected' },
      ], { name: 'X' })
      assert.match(yaml, /- \{ from: approval_2\.approved, to: terminal_3 \}/)
      assert.match(yaml, /- \{ from: approval_2\.rejected, to: n1 \}/)
    })
    it('审批出边缺失/非法端口时不猜去向：输出不带端口，绝不自动补 done/approved（YIYA-43 选项1）', () => {
      const yaml = graphToYaml(nodes, [
        { id: 'e2', source: 'b', target: 'c' },
        { id: 'e3', source: 'b', target: 'a', sourceHandle: 'done' },
      ], { name: 'X' })
      assert.match(yaml, /- \{ from: approval_2, to: terminal_3 \}/)
      assert.match(yaml, /- \{ from: approval_2, to: n1 \}/)
      assert.doesNotMatch(yaml, /approval_2\./)
    })
    it('agent/human/join 出边仍是默认端口 .done（回归锚点）', () => {
      const human = node('h', { kind: 'human', role: '评审' })
      const join = node('j', { kind: 'join' })
      const end = node('c2', { kind: 'terminal' })
      const yaml = graphToYaml(
        [node('a', { kind: 'agent', system: 'x' }), human, join, end],
        [
          { id: 'e1', source: 'h', target: 'j', sourceHandle: 'approved' },
          { id: 'e2', source: 'j', target: 'c2' },
        ],
        { name: 'X' })
      assert.match(yaml, /- \{ from: n2\.done, to: join_3 \}/)
      assert.match(yaml, /- \{ from: join_3\.done, to: terminal_4 \}/)
    })
  })
})

describe('workflowYaml 标量与名称门禁', () => {
  it('多行系统提示词被折叠为单行，不产出非法 YAML（P1 对抗修复）', () => {
    const nodes = [node('a', { kind: 'agent', system: '第一行\n第二行' })]
    const yaml = graphToYaml(nodes, [], { name: 'X' })
    assert.match(yaml, /system: 第一行 第二行/)
    assert.doesNotMatch(yaml, /system: 第一行\n/)
  })

  it('条件节点默认路由兜底到真实存在的后继节点 id', () => {
    const nodes = [
      node('a', { kind: 'agent', system: 's' }),
      node('b', { kind: 'condition' }),
      node('c', { kind: 'terminal' }),
    ]
    const edges = [
      { id: 'e1', source: 'a', target: 'b' },
      { id: 'e2', source: 'b', target: 'c' },
    ]
    const yaml = graphToYaml(nodes, edges, { name: 'X' })
    // condition=b 的直连后继是 terminal（id 形如 terminal_N），绝不指向不存在的字面量
    assert.match(yaml, /default: \[terminal_\d+\]/)
  })

  it('作者填写的默认路由存在时优先使用', () => {
    const nodes = [
      node('a', { kind: 'agent', system: 's' }),
      node('b', { kind: 'condition', defaultTarget: 'n1' }),
      node('c', { kind: 'terminal' }),
    ]
    const edges = [{ id: 'e1', source: 'a', target: 'b' }, { id: 'e2', source: 'b', target: 'c' }]
    const yaml = graphToYaml(nodes, edges, { name: 'X' })
    assert.match(yaml, /default: \[n1\]/)
  })
})

describe('parseYamlName / withMetadataName（YAML 模式保存门禁）', () => {
  it('解析 2 空格缩进的 metadata.name', () => {
    assert.equal(parseYamlName('kind: Workflow\nmetadata:\n  name: 发布说明生成\n  description: x'), '发布说明生成')
    assert.equal(parseYamlName('kind: Workflow\nmetadata:\n  description: x'), '')
  })
  it('替换既有 name 行；缺失时插到 metadata 后或文末追加', () => {
    assert.match(
      withMetadataName('metadata:\n  name: 旧名\nnodes: {}', '新名'),
      /^ {2}name: 新名$/m)
    const inserted = withMetadataName('metadata:\n  description: x', '新名')
    assert.match(inserted, /metadata:\n {2}name: 新名\n {2}description: x/)
    const appended = withMetadataName('kind: Workflow\n', '新名')
    assert.match(appended, /metadata:\n {2}name: 新名\n$/)
  })
  it('空名称原样返回（调用方必填校验后的防御）', () => {
    assert.equal(withMetadataName('metadata:\n  name: 旧\n', '  '), 'metadata:\n  name: 旧\n')
  })
})
