"""Administrator directory contracts; never expose native credentials."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, SecretStr


class KnowledgeBaseRef(BaseModel):
    id: str
    name: str


class WeKnoraEndpointOut(BaseModel):
    id: str
    name: str
    enabled: bool
    knowledge_base_ids: list[str]
    knowledge_bases: list[KnowledgeBaseRef]
    tools: list[str]
    readonly: bool
    server_id: str | None = None
    connection_status: Literal[
        "not_connected", "ready", "disabled", "needs_verification",
        "unavailable", "authorization_required",
    ]
    message: str | None = None
    missing: bool = False


class WeKnoraDirectoryOut(BaseModel):
    endpoints: list[WeKnoraEndpointOut]
    initialized: bool
    message: str | None = None


class WeKnoraInitializeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recreate: bool = False


class WeKnoraConnectIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: SecretStr | None = None
    rotate_token: bool = False
