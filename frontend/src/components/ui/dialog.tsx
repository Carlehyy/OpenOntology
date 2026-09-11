/* shadcn 风格的 Dialog 封装(Radix 原语),作为全站弹窗的标准件
   (component-catalog.ts「弹窗 / 对话框」条目)。视觉全部走 tokens.css
   语义令牌,明暗两态自适应;入场动效用 animate-dialog-in (opacity +
   独立 scale 属性):slide-up 的 transform 关键帧会覆盖居中定位的
   translate,导致弹窗从错位位置滑入(验收反馈)。
   关闭按钮渲染在内容之后,保证正文首个可聚焦元素
   (如文本域)优先获得焦点。 */
import * as React from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { X } from 'lucide-react'
import { cn } from '@/lib/utils'

const Dialog = DialogPrimitive.Root
const DialogTrigger = DialogPrimitive.Trigger
const DialogClose = DialogPrimitive.Close

function DialogOverlay({
  className,
  ...props
}: React.ComponentPropsWithoutRef<typeof DialogPrimitive.Overlay>) {
  return (
    <DialogPrimitive.Overlay
      className={cn(
        'fixed inset-0 z-[var(--z-modal)] animate-fade-in bg-[var(--color-bg-overlay)] backdrop-blur-[2px]',
        className,
      )}
      {...props}
    />
  )
}

function DialogContent({
  className,
  children,
  dismissible = true,
  ...props
}: React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & { dismissible?: boolean }) {
  return (
    <DialogPrimitive.Portal>
      <DialogOverlay />
      <DialogPrimitive.Content
        onEscapeKeyDown={event => { if (!dismissible) event.preventDefault() }}
        onInteractOutside={event => { if (!dismissible) event.preventDefault() }}
        className={cn(
          'fixed left-1/2 top-1/2 z-[var(--z-modal)] w-[min(92vw,32rem)] -translate-x-1/2 -translate-y-1/2',
          'animate-dialog-in rounded-2xl border border-border bg-popover p-5 text-foreground shadow-[var(--shadow-lg)]',
          'focus:outline-none',
          className,
        )}
        {...props}
      >
        {children}
        {dismissible && (
          <DialogPrimitive.Close
            aria-label="关闭"
            className="absolute right-4 top-4 flex h-7 w-7 items-center justify-center rounded-lg text-muted-foreground transition hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            <X size={14} />
          </DialogPrimitive.Close>
        )}
      </DialogPrimitive.Content>
    </DialogPrimitive.Portal>
  )
}

function DialogHeader({
  className,
  icon,
  iconClassName,
  children,
  ...props
}: React.HTMLAttributes<HTMLDivElement> & {
  /** 标准图标盒（h-10 w-10 rounded-xl，默认品牌底）；传入后 children 自动进入
      文字列，行 items-center 使图标盒垂直居中于「标题+描述」整块：
      标准两行块高 52px，(52-40)/2=6px 上下对称；纯标题块(24px)时行高撑到
      40px，标题行中心仍与图标盒中心重合 */
  icon?: React.ReactNode
  iconClassName?: string
}) {
  return (
    <div className={cn('mb-4 flex items-center gap-3 pr-14', className)} {...props}>
      {icon && (
        <div
          className={cn(
            'flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-brand-soft text-brand-ink',
            iconClassName,
          )}
        >
          {icon}
        </div>
      )}
      {icon ? <div className="min-w-0">{children}</div> : children}
    </div>
  )
}

function DialogFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('mt-5 flex items-center justify-end gap-2', className)} {...props} />
}

function DialogTitle({
  className,
  ...props
}: React.ComponentPropsWithoutRef<typeof DialogPrimitive.Title>) {
  return (
    <DialogPrimitive.Title
      className={cn('text-base font-semibold leading-6 text-foreground', className)}
      {...props}
    />
  )
}

function DialogDescription({
  className,
  ...props
}: React.ComponentPropsWithoutRef<typeof DialogPrimitive.Description>) {
  return (
    <DialogPrimitive.Description
      className={cn('mt-1 text-sm leading-6 text-muted-foreground', className)}
      {...props}
    />
  )
}

export {
  Dialog,
  DialogTrigger,
  DialogClose,
  DialogContent,
  DialogHeader,
  DialogFooter,
  DialogTitle,
  DialogDescription,
}
