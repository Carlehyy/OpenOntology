/**
 * YAML 编排页（作者面核心，设计方案 §10-2）：
 * 路由壳 + 复用 TemplateEditorPanel（YAML 加载/校验/保存/版本/预览与
 * 预置只读保护）。新建（/new）与编辑既有模板共用同一面板。
 */
import { useNavigate, useParams } from 'react-router-dom'
import { type TemplateDetail } from '@/api/taskInstances'
import TemplateEditorPanel from '@/components/task-instances/TemplateEditorPanel'
import * as copy from '@/components/task-instances/copy'

export default function TemplateEditorView() {
  const params = useParams<{ id: string }>()
  const navigate = useNavigate()
  const templateId = params.id === 'new' ? undefined : params.id

  // 新建保存成功后进入正式编辑路由（replace 不新增标签）
  const handleCreated = (template: TemplateDetail) => {
    navigate(`/task-instances/templates/${template.id}`, { replace: true })
  }

  return (
    <TemplateEditorPanel
      templateId={templateId}
      exitLabel={copy.RETURN_TO_LIST}
      onExit={() => navigate('/task-instances')}
      onSaved={templateId ? undefined : handleCreated}
    />
  )
}
