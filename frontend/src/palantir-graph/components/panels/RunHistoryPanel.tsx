import { formatDateTime } from '@/utils/datetime'
import { useCallback, useEffect, useState } from 'react';
import {
  XMarkIcon,
  ClockIcon,
  BoltIcon,
  ShieldExclamationIcon,
  BellIcon,
  ArrowPathIcon,
  CheckCircleIcon,
  XCircleIcon,
  HandRaisedIcon,
  ExclamationTriangleIcon,
} from '@heroicons/react/24/outline';
import { useOntologyStore } from '../../store/ontologyStore';
import { listExecutionLogs, listPendingActions, decidePendingAction } from '../../api/formalApi';
import { sentinelApi, type SentinelFiring, type SentinelNotification } from '../../../api/sentinelApi';
import type { ActionExecutionLog } from '../../types/ontology';
import {
  SentinelFiringSummary,
  sentinelFiringStatusLabel,
} from './SentinelFiringSummary';

interface Props {
  isOpen: boolean;
  onClose: () => void;
}

type Tab = 'approvals' | 'actions' | 'firings' | 'notifications';

/**
 * 运行历史面板 — 后端权威运行时的四类记录：
 *   待审批（HITL 闸门）/ Action 执行日志 / 哨兵触发记录 / 通知收件箱。
 * 这是"平台做了什么、还有什么等人拍板"的统一审计入口（本地模拟执行不在此列）。
 */
export default function RunHistoryPanel({ isOpen, onClose }: Props) {
  const backendId = useOntologyStore((s) => s.backendId);
  const loadFromBackend = useOntologyStore((s) => s.loadFromBackend);
  const workspaceMode = useOntologyStore((s) => s.workspaceMode);
  const trialRun = useOntologyStore((s) => s.workspaceTrialRun);
  const runtimeAccessible = workspaceMode === 'runtime';
  const [tab, setTab] = useState<Tab>('actions');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [logs, setLogs] = useState<ActionExecutionLog[]>([]);
  const [pending, setPending] = useState<ActionExecutionLog[]>([]);
  const [firings, setFirings] = useState<SentinelFiring[]>([]);
  const [notifications, setNotifications] = useState<SentinelNotification[]>([]);
  const [deciding, setDeciding] = useState<string | null>(null);
  const [feedback, setFeedback] = useState<{ type: 'success' | 'info' | 'error'; message: string } | null>(null);

  const refresh = useCallback(async () => {
    if (!backendId || !runtimeAccessible) {
      setLogs([]); setPending([]); setFirings([]); setNotifications([]);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const [l, p, f, n] = await Promise.all([
        listExecutionLogs(backendId),
        listPendingActions(backendId, { currentReleaseOnly: true }),
        sentinelApi.firings(backendId),
        sentinelApi.notifications(backendId),
      ]);
      setLogs(l);
      setPending(p || []);
      setFirings(f || []);
      setNotifications(n || []);
    } catch (e: any) {
      setError(typeof e?.detail === 'string' ? e.detail : (e?.message || '加载失败'));
    } finally {
      setLoading(false);
    }
  }, [backendId, runtimeAccessible]);

  useEffect(() => {
    if (isOpen) void refresh();
  }, [isOpen, refresh]);

  // 有待审批时默认落到审批页——决策等着人做，别藏在第二个 tab 后面
  useEffect(() => {
    if (isOpen && pending.length > 0) setTab('approvals');

  }, [isOpen, pending.length > 0]);

  if (!isOpen) return null;

  const fmtTime = (iso?: string | null) => {
    if (!iso) return '-';
    const d = new Date(iso);
    return formatDateTime(d, { seconds: true });
  };

  const handleDecide = async (log: ActionExecutionLog, decision: 'approved' | 'rejected') => {
    if (!backendId || !runtimeAccessible) return;
    let reason: string | undefined;
    if (decision === 'rejected') {
      const input = window.prompt('拒绝原因（会记录进决策事实，可留空）：');
      if (input === null) return; // 用户取消
      reason = input || undefined;
    }
    setDeciding(log.id);
    setError(null);
    setFeedback({
      type: 'info',
      message: decision === 'approved'
        ? (log.status === 'executing' ? '正在恢复已批准动作的技术执行…' : '正在批准并执行动作…')
        : '正在记录拒绝决策…',
    });
    try {
      if (!log.ontologyReleaseId) {
        throw new Error('该待审批动作缺少发布节点标识，已阻止跨版本审批。');
      }
      const decisionResult = await decidePendingAction(
        backendId,
        log.id,
        decision,
        reason,
        log.ontologyReleaseId,
      );
      // The decision endpoint may persist an approved human decision while the
      // technical execution fails.  Refresh first so the stale pending card is
      // removed even when we surface that failure below.
      await refresh();
      if (decision === 'approved') {
        const executionLog = (
          'executionLog' in decisionResult
            ? decisionResult.executionLog
            : undefined
        );
        if (!executionLog || executionLog.status !== 'success') {
          throw new Error(
            executionLog?.errorMessage
              || '审批决策已记录，但动作技术执行失败；平台未将其标记为已执行。'
          );
        }
      }
      if (decision === 'approved') {
        // 批准会真正执行动作 → 拉回最新实例/链接投影，画布与后端保持一致
        await loadFromBackend(backendId);
      }
      setFeedback({
        type: 'success',
        message: decision === 'approved'
          ? '已批准，动作已执行，实例数据与运行历史已刷新。'
          : '已拒绝，决策事实已记录到运行历史。',
      });
    } catch (e: any) {
      const msg = typeof e?.detail === 'string' ? e.detail : (e?.detail?.message || e?.message || '决策失败');
      setError(msg);
      setFeedback({ type: 'error', message: msg });
    } finally {
      setDeciding(null);
    }
  };

  const statusIcon = (s: string) => {
    switch (s) {
      case 'success': return <CheckCircleIcon className="w-4 h-4 text-green-400 shrink-0" />;
      case 'approved': return <CheckCircleIcon className="w-4 h-4 text-teal-400 shrink-0" />;
      case 'pending': return <HandRaisedIcon className="w-4 h-4 text-blue-400 shrink-0" />;
      case 'executing': return <ArrowPathIcon className="w-4 h-4 text-amber-400 shrink-0" />;
      case 'rejected': return <XCircleIcon className="w-4 h-4 text-amber-400 shrink-0" />;
      default: return <XCircleIcon className="w-4 h-4 text-red-400 shrink-0" />;
    }
  };

  const statusLabel = (s: string) => (
    s === 'pending'
      ? '待审批'
      : s === 'executing'
        ? '已批准，待恢复执行'
        : s === 'approved'
          ? '已批准'
          : s === 'rejected'
            ? '已拒绝'
            : s
  );

  const TABS: { id: Tab; label: string; icon: React.ElementType; count: number; alert?: boolean }[] = [
    { id: 'approvals', label: '待审批', icon: HandRaisedIcon, count: pending.length, alert: pending.length > 0 },
    { id: 'actions', label: 'Action 日志', icon: BoltIcon, count: logs.length },
    { id: 'firings', label: '哨兵触发', icon: ShieldExclamationIcon, count: firings.length },
    { id: 'notifications', label: '通知', icon: BellIcon, count: notifications.length },
  ];

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />
      <div className="relative w-full max-w-2xl bg-gradient-to-b from-slate-900 to-slate-950 shadow-2xl flex flex-col animate-slide-in-right">
        {/* Header */}
        <div className="flex items-center justify-between px-6 py-4 bg-gradient-to-r from-teal-900/40 to-cyan-900/40 border-b border-teal-700/30">
          <div className="flex items-center gap-3">
            <div className="p-2 rounded-lg bg-teal-500/20">
              <ClockIcon className="w-6 h-6 text-teal-400" />
            </div>
            <div>
              <h2 className="text-lg font-bold text-white">运行历史</h2>
              <p className="text-xs text-teal-300/70">待审批闸门 · 执行 · 触发 · 通知（后端权威记录）</p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button onClick={() => void refresh()} disabled={loading || !runtimeAccessible}
              className="p-2 rounded-lg hover:bg-white/10 disabled:opacity-40" title="刷新">
              <ArrowPathIcon className={`w-5 h-5 text-gray-400 ${loading ? 'animate-spin' : ''}`} />
            </button>
            <button onClick={onClose} className="p-2 rounded-lg hover:bg-white/10" aria-label="关闭运行历史" title="关闭运行历史">
              <XMarkIcon className="w-5 h-5 text-gray-400" />
            </button>
          </div>
        </div>

        {/* Tabs */}
        <div className="flex gap-1 px-6 pt-3">
          {TABS.map((t) => (
            <button key={t.id} onClick={() => setTab(t.id)}
              className={`flex items-center gap-1.5 px-3 py-2 rounded-t-lg text-xs transition-colors border-b-2 ${
                tab === t.id
                  ? 'text-teal-300 border-teal-400 bg-slate-800/60'
                  : 'text-gray-500 border-transparent hover:text-gray-300'
              }`}>
              <t.icon className={`w-4 h-4 ${t.alert ? 'text-blue-400' : ''}`} />
              {t.label}
              <span className={`px-1.5 rounded text-[10px] ${
                t.alert ? 'bg-blue-500/30 text-blue-200' : 'bg-slate-700/80'
              }`}>{t.count}</span>
            </button>
          ))}
        </div>

        {feedback && (
          <div className={`mx-6 mt-3 rounded-lg border px-3 py-2 text-xs ${
            feedback.type === 'success'
              ? 'border-green-500/40 bg-green-500/10 text-green-200'
              : feedback.type === 'error'
                ? 'border-red-500/40 bg-red-500/10 text-red-200'
                : 'border-blue-500/40 bg-blue-500/10 text-blue-200'
          }`}>
            {feedback.message}
          </div>
        )}

        {!runtimeAccessible && (
          <div role="status" className="mx-6 mt-3 rounded-lg border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
            {workspaceMode === 'trial'
              ? '这里展示本次隔离试跑摘要；正式动作日志、审批、哨兵触发和通知不会混入试跑版本。'
              : '运行历史功能区保持可见，但该版本不读取当前正式环境的运行记录。'}
          </div>
        )}

        {!runtimeAccessible && workspaceMode === 'trial' && trialRun && (
          <div data-testid="trial-run-summary" className="mx-6 mt-3 rounded-xl border border-amber-500/20 bg-slate-800/60 p-3 text-xs text-gray-300">
            <div className="flex items-center justify-between"><span className="font-medium text-amber-200">隔离试跑 · {trialRun.status}</span><span className="font-mono text-gray-500">{trialRun.id.slice(0, 8)}</span></div>
            <div className="mt-2 grid grid-cols-4 gap-2">
              {Object.entries(trialRun.result?.counts || {}).map(([key, value]) => (
                <div key={key} className="rounded bg-slate-900/60 px-2 py-1.5"><b className="block text-sm text-white">{String(value ?? 0)}</b>{key}</div>
              ))}
            </div>
            <p className="mt-2 text-emerald-300">外部动作执行数：{trialRun.result?.actionsExecuted ?? 0} · 副作用已阻断</p>
          </div>
        )}

        <div className="flex-1 overflow-y-auto p-6 space-y-2">
          {!backendId && runtimeAccessible && (
            <p className="text-center text-sm text-gray-500 py-10">本地模式没有后端运行历史</p>
          )}
          {error && (
            <p className="text-center text-xs text-red-400 py-4">{error}</p>
          )}

          {/* 待审批（HITL 决策闸门） */}
          {tab === 'approvals' && pending.map((l) => (
            <div key={l.id} className="rounded-lg border border-blue-500/40 bg-blue-500/5 px-3 py-3">
              <div className="flex items-center gap-2 text-sm">
                <HandRaisedIcon className="w-4 h-4 text-blue-400 shrink-0" />
                <span className="text-gray-100 font-medium truncate">{l.actionName || l.actionId}</span>
                <span className="ml-auto text-[11px] text-gray-500 shrink-0">{fmtTime(l.executedAt)}</span>
              </div>
              <div className="mt-1.5 pl-6 space-y-0.5 text-[11px] text-gray-400">
                {l.status === 'executing' && (
                  <div className="text-amber-300">
                    人工批准事实已保存，但技术执行尚未完成；可安全恢复，不会重复副作用。
                  </div>
                )}
                {l.objectInstanceId && <div>目标实例：<code className="text-gray-300">{l.objectInstanceId}</code></div>}
                {Object.keys(l.parameters || {}).length > 0 && (
                  <div>参数：{Object.entries(l.parameters).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join('，')}</div>
                )}
                <div className="text-gray-600">发起：{l.actorId ? `用户 ${l.actorId.slice(0, 8)}…` : '哨兵自动触发'}</div>
              </div>
              <div className="mt-2.5 pl-6 flex gap-2">
                <button
                  onClick={() => void handleDecide(l, 'approved')}
                  disabled={deciding === l.id}
                  className="px-4 py-1.5 rounded-lg text-xs font-medium bg-green-500/15 border border-green-500/50 text-green-300 hover:bg-green-500/25 disabled:opacity-40"
                >
                  {deciding === l.id
                    ? '处理中…'
                    : l.status === 'executing'
                      ? '↻ 恢复执行'
                      : '👍 批准并执行'}
                </button>
                {l.status === 'pending' && (
                  <button
                    onClick={() => void handleDecide(l, 'rejected')}
                    disabled={deciding === l.id}
                    className="px-4 py-1.5 rounded-lg text-xs font-medium bg-red-500/10 border border-red-500/40 text-red-300 hover:bg-red-500/20 disabled:opacity-40"
                  >
                    👎 拒绝
                  </button>
                )}
                <span className="self-center text-[10px] text-gray-600">
                  {l.status === 'executing'
                    ? '恢复使用原批准事实与稳定幂等键'
                    : '批准/拒绝都会写入决策事实（可回放）'}
                </span>
              </div>
            </div>
          ))}
          {tab === 'approvals' && !loading && pending.length === 0 && backendId && runtimeAccessible && (
            <div className="text-center py-10 text-gray-500">
              <HandRaisedIcon className="w-10 h-10 mx-auto mb-2 opacity-25" />
              <p className="text-xs">没有等待审批的动作</p>
              <p className="text-[11px] text-gray-600 mt-1">在动作编辑器勾选「需人工审批」后，真实执行会先在这里等你拍板</p>
            </div>
          )}

          {/* Action 日志 */}
          {tab === 'actions' && logs.map((l) => (
            <div key={l.id} className="rounded-lg border border-slate-700/70 bg-slate-800/40 px-3 py-2.5">
              <div className="flex items-center gap-2 text-sm">
                {statusIcon(l.status)}
                <span className="text-gray-200 font-medium truncate">{l.actionName || l.actionId}</span>
                <span className="px-1.5 rounded bg-slate-700 text-[10px] text-gray-400">{statusLabel(l.status)}</span>
                {l.dryRun && <span className="px-1.5 rounded bg-slate-700 text-[10px] text-gray-400">dry-run</span>}
                <span className="ml-auto text-[11px] text-gray-500 shrink-0">{fmtTime(l.executedAt)}</span>
              </div>
              {(l.effects || []).length > 0 && (
                <div className="mt-1.5 pl-6 space-y-0.5">
                  {(l.effects || []).slice(0, 4).map((e, i) => (
                    <div key={i} className="text-[11px] text-gray-400 truncate">· {e.description}</div>
                  ))}
                  {(l.effects || []).length > 4 && (
                    <div className="text-[11px] text-gray-600">… 共 {(l.effects || []).length} 项效果</div>
                  )}
                </div>
              )}
              {(l.status === 'approved' || l.status === 'rejected') && (
                <div className="mt-1 pl-6 text-[11px] text-gray-500">
                  {l.status === 'approved' ? '已由' : '已被'}
                  {l.decidedBy ? ` 用户 ${l.decidedBy.slice(0, 8)}… ` : ' '}
                  {l.status === 'approved' ? '批准执行' : '拒绝'}
                  {l.decisionReason ? `（${l.decisionReason}）` : ''}
                </div>
              )}
              {(l.errors || []).length > 0 && (
                <div className="mt-1 pl-6 text-[11px] text-red-400 truncate">{(l.errors || [])[0]}</div>
              )}
            </div>
          ))}
          {tab === 'actions' && !loading && logs.length === 0 && backendId && runtimeAccessible && (
            <p className="text-center text-xs text-gray-500 py-10">还没有 Action 执行记录</p>
          )}

          {/* 哨兵触发 */}
          {tab === 'firings' && firings.map((f) => (
            <div
              key={f.id}
              className="rounded-lg border border-slate-700/70 bg-slate-800/40 px-3 py-2.5"
              data-testid={`sentinel-firing-${f.id}`}
            >
              <div className="flex items-center gap-2 text-sm">
                {f.status === 'error'
                  ? <ExclamationTriangleIcon className="w-4 h-4 text-red-400 shrink-0" />
                  : <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${
                      f.status === 'fired' ? 'bg-rose-400' : f.status === 'no_match' ? 'bg-gray-500' : 'bg-amber-400'
                    }`} />}
                <span className="text-gray-200 font-medium truncate">{f.sentinelName}</span>
                <span className={`px-1.5 rounded text-[10px] ${
                  f.status === 'error' ? 'bg-red-500/20 text-red-300' : 'bg-slate-700 text-gray-400'
                }`}>{sentinelFiringStatusLabel(f.status)}</span>
                <span className="px-1.5 rounded bg-slate-700 text-[10px] text-gray-400">{f.triggerSource}</span>
                <span className="ml-auto text-[11px] text-gray-500 shrink-0">{fmtTime(f.createdAt)}</span>
              </div>
              <div className="mt-1 pl-3.5 text-[11px] text-gray-400">
                命中 {f.matchCount} 项
                {(f.actionResults || []).length > 0 && ` · 执行 ${(f.actionResults || []).length} 个动作`}
                {f.error && <span className="text-red-400"> · {f.error}</span>}
              </div>
              <div className="pl-3.5">
                <SentinelFiringSummary firing={f} />
              </div>
            </div>
          ))}
          {tab === 'firings' && !loading && firings.length === 0 && backendId && runtimeAccessible && (
            <p className="text-center text-xs text-gray-500 py-10">还没有哨兵触发记录</p>
          )}

          {/* 通知 */}
          {tab === 'notifications' && notifications.map((n) => (
            <div key={n.id} className="rounded-lg border border-slate-700/70 bg-slate-800/40 px-3 py-2.5">
              <div className="flex items-center gap-2 text-sm">
                <BellIcon className="w-4 h-4 text-yellow-400 shrink-0" />
                <span className="text-gray-200 font-medium truncate">{n.subject || '(无主题)'}</span>
                <span className="px-1.5 rounded bg-slate-700 text-[10px] text-gray-400">{n.channel}</span>
                <span className="ml-auto text-[11px] text-gray-500 shrink-0">{fmtTime(n.createdAt)}</span>
              </div>
              {n.body && <div className="mt-1 pl-6 text-[11px] text-gray-400 line-clamp-2">{n.body}</div>}
              <div className="mt-0.5 pl-6 text-[10px] text-gray-600">
                → {n.recipient} · {n.status}
                {n.ontologyReleaseId && (
                  <span title={n.ontologyReleaseId}> · 发布 {n.ontologyReleaseId.slice(0, 8)}</span>
                )}
                {n.actionLogId && (
                  <span title={n.actionLogId}> · 动作日志 {n.actionLogId.slice(0, 8)}</span>
                )}
              </div>
            </div>
          ))}
          {tab === 'notifications' && !loading && notifications.length === 0 && backendId && runtimeAccessible && (
            <p className="text-center text-xs text-gray-500 py-10">还没有通知</p>
          )}
        </div>
      </div>
    </div>
  );
}
