import axios, { type AxiosRequestConfig } from 'axios'

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
  post: <T = any>(url: string, data?: unknown, config?: AxiosRequestConfig) => Promise<T>
  put: <T = any>(url: string, data?: unknown, config?: AxiosRequestConfig) => Promise<T>
  patch: <T = any>(url: string, data?: unknown, config?: AxiosRequestConfig) => Promise<T>
  delete: <T = any>(url: string, config?: AxiosRequestConfig) => Promise<T>
}

function createApiClient(version: string): ApiClient {
  const client = axios.create({ baseURL: getBaseURL(version) })
  client.interceptors.request.use(config => {
    const token = localStorage.getItem('token')
    if (token) config.headers.Authorization = `Bearer ${token}`
    return config
  })
  client.interceptors.response.use(
    res => res.data.data !== undefined ? res.data.data : res.data,
    err => {
      // 401 未授权，或 403 但属于「未鉴权」（FastAPI 未带 token 时返回 403 Not authenticated）→ 跳登录
      // 注意：登录后无权限的 403 不跳转，保留「权限不足」语义交给业务层处理
      const status = err.response?.status
      const detail = err.response?.data?.detail || err.response?.data?.message || ''
      const isUnauthenticated = status === 401 || (status === 403 && /not authenticated|unauthenticated/i.test(String(detail)))
      if (isUnauthenticated) {
        localStorage.removeItem('token')
        const currentRoute = window.location.hash.replace(/^#/, '') || '/'
        if (!currentRoute.startsWith('/login')) {
          // 登录页自身的 401（密码错误等）不做整页跳转：重写成裸 /#/login 会
          // 丢掉 ?returnTo= 深链参数，留给页面内联报错即可
          window.location.href = `/#/login?returnTo=${encodeURIComponent(currentRoute)}`
        }
      }
      return Promise.reject(err.response?.data ?? err)
    }
  )
  return {
    get: (url, config) => client.get(url, config) as Promise<any>,
    post: (url, data, config) => client.post(url, data, config) as Promise<any>,
    put: (url, data, config) => client.put(url, data, config) as Promise<any>,
    patch: (url, data, config) => client.patch(url, data, config) as Promise<any>,
    delete: (url, config) => client.delete(url, config) as Promise<any>,
  }
}

export const apiClient = createApiClient('v1')
export const apiClientV2 = createApiClient('v2')
