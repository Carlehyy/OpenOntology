import { Database } from 'lucide-react'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import ConnectionManagementPanel from './ConnectionManagementPanel'

export default function ConnectionManagementDialog({
  open,
  onClose,
  onChanged,
}: {
  open: boolean
  onClose: () => void
  onChanged?: () => void
}) {
  return (
    <Dialog open={open} onOpenChange={next => { if (!next) onClose() }}>
      <DialogContent
        layer="nested"
        data-testid="connection-management-dialog"
        className="flex max-h-[84vh] w-[min(96vw,900px)] flex-col p-0"
      >
        <DialogHeader className="mb-0 shrink-0 border-b border-border px-5 pb-3 pt-4" icon={<Database size={18} />}>
          <DialogTitle>管理数据连接</DialogTitle>
          <DialogDescription>
            在当前登记流程中维护外部数据库和 API 连接。凭据只提交到服务端，不会回显到表单。
          </DialogDescription>
        </DialogHeader>
        <main className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          <ConnectionManagementPanel embedded dialogLayer="top" onChanged={onChanged} />
        </main>
      </DialogContent>
    </Dialog>
  )
}
