import { useId } from 'react'
import { Hammer, Sparkles } from 'lucide-react'


interface OperationsComingSoonProps {
  /** 子页名称，标题渲染为「{name}即将上线」 */
  name: string
  description: string
}

/** 智能运营子页的待开发占位，版式与技能社区占位页保持一致。 */
export function OperationsComingSoon({ name, description }: OperationsComingSoonProps) {
  const titleId = useId()
  return (
    <div className="relative flex min-h-[calc(100vh-7rem)] items-center justify-center overflow-hidden rounded-3xl border border-border bg-card p-6 shadow-[var(--shadow-lg)] backdrop-blur-xl">
      <div aria-hidden="true" className="pointer-events-none absolute -left-24 top-10 h-72 w-72 rounded-full bg-brand-mist blur-3xl" />
      <div aria-hidden="true" className="pointer-events-none absolute -right-24 bottom-0 h-80 w-80 rounded-full bg-[var(--color-info-bg)] blur-3xl" />
      <section className="relative w-full max-w-xl text-center" aria-labelledby={titleId}>
        <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-2xl border border-brand-line bg-card text-brand-ink shadow-[var(--shadow-md)]">
          <Hammer size={26} strokeWidth={1.8} />
        </div>
        <div className="mt-5 inline-flex items-center gap-1.5 rounded-full border border-brand-line bg-brand-soft px-3 py-1 text-xs font-medium text-brand-ink">
          <Sparkles size={12} /> 智能运营
        </div>
        <h1 id={titleId} className="mt-4 text-2xl font-semibold tracking-tight text-foreground sm:text-3xl">
          {name}即将上线
        </h1>
        <p className="mx-auto mt-3 max-w-md text-sm leading-6 text-muted-foreground">
          {description}
        </p>
      </section>
    </div>
  )
}
