"""Portable JSON contract for ontology structure export and import."""
from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

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
