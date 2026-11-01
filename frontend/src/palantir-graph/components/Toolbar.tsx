import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import {
  CubeIcon,
  LinkIcon,
  BoltIcon,
  CodeBracketIcon,
  ArrowDownTrayIcon,
  ArrowUpTrayIcon,
  ArrowPathIcon,
  SparklesIcon,
  ArrowsPointingOutIcon,
  ChevronRightIcon,
  ArrowUturnLeftIcon,
  ArrowUturnRightIcon,
  MagnifyingGlassIcon,
} from '@heroicons/react/24/outline';
import { ontologyApi } from '@/api/ontologies';
import { useOntologyStore } from '../store/ontologyStore';
import { LAYOUT_ALGORITHMS, LAYOUT_DIRECTIONS, type LayoutAlgorithm, type LayoutDirection } from '../utils/layoutAlgorithms';
import type { GraphWorkspaceCapabilities } from '../workspaceCapabilities';

/** 后端错误体（{detail}/{message} 或嵌套 detail.message）转可读文案。 */
function exportErrorMessage(error: unknown): string {
  const candidate = typeof error === 'object' && error !== null
    ? error as { detail?: unknown; message?: unknown }
    : null;
  const detail = candidate?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail === 'object' && 'message' in detail && typeof detail.message === 'string') {
    return detail.message;
  }
  return typeof candidate?.message === 'string' ? candidate.message : '导出失败，请稍后重试';
}

const STRUCTURE_PACKAGE_GUIDE = '该文件是本体的可移植结构包（含对象实体/实体关系/执行动作/激活函数/哨兵引擎），'
  + '请到「本体管理」页使用「本地导入」创建新本体；图谱编辑器的导入仅支持画布本地备份 JSON。';

interface ToolbarProps {
  capabilities: GraphWorkspaceCapabilities;
  onOpenSearch: () => void;
  /** 覆盖「返回」行为；缺省为 history 后退。嵌入模式下由宿主接管或不渲染 */
  onBack?: () => void;
  /** 是否渲染「返回」按钮（嵌入宿主有自己的导航时传 false） */
  showBack?: boolean;
}

export default function Toolbar({ capabilities, onOpenSearch, onBack, showBack = true }: ToolbarProps) {
  const navigate = useNavigate();
  const {
    ontology, openPanel, exportOntology, importOntology, reset, autoLayout,
    backendId, workspaceVersionId, isDirty, discardAndReload,
    canUndo, canRedo, undo, redo,
  } = useOntologyStore();
  const [showImportModal, setShowImportModal] = useState(false);
  const [importData, setImportData] = useState('');
  const [importError, setImportError] = useState('');
  const [isExporting, setIsExporting] = useState(false);
  const [showLayoutMenu, setShowLayoutMenu] = useState(false);
  const [selectedAlgorithm, setSelectedAlgorithm] = useState<LayoutAlgorithm>('dagre');
  const [selectedDirection, setSelectedDirection] = useState<LayoutDirection>('TB');
  const schemaDisabledReason = capabilities.schemaDisabledReason || '当前状态不可修改模型结构';

  const handleAutoLayout = () => {
    autoLayout(selectedAlgorithm, selectedDirection);
    setShowLayoutMenu(false);
  };

  // 后端绑定模式：与详情页「导出本体结构」调用同一端点，下载完整可移植结构包
  // （草稿/归档工作区带 version_id 精确导出该快照）。纯本地演示模式保留原有
  // 画布备份下载，不伪装成结构包。
  const handleExport = async () => {
    if (isExporting) return;
    if (!backendId) {
      const data = exportOntology();
      const blob = new Blob([data], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `${ontology?.name || 'ontology'}.json`;
      a.click();
      URL.revokeObjectURL(url);
      return;
    }
    setIsExporting(true);
    try {
      await ontologyApi.exportOntology(
        backendId,
        ontology?.name || 'ontology',
        ontology?.version || 'v0',
        workspaceVersionId,
      );
      toast.success('本体结构 JSON 已下载');
    } catch (error: unknown) {
      toast.error('本体结构导出失败', { description: exportErrorMessage(error) });
    } finally {
      setIsExporting(false);
    }
  };

  const closeImportModal = () => {
    setShowImportModal(false);
    setImportError('');
  };

  const handleImport = () => {
    if (!importData.trim()) return;
    let parsed: unknown;
    try {
      parsed = JSON.parse(importData);
    } catch {
      setImportError('JSON 解析失败：请检查粘贴内容是否为完整的 JSON。');
      return;
    }
    // 结构包不是画布备份：直接导入会把现有画布清空，先拦截并指引到正确的入口。
    if (parsed && typeof parsed === 'object' && (parsed as { format?: unknown }).format === 'ontology-structure') {
      setImportError(STRUCTURE_PACKAGE_GUIDE);
      return;
    }
    importOntology(importData);
    setImportData('');
    setImportError('');
    setShowImportModal(false);
  };

  // 后端绑定模式下："重置"= 放弃本地改动、从数据库重新加载（绝不载入演示数据，
  // 避免误点后一键保存把真实本体覆盖成 demo）。纯本地模式才允许载入演示数据。
  const handleReset = () => {
    if (backendId) {
      if (confirm(isDirty
        ? '放弃当前未保存的改动，并从数据库重新加载？'
        : '从数据库重新加载本体？')) {
        void discardAndReload();
      }
    } else if (confirm('将画布重置为演示数据（贸易 ERP 示例）？当前本地内容将被替换。')) {
      reset();
    }
  };

  // 离开语义统一由宿主（GraphWorkspace）收口：未保存结构拦截弹窗在画布内渲染，
  // 左上角本体图标与本按钮共用同一个 requestLeave，保证两边点击效果一致。
  const handleBack = () => {
    if (onBack) {
      onBack();
      return;
    }
    navigate(-1);
  };

  const tools = [
    {
      icon: CubeIcon,
      label: '对象实体',
      onClick: () => openPanel('create', 'objectType'),
      color: 'text-[var(--color-accent-object)]',
      bgColor: 'bg-indigo-500/10 hover:bg-indigo-500/20',
    },
    {
      icon: LinkIcon,
      label: '实体关系',
      onClick: () => openPanel('create', 'linkType'),
      color: 'text-[var(--color-accent-link)]',
      bgColor: 'bg-cyan-500/10 hover:bg-cyan-500/20',
    },
    {
      icon: BoltIcon,
      label: '执行动作',
      onClick: () => openPanel('create', 'action'),
      color: 'text-[var(--color-accent-action)]',
      bgColor: 'bg-yellow-500/10 hover:bg-yellow-500/20',
    },
    {
      icon: CodeBracketIcon,
      label: '激活函数',
      onClick: () => openPanel('create', 'function'),
      color: 'text-[var(--color-accent-fn)]',
      bgColor: 'bg-pink-500/10 hover:bg-pink-500/20',
    },
  ];

  return (
    <>
      <div className="fixed left-6 top-1/2 -translate-y-1/2 z-50">
        <div className="glass space-y-1 rounded-2xl border border-surface-700 p-1.5 shadow-2xl">
          {/* Logo */}
          <div className="mb-1 flex h-11 w-11 items-center justify-center">
            <SparklesIcon className="w-7 h-7 text-onto-400" />
          </div>

          <div className="h-px bg-surface-700 mx-1" />

          {/* Tools */}
          {tools.map((tool, index) => (
            <button
              key={index}
              onClick={tool.onClick}
              disabled={!capabilities.canEditSchema}
              className={`
                w-11 h-11 flex items-center justify-center rounded-xl
                ${tool.bgColor} ${tool.color}
                transition-all duration-200 group relative
                disabled:cursor-not-allowed disabled:bg-surface-800/40 disabled:text-surface-600 disabled:opacity-70
              `}
              title={capabilities.canEditSchema ? tool.label : `${tool.label}：${schemaDisabledReason}`}
              aria-label={tool.label}
            >
              <tool.icon className="w-6 h-6" />
              <span className="absolute left-full ml-3 px-2 py-1 bg-surface-800 text-surface-200 text-sm rounded-lg opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap pointer-events-none">
                {capabilities.canEditSchema ? tool.label : `${tool.label} · ${schemaDisabledReason}`}
              </span>
            </button>
          ))}

          <div className="h-px bg-surface-700 mx-1" />

          {/* Undo / Redo */}
          <button
            onClick={undo}
            disabled={!capabilities.canEditSchema || !canUndo}
            className="w-11 h-11 flex items-center justify-center rounded-xl bg-surface-800/50 hover:bg-surface-700 text-surface-400 hover:text-surface-200 disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-200 group relative"
            title={capabilities.canEditSchema ? '撤销 (Ctrl+Z)' : `撤销：${schemaDisabledReason}`}
            aria-label="撤销"
          >
            <ArrowUturnLeftIcon className="w-5 h-5" />
            <span className="absolute left-full ml-3 px-2 py-1 bg-surface-800 text-surface-200 text-sm rounded-lg opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap pointer-events-none">
              撤销 Ctrl+Z
            </span>
          </button>
          <button
            onClick={redo}
            disabled={!capabilities.canEditSchema || !canRedo}
            className="w-11 h-11 flex items-center justify-center rounded-xl bg-surface-800/50 hover:bg-surface-700 text-surface-400 hover:text-surface-200 disabled:opacity-30 disabled:cursor-not-allowed transition-all duration-200 group relative"
            title={capabilities.canEditSchema ? '重做 (Ctrl+Shift+Z)' : `重做：${schemaDisabledReason}`}
            aria-label="重做"
          >
            <ArrowUturnRightIcon className="w-5 h-5" />
            <span className="absolute left-full ml-3 px-2 py-1 bg-surface-800 text-surface-200 text-sm rounded-lg opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap pointer-events-none">
              重做 Ctrl+Shift+Z
            </span>
          </button>

          <div className="h-px bg-surface-700 mx-1" />

          <button
            onClick={onOpenSearch}
            className="w-11 h-11 flex items-center justify-center rounded-xl bg-surface-800/50 hover:bg-surface-700 text-surface-400 hover:text-surface-200 transition-all duration-200 group relative focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-onto-400"
            title="搜索定义 (Ctrl+K)"
            aria-label="搜索定义"
          >
            <MagnifyingGlassIcon className="w-5 h-5" />
            <span className="absolute left-full ml-3 px-2 py-1 bg-surface-800 text-surface-200 text-sm rounded-lg opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap pointer-events-none">
              搜索定义 Ctrl+K
            </span>
          </button>

          <div className="h-px bg-surface-700 mx-1" />

          {/* Auto Layout */}
          <div className="relative">
            <button
              onClick={() => setShowLayoutMenu(!showLayoutMenu)}
              className="w-11 h-11 flex items-center justify-center rounded-xl bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-400 transition-all duration-200 group relative"
              title="自动布局"
            >
              <ArrowsPointingOutIcon className="w-5 h-5" />
              <span className="absolute left-full ml-3 px-2 py-1 bg-surface-800 text-surface-200 text-sm rounded-lg opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap pointer-events-none">
                自动布局
              </span>
            </button>

            {/* Layout Menu */}
            {showLayoutMenu && (
              <div className="absolute left-full ml-3 top-1/2 -translate-y-1/2 bg-surface-800 border border-surface-600 rounded-xl shadow-2xl p-4 min-w-[280px] z-50 animate-fade-in">
                <div className="flex items-center justify-between mb-3">
                  <h3 className="text-sm font-semibold text-surface-100">自动布局</h3>
                  <button
                    onClick={() => setShowLayoutMenu(false)}
                    className="text-surface-400 hover:text-surface-100"
                  >
                    ×
                  </button>
                </div>

                {/* Algorithm Selection */}
                <div className="mb-4">
                  <label className="text-xs text-surface-400 mb-2 block">布局算法</label>
                  <div className="space-y-1">
                    {LAYOUT_ALGORITHMS.map((algo) => (
                      <button
                        key={algo.id}
                        onClick={() => setSelectedAlgorithm(algo.id)}
                        className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left transition-colors
                          ${selectedAlgorithm === algo.id
                            ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                            : 'bg-surface-700/50 text-surface-300 hover:bg-surface-700 border border-transparent'}`}
                      >
                        <span className="text-lg">{algo.icon}</span>
                        <div className="flex-1">
                          <div className="text-sm font-medium">{algo.name}</div>
                          <div className="text-xs text-surface-500">{algo.description}</div>
                        </div>
                        {selectedAlgorithm === algo.id && (
                          <ChevronRightIcon className="w-4 h-4 text-emerald-400" />
                        )}
                      </button>
                    ))}
                  </div>
                </div>

                {/* Direction (only for dagre) */}
                {selectedAlgorithm === 'dagre' && (
                  <div className="mb-4">
                    <label className="text-xs text-surface-400 mb-2 block">布局方向</label>
                    <div className="flex gap-2">
                      {LAYOUT_DIRECTIONS.map((dir) => (
                        <button
                          key={dir.id}
                          onClick={() => setSelectedDirection(dir.id)}
                          className={`flex-1 flex flex-col items-center gap-1 px-2 py-2 rounded-lg transition-colors
                            ${selectedDirection === dir.id
                              ? 'bg-emerald-500/20 text-emerald-400 border border-emerald-500/30'
                              : 'bg-surface-700/50 text-surface-400 hover:bg-surface-700 border border-transparent'}`}
                          title={dir.name}
                        >
                          <span className="text-lg">{dir.icon}</span>
                          <span className="text-xs">{dir.name}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}

                {/* Apply Button */}
                <button
                  onClick={handleAutoLayout}
                  className="w-full py-2.5 bg-emerald-600 hover:bg-emerald-500 text-white rounded-lg font-medium transition-colors flex items-center justify-center gap-2"
                >
                  <ArrowsPointingOutIcon className="w-4 h-4" />
                  应用布局
                </button>
              </div>
            )}
          </div>

          <div className="h-px bg-surface-700 mx-1" />

          {/* Actions */}
          <button
            onClick={() => void handleExport()}
            disabled={isExporting}
            aria-busy={isExporting}
            className="w-11 h-11 flex items-center justify-center rounded-xl bg-surface-800/50 hover:bg-surface-700 text-surface-400 hover:text-surface-200 disabled:cursor-wait disabled:opacity-60 transition-all duration-200 group relative"
            title={isExporting ? '正在导出' : '导出'}
            aria-label={isExporting ? '正在导出' : '导出'}
          >
            <ArrowDownTrayIcon className="w-5 h-5" />
            <span className="absolute left-full ml-3 px-2 py-1 bg-surface-800 text-surface-200 text-sm rounded-lg opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap pointer-events-none">
              {isExporting ? '正在导出' : '导出'}
            </span>
          </button>

          <button
            onClick={() => {
              setImportError('');
              setShowImportModal(true);
            }}
            disabled={!capabilities.canImport}
            className="w-11 h-11 flex items-center justify-center rounded-xl bg-surface-800/50 hover:bg-surface-700 text-surface-400 hover:text-surface-200 disabled:cursor-not-allowed disabled:opacity-35 transition-all duration-200 group relative"
            title={capabilities.canImport ? '导入' : `导入：${schemaDisabledReason}`}
            aria-label="导入"
          >
            <ArrowUpTrayIcon className="w-5 h-5" />
            <span className="absolute left-full ml-3 px-2 py-1 bg-surface-800 text-surface-200 text-sm rounded-lg opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap pointer-events-none">
              {capabilities.canImport ? '导入' : `导入 · ${schemaDisabledReason}`}
            </span>
          </button>

          <div className="h-px bg-surface-700 mx-1" />

          <button
            onClick={handleReset}
            className="w-11 h-11 flex items-center justify-center rounded-xl bg-red-500/10 hover:bg-red-500/20 text-red-400 transition-all duration-200 group relative"
            title={backendId ? '重新加载' : '重置为演示数据'}
          >
            <ArrowPathIcon className="w-5 h-5" />
            <span className="absolute left-full ml-3 px-2 py-1 bg-surface-800 text-surface-200 text-sm rounded-lg opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap pointer-events-none">
              {backendId ? '重新加载' : '重置为演示数据'}
            </span>
          </button>

          {showBack && (
            <button
              onClick={handleBack}
              data-testid="graph-workspace-back"
              aria-label="返回"
              className="w-11 h-11 flex items-center justify-center rounded-xl bg-surface-700/50 hover:bg-surface-600/50 text-surface-400 hover:text-surface-200 transition-all duration-200 group relative"
              title="返回"
            >
              <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
              <span className="absolute left-full ml-3 px-2 py-1 bg-surface-800 text-surface-200 text-sm rounded-lg opacity-0 group-hover:opacity-100 transition-opacity whitespace-nowrap pointer-events-none">
                返回
              </span>
            </button>
          )}
        </div>
      </div>

      {/* Import Modal */}
      {showImportModal && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-black/50 backdrop-blur-sm">
          <div
            role="dialog"
            aria-modal="true"
            aria-label="导入本体"
            className="bg-surface-800 border border-surface-600 rounded-2xl p-6 w-full max-w-lg mx-4 animate-fade-in"
          >
            <h3 className="text-lg font-display font-semibold text-surface-100 mb-4">导入本体</h3>
            <textarea
              value={importData}
              onChange={(e) => {
                setImportData(e.target.value);
                setImportError('');
              }}
              className="input-field h-64 font-mono text-sm resize-none"
              placeholder="粘贴画布本地备份 JSON..."
            />
            {importError && (
              <p
                role="alert"
                className="mt-3 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm leading-relaxed text-red-300"
              >
                {importError}
              </p>
            )}
            <div className="flex justify-end gap-3 mt-4">
              <button
                onClick={closeImportModal}
                className="btn-secondary"
              >
                取消
              </button>
              <button onClick={handleImport} className="btn-primary">
                导入
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
