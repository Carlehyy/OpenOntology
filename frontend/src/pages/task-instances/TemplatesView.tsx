/**
 * 模板列表视图：卡片网格 + 关键字筛选 + 新建/删除 + 进入编排页。
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FileCode2, Plus, Search, Trash2 } from 'lucide-react'
import { toast } from 'sonner'
import { apiError, taskInstancesApi } from '@/api/taskInstances'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { formatDateTime } from '@/utils/datetime'

export default function TemplatesView({ onNew }: { onNew: () => void }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [keyword, setKeyword] = useState('')
  const [pendingDelete, setPendingDelete] = useState<string | null>(null)

  const templates = useQuery({
    queryKey: ['task-instances', 'templates', keyword],
    queryFn: () => taskInstancesApi.listTemplates(keyword),
    placeholderData: previous => previous,
  })

  const remove = useMutation({
    mutationFn: (id: string) => taskInstancesApi.deleteTemplate(id),
    onSuccess: () => {
      toast.success('模板已删除')
      void queryClient.invalidateQueries({ queryKey: ['task-instances', 'templates'] })
    },
    onError: error => toast.error(apiError(error)),
  })

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative w-72">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-[var(--color-text-tertiary)]" />
          <Input
            aria-label="搜索模板"
            placeholder="搜索模板名称或描述"
            className="pl-9"
            value={keyword}
            onChange={event => setKeyword(event.target.value)}
          />
        </div>
        <Button onClick={onNew} className="ml-auto gap-1.5">
          <Plus className="h-4 w-4" />新建模板
        </Button>
      </div>

      {templates.isLoading ? (
        <div className="py-16 text-center text-sm text-[var(--color-text-tertiary)]">加载中…</div>
      ) : !templates.data?.length ? (
        <div className="rounded-lg border border-dashed border-border py-16 text-center">
          <p className="text-sm text-[var(--color-text-tertiary)]">此处尚待落墨</p>
          <p className="mt-1 text-xs text-[var(--color-text-tertiary)]">创建第一个流程模板，开启任务轨道</p>
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {templates.data.map(template => (
            <Card
              key={template.id}
              className="cursor-pointer transition-shadow hover:shadow-md"
              onClick={() => navigate(`/task-instances/templates/${template.id}`)}
            >
              <CardContent className="space-y-2">
                <div className="flex items-start gap-2">
                  <FileCode2 className="mt-0.5 h-4 w-4 shrink-0 text-[var(--color-text-tertiary)]" />
                  <div className="min-w-0 flex-1">
                    <div className="truncate font-medium text-[var(--color-text-primary)]">
                      {template.name}
                    </div>
                    <div className="mt-0.5 line-clamp-2 text-xs text-[var(--color-text-tertiary)]">
                      {template.description || '（无描述）'}
                    </div>
                  </div>
                  <Button
                    variant="ghost" size="sm" aria-label={`删除模板 ${template.name}`}
                    className="h-7 w-7 p-0 text-[var(--color-danger)]"
                    onClick={event => {
                      event.stopPropagation()
                      setPendingDelete(template.id)
                    }}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
                <div className="flex items-center justify-between text-xs text-[var(--color-text-tertiary)]">
                  <span>v{template.latest_revision_no ?? '-'} · {template.instance_count} 次实例</span>
                  <span>{formatDateTime(template.updated_at)}</span>
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <ConfirmDialog
        open={pendingDelete !== null}
        title="删除模板"
        description="删除后不可恢复；已创建的实例及其事件历史不受影响。"
        confirmText="删除"
        variant="danger"
        onConfirm={() => {
          if (pendingDelete) remove.mutate(pendingDelete)
          setPendingDelete(null)
        }}
        onClose={() => setPendingDelete(null)}
      />
    </div>
  )
}
