import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { menuKeyForPath } from '../../../stores/tabTitle.ts'

describe('menuKeyForPath', () => {
  it('分辨各菜单域与未知路径', () => {
    assert.equal(menuKeyForPath('/ontologies/123'), 'ontologies')
    assert.equal(menuKeyForPath('/ontology-model/network'), 'ontology_model.network')
    assert.equal(menuKeyForPath('/ontology-model'), 'ontology_model')
    assert.equal(menuKeyForPath('/settings/domains'), 'system_settings')
    assert.equal(menuKeyForPath('/world-model/models/m1/develop'), 'world_model.models')
    assert.equal(menuKeyForPath('/world-model/services'), 'world_model.services')
    assert.equal(menuKeyForPath('/world-model/services/svc-1'), 'world_model.services')
    assert.equal(menuKeyForPath('/world-model/calls'), 'world_model.calls')
    assert.equal(menuKeyForPath('/data/pipelines/sync-tasks'), 'data.sync_tasks')
    assert.equal(menuKeyForPath('/data/structured'), 'data.structured')
    assert.equal(menuKeyForPath('/data/pipelines/steward'), 'data.pipelines')
    assert.equal(menuKeyForPath('/agent/reports'), 'agent')
    assert.equal(menuKeyForPath('/scenes'), 'scenes')
    assert.equal(menuKeyForPath('/scenes/scn-1'), 'scenes')
    assert.equal(menuKeyForPath('/scenes/scn-1?tab=models'), 'scenes')
    assert.equal(menuKeyForPath('/api-hub/interfaces'), 'api_hub.interfaces')
    assert.equal(menuKeyForPath('/no-such-page'), null)
  })
})
