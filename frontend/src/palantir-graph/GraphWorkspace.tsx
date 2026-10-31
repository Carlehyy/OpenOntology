import { useState, lazy, Suspense, useEffect, useMemo, useRef } from 'react';
import { ReactFlowProvider } from '@xyflow/react';
import { useNavigate } from 'react-router-dom';
import { ontologyVersionApi } from '@/api/v2/ontology-versions';

import './palantir-graph.css';

import Canvas from './components/Canvas';
import Header, { DefinitionStatBadges } from './components/Header';
import { useOntologyStore, attachAutoSave, attachHistory } from './store/ontologyStore';
import Toolbar from './components/Toolbar';
import Panel from './components/Panel';
import SearchPalette from './components/SearchPalette';
import DeleteSelectedDialog, { type DeleteTarget } from './components/DeleteSelectedDialog';
import ActionList from './components/panels/ActionList';
import FunctionList from './components/panels/FunctionList';
import LinkList from './components/panels/LinkList';
import ObjectList from './components/panels/ObjectList';
import SentinelPanel from './components/panels/SentinelPanel';
import { FloatingMenu } from './components/FloatingMenu';
import { getGraphWorkspaceCapabilities } from './workspaceCapabilities';

const HelpGuide = lazy(() => import('./components/panels/HelpGuide'));
const GraphDatabaseView = lazy(() => import('./components/panels/GraphDatabaseView'));
const FunctionTester = lazy(() => import('./components/panels/FunctionTester'));
const ActionRunner = lazy(() => import('./components/panels/ActionRunner'));
import InstanceBrowser from './components/panels/InstanceBrowser';
const RunHistoryPanel = lazy(() => import('./components/panels/RunHistoryPanel'));
const AutonomyPanel = lazy(() => import('./components/panels/AutonomyPanel'));

function PanelLoader() {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm">
      <div className="flex flex-col items-center gap-4">
        <div className="w-10 h-10 border-4 border-violet-500 border-t-transparent rounded-full animate-spin" />
        <span className="text-gray-400 text-sm">加载中...</span>
      </div>
    </div>
  );
}

export interface GraphWorkspaceProps {
  ontologyId: string;
  versionId: string | null;
  /** 视觉主题：全屏路由页用深色（默认）；嵌入浅色宿主（如业务澄清页）时传 light */
  theme?: 'dark' | 'light';
  /** fullpage=fixed 整页接管；embedded=填满父容器（fixed 面板经 transform 收编进容器） */
  layout?: 'fullpage' | 'embedded';
  /** 是否渲染编辑器自带 Header（嵌入浅色工作台时关闭，四类定义统计徽章转为
   *  画布顶部浮动条；保存/校验入口与版本状态条只在全屏编辑器提供） */
  showHeader?: boolean;
  /** Header「数据映射」入口；不传则隐藏该按钮 */
  onOpenMapping?: () => void;
  /** 全屏版本状态条的「返回版本演进」；嵌入工作台不渲染状态条，返回入口由宿主页承载 */
  onBackToVersions?: () => void;
  /** runtime 态「基于此版本开始修改」创建草稿成功后的回调；缺省跳转全屏编辑器路由 */
  onDraftCreated?: (versionId: string) => void;
  /** 本工作区版本保存落库（syncStatus → 'saved'）后的回调；每次保存触发一次。
   *  仅当 store 的 workspaceVersionId 与本组件 versionId 一致时触发，避免与
   *  共享 store 上的其他工作区（如全屏编辑器）串扰。 */
  onSaved?: () => void;
}

/**
 * 可嵌入的本体图谱工作区：编辑器组装、数据加载、快捷键、自动保存等全部
 * 收敛在此组件内，路由页（OntologyGraphPage）与嵌入宿主（业务澄清页）共享。
 */
export default function GraphWorkspace({
  ontologyId,
  versionId,
  theme = 'dark',
  layout = 'fullpage',
  showHeader = true,
  onOpenMapping,
  onBackToVersions,
  onDraftCreated,
  onSaved,
}: GraphWorkspaceProps) {
  const embedded = layout === 'embedded';
  const navigate = useNavigate();
  const rootRef = useRef<HTMLDivElement>(null);

  const [showHelp, setShowHelp] = useState(false);
  const [showGraphDB, setShowGraphDB] = useState(false);
  const [showFunctionTester, setShowFunctionTester] = useState(false);
  const [testFunctionId, setTestFunctionId] = useState<string>('');
  const [showActionRunner, setShowActionRunner] = useState(false);
  const [runActionId, setRunActionId] = useState<string>('');
  const [runInstanceId, setRunInstanceId] = useState<string>('');
  const [showInstanceBrowser, setShowInstanceBrowser] = useState(false);
  const [instanceBrowserTypeId, setInstanceBrowserTypeId] = useState<string>('');
  const [showActionPanel, setShowActionPanel] = useState(false);
  const [showFunctionPanel, setShowFunctionPanel] = useState(false);
  const [showLinkPanel, setShowLinkPanel] = useState(false);
  const [showObjectPanel, setShowObjectPanel] = useState(false);
  const [showSentinel, setShowSentinel] = useState(false);
  const [showSearch, setShowSearch] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);
  const [showRunHistory, setShowRunHistory] = useState(false);
  const [showAutonomy, setShowAutonomy] = useState(false);

  const loadFromBackend = useOntologyStore((s) => s.loadFromBackend);
  const syncStatus = useOntologyStore((s) => s.syncStatus);
  const isDirty = useOntologyStore((s) => s.isDirty);
  const workspaceMode = useOntologyStore((s) => s.workspaceMode);
  const ontology = useOntologyStore((s) => s.ontology);
  const capabilities = useMemo(() => getGraphWorkspaceCapabilities(workspaceMode), [workspaceMode]);
  const schemaReadOnly = !capabilities.canEditSchema;
  const [creatingDraft, setCreatingDraft] = useState(false);
  const [draftError, setDraftError] = useState('');

  // 有未保存改动时，拦截关闭/刷新页面（浏览器原生确认框）
  useEffect(() => {
    if (schemaReadOnly || !isDirty) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = '';
    };
    window.addEventListener('beforeunload', handler);
    return () => window.removeEventListener('beforeunload', handler);
  }, [isDirty, schemaReadOnly]);

  // 加载后端本体后再绑定脏标记监听，避免把后端加载结果误判为用户编辑
  useEffect(() => {
    if (!ontologyId) return;
    let off: (() => void) | undefined;
    let cancelled = false;

    void loadFromBackend(ontologyId, versionId).then(() => {
      if (cancelled) return;
      if (useOntologyStore.getState().workspaceMode === 'draft') {
        off = attachAutoSave();
      }
    });

    return () => {
      cancelled = true;
      off?.();
    };
  }, [ontologyId, versionId, loadFromBackend]);

  // 撤销/重做历史记录（与后端加载无关，进入编辑器即绑定）
  useEffect(() => attachHistory(), []);

  // 保存成功（syncStatus 边沿进入 'saved'）通知宿主，用于失效语义一致性等派生查询。
  // ontology store 是模块级单例：仅在保存归属本工作区版本时触发，防止跨工作区串扰。
  const onSavedRef = useRef(onSaved);
  onSavedRef.current = onSaved;
  useEffect(() => {
    if (!versionId) return;
    return useOntologyStore.subscribe((state, prev) => {
      if (
        state.syncStatus === 'saved'
        && prev.syncStatus !== 'saved'
        && state.workspaceVersionId === versionId
      ) {
        onSavedRef.current?.();
      }
    });
  }, [versionId]);

  // 快捷键：Ctrl+K 搜索 / Ctrl+Z 撤销 / Ctrl+Shift+Z·Ctrl+Y 重做 / Ctrl+S 保存
  // Delete 删除选中（带影响预览）/ Esc 关闭右侧面板。
  // 嵌入模式下只在事件源自工作区内部时响应，避免劫持宿主页（如澄清页对话框输入）。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (embedded && !rootRef.current?.contains(e.target as Node | null)) return;
      const el = e.target as HTMLElement | null;
      const inEditable = !!el && (
        el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT' || el.isContentEditable
      );
      const mod = e.metaKey || e.ctrlKey;

      if (mod && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setShowSearch((v) => !v);
        return;
      }
      if (mod && e.key.toLowerCase() === 's' && capabilities.canEditSchema) {
        e.preventDefault();
        void useOntologyStore.getState().saveToBackend();
        return;
      }
      if (inEditable) return;

      if (capabilities.canEditSchema && mod && !e.shiftKey && e.key.toLowerCase() === 'z') {
        e.preventDefault();
        useOntologyStore.getState().undo();
        return;
      }
      if (capabilities.canEditSchema && ((mod && e.shiftKey && e.key.toLowerCase() === 'z') || (mod && e.key.toLowerCase() === 'y'))) {
        e.preventDefault();
        useOntologyStore.getState().redo();
        return;
      }
      if (capabilities.canEditSchema && (e.key === 'Delete' || e.key === 'Backspace')) {
        const s = useOntologyStore.getState();
        if (s.selectedNodeId) {
          e.preventDefault();
          setDeleteTarget({ kind: 'objectType', id: s.selectedNodeId });
        } else if (s.selectedEdgeId) {
          e.preventDefault();
          setDeleteTarget({ kind: 'linkType', id: s.selectedEdgeId });
        }
        return;
      }
      if (e.key === 'Escape') {
        const s = useOntologyStore.getState();
        if (s.isPanelOpen) s.closePanel();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [capabilities.canEditSchema, embedded]);

  // 全屏模式：为 body 添加作用域类并锁滚动（历史行为）。
  // 嵌入模式：作用域类直接挂在工作区根节点上，不触碰 body。
  useEffect(() => {
    if (embedded) return;
    const originalClass = document.body.className;
    const originalOverflow = document.body.style.overflow;
    document.body.classList.add('palantir-graph-root');
    document.body.style.overflow = 'hidden';
    return () => {
      document.body.className = originalClass;
      document.body.style.overflow = originalOverflow;
    };
  }, [embedded]);

  const openFunctionTest = (fnId?: string) => {
    setTestFunctionId(fnId || '');
    setShowFunctionTester(true);
  };
  const openActionRun = (actionId?: string, instanceId?: string) => {
    setRunActionId(actionId || '');
    setRunInstanceId(instanceId || '');
    setShowActionRunner(true);
  };

  const beginCurrentReleaseEdit = async () => {
    if (!ontologyId || creatingDraft) return;
    setDraftError('');
    setCreatingDraft(true);
    try {
      const tree = await ontologyVersionApi.tree(ontologyId);
      const draft = await ontologyVersionApi.createDraft(
        ontologyId,
        tree.current_release_id,
        {
          versionLabel: '模型修改',
          description: `基于当前发布版 ${tree.current_release_version} 创建`,
        },
      );
      if (onDraftCreated) {
        onDraftCreated(draft.id);
      } else {
        navigate(`/ontologies/${ontologyId}/graph?versionId=${draft.id}`);
      }
    } catch (error: any) {
      const detail = error?.response?.data?.detail ?? error?.detail;
      setDraftError(typeof detail === 'string' ? detail : detail?.message || error?.message || '创建修改分支失败');
    } finally {
      setCreatingDraft(false);
    }
  };

  const stage = workspaceMode === 'draft'
    ? { tone: 'sky', text: `草稿 ${ontology?.version || ''} · 可编辑并查看全部模型定义，不产生 Fact、不执行本体网络` }
    : workspaceMode === 'trial'
      ? { tone: 'amber', text: `试跑态 ${ontology?.version || ''} · 可只读查看本次隔离实例，动作与外部副作用保持禁用` }
      : workspaceMode === 'release'
        ? { tone: 'slate', text: `历史发布 ${ontology?.version || ''} · 可查看定义并保存画布布局，不承载当前运行数据` }
        : workspaceMode === 'archived'
          ? { tone: 'slate', text: `已归档分支 ${ontology?.version || ''} · 可查看定义并保存画布布局` }
          : { tone: 'emerald', text: `当前发布 ${ontology?.version || ''} · 可查看定义并保存画布布局，正式数据与本体网络持续运行` };

  const stageClass = theme === 'light'
    ? {
        sky: 'border-sky-200 bg-sky-50/95 text-sky-700',
        amber: 'border-amber-200 bg-amber-50/95 text-amber-700',
        slate: 'border-slate-200 bg-white/95 text-slate-600',
        emerald: 'border-emerald-200 bg-emerald-50/95 text-emerald-700',
      }[stage.tone]
    : {
        sky: 'border-sky-500/40 bg-sky-950/90 text-sky-200',
        amber: 'border-amber-500/40 bg-amber-950/90 text-amber-200',
        slate: 'border-slate-500/40 bg-slate-950/90 text-slate-200',
        emerald: 'border-emerald-500/40 bg-emerald-950/90 text-emerald-200',
      }[stage.tone];

  return (
    <ReactFlowProvider>
      <div
        ref={rootRef}
        className={
          embedded
            ? `palantir-graph-root${theme === 'light' ? ' pg-light' : ''} relative h-full w-full overflow-hidden bg-surface-950 [transform:translateZ(0)]`
            : 'fixed inset-0 z-[9999] h-screen w-screen overflow-hidden bg-surface-950'
        }
      >
        {/* 版本状态条：全屏编辑器专属。嵌入工作台不放（返回入口由宿主页承载） */}
        {!embedded && (
          <div
            data-testid="graph-workspace-stage"
            className={`fixed bottom-6 left-1/2 z-50 flex max-w-[calc(100vw-2rem)] -translate-x-1/2 items-center gap-2 rounded-lg border px-3 py-1.5 text-xs shadow-lg backdrop-blur ${stageClass}`}
          >
            <span className="truncate">{stage.text}</span>
            {workspaceMode === 'runtime' ? (
              <button
                className="shrink-0 font-medium underline disabled:cursor-wait disabled:opacity-60"
                disabled={creatingDraft}
                onClick={() => void beginCurrentReleaseEdit()}
              >
                {creatingDraft ? '正在创建草稿' : '基于此版本开始修改'}
              </button>
            ) : (
              onBackToVersions && (
                <button className="shrink-0 font-medium underline" onClick={onBackToVersions}>返回版本演进</button>
              )
            )}
          </div>
        )}
        {draftError && (
          <div role="alert" className="fixed left-1/2 top-[6.8rem] z-50 -translate-x-1/2 rounded-lg border border-red-500/40 bg-red-950/95 px-3 py-2 text-xs text-red-200 shadow-lg">
            {draftError}
          </div>
        )}
        {/* 加载本体时的轻量提示（保存状态已整合到 Header 的保存按钮） */}
        {syncStatus === 'loading' && (
          <div className="fixed top-3 left-1/2 -translate-x-1/2 z-50">
            <span className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-surface-800/80 backdrop-blur border border-surface-700 text-xs text-surface-300">
              <span className="w-3 h-3 border-2 border-violet-500 border-t-transparent rounded-full animate-spin" />
              加载本体…
            </span>
          </div>
        )}

        {showHeader ? (
          <Header
            readOnly={schemaReadOnly}
            stageLabel={workspaceMode === 'runtime' ? '当前发布' : workspaceMode === 'draft' ? '草稿编辑' : workspaceMode === 'trial' ? '试跑快照' : '历史快照'}
            onToggleActions={() => setShowActionPanel(v => !v)}
            onToggleFunctions={() => setShowFunctionPanel(v => !v)}
            showActions={showActionPanel}
            showFunctions={showFunctionPanel}
            onToggleLinks={() => setShowLinkPanel(v => !v)}
            showLinks={showLinkPanel}
            onToggleObjects={() => setShowObjectPanel(v => !v)}
            showObjects={showObjectPanel}
            onOpenMapping={onOpenMapping}
          />
        ) : (
          /* 无 Header 时（嵌入浅色工作台）：四类定义统计徽章浮动在画布顶部中间
             （与全屏编辑器 Header 中部同源）。保存/校验入口不进嵌入画布：
             草稿自动保存，人工保存与冲突处置走全屏编辑器。 */
          ontology && (
            <DefinitionStatBadges
              className="fixed left-1/2 top-3 z-40 max-w-[calc(100%-2rem)] -translate-x-1/2 overflow-x-auto"
              showObjects={showObjectPanel}
              showLinks={showLinkPanel}
              showActions={showActionPanel}
              showFunctions={showFunctionPanel}
              onToggleObjects={() => setShowObjectPanel(v => !v)}
              onToggleLinks={() => setShowLinkPanel(v => !v)}
              onToggleActions={() => setShowActionPanel(v => !v)}
              onToggleFunctions={() => setShowFunctionPanel(v => !v)}
            />
          )
        )}
        <main className={showHeader ? 'h-full pt-16' : 'h-full'}>
          <Canvas
            schemaReadOnly={schemaReadOnly}
            layoutScope={`${ontologyId || 'ontology'}:${versionId || 'runtime'}`}
            onBrowseInstances={(objectTypeId) => {
              setInstanceBrowserTypeId(objectTypeId);
              setShowInstanceBrowser(true);
            }}
          />
        </main>
        <Toolbar capabilities={capabilities} onOpenSearch={() => setShowSearch(true)} showBack={!embedded} />
        <Panel readOnly={schemaReadOnly} />
        <ActionList
          readOnly={schemaReadOnly}
          onRunAction={openActionRun}
          isOpen={showActionPanel}
          onClose={() => setShowActionPanel(false)}
        />
        <FunctionList
          readOnly={schemaReadOnly}
          onTestFunction={openFunctionTest}
          isOpen={showFunctionPanel}
          onClose={() => setShowFunctionPanel(false)}
        />
        <LinkList readOnly={schemaReadOnly} isOpen={showLinkPanel} onClose={() => setShowLinkPanel(false)} />
        <ObjectList readOnly={schemaReadOnly} isOpen={showObjectPanel} onClose={() => setShowObjectPanel(false)} />
        <SentinelPanel isOpen={showSentinel} onClose={() => setShowSentinel(false)} />

        {showSearch && <SearchPalette onClose={() => setShowSearch(false)} />}
        {capabilities.canEditSchema && deleteTarget && (
          <DeleteSelectedDialog target={deleteTarget} onClose={() => setDeleteTarget(null)} />
        )}

        <FloatingMenu
          capabilities={capabilities}
          onOpenHelp={() => setShowHelp(true)}
          onOpenFunctionTest={() => openFunctionTest()}
          onOpenActionRun={() => openActionRun()}
          onOpenSentinel={() => setShowSentinel(true)}
          onOpenInstances={() => {
            setInstanceBrowserTypeId('');
            setShowInstanceBrowser(true);
          }}
          onOpenRunHistory={() => setShowRunHistory(true)}
          onOpenAutonomy={() => setShowAutonomy(true)}
          onOpenGraphDatabase={() => setShowGraphDB(true)}
        />

        <Suspense fallback={<PanelLoader />}>
          {showHelp && <HelpGuide isOpen={showHelp} onClose={() => setShowHelp(false)} />}
          {showGraphDB && <GraphDatabaseView isOpen={showGraphDB} onClose={() => setShowGraphDB(false)} />}
          {showFunctionTester && (
            <FunctionTester
              isOpen={showFunctionTester}
              initialFunctionId={testFunctionId}
              onClose={() => setShowFunctionTester(false)}
            />
          )}
          {showActionRunner && (
            <ActionRunner
              isOpen={showActionRunner}
              initialActionId={runActionId}
              initialInstanceId={runInstanceId}
              onClose={() => setShowActionRunner(false)}
            />
          )}
          {showInstanceBrowser && (
            <InstanceBrowser
              isOpen={showInstanceBrowser}
              initialObjectTypeId={instanceBrowserTypeId || undefined}
              onClose={() => setShowInstanceBrowser(false)}
              onRunAction={openActionRun}
            />
          )}
          {showRunHistory && <RunHistoryPanel isOpen={showRunHistory} onClose={() => setShowRunHistory(false)} />}
          {showAutonomy && <AutonomyPanel isOpen={showAutonomy} onClose={() => setShowAutonomy(false)} />}
        </Suspense>
      </div>
    </ReactFlowProvider>
  );
}
