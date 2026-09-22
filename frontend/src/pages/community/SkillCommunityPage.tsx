import { Hammer, Sparkles } from 'lucide-react'


export default function SkillCommunityPage() {
  return (
    <div className="relative flex min-h-[calc(100vh-7rem)] items-center justify-center overflow-hidden rounded-3xl border border-border bg-card p-6 shadow-[0_18px_60px_rgba(15,23,42,0.06)] backdrop-blur-xl">
      <div aria-hidden="true" className="pointer-events-none absolute -left-24 top-10 h-72 w-72 rounded-full bg-brand-mist blur-3xl" />
      <div aria-hidden="true" className="pointer-events-none absolute -right-24 bottom-0 h-80 w-80 rounded-full bg-[var(--color-info-bg)] blur-3xl" />
      <section className="relative w-full max-w-xl text-center" aria-labelledby="skill-community-title">
        <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-2xl border border-brand-line bg-card text-brand-ink shadow-[0_12px_34px_rgba(13,148,136,0.12)]">
          <Hammer size={26} strokeWidth={1.8} />
        </div>
        <div className="mt-5 inline-flex items-center gap-1.5 rounded-full border border-brand-line bg-brand-soft px-3 py-1 text-xs font-medium text-brand-ink">
          <Sparkles size={12} /> 技能社区
        </div>
        <h1 id="skill-community-title" className="mt-4 text-2xl font-semibold tracking-tight text-foreground sm:text-3xl">
          技能社区即将上线
        </h1>
        <p className="mx-auto mt-3 max-w-md text-sm leading-6 text-muted-foreground">
          技能的发现、安装与管理能力正在开发中。
        </p>
      </section>
    </div>
  )
}
