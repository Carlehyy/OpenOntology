/**
 * Tree 组件（vendor 自 reui.io/r/tree.json，ReUI copy-and-own 引入模式）。
 *
 * 与上游源码的差异（按 components/README.md 引入流程与 DESIGN.md §4.2 换肤规范）：
 * - Tailwind v4 语法降级为 v3.4：ps-(--var) → ps-[var(--var)]，祖先状态变体
 *   （in-data-[...]）改用命名 group（group/tree-item + group-data-[...]）；
 * - shadcn 色板（background/accent/muted-foreground）替换为平台语义 token；
 * - 上游经 @base-ui/react 的 mergeProps/useRender 渲染：为避免整包引入新组件
 *   运行时，这里内联等价实现（button 渲染 + 与 headless-tree 事件链式合并），
 *   数据状态机仍由 @headless-tree/core 提供；
 * - 图标直接用 lucide-react（上游 IconPlaceholder 是 reui 站内私有组件）；
 * - 未引入拖拽（上游 TreeDragLine 略），消费方按需再扩展。
 *
 * 用法（与 reui.io/components/tree 示例一致）：useTree 创建 tree 实例后，
 * <Tree tree={tree} label="…"> 内逐项渲染 TreeItem + TreeItemLabel。
 * 选中态依赖 TreeItem 上的 data-selected（由 headless-tree selectionFeature 注入）。
 */
import * as React from 'react'
import { createContext, useContext } from 'react'
import type { ItemInstance } from '@headless-tree/core'
import { ChevronDown, Minus, Plus } from 'lucide-react'

import { cn } from '@/lib/utils'

type ToggleIconType = 'chevron' | 'plus-minus'

interface TreeContextValue<T = any> {
  indent: number
  currentItem?: ItemInstance<T>
  tree?: any
  toggleIconType?: ToggleIconType
}

const TreeContext = createContext<TreeContextValue>({
  indent: 20,
  toggleIconType: 'plus-minus',
})

function useTreeContext<T = any>() {
  return useContext(TreeContext) as TreeContextValue<T>
}

interface TreeProps extends React.HTMLAttributes<HTMLDivElement> {
  indent?: number
  tree?: any
  /** 传给 headless-tree getContainerProps 的 aria-label（role=tree 容器） */
  label?: string
  toggleIconType?: ToggleIconType
}

function Tree({
  indent = 20,
  tree,
  label,
  className,
  toggleIconType = 'chevron',
  ...props
}: TreeProps) {
  const containerProps =
    tree && typeof tree.getContainerProps === 'function' ? tree.getContainerProps(label) : {}
  const { style: propStyle, ...otherProps } = { ...props, ...containerProps }

  return (
    <TreeContext.Provider value={{ indent, tree, toggleIconType }}>
      <div
        data-slot="tree"
        style={{ ...propStyle, '--tree-indent': `${indent}px` } as React.CSSProperties}
        className={cn('flex flex-col outline-none', className)}
        {...otherProps}
      />
    </TreeContext.Provider>
  )
}

interface TreeItemProps<T = any>
  extends Omit<React.ButtonHTMLAttributes<HTMLButtonElement>, 'indent'> {
  item: ItemInstance<T>
  indent?: number
  /** 单击选中回调：在 headless-tree 自身点击处理之后追加，defaultPrevented 时不触发 */
  onItemSelect?: (item: ItemInstance<T>) => void
}

function TreeItem<T = any>({
  item,
  indent,
  className,
  onItemSelect,
  onClick,
  children,
  ...props
}: TreeItemProps<T>) {
  const parentContext = useTreeContext<T>()
  const itemIndent = indent ?? parentContext.indent
  // headless-tree 注入 role/tabIndex/键盘处理等；style/onClick 单独摘出合并，
  // 避免 itemProps 整体展开时覆盖视觉属性
  const itemProps = typeof item.getProps === 'function' ? item.getProps() : {}
  const {
    style: itemStyle,
    onClick: itemOnClick,
    ...restItemProps
  } = itemProps as React.ButtonHTMLAttributes<HTMLButtonElement>
  const { style: propStyle, ...restProps } = props
  const handleClick = (event: React.MouseEvent<HTMLButtonElement>) => {
    itemOnClick?.(event)
    onClick?.(event)
    if (!event.defaultPrevented) onItemSelect?.(item)
  }

  return (
    <TreeContext.Provider value={{ ...parentContext, indent: itemIndent, currentItem: item }}>
      <button
        type="button"
        data-slot="tree-item"
        data-folder={item.isFolder() || undefined}
        data-focus={
          typeof item.isFocused === 'function' ? item.isFocused() || undefined : undefined
        }
        data-selected={
          typeof item.isSelected === 'function' ? item.isSelected() || undefined : undefined
        }
        aria-expanded={item.isFolder() ? item.isExpanded() : undefined}
        style={
          {
            ...itemStyle,
            ...propStyle,
            '--tree-padding': `${item.getItemMeta().level * itemIndent}px`,
          } as React.CSSProperties
        }
        className={cn(
          'group/tree-item relative z-10 flex w-full select-none ps-[var(--tree-padding)] text-left outline-none focus:z-20 focus-visible:ring-2 focus-visible:ring-ring',
          className,
        )}
        {...restProps}
        {...restItemProps}
        onClick={handleClick}
      >
        {children}
      </button>
    </TreeContext.Provider>
  )
}

interface TreeItemLabelProps<T = any> extends React.HTMLAttributes<HTMLSpanElement> {
  item?: ItemInstance<T>
}

function TreeItemLabel<T = any>({
  item: propItem,
  children,
  className,
  ...props
}: TreeItemLabelProps<T>) {
  const { currentItem, toggleIconType } = useTreeContext<T>()
  const item = propItem || currentItem

  if (!item) {
    return null
  }
  const folder = item.isFolder()

  return (
    <span
      data-slot="tree-item-label"
      className={cn(
        'flex min-w-0 flex-1 items-center gap-1.5 rounded-md px-2 py-1.5 text-sm text-[var(--color-text-primary)] transition-colors',
        'hover:bg-[var(--color-bg-hover)]',
        'group-data-[selected=true]/tree-item:bg-[var(--color-bg-hover)]',
        'group-focus-visible/tree-item:ring-2 group-focus-visible/tree-item:ring-[var(--color-ring)]',
        !folder && 'ps-7',
        className,
      )}
      {...props}
    >
      {folder &&
        (toggleIconType === 'plus-minus' ? (
          item.isExpanded() ? (
            <Minus size={14} strokeWidth={1} className="shrink-0 text-[var(--color-text-tertiary)]" />
          ) : (
            <Plus size={14} strokeWidth={1} className="shrink-0 text-[var(--color-text-tertiary)]" />
          )
        ) : (
          <ChevronDown
            size={16}
            strokeWidth={1}
            className={cn(
              'shrink-0 text-[var(--color-text-tertiary)] transition-transform',
              !item.isExpanded() && '-rotate-90',
            )}
          />
        ))}
      {children ||
        (typeof item.getItemName === 'function' ? item.getItemName() : null)}
    </span>
  )
}

export { Tree, TreeItem, TreeItemLabel }
