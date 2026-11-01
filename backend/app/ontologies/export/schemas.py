"""Portable JSON contract for ontology structure export and import."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from pydantic import ConfigDict, Field, field_validator

from app.ontologies.formal_modeling import schemas as formal_schemas

PORTABLE_MODEL_CONFIG = ConfigDict(
    alias_generator=formal_schemas._to_camel,
    populate_by_name=True,
    extra="forbid",
)


class PortableObjectType(formal_schemas.ObjectTypeBase):
    model_config = PORTABLE_MODEL_CONFIG
    id: str = Field(min_length=1, max_length=200)

    @field_validator("id", "name", "display_name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class PortableLinkType(formal_schemas.LinkTypeBase):
    model_config = PORTABLE_MODEL_CONFIG
    id: str = Field(min_length=1, max_length=200)

    @field_validator("id", "name", "display_name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class PortableActionType(formal_schemas.ActionTypeBase):
    model_config = PORTABLE_MODEL_CONFIG
    id: str = Field(min_length=1, max_length=200)

    @field_validator("id", "name", "display_name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class PortableFunction(formal_schemas.FunctionBase):
    model_config = PORTABLE_MODEL_CONFIG
    id: str = Field(min_length=1, max_length=200)

    @field_validator("id", "name", "display_name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class PortableSentinel(formal_schemas.CamelModel):
    """Sentinel engine definition carried by the structure package.

    Only structure semantics travel: the release it was bound to, its publish
    status, firing history, scan watermarks and enable generation are runtime
    state and are reconstructed (or reset) by the import path. ``enabled`` and
    ``muted`` are operational defaults that seed the imported live row.
    """

    model_config = PORTABLE_MODEL_CONFIG
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=10_000)
    bindings: list[dict[str, Any]] = Field(default_factory=list, max_length=200)
    links: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    condition: Optional[str] = Field(default=None, max_length=20_000)
    pattern: Optional[dict[str, Any]] = None
    condition_rows: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    condition_logic: Literal["and", "or"] = "and"
    primary_alias: Optional[str] = Field(default=None, max_length=50)
    action_ids: list[str] = Field(default_factory=list, max_length=100)
    action_parameters: dict[str, dict[str, Any]] = Field(default_factory=dict)
    on_change: bool = Field(default=True, strict=True)
    on_schedule: bool = Field(default=False, strict=True)
    scan_interval_seconds: int = Field(
        default=300, strict=True, ge=60, le=86_400)
    trigger_mode: Literal[
        "on_enter", "on_enter_leave", "run_on_all", "on_pattern"
    ] = "on_enter"
    muted: bool = Field(default=False, strict=True)
    enabled: bool = Field(default=True, strict=True)

    @field_validator("id", "name", "display_name")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class PortableOntologyMetadata(formal_schemas.CamelModel):
    model_config = PORTABLE_MODEL_CONFIG
    id: Optional[str] = Field(default=None, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    domain: str = Field(min_length=1, max_length=100)
    description: Optional[str] = Field(default=None, max_length=10_000)
    icon: Optional[str] = Field(default="network", max_length=50)
    source_version: Optional[str] = Field(default=None, max_length=20)
    source_status: Optional[str] = Field(default=None, max_length=20)

    @field_validator("name", "domain")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("icon")
    @classmethod
    def normalize_icon(cls, value: Optional[str]) -> str:
        return value.strip() if value and value.strip() else "network"


class PortableOntologyStructure(formal_schemas.CamelModel):
    model_config = PORTABLE_MODEL_CONFIG
    object_types: list[PortableObjectType] = Field(default_factory=list, max_length=5000)
    link_types: list[PortableLinkType] = Field(default_factory=list, max_length=10_000)
    actions: list[PortableActionType] = Field(default_factory=list, max_length=5000)
    functions: list[PortableFunction] = Field(default_factory=list, max_length=5000)
    # 哨兵引擎（第五类模型）是发布快照中的结构语义。旧格式包没有该字段，
    # 导入端按空集合处理，因此 format_version 仍保持 1。
    sentinels: list[PortableSentinel] = Field(default_factory=list, max_length=5000)


class OntologyStructurePackage(formal_schemas.CamelModel):
    """Versioned, structure-only package accepted by the local import API."""

    model_config = PORTABLE_MODEL_CONFIG

    format: Literal["ontology-structure"] = "ontology-structure"
    format_version: Literal[1] = 1
    exported_at: datetime
    ontology: PortableOntologyMetadata
    structure: PortableOntologyStructure
    # 画布布局是呈现层状态：key 为节点 ID（l1:/l2: 前缀或全屏编辑器的
    # 无前缀实体 ID），不含结构语义，缺失时导入端回退自动布局。旧格式
    # 包（无此字段）保持可导入，因此 format_version 仍为 1。
    canvas_layout: Optional[dict[str, dict[str, float]]] = Field(default=None)
