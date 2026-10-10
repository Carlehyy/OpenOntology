"""任务实例 — YAML WorkflowSpec 解析与编译（设计方案 §4）。

信封：api_version=openontology.task/v1 / kind=Workflow（无 legacy 双轨，
缺省即拒绝）。编译产物为归一化 canonical dict（排序键、显式默认值），
其 sha256 即 canonical_hash —— 内容不变不建新 revision（§4.5）。

语义校验（环/端口/契约引用等）不在本文件，见 validator.py；
本文件只做结构解析（pydantic，extra=forbid）与归一化。
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field


class _TemplateLoader(yaml.SafeLoader):
    """模板专用 Loader：规避 YAML 1.1 的 on/off/yes/no 布尔陷阱。

    condition 节点的上游声明键就是 ``on:``，默认 SafeLoader 会把它解析
    成布尔 True 导致整个 spec 拒载。这里收窄布尔隐式解析到 true/false
    （含大小写），on/off/yes/no 一律保留字符串。
    """


_TemplateLoader.yaml_implicit_resolvers = {
    first_char: [
        (tag, regexp)
        for tag, regexp in resolvers
        if tag != "tag:yaml.org,2002:bool"
    ]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_TemplateLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)

API_VERSION = "openontology.task/v1"
SPEC_KIND = "Workflow"

NODE_AGENT = "agent"
NODE_HUMAN = "human"
NODE_APPROVAL = "approval"
NODE_CONDITION = "condition"
NODE_JOIN = "join"
NODE_TERMINAL = "terminal"
NODE_KINDS = (
    NODE_AGENT,
    NODE_HUMAN,
    NODE_APPROVAL,
    NODE_CONDITION,
    NODE_JOIN,
    NODE_TERMINAL,
)

# 节点数 / 边数 / 契约数上限（防失控；设计方案 §4.1）
MAX_NODES = 200
MAX_EDGES = 1000
MAX_CONTRACTS = 128

DEFAULT_OUTPUT_PORT = "done"
APPROVAL_PORTS = ("approved", "rejected")


class SpecError(Exception):
    """YAML 结构不合法（语法 / 类型 / 越界）。语义错误走 validator。"""

    def __init__(self, message: str, *, line: int | None = None):
        self.line = line
        super().__init__(message)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class MetadataSpec(_Strict):
    name: str = Field(min_length=5, max_length=64)
    description: str | None = Field(default=None, max_length=2000)


class PortSpec(_Strict):
    """输出端口：契约可选（逐端口收紧），可声明必交产物字段名。"""

    contract: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=2000)
    required_artifacts: list[str] | None = Field(default=None, max_length=8)


class AgentNodeSpec(_Strict):
    kind: Literal["agent"]
    system: str = Field(min_length=1, max_length=20000)
    model: str | None = Field(default=None, max_length=64)
    timeout_minutes: int = Field(default=30, ge=1, le=1440)
    outputs: dict[str, PortSpec] | None = None


class HumanNodeSpec(_Strict):
    kind: Literal["human"]
    role: str = Field(min_length=1, max_length=500)
    deliverable: str | None = Field(default=None, max_length=2000)
    outputs: dict[str, PortSpec] | None = None


class ApprovalNodeSpec(_Strict):
    kind: Literal["approval"]
    approvers: list[str] = Field(min_length=1, max_length=16)
    expires_hours: int = Field(default=72, ge=1, le=720)


class PredicateSpec(_Strict):
    """确定性谓词：field 路径 + equals / in / matches 三选一。"""

    field: str = Field(min_length=1, max_length=200)
    equals: Any | None = None
    in_: list[Any] | None = Field(default=None, alias="in", max_length=64)
    matches: str | None = Field(default=None, max_length=256)


class ConditionBranchSpec(_Strict):
    when: PredicateSpec
    to: list[str] = Field(min_length=1, max_length=32)


class ConditionNodeSpec(_Strict):
    kind: Literal["condition"]
    on: str = Field(min_length=1, max_length=200)
    branches: list[ConditionBranchSpec] = Field(min_length=1, max_length=32)
    default: list[str] | None = Field(default=None, max_length=32)


class JoinNodeSpec(_Strict):
    kind: Literal["join"]
    mode: Literal["all", "any"] = "all"


class TerminalNodeSpec(_Strict):
    kind: Literal["terminal"]
    outcome: Literal["success", "failure"]


NodeSpec = Annotated[
    Union[
        AgentNodeSpec,
        HumanNodeSpec,
        ApprovalNodeSpec,
        ConditionNodeSpec,
        JoinNodeSpec,
        TerminalNodeSpec,
    ],
    Field(discriminator="kind"),
]


class EdgeSpec(_Strict):
    from_: str = Field(alias="from", min_length=1, max_length=200)
    to: str = Field(min_length=1, max_length=64)
    rework: bool = False


class PoliciesSpec(_Strict):
    max_parallelism: int = Field(default=8, ge=1, le=64)
    corrections_per_node: int = Field(default=2, ge=0, le=10)
    rework_per_edge: int = Field(default=3, ge=0, le=50)
    event_budget: int = Field(default=10000, ge=100, le=1000000)


class WorkflowSpec(_Strict):
    api_version: Literal["openontology.task/v1"]
    kind: Literal["Workflow"]
    metadata: MetadataSpec
    contracts: dict[str, dict] | None = None
    nodes: dict[str, NodeSpec]
    edges: list[EdgeSpec]
    policies: PoliciesSpec = PoliciesSpec()


class CompiledWorkflow(BaseModel):
    """编译产物：原 spec + canonical dict + canonical_hash。"""

    spec: WorkflowSpec
    canonical: dict[str, Any]
    canonical_hash: str


def parse_workflow_yaml(text: str) -> WorkflowSpec:
    """YAML 文本 → WorkflowSpec；语法/结构错误带行号（尽 YAML 所能）。"""
    try:
        raw = yaml.load(text, Loader=_TemplateLoader)
    except yaml.YAMLError as exc:
        line = None
        if hasattr(exc, "problem_mark") and exc.problem_mark is not None:
            line = exc.problem_mark.line + 1
        raise SpecError(f"YAML 语法错误: {exc}", line=line) from exc
    if not isinstance(raw, dict):
        raise SpecError("WorkflowSpec 必须是 YAML 映射（键值结构）")
    try:
        return WorkflowSpec.model_validate(raw)
    except Exception as exc:  # pydantic ValidationError
        raise SpecError(f"结构不合法: {exc}") from exc


def canonical_json(spec: WorkflowSpec) -> dict[str, Any]:
    """归一化：显式默认值填充 + 键排序（list 顺序保留：分支求值有序）。"""
    data = spec.model_dump(by_alias=True)
    if data.get("contracts") is None:
        data["contracts"] = {}
    for node_id, node in list(data["nodes"].items()):
        if node.get("outputs") is None:
            node["outputs"] = {}
    return data


def canonical_hash(canonical: dict[str, Any]) -> str:
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compile_workflow_yaml(text: str) -> CompiledWorkflow:
    spec = parse_workflow_yaml(text)
    canonical = canonical_json(spec)
    return CompiledWorkflow(
        spec=spec,
        canonical=canonical,
        canonical_hash=canonical_hash(canonical),
    )
