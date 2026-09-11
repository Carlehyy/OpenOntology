/* shadcn/ui Sidebar 的展示原语子集（SidebarGroup/GroupLabel/GroupContent/Menu/
   MenuItem/MenuButton/MenuBadge），来源：https://ui.shadcn.com/r/styles/new-york-v4/sidebar.json
   拷贝日期：2026-09-03。
   裁剪说明：平台工作台侧栏是定制 aside（自带移动端抽屉），不引入
   SidebarProvider/折叠态/Rail/Tooltip 等应用壳逻辑，故 SidebarMenuButton 去掉
   tooltip 与 icon 折叠态分支；Tailwind 3.4 适配（outline-hidden → outline-none，
   去除 TW4 重要修饰符后缀写法）；颜色按平台规范映射为语义令牌
   （sidebar-accent → --color-bg-hover，sidebar-foreground → --color-text-primary，
   label 弱化色 → --color-text-tertiary，ring → --color-ring）。 */
import * as React from 'react'
import { cn } from '@/lib/utils'

function SidebarGroup({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="sidebar-group"
      className={cn('relative flex w-full min-w-0 flex-col px-2 py-1.5', className)}
      {...props}
    />
  )
}

function SidebarGroupLabel({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="sidebar-group-label"
      className={cn(
        'flex h-8 shrink-0 items-center rounded-md px-2 text-xs font-medium text-[var(--color-text-tertiary)]',
        className,
      )}
      {...props}
    />
  )
}

function SidebarGroupContent({ className, ...props }: React.ComponentProps<'div'>) {
  return (
    <div
      data-slot="sidebar-group-content"
      className={cn('w-full text-sm', className)}
      {...props}
    />
  )
}

function SidebarMenu({ className, ...props }: React.ComponentProps<'ul'>) {
  return (
    <ul
      data-slot="sidebar-menu"
      className={cn('flex w-full min-w-0 flex-col gap-0.5', className)}
      {...props}
    />
  )
}

function SidebarMenuItem({ className, ...props }: React.ComponentProps<'li'>) {
  return (
    <li
      data-slot="sidebar-menu-item"
      className={cn('group/menu-item relative', className)}
      {...props}
    />
  )
}

function SidebarMenuButton({
  className,
  isActive = false,
  ...props
}: React.ComponentProps<'button'> & { isActive?: boolean }) {
  return (
    <button
      type="button"
      data-slot="sidebar-menu-button"
      data-active={isActive}
      className={cn(
        'flex w-full items-center gap-2 overflow-hidden rounded-lg px-2 py-1.5 text-left text-sm outline-none transition-colors',
        'hover:bg-[var(--color-bg-hover)] focus-visible:ring-2 focus-visible:ring-ring',
        'disabled:pointer-events-none disabled:opacity-50 [&>svg]:h-4 [&>svg]:w-4 [&>svg]:shrink-0',
        isActive && 'bg-[var(--color-bg-hover)] font-medium',
        className,
      )}
      {...props}
    />
  )
}

function SidebarMenuBadge({ className, ...props }: React.ComponentProps<'span'>) {
  return (
    <span
      data-slot="sidebar-menu-badge"
      className={cn(
        'pointer-events-none ml-auto flex h-5 min-w-5 items-center justify-center rounded-md px-1 text-xs font-medium tabular-nums text-[var(--color-text-tertiary)] select-none',
        className,
      )}
      {...props}
    />
  )
}

export {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuBadge,
  SidebarMenuButton,
  SidebarMenuItem,
}
