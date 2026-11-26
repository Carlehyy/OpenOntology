export type RuleRoutingTab = 'events' | 'rules' | 'assistant' | 'stats'

export interface RuleRoutingLocation {
  tab: RuleRoutingTab
  eventId: string | null
  scheduledTaskId: string | null
  scheduledRunId: string | null
}

const viewKey = 'ruleRoutingTab'

export function ruleRoutingLocation(params: URLSearchParams): RuleRoutingLocation | null {
  const eventId = params.get('autoloopEvent') || null
  if (!eventId && params.get('ruleRouting') !== '1') return null
  const requested = params.get(viewKey)
  const tab = !eventId && (requested === 'rules' || requested === 'assistant' || requested === 'stats') ? requested : 'events'
  return { tab, eventId, scheduledTaskId: params.get('schedule'), scheduledRunId: params.get('scheduleRun') }
}

export function sameRuleRoutingLocation(a: RuleRoutingLocation | null, b: RuleRoutingLocation | null) {
  return a?.tab === b?.tab && a?.eventId === b?.eventId && a?.scheduledTaskId === b?.scheduledTaskId && a?.scheduledRunId === b?.scheduledRunId
}

export function writeRuleRoutingLocation(params: URLSearchParams, location: RuleRoutingLocation | null) {
  const next = new URLSearchParams(params)
  next.delete('ruleRouting'); next.delete(viewKey); next.delete('autoloopEvent')
  if (location) {
    next.set('ruleRouting', '1')
    if (location.tab !== 'events') next.set(viewKey, location.tab)
    if (location.eventId) next.set('autoloopEvent', location.eventId)
    if (location.scheduledTaskId) next.set('schedule', location.scheduledTaskId)
    else next.delete('schedule')
    if (location.scheduledRunId) next.set('scheduleRun', location.scheduledRunId)
    else next.delete('scheduleRun')
  }
  return next
}

export type GuardRoutingNavigation = (proceed: () => void, stay: () => void) => void
