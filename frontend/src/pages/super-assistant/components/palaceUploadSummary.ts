/**
 * 多文件上传结果的汇总口径（纯函数，供 MemoryPalaceDialog 与单测共用）：
 * - 全部成功 → success toast；
 * - 部分失败 → error toast，描述里列出失败文件（前 3 个，超出折叠）；
 * - 失败 ≤ PER_FILE_TOAST_LIMIT 时额外逐份弹错误（保留原因可见性），
 *   更多失败时只留汇总，避免 sonner 堆叠下逐份 toast 淹没关键信息；
 * - 刷新文件库失败不影响成败计数，但要在描述里提示手动刷新，
 *   否则「已上传 N 份」与树里看不到新文件会互相矛盾。
 */

/** 失败不超过该数量时逐份弹单条错误 toast */
export const PER_FILE_TOAST_LIMIT = 2

/** 汇总描述里点名列失败文件的上限，超出折叠为「等 N 份」 */
const NAMED_LIMIT = 3

export interface PalaceUploadOutcome {
  /** 本次尝试上传的总份数 */
  total: number
  /** 上传失败的份数 */
  failed: number
  /** 失败文件的文件名（按上传顺序） */
  failedNames: string[]
  /** 上传后刷新文件库是否失败 */
  refreshFailed: boolean
}

export interface PalaceUploadToast {
  variant: 'success' | 'error'
  title: string
  description?: string
  /** 是否还应对单份失败弹独立 toast */
  perFileToasts: boolean
}

export function summarizePalaceUpload(outcome: PalaceUploadOutcome): PalaceUploadToast | null {
  const { total, failed, failedNames, refreshFailed } = outcome
  if (total <= 0) {
    return null
  }
  const refreshNote = refreshFailed ? '文件库刷新失败，请手动刷新查看。' : ''
  if (failed <= 0) {
    return {
      variant: 'success',
      title: `已上传 ${total} 份文件`,
      description: ['文档解析完成后自动抽取实体与关系。', refreshNote]
        .filter(Boolean)
        .join(''),
      perFileToasts: false,
    }
  }
  const named = failedNames.slice(0, NAMED_LIMIT)
  const overflow = failedNames.length - named.length
  const failedList = overflow > 0 ? `${named.join('、')} 等 ${failedNames.length} 份` : named.join('、')
  return {
    variant: 'error',
    title: `上传完成：成功 ${total - failed} 份，失败 ${failed} 份`,
    description: [
      failedList ? `失败：${failedList}。` : '',
      '失败项可重试；已上传的文件正在排队抽取。',
      refreshNote,
    ].filter(Boolean).join(''),
    perFileToasts: failed <= PER_FILE_TOAST_LIMIT,
  }
}
