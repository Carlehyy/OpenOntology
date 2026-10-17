/**
 * 任务实例域共享文案（弹窗与独立页唯一来源）。
 * 背景：同一功能域曾有弹窗/独立页两套平行文案，措辞各自漂移
 * （空态两版、占位符两版、校验错误重复定义）；此处收敛为常量，
 * 改动只落一处。组件级一句话文案仍可内联，跨视图复用的才进这里。
 */

// —— 看板 ——
export const BOARD_GROUP_EMPTY = '暂无'
export const BOARD_COUNT_LABEL = (total: number) => `共 ${total} 个实例`
export const BOARD_EMPTY_TITLE = '还没有任务实例'
export const BOARD_EMPTY_HINT = '从流程模板激活第一个任务，任务会按流程自动流转'
export const BOARD_EMPTY_ACTION = '去流程模板'

// —— 模板 ——
export const TEMPLATE_EMPTY_TITLE = '还没有流程模板'
export const TEMPLATE_EMPTY_HINT = '点击右上角「新建流程」创建第一个自动化流程'
export const TEMPLATE_NEW_BUTTON = '新建流程'
export const TEMPLATE_GOAL_PLACEHOLDER = '这次任务要完成什么？Agent 节点的执行以此为骨干……'
export const TEMPLATE_NAME_PLACEHOLDER = '例如：登录页改版评审-1023'
export const TEMPLATE_UNTITLED_GOAL = '未填写目标'
export const TEMPLATE_UNTITLED_DESC = '未填写描述'

// —— 模板搜索 / 来源单选（弹窗模板列表，YIYA-44/45）——
export const TEMPLATE_SEARCH_LABEL = '搜索模板名称'
export const TEMPLATE_SOURCE_LABEL = '模板来源'
export const TEMPLATE_SOURCE_PRESET = '预置'
export const TEMPLATE_SOURCE_PERSONAL = '个人'
export const TEMPLATE_PERSONAL_EMPTY_TITLE = '还没有个人模板'
export const TEMPLATE_PERSONAL_EMPTY_HINT = '点击右上角「新建流程」创建属于你的流程'
export const TEMPLATE_PRESET_EMPTY_TITLE = '暂无预置模板'
export const TEMPLATE_PRESET_EMPTY_HINT = '预置模板由平台统一提供'
export const TEMPLATE_NOMATCH_TITLE = '没有匹配的模板'
export const TEMPLATE_NOMATCH_HINT = (keyword: string, sourceLabel: string) =>
  `未找到名称包含「${keyword.trim()}」的${sourceLabel}模板`

// —— 模板卡四操作（可访问名称/禁用原因）——
export const TEMPLATE_VIEW_BUTTON = '查看'
export const TEMPLATE_EDIT_BUTTON = '编辑'
export const TEMPLATE_ACTIVATE_BUTTON = '激活'
export const TEMPLATE_DELETE_NAME = (name: string) => `删除模板 ${name}`
export const TEMPLATE_PRESET_EDIT_DISABLED = '预置模板不允许编辑'
export const TEMPLATE_PRESET_DELETE_DISABLED = '预置模板不允许删除'
export const TEMPLATE_DELETE_TITLE = '删除模板'
export const TEMPLATE_DELETE_DESC = '删除后不可恢复；已创建的实例及其事件历史不受影响。'
export const TEMPLATE_DELETED_TOAST = '模板已删除'
export const TEMPLATE_VIEW_TITLE = '模板详情'
export const TEMPLATE_EDIT_TITLE = '编辑模板'
export const TEMPLATE_YAML_SOURCE = 'YAML 原文'

// —— 激活 ——
export const ACTIVATE_MODAL_TITLE = '激活实例'
export const ACTIVATE_BUTTON = '激活实例'
export const ACTIVATED_TOAST = '实例已激活'
export const ACTIVATE_RUNNING = '激活中…'

// —— 交互（审批 / 人工）——
export const APPROVAL_SECTION_TITLE = '审批关口'
export const HUMAN_SECTION_TITLE = '人工待办'
export const REJECT_INPUT_ARIA = '退回理由（退回时必填，将随退回送达上游重做）'
export const REJECT_INPUT_PLACEHOLDER = '退回理由（退回时必填，将随退回送达上游重做）'
export const HUMAN_REJECT_INPUT_ARIA = '退回重做理由'
export const HUMAN_REJECT_INPUT_PLACEHOLDER = '退回直接上游重做的理由（可选）'
export const APPROVE_BUTTON = '批准'
export const REJECT_BUTTON = '退回重做'
export const DECIDED_TOAST = '审批决定已提交'
export const HUMAN_OUTPUT_ARIA = '成果 JSON'
export const HUMAN_OUTPUT_PLACEHOLDER = '成果产出（JSON 对象，须满足端口契约）'
export const HUMAN_SUBMIT_BUTTON = '提交成果'
export const HUMAN_SUBMITTED_TOAST = '成果已提交'
export const HUMAN_REJECTED_TOAST = '已退回重做，等待上游节点重新执行'
export const HUMAN_OUTPUT_INVALID = '成果产出必须是合法 JSON 对象'

// —— 导航 / 出口 ——
export const RETURN_TO_BOARD = '返回看板'
export const RETURN_TO_TEMPLATES = '返回模板'
/** 独立页（详情/编排）回任务实例列表页 */
export const RETURN_TO_LIST = '返回列表'
export const OPEN_FULL_DETAIL = '在完整页面打开'
export const TERMINAL_NO_ACTION = '实例已结束，无待处理事项'
export const FULL_PAGE_HINT = '插话干预、事件时间线等更多操作，请前往完整详情页'

// —— 画布编辑器 ——
export const TEMPLATE_SAVE_BUTTON = '保存模板'
export const CANVAS_DISCARD_YAML_TITLE = '切回画布模式'
export const CANVAS_DISCARD_YAML_DESC = 'YAML 模式中有未应用的修改；切回画布后将按画布当前内容重新生成 YAML，这些修改会被覆盖。'

// —— 页头 / 弹窗副标题（弹窗与独立页共用一版）——
export const DOMAIN_SUBTITLE = '用流程模板编排 AI 与人工协作，任务自动流转、关键节点人工把关'

// —— 失败 / 重试 ——
export const REACTIVATE_BUTTON = '用相同目标重新激活'
