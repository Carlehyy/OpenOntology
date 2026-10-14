/**
 * 语音听写纯 DSP 逻辑：声道折叠、任意采样率线性重采样、电平计算。
 * 不依赖浏览器 API（node:test 可直接单测），recorder.ts 负责采集后调用。
 */

/** 交错多声道样本折叠为单声道（等权平均）。 */
export function downmixToMono(samples: Float32Array, channels: number): Float32Array {
  if (channels <= 1) return samples
  const frames = Math.floor(samples.length / channels)
  const mono = new Float32Array(frames)
  for (let frame = 0; frame < frames; frame += 1) {
    let sum = 0
    for (let channel = 0; channel < channels; channel += 1) {
      sum += samples[frame * channels + channel]
    }
    mono[frame] = sum / channels
  }
  return mono
}

/** 线性插值重采样（与后端 voice_service.decode_wav 同一算法）。 */
export function resampleLinear(samples: Float32Array, fromRate: number, toRate: number): Float32Array {
  if (fromRate === toRate || samples.length === 0) return samples
  const count = Math.max(Math.round((samples.length * toRate) / fromRate), 1)
  const output = new Float32Array(count)
  const step = fromRate / toRate
  for (let index = 0; index < count; index += 1) {
    const position = index * step
    const left = Math.floor(position)
    const right = Math.min(left + 1, samples.length - 1)
    const weight = position - left
    output[index] = samples[left] * (1 - weight) + samples[right] * weight
  }
  return output
}

/** 均方根电平（0~1），录音条音量轨迹用。 */
export function rmsLevel(samples: Float32Array): number {
  if (samples.length === 0) return 0
  let sum = 0
  for (let index = 0; index < samples.length; index += 1) {
    sum += samples[index] * samples[index]
  }
  return Math.sqrt(sum / samples.length)
}
