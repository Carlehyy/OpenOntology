"""任务实例 — WorkflowSpec 语义校验（设计方案 §4.3）。

结构解析（pydantic）之后的第二道闸：图论与引用完整性。产出结构化
错误列表（code/message/ref），模板保存与激活前强制全绿：

  - 节点/边/契约数量在界内
  - 边端点引用存在的节点与端口（condition/terminal 不允许出边；
    condition 的上游走 on 字段，不允许入边）
  - 数据边 + condition 分支构成 DAG（Kahn 拓扑，报出环路径）
  - 打回边（rework）单独检测「仅含打回边」的环
  - 至少一个 terminal，且从某源点可达
  - 端口契约必须引用已命名契约，契约声明本身合法（白名单子集）
  - condition 必须配置 default（穷举不可判定，一律要求兜底路由）
"""
from __future__ import annotations

from collections import deque

from app.task_instances import contracts as contract_engine
from app.task_instances.spec import (
    APPROVAL_PORTS,
    DEFAULT_OUTPUT_PORT,
    MAX_CONTRACTS,
    MAX_EDGES,
    MAX_NODES,
    NODE_AGENT,
    NODE_APPROVAL,
    NODE_CONDITION,
    NODE_HUMAN,
    NODE_JOIN,
    NODE_TERMINAL,
    WorkflowSpec,
)

_VALID_NODE_ID_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def node_kind(spec: WorkflowSpec, node_id: str) -> str | None:
    node = spec.nodes.get(node_id)
    return node.kind if node else None


def output_ports(spec: WorkflowSpec, node_id: str) -> list[str]:
    """节点的合法出边端口集合。"""
    node = spec.nodes.get(node_id)
    if node is None:
        return []
    kind = node.kind
    if kind == NODE_APPROVAL:
        return list(APPROVAL_PORTS)
    if kind in (NODE_AGENT, NODE_HUMAN):
        ports = list((node.outputs or {}).keys())
        if DEFAULT_OUTPUT_PORT not in ports:
            ports.append(DEFAULT_OUTPUT_PORT)
        return ports
    if kind == NODE_JOIN:
        return [DEFAULT_OUTPUT_PORT]
    return []


def split_ref(ref: str) -> tuple[str, str | None]:
    """`node` / `node.port` → (node_id, port|None)。"""
    if "." in ref:
        node_id, port = ref.split(".", 1)
        return node_id, port
    return ref, None


def validate_workflow(spec: WorkflowSpec) -> list[dict]:
    errors: list[dict] = []

    def err(code: str, message: str, ref: str | None = None) -> None:
        item = {"code": code, "message": message}
        if ref:
            item["ref"] = ref
        errors.append(item)

    # —— 数量界限 ——
    if not 1 <= len(spec.nodes) <= MAX_NODES:
        err("NODE_COUNT", f"节点数必须在 1~{MAX_NODES} 之间，当前 {len(spec.nodes)}")
    if not 1 <= len(spec.edges) <= MAX_EDGES:
        err("EDGE_COUNT", f"边数必须在 1~{MAX_EDGES} 之间，当前 {len(spec.edges)}")
    contracts = spec.contracts or {}
    if len(contracts) > MAX_CONTRACTS:
        err("CONTRACT_COUNT", f"契约数超过上限 {MAX_CONTRACTS}")

    # —— 节点 id 合法性 ——
    for node_id in spec.nodes:
        if not node_id or not set(node_id) <= _VALID_NODE_ID_CHARS:
            err("NODE_ID", f"节点 id 只允许字母/数字/下划线/连字符: {node_id!r}",
                node_id)

    # —— 契约声明合法性 ——
    for name, schema in contracts.items():
        try:
            contract_engine.validate_contract_schema(schema)
        except contract_engine.ContractError as exc:
            err("CONTRACT_INVALID", f"契约 {name}: {exc}", name)

    # —— 端口契约引用 ——
    for node_id, node in spec.nodes.items():
        outputs = getattr(node, "outputs", None) or {}
        for port_name, port in outputs.items():
            if port.contract is not None and port.contract not in contracts:
                err("CONTRACT_REF",
                    f"节点 {node_id}.{port_name} 引用了未定义契约 {port.contract!r}",
                    f"{node_id}.{port_name}")

    # —— 边引用与端口 ——
    data_edges: list[tuple[str, str]] = []  # 非打回数据边（拓扑用）
    rework_edges: list[tuple[str, str]] = []
    branch_targets: dict[str, list[str]] = {}
    for index, edge in enumerate(spec.edges, start=1):
        ref = f"edges[{index}]"
        src_node, src_port = split_ref(edge.from_)
        dst_node, _ = split_ref(edge.to)
        if src_node not in spec.nodes:
            err("EDGE_REF", f"{ref}: from 引用了不存在的节点 {src_node!r}", ref)
            continue
        if dst_node not in spec.nodes:
            err("EDGE_REF", f"{ref}: to 引用了不存在的节点 {dst_node!r}", ref)
            continue
        src_kind = node_kind(spec, src_node)
        dst_kind = node_kind(spec, dst_node)
        if src_kind in (NODE_CONDITION, NODE_TERMINAL):
            err("EDGE_FROM_FORBIDDEN",
                f"{ref}: {src_kind} 节点不允许出边（condition 走 branches 路由）",
                ref)
            continue
        if dst_kind == NODE_CONDITION:
            err("EDGE_TO_CONDITION",
                f"{ref}: condition 节点不接受入边（上游用 on 字段声明）", ref)
            continue
        ports = output_ports(spec, src_node)
        if src_port is not None and src_port not in ports:
            err("PORT_REF",
                f"{ref}: 节点 {src_node} 不存在端口 {src_port!r}（合法: {ports}）",
                ref)
            continue
        if src_kind == NODE_APPROVAL and src_port is None:
            err("PORT_REQUIRED",
                f"{ref}: approval 节点的出边必须显式端口（approved/rejected）",
                ref)
            continue
        if edge.rework:
            if dst_kind not in (NODE_AGENT, NODE_HUMAN):
                err("REWORK_TARGET",
                    f"{ref}: 打回边目标必须是可重做的 agent/human 节点，"
                    f"当前 {dst_kind}", ref)
                continue
            rework_edges.append((src_node, dst_node))
        else:
            data_edges.append((src_node, dst_node))

    # —— condition：on 引用、分支目标、default 必填 ——
    for node_id, node in spec.nodes.items():
        if node.kind != NODE_CONDITION:
            continue
        on_node, on_port = split_ref(node.on)
        if on_node not in spec.nodes:
            err("CONDITION_ON", f"节点 {node_id}: on 引用了不存在的节点 {on_node!r}",
                node_id)
            continue
        if node_kind(spec, on_node) in (NODE_CONDITION,):
            err("CONDITION_ON", f"节点 {node_id}: on 不能引用 condition 节点",
                node_id)
        if on_port is not None and on_port not in output_ports(spec, on_node):
            err("PORT_REF",
                f"节点 {node_id}: on 引用的端口 {node.on!r} 不存在", node_id)
        if not node.default:
            err("CONDITION_DEFAULT",
                f"节点 {node_id}: condition 必须配置 default 兜底路由", node_id)
        for branch_index, branch in enumerate(node.branches, start=1):
            for target in branch.to:
                if target not in spec.nodes:
                    err("BRANCH_REF",
                        f"节点 {node_id} 分支#{branch_index}: 目标 {target!r} 不存在",
                        node_id)
                    continue
                if node_kind(spec, target) == NODE_CONDITION:
                    err("BRANCH_REF",
                        f"节点 {node_id} 分支#{branch_index}: 目标不能是 condition",
                        node_id)
                    continue
                branch_targets.setdefault(node_id, []).append(target)
        if node.default:
            for target in node.default:
                if target not in spec.nodes:
                    err("BRANCH_REF",
                        f"节点 {node_id} default: 目标 {target!r} 不存在", node_id)

    # —— DAG 环检测（数据边 + condition 分支） ——
    graph_edges = list(data_edges)
    for src, targets in branch_targets.items():
        for target in targets:
            graph_edges.append((src, target))
    cycle = _find_cycle(spec, graph_edges)
    if cycle:
        err("CYCLE", "检测到环: " + " -> ".join(cycle + [cycle[0]]))

    # —— 仅含打回边的环 ——
    rework_cycle = _find_cycle(spec, rework_edges)
    if rework_cycle:
        err("REWORK_CYCLE",
            "打回边构成环: " + " -> ".join(rework_cycle + [rework_cycle[0]]))

    # —— terminal 存在且可达 ——
    terminals = [nid for nid, n in spec.nodes.items() if n.kind == NODE_TERMINAL]
    if not terminals:
        err("NO_TERMINAL", "流程必须至少包含一个 terminal 节点")
    else:
        incoming: dict[str, set[str]] = {nid: set() for nid in spec.nodes}
        for src, dst in graph_edges:
            incoming.setdefault(dst, set()).add(src)
        for node_id, node in spec.nodes.items():
            if node.kind == NODE_CONDITION:
                upstream, _ = split_ref(node.on)
                if upstream in spec.nodes:
                    incoming.setdefault(node_id, set()).add(upstream)
        sources = [nid for nid in spec.nodes
                   if not incoming.get(nid)
                   and node_kind(spec, nid) != NODE_TERMINAL]
        reachable = _reachable_from(spec, sources, graph_edges)
        if terminals and not any(t in reachable for t in terminals):
            err("TERMINAL_UNREACHABLE",
                "没有任何 terminal 节点从源点可达（源点: "
                f"{sources or '无（可能全部成环）'}）")

    return errors


def _find_cycle(spec: WorkflowSpec, edges: list[tuple[str, str]]) -> list[str] | None:
    """Kahn 拓扑：返回一个环路径（无环返回 None）。"""
    nodes = set(spec.nodes)
    for src, dst in edges:
        nodes.update((src, dst))
    indegree = {nid: 0 for nid in nodes}
    adjacency: dict[str, list[str]] = {nid: [] for nid in nodes}
    for src, dst in edges:
        adjacency[src].append(dst)
        indegree[dst] += 1
    queue = deque(sorted(nid for nid, deg in indegree.items() if deg == 0))
    seen = 0
    while queue:
        current = queue.popleft()
        seen += 1
        for nxt in sorted(adjacency[current]):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if seen == len(nodes):
        return None
    remaining = sorted(nid for nid, deg in indegree.items() if deg > 0)
    start = remaining[0]
    path: list[str] = []
    position: dict[str, int] = {}
    cursor = start
    while cursor not in position:
        position[cursor] = len(path)
        path.append(cursor)
        nxts = [n for n in adjacency[cursor] if indegree.get(n, 0) > 0]
        cursor = sorted(nxts)[0] if nxts else start
    return path[position[cursor]:]


def _reachable_from(spec: WorkflowSpec, sources: list[str],
                    edges: list[tuple[str, str]]) -> set[str]:
    adjacency: dict[str, list[str]] = {}
    for src, dst in edges:
        adjacency.setdefault(src, []).append(dst)
    for node_id, node in spec.nodes.items():
        if node.kind == NODE_CONDITION:
            upstream = (node.on or "").split(".", 1)[0]
            if upstream in spec.nodes:
                # condition 的入口来自其 on 上游（非数据边）
                adjacency.setdefault(upstream, []).append(node_id)
            for targets in ([b.to for b in node.branches]
                            + [node.default or []]):
                for target in targets:
                    adjacency.setdefault(node_id, []).append(target)
    seen: set[str] = set()
    queue = deque(sources)
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(adjacency.get(current, []))
    return seen
