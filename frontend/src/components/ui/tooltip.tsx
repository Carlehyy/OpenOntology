/* shadcn/reUI 同源 Tooltip（Radix 原语），component-catalog「Tooltip 气泡提示」
   标准件。按 components/README.md 引入流程 vendored（拷贝源：
   ui.shadcn.com/r/styles/default/tooltip.json，2026-10-13）。
   平台适配：色板走 tokens.css 语义令牌、层级用 --z-tooltip(400)、
   密度取 text-xs（工作台弹窗内与正文辅文一致）、TW3 兼容改写
   origin-[var(--radix-tooltip-content-transform-origin)]；阴影必须写任意属性
   形式（方括号内 box-shadow 冒号声明），不能用 shadow 工具类挂 var 值——
   TW3 会把那种写法解析成「阴影颜色」任意值、不产出 box-shadow 声明
   （对抗审查实测；此处也不在注释里写出该写法的字面量，避免被 Tailwind
   内容扫描器当成类名候选生成非法 CSS 撑爆构建）。
   已知状态：Radix 各包精确钉死自身依赖版本，本包与顶层 dialog/popover 带
   存在嵌套副本（上下文自包含，无功能冲突）；待全仓 @radix-ui 统一升级收敛。
   首个消费方：超级助手消息通知弹窗的图标操作按钮。 */
"use client"

import * as React from "react"
import * as TooltipPrimitive from "@radix-ui/react-tooltip"

import { cn } from "@/lib/utils"

const TooltipProvider = TooltipPrimitive.Provider

const Tooltip = TooltipPrimitive.Root

const TooltipTrigger = TooltipPrimitive.Trigger

const TooltipContent = React.forwardRef<
  React.ElementRef<typeof TooltipPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TooltipPrimitive.Content>
>(({ className, sideOffset = 4, ...props }, ref) => (
  <TooltipPrimitive.Content
    ref={ref}
    sideOffset={sideOffset}
    className={cn(
      "z-[var(--z-tooltip)] overflow-hidden rounded-md border border-border bg-popover px-3 py-1.5 text-xs text-popover-foreground [box-shadow:var(--shadow-md)] animate-in fade-in-0 zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 origin-[var(--radix-tooltip-content-transform-origin)]",
      className
    )}
    {...props}
  />
))
TooltipContent.displayName = TooltipPrimitive.Content.displayName

export { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider }
