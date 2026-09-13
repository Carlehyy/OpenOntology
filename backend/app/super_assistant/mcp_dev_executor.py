"""自研 MCP（developed 传输）执行器 — 用户 Python 工具经 Jupyter 内核网关执行。

与推演服务/数据通道共用执行通道（data_channel python_engine client），输出
协议同样复用 ``__OB_RESULT_*__`` 标记。代码组装为三段：

  prelude  —— 平台注入的 ``@mcp_tool`` 注册器（从类型注解推导 input_schema）
              与 ``OB_ENV`` / ``OB_SECRET`` 个人变量（服务端解析、仅存在于
              本次执行的内核内存，不落库、不进任何快照或日志）；
  script   —— 用户脚本原样；
  epilogue —— 按场景注入：解析工具清单（introspect）、调用单个工具（call）
              或逐工具跑发布闸门样例（gates）。用户脚本抛错时
              stop_on_error 生效、epilogue 不会执行，错误经 iopub 回收。

密钥纪律：agent 运行链路的返回值会回灌模型上下文并落 ToolRun.result，
序列化前必须用 :func:`mask_secret_values` 把已知个人变量明文替换为 ``***``；
开发期试跑面向本人，原样返回。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.auth.crypto import decrypt_value
from app.auth.models import UserEnvVar, UserPrivacyVar
from app.data_channel.pipelines.python_engine.client import (
    PythonEngineError,
    execute_code,
    extract_payload,
    tail_stdout,
)

# 与 mcp_client.namespaced_tool_name 的公开名长度预算对齐：工具名本身保持
# Python 标识符，交给命名空间函数去截断加后缀
_TOOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_TOOL_NAME = 64
# 个人变量注入内核代码的序列化上限（50 键 × 4K 值的常态远低于此）
_PERSONAL_VARS_CHARS = 200_000
# agent 链路返回 JSON 的字符上限（runtime 截断之外的第二道闸，
# 防止超大 payload 在 masking 阶段反复全量替换拖垮会话）
_RUNTIME_OUTPUT_CHARS = 60_000
# 参与输出打码的密钥最小长度：过短的值（如 "1"）替换反而破坏正常内容
_MASK_MIN_LEN = 6


class McpDevExecutorError(Exception):
    """执行器输入侧错误（工具名非法/个人变量过大），message 面向用户。"""


@dataclass
class PersonalVars:
    env: dict[str, str]
    secret: dict[str, str]

    @property
    def all_values(self) -> list[str]:
        return [*self.env.values(), *self.secret.values()]


def load_personal_vars(db: Session, owner_id: str) -> PersonalVars:
    """解出当前用户全部环境/隐私变量明文（仅注入执行内存，不落库不回显）。"""
    env: dict[str, str] = {}
    for row in db.query(UserEnvVar).filter(UserEnvVar.user_id == owner_id).all():
        try:
            env[row.key] = decrypt_value(row.value_encrypted)
        except Exception:  # noqa: BLE001 — 解密失败按无值处理，脚本读到缺 key 自行报错
            continue
    secret: dict[str, str] = {}
    for row in db.query(UserPrivacyVar).filter(UserPrivacyVar.user_id == owner_id).all():
        try:
            secret[row.key] = decrypt_value(row.value_encrypted)
        except Exception:  # noqa: BLE001
            continue
    return PersonalVars(env=env, secret=secret)


def mask_secret_values(text: str, values: list[str]) -> str:
    """把已知个人变量明文从文本中替换为 ``***``（长值优先，避免部分命中）。"""
    for value in sorted({v for v in values if v and len(v) >= _MASK_MIN_LEN}, key=len, reverse=True):
        text = text.replace(value, "***")
    return text


# ──────────────────────────── 内核代码组装 ────────────────────────────

# 注册器以内核侧 Python 实现：input_schema 从函数签名推导（str/int/float/
# bool/dict/list/Optional 与 JSON 安全默认值），清单形状与导入 MCP 发现的
# tool_manifest 同构（name/description/input_schema），可直接进 agent 目录。
_PRELUDE = '''\
# ── OpenOntology MCP 开发平台注入（自动前置，请勿删除） ──
import inspect as _ob_inspect
import json as _ob_json
import time as _ob_time
import typing as _ob_typing


def _ob_param_schema(tp):
    origin = _ob_typing.get_origin(tp)
    if tp is str:
        return {"type": "string"}
    if tp is bool:
        return {"type": "boolean"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    if tp is dict or origin is dict:
        return {"type": "object"}
    if tp is list or origin is list:
        return {"type": "array"}
    if origin is _ob_typing.Union:
        args = [a for a in _ob_typing.get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return _ob_param_schema(args[0])
    return {}


class _ObToolRegistry:
    def __init__(self):
        self._tools = {}

    def tool(self, func=None, *, description=""):
        def _register(fn):
            # 用户脚本可能带 from __future__ import annotations（PEP 563），
            # 此时 __annotations__ 是字符串；get_type_hints 在函数 globals
            # 里解析回真实类型，解析失败再退回原始注解（schema 退化为无约束）
            try:
                hints = _ob_typing.get_type_hints(fn)
            except Exception:
                hints = dict(getattr(fn, "__annotations__", {}) or {})
            properties = {}
            required = []
            for pname, param in _ob_inspect.signature(fn).parameters.items():
                if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
                    continue
                annotation = hints.get(pname, param.annotation)
                schema = _ob_param_schema(annotation) if annotation is not param.empty else {}
                if param.default is param.empty:
                    required.append(pname)
                elif param.default is None or isinstance(param.default, (bool, int, float, str)):
                    schema = dict(schema)
                    schema["default"] = param.default
                properties[pname] = schema
            input_schema = {"type": "object", "properties": properties}
            if required:
                input_schema["required"] = required
            self._tools[fn.__name__] = {
                "fn": fn,
                "description": description or "",
                "input_schema": input_schema,
            }
            return fn

        if func is not None:
            return _register(func)
        return _register

    def manifest(self):
        return [
            {"name": name, "description": t["description"], "input_schema": t["input_schema"]}
            for name, t in self._tools.items()
        ]


ob = _ObToolRegistry()
mcp_tool = ob.tool
'''

_INTROSPECT_EPILOGUE = '''

# ── OpenOntology MCP 工具清单解析（自动注入，请勿删除） ──
print()
print("__OB_RESULT_BEGIN__")
print(_ob_json.dumps({"tools": ob.manifest()}, ensure_ascii=False, default=str))
print("__OB_RESULT_END__")
'''

_CALL_EPILOGUE_TEMPLATE = '''

# ── OpenOntology MCP 工具调用（自动注入，请勿删除） ──
_ob_name = {tool_name!r}
_ob_out = {{"ok": False, "error": "工具 " + _ob_name + " 未注册：请先在脚本中用 @mcp_tool 声明"}}
_ob_entry = ob._tools.get(_ob_name)
if _ob_entry is not None:
    _ob_t0 = _ob_time.monotonic()
    try:
        _ob_payload = _ob_entry["fn"](**_ob_json.loads({arguments!r}))
        _ob_out = {{"ok": True, "payload": _ob_payload, "duration_ms": int((_ob_time.monotonic() - _ob_t0) * 1000)}}
    except Exception as _ob_exc:
        import traceback as _ob_traceback
        _ob_out = {{
            "ok": False,
            "error": "{{}}: {{}}".format(type(_ob_exc).__name__, _ob_exc),
            "traceback": _ob_traceback.format_exc(),
            "duration_ms": int((_ob_time.monotonic() - _ob_t0) * 1000),
        }}
print()
print("__OB_RESULT_BEGIN__")
print(_ob_json.dumps(_ob_out, ensure_ascii=False, default=str))
print("__OB_RESULT_END__")
'''

# 发布闸门：逐工具以样例参数真实执行（用户明确要求的发布前检查），
# 单工具异常不中断其余工具，结果列表回传后由服务层判定与落库
_GATES_EPILOGUE_TEMPLATE = '''

# ── OpenOntology MCP 发布校验（自动注入，请勿删除） ──
_ob_gates = []
for _ob_name, _ob_args in _ob_json.loads({samples!r}).items():
    _ob_entry = {{"name": _ob_name, "ok": False, "error": ""}}
    _ob_t0 = _ob_time.monotonic()
    try:
        _ob_fn = ob._tools.get(_ob_name)
        if _ob_fn is None:
            _ob_entry["error"] = "工具未注册"
        else:
            _ob_fn["fn"](**(_ob_args or {{}}))
            _ob_entry["ok"] = True
    except Exception as _ob_exc:
        _ob_entry["error"] = "{{}}: {{}}".format(type(_ob_exc).__name__, _ob_exc)
    _ob_entry["duration_ms"] = int((_ob_time.monotonic() - _ob_t0) * 1000)
    _ob_gates.append(_ob_entry)
print()
print("__OB_RESULT_BEGIN__")
print(_ob_json.dumps({{"gates": _ob_gates}}, ensure_ascii=False, default=str))
print("__OB_RESULT_END__")
'''


def _personal_vars_segment(personal: PersonalVars) -> str:
    env_json = json.dumps(personal.env, ensure_ascii=False)
    secret_json = json.dumps(personal.secret, ensure_ascii=False)
    if len(env_json) + len(secret_json) > _PERSONAL_VARS_CHARS:
        raise McpDevExecutorError(
            "个人变量总量过大（200K 字符上限）：请到个人设置精简环境/隐私变量后重试。"
        )
    # repr() 产出合法 Python 字符串字面量（与推演服务测试入参同一纪律）
    return (
        "\n# ── 个人变量（服务端解析注入，仅本次执行可见） ──\n"
        f"OB_ENV = _ob_json.loads({env_json!r})\n"
        f"OB_SECRET = _ob_json.loads({secret_json!r})\n\n"
    )


def build_introspect_code(script: str, personal: PersonalVars) -> str:
    return _PRELUDE + _personal_vars_segment(personal) + script + _INTROSPECT_EPILOGUE


def build_call_code(script: str, personal: PersonalVars, tool_name: str, arguments: dict) -> str:
    _validate_tool_name(tool_name)
    arguments_json = json.dumps(arguments or {}, ensure_ascii=False, default=str)
    epilogue = _CALL_EPILOGUE_TEMPLATE.format(
        tool_name=tool_name,
        arguments=arguments_json,
    )
    return _PRELUDE + _personal_vars_segment(personal) + script + epilogue


def build_gates_code(script: str, personal: PersonalVars, samples: dict) -> str:
    samples_json = json.dumps(samples or {}, ensure_ascii=False, default=str)
    if len(samples_json) > _PERSONAL_VARS_CHARS:
        raise McpDevExecutorError("样例参数总量过大（200K 字符上限）：请精简后重试。")
    epilogue = _GATES_EPILOGUE_TEMPLATE.format(samples=samples_json)
    return _PRELUDE + _personal_vars_segment(personal) + script + epilogue


def _validate_tool_name(tool_name: str) -> None:
    if not tool_name or len(tool_name) > _MAX_TOOL_NAME or not _TOOL_NAME_RE.match(tool_name):
        raise McpDevExecutorError("工具名必须是 1-64 位的 Python 标识符（字母/下划线开头）。")


# ──────────────────────────── 执行入口 ────────────────────────────


@dataclass
class McpDevExecution:
    """一次内核执行的统一结果（payload 形态由调用场景解释）。"""

    ok: bool
    payload: object | None
    stdout: str
    error: str | None
    traceback: str
    duration_ms: int


def _run_code(code: str, *, timeout: int | None = None) -> McpDevExecution:
    """在内核上执行组装好的代码并解析标记协议；基础设施失败抛 502 语义异常。"""
    try:
        # full_stdout：结果块可能超过尾部截断上限，必须在完整 stdout 上解析
        execution = execute_code(code, timeout=timeout, full_stdout=True)
    except PythonEngineError:
        raise
    payload = None
    error = execution.error
    if not error:
        try:
            payload = extract_payload(execution.stdout)
        except PythonEngineError as exc:
            error = str(exc)
    return McpDevExecution(
        ok=error is None,
        payload=payload,
        stdout=tail_stdout(execution.stdout),
        error=error,
        traceback=execution.traceback,
        duration_ms=execution.duration_ms,
    )


def introspect(script: str, personal: PersonalVars) -> McpDevExecution:
    """执行脚本并回传工具清单 [{name, description, input_schema}]。"""
    if not script.strip():
        raise McpDevExecutorError("脚本内容为空，无法执行。")
    result = _run_code(build_introspect_code(script, personal))
    if result.ok and isinstance(result.payload, dict):
        result.payload = result.payload.get("tools") or []
    return result


def call_tool(
    script: str,
    personal: PersonalVars,
    tool_name: str,
    arguments: dict,
    *,
    timeout: int | None = None,
) -> McpDevExecution:
    """执行脚本并调用指定工具。payload 为 {ok, payload|error, traceback?, duration_ms}。"""
    if not script.strip():
        raise McpDevExecutorError("脚本内容为空，无法执行。")
    arguments_json = json.dumps(arguments or {}, ensure_ascii=False, default=str)
    if len(arguments_json) > _PERSONAL_VARS_CHARS:
        raise McpDevExecutorError("工具入参过大（200K 字符上限）。")
    return _run_code(
        build_call_code(script, personal, tool_name, arguments or {}),
        timeout=timeout,
    )


def run_gates(script: str, personal: PersonalVars, samples: dict) -> McpDevExecution:
    """以样例参数逐工具真实执行（发布校验）。payload 为 {gates: [...]}。"""
    result = _run_code(build_gates_code(script, personal, samples))
    if result.ok and isinstance(result.payload, dict):
        result.payload = result.payload.get("gates") or []
    return result


def serialize_for_runtime(result: McpDevExecution, personal: PersonalVars) -> str:
    """把执行结果序列化为 agent 工具输出：打码个人变量明文并限长。

    任何异常都收敛为 JSON 错误串——工具失败要进模型上下文让模型自行恢复，
    不允许把执行器异常抛穿 runtime 的工具循环。
    """
    try:
        if not result.ok:
            body = {"error": result.error or "工具执行失败", "traceback": result.traceback or ""}
        else:
            body = result.payload
        text = json.dumps(body, ensure_ascii=False, default=str)
        text = mask_secret_values(text, personal.all_values)
        if len(text) > _RUNTIME_OUTPUT_CHARS:
            text = text[:_RUNTIME_OUTPUT_CHARS] + "\n…[结果已截断]"
        return text
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"error": f"工具结果序列化失败：{exc}"}, ensure_ascii=False)
