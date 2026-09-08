import { expect } from '@playwright/test'

/**
 * 像素尺寸一致性断言（亚像素容差）。
 *
 * dvh/rem 推导的分数像素在两次布局测量间存在浮点舍入差（CI Linux 实测
 * ~2e-5px，先例 6596ffa7 的 590.390625 vs 590.3906478），对 boundingBox
 * 的宽高做 toBe 精确相等断言会误报。尺寸一致性统一走本助手：容差
 * 0.05px 只吸收亚像素噪声，不放宽「尺寸不变」语义。
 */
export function expectSameBoundingBox(
  before: { width: number; height: number } | null | undefined,
  after: { width: number; height: number } | null | undefined,
  label = '元素',
): void {
  expect(after?.height, `${label}高度应保持不变`).toBeCloseTo(before?.height ?? 0, 1)
  expect(after?.width, `${label}宽度应保持不变`).toBeCloseTo(before?.width ?? 0, 1)
}
