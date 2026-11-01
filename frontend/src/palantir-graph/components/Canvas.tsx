import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  ReactFlow,
  Background,
  MiniMap,
  useNodesState,
  useEdgesState,
  useReactFlow,
  type Connection,
  type Edge,
  type Node,
  BackgroundVariant,
  ConnectionMode,
  MarkerType,
  type EdgeChange,
  type NodeChange,
} from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import {
  PencilSquareIcon, TrashIcon, TableCellsIcon, CubeIcon, ArrowsPointingOutIcon,
} from '@heroicons/react/24/outline';

import { useOntologyStore } from '../store/ontologyStore';
import { saveCanvasLayout, saveCanvasLayoutKeepAlive } from '../api/formalApi';
import { routeEdgeHandles } from '../utils/routeEdgeHandles';
import ObjectTypeNode from './nodes/ObjectTypeNode';
import MultiConnectionEdge from './edges/MultiConnectionEdge';
import ConnectLinkDialog from './ConnectLinkDialog';
import DeleteSelectedDialog, { type DeleteTarget } from './DeleteSelectedDialog';

const nodeTypes = {
  objectType: ObjectTypeNode,
};

const edgeTypes = {
  multi: MultiConnectionEdge,
};

const defaultEdgeOptions = {
  type: 'multi',
  animated: true,
  style: { stroke: 'var(--color-link)', strokeWidth: 2 },
  markerEnd: {
    type: MarkerType.ArrowClosed,
    color: 'var(--color-link)',
  },
};

interface CanvasProps {
  /** 右键菜单"浏览实例"入口（由页面渲染 InstanceBrowser） */
  onBrowseInstances?: (objectTypeId: string) => void;
  /** 仅控制模型结构写入；节点布局在所有本体状态下都可编辑 */
  schemaReadOnly?: boolean;
  /** 切换版本时重载该版本的布局，避免不同快照之间串位 */
  layoutScope?: string;
}

/** 相邻平行边顶点之间的间距(px) */
const PARALLEL_SPACING = 42;

/**
 * 同一对节点间的多条边错开偏移，避免平行边完全重叠。
 *
 * 交给自定义边 `multi`（MultiConnectionEdge）按 `data.__offset` 弯出不同弧度：
 * - 无向分组：A→B 与 B→A 归为同组一起散开；
 * - 偏移量以 0 为中心对称展开（1 条→0；2 条→±½;3 条→-1/0/1 …）；
 * - 反向边(source 非规范首节点)法线方向相反，翻转符号保证同组分列两侧不叠。
 */
function offsetParallelEdges(edges: Edge[]): Edge[] {
  const groups = new Map<string, Edge[]>();
  for (const e of edges) {
    const key = [e.source, e.target].sort().join('::');
    const arr = groups.get(key) || [];
    arr.push(e);
    groups.set(key, arr);
  }
  const out: Edge[] = [];
  for (const [key, arr] of groups.entries()) {
    const canonicalFirst = key.split('::')[0]; // = min(source, target)
    const n = arr.length;
    arr.forEach((e, i) => {
      const signed = (i - (n - 1) / 2) * PARALLEL_SPACING;
      const flip = e.source !== canonicalFirst;
      const offset = flip ? -signed : signed;
      out.push({
        ...e,
        type: 'multi',
        data: { ...(e.data as Record<string, unknown>), __offset: offset },
      } as Edge);
    });
  }
  return out;
}

export default function Canvas({ onBrowseInstances, schemaReadOnly = false, layoutScope = 'default' }: CanvasProps = {}) {
  const {
    ontology,
    nodes: storeNodes,
    edges: storeEdges,
    selectedNodeId,
    selectedEdgeId,
    setSelectedNode,
    setSelectedEdge,
    updateNodePosition,
    openPanel,
    setPendingNodePosition,
    autoLayout,
    backendId,
    workspaceVersionId,
    syncStatus,
  } = useOntologyStore();
  const { screenToFlowPosition, fitView } = useReactFlow();

  const selectedStoreNodes = useMemo<Node[]>(
    () => (storeNodes as unknown as Node[]).map((node): Node => ({
      ...node,
      selected: node.id === selectedNodeId,
    })),
    [storeNodes, selectedNodeId]
  );
  const sourceStoreEdges = useMemo<Edge[]>(() => {
    const direct = storeEdges as Edge[];
    if (direct.length > 0 || !ontology) return direct;
    return ontology.linkTypes.map((lt): Edge => ({
      id: lt.id,
      source: lt.sourceObjectTypeId,
      target: lt.targetObjectTypeId,
      type: 'multi',
      data: lt as unknown as Record<string, unknown>,
      label: lt.displayName,
    }));
  }, [ontology, storeEdges]);
  const selectedStoreEdges = useMemo<Edge[]>(
    () => offsetParallelEdges(sourceStoreEdges.map((edge): Edge => ({
      ...edge,
      selected: edge.id === selectedEdgeId,
    }))),
    [sourceStoreEdges, selectedEdgeId]
  );
  const topologyKey = useMemo(() => {
    const nodeIds = storeNodes.map((node) => node.id).sort().join(',');
    const edgeIds = sourceStoreEdges.map((edge) => edge.id).sort().join(',');
    return `${layoutScope}|${nodeIds}|${edgeIds}`;
  }, [layoutScope, sourceStoreEdges, storeNodes]);

  const [nodes, setNodes, onNodesChange] = useNodesState<Node>(selectedStoreNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>(selectedStoreEdges);
  const [layoutSaveStatus, setLayoutSaveStatus] = useState<'idle' | 'saving' | 'saved' | 'error'>('idle');
  const [layoutSaveError, setLayoutSaveError] = useState('');
  const [layoutRetryNonce, setLayoutRetryNonce] = useState(0);
  const layoutSaveTimer = useRef<number | null>(null);
  const layoutSavedResetTimer = useRef<number | null>(null);
  const layoutSaveQueue = useRef<Promise<void>>(Promise.resolve());
  const initializedLayoutScope = useRef<string | null>(null);
  const wasLoading = useRef(false);
  const previousSyncStatus = useRef(syncStatus);
  const persistedNodeIds = useRef<Set<string>>(new Set());
  const lastPersistedLayout = useRef('');
  const latestLayout = useRef('');
  const currentLayoutScope = useRef(layoutScope);

  /** 已排期但尚未确认落库的最新布局批次：离开画布（卸载/页面隐藏/切版本）时补发，
   *  避免 650ms 防抖窗口内返回/刷新丢掉最后一次拖拽的节点位置。 */
  const pendingLayoutSave = useRef<{
    backendId: string;
    versionId: string | null;
    positions: Record<string, { x: number; y: number }>;
  } | null>(null);

  const flushPendingLayoutSave = useCallback(() => {
    const pending = pendingLayoutSave.current;
    if (!pending) return;
    pendingLayoutSave.current = null;
    saveCanvasLayoutKeepAlive(pending.backendId, pending.positions, pending.versionId);
  }, []);

  const renderNodes = useMemo(
    () => schemaReadOnly
      ? nodes.map((node) => ({
          ...node,
          width: node.width ?? 280,
          height: node.height ?? 140,
          measured: node.measured ?? { width: node.width ?? 280, height: node.height ?? 140 },
          style: { ...(node.style || {}), visibility: 'visible' as const },
          data: { ...(node.data as Record<string, unknown>), __readOnly: true },
        }))
      : nodes,
    [nodes, schemaReadOnly]
  );
  // 渲染期按节点实时相对位置重选边锚点：拖拽时连线端点自动换到更合适的侧面。
  // 纯派生不写 store，自环保持「右出左进」配合 MultiConnectionEdge 的自环画法。
  const renderEdges = useMemo(
    () => routeEdgeHandles(schemaReadOnly ? selectedStoreEdges : edges, renderNodes),
    [schemaReadOnly, selectedStoreEdges, edges, renderNodes]
  );

  // Store 中的坐标只代表客户端视图，不再等同于模型结构变更。
  useEffect(() => {
    setNodes(selectedStoreNodes);
  }, [selectedStoreNodes, setNodes]);

  useEffect(() => {
    setEdges(selectedStoreEdges);
  }, [selectedStoreEdges, setEdges]);

  useEffect(() => {
    if (storeNodes.length === 0) return;
    const timer = window.setTimeout(() => {
      void fitView({ padding: 0.25, maxZoom: schemaReadOnly ? 0.72 : 0.8, duration: 220 });
    }, 80);
    return () => window.clearTimeout(timer);
  }, [fitView, schemaReadOnly, storeNodes.length, topologyKey]);

  useEffect(() => {
    // 切版本会重建基线：先把上一 scope 的待提交布局补发出去，避免防抖窗口内切版丢位置
    flushPendingLayoutSave();
    currentLayoutScope.current = layoutScope;
    setLayoutSaveError('');
    setLayoutSaveStatus('idle');
    initializedLayoutScope.current = null;
    if (layoutSaveTimer.current !== null) window.clearTimeout(layoutSaveTimer.current);
    if (layoutSavedResetTimer.current !== null) window.clearTimeout(layoutSavedResetTimer.current);
  }, [flushPendingLayoutSave, layoutScope]);

  useEffect(() => () => {
    if (layoutSaveTimer.current !== null) window.clearTimeout(layoutSaveTimer.current);
    if (layoutSavedResetTimer.current !== null) window.clearTimeout(layoutSavedResetTimer.current);
  }, []);

  // 布局保存兜底：路由卸载 / 页面隐藏时把待提交批次用 keepalive 立即送达。
  // 普通 fetch 在文档卸载后会被浏览器中止，keepalive 保证防抖窗口内拖拽的坐标不丢。
  useEffect(() => {
    const onPageHide = () => flushPendingLayoutSave();
    window.addEventListener('pagehide', onPageHide);
    return () => {
      window.removeEventListener('pagehide', onPageHide);
      flushPendingLayoutSave();
    };
  }, [flushPendingLayoutSave]);

  // 后端加载完成后建立已持久化节点基线；新建但尚未保存的对象类型不提前写布局接口。
  useEffect(() => {
    const enteredSaved = syncStatus === 'saved' && previousSyncStatus.current !== 'saved';
    previousSyncStatus.current = syncStatus;
    if (!enteredSaved) return;
    persistedNodeIds.current = new Set(ontology?.objectTypes.map((item) => item.id) || []);
    const positions = storeNodes
      .filter((node) => node.type === 'objectType' && persistedNodeIds.current.has(node.id))
      .map((node) => [node.id, node.position.x, node.position.y] as const)
      .sort(([left], [right]) => left.localeCompare(right));
    const signature = JSON.stringify(positions);
    lastPersistedLayout.current = signature;
    latestLayout.current = signature;
    // 结构保存会连同节点坐标一起落库，未确认的布局批次已被覆盖
    pendingLayoutSave.current = null;
  }, [ontology?.objectTypes, storeNodes, syncStatus]);

  // 所有状态共用独立布局自动保存：停止变化 650ms 后提交，串行排队避免旧请求覆盖新位置。
  useEffect(() => {
    if (syncStatus === 'loading') {
      wasLoading.current = true;
      if (layoutSaveTimer.current !== null) window.clearTimeout(layoutSaveTimer.current);
      return;
    }
    if (!backendId) return;

    if (initializedLayoutScope.current !== layoutScope || wasLoading.current) {
      initializedLayoutScope.current = layoutScope;
      wasLoading.current = false;
      persistedNodeIds.current = new Set(ontology?.objectTypes.map((item) => item.id) || []);
      const baseline = storeNodes
        .filter((node) => node.type === 'objectType' && persistedNodeIds.current.has(node.id))
        .map((node) => [node.id, node.position.x, node.position.y] as const)
        .sort(([left], [right]) => left.localeCompare(right));
      const signature = JSON.stringify(baseline);
      lastPersistedLayout.current = signature;
      latestLayout.current = signature;
      pendingLayoutSave.current = null;
      return;
    }

    const entries = storeNodes
      .filter((node) => node.type === 'objectType' && persistedNodeIds.current.has(node.id))
      .map((node) => [node.id, node.position] as const)
      .sort(([left], [right]) => left.localeCompare(right));
    const positions = Object.fromEntries(entries);
    const signature = JSON.stringify(entries.map(([id, position]) => [id, position.x, position.y]));
    latestLayout.current = signature;
    if (!entries.length || signature === lastPersistedLayout.current) {
      // 已与落库基线一致（例如拖回原位）：撤销待补发批次，避免卸载时用旧坐标覆盖
      pendingLayoutSave.current = null;
      return;
    }

    if (layoutSaveTimer.current !== null) window.clearTimeout(layoutSaveTimer.current);
    setLayoutSaveError('');
    pendingLayoutSave.current = { backendId, versionId: workspaceVersionId, positions };
    layoutSaveTimer.current = window.setTimeout(() => {
      const requestedScope = layoutScope;
      const requestedSignature = signature;
      setLayoutSaveStatus('saving');
      layoutSaveQueue.current = layoutSaveQueue.current
        .catch(() => undefined)
        .then(async () => {
          try {
            await saveCanvasLayout(backendId, positions, workspaceVersionId);
            lastPersistedLayout.current = requestedSignature;
            if (currentLayoutScope.current !== requestedScope) return;
            if (latestLayout.current === requestedSignature) {
              pendingLayoutSave.current = null;
              setLayoutSaveStatus('saved');
              if (layoutSavedResetTimer.current !== null) window.clearTimeout(layoutSavedResetTimer.current);
              layoutSavedResetTimer.current = window.setTimeout(() => setLayoutSaveStatus('idle'), 1800);
            }
          } catch (error: any) {
            if (currentLayoutScope.current !== requestedScope || latestLayout.current !== requestedSignature) return;
            const detail = error?.response?.data?.detail ?? error?.detail;
            setLayoutSaveStatus('error');
            setLayoutSaveError(
              typeof detail === 'string'
                ? detail
                : detail?.message || error?.message || '节点位置保存失败',
            );
          }
        });
    }, 650);

    return () => {
      if (layoutSaveTimer.current !== null) window.clearTimeout(layoutSaveTimer.current);
    };
  }, [backendId, layoutRetryNonce, layoutScope, ontology?.objectTypes, storeNodes, syncStatus, workspaceVersionId]);

  // 右键上下文菜单
  const [ctxMenu, setCtxMenu] = useState<{ x: number; y: number; kind: 'node' | 'edge' | 'pane'; id?: string } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<DeleteTarget | null>(null);

  // 待确认的连线：拖线松手后先弹卡片让用户命名/选基数，确认才真正创建 LinkType。
  // 不再静默创建，也不再往画布加"只有视觉、不进 store"的边。
  const [pendingConn, setPendingConn] = useState<{ sourceId: string; targetId: string } | null>(null);

  const onConnect = useCallback(
    (connection: Connection) => {
      if (schemaReadOnly) return;
      if (!connection.source || !connection.target) return;
      const sourceNode = storeNodes.find((n) => n.id === connection.source);
      const targetNode = storeNodes.find((n) => n.id === connection.target);
      if (sourceNode?.type === 'objectType' && targetNode?.type === 'objectType') {
        setPendingConn({ sourceId: connection.source, targetId: connection.target });
      }
    },
    [storeNodes, schemaReadOnly]
  );

  const onNodeDragStop = useCallback(
    (_event: unknown, node: Node) => {
      updateNodePosition(node.id, node.position);
    },
    [updateNodePosition]
  );

  const onNodeClick = useCallback(
    (_event: unknown, node: Node) => {
      setSelectedNode(node.id);
    },
    [setSelectedNode]
  );

  const onEdgeClick = useCallback(
    (_: React.MouseEvent, edge: Edge) => {
      setSelectedEdge(edge.id);
    },
    [setSelectedEdge]
  );

  const onEdgeDoubleClick = useCallback(
    (_: React.MouseEvent, edge: Edge) => {
      setSelectedEdge(edge.id);
      openPanel('edit', 'linkType');
    },
    [setSelectedEdge, openPanel]
  );

  const onPaneClick = useCallback(() => {
    setSelectedNode(null);
    setSelectedEdge(null);
    setCtxMenu(null);
  }, [setSelectedNode, setSelectedEdge]);

  const onNodeContextMenu = useCallback((e: React.MouseEvent, node: Node) => {
    e.preventDefault();
    setSelectedNode(node.id);
    if (schemaReadOnly) return;
    setCtxMenu({ x: e.clientX, y: e.clientY, kind: 'node', id: node.id });
  }, [setSelectedNode, schemaReadOnly]);

  const onEdgeContextMenu = useCallback((e: React.MouseEvent, edge: Edge) => {
    e.preventDefault();
    setSelectedEdge(edge.id);
    if (schemaReadOnly) return;
    setCtxMenu({ x: e.clientX, y: e.clientY, kind: 'edge', id: edge.id });
  }, [setSelectedEdge, schemaReadOnly]);

  const onPaneContextMenu = useCallback((e: React.MouseEvent | MouseEvent) => {
    e.preventDefault();
    if (schemaReadOnly) return;
    setCtxMenu({ x: e.clientX, y: e.clientY, kind: 'pane' });
  }, [schemaReadOnly]);

  const handleNodesChange = useCallback((changes: NodeChange<Node>[]) => {
    onNodesChange(schemaReadOnly
      ? changes.filter((change) => change.type === 'select' || change.type === 'position' || change.type === 'dimensions')
      : changes);
  }, [onNodesChange, schemaReadOnly]);

  const handleEdgesChange = useCallback((changes: EdgeChange<Edge>[]) => {
    onEdgesChange(schemaReadOnly ? changes.filter((change) => change.type === 'select') : changes);
  }, [onEdgesChange, schemaReadOnly]);

  // 右键菜单条目
  const menuItems = useMemo(() => {
    if (schemaReadOnly) return [];
    if (!ctxMenu) return [];
    const close = () => setCtxMenu(null);
    if (ctxMenu.kind === 'node' && ctxMenu.id) {
      const id = ctxMenu.id;
      return [
        {
          icon: PencilSquareIcon, label: '编辑对象实体',
          onClick: () => { setSelectedNode(id); openPanel('edit', 'objectType'); close(); },
        },
        ...(onBrowseInstances ? [{
          icon: TableCellsIcon, label: '浏览实例数据',
          onClick: () => { onBrowseInstances(id); close(); },
        }] : []),
        {
          icon: TrashIcon, label: '删除…', danger: true,
          onClick: () => { setDeleteTarget({ kind: 'objectType', id }); close(); },
        },
      ];
    }
    if (ctxMenu.kind === 'edge' && ctxMenu.id) {
      const id = ctxMenu.id;
      return [
        {
          icon: PencilSquareIcon, label: '编辑实体关系',
          onClick: () => { setSelectedEdge(id); openPanel('edit', 'linkType'); close(); },
        },
        {
          icon: TrashIcon, label: '删除…', danger: true,
          onClick: () => { setDeleteTarget({ kind: 'linkType', id }); close(); },
        },
      ];
    }
    return [
      {
        icon: CubeIcon, label: '在此创建对象实体',
        onClick: () => {
          const pos = screenToFlowPosition({ x: ctxMenu.x, y: ctxMenu.y });
          setPendingNodePosition(pos);
          openPanel('create', 'objectType');
          close();
        },
      },
      {
        icon: ArrowsPointingOutIcon, label: '自动布局',
        onClick: () => { autoLayout('dagre', 'LR'); close(); },
      },
    ];
  }, [ctxMenu, schemaReadOnly, setSelectedNode, setSelectedEdge, openPanel, onBrowseInstances, screenToFlowPosition, setPendingNodePosition, autoLayout]);

  return (
    <div className="w-full h-full canvas-bg">
      <ReactFlow
        nodes={renderNodes}
        edges={renderEdges}
        onNodesChange={handleNodesChange}
        onEdgesChange={handleEdgesChange}
        onConnect={onConnect}
        onNodeDragStop={onNodeDragStop}
        onNodeClick={onNodeClick}
        onEdgeClick={onEdgeClick}
        onEdgeDoubleClick={onEdgeDoubleClick}
        onPaneClick={onPaneClick}
        onNodeContextMenu={onNodeContextMenu}
        onEdgeContextMenu={onEdgeContextMenu}
        onPaneContextMenu={onPaneContextMenu}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        defaultEdgeOptions={defaultEdgeOptions}
        connectionMode={ConnectionMode.Loose}
        nodesDraggable
        nodesConnectable={!schemaReadOnly}
        deleteKeyCode={null}
        fitView
        fitViewOptions={{ padding: 0.2, maxZoom: 0.8 }}
        minZoom={0.1}
        maxZoom={2}
        snapToGrid
        snapGrid={[12, 12]}
        proOptions={{ hideAttribution: true }}
      >
        <Background
          variant={BackgroundVariant.Dots}
          gap={24}
          size={1}
          color="var(--color-canvas-dot)"
        />
        <MiniMap
          className="!bottom-6 !right-6"
          nodeColor={(node) => {
            if (node.type === 'objectType') return 'var(--color-object)';
            return 'var(--color-minimap-node-alt)';
          }}
          maskColor="var(--color-minimap-mask)"
        />
      </ReactFlow>

      {layoutSaveStatus !== 'idle' && (
        <div
          role={layoutSaveStatus === 'error' ? 'alert' : 'status'}
          data-testid="layout-save-status"
          className={`absolute bottom-20 left-1/2 z-50 flex -translate-x-1/2 items-center gap-2 rounded-lg border px-3 py-2 text-xs shadow-lg backdrop-blur ${
            layoutSaveStatus === 'error'
              ? 'border-red-500/40 bg-red-950/95 text-red-200'
              : 'border-emerald-500/30 bg-surface-900/90 text-emerald-300'
          }`}
        >
          {layoutSaveStatus === 'saving' && (
            <span className="h-3 w-3 animate-spin rounded-full border-2 border-emerald-400 border-t-transparent" />
          )}
          <span>
            {layoutSaveStatus === 'saving'
              ? '正在自动保存布局…'
              : layoutSaveStatus === 'saved'
                ? '布局已自动保存'
                : layoutSaveError}
          </span>
          {layoutSaveStatus === 'error' && (
            <button
              type="button"
              className="font-medium underline underline-offset-2 hover:text-white focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-red-300"
              onClick={() => setLayoutRetryNonce((value) => value + 1)}
            >
              重试
            </button>
          )}
        </div>
      )}

      {/* 空态引导：还没有任何对象类型时给出下一步指引 */}
      {storeNodes.length === 0 && (
        <div className="absolute inset-0 flex items-center justify-center pointer-events-none">
          <div className="pointer-events-auto text-center max-w-sm px-8 py-10 rounded-2xl bg-surface-900/70 border border-surface-700 backdrop-blur">
            <CubeIcon className="w-10 h-10 text-indigo-400/70 mx-auto mb-3" />
            <h3 className="text-surface-100 font-medium mb-1.5">
              {schemaReadOnly ? '暂无模型结构可展示' : '从第一个对象实体开始'}
            </h3>
            <p className="text-xs text-surface-400 leading-relaxed mb-4">
              {schemaReadOnly
                ? '选择一个已经建模的本体后，这里会展示对象实体、关系与运行能力的结构视图。'
                : '对象实体是本体的骨架。先声明业务里"有什么"（如 订单、客户），再拖线建立它们之间的关系。'}
            </p>
            {!schemaReadOnly && (
              <>
                <button
                  onClick={() => openPanel('create', 'objectType')}
                  className="px-4 py-2 rounded-lg bg-indigo-600 hover:bg-indigo-500 text-white text-sm font-medium transition-colors"
                >
                  ＋ 创建对象实体
                </button>
                <p className="text-[11px] text-surface-500 mt-3">也可以右键画布，或按 Ctrl+K 搜索</p>
              </>
            )}
          </div>
        </div>
      )}

      {/* 右键上下文菜单 */}
      {ctxMenu && menuItems.length > 0 && (
        <>
          <div className="fixed inset-0 z-[104]" onClick={() => setCtxMenu(null)} onContextMenu={(e) => { e.preventDefault(); setCtxMenu(null); }} />
          <div
            className="fixed z-[105] min-w-[180px] bg-surface-800 border border-surface-600 rounded-xl shadow-2xl p-1.5 animate-fade-in"
            style={{
              left: Math.min(ctxMenu.x, window.innerWidth - 200),
              top: Math.min(ctxMenu.y, window.innerHeight - menuItems.length * 40 - 16),
            }}
          >
            {menuItems.map((item) => (
              <button
                key={item.label}
                onClick={item.onClick}
                className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-left text-sm transition-colors ${
                  'danger' in item && item.danger
                    ? 'text-red-300 hover:bg-red-900/40'
                    : 'text-surface-200 hover:bg-surface-700'
                }`}
              >
                <item.icon className="w-4 h-4 shrink-0" />
                {item.label}
              </button>
            ))}
          </div>
        </>
      )}

      {pendingConn && (
        <ConnectLinkDialog
          sourceId={pendingConn.sourceId}
          targetId={pendingConn.targetId}
          onClose={() => setPendingConn(null)}
        />
      )}

      {deleteTarget && (
        <DeleteSelectedDialog target={deleteTarget} onClose={() => setDeleteTarget(null)} />
      )}
    </div>
  );
}
