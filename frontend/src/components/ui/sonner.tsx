"use client"

import {
  CircleCheck,
  Info,
  LoaderCircle,
  OctagonX,
  TriangleAlert,
} from 'lucide-react'
import * as React from 'react'
import { Toaster as Sonner } from 'sonner'

/**
 * shadcn/ui 官方 sonner 封装（copy-and-own）。
 * 出处：ui.shadcn.com/r/styles/default/sonner.json，2026-09-05 拷贝。
 *
 * 与上游仅两处差异（DIFF 标注）：
 * 1. 上游经 next-themes 的 useTheme 提供 theme；本仓库为 Vite + .dark 类主题，
 *    不引入第二套主题系统。深浅色由令牌工具类（bg-background 等）随 .dark 自动翻转，
 *    关闭按钮等内部变量的令牌映射在 App.tsx 挂载处以 style props 传入。
 * 2. toastOptions 补平台时长与中文无障碍标签。
 * 位置/层级/偏移/堆叠数等部署策略在 App.tsx 挂载处以 props 传入（官方 {...props} 透传）。
 */

type ToasterProps = React.ComponentProps<typeof Sonner>

const Toaster = ({ ...props }: ToasterProps) => {
  return (
    <Sonner
      className="toaster group"
      icons={{
        success: <CircleCheck className="h-4 w-4" />,
        info: <Info className="h-4 w-4" />,
        warning: <TriangleAlert className="h-4 w-4" />,
        error: <OctagonX className="h-4 w-4" />,
        loading: <LoaderCircle className="h-4 w-4 animate-spin" />,
      }}
      toastOptions={{
        classNames: {
          toast:
            'group toast group-[.toaster]:bg-background group-[.toaster]:text-foreground group-[.toaster]:border-border group-[.toaster]:shadow-lg',
          // 成功态给语义浅底：默认 bg-background 与页面底色同值，提示会被当成没发生。
          // toast 键的 bg-background 在编译产物中靠后、同特异性会级联胜出，需 ! 提权
          success: 'group-[.toaster]:!bg-[var(--color-success-bg)]',
          description: 'group-[.toast]:text-muted-foreground',
          actionButton:
            'group-[.toast]:bg-primary group-[.toast]:text-primary-foreground',
          cancelButton:
            'group-[.toast]:bg-muted group-[.toast]:text-muted-foreground',
        },
        // DIFF: 平台时长与中文无障碍标签
        duration: 4000,
        closeButtonAriaLabel: '关闭提示',
      }}
      {...props}
    />
  )
}

export { Toaster }
