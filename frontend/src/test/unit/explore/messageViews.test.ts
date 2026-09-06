import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import {
  derivePlanFromSteps,
  emptyMessageView,
  foldStep,
  foldTextDelta,
} from '../../../pages/explore/messageViews.ts'
import type { BxStep } from '../../../api/exploration.ts'

const step = (tool: string, args: Record<string, unknown> = {}, error?: string): BxStep => ({
  tool,
  arguments: args,
  summary: 's',
  durationMs: 1,
  error,
})

describe('foldTextDelta / foldStep', () => {
  it('增量累积正文；工具步骤到达时叙述定格为回放块并清空正文', () => {
    let view = emptyMessageView()
    view = foldTextDelta(view, '正在')
    view = foldTextDelta(view, '读取画布…')
    assert.equal(view.content, '正在读取画布…')
    view = foldStep(view)
    assert.deepEqual(view.narrations, ['正在读取画布…'])
    assert.equal(view.content, '')
  })

  it('空正文时步骤不产生叙述块；空增量不改变视图', () => {
    const view = foldStep(emptyMessageView())
    assert.deepEqual(view.narrations, [])
    assert.deepEqual(foldTextDelta(view, ''), view)
  })
})

describe('derivePlanFromSteps', () => {
  it('取最后一次成功的 todo_write 生效', () => {
    const steps = [
      step('todo_write', { items: [{ content: '旧计划', status: 'done' }] }),
      step('upsert_elements', { kind: 'object' }),
      step('todo_write', {
        items: [
          { content: '建对象', status: 'done' },
          { content: '建行为', status: 'in_progress' },
          { content: '出图', status: 'pending' },
        ],
      }),
    ]
    assert.deepEqual(derivePlanFromSteps(steps), [
      { content: '建对象', status: 'done' },
      { content: '建行为', status: 'in_progress' },
      { content: '出图', status: 'pending' },
    ])
  })

  it('失败的 todo_write 被跳过；非法项被过滤', () => {
    const steps = [
      step('todo_write', { items: [{ content: '合法', status: 'pending' }] }),
      step('todo_write', {
        items: [
          { content: 'B', status: 'bad-status' },
          { content: '', status: 'done' },
          { content: 'C', status: 'done' },
        ],
      }, '工具执行失败'),
    ]
    assert.deepEqual(derivePlanFromSteps(steps), [{ content: '合法', status: 'pending' }])
  })

  it('没有 todo_write 时返回 null', () => {
    assert.equal(derivePlanFromSteps([step('upsert_elements'), step('show_diagram')]), null)
    assert.equal(derivePlanFromSteps([]), null)
  })
})
