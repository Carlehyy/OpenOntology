/** 世界模型引擎类型选项与展示名（域内共享：模型页表单与开发页页头标签同源）。 */
import type { EngineType } from '@/api/worldModel'

export const ENGINE_TYPE_OPTIONS: { value: EngineType; label: string; hint: string }[] = [
  { value: 'statistical', label: '统计预测', hint: '基于历史数据的统计/机器学习方法' },
  { value: 'mechanistic', label: '机理仿真', hint: '基于物理定律或业务机理的仿真' },
  { value: 'state_machine', label: '状态机推演', hint: '基于规则与离散状态转移' },
  { value: 'learned', label: '学习型动力学', hint: '从交互数据学习状态转移规律' },
]

export function engineTypeLabel(value: string): string {
  return ENGINE_TYPE_OPTIONS.find(item => item.value === value)?.label ?? value
}
