import assert from 'node:assert/strict'
import { describe, it } from 'node:test'

import { downmixToMono, resampleLinear, rmsLevel } from '../../../lib/voice/dsp.ts'
import { encodeWav } from '../../../lib/voice/wav.ts'
import {
  insertAtCursor,
  needsSpacingBoundary,
  shouldHoldInsertion,
  withDictationSpacing,
} from '../../../lib/voice/insertion.ts'

describe('voice dsp', () => {
  it('downmixToMono 折叠立体声为平均单声道', () => {
    const stereo = new Float32Array([1, -1, 0.5, -0.5, 0.25, 0.75])
    const mono = downmixToMono(stereo, 2)
    assert.deepEqual(Array.from(mono), [0, 0, 0.5])
  })

  it('downmixToMono 单声道原样返回', () => {
    const mono = new Float32Array([1, 2, 3])
    assert.equal(downmixToMono(mono, 1), mono)
  })

  it('resampleLinear 48k→16k 长度缩减为三分之一', () => {
    const samples = new Float32Array(4800)
    for (let i = 0; i < samples.length; i += 1) samples[i] = Math.sin((i / 4800) * Math.PI * 2)
    const resampled = resampleLinear(samples, 48000, 16000)
    assert.equal(resampled.length, 1600)
    // 起点对齐（线性插值在 0 处取原值）
    assert.ok(Math.abs(resampled[0] - samples[0]) < 1e-6)
  })

  it('resampleLinear 同采样率原样返回', () => {
    const samples = new Float32Array([0.1, 0.2])
    assert.equal(resampleLinear(samples, 16000, 16000), samples)
  })

  it('rmsLevel 空样本为 0，正弦幅值接近 0.707', () => {
    assert.equal(rmsLevel(new Float32Array(0)), 0)
    const sine = new Float32Array(1600)
    for (let i = 0; i < sine.length; i += 1) sine[i] = Math.sin((i / sine.length) * Math.PI * 40)
    const level = rmsLevel(sine)
    assert.ok(Math.abs(level - Math.SQRT1_2) < 0.01, `rms=${level}`)
  })
})

describe('voice wav 编码', () => {
  const ascii = (bytes: Uint8Array, offset: number, length: number) =>
    String.fromCharCode(...bytes.subarray(offset, offset + length))

  it('产出合法 16-bit PCM 单声道 WAV 头', () => {
    const wav = encodeWav(new Float32Array(160), 16000)
    assert.equal(ascii(wav, 0, 4), 'RIFF')
    assert.equal(ascii(wav, 8, 4), 'WAVE')
    assert.equal(ascii(wav, 12, 4), 'fmt ')
    assert.equal(ascii(wav, 36, 4), 'data')
    const view = new DataView(wav.buffer, wav.byteOffset, wav.byteLength)
    assert.equal(view.getUint16(20, true), 1) // PCM
    assert.equal(view.getUint16(22, true), 1) // mono
    assert.equal(view.getUint32(24, true), 16000)
    assert.equal(view.getUint16(34, true), 16)
    assert.equal(view.getUint32(40, true), 320)
    assert.equal(wav.length, 44 + 320)
  })

  it('样本往返：编码后按 PCM16 读回应还原（±1 量化误差内）', () => {
    const samples = new Float32Array([0, 0.5, -0.5, 0.999, -0.999])
    const wav = encodeWav(samples, 16000)
    const view = new DataView(wav.buffer, wav.byteOffset, wav.byteLength)
    for (let i = 0; i < samples.length; i += 1) {
      const raw = view.getInt16(44 + i * 2, true)
      const restored = raw < 0 ? raw / 0x8000 : raw / 0x7fff
      assert.ok(Math.abs(restored - samples[i]) < 0.001, `i=${i}`)
    }
  })

  it('超界样本被钳制到 [-1, 1]', () => {
    const wav = encodeWav(new Float32Array([2, -2]), 16000)
    const view = new DataView(wav.buffer, wav.byteOffset, wav.byteLength)
    assert.equal(view.getInt16(44, true), 0x7fff)
    assert.equal(view.getInt16(46, true), -0x8000)
  })
})

describe('voice 插入策略', () => {
  it('中英边界：仅两侧均为拉丁字母/数字时补空格', () => {
    assert.equal(withDictationSpacing('hello', 'world'), 'hello world')
    assert.equal(withDictationSpacing('你好', 'world'), '你好world')
    assert.equal(withDictationSpacing('hello', '世界'), 'hello世界')
    assert.equal(withDictationSpacing('你好', '世界'), '你好世界')
    assert.equal(withDictationSpacing('第2', '章'), '第2章')
    assert.equal(withDictationSpacing('共100', '页'), '共100页')
    assert.equal(withDictationSpacing('值是42', '项'), '值是42项')
  })

  it('needsSpacingBoundary 边界判定', () => {
    assert.equal(needsSpacingBoundary('abc', 'def'), true)
    assert.equal(needsSpacingBoundary('abc.', 'def'), false)
    assert.equal(needsSpacingBoundary('中文', 'abc'), false)
    assert.equal(needsSpacingBoundary('abc', '中文'), false)
  })

  it('insertAtCursor 在光标处插入并停在插入文本末尾', () => {
    const plan = insertAtCursor('你好世界', 2, '语音')
    assert.deepEqual(plan, { text: '你好语音世界', nextCursor: 4 })
  })

  it('insertAtCursor 光标越界时钳制', () => {
    const plan = insertAtCursor('abc', 99, 'XY')
    assert.deepEqual(plan, { text: 'abc XY', nextCursor: 6 })
  })

  it('insertAtCursor 前后边界同时补空格', () => {
    const plan = insertAtCursor('say it', 3, 'hello')
    assert.equal(plan.text, 'say hello it')
    assert.equal(plan.nextCursor, 9)
  })

  it('shouldHoldInsertion：草稿被改/组词中才扣留', () => {
    assert.equal(shouldHoldInsertion(3, 3, false), false)
    assert.equal(shouldHoldInsertion(3, 4, false), true)
    assert.equal(shouldHoldInsertion(3, 3, true), true)
  })
})
