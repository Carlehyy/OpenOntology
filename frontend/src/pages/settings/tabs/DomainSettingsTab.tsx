import { formatDateTime } from '@/utils/datetime'
import { useEffect, useId, useRef } from 'react'
import { Pencil, Plus, Search, Trash2, X } from 'lucide-react'

import { Button } from '@/components/ui/Button'
import { Card } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { LoadingState, EmptyState } from '@/components/ui/LoadingState'
import { Modal, ConfirmModal } from '@/components/ui/Modal'
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
    domainSearchInput,
    setDomainSearchInput,
    showDomainModal,
    setShowDomainModal,
    editingDomain,
    setEditingDomain,
    domainName,
    setDomainName,
    domainDescription,
    setDomainDescription,
    nameError,
    setNameError,
    nameErrorNonce,
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

  const nameInputRef = useRef<HTMLInputElement>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const descriptionId = useId()
  // 名称框组词态自管：Safari/WebKit 的 compositionend 先于同一次上屏 Enter 的
  // keydown 派发，nativeEvent.isComposing 届时已是 false，仅靠它会让选词回车
  // 误触发保存；ref 延迟一拍复位兜住该顺序（Chromium/Firefox 由 isComposing 覆盖）
  const nameComposingRef = useRef(false)

  // 行内错误出现时（空名称 / 重名）把焦点拉回名称框，让反馈落在视线所在处；
  // nonce 兜住「连续两次相同错误」时 state 同值跳过 effect 的场景
  useEffect(() => {
    if (nameError) nameInputRef.current?.focus()
  }, [nameError, nameErrorNonce])

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

        {/* 工具条：搜索。输入框绑即时值，列表查询走 hook 内的 300ms 防抖词 */}
        <div className="flex items-center gap-3 px-5 pt-4">
          <div className="relative w-56 max-w-full">
            <Search
              size={14}
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-[var(--color-text-tertiary)]"
            />
            <Input
              ref={searchInputRef}
              value={domainSearchInput}
              onChange={e => setDomainSearchInput(e.target.value)}
              placeholder="按名称搜索"
              className="h-8 pl-8 pr-7 text-xs"
              aria-label="按名称搜索领域"
            />
            {domainSearchInput && (
              <button
                type="button"
                onClick={() => {
                  // 清空后按钮随即卸载，把焦点还给输入框，避免键盘用户焦点落回 body
                  setDomainSearchInput('')
                  searchInputRef.current?.focus()
                }}
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
            <div className="overflow-x-auto rounded-lg border border-[var(--color-border)]">
              {/* overflow-x-auto：窄视口下固定列宽超出容器时在容器内横滑，操作列
                  始终可以滑到，页面本身不出现横向滚动条；table-fixed + colgroup 固定
                  列宽配额，任何长度的名称/描述都只能在本列内截断，操作列不会被内容
                  挤出容器；min-w 兜底极窄容器下时间/操作列不被按比例压扁 */}
              <table className="w-full min-w-[600px] table-fixed text-sm">
                <colgroup>
                  <col className="w-[30%]" />
                  <col />
                  <col className="w-[160px]" />
                  <col className="w-24" />
                </colgroup>
                <thead className="border-b border-[var(--color-border)] bg-[var(--color-muted)]">
                  <tr>
                    <th className="px-4 py-2.5 text-left text-xs font-medium text-[var(--color-text-secondary)]">名称</th>
                    <th className="px-4 py-2.5 text-left text-xs font-medium text-[var(--color-text-secondary)]">描述</th>
                    <th className="px-4 py-2.5 text-left text-xs font-medium text-[var(--color-text-secondary)]">更新时间</th>
                    <th className="px-4 py-2.5 text-right text-xs font-medium text-[var(--color-text-secondary)]">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {list.map((d: any) => (
                    <tr
                      key={d.id}
                      className="border-b border-[var(--color-border)] transition-colors last:border-0 hover:bg-[var(--color-bg-hover)]"
                    >
                      <td className="px-4 py-3 font-medium text-[var(--color-text-primary)]">
                        <span className="block truncate" title={d.name}>{d.name}</span>
                      </td>
                      <td className="px-4 py-3 text-[var(--color-text-secondary)]">
                        {d.description ? (
                          <span className="block truncate" title={d.description}>{d.description}</span>
                        ) : (
                          <span className="text-[var(--color-text-tertiary)]">—</span>
                        )}
                      </td>
                      <td className="whitespace-nowrap px-4 py-3 text-[var(--color-text-tertiary)]">
                        {d.updated_at ? formatDateTime(d.updated_at) : '—'}
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
        disableClose={saving}
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
            ref={nameInputRef}
            label="名称"
            value={domainName}
            onChange={e => {
              setDomainName(e.target.value)
              if (nameError) setNameError('')
            }}
            onKeyDown={e => {
              // Enter 直接保存；双保险排除输入法组词回车：isComposing 覆盖
              // Chromium/Firefox，nameComposingRef 兜 WebKit 的上屏回车顺序
              if (e.key === 'Enter' && !nameComposingRef.current && !e.nativeEvent.isComposing && !saving) {
                handleSaveDomain()
              }
            }}
            onCompositionStart={() => { nameComposingRef.current = true }}
            onCompositionEnd={() => {
              // 延迟一拍复位：Safari 上屏 Enter 的 keydown 紧跟 compositionend 派发
              setTimeout(() => { nameComposingRef.current = false }, 0)
            }}
            maxLength={100}
            placeholder="输入领域名称"
            autoFocus
            required
            error={nameError}
            aria-invalid={nameError ? true : undefined}
          />
          <div>
            <label htmlFor={descriptionId} className="mb-1.5 block text-sm font-medium text-[var(--color-text-primary)]">描述</label>
            <textarea
              id={descriptionId}
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
