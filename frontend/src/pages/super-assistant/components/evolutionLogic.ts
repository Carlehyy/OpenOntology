// 助手进化（反思审批）的纯展示逻辑。旧记忆分区/过滤/蒸馏辅助已随
// Hindsight 长期记忆上线退役；技能进化审批（ApprovalTab）继续使用本模块。

export interface CandidateAction {
  decision: string
  label: string
  primary?: boolean
}

/** 按候选类型给出审批动作（与后端 decision 枚举一一对应） */
export const candidateActions = (kind: string): CandidateAction[] => {
  if (kind === 'conflict') {
    return [
      { decision: 'new_supersedes', label: '新记忆取代旧的', primary: true },
      { decision: 'keep_old', label: '保留旧的' },
      { decision: 'skip', label: '跳过' },
    ]
  }
  return [
    { decision: 'accept', label: '接受', primary: true },
    { decision: 'reject', label: '拒绝' },
  ]
}
