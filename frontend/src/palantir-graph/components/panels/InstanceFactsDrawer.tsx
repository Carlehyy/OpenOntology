import { formatDateTime } from '@/utils/datetime'
import { useEffect, useMemo, useState } from 'react';
import {
  XMarkIcon,
  ClockIcon,
  ArrowPathIcon,
  CircleStackIcon,
  UserIcon,
  BoltIcon,
  PencilSquareIcon,
  LinkIcon,
  TrashIcon,
  ArrowUturnLeftIcon,
} from '@heroicons/react/24/outline';
import { useOntologyStore } from '../../store/ontologyStore';
import { listInstanceFacts, instanceAsOf, type PropertyFactDTO, type InstanceAsOfDTO } from '../../api/formalApi';

interface Props {
  instanceId: string;
  instanceLabel: string;
  onClose: () => void;
}

function sourceMeta(source: string): { icon: React.ElementType; cls: string; label: string } {
  if (source.startsWith('action://')) return { icon: BoltIcon, cls: 'text-yellow-400', label: source };
  if (source.startsWith('user://')) return { icon: UserIcon, cls: 'text-amber-400', label: source };
  if (source.startsWith('fn:')) return { icon: BoltIcon, cls: 'text-purple-400', label: source };
  if (source.startsWith('editor-save')) return { icon: PencilSquareIcon, cls: 'text-violet-400', label: source === 'editor-save' ? '编辑器保存' : '编辑器级联清理' };
  if (source === 'manual') return { icon: UserIcon, cls: 'text-amber-400', label: '手工录入' };
  if (source === 'collector' || source === 'import') return { icon: CircleStackIcon, cls: 'text-teal-400', label: source === 'collector' ? '数据采集' : '批量导入' };
  return { icon: CircleStackIcon, cls: 'text-gray-400', label: source };
}

const KIND_BADGE: Record<string, { label: string; cls: string; title: string }> = {
  derived: { label: '派生', cls: 'bg-purple-500/20 text-purple-300', title: '由函数从其他属性推算而来，不是数据源写入' },
  link: { label: '链接', cls: 'bg-cyan-500/20 text-cyan-300', title: '链接事实：关系的建立/解除与边属性变化都是事实' },
  object: { label: '存在性', cls: 'bg-red-500/20 text-red-300', title: '实例存在性（墓碑）事实：删除也留痕' },
  decision: { label: '决策', cls: 'bg-amber-500/20 text-amber-300', title: '人的审批决策：批准/拒绝都是事实' },
  absence: { label: '缺席', cls: 'bg-slate-500/30 text-slate-300', title: '缺席事实：查询结果为空/非空的翻转快照——"没有"也有出处，可回放当时确实是空的' },
};

/**
 * 实例属性溯源抽屉 — 展示 fo_property_facts 的追加式变更链：
 * 每个属性一组，从新到旧；每条事实带来源 / 操作者 / 因果指针 / supersedes 链。
 * 底部支持时态回放：选一个时刻，看那一刻实例"是什么样"。
 */
export default function InstanceFactsDrawer({ instanceId, instanceLabel, onClose }: Props) {
  const backendId = useOntologyStore((s) => s.backendId);
  const [facts, setFacts] = useState<PropertyFactDTO[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filterProp, setFilterProp] = useState<string>('');
  const [highlightId, setHighlightId] = useState<string | null>(null);
  // 时态回放
  const [replayT, setReplayT] = useState<string>('');
  const [replay, setReplay] = useState<InstanceAsOfDTO | null>(null);
  const [replayLoading, setReplayLoading] = useState(false);

  useEffect(() => {
    if (!backendId) return;
    setLoading(true);
    setError(null);
    listInstanceFacts(backendId, instanceId)
      .then(setFacts)
      .catch((e: any) => setError(typeof e?.detail === 'string' ? e.detail : (e?.message || '加载失败')))
      .finally(() => setLoading(false));
  }, [backendId, instanceId]);

  const propNames = useMemo(
    () => Array.from(new Set(facts.map((f) => f.propertyName))),
    [facts]
  );
  const factById = useMemo(() => new Map(facts.map((f) => [f.id, f])), [facts]);
  const visible = filterProp ? facts.filter((f) => f.propertyName === filterProp) : facts;

  const fmtTime = (iso?: string | null) => {
    if (!iso) return '-';
    return formatDateTime(new Date(iso), { seconds: true });
  };
  const fmtVal = (v: unknown) => {
    if (v === null || v === undefined) return '∅';
    if (typeof v === 'object') return JSON.stringify(v);
    return String(v);
  };

  /** 点击因果/推导指针 → 若指向本实例的事实则高亮滚动过去 */
  const jumpTo = (factId: string) => {
    if (!factById.has(factId)) return;
    setFilterProp('');
    setHighlightId(factId);
    setTimeout(() => {
      document.getElementById(`fact-${factId}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }, 50);
  };

  const doReplay = async (t: string) => {
    if (!backendId || !t) return;
    setReplayLoading(true);
    try {
      const r = await instanceAsOf(backendId, instanceId, new Date(t).toISOString());
      setReplay(r);
    } catch (e: any) {
      setError(typeof e?.detail === 'string' ? e.detail : (e?.message || '回放失败'));
    } finally {
      setReplayLoading(false);
    }
  };

  const setReplayToNow = () => {
    const d = new Date();
    const pad = (n: number) => String(n).padStart(2, '0');
    setReplayT(`${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`);
  };

  return (
    <div className="fixed inset-0 z-[80] flex justify-end">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-sm" onClick={onClose} />
      <div className="relative w-full max-w-md bg-gradient-to-b from-slate-900 to-slate-950 shadow-2xl flex flex-col animate-slide-in-right">
        <div className="flex items-center justify-between px-5 py-4 bg-gradient-to-r from-violet-900/40 to-indigo-900/40 border-b border-violet-700/30">
          <div className="flex items-center gap-2.5">
            <ClockIcon className="w-5 h-5 text-violet-400" />
            <div>
              <h3 className="text-sm font-bold text-white">属性溯源</h3>
              <p className="text-[11px] text-violet-300/70 font-mono truncate max-w-[240px]">{instanceLabel}</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg hover:bg-white/10"
            aria-label="关闭属性溯源"
            title="关闭属性溯源"
          >
            <XMarkIcon className="w-5 h-5 text-gray-400" />
          </button>
        </div>

        {/* 属性过滤 */}
        {propNames.length > 1 && (
          <div className="flex gap-1.5 flex-wrap px-5 py-2.5 border-b border-slate-800">
            <button onClick={() => setFilterProp('')}
              className={`px-2 py-0.5 rounded-full text-[11px] border transition-colors ${
                !filterProp ? 'bg-violet-500/20 text-violet-300 border-violet-500/40' : 'text-gray-500 border-slate-700 hover:text-gray-300'
              }`}>
              全部
            </button>
            {propNames.map((p) => (
              <button key={p} onClick={() => setFilterProp(p)}
                className={`px-2 py-0.5 rounded-full text-[11px] font-mono border transition-colors ${
                  filterProp === p ? 'bg-violet-500/20 text-violet-300 border-violet-500/40' : 'text-gray-500 border-slate-700 hover:text-gray-300'
                }`}>
                {p}
              </button>
            ))}
          </div>
        )}

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {loading && (
            <div className="flex justify-center py-10">
              <ArrowPathIcon className="w-5 h-5 text-gray-500 animate-spin" />
            </div>
          )}
          {error && <p className="text-center text-xs text-red-400 py-6">{error}</p>}
          {!loading && !error && visible.length === 0 && (
            <p className="text-center text-xs text-gray-500 py-10">
              还没有事实记录。<br />保存 / 执行动作后，属性变更会逐条留痕。
            </p>
          )}

          {/* 时间线（新 → 旧） */}
          <div className="relative pl-4 space-y-3">
            {visible.length > 0 && (
              <div className="absolute left-[5px] top-2 bottom-2 w-px bg-slate-700" />
            )}
            {visible.map((f) => {
              const meta = sourceMeta(f.source);
              const badge = KIND_BADGE[f.kind || 'property'];
              return (
                <div key={f.id} id={`fact-${f.id}`} className="relative">
                  <span className={`absolute -left-[15px] top-1.5 w-2.5 h-2.5 rounded-full border-2 border-slate-900 ${
                    f.supersedesId ? 'bg-violet-400' : 'bg-emerald-400'
                  }`} title={f.supersedesId ? '替代旧值' : '首个事实'} />
                  <div className={`rounded-lg border px-3 py-2 transition-all ${
                    highlightId === f.id
                      ? 'border-amber-400 bg-amber-500/10 ring-1 ring-amber-400/50'
                      : f.kind === 'derived'
                        ? 'border-purple-500/40 bg-purple-900/15'
                        : f.kind === 'decision'
                          ? 'border-amber-500/40 bg-amber-900/15'
                          : 'border-slate-700/70 bg-slate-800/40'
                  }`}>
                    <div className="flex items-center gap-2 text-xs">
                      <span className="font-mono text-violet-300">{f.propertyName}</span>
                      <span className="text-gray-500">=</span>
                      <span
                        className={`font-mono truncate flex-1 ${
                          f.present === false ? 'text-red-300' : 'text-gray-100'
                        }`}
                        title={f.present === false ? '该属性已从对象中删除' : undefined}
                      >
                        {f.present === false ? '（已删除）' : fmtVal(f.value)}
                      </span>
                      {badge && (
                        <span className={`px-1.5 rounded-full text-[10px] shrink-0 ${badge.cls}`} title={badge.title}>
                          {badge.label}
                        </span>
                      )}
                    </div>
                    <div className="flex items-center gap-2 mt-1.5 text-[10px] text-gray-500 flex-wrap">
                      <meta.icon className={`w-3 h-3 ${meta.cls}`} />
                      <span className="truncate max-w-[140px]" title={f.source}>{meta.label}</span>
                      {f.actorId && (
                        <span className="flex items-center gap-0.5 px-1 rounded bg-amber-900/30 text-amber-300/90"
                          title={`操作者：${f.actorId}`}>
                          <UserIcon className="w-2.5 h-2.5" />
                          {f.actorId.slice(0, 8)}
                        </span>
                      )}
                      {f.sourceDatasetVersionId && (
                        <span className="flex items-center gap-0.5 px-1 rounded bg-teal-900/40 text-teal-300/90"
                          title={`来源数据版本：${f.sourceDatasetVersionId}`}>
                          <CircleStackIcon className="w-2.5 h-2.5" />
                          {f.sourceDatasetVersionId.slice(0, 8)}
                        </span>
                      )}
                      {typeof f.confidence === 'number' && f.confidence < 1 && (
                        <span className="px-1 rounded bg-slate-700/80 text-gray-400" title="来源置信度">
                          conf {f.confidence.toFixed(2)}
                        </span>
                      )}
                      {(f.derivedFrom || []).length > 0 && (
                        <button
                          onClick={() => (f.derivedFrom || []).forEach((d, i) => { if (i === 0) jumpTo(d); })}
                          className="px-1 rounded bg-purple-900/40 text-purple-300/80 hover:bg-purple-800/50"
                          title={`推导自 ${(f.derivedFrom || []).length} 条输入事实（点击跳到第一条）：\n${(f.derivedFrom || []).join('\n')}`}>
                          ← {(f.derivedFrom || []).length} 条输入
                        </button>
                      )}
                      {f.causedBy && (
                        <button
                          onClick={() => jumpTo(f.causedBy!)}
                          className="px-1 rounded bg-slate-700/80 text-gray-400 hover:bg-slate-600/80"
                          title={`因果指针 → ${f.causedBy}${factById.has(f.causedBy) ? '（点击跳转）' : '（指向动作执行/决策记录）'}`}>
                          因 {f.causedBy.slice(0, 8)}…
                        </button>
                      )}
                      {f.supersedesId && (
                        <button
                          onClick={() => jumpTo(f.supersedesId!)}
                          className="px-1 rounded bg-violet-900/40 text-violet-300/80 hover:bg-violet-800/50"
                          title={`替代旧事实 ${f.supersedesId}（点击跳转）`}>
                          ⤴ 覆盖
                        </button>
                      )}
                      <span className="ml-auto shrink-0">{fmtTime(f.recordedAt)}</span>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>

        {/* 时态回放 */}
        <div className="px-5 py-3 border-t border-slate-800 space-y-2">
          <div className="flex items-center gap-2">
            <ArrowUturnLeftIcon className="w-3.5 h-3.5 text-violet-400 shrink-0" />
            <span className="text-[11px] text-gray-400 shrink-0">时态回放</span>
            <input
              type="datetime-local"
              value={replayT}
              onChange={(e) => setReplayT(e.target.value)}
              className="flex-1 bg-slate-800 border border-slate-700 rounded px-2 py-1 text-[11px] text-gray-200 focus:outline-none focus:border-violet-500/60"
            />
            <button
              onClick={setReplayToNow}
              className="px-2 py-1 rounded text-[11px] text-gray-400 hover:text-gray-200 hover:bg-slate-800"
            >
              现在
            </button>
            <button
              onClick={() => void doReplay(replayT)}
              disabled={!replayT || replayLoading}
              className="px-2.5 py-1 rounded bg-violet-500/20 border border-violet-500/40 text-violet-300 text-[11px] hover:bg-violet-500/30 disabled:opacity-40"
            >
              {replayLoading ? '…' : '回放'}
            </button>
            {replay && (
              <button onClick={() => setReplay(null)}
                className="px-2 py-1 rounded text-[11px] text-gray-500 hover:text-gray-300">
                清除
              </button>
            )}
          </div>
          {replay && (
            <div className="rounded-lg border border-violet-500/30 bg-violet-900/10 px-3 py-2 text-[11px]">
              <div className="flex items-center gap-2 text-violet-300 mb-1">
                {replay.exists
                  ? <ClockIcon className="w-3.5 h-3.5" />
                  : <TrashIcon className="w-3.5 h-3.5 text-red-400" />}
                <span>{fmtTime(replay.asOf)} 时刻{replay.exists ? '的世界' : '——实例已被删除'}</span>
                <span className="ml-auto text-gray-500">{replay.totalFacts} 条事实 ≤ T</span>
              </div>
              {replay.exists && (
                <div className="space-y-0.5 max-h-32 overflow-y-auto">
                  {Object.entries(replay.properties).map(([k, v]) => (
                    <div key={k} className="flex gap-2 font-mono">
                      <span className="text-violet-300/80">{k}</span>
                      <span className="text-gray-500">=</span>
                      <span className="text-gray-200 truncate">{fmtVal(v)}</span>
                    </div>
                  ))}
                  {Object.entries(replay.computed).map(([k, v]) => (
                    <div key={k} className="flex gap-2 font-mono">
                      <span className="text-purple-300/80">{k}</span>
                      <span className="text-gray-500">=</span>
                      <span className="text-gray-200 truncate">{fmtVal(v)}</span>
                      <span className="text-[9px] text-purple-400/60 self-center">派生</span>
                    </div>
                  ))}
                  {Object.keys(replay.properties).length === 0 && Object.keys(replay.computed).length === 0 && (
                    <div className="text-gray-500">该时刻还没有任何属性事实</div>
                  )}
                </div>
              )}
            </div>
          )}
          <div className="text-[10px] text-gray-600 flex items-center gap-1">
            <LinkIcon className="w-3 h-3" />
            追加式事实流：值从不被修改，只被新事实替代；任意时刻的世界 = 该时刻未被覆盖的事实投影
          </div>
        </div>
      </div>
    </div>
  );
}
