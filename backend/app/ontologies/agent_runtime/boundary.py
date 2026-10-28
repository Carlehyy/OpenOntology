"""
授权边界 (AgentScope) — agent 世界观的唯一入口

把「本体 × AgentProfile」解析成 agent 可见的对象类型 / 链接类型 / 动作集合，
并从这个受限视图确定性地生成一份「本体技能卡」(skill card)：
对象、属性、关系、动作的紧凑说明，注入 system prompt。

规则：
  - 白名单语义：None=全部允许，[]=全部拒绝，[id...]=仅列表内。
  - 链接类型仅当"两端对象类型都可见"时才可见（防止经由链接泄露隐藏类型）。
  - 动作若绑定了不可见的对象类型则不可见。
所有工具都必须经 require_* 取类型/动作 —— 越界即 ToolError，不存在旁路。
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.ontology import OntologyProject
from app.models.ontology_formal import (
    ObjectType, LinkType, ActionType, ObjectInstance,
)
from app.ontologies.agent_runtime.models import AgentProfile
from app.ontologies.release_context import CurrentReleaseContext, current_release_context
from app.ontologies.versions.snapshot_contract import (
    snapshot_models,
)


class ToolError(Exception):
    """工具层可恢复错误 — 返回给 LLM 让其自我修正，不打断对话。"""


def remote_object_type_ids(scope: AgentScope) -> set[str]:
    if scope.release is None:
        return set()
    from app.ontologies.mappings.remote_binding import remote_bindings_by_type
    return set(remote_bindings_by_type(scope.release.snapshot))


def require_materialized_object_type(scope: AgentScope, object_type) -> None:
    if object_type.id in remote_object_type_ids(scope):
        raise ToolError(
            "remote_ontology_semantic_query_required: "
            f"对象类型「{object_type.display_name}」为远程绑定，本地无物化事实，"
            "不返回空结果或 0 统计冒充完整；请经超级助手 ontology_query "
            "或统一查询 API 获取远程数据")


def get_or_create_profile(db: Session, ontology_id: str) -> AgentProfile:
    profile = db.query(AgentProfile).filter(
        AgentProfile.ontology_id == ontology_id).first()
    if not profile:
        profile = AgentProfile(ontology_id=ontology_id, allowed_action_ids=[])
        db.add(profile)
        db.commit()
        db.refresh(profile)
    return profile


def _allowed(ids: Optional[list], item_id: str) -> bool:
    if ids is None:
        return True
    return item_id in ids


class AgentScope:
    """profile 过滤后的本体视图。工具层只认识这个对象，不认识原始表。"""

    def __init__(self, db: Session, ontology: OntologyProject, profile: AgentProfile,
                 release: CurrentReleaseContext | None = None):
        self.db = db
        self.ontology = ontology
        self.profile = profile
        self.release = release
        self.release_id = release.id if release is not None else None

        oid = ontology.id
        if release is not None:
            # The mutable fo_* tables are only the runtime projection.  Names,
            # properties, action rules and relationship endpoints shown to the
            # assistant must come from the exact immutable release snapshot,
            # not merely from live rows that happen to share the same ids.
            released = snapshot_models(release.snapshot)
            all_types = released["objectTypes"]
            all_links = released["linkTypes"]
            all_actions = released["actions"]
        else:
            all_types = db.query(ObjectType).filter(ObjectType.ontology_id == oid).all()
            all_links = db.query(LinkType).filter(LinkType.ontology_id == oid).all()
            all_actions = db.query(ActionType).filter(ActionType.ontology_id == oid).all()

        self.object_types: dict[str, Any] = {
            t.id: t for t in all_types
            if _allowed(profile.allowed_object_type_ids, t.id)
        }

        self.link_types: dict[str, Any] = {
            lt.id: lt for lt in all_links
            if _allowed(profile.allowed_link_type_ids, lt.id)
            and lt.source_object_type_id in self.object_types
            and lt.target_object_type_id in self.object_types
        }

        self.actions: dict[str, Any] = {
            a.id: a for a in all_actions
            if _allowed(profile.allowed_action_ids, a.id)
            and (a.object_type_id is None or a.object_type_id in self.object_types)
        }

    def with_profile(self, profile: AgentProfile) -> "AgentScope":
        """返回绑定另一份 profile 的同界视图。

        评估沙箱试跑（assistant_evaluation 双臂实验）用它在同一本体、同一
        release 界内换用草稿 profile——草稿对象不落库，生产配置零改动。
        """
        return AgentScope(self.db, self.ontology, profile, self.release)

    # ---------- 引用解析（id / name / displayName 均可） ----------

    def _resolve(self, pool: dict, ref: str, kind: str):
        ref = (ref or "").strip()
        if ref in pool:
            return pool[ref]
        for item in pool.values():
            if item.name == ref or item.display_name == ref:
                return item
        names = "、".join(f"{i.display_name}({i.name})" for i in pool.values()) or "（无）"
        raise ToolError(f"{kind}「{ref}」不存在或不在授权范围内。可用{kind}：{names}")

    def require_object_type(self, ref: str) -> Any:
        return self._resolve(self.object_types, ref, "对象类型")

    def require_link_type(self, ref: str) -> Any:
        return self._resolve(self.link_types, ref, "链接类型")

    def require_action(self, ref: str) -> Any:
        return self._resolve(self.actions, ref, "动作")

    def resolve_property(self, ot: Any, ref: str) -> str:
        """把属性引用（name 或 displayName）解析为规范 name；越界即 ToolError（含可用属性）。

        合法集直接取自 ot.properties —— 派生/计算属性也声明在其中（source='computed'，
        computed 的键名即 prop['name']），故一并覆盖，不会误伤合法的派生属性过滤。
        这是「越界硬失败」：LLM 拼错或臆造属性名时，返回可用清单让它自我修正，
        而不是让一个查不到的条件静默匹配为空、给出看似正确实则错误的答案。

        属性级授权（YIYA-36 P2-2）在边界层统一强制：allowed_properties 收紧后，
        既有搜索/遍历/聚合/图谱等全部引用路径与新查询入口同等受限。
        """
        ref = (ref or "").strip()
        allowed = self.allowed_property_names(ot.id)
        for p in (ot.properties or []):
            if isinstance(p, dict) and ref in (p.get("name"), p.get("displayName"), p.get("display_name")):
                name = p.get("name")
                if allowed is not None and name not in allowed:
                    raise ToolError(
                        f"属性「{ref}」不在对象类型「{ot.display_name}」的授权范围内")
                return name
        avail = "、".join(
            f"{p.get('displayName') or p.get('name')}({p.get('name')})"
            for p in (ot.properties or []) if isinstance(p, dict) and p.get("name")
        ) or "（无）"
        raise ToolError(f"属性「{ref}」不在对象类型「{ot.display_name}」上，或拼写有误。可用属性：{avail}")

    def visible_instance(self, inst: Optional[ObjectInstance]) -> ObjectInstance:
        """实例必须属于本体 + 其类型在授权范围内。"""
        if not inst or inst.ontology_id != self.ontology.id:
            raise ToolError("对象实例不存在")
        if self.release_id is not None and inst.ontology_release_id != self.release_id:
            raise ToolError("对象实例不属于当前发布版本")
        if inst.object_type_id not in self.object_types:
            raise ToolError("该实例所属的对象类型不在授权范围内")
        return inst

    def allowed_property_names(self, object_type_id: str) -> Optional[set]:
        """属性级授权的规范名集合；None=该类型全部已声明属性允许。

        ``AgentProfile.allowed_properties`` 语义（与对象/链接/动作白名单一致）：
        整列为 NULL=全部声明属性；map 中类型键为 NULL=该类型全部、[]=无业务
        属性、列表=仅列出属性；map 非空时未列出的类型默认无业务属性。
        """
        allowed_map = getattr(self.profile, "allowed_properties", None)
        if allowed_map is None:
            return None
        if not isinstance(allowed_map, dict):
            return set()
        entry = allowed_map.get(object_type_id)
        if entry is None:
            if object_type_id in allowed_map:
                return None
            return set()
        if isinstance(entry, list):
            return {str(name) for name in entry}
        return set()

    def resolve_visible_property(self, object_type, ref: str) -> str:
        """声明检查 + 属性授权合一的解析入口；越界即 ToolError。"""
        name = self.resolve_property(object_type, ref)
        allowed = self.allowed_property_names(object_type.id)
        if allowed is not None and name not in allowed:
            raise ToolError(
                f"属性「{ref}」不在对象类型「{object_type.display_name}」的授权范围内"
            )
        return name

    def instance_counts(self, *, fresh: bool = False) -> dict[str, int]:
        """按类型计数实例。

        上亿实例下 group-by count 可能长跑：默认走 Redis cache-aside
        （短 TTL + 版本键失效，Redis 不可用时静默降级直查）；fresh=True
        强制直查，供「刷新本体清单」等显式操作绕过缓存。
        """

        def build() -> dict[str, int]:
            rows = (self.db.query(ObjectInstance.object_type_id, func.count(ObjectInstance.id))
                    .filter(ObjectInstance.ontology_id == self.ontology.id)
                    )
            if self.release_id is not None:
                rows = rows.filter(ObjectInstance.ontology_release_id == self.release_id)
            rows = rows.group_by(ObjectInstance.object_type_id).all()
            return {tid: n for tid, n in rows if tid in self.object_types}

        if fresh:
            return build()
        from app.ontologies import cache as ontology_cache
        key = ontology_cache.instance_counts_cache_key(self.ontology.id, self.release_id)
        return ontology_cache.cached_call(
            key, ontology_cache.INSTANCE_COUNTS_TTL_SECONDS, build)

    def skill_card(self) -> str:
        """从受限视图生成紧凑的 schema 说明 — agent 对世界的全部认知。"""
        counts = self.instance_counts()
        lines: list[str] = []
        lines.append(f"# 本体「{self.ontology.name}」")
        if self.ontology.description:
            lines.append(str(self.ontology.description).strip())

        lines.append("\n## 对象类型")
        if not self.object_types:
            lines.append("（无授权的对象类型）")
        for t in self.object_types.values():
            n = counts.get(t.id, 0)
            desc = f" — {t.description}" if t.description else ""
            lines.append(f"### {t.display_name} (name={t.name}, 实例数={n}){desc}")
            allowed_props = self.allowed_property_names(t.id)
            for p in (t.properties or []):
                if not isinstance(p, dict):
                    continue
                if allowed_props is not None and p.get("name") not in allowed_props:
                    continue
                req = "必填" if p.get("required") else "可选"
                pdesc = f" — {p['description']}" if p.get("description") else ""
                pk = " [主键]" if t.primary_key and (p.get("id") == t.primary_key or p.get("name") == t.primary_key) else ""
                lines.append(f"- {p.get('displayName') or p.get('name')} (name={p.get('name')}, {p.get('type', 'string')}, {req}){pk}{pdesc}")

        lines.append("\n## 链接类型（对象间关系）")
        if not self.link_types:
            lines.append("（无）")
        for lt in self.link_types.values():
            src = self.object_types.get(lt.source_object_type_id)
            tgt = self.object_types.get(lt.target_object_type_id)
            desc = f" — {lt.description}" if lt.description else ""
            lines.append(
                f"- {lt.display_name} (name={lt.name}): "
                f"{src.display_name if src else '?'} → {tgt.display_name if tgt else '?'}"
                f" [{lt.cardinality}]{desc}")

        lines.append("\n## 可用动作（业务操作，先 propose_action 预演，永不直接执行）")
        if not self.actions:
            lines.append("（当前授权范围内没有可用动作 — 只能做查询和分析）")
        for a in self.actions.values():
            ot = self.object_types.get(a.object_type_id) if a.object_type_id else None
            approval = "，需人工审批" if a.requires_approval else ""
            desc = f" — {a.description}" if a.description else ""
            lines.append(f"### {a.display_name} (name={a.name}"
                         f"{f', 作用于 {ot.display_name}' if ot else ''}{approval}){desc}")
            for p in (a.parameters or []):
                if not isinstance(p, dict):
                    continue
                req = "必填" if p.get("required") else "可选"
                pdesc = f" — {p['description']}" if p.get("description") else ""
                lines.append(f"- 参数 {p.get('displayName') or p.get('name')} "
                             f"(name={p.get('name')}, {p.get('type', 'string')}, {req}){pdesc}")
        return "\n".join(lines)

    def summary(self) -> dict:
        """给前端的能力概览（边界可视化）。"""
        counts = self.instance_counts()
        return {
            "enabled": self.profile.enabled,
            "objectTypes": [
                {"id": t.id, "name": t.name, "displayName": t.display_name,
                 "instanceCount": counts.get(t.id, 0)}
                for t in self.object_types.values()
            ],
            "linkTypes": [
                {"id": lt.id, "name": lt.name, "displayName": lt.display_name}
                for lt in self.link_types.values()
            ],
            "actions": [
                {"id": a.id, "name": a.name, "displayName": a.display_name,
                 "requiresApproval": bool(a.requires_approval)}
                for a in self.actions.values()
            ],
            "allowActionProposals": self.profile.allow_action_proposals,
            "maxRowsPerQuery": self.profile.max_rows_per_query,
            "maxSteps": self.profile.max_steps,
        }


def build_scope(db: Session, ontology_id: str, *,
                release_id: str | None = None) -> tuple[OntologyProject, AgentProfile, AgentScope]:
    ontology = db.query(OntologyProject).filter(OntologyProject.id == ontology_id).first()
    if not ontology:
        raise ToolError("本体不存在")
    release = None
    if release_id is not None:
        try:
            release = current_release_context(
                db, ontology_id, expected_release_id=release_id)
        except Exception as exc:
            detail = getattr(exc, "detail", None)
            if isinstance(detail, dict):
                raise ToolError(str(detail.get("message") or detail)) from exc
            raise ToolError(str(detail or exc)) from exc
    profile = get_or_create_profile(db, ontology_id)
    return ontology, profile, AgentScope(db, ontology, profile, release)
