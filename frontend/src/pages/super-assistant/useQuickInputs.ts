import { useCallback, useRef, useState } from 'react'

import { superAssistantApi, type QuickInput } from '@/api/superAssistant'

import { errorText } from './components/assistantPanelUtils'

/**
 * 输入框快捷内容的页面级数据源：小卡片（QuickInputMenu）与设置弹窗
 * （QuickInputsDialog）共享同一份 items，弹窗内增删改后卡片即时同步。
 * 全部变更走服务端（跨设备跟随账号），本地不做持久化；
 * reorder 失败自动回滚到重排前快照（拖拽是即发即忘交互，回滚必须在
 * 数据源内完成，调用方只负责 toast）。
 */
export function useQuickInputs() {
  const [items, setItems] = useState<QuickInput[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const itemsRef = useRef(items)
  itemsRef.current = items
  // 请求序号：慢响应回来时不覆盖更新的状态（打开卡片即刷新，竞态高频）
  const requestSeqRef = useRef(0)

  const refresh = useCallback(async () => {
    const seq = ++requestSeqRef.current
    setLoading(true)
    setError('')
    try {
      const data = await superAssistantApi.listQuickInputs()
      if (seq !== requestSeqRef.current) return
      setItems(data)
    }
    catch (err) {
      if (seq !== requestSeqRef.current) return
      setError(errorText(err, '快捷内容加载失败'))
    }
    finally {
      if (seq === requestSeqRef.current) setLoading(false)
    }
  }, [])

  /** 新建：服务端追加到末尾，成功后并入本地列表。失败抛出，调用方 toast。 */
  const create = useCallback(async (title: string, content: string) => {
    const created = await superAssistantApi.createQuickInput({ title, content })
    requestSeqRef.current += 1 // 变更成功后失效在途 GET，防旧列表响应回来覆盖
    setItems(current => [...current, created])
    return created
  }, [])

  /** 更新标题/内容：成功后局部替换。失败抛出，调用方 toast。 */
  const update = useCallback(async (id: string, patch: { title?: string; content?: string }) => {
    const updated = await superAssistantApi.updateQuickInput(id, patch)
    requestSeqRef.current += 1
    setItems(current => current.map(item => (item.id === updated.id ? updated : item)))
    return updated
  }, [])

  /** 删除：成功后局部移除。失败抛出，调用方 toast。 */
  const remove = useCallback(async (id: string) => {
    await superAssistantApi.deleteQuickInput(id)
    requestSeqRef.current += 1
    setItems(current => current.filter(item => item.id !== id))
  }, [])

  /** 全量顺序重排：乐观更新，失败回滚到快照后原样抛出。 */
  const reorder = useCallback(async (ids: string[]) => {
    const previous = itemsRef.current
    const byId = new Map(previous.map(item => [item.id, item]))
    const reordered = ids.map(id => byId.get(id)).filter((item): item is QuickInput => item !== undefined)
    const leftovers = previous.filter(item => !ids.includes(item.id))
    setItems([...reordered, ...leftovers])
    try {
      const next = await superAssistantApi.reorderQuickInputs(ids)
      requestSeqRef.current += 1
      setItems(next)
    }
    catch (err) {
      setItems(previous)
      throw err
    }
  }, [])

  return { items, loading, error, refresh, create, update, remove, reorder }
}
