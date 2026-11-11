import { ArrowDown, ArrowRight, Bell, BookOpen, Clock, Layers3, ListChecks, Network, Plug, Sparkles, type LucideIcon } from 'lucide-react'

import { Badge } from '@/components/ui/Badge'
import { DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'

/** 业务能力与连接边界的说明图；模块可用不代表跨模块自动化链路已经打通。 */
function CapabilityCard({ icon: Icon, title, status, children }: {
  icon: LucideIcon
  title: string
  status: '已接入' | '独立可用' | '按配置接入'
  children: React.ReactNode
}) {
  return (
    <article className="rounded-xl border border-border bg-card p-3">
      <div className="flex flex-wrap items-center gap-2">
        <Icon size={15} className="shrink-0 text-muted-foreground" aria-hidden="true" />
        <h3 className="text-sm font-medium">{title}</h3>
        <Badge variant={status === '已接入' ? 'success' : 'secondary'} className="ml-auto shrink-0 px-2 text-[11px]">{status}</Badge>
      </div>
      <p className="mt-2 text-xs leading-5 text-muted-foreground">{children}</p>
    </article>
  )
}

export default function CapabilityMapDialog() {
  return (
    <DialogContent className="flex max-h-[90dvh] w-[min(96vw,64rem)] flex-col overflow-hidden p-0" data-capability-map>
      <DialogHeader icon={<Layers3 size={18} aria-hidden="true" />} className="mb-0 shrink-0 border-b border-border px-5 py-4 pr-14">
        <DialogTitle>超级助手能力地图</DialogTitle>
        <DialogDescription>从业务信息到执行协作，了解超级助手的能力与连接边界。</DialogDescription>
      </DialogHeader>
      <div className="min-h-0 overflow-y-auto p-4 sm:p-5" data-capability-map-body>
        <div className="grid gap-3 md:grid-cols-[1fr_1.05fr_1fr]" aria-label="能力分层">
          <section className="min-w-0 rounded-xl border border-border bg-background p-3" aria-labelledby="capability-input">
            <h2 id="capability-input" className="mb-1 text-sm font-semibold">01 · 信息与业务背景</h2>
            <p className="mb-3 text-xs text-muted-foreground">理解业务发生了什么</p>
            <div className="space-y-2">
              <CapabilityCard icon={Bell} title="消息通知" status="独立可用">汇集站内与外部消息；自动交给助手解读的链路待打通。</CapabilityCard>
              <CapabilityCard icon={BookOpen} title="知识库（WeKnora）" status="按配置接入">原生 Web 由管理员配置模型与知识库，并把只读工具登记到平台；完成连接测试和登记后，助手才可检索与阅读文档。登录使用 WeKnora 原生账号。</CapabilityCard>
              <CapabilityCard icon={Network} title="本体治理" status="按配置接入">管理业务模型；助手可按授权委派本体助手查证、提出操作建议，修改需在本体界面确认。</CapabilityCard>
            </div>
          </section>

          <section className="flex min-w-0 flex-col rounded-xl border border-brand-line bg-brand-soft p-4" aria-labelledby="capability-assistant">
            <h2 id="capability-assistant" className="text-sm font-semibold text-brand-ink">02 · 理解与协调</h2>
            <div className="flex flex-1 flex-col items-center justify-center py-6 text-center">
              <Sparkles size={26} className="mb-3 text-brand-ink" aria-hidden="true" />
              <h3 className="text-lg font-semibold">超级助手</h3>
              <p className="mt-2 text-sm leading-6 text-muted-foreground">理解意图 · 检索知识<br />拆解任务 · 协调执行</p>
              <div className="mt-4 flex items-center gap-2 text-xs text-brand-ink" aria-hidden="true"><ArrowDown size={14} className="md:hidden" /><ArrowRight size={14} className="hidden md:block" />连接业务与行动</div>
            </div>
            <p className="border-t border-brand-line pt-3 text-xs leading-5 text-muted-foreground">按已接入的能力执行；需要授权的操作先确认，执行结果可回看。</p>
          </section>

          <section className="min-w-0 rounded-xl border border-border bg-background p-3" aria-labelledby="capability-output">
            <h2 id="capability-output" className="mb-1 text-sm font-semibold">03 · 执行与协作</h2>
            <p className="mb-3 text-xs text-muted-foreground">把目标推进为可追踪的行动</p>
            <div className="space-y-2">
              <CapabilityCard icon={Clock} title="定时任务" status="已接入">在定时任务入口设置时间与指令，到点调用助手并留下执行记录。</CapabilityCard>
              <CapabilityCard icon={ListChecks} title="任务实例" status="独立可用">手动激活任务模板并跟踪实例；助手自动生成实例的链路待打通。</CapabilityCard>
              <CapabilityCard icon={Plug} title="能力拓展" status="按配置接入">连接外部工作台与远程助手，查询、委派或协作；需先完成配置与授权。</CapabilityCard>
            </div>
          </section>
        </div>
      </div>
    </DialogContent>
  )
}
