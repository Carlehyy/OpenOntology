import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import type { WorldModelServiceInfo } from '../../api/worldModel.ts'
import { findServiceByOntology } from '../../pages/world-model/publishPrefill.ts'


function service(id: string, ontologyId: string | null): WorldModelServiceInfo {
  return {
    id,
    project_id: 'p1',
    version_id: 'v1',
    version_no: 1,
    name: `服务-${id}`,
    description: '',
    status: 'online',
    endpoint_path: `/api/v2/world-model/services/${id}/invoke`,
    applicable_object_types: ontologyId
      ? { ontology_id: ontologyId, object_type_ids: ['ot-1'] }
      : null,
    preconditions: [],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  }
}


describe('findServiceByOntology（发布弹窗按本体回填）', () => {
  it('返回绑定该本体的服务（多本体覆盖更新的回填来源）', () => {
    const svcA = service('svc-a', 'ontology-a')
    const svcB = service('svc-b', 'ontology-b')
    assert.equal(findServiceByOntology([svcA, svcB], 'ontology-b'), svcB)
    assert.equal(findServiceByOntology([svcA, svcB], 'ontology-a'), svcA)
  })

  it('该本体尚未发布过 → null（新增发布，回填项目默认值）', () => {
    assert.equal(findServiceByOntology([service('svc-a', 'ontology-a')], 'ontology-c'), null)
  })

  it('未选择本体或服务列表为空 → null', () => {
    assert.equal(findServiceByOntology([service('svc-a', 'ontology-a')], ''), null)
    assert.equal(findServiceByOntology([], 'ontology-a'), null)
  })

  it('跳过语义注册缺失（applicable_object_types 为空）的服务', () => {
    const unbound = service('svc-unbound', null)
    assert.equal(findServiceByOntology([unbound], 'ontology-a'), null)
  })
})
