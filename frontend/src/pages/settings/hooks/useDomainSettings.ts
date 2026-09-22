import { useState } from 'react'
import { keepPreviousData, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { domainApi } from '@/api/ontologies'
import { useDebouncedValue } from '@/utils/useDebouncedValue'
import {
  domainErrorMessage,
  domainNameValidationError,
  isDuplicateNameError,
  shortenDeleteDetail,
  successNotice,
} from '../domainUxHelpers'

export function useDomainSettings(activeTab: string) {
  // 搜索词双轨：输入框绑即时值，查询用 300ms 防抖值，避免每敲一键发一次请求；
  // keepPreviousData 让键入换词期间保留上一屏列表，不闪回加载占位（UX 评审 P1-3）
  const [domainSearchInput, setDomainSearchInput] = useState('')
  const domainSearch = useDebouncedValue(domainSearchInput, 300)
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
    placeholderData: keepPreviousData,
  })

  function notifySuccess(action: 'create' | 'update' | 'delete', name?: string) {
    const { title, description } = successNotice(action, name)
    toast.success(title, { description })
  }

  const createDomainMut = useMutation({
    mutationFn: (body: { name: string; description: string }) => domainApi.create(body),
    onSuccess: (_result, variables) => {
      qc.invalidateQueries({ queryKey: ['domains'] })
      setShowDomainModal(false)
      notifySuccess('create', variables.name)
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
    onSuccess: (_result, variables) => {
      qc.invalidateQueries({ queryKey: ['domains'] })
      qc.invalidateQueries({ queryKey: ['ontologies'] })
      setShowDomainModal(false)
      setEditingDomain(null)
      notifySuccess('update', variables.name)
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
      notifySuccess('delete', deleteDomainTarget?.name)
    },
    onError: (e: any) => {
      const message = shortenDeleteDetail(domainErrorMessage(e, '删除失败'), deleteDomainTarget?.name)
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
    const validationError = domainNameValidationError(domainName)
    if (validationError) {
      failNameValidation(validationError)
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
  }
}

export type DomainSettingsViewModel = ReturnType<typeof useDomainSettings>
