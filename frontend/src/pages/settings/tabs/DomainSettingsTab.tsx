import { formatDate, formatDateTime } from '@/utils/datetime'
import { useEffect } from 'react'
import { Pencil, Plus, Search, Trash2, X } from 'lucide-react'

import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { LoadingState, EmptyState } from '@/components/ui/LoadingState'
import { Modal, ConfirmModal } from '@/components/ui/Modal'
import { toast } from 'sonner'
import { cn } from '@/lib/utils'
import type { DomainSettingsViewModel } from '../hooks/useDomainSettings'

type DomainSettingsTabProps = {
  settings: DomainSettingsViewModel
}

export default function DomainSettingsTab({ settings }: DomainSettingsTabProps) {
  const {
    domainList,
    domainsLoading,
    domainSearch,
    setDomainSearch,
    showDomainModal,
    setShowDomainModal,
    editingDomain,
    setEditingDomain,
    domainName,
    setDomainName,
    domainDescription,
    setDomainDescription,
    domainMsg,
    setDomainMsg,
    deleteDomainTarget,
    setDeleteDomainTarget,
    createDomainMut,
    updateDomainMut,
    deleteDomainMut,
    openCreateDomain,
    openEditDomain,
    handleSaveDomain,
    handleDeleteDomain,
  } = settings


  // 把 hook 内的 domainMsg（成功/失败文案）转成 toast，并清空原值避免残留。
  useEffect(() => {
    if (!domainMsg) return
    const ok = domainMsg.includes('成功')
    const notifyDomain = ok ? toast.success : toast.error
    notifyDomain(ok ? '领域设置' : '操作失败', { description: domainMsg })
    setDomainMsg('')
  }, [domainMsg, setDomainMsg])

  const saving = createDomainMut.isPending || updateDomainMut.isPending
  const list = (domainList as any[])

  return (
    <div className="min-h-full">
      <Card className="overflow-hidden">
        {/* 页头：标题 + 一句描述 + 主操作右置 */}
        <header className="flex flex-wrap items-start justify-between gap-3 border-b border-[var(--color-border)] px-5 py-4">
          <div className="min-w-0">
            <h2 className="text-sm font-semibold text-[var(--color-text-primary)]">领域设置</h2>
            <p className="mt-1 text-xs leading-5 text-[var(--color-text-secondary)]">
              管理本体的业务领域分类。新建本体时可选择所属领域；删除前需确保无本体引用该领域。
            </p>
          </div>
          <Button size="sm" onClick={openCreateDomain} className="shrink-0">
            <Plus size={14} /> 新增领域
          </Button>
        </header>

        {/* 工具条：搜索 */}
        <div className="flex items-center gap-3 px-5 pt-4">
          <div className="relative w-56 max-w-full">
            <Search
              size={14}
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]"
            />
            <Input
              value={domainSearch}
              onChange={e => setDomainSearch(e.target.value)}
              placeholder="按名称搜索"
              className="h-8 pl-8 pr-7 text-xs"
              aria-label="按名称搜索领域"
            />
            {domainSearch && (
              <button
                type="button"
                onClick={() => setDomainSearch('')}
                aria-label="清除搜索"
                className="absolute right-2 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)] transition-colors hover:text-[var(--color-text-primary)]"
              >
                <X size={12} />
              </button>
            )}
          </div>
        </div>

        {/* 列表 */}
        <div className="p-5 pt-4">
          {domainsLoading ? (
            <LoadingState message="正在加载领域..." />
          ) : list.length === 0 ? (
            <EmptyState
              title={domainSearch ? '未找到匹配的领域' : '暂无领域'}
              description={domainSearch ? '尝试更换关键词或清除搜索条件。' : '点击“新增领域”创建第一个业务领域分类。'}
              action={
                !domainSearch ? (
                  <Button size="sm" onClick={openCreateDomain}>
                    <Plus size={14} /> 新增领域
                  </Button>
                ) : undefined
              }
            />
          ) : (
            <div className="overflow-hidden rounded-lg border border-[var(--color-border)]">
              <table className="w-full text-sm">
                <thead className="border-b border-[var(--color-border)] bg-[var(--color-muted)]">
                  <tr>
                    <th className="px-4 py-2.5 text-left text-xs font-medium text-[var(--color-text-secondary)]">名称</th>
                    <th className="px-4 py-2.5 text-left text-xs font-medium text-[var(--color-text-secondary)]">描述</th>
                    <th className="px-4 py-2.5 text-left text-xs font-medium text-[var(--color-text-secondary)]">更新时间</th>
                    <th className="w-20 px-4 py-2.5 text-right text-xs font-medium text-[var(--color-text-secondary)]">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((d: any) => (
                    <tr
                      key={d.id}
                      className="border-b border-[var(--color-border)] transition-colors last:border-0 hover:bg-[var(--color-bg-hover)]"
                    >
                      <td className="px-4 py-3 font-medium text-[var(--color-text-primary)]">
                        <span className="truncate">{d.name}</span>
                      </td>
                      <td className="max-w-xs px-4 py-3 text-[var(--color-text-secondary)]">
                        {d.description ? (
                          <span className="block truncate" title={d.description}>{d.description}</span>
                        ) : (
                          <Badge variant="outline">无描述</Badge>
                        )}
                      </td>
                      <td className="px-4 py-3 text-[var(--color-text-tertiary)]" title={d.updated_at ? formatDateTime(d.updated_at) : ''}>
                        {d.updated_at ? formatDate(new Date(d.updated_at)) : '—'}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex items-center justify-end gap-1">
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            onClick={() => openEditDomain(d)}
                            aria-label={'编辑领域 ' + d.name}
                          >
                            <Pencil size={14} />
                          </Button>
                          <Button
                            variant="ghost"
                            size="icon-sm"
                            onClick={() => setDeleteDomainTarget(d)}
                            aria-label={'删除领域 ' + d.name}
                            className="text-[var(--color-text-tertiary)] hover:text-[var(--color-danger)] hover:bg-[var(--color-danger-bg)]"
                          >
                            <Trash2 size={14} />
                          </Button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </Card>

      {/* 新增 / 编辑 弹层 */}
      <Modal
        open={showDomainModal}
        onClose={() => { setShowDomainModal(false); setEditingDomain(null) }}
        title={editingDomain ? '编辑领域' : '新增领域'}
        description={editingDomain ? '修改领域名称或描述。' : '创建一个新的业务领域分类。'}
        size="md"
        footer={
          <>
            <Button
              variant="outline"
              onClick={() => { setShowDomainModal(false); setEditingDomain(null) }}
              disabled={saving}
            >
              取消
            </Button>
            <Button onClick={handleSaveDomain} loading={saving}>
              保存
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          <Input
            label="名称"
            value={domainName}
            onChange={e => setDomainName(e.target.value)}
            maxLength={100}
            placeholder="输入领域名称"
            autoFocus
            required
          />
          <div>
            <div className="mb-1.5 text-sm font-medium text-[var(--color-text-primary)]">描述</div>
            <textarea
              value={domainDescription}
              onChange={e => setDomainDescription(e.target.value)}
              placeholder="输入领域描述（可选）"
              rows={3}
              className={cn(
                'w-full resize-none rounded-md border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-3 py-2 text-sm shadow-sm transition-colors',
                'placeholder:text-[var(--color-text-tertiary)]',
                'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus:border-[var(--color-primary)]',
              )}
            />
          </div>
        </div>
      </Modal>

      {/* 删除确认 */}
      <ConfirmModal
        open={!!deleteDomainTarget}
        onClose={() => setDeleteDomainTarget(null)}
        onConfirm={handleDeleteDomain}
        title="确认删除"
        description={deleteDomainTarget
          ? '确定要删除领域「' + deleteDomainTarget.name + '」吗？若仍有本体使用该领域，系统会阻止删除；删除后不可撤销。'
          : ''}
        confirmText="确认删除"
        cancelText="取消"
        variant="danger"
        loading={deleteDomainMut.isPending}
      />
    </div>
  )
}
