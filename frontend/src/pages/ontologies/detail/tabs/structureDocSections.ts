/**
 * 业务文档弹窗的文档目录模型：把需求文档 Markdown 按标题切分为可跳转的小节。
 *
 * 切分是纯函数且感知代码围栏（fence-aware）：```/~~~ 内部的 `#` 注释行不会
 * 被误认成标题。每节携带自己的 Markdown 原文（含标题行），渲染时整节交给
 * Markdown 组件，节外壳的 DOM id 即目录跳转锚点；startLine 记录节首行号
 * （0 基），编辑态目录点击时用于把编辑器滚动到对应标题行。
 */

export interface StructureDocSection {
  /** 弹窗内唯一的 DOM 锚点 id。 */
  id: string
  /** 标题级别 1-6；首标题之前的导语小节为 0。 */
  level: number
  /** 标题文本；导语小节为空字符串。 */
  title: string
  /** 本节 Markdown 原文（含标题行）。 */
  markdown: string
  /** 节首行在全文中的 0 基行号（编辑器定位用）。 */
  startLine: number
}

// CommonMark fence：最多 3 个前导空格 + 至少 3 个同种字符（` 或 ~），
// 之后允许 info 字符串（开栏用；闭栏行其后只允许空白）。
// 闭合规则是长度感知的：同种字符且长度 >= 开栏长度才闭合——否则 4 反引号
// 外栏会被内层 3 反引号提前关掉，栏内后续 `#` 行就会污染目录。
const FENCE_LINE = /^ {0,3}(`{3,}|~{3,})(.*)$/
const HEADING_LINE = /^(#{1,6})\s+(.+?)\s*#*\s*$/

interface FenceState {
  char: string
  length: number
}

export function splitMarkdownSections(text: string): StructureDocSection[] {
  const lines = String(text || '').split(/\r?\n/)
  const sections: StructureDocSection[] = []
  let current: { level: number; title: string; lines: string[]; startLine: number } = {
    level: 0, title: '', lines: [], startLine: 0,
  }
  let fence: FenceState | null = null

  const flush = () => {
    if (current.lines.some(line => line.trim())) {
      sections.push({
        id: `structure-doc-section-${sections.length}`,
        level: current.level,
        title: current.title,
        markdown: current.lines.join('\n').trim(),
        startLine: current.startLine,
      })
    }
  }

  lines.forEach((line, index) => {
    const fenceMatch = line.match(FENCE_LINE)
    if (fenceMatch) {
      const marker = fenceMatch[1]
      const isCloserCandidate = fenceMatch[2].trim() === ''
      // 同种围栏字符成对开合，且闭合长度不小于开栏长度；开栏期间的
      // 任何行（含 `#` 注释行）都不视为标题。闭栏行不允许 info 字符串。
      if (!fence) fence = { char: marker[0], length: marker.length }
      else if (isCloserCandidate && fence.char === marker[0]
        && marker.length >= fence.length) fence = null
      current.lines.push(line)
      return
    }
    if (!fence) {
      const heading = line.match(HEADING_LINE)
      if (heading) {
        flush()
        current = {
          level: heading[1].length,
          title: heading[2].trim(),
          lines: [line],
          startLine: index,
        }
        return
      }
    }
    current.lines.push(line)
  })
  flush()

  return sections
}

/** 目录只列真实标题小节，导语（level 0）不进目录。 */
export function tocSections(sections: StructureDocSection[]): StructureDocSection[] {
  return sections.filter(section => section.level >= 1)
}
