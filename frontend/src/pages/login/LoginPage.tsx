import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { useLocation, useNavigate } from 'react-router-dom'
import {
  Bot,
  Eye,
  EyeOff,
  Layers3,
  LockKeyhole,
  ShieldCheck,
  UserRound,
} from 'lucide-react'
import { authApi } from '@/api/auth'
import { useAuthStore } from '@/stores/authStore'
import loginReference from '@/assets/login-reference.png'
import './login.css'

type LoginForm = {
  username: string
  password: string
}

function safeReturnTo(value: unknown): string {
  if (typeof value !== 'string') return '/'
  const path = value.trim()
  if (!path.startsWith('/') || path.startsWith('//') || path.startsWith('/login')) return '/'
  return path
}

function OntologyMark({ size = 30 }: { size?: number }) {
  return (
    <svg
      aria-hidden="true"
      width={size}
      height={size}
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
    >
      <path d="M16 7.2v6.1M9.8 20.2l4.1-3.3M22.2 20.2l-4.1-3.3" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" />
      <circle cx="16" cy="5.5" r="4" stroke="currentColor" strokeWidth="2.6" />
      <circle cx="7.6" cy="22.5" r="4" stroke="currentColor" strokeWidth="2.6" />
      <circle cx="24.4" cy="22.5" r="4" stroke="currentColor" strokeWidth="2.6" />
      <circle cx="16" cy="15.6" r="3.2" fill="currentColor" />
    </svg>
  )
}

const features = [
  {
    icon: OntologyMark,
    title: '可视化本体建模',
    description: '编排对象、关系、动作与规则',
  },
  {
    icon: Layers3,
    title: '数据资产治理',
    description: '采集、映射与投影企业数据',
  },
  {
    icon: Bot,
    title: '智能推理与应用',
    description: '状态监听、条件判断、自动执行',
  },
  {
    icon: ShieldCheck,
    title: '安全合规可靠',
    description: '角色权限控制与可审计接口',
  },
]

export default function LoginPage() {
  const { register, handleSubmit, formState: { errors } } = useForm<LoginForm>()
  const setAuth = useAuthStore(state => state.setAuth)
  const navigate = useNavigate()
  const location = useLocation()
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [showPassword, setShowPassword] = useState(false)

  const onSubmit = async (data: LoginForm) => {
    setLoading(true)
    setError('')
    try {
      const res = await authApi.login(data.username, data.password) as any
      localStorage.setItem('token', res.access_token)
      const profile = await authApi.profile() as any
      setAuth(profile, res.access_token)
      const routeState = location.state as { returnTo?: unknown } | null
      const queryReturnTo = new URLSearchParams(location.search).get('returnTo')
      navigate(safeReturnTo(routeState?.returnTo ?? queryReturnTo), { replace: true })
    } catch (e: any) {
      localStorage.removeItem('token')
      // axios 拦截器 reject 的是已解包的响应体（{detail}）或原始错误，不能按
      // e.response.data.detail 取值；detail 也可能是对象（如 422 校验数组），
      // 必须确认是字符串才能渲染，否则 React 渲染对象会崩溃
      const detail = e?.detail
      const message = e?.message
      setError(
        (typeof detail === 'string' && detail)
        || (typeof message === 'string' && message)
        || '登录失败，请检查用户名和密码',
      )
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="ontology-login">
      <section className="ontology-login__canvas" aria-label="OpenOntology 登录">
        <img
          className="ontology-login__reference-art"
          src={loginReference}
          alt=""
          aria-hidden="true"
        />

        <header className="ontology-login__brand">
          <span className="ontology-login__brand-mark"><OntologyMark size={31} /></span>
          <span className="ontology-login__brand-name">
            <span>Open</span><span>Ontology</span>
          </span>
        </header>

        <section className="ontology-login__story" aria-labelledby="login-story-title">
          <h1 id="login-story-title">
            <span>构建本体智能</span>
            <strong>让数据辅助决策</strong>
          </h1>
          <p>
            OpenOntology 是一款新一代本体构建与管理平台，帮助企业快速构建、管理和应用领域本体，让数据变化自动驱动业务运转。
          </p>
        </section>

        <ul className="ontology-login__features" aria-label="平台能力">
          {features.map(({ icon: Icon, title, description }) => (
            <li key={title}>
              <span className="ontology-login__feature-icon"><Icon size={21} /></span>
              <span>
                <strong>{title}</strong>
                <small>{description}</small>
              </span>
            </li>
          ))}
        </ul>

        <section className="ontology-login__card" aria-labelledby="login-title">
          <div className="ontology-login__dots" aria-hidden="true" />
          <div className="ontology-login__card-content">
            <div className="ontology-login__heading">
              <h2 id="login-title">欢迎回来</h2>
              <p>登录您的 OpenOntology 账号</p>
            </div>

            <form onSubmit={handleSubmit(onSubmit)} className="ontology-login__form" noValidate>
              <label htmlFor="login-username">用户名</label>
              <div className="ontology-login__input-wrap">
                <UserRound size={20} aria-hidden="true" />
                <input
                  id="login-username"
                  autoComplete="username"
                  {...register('username', { required: '请输入用户名' })}
                  placeholder="请输入用户名"
                  aria-invalid={errors.username ? 'true' : undefined}
                />
              </div>
              {errors.username && (
                <p className="ontology-login__field-error" role="alert">{errors.username.message}</p>
              )}

              <label htmlFor="login-password">密码</label>
              <div className="ontology-login__input-wrap">
                <LockKeyhole size={19} aria-hidden="true" />
                <input
                  id="login-password"
                  autoComplete="current-password"
                  {...register('password', { required: '请输入密码' })}
                  type={showPassword ? 'text' : 'password'}
                  placeholder="请输入密码"
                  aria-invalid={errors.password ? 'true' : undefined}
                />
                <button
                  type="button"
                  className="ontology-login__visibility"
                  onClick={() => setShowPassword(value => !value)}
                  aria-label={showPassword ? '隐藏密码' : '显示密码'}
                  aria-pressed={showPassword}
                >
                  {showPassword ? <EyeOff size={20} /> : <Eye size={20} />}
                </button>
              </div>
              {errors.password && (
                <p className="ontology-login__field-error" role="alert">{errors.password.message}</p>
              )}

              {error && <div className="ontology-login__error" role="alert">{error}</div>}

              <button type="submit" className="ontology-login__submit" disabled={loading}>
                {loading ? (
                  <span><i aria-hidden="true" />登录中...</span>
                ) : '登录'}
              </button>
            </form>
          </div>

          <div className="ontology-login__wave" aria-hidden="true">
            <span className="ontology-login__shield"><ShieldCheck size={27} /></span>
          </div>
        </section>
      </section>
    </main>
  )
}
