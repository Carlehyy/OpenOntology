"""
后端函数引擎 (Function Engine)

执行本体函数 (OntologyFunction)。安全策略：
  - language='expression' → 用 safe_eval 在白名单 AST 沙箱里求值
  - language='typescript'  → 后端不执行（标记为客户端执行），返回提示
三类：object / object_set / action_validation
"""
from __future__ import annotations
import ast
import decimal
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Iterable, Optional

from sqlalchemy.orm import Session

from app.models.ontology_formal import OntologyFunction, ObjectInstance
from app.services.formal.safe_eval import safe_eval, SafeEvalError


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


ScopeObjectLoader = Callable[[], Iterable[dict]]


def _contract_field(item: Any, *names: str, default=None):
    if isinstance(item, dict):
        for name in names:
            if name in item:
                return item[name]
        return default
    for name in names:
        if hasattr(item, name):
            return getattr(item, name)
    return default


def _expression_contract_references(
    body: str,
) -> tuple[set[str], bool, bool]:
    """Extract statically knowable object refs and forbidden live scopes."""
    try:
        tree = ast.parse(body or "", mode="eval")
    except SyntaxError:
        # The canonical expression compiler reports syntax errors elsewhere.
        return set(), False, False

    object_properties: set[str] = set()
    uses_params = False
    uses_objects = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            uses_params = uses_params or node.id == "params"
            uses_objects = uses_objects or node.id == "objects"
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "object"
            and node.attr != "get"
        ):
            object_properties.add(node.attr)
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "object"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            object_properties.add(node.slice.value)
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "object"
            and node.func.attr == "get"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            object_properties.add(node.args[0].value)
    return object_properties, uses_params, uses_objects


def derived_function_contract_issues(
    fn: Any,
    *,
    object_type_id: str,
    computed_property_names: set[str],
    stored_property_names: set[str] | None = None,
    expected_return_type: str | None = None,
) -> list[tuple[str, str]]:
    """Validate the server-executable contract for one computed property.

    Computed projection evaluation has exactly one input: the instance's
    stored ``object`` properties.  There is no parameter binding, collection
    scope, or computed-property DAG/topological scheduler.  Rejecting those
    unsupported dependencies at save/publish and runtime prevents a model that
    looks valid but inevitably fails when real data arrives.
    """
    issues: list[tuple[str, str]] = []
    function_type = str(
        _contract_field(fn, "functionType", "function_type", default="")
        or ""
    ).strip().lower()
    if function_type != "object":
        issues.append((
            "invalid_derived_function_type",
            f"函数类型必须为 object，实际为 "
            f"{function_type or '(空)'}",
        ))

    target_type_id = str(
        _contract_field(
            fn, "targetObjectTypeId", "target_object_type_id", default="")
        or ""
    )
    if target_type_id and target_type_id != str(object_type_id):
        issues.append((
            "derived_function_target_mismatch",
            f"函数绑定了其他对象类型: {target_type_id}",
        ))

    parameters = _contract_field(fn, "parameters", default=[]) or []
    if parameters:
        issues.append((
            "derived_function_parameters_unsupported",
            "派生函数不能声明参数；运行时没有计算属性参数绑定机制",
        ))

    language = str(
        _contract_field(fn, "language", default="expression")
        or "expression"
    ).strip().lower()
    if language == "expression":
        body = str(_contract_field(fn, "body", default="") or "")
        object_refs, uses_params, uses_objects = (
            _expression_contract_references(body)
        )
        if uses_params:
            issues.append((
                "derived_function_params_scope_unsupported",
                "派生函数表达式不能引用 params；运行时不会注入动作参数",
            ))
        if uses_objects:
            issues.append((
                "derived_function_objects_scope_unsupported",
                "派生函数表达式不能引用 objects；派生属性只接受单对象输入",
            ))
        computed_dependencies = sorted(
            object_refs & set(computed_property_names))
        if computed_dependencies:
            issues.append((
                "derived_function_dependency_unsupported",
                "派生函数不能依赖其他派生属性（尚无 DAG 调度）: "
                + ", ".join(computed_dependencies),
            ))
        if stored_property_names is not None:
            unknown_properties = sorted(
                object_refs
                - set(computed_property_names)
                - set(stored_property_names)
            )
            if unknown_properties:
                issues.append((
                    "derived_function_unknown_property",
                    "派生函数引用了对象类型未声明的存储属性: "
                    + ", ".join(unknown_properties),
                ))

    def normalize_type(raw: Any) -> str:
        value = str(raw or "").strip().lower()
        return {
            "integer": "number",
            "int": "number",
            "float": "number",
            "double": "number",
            "bool": "boolean",
            "dict": "object",
            "json": "object",
            "list": "array",
            "timestamp": "datetime",
        }.get(value, value)

    declared_return_type = normalize_type(
        _contract_field(fn, "returnType", "return_type", default=""))
    expected = normalize_type(expected_return_type)
    compatible_return_types = (
        {"reference", "string", "number"}
        if expected == "reference" else {expected}
    )
    if (
        declared_return_type
        and expected
        and declared_return_type not in compatible_return_types
    ):
        issues.append((
            "derived_function_return_type_mismatch",
            f"函数声明返回类型 {declared_return_type} 与派生属性类型 "
            f"{expected} 不一致",
        ))
    return issues


def function_uses_object_collection(fn: Any) -> bool:
    """Whether this function contract receives a populated ``objects`` scope."""
    return str(
        getattr(fn, "function_type", None) or "object"
    ).strip().lower() in ("object_set", "action_validation")


def _expression_number(value: Any) -> Any:
    """Map exact ``Decimal`` back to the expression sandbox's numeric domain.

    Integral Decimals become Python ints (exact bigint semantics, no float64
    rounding); true fractional Decimals become floats, matching the original
    int-then-float coercion contract. ``Decimal * float`` must not leak into
    expression evaluation, while the stored property token stays exact.
    """
    if isinstance(value, Decimal):
        try:
            if (value.is_finite()
                    and value == value.to_integral_value()
                    and abs(value) <= Decimal("1e308")):
                return int(value)
            return float(value)
        except decimal.DecimalException:
            return float(value)
    if isinstance(value, dict):
        return {key: _expression_number(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_expression_number(item) for item in value]
    return value


def build_function_scope(
    fn: Any,
    *,
    obj_props: Optional[dict] = None,
    params: Optional[dict] = None,
    object_loader: ScopeObjectLoader | None = None,
) -> dict[str, Any]:
    """Build the expression scope shared by production and isolated trials.

    Object functions intentionally receive an empty ``objects`` collection.
    Only ``object_set`` and ``action_validation`` functions may load the
    release/type-scoped collection supplied by the caller.
    """
    objects: list[dict] = []
    if function_uses_object_collection(fn) and object_loader is not None:
        objects = [
            _expression_number(dict(item or {}))
            for item in object_loader()
        ]
    scope: dict[str, Any] = {
        "object": _expression_number(obj_props or {}),
        "params": _expression_number(params or {}),
        "objects": objects,
    }
    return scope


def evaluate_function_contract(
    fn: Any,
    *,
    obj_props: Optional[dict] = None,
    params: Optional[dict] = None,
    object_loader: ScopeObjectLoader | None = None,
) -> dict[str, Any]:
    """Evaluate one function definition without coupling it to a database.

    Both the live executor and version-trial projection call this function, so
    enabled/language checks, scope construction and action-validation result
    normalization cannot drift between the two paths.
    """
    label = str(
        getattr(fn, "display_name", None)
        or getattr(fn, "name", None)
        or getattr(fn, "id", None)
        or "(未命名函数)"
    )
    if not bool(getattr(fn, "enabled", True)):
        return {"success": False, "error": f'函数 "{label}" 已禁用'}

    language = str(
        getattr(fn, "language", None) or "expression"
    ).strip().lower()
    if language != "expression":
        # 后端不执行 TS，交给前端 functionEngine；这里给出明确契约
        message = (
            "TypeScript 函数在前端执行（请用图谱编辑页的函数测试器）；"
            "后端仅支持 expression 语言。"
            if language == "typescript"
            else f"后端不支持函数语言: {language}"
        )
        return {"success": False,
                "error": message,
                "clientSide": language == "typescript"}

    try:
        scope = build_function_scope(
            fn,
            obj_props=obj_props,
            params=params,
            object_loader=object_loader,
        )
        result = safe_eval(str(getattr(fn, "body", None) or ""), scope)

        if str(
            getattr(fn, "function_type", None) or "object"
        ).strip().lower() == "action_validation":
            # 规范化为 ValidationResult
            if isinstance(result, bool):
                result = {"valid": result, "errors": [] if result else ["校验失败"]}
            elif isinstance(result, dict) and isinstance(result.get("valid"), bool):
                result.setdefault("errors", [])
                if not isinstance(result.get("errors"), list):
                    return {
                        "success": False,
                        "error": "action_validation 的 errors 必须是数组",
                    }
            else:
                return {
                    "success": False,
                    "error": "action_validation 必须返回 bool 或包含布尔 valid 的对象",
                }

        return {"success": True, "result": result}
    except SafeEvalError as e:
        return {"success": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"success": False, "error": str(e)}


def execute_function(fn: OntologyFunction, db: Session, ontology_id: str,
                     obj_props: Optional[dict] = None,
                     params: Optional[dict] = None,
                     ontology_release_id: str | None = None) -> dict[str, Any]:
    """执行单个函数，返回 {success, result, error, durationMs, timestamp}（camelCase）"""
    start = time.time()
    ts = _now_iso()

    def load_scope_objects() -> list[dict]:
        q = db.query(ObjectInstance).filter(
            ObjectInstance.ontology_id == ontology_id)
        if ontology_release_id is not None:
            q = q.filter(
                ObjectInstance.ontology_release_id == ontology_release_id)
        if fn.target_object_type_id:
            q = q.filter(
                ObjectInstance.object_type_id == fn.target_object_type_id)
        return [dict(item.properties or {}) for item in q.all()]

    result = evaluate_function_contract(
        fn,
        obj_props=obj_props,
        params=params,
        object_loader=load_scope_objects,
    )
    result["durationMs"] = int((time.time() - start) * 1000)
    result["timestamp"] = ts
    if result.get("success"):
        result["data"] = result.get("result")
    return result


def compute_derived_properties(object_type, instance_props: dict, db: Session,
                               ontology_id: str) -> dict[str, Any]:
    """计算某对象实例的所有 computed 属性"""
    computed: dict[str, Any] = {}
    for prop in (object_type.properties or []):
        if prop.get("source") == "computed" and prop.get("functionId"):
            fn = db.query(OntologyFunction).filter(
                OntologyFunction.id == prop["functionId"],
                OntologyFunction.ontology_id == ontology_id,
            ).first()
            if fn:
                r = execute_function(fn, db, ontology_id, obj_props=instance_props)
                computed[prop["name"]] = r.get("result") if r.get("success") else f"#ERROR: {r.get('error')}"
    return computed


def compute_object_set_aggregates(db: Session, ontology_id: str,
                                  object_type_id: str) -> list[dict[str, Any]]:
    """某对象类型下所有启用的 object_set 函数的聚合结果（"集合指标"消费端）。

    语言分工与派生属性/校验函数一致：expression 在后端权威求值；
    typescript 返回 clientSide=True，交前端引擎用已加载实例计算。
    objects 作用域由 _build_scope 按 target_object_type_id 自动注入。
    """
    fns = (db.query(OntologyFunction)
           .filter(OntologyFunction.ontology_id == ontology_id,
                   OntologyFunction.function_type == "object_set",
                   OntologyFunction.target_object_type_id == object_type_id,
                   OntologyFunction.enabled.is_(True))
           .order_by(OntologyFunction.created_at.asc())
           .all())
    out: list[dict[str, Any]] = []
    for fn in fns:
        r = execute_function(fn, db, ontology_id)
        out.append({
            "functionId": fn.id,
            "name": fn.name,
            "displayName": fn.display_name,
            "returnType": fn.return_type,
            "language": fn.language,
            "success": bool(r.get("success")),
            "result": r.get("result"),
            "error": r.get("error"),
            "clientSide": bool(r.get("clientSide")),
            "durationMs": r.get("durationMs", 0),
        })
    return out


def test_function(db: Session, ontology_id: str, body) -> dict[str, Any]:
    """API 入口：测试一个函数 (body 是 TestFunctionRequest)"""
    fn = db.query(OntologyFunction).filter(
        OntologyFunction.id == body.function_id,
        OntologyFunction.ontology_id == ontology_id,
    ).first()
    if not fn:
        return {"success": False, "error": "函数不存在", "durationMs": 0, "timestamp": _now_iso()}

    obj_props = body.object_props
    if obj_props is None and body.object_instance_id:
        inst = db.query(ObjectInstance).filter(
            ObjectInstance.id == body.object_instance_id,
            ObjectInstance.ontology_id == ontology_id,
        ).first()
        obj_props = inst.properties if inst else None

    return execute_function(fn, db, ontology_id, obj_props=obj_props, params=body.params)
