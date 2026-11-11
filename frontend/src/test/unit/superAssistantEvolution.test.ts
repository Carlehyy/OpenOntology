import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { candidateActions } from '../../pages/super-assistant/components/evolutionLogic.ts'

describe('candidateActions', () => {
  it('memory and skill candidates use accept/reject', () => {
    assert.deepEqual(candidateActions('memory').map(action => action.decision), ['accept', 'reject'])
    assert.deepEqual(candidateActions('skill').map(action => action.decision), ['accept', 'reject'])
  })

  it('conflict candidates offer the three-way decision', () => {
    assert.deepEqual(
      candidateActions('conflict').map(action => action.decision),
      ['new_supersedes', 'keep_old', 'skip'],
    )
  })
})
