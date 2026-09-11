import { formatDateTime } from '@/utils/datetime'
import { useMemo, useState } from 'react';
import {
  DocumentTextIcon,
  ClockIcon,
  BoltIcon,
  CodeBracketIcon,
  CubeIcon,
  LinkIcon,
  CloudArrowUpIcon,
  CheckCircleIcon,
  ExclamationCircleIcon,
  ExclamationTriangleIcon,
  ShieldCheckIcon,
  ArrowPathIcon,
  CircleStackIcon,
} from '@heroicons/react/24/outline';
import { useOntologyStore } from '../store/ontologyStore';
import { lintOntology, lintSummary, type LintIssue } from '../utils/schemaLint';

interface HeaderProps {
  readOnly?: boolean;
  stageLabel?: string;
  onToggleActions?: () => void;
  onToggleFunctions?: () => void;
  showActions?: boolean;
  showFunctions?: boolean;
  onToggleLinks?: () => void;
  showLinks?: boolean;
  onToggleObjects?: () => void;
  showObjects?: boolean;
  onOpenMapping?: () => void;
}

export default function Header({ readOnly = false, stageLabel, onToggleActions, onToggleFunctions, showActions, showFunctions, onToggleLinks, showLinks, onToggleObjects, showObjects, onOpenMapping }: HeaderProps) {
  const { ontology, syncStatus, isDirty, syncError, saveToBackend, discardAndReload, lastSentinelSummary } = useOntologyStore();

  if (!ontology) return null;

  const stats = {
    objects: ontology.objectTypes.length,
    links: ontology.linkTypes.length,
    actions: ontology.actions.length,
    functions: ontology.functions?.length || 0,
  };

  return (
    <header className="fixed top-0 left-0 right-0 z-40 glass border-b border-surface-700">
      <div className="flex flex-wrap items-center gap-3 px-4 py-3 md:px-6 md:grid md:grid-cols-[1fr_auto_1fr]">
        <div className="flex min-w-0 items-center gap-4 md:justify-self-start">
          <div className="flex min-w-0 items-center gap-3">
            <div className="w-10 h-10 shrink-0 rounded-xl bg-gradient-to-br from-onto-500 to-purple-600 flex items-center justify-center">
              <DocumentTextIcon className="w-5 h-5 text-white" />
            </div>
            <div className="min-w-0">
              <h1 className="max-w-[13rem] truncate font-display text-lg font-bold text-surface-100 sm:max-w-[18rem] xl:max-w-none">
                {ontology.name}
              </h1>
              <p className="text-xs text-surface-500">{formatVersion(ontology.version)}</p>
            </div>
          </div>
        </div>

        <div className="order-3 flex w-full items-center justify-center gap-2 overflow-x-auto pb-1 md:order-none md:w-auto md:flex-none md:overflow-visible md:pb-0 md:justify-self-center">
          <button onClick={onToggleObjects} className="flex" aria-label={`查看对象实体，共 ${stats.objects} 个`}>
            <StatBadge icon={CubeIcon} label="对象实体" value={stats.objects} color="indigo" active={showObjects} />
          </button>
          <button onClick={onToggleLinks} className="flex" aria-label={`查看实体关系，共 ${stats.links} 个`}>
            <StatBadge
              icon={LinkIcon}
              label="实体关系"
              value={stats.links}
              color="cyan"
              active={showLinks}
            />
          </button>
          <button onClick={onToggleActions} className="flex" aria-label={`查看执行动作，共 ${stats.actions} 个`}>
            <StatBadge
              icon={BoltIcon}
              label="执行动作"
              value={stats.actions}
              color="yellow"
              active={showActions}
            />
          </button>
          <button onClick={onToggleFunctions} className="flex" aria-label={`查看激活函数，共 ${stats.functions} 个`}>
            <StatBadge
              icon={CodeBracketIcon}
              label="激活函数"
              value={stats.functions}
              color="cyan"
              active={showFunctions}
            />
          </button>
        </div>

        <div className="ml-auto flex shrink-0 flex-wrap items-center justify-end gap-2 text-sm text-surface-500 md:ml-0 md:justify-self-end">
          {onOpenMapping && (
            <button
              type="button"
              data-testid="open-mapping-workspace"
              onClick={onOpenMapping}
              className="inline-flex items-center gap-1.5 rounded-lg border border-teal-500/30 bg-teal-500/10 px-2.5 py-1.5 text-xs font-medium text-teal-300 transition-colors hover:border-teal-400/50 hover:bg-teal-500/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-teal-400"
              title="查看对象实体、实体关系与数据资产湖的映射"
            >
              <CircleStackIcon className="h-4 w-4" />
              数据映射
            </button>
          )}
          {readOnly && (
            <span className="rounded-lg border border-surface-700 bg-surface-800/70 px-2.5 py-1.5 text-xs font-medium text-surface-300">
              {stageLabel || '只读快照'}
            </span>
          )}
          <LintBadge readOnly={readOnly} />
          {syncStatus === 'saved' && lastSentinelSummary && lastSentinelSummary.fired > 0 && (
            <span className="flex items-center gap-1 px-2 py-1 rounded-lg border border-rose-500/40 bg-rose-900/25 text-xs text-rose-300"
              title={`本次保存评估了 ${lastSentinelSummary.evaluated} 个哨兵`}>
              ⚡ 触发 {lastSentinelSummary.fired} 个哨兵
            </span>
          )}
          {!readOnly && <SaveButton
            syncStatus={syncStatus}
            isDirty={isDirty}
            syncError={syncError}
            onSave={() => void saveToBackend()}
            onForceSave={() => void saveToBackend({ force: true })}
            onReload={() => void discardAndReload()}
          />}
          <ClockIcon className="hidden w-4 h-4 xl:block" />
          <span className="hidden xl:inline">更新于 {formatTime(ontology.updatedAt)}</span>
        </div>
      </div>
    </header>
  );
}

// Schema Lint 徽标：实时校验建模问题，点击展开问题列表，点条目跳转到对应元素
export function LintBadge({ readOnly = false }: { readOnly?: boolean }) {
  const ontology = useOntologyStore((s) => s.ontology);
  const { setSelectedNode, setSelectedEdge, setSelectedAction, setSelectedFunction, openPanel } = useOntologyStore();
  const [open, setOpen] = useState(false);

  const issues = useMemo(() => lintOntology(ontology), [ontology]);
  const { errors, warnings } = useMemo(() => lintSummary(issues), [issues]);

  if (issues.length === 0) {
    return (
      <span className="flex items-center gap-1 px-2 py-1.5 rounded-lg text-xs text-emerald-500/80" title="模型校验通过">
        <ShieldCheckIcon className="w-4 h-4" />
      </span>
    );
  }

  const jumpTo = (issue: LintIssue) => {
    switch (issue.targetKind) {
      case 'objectType':
        setSelectedNode(issue.targetId);
        openPanel('edit', 'objectType');
        break;
      case 'linkType':
        setSelectedEdge(issue.targetId);
        openPanel('edit', 'linkType');
        break;
      case 'action':
        setSelectedAction(issue.targetId);
        openPanel('edit', 'action');
        break;
      case 'function':
        setSelectedFunction(issue.targetId);
        openPanel('edit', 'function');
        break;
    }
    setOpen(false);
  };

  const hasError = errors > 0;
  if (readOnly) {
    return (
      <span
        className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs ${hasError
          ? 'border-red-500/40 bg-red-900/20 text-red-300'
          : 'border-amber-500/40 bg-amber-900/20 text-amber-300'}`}
        title={`模型校验：${errors} 个错误，${warnings} 个警告`}
      >
        <ExclamationTriangleIcon className="h-4 w-4" />
        {errors > 0 && <span>{errors} 错误</span>}
        {warnings > 0 && <span>{warnings} 警告</span>}
      </span>
    );
  }
  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-xs transition-colors ${
          hasError
            ? 'border-red-500/50 bg-red-900/30 text-red-300 hover:bg-red-900/50'
            : 'border-amber-500/40 bg-amber-900/20 text-amber-300 hover:bg-amber-900/40'
        }`}
        title="模型校验问题，点击查看"
      >
        <ExclamationTriangleIcon className="w-4 h-4" />
        {errors > 0 && <span>{errors} 错误</span>}
        {warnings > 0 && <span>{warnings} 警告</span>}
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full mt-2 z-50 w-96 max-h-[60vh] overflow-y-auto bg-surface-800 border border-surface-600 rounded-xl shadow-2xl p-2 animate-fade-in">
            <div className="px-2 py-1.5 text-xs text-surface-400 border-b border-surface-700 mb-1">
              模型校验 · {errors} 个错误，{warnings} 个警告
            </div>
            {issues.map((issue) => (
              <button
                key={issue.id}
                onClick={() => jumpTo(issue)}
                className="w-full text-left px-2 py-2 rounded-lg hover:bg-surface-700/70 transition-colors"
              >
                <div className="flex items-center gap-2">
                  <span className={`shrink-0 w-1.5 h-1.5 rounded-full ${issue.severity === 'error' ? 'bg-red-400' : 'bg-amber-400'}`} />
                  <span className="text-xs text-surface-200 font-medium truncate">{issue.targetLabel}</span>
                  <span className="text-[10px] text-surface-500 shrink-0">
                    {{ objectType: '对象', linkType: '关系', action: '动作', function: '函数' }[issue.targetKind]}
                  </span>
                </div>
                <div className="pl-3.5 text-xs text-surface-400 mt-0.5">{issue.message}</div>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

// 手动保存按钮：承载 idle/dirty/saving/saved/error/conflict 六态
export function SaveButton({
  syncStatus,
  isDirty,
  syncError,
  onSave,
  onForceSave,
  onReload,
}: {
  syncStatus: 'idle' | 'loading' | 'saving' | 'saved' | 'error' | 'conflict';
  isDirty: boolean;
  syncError: string | null;
  onSave: () => void;
  onForceSave: () => void;
  onReload: () => void;
}) {
  // conflict：保存被拒（其他会话已修改），必须由用户显式二选一
  if (syncStatus === 'conflict') {
    return (
      <span className="flex items-center gap-1.5 px-2 py-1 rounded-lg border border-amber-500/50 bg-amber-900/30 text-xs text-amber-200"
        title={syncError || '该本体已被其他会话修改'}>
        <ExclamationCircleIcon className="w-4 h-4 shrink-0" />
        <span className="hidden xl:inline">其他会话已修改</span>
        <button onClick={onReload}
          className="px-2 py-0.5 rounded bg-surface-800 hover:bg-surface-700 text-surface-200 border border-surface-600"
          title="放弃本地改动，重新加载最新数据">
          重新加载
        </button>
        <button onClick={() => {
            if (confirm('强制覆盖将丢弃其他会话保存的内容，用你当前的画布状态覆盖数据库。确定继续？')) onForceSave();
          }}
          className="px-2 py-0.5 rounded bg-red-900/50 hover:bg-red-900/70 text-red-200 border border-red-500/40"
          title="用当前画布状态覆盖数据库（丢弃他人改动）">
          强制覆盖
        </button>
      </span>
    );
  }
  // saving：保存中，禁用 + spinner
  if (syncStatus === 'saving') {
    return (
      <button disabled
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-surface-700 text-xs text-surface-400 cursor-wait">
        <ArrowPathIcon className="w-4 h-4 animate-spin" />
        保存中…
      </button>
    );
  }
  // error：保存失败，红色，可点击重试
  if (syncStatus === 'error') {
    return (
      <button onClick={onSave}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-red-500/50 bg-red-900/40 text-xs text-red-300 hover:bg-red-900/60"
        title={syncError || '保存失败，点击重试'}>
        <ExclamationCircleIcon className="w-4 h-4" />
        重试
      </button>
    );
  }
  // saved：刚刚保存成功，绿色 ✓（store 层 3s 后自动复位回 idle）
  if (syncStatus === 'saved') {
    return (
      <span className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-emerald-500/50 bg-emerald-900/30 text-xs text-emerald-300">
        <CheckCircleIcon className="w-4 h-4" />
        已保存到数据库
      </span>
    );
  }
  // idle + dirty：有未保存改动，高亮可点
  if (isDirty) {
    return (
      <button onClick={onSave}
        className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-violet-500/50 bg-violet-900/40 text-xs text-violet-200 hover:bg-violet-800/50">
        <CloudArrowUpIcon className="w-4 h-4" />
        保存全部更改
      </button>
    );
  }
  // idle 且无改动：默认灰态（全部已保存）
  return (
    <span className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg border border-surface-700 text-xs text-surface-500">
      <CheckCircleIcon className="w-4 h-4" />
      已保存到数据库
    </span>
  );
}

function formatVersion(version?: string) {
  if (!version) return 'v0';
  return version.trim().toLowerCase().startsWith('v') ? version : `v${version}`;
}

function StatBadge({
  icon: Icon,
  label,
  value,
  color,
  active,
}: {
  icon: React.ElementType;
  label: string;
  value: number;
  color: 'indigo' | 'cyan' | 'purple' | 'yellow' | 'violet';
  active?: boolean;
}) {
  const base = {
    indigo: 'bg-indigo-500/10 text-indigo-400 border-indigo-500/20',
    cyan: 'bg-cyan-500/10 text-cyan-400 border-cyan-500/20',
    purple: 'bg-purple-500/10 text-purple-400 border-purple-500/20',
    yellow: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20',
    violet: 'bg-violet-500/10 text-violet-400 border-violet-500/20',
  };
  const countBg = {
    indigo: 'bg-indigo-500/20 text-indigo-300',
    cyan: 'bg-cyan-500/20 text-cyan-300',
    purple: 'bg-purple-500/20 text-purple-300',
    yellow: 'bg-yellow-500/20 text-yellow-300',
    violet: 'bg-violet-500/20 text-violet-300',
  };
  const activeBase: Partial<Record<typeof color, string>> = {
    yellow: 'bg-yellow-500/20 text-yellow-300 border-yellow-500/30',
    cyan: 'bg-cyan-500/20 text-cyan-300 border-cyan-500/30',
  };
  const cls = active && activeBase[color] ? activeBase[color] : base[color];

  return (
    <div className={`px-3 py-1.5 rounded-lg border ${cls} flex shrink-0 items-center gap-2 transition-colors`}>
      <Icon className="w-4 h-4 shrink-0" />
      <span className="text-xs text-surface-200">{label}</span>
      <span className={`px-1.5 py-0.5 text-xs rounded font-mono font-semibold ${countBg[color]}`}>{value}</span>
    </div>
  );
}

function formatTime(isoString: string): string {
  return formatDateTime(isoString);
}
