import axios, { type AxiosInstance, type AxiosRequestConfig } from 'axios'

// 根据环境动态设置 API baseURL
const getBaseURL = (version: string): string => {
  // 运行时可通过 window.__API_BASE_URL__ 注入后端地址
  const runtimeBase = (typeof window !== 'undefined' && (window as any).__API_BASE_URL__) || ''
  if (runtimeBase) {
    return `${runtimeBase}/api/${version}`
  }
  // 默认：使用相对路径
  // - 开发环境：Vite 代理到 localhost:8000
  // - 生产环境：需要 Nginx 将 /api 转发到后端，或同域名部署
  return `/api/${version}`
}

type ApiClient = {
  get: <T = any>(url: string, config?: AxiosRequestConfig) => Promise<T>
  getBlob: (url: string, config?: AxiosRequestConfig) => Promise<Blob>
  post: <T = any>(url: string, data?: unknown, config?: AxiosRequestConfig) => Promise<T>
  put: <T = any>(url: string, data?: unknown, config?: AxiosRequestConfig) => Promise<T>
  patch: <T = any>(url: string, data?: unknown, config?: AxiosRequestConfig) => Promise<T>
  delete: <T = any>(url: string, config?: AxiosRequestConfig) => Promise<T>
}

/** 请求拦截器：注入本地登录态 Bearer token（apiClient/apiClientV2 与 api-hub 实例共用）。 */
export function attachAuthInterceptor(client: AxiosInstance): void {
  client.interceptors.request.use(config => {
    const token = localStorage.getItem('token')
    if (token) config.headers.Authorization = `Bearer ${token}`
    return config
  })
}

/**
 * 响应错误统一处理：401 跳登录（带 returnTo 深链）、authRedirected 标记、status 附着，
 * 最后 reject 业务错误负载。Blob 错误体（responseType: 'blob' 的请求失败时 axios
 * 会把错误体也包成 Blob）先读出文本再按 JSON 解析，保证下载类接口与普通请求
 * 共享同一套 detail 提取与 401 跳转逻辑。
 */
export async function handleApiError(err: unknown): Promise<never> {
  const response = (err as { response?: { status?: number; data?: unknown } } | undefined)?.response
  if (response?.data instanceof Blob) {
    const text = await response.data.text()
    try {
      response.data = JSON.parse(text) as unknown
    } catch {
      response.data = { detail: text.slice(0, 500) }
    }
  }
  // 401 未授权，或 403 但属于「未鉴权」（FastAPI 未带 token 时返回 403 Not authenticated）→ 跳登录
  // 注意：登录后无权限的 403 不跳转，保留「权限不足」语义交给业务层处理
  const data = response?.data as { detail?: unknown; message?: unknown } | undefined
  const status = response?.status
  const detail = data?.detail || data?.message || ''
  const isUnauthenticated = status === 401 || (status === 403 && /not authenticated|unauthenticated/i.test(String(detail)))
  if (isUnauthenticated) {
    localStorage.removeItem('token')
    const currentRoute = window.location.hash.replace(/^#/, '') || '/'
    if (!currentRoute.startsWith('/login')) {
      // 登录页自身的 401（密码错误等）不做整页跳转：重写成裸 /#/login 会
      // 丢掉 ?returnTo= 深链参数，留给页面内联报错即可
      window.location.href = `/#/login?returnTo=${encodeURIComponent(currentRoute)}`
      // 标记「已跳转登录」：业务页的 catch 据此静默，不再弹
      // 「请检查服务连接」这类误导排障方向的错误 toast
      const rejection = response?.data
      if (rejection && typeof rejection === 'object') {
        ;(rejection as { authRedirected?: boolean }).authRedirected = true
      }
    }
  }
  const payload = response?.data ?? err
  // 携带 HTTP 状态码，业务层据此区分「权限不足」与「服务故障」（如密钥列表 403）
  if (status != null && payload && typeof payload === 'object' && (payload as { status?: number }).status === undefined) {
    ;(payload as { status?: number }).status = status
  }
  return Promise.reject(payload)
}

function createApiClient(version: string): ApiClient {
  const client = axios.create({ baseURL: getBaseURL(version) })
  attachAuthInterceptor(client)
  client.interceptors.response.use(
    res => res.data.data !== undefined ? res.data.data : res.data,
    handleApiError,
  )
  return {
    get: (url, config) => client.get(url, config) as Promise<any>,
    getBlob: (url, config) => client.get(url, { ...config, responseType: 'blob' }) as Promise<Blob>,
    post: (url, data, config) => client.post(url, data, config) as Promise<any>,
    put: (url, data, config) => client.put(url, data, config) as Promise<any>,
    patch: (url, data, config) => client.patch(url, data, config) as Promise<any>,
    delete: (url, config) => client.delete(url, config) as Promise<any>,
  }
}

export const apiClient = createApiClient('v1')
export const apiClientV2 = createApiClient('v2')
