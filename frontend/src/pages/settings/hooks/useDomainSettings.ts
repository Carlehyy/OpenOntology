import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { domainApi } from '@/api/ontologies'


function domainErrorMessage(error: any, fallback: string) {
  const detail = error?.detail
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail) && typeof detail[0]?.msg === 'string') return detail[0].msg
  if (detail && typeof detail.message === 'string') return detail.message
  if (typeof error?.message === 'string') return error.message
  return fallback
}

// create/update 的 409 detail 契约固定为「领域「xx」已存在」（settings/domains/router.py）；
// 识别后落到名称字段行内错误，不弹 toast
function isDuplicateNameError(error: any) {
  return typeof error?.detail === 'string' && error.detail.includes('已存在')
}

export function useDomainSettings(activeTab: string) {
  const [domainSearch, setDomainSearch] = useState('')
  const [showDomainModal, setShowDomainModal] = useState(false)
  const [editingDomain, setEditingDomain] = useState<any | null>(null)
  const [domainName, setDomainName] = useState('')
  const [domainDescription, setDomainDescription] = useState('')
  const [nameError, setNameError] = useState('')
  // 连续两次相同校验错误（如连点两次保存）时 nameError 值不变、focus effect 不会重跑，
  // nonce 保证每次失败提交都重新触发焦点回位
  const [nameErrorNonce, setNameErrorNonce] = useState(0)
  const [deleteDomainTarget, setDeleteDomainTarget] = useState<any | null>(null)
  const qc = useQueryClient()

  // -- Domain CRUD -------------------------------------------------------
  const { data: domainList = [], isLoading: domainsLoading } = useQuery({
    queryKey: ['domains', domainSearch],
    queryFn: () => domainApi.list(domainSearch || undefined) as any,
    enabled: activeTab === 'domains',
  })

  const createDomainMut = useMutation({
    mutationFn: (body: { name: string; description: string }) => domainApi.create(body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['domains'] })
      setShowDomainModal(false)
      toast.success('领域设置', { description: '创建成功' })
    },
    onError: (e: any) => {
      if (isDuplicateNameError(e)) {
        failNameValidation('该名称已存在')
        return
      }
      toast.error('操作失败', { description: domainErrorMessage(e, '创建失败') })
    },
  })

  const updateDomainMut = useMutation({
    mutationFn: ({ id, ...body }: { id: string; name?: string; description?: string }) =>
      domainApi.update(id, body),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['domains'] })
      qc.invalidateQueries({ queryKey: ['ontologies'] })
      setShowDomainModal(false)
      setEditingDomain(null)
      toast.success('领域设置', { description: '更新成功' })
    },
    onError: (e: any) => {
      if (isDuplicateNameError(e)) {
        failNameValidation('该名称已存在')
        return
      }
      toast.error('操作失败', { description: domainErrorMessage(e, '更新失败') })
    },
  })

  const deleteDomainMut = useMutation({
    mutationFn: (id: string) => domainApi.delete(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['domains'] })
      setDeleteDomainTarget(null)
      toast.success('领域设置', { description: '删除成功' })
    },
    onError: (e: any) => {
      const detail = domainErrorMessage(e, '删除失败')
      // 后端 409 detail 内嵌完整领域名，超长名称会把 toast 撑成多行；
      // 被删对象就是本次点击的领域，toast 中改称「该领域」，保留引用次数与处理办法
      const prefix = deleteDomainTarget?.name ? `领域「${deleteDomainTarget.name}」` : ''
      const message = prefix && detail.startsWith(prefix) ? `该领域${detail.slice(prefix.length)}` : detail
      toast.error('操作失败', { description: message })
    },
  })

  function failNameValidation(message: string) {
    setNameError(message)
    setNameErrorNonce(nonce => nonce + 1)
  }

  function openCreateDomain() {
    setEditingDomain(null)
    setDomainName('')
    setDomainDescription('')
    setNameError('')
    setShowDomainModal(true)
  }

  function openEditDomain(d: any) {
    setEditingDomain(d)
    setDomainName(d.name)
    setDomainDescription(d.description)
    setNameError('')
    setShowDomainModal(true)
  }

  function handleSaveDomain() {
    if (!domainName.trim()) {
      failNameValidation('请输入领域名称')
      return
    }
    setNameError('')
    if (editingDomain) {
      updateDomainMut.mutate({ id: editingDomain.id, name: domainName.trim(), description: domainDescription.trim() })
    } else {
      createDomainMut.mutate({ name: domainName.trim(), description: domainDescription.trim() })
    }
  }

  function handleDeleteDomain() {
    if (!deleteDomainTarget) return
    deleteDomainMut.mutate(deleteDomainTarget.id)
  }

  return {
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
  }
}

export type DomainSettingsViewModel = ReturnType<typeof useDomainSettings>
