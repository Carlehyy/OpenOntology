/**
 * 会话审计导出文件名：标题清洗 + 会话 id 前 8 位后缀。
 * 纯函数无副作用，供 node:test 直接单测（src/test/unit/superAssistant）。
 */
export function buildConversationExportFilename(
  title: string,
  conversationId: string,
): string {
  // 清洗范围：Windows 非法字符、C0 控制字符、换行、Unicode 双向格式符
  // （U+202E 等 RTL override 可在文件管理器里视觉伪装 .json 扩展名）
  const safeTitle = (title.trim() || '未命名会话')
    .replace(/[\\/:*?"<>|\r\n\u0000-\u001f\u200e\u200f\u202a-\u202e\u2066-\u2069]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 80)
    // 全非法字符标题（如 "???"）清洗后为空：回退默认名，避免产出前导连字符文件名
    || '未命名会话'
  return `${safeTitle}-${conversationId.slice(0, 8)}.json`
}
