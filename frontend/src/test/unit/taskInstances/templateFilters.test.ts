import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { TemplateFilterInput } from '../../../components/task-instances/templateFilters.ts'
import {
  filterTemplates, isPresetTemplate, templateEmptyKind,
} from '../../../components/task-instances/templateFilters.ts'

interface Fixture extends TemplateFilterInput {
  id: string
  description?: string
}

const fixture = (id: string, name: string, is_preset?: boolean,
  description = ''): Fixture => ({ id, name, is_preset, description })

// 预置 + 个人混合列表：覆盖大小写、首尾空格、描述干扰项
const templates: Fixture[] = [
  fixture('p1', '代码变更评审', true, '条件分流 + 关口审批'),
  fixture('p2', 'Release Notes 生成', true),
  fixture('u1', '代码变更评审-我的副本'),
  fixture('u2', '发布说明', undefined, '描述里含 代码变更评审 关键词但名称不含'),
  fixture('u3', '未命名流程'),
]

describe('task instances templateFilters', () => {
  describe('isPresetTemplate', () => {
    it('only is_preset===true counts as preset; undefined/false are personal', () => {
      assert.equal(isPresetTemplate({ is_preset: true }), true)
      assert.equal(isPresetTemplate({ is_preset: false }), false)
      // API 缺省 is_preset 视为用户自建
      assert.equal(isPresetTemplate({}), false)
    })
  })

  describe('filterTemplates', () => {
    it('default preset source lists only preset templates', () => {
      const result = filterTemplates(templates, { keyword: '', source: 'preset' })
      assert.deepEqual(result.map(t => t.id), ['p1', 'p2'])
    })

    it('personal source includes templates without is_preset marker', () => {
      const result = filterTemplates(templates, { keyword: '', source: 'personal' })
      assert.deepEqual(result.map(t => t.id), ['u1', 'u2', 'u3'])
    })

    it('matches name substring case-insensitively and ignores surrounding spaces', () => {
      const result = filterTemplates(templates, {
        keyword: '  release NOTES  ', source: 'preset' })
      assert.deepEqual(result.map(t => t.id), ['p2'])
    })

    it('description-only matches are never selected', () => {
      const result = filterTemplates(templates, {
        keyword: '描述里含', source: 'personal' })
      assert.deepEqual(result, [])
    })

    it('keyword and source intersect, not union', () => {
      const presetHits = filterTemplates(templates, {
        keyword: '代码变更评审', source: 'preset' })
      assert.deepEqual(presetHits.map(t => t.id), ['p1'])
      const personalHits = filterTemplates(templates, {
        keyword: '代码变更评审', source: 'personal' })
      assert.deepEqual(personalHits.map(t => t.id), ['u1'])
    })

    it('clearing keyword keeps the selected source', () => {
      const cleared = filterTemplates(templates, { keyword: '', source: 'personal' })
      const neverTyped = filterTemplates(templates, {
        keyword: '发布说明', source: 'personal' })
      const after = filterTemplates(templates, { keyword: '   ', source: 'personal' })
      assert.deepEqual(after.map(t => t.id), cleared.map(t => t.id))
      assert.deepEqual(neverTyped.map(t => t.id), ['u2'])
    })
  })

  describe('templateEmptyKind', () => {
    it('empty platform list is the global none state regardless of filters', () => {
      assert.equal(templateEmptyKind([], {
        keyword: 'x', source: 'personal' }), 'none')
      assert.equal(templateEmptyKind([], {
        keyword: '', source: 'preset' }), 'none')
    })

    it('category empty is reported when the source has no templates at all', () => {
      const onlyPreset = [templates[0], templates[1]]
      assert.equal(templateEmptyKind(onlyPreset, {
        keyword: '', source: 'personal' }), 'category')
      assert.equal(templateEmptyKind(onlyPreset, {
        keyword: '任意词', source: 'personal' }), 'category')
    })

    it('keyword no-match is distinguished from category empty', () => {
      assert.equal(templateEmptyKind(templates, {
        keyword: '不存在的模板', source: 'personal' }), 'nomatch')
      assert.equal(templateEmptyKind(templates, {
        keyword: '描述里含', source: 'personal' }), 'nomatch')
    })

    it('returns null when results exist', () => {
      assert.equal(templateEmptyKind(templates, {
        keyword: '', source: 'preset' }), null)
      assert.equal(templateEmptyKind(templates, {
        keyword: '发布说明', source: 'personal' }), null)
    })
  })
})
