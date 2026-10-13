/* shadcn/reUI 同源 Tooltip（Radix 原语），component-catalog「Tooltip 气泡提示」
   标准件。按 components/README.md 引入流程 vendored（拷贝源：
   ui.shadcn.com/r/styles/default/tooltip.json，2026-10-13）。
   平台适配：色板/阴影走 tokens.css 语义令牌、层级用 --z-tooltip(400)、
   密度取 text-xs（工作台弹窗内与正文辅文一致）、TW3 兼容改写
   origin-[var(--radix-tooltip-content-transform-origin)]。
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
      "z-[var(--z-tooltip)] overflow-hidden rounded-md border border-border bg-popover px-3 py-1.5 text-xs text-popover-foreground shadow-[var(--shadow-md)] animate-in fade-in-0 zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95 data-[side=bottom]:slide-in-from-top-2 data-[side=left]:slide-in-from-right-2 data-[side=right]:slide-in-from-left-2 data-[side=top]:slide-in-from-bottom-2 origin-[var(--radix-tooltip-content-transform-origin)]",
      className
    )}
    {...props}
  />
))
TooltipContent.displayName = TooltipPrimitive.Content.displayName

export { Tooltip, TooltipTrigger, TooltipContent, TooltipProvider }
