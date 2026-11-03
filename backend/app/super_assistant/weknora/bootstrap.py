"""One-shot WeKnora initialisation for the shared knowledge base.

The temporary ``weknora-app-init`` container is the only WeKnora process that
ever runs with public registration enabled.  It shares the database with the
formal app but is never published through the gateway.  This module creates
the administrator, the chat/embedding model rows, the single graph-enabled
shared knowledge base and one read-only MCP endpoint, then writes the
integration state consumed by :mod:`app.super_assistant.weknora.service`.
The chat model remains the knowledge-base summary/extraction model and is
exercised by verify; the endpoint never publishes ``ask`` or any agent tool.

Run ``python -m app.super_assistant.weknora.bootstrap`` while the init app is
healthy.  After the formal app starts, run the same command with ``--verify``
to prove that registration is closed, the endpoint is still bound to exactly
one knowledge base and the MCP tool surface is readable.

This module deliberately does not import ``app.shared.config`` or the
WeKnora service at module import time: configuration comes from the
bootstrap variables and must fail closed on its own.  The read-only tool
allowlist comes from :mod:`app.super_assistant.weknora.policy`, which has no
settings or runtime side effects.  The MCP client's internal transport also
works without importing the platform's dependency configuration.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import errno
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from typing import Any, Mapping
from urllib.parse import urlsplit

import httpx

from app.super_assistant.weknora.policy import WEKNORA_ALLOWED_TOOLS, WEKNORA_REQUIRED_TOOLS


_MAX_STATE_BYTES = 1_048_576
_STATE_VERSION = 1

READ_TOOL_NAMES = WEKNORA_ALLOWED_TOOLS
READ_TOOLS = frozenset(READ_TOOL_NAMES)
REQUIRED_TOOLS = WEKNORA_REQUIRED_TOOLS
_WRITE_TOOLS = frozenset({"add_document", "update_document", "delete_document"})

_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}")
_TOKEN_RE = re.compile(r"mcp_[A-Za-z0-9_-]{16,256}")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
_ASCII_DIGIT_RE = re.compile(r"[0-9]")

_API_PREFIX = "/api/v1"
_REGISTRATION_SETTING_KEY = "auth.registration_mode"
_REGISTRATION_INVITE_ONLY = "invite_only"
_REGISTRATION_SELF_SERVE = "self_serve"

_DEFAULT_KB_NAME = "共享知识库"
_DEFAULT_TIMEOUT_SECONDS = 15
_MIN_EMBEDDING_DIMENSIONS = 1
_MAX_EMBEDDING_DIMENSIONS = 8192

_ENV_BASE_URL = "WEKNORA_BASE_URL"
_ENV_BOOTSTRAP_BASE_URL = "WEKNORA_BOOTSTRAP_BASE_URL"
_ENV_STATE_PATH = "WEKNORA_STATE_PATH"
_ENV_ADMIN_USER = "WEKNORA_ADMIN_USER"
_ENV_ADMIN_EMAIL = "WEKNORA_ADMIN_EMAIL"
_ENV_ADMIN_PASSWORD = "WEKNORA_ADMIN_PASSWORD"
_ENV_CHAT_BASE_URL = "WEKNORA_CHAT_BASE_URL"
_ENV_CHAT_API_KEY = "WEKNORA_CHAT_API_KEY"
_ENV_CHAT_MODEL = "WEKNORA_CHAT_MODEL"
_ENV_EMBEDDING_BASE_URL = "WEKNORA_EMBEDDING_BASE_URL"
_ENV_EMBEDDING_API_KEY = "WEKNORA_EMBEDDING_API_KEY"
_ENV_EMBEDDING_MODEL = "WEKNORA_EMBEDDING_MODEL"
_ENV_EMBEDDING_DIMENSIONS = "WEKNORA_EMBEDDING_DIMENSIONS"
_ENV_KB_NAME = "WEKNORA_KNOWLEDGE_BASE_NAME"
_ENV_TIMEOUT = "WEKNORA_TIMEOUT_SECONDS"

# The state fields the backend service requires.  ``mcp_token`` is written
# exactly once per creation or explicit rotation and is never logged.
_STATE_KEYS = (
    "version",
    "base_url",
    "mcp_endpoint_id",
    "mcp_token",
    "knowledge_base_id",
    "knowledge_base_name",
    "tools",
)


class BootstrapError(RuntimeError):
    """A safe, stable failure code.

    ``str(exc)`` is always the bare code.  Details are intentionally kept out
    of the message so an HTTP body, a password, an API key or a bearer token
    can never reach the container log.
    """

    def __init__(self, code: str, *, status: int | None = None,
                 detail: str = "") -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class BootstrapConfig:
    """Validated bootstrap inputs.

    ``repr`` omits every credential field so a dataclass dump cannot leak
    secrets.
    """

    base_url: str
    state_path: str
    admin_username: str
    admin_email: str
    admin_password: str = field(repr=False)
    chat_base_url: str
    chat_api_key: str = field(repr=False)
    chat_model: str
    embedding_base_url: str
    embedding_api_key: str = field(repr=False)
    embedding_model: str
    embedding_dimensions: int
    knowledge_base_name: str = _DEFAULT_KB_NAME
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS
    bootstrap_base_url: str | None = None

    @property
    def endpoint_name(self) -> str:
        return f"{self.knowledge_base_name} MCP"


@dataclass(frozen=True)
class _ModelSpec:
    name: str
    model_type: str
    base_url: str
    api_key: str = field(repr=False)
    dimensions: int | None = None

    @property
    def description(self) -> str:
        return "OpenOntology shared knowledge base model (bootstrap managed)"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _required(env: Mapping[str, str], names: list[str]) -> list[str]:
    missing = [name for name in names if not str(env.get(name) or "").strip()]
    if missing:
        raise BootstrapError(
            "WEKNORA_BOOTSTRAP_CONFIG_MISSING",
            detail="missing:" + ",".join(missing),
        )
    return missing


def _normalize_origin(value: object) -> str:
    """Return the canonical origin shared with ``service.origin``.

    Only scheme/host/port are accepted; credentials, paths, queries and
    fragments are rejected.  The host is lower-cased so two spellings of the
    same origin cannot produce two integration states.
    """
    raw = str(value or "")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("invalid origin") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in raw)
    ):
        raise ValueError("invalid origin")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    suffix = f":{port}" if port is not None else ""
    return f"{parsed.scheme}://{host}{suffix}"


def _normalize_api_url(value: object) -> str:
    """Validate an OpenAI-compatible base URL (a path such as /v1 is allowed)."""
    raw = str(value or "").rstrip("/")
    try:
        parsed = urlsplit(raw)
        parsed.port
    except ValueError as exc:
        raise ValueError("invalid api url") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in raw)
        or any(segment == ".." for segment in parsed.path.split("/"))
    ):
        raise ValueError("invalid api url")
    return raw


def _validate_admin_user(value: object) -> str:
    name = str(value or "").strip()
    if (
        not 2 <= len(name) <= 50
        or any(char.isspace() or not char.isprintable() for char in name)
    ):
        raise ValueError("invalid admin user")
    return name


def _validate_admin_email(value: object) -> str:
    email = str(value or "").strip()
    if not _EMAIL_RE.fullmatch(email):
        raise ValueError("invalid admin email")
    return email


def _validate_admin_password(value: object) -> str:
    """WeKnora's default policy: 8-32 characters with a letter and a digit."""
    password = str(value or "")
    if (
        not 8 <= len(password) <= 32
        or not _ASCII_LETTER_RE.search(password)
        or not _ASCII_DIGIT_RE.search(password)
        or any(char.isspace() or not char.isprintable() for char in password)
    ):
        raise ValueError("invalid admin password")
    return password


def _validate_model_name(value: object) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= 128 or any(
        char.isspace() or not char.isprintable() for char in name
    ):
        raise ValueError("invalid model name")
    return name


def _validate_kb_name(value: object) -> str:
    name = str(value or "").strip()
    if not 1 <= len(name) <= 255 or any(not char.isprintable() for char in name):
        raise ValueError("invalid knowledge base name")
    return name


def _validate_timeout(value: object) -> float:
    """Keep the bootstrap timeout in the same integer 3..60 window as settings."""
    raw = str(value or _DEFAULT_TIMEOUT_SECONDS).strip()
    if not raw.isdigit():
        raise ValueError("invalid timeout")
    timeout = int(raw)
    if not 3 <= timeout <= 60:
        raise ValueError("invalid timeout")
    return float(timeout)


def _validate_dimensions(value: object) -> int:
    raw = str(value or "").strip()
    if not raw.isdigit():
        raise ValueError("invalid dimensions")
    dimensions = int(raw)
    if not _MIN_EMBEDDING_DIMENSIONS <= dimensions <= _MAX_EMBEDDING_DIMENSIONS:
        raise ValueError("invalid dimensions")
    return dimensions


def _validate_state_path(value: object) -> str:
    raw = str(value or "").strip()
    path = Path(raw)
    if (
        not raw
        or "\x00" in raw
        or not path.is_absolute()
        or path.name in {"", ".", ".."}
        or ".." in path.parts
    ):
        raise ValueError("invalid state path")
    return raw


def build_config(env: Mapping[str, str], *, for_verify: bool = False) -> BootstrapConfig:
    """Build and validate the bootstrap configuration from *env*.

    Every credential, model endpoint and dimension is an explicit input.  A
    missing value is a hard failure: there is no implicit demo model, sample
    document or fallback password.
    """
    required = [
        _ENV_BASE_URL,
        _ENV_STATE_PATH,
        _ENV_ADMIN_USER,
        _ENV_ADMIN_EMAIL,
        _ENV_ADMIN_PASSWORD,
        _ENV_CHAT_BASE_URL,
        _ENV_CHAT_API_KEY,
        _ENV_CHAT_MODEL,
        _ENV_EMBEDDING_BASE_URL,
        _ENV_EMBEDDING_API_KEY,
        _ENV_EMBEDDING_MODEL,
        _ENV_EMBEDDING_DIMENSIONS,
    ]
    if not for_verify:
        required.insert(1, _ENV_BOOTSTRAP_BASE_URL)
    _required(env, required)
    try:
        return BootstrapConfig(
            base_url=_normalize_origin(env[_ENV_BASE_URL]),
            bootstrap_base_url=(
                None if for_verify else _normalize_origin(env[_ENV_BOOTSTRAP_BASE_URL])
            ),
            state_path=_validate_state_path(env[_ENV_STATE_PATH]),
            admin_username=_validate_admin_user(env[_ENV_ADMIN_USER]),
            admin_email=_validate_admin_email(env[_ENV_ADMIN_EMAIL]),
            admin_password=_validate_admin_password(env[_ENV_ADMIN_PASSWORD]),
            chat_base_url=_normalize_api_url(env[_ENV_CHAT_BASE_URL]),
            chat_api_key=str(env[_ENV_CHAT_API_KEY]).strip(),
            chat_model=_validate_model_name(env[_ENV_CHAT_MODEL]),
            embedding_base_url=_normalize_api_url(env[_ENV_EMBEDDING_BASE_URL]),
            embedding_api_key=str(env[_ENV_EMBEDDING_API_KEY]).strip(),
            embedding_model=_validate_model_name(env[_ENV_EMBEDDING_MODEL]),
            embedding_dimensions=_validate_dimensions(env[_ENV_EMBEDDING_DIMENSIONS]),
            knowledge_base_name=(
                _validate_kb_name(env.get(_ENV_KB_NAME) or _DEFAULT_KB_NAME)
            ),
            timeout_seconds=_validate_timeout(
                env.get(_ENV_TIMEOUT) or _DEFAULT_TIMEOUT_SECONDS
            ),
        )
    except ValueError:
        raise BootstrapError("WEKNORA_BOOTSTRAP_CONFIG_INVALID") from None


def load_config(*, for_verify: bool = False) -> BootstrapConfig:
    return build_config(os.environ, for_verify=for_verify)


# ---------------------------------------------------------------------------
# State file handling
# ---------------------------------------------------------------------------


def _state_path(config: BootstrapConfig) -> Path:
    try:
        raw = _validate_state_path(config.state_path)
    except ValueError:
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_UNSAFE") from None
    return Path(raw)


def _ensure_parent(path: Path) -> None:
    """Create missing parents and reject a symlinked or non-directory parent."""
    parent = path.parent
    missing: list[Path] = []
    current = parent
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            if current == current.parent:
                raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_UNSAFE") from None
            missing.append(current)
            current = current.parent
            continue
        if stat.S_ISLNK(info.st_mode):
            raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_UNSAFE")
        if not stat.S_ISDIR(info.st_mode):
            raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_UNSAFE")
        break
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        info = os.lstat(directory)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_UNSAFE")


def _read_state_file(path: Path, *, required: bool) -> bytes | None:
    parent = path.parent
    try:
        parent_info = os.lstat(parent)
    except FileNotFoundError:
        if required:
            raise BootstrapError("WEKNORA_BOOTSTRAP_NOT_INITIALIZED")
        return None
    if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_UNSAFE")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        if required:
            raise BootstrapError("WEKNORA_BOOTSTRAP_NOT_INITIALIZED")
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_UNSAFE") from None
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_INVALID") from None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_UNSAFE")
        if info.st_size > _MAX_STATE_BYTES:
            raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_TOO_LARGE")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            raw = handle.read(_MAX_STATE_BYTES + 1)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw) > _MAX_STATE_BYTES:
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_TOO_LARGE")
    return raw


def _validate_tools(tools: object, *, code_prefix: str = "WEKNORA_BOOTSTRAP") -> list[dict]:
    if not isinstance(tools, list):
        raise BootstrapError(f"{code_prefix}_INVALID_TOOLS")
    result: list[dict] = []
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            raise BootstrapError(f"{code_prefix}_INVALID_TOOLS")
        name = tool.get("name")
        if not isinstance(name, str) or name not in READ_TOOLS:
            raise BootstrapError(f"{code_prefix}_UNEXPECTED_TOOL")
        if name in names:
            raise BootstrapError(f"{code_prefix}_INVALID_TOOLS")
        schema = tool.get("input_schema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise BootstrapError(f"{code_prefix}_INVALID_TOOL_SCHEMA")
        names.add(name)
        result.append({
            "name": name,
            "description": str(tool.get("description") or ""),
            "input_schema": schema,
        })
    if not REQUIRED_TOOLS.issubset(names):
        raise BootstrapError(f"{code_prefix}_REQUIRED_TOOLS_MISSING")
    return result


def _validate_state(state: object, config: BootstrapConfig) -> dict:
    if not isinstance(state, dict):
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_INVALID")
    if type(state.get("version")) is not int or state.get("version") != _STATE_VERSION:
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_INVALID")
    for key in _STATE_KEYS:
        if key not in state:
            raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_INVALID")
    try:
        stored_origin = _normalize_origin(state.get("base_url"))
    except ValueError:
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_INVALID") from None
    endpoint_id = state.get("mcp_endpoint_id")
    token = state.get("mcp_token")
    kb_id = state.get("knowledge_base_id")
    kb_name = state.get("knowledge_base_name")
    if (
        not isinstance(endpoint_id, str) or not _ID_RE.fullmatch(endpoint_id)
        or not isinstance(token, str) or not _TOKEN_RE.fullmatch(token)
        or not isinstance(kb_id, str) or not _ID_RE.fullmatch(kb_id)
        or not isinstance(kb_name, str) or not kb_name.strip()
        or len(kb_name) > 255
    ):
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_INVALID")
    _validate_tools(state.get("tools"), code_prefix="WEKNORA_BOOTSTRAP_STATE")
    if "verified" in state and not isinstance(state.get("verified"), bool):
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_INVALID")
    for key in ("chat_model_id", "embedding_model_id", "admin_user_id"):
        if key in state and (
            not isinstance(state.get(key), str) or not _ID_RE.fullmatch(state[key])
        ):
            raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_INVALID")
    if stored_origin != config.base_url:
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_ORIGIN_MISMATCH")
    if kb_name != config.knowledge_base_name:
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_INCONSISTENT")
    return state


def _load_state(config: BootstrapConfig, *, required: bool) -> dict | None:
    path = _state_path(config)
    raw = _read_state_file(path, required=required)
    if raw is None:
        return None
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_INVALID") from None
    return _validate_state(payload, config)


def _write_state(config: BootstrapConfig, state: dict) -> None:
    path = _state_path(config)
    payload = (
        json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    ).encode("utf-8")
    if len(payload) > _MAX_STATE_BYTES:
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_TOO_LARGE")
    _ensure_parent(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        info = None
    if info is not None and (
        stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode)
    ):
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_UNSAFE")

    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary_path: Path | None = Path(temporary)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        os.chmod(path, 0o600)
        try:
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# HTTP API helpers
# ---------------------------------------------------------------------------


def _client(config: BootstrapConfig, origin: str,
            transport: httpx.BaseTransport | None) -> httpx.Client:
    return httpx.Client(
        base_url=origin,
        timeout=config.timeout_seconds,
        follow_redirects=False,
        trust_env=False,
        transport=transport,
    )


def _request(client: httpx.Client, method: str, path: str, *,
             token: str | None = None, body: dict | None = None,
             form: dict[str, str] | None = None) -> httpx.Response:
    if body is not None and form is not None:
        raise ValueError("body and form are mutually exclusive")
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        return client.request(method, path, json=body, data=form, headers=headers)
    except httpx.HTTPError:
        raise BootstrapError("WEKNORA_BOOTSTRAP_HTTP_UNAVAILABLE") from None


def _payload(response: httpx.Response, *, expected: tuple[int, ...]) -> dict:
    if response.status_code not in expected:
        raise BootstrapError(
            "WEKNORA_BOOTSTRAP_HTTP_FAILED", status=response.status_code
        )
    try:
        payload = response.json()
    except ValueError:
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_RESPONSE") from None
    if not isinstance(payload, dict):
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_RESPONSE")
    return payload


def _list_data(response: httpx.Response) -> list[dict]:
    # WeKnora v0.9.0's model, knowledge-base and MCP-endpoint list handlers
    # return the complete array in ``data`` (no cursor/page envelope).  Reject
    # any other shape instead of silently reading a paginated object as empty.
    payload = _payload(response, expected=(200,))
    data = payload.get("data")
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_RESPONSE")
    return data


def _dict_data(response: httpx.Response, *, expected: tuple[int, ...] = (200, 201)) -> dict:
    payload = _payload(response, expected=expected)
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_RESPONSE")
    return data


def _auth_config(client: httpx.Client) -> str:
    payload = _payload(
        _request(client, "GET", f"{_API_PREFIX}/auth/config"), expected=(200,)
    )
    mode = payload.get("registration_mode")
    if not isinstance(mode, str):
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_RESPONSE")
    return mode


def _login(client: httpx.Client, config: BootstrapConfig) -> tuple[str, dict] | None:
    response = _request(
        client, "POST", f"{_API_PREFIX}/auth/login",
        body={"email": config.admin_email, "password": config.admin_password},
    )
    if response.status_code == 401:
        return None
    payload = _payload(response, expected=(200,))
    if payload.get("success") is not True:
        return None
    token = payload.get("token")
    user = payload.get("user")
    if not isinstance(token, str) or not token or not isinstance(user, dict):
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_RESPONSE")
    return token, user


def _register(client: httpx.Client, config: BootstrapConfig) -> None:
    response = _request(
        client, "POST", f"{_API_PREFIX}/auth/register",
        body={
            "username": config.admin_username,
            "email": config.admin_email,
            "password": config.admin_password,
        },
    )
    if response.status_code != 201:
        raise BootstrapError(
            "WEKNORA_BOOTSTRAP_REGISTRATION_FAILED", status=response.status_code
        )


def _authenticate(client: httpx.Client, config: BootstrapConfig, *,
                  register_allowed: bool) -> tuple[str, dict]:
    result = _login(client, config)
    if result is not None:
        return result
    if not register_allowed:
        raise BootstrapError("WEKNORA_BOOTSTRAP_LOGIN_FAILED")
    if _auth_config(client) != _REGISTRATION_SELF_SERVE:
        raise BootstrapError("WEKNORA_BOOTSTRAP_REGISTRATION_CLOSED")
    _register(client, config)
    result = _login(client, config)
    if result is None:
        raise BootstrapError("WEKNORA_BOOTSTRAP_REGISTRATION_FAILED")
    return result


def _user_identity(client: httpx.Client, token: str, user: dict) -> tuple[str, bool]:
    identifier = user.get("id")
    is_admin = user.get("is_system_admin")
    if not isinstance(identifier, str) or not _ID_RE.fullmatch(identifier):
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_RESPONSE")
    if not isinstance(is_admin, bool):
        payload = _payload(
            _request(client, "GET", f"{_API_PREFIX}/auth/me", token=token),
            expected=(200,),
        )
        data = payload.get("data")
        current = data.get("user") if isinstance(data, dict) else None
        if not isinstance(current, dict) or not isinstance(current.get("is_system_admin"), bool):
            raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_RESPONSE")
        is_admin = current["is_system_admin"]
    return identifier, is_admin


def _list_models(client: httpx.Client, token: str) -> list[dict]:
    return _list_data(
        _request(client, "GET", f"{_API_PREFIX}/models", token=token)
    )


def _create_model(client: httpx.Client, token: str, spec: _ModelSpec) -> dict:
    parameters: dict[str, Any] = {
        "base_url": spec.base_url,
        "api_key": spec.api_key,
        "provider": "generic",
        "interface_type": "openai",
    }
    if spec.dimensions is not None:
        parameters["embedding_parameters"] = {"dimension": spec.dimensions}
    body = {
        "name": spec.name,
        "display_name": spec.name,
        "type": spec.model_type,
        "source": "remote",
        "description": spec.description,
        "parameters": parameters,
    }
    return _dict_data(
        _request(client, "POST", f"{_API_PREFIX}/models", token=token, body=body),
        expected=(201,),
    )


def _same_url(left: object, right: object) -> bool:
    return str(left or "").rstrip("/") == str(right or "").rstrip("/")


def _model_matches(model: dict, spec: _ModelSpec) -> bool:
    if model.get("name") != spec.name or model.get("type") != spec.model_type:
        return False
    if model.get("source") != "remote":
        return False
    status = model.get("status")
    if status not in (None, "", "active"):
        return False
    parameters = model.get("parameters")
    if not isinstance(parameters, dict):
        return False
    if parameters.get("provider") != "generic":
        return False
    if parameters.get("interface_type") != "openai":
        return False
    if not _same_url(parameters.get("base_url"), spec.base_url):
        return False
    if spec.dimensions is not None:
        embedding = parameters.get("embedding_parameters")
        if not isinstance(embedding, dict):
            return False
        try:
            if int(embedding.get("dimension")) != spec.dimensions:
                return False
        except (TypeError, ValueError):
            return False
    return True


def _locate_model(models: list[dict], spec: _ModelSpec,
                  existing_id: str | None) -> dict | None:
    if existing_id is not None:
        rows = [model for model in models if model.get("id") == existing_id]
        if len(rows) != 1 or not _model_matches(rows[0], spec):
            raise BootstrapError("WEKNORA_BOOTSTRAP_MODEL_CONFLICT")
        return rows[0]
    named = [
        model for model in models
        if model.get("name") == spec.name and model.get("type") == spec.model_type
    ]
    if not named:
        return None
    if len(named) != 1 or not _model_matches(named[0], spec):
        raise BootstrapError("WEKNORA_BOOTSTRAP_MODEL_CONFLICT")
    return named[0]


def _debug_model(client: httpx.Client, token: str, model_id: str,
                 text: str) -> dict:
    """Run one saved model through the official debug endpoint.

    The debug handler reads ``application/x-www-form-urlencoded`` fields via
    ``c.PostForm`` (not JSON), uses the model's saved credentials, and always
    answers 200 with ``{"success": true, "data": {"ok": ...}}``.  The response
    body is inspected only for the fields needed here and never logged.
    """
    payload = _payload(
        _request(
            client, "POST",
            f"{_API_PREFIX}/models/{model_id}/debug",
            token=token,
            form={"input": text},
        ),
        expected=(200,),
    )
    if payload.get("success") is not True:
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_MODEL_DEBUG_RESPONSE")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_MODEL_DEBUG_RESPONSE")
    return data


def _verify_chat_model(client: httpx.Client, token: str,
                       model_id: str) -> None:
    data = _debug_model(client, token, model_id, "ping")
    if data.get("ok") is not True:
        raise BootstrapError("WEKNORA_BOOTSTRAP_CHAT_MODEL_SMOKE_FAILED")
    raw = data.get("raw_response")
    if not isinstance(raw, dict):
        raise BootstrapError("WEKNORA_BOOTSTRAP_CHAT_MODEL_SMOKE_FAILED")
    content = raw.get("content")
    if not isinstance(content, str) or not content.strip():
        raise BootstrapError("WEKNORA_BOOTSTRAP_CHAT_MODEL_SMOKE_FAILED")


def _verify_embedding_model(client: httpx.Client, token: str, model_id: str,
                            expected_dimensions: int) -> None:
    data = _debug_model(client, token, model_id, "hello")
    if data.get("ok") is not True:
        raise BootstrapError("WEKNORA_BOOTSTRAP_EMBEDDING_MODEL_SMOKE_FAILED")
    observations = data.get("observations")
    if not isinstance(observations, dict):
        raise BootstrapError("WEKNORA_BOOTSTRAP_EMBEDDING_DIMENSION_MISMATCH")
    dimension = observations.get("dimension")
    # bool is a subclass of int in Python but not a valid dimension.
    if type(dimension) is not int or dimension != expected_dimensions:
        raise BootstrapError("WEKNORA_BOOTSTRAP_EMBEDDING_DIMENSION_MISMATCH")


def _list_knowledge_bases(client: httpx.Client, token: str) -> list[dict]:
    return _list_data(
        _request(client, "GET", f"{_API_PREFIX}/knowledge-bases", token=token)
    )


def _extract_config() -> dict:
    """Deterministic, harmless few-shot graph extraction configuration."""
    return {
        "enabled": True,
        "text": (
            "示例：示例团队维护订单系统。订单系统依赖客户中心，"
            "并产生营收指标。"
        ),
        "tags": ["团队", "系统", "指标"],
        "nodes": [
            {"name": "示例团队", "attributes": ["负责方"]},
            {"name": "订单系统", "attributes": ["业务系统"]},
            {"name": "客户中心", "attributes": ["业务系统"]},
            {"name": "营收指标", "attributes": ["经营指标"]},
        ],
        "relations": [
            {"node1": "示例团队", "node2": "订单系统", "type": "维护"},
            {"node1": "订单系统", "node2": "客户中心", "type": "依赖"},
            {"node1": "订单系统", "node2": "营收指标", "type": "产生"},
            {"node1": "示例团队", "node2": "营收指标", "type": "关注"},
        ],
    }


def _create_knowledge_base(client: httpx.Client, token: str, config: BootstrapConfig,
                           embedding_id: str, summary_id: str) -> dict:
    body = {
        "name": config.knowledge_base_name,
        "type": "document",
        "description": "OpenOntology 平台共享知识库（bootstrap 初始化）",
        "embedding_model_id": embedding_id,
        "summary_model_id": summary_id,
        "indexing_strategy": {
            "vector_enabled": True,
            "keyword_enabled": True,
            "wiki_enabled": False,
            "graph_enabled": True,
        },
        "extract_config": _extract_config(),
    }
    return _dict_data(
        _request(
            client, "POST", f"{_API_PREFIX}/knowledge-bases", token=token, body=body,
        ),
        expected=(201,),
    )


def _check_kb(kb: dict, config: BootstrapConfig, embedding_id: str) -> None:
    if kb.get("name") != config.knowledge_base_name:
        raise BootstrapError("WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_CONFLICT")
    if (kb.get("type") or "document") != "document":
        raise BootstrapError("WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_CONFLICT")
    if kb.get("embedding_model_id") != embedding_id:
        raise BootstrapError("WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_CONFLICT")
    strategy = kb.get("indexing_strategy")
    if not isinstance(strategy, dict):
        raise BootstrapError("WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_CONFLICT")
    if strategy.get("vector_enabled") is not True or strategy.get("keyword_enabled") is not True:
        raise BootstrapError("WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_CONFLICT")
    extract = kb.get("extract_config")
    if (
        strategy.get("graph_enabled") is not True
        or not isinstance(extract, dict)
        or extract.get("enabled") is not True
        or not str(extract.get("text") or "").strip()
        or not isinstance(extract.get("tags"), list) or not extract["tags"]
        or not isinstance(extract.get("nodes"), list) or not extract["nodes"]
        or not isinstance(extract.get("relations"), list) or not extract["relations"]
    ):
        raise BootstrapError("WEKNORA_BOOTSTRAP_GRAPH_DISABLED")


def _locate_kb(kbs: list[dict], config: BootstrapConfig, embedding_id: str,
               existing_id: str | None) -> dict | None:
    if existing_id is not None:
        rows = [kb for kb in kbs if kb.get("id") == existing_id]
        if len(rows) != 1:
            raise BootstrapError("WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_UNAVAILABLE")
        _check_kb(rows[0], config, embedding_id)
        return rows[0]
    named = [kb for kb in kbs if kb.get("name") == config.knowledge_base_name]
    if not named:
        return None
    if len(named) != 1:
        raise BootstrapError("WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_CONFLICT")
    _check_kb(named[0], config, embedding_id)
    return named[0]


def _list_endpoints(client: httpx.Client, token: str) -> list[dict]:
    return _list_data(
        _request(client, "GET", f"{_API_PREFIX}/mcp-endpoints", token=token)
    )


def _endpoint_matches(endpoint: dict, config: BootstrapConfig, kb_id: str) -> bool:
    kb_ids = endpoint.get("knowledge_base_ids")
    tools = endpoint.get("tools")
    return (
        endpoint.get("name") == config.endpoint_name
        and endpoint.get("enabled") is True
        and isinstance(kb_ids, list) and kb_ids == [kb_id]
        and isinstance(tools, list)
        and len(tools) == len(READ_TOOL_NAMES)
        and set(tools) == READ_TOOLS
        and str(endpoint.get("default_agent_id") or "") == ""
    )


def _locate_endpoint(endpoints: list[dict], config: BootstrapConfig,
                     kb_id: str, existing_id: str | None) -> dict | None:
    named = [
        endpoint for endpoint in endpoints
        if endpoint.get("name") == config.endpoint_name
    ]
    if len(named) > 1:
        raise BootstrapError("WEKNORA_BOOTSTRAP_DUPLICATE_ENDPOINT")
    if existing_id is not None:
        rows = [endpoint for endpoint in endpoints if endpoint.get("id") == existing_id]
        if len(rows) != 1:
            raise BootstrapError("WEKNORA_BOOTSTRAP_ENDPOINT_UNAVAILABLE")
        if not _endpoint_matches(rows[0], config, kb_id):
            raise BootstrapError("WEKNORA_BOOTSTRAP_ENDPOINT_CONFLICT")
        return rows[0]
    if not named:
        return None
    if not _endpoint_matches(named[0], config, kb_id):
        raise BootstrapError("WEKNORA_BOOTSTRAP_ENDPOINT_CONFLICT")
    return named[0]


def _create_endpoint(client: httpx.Client, token: str, config: BootstrapConfig,
                     kb_id: str) -> tuple[dict, str]:
    body = {
        "name": config.endpoint_name,
        "description": "OpenOntology 共享知识库只读 MCP 端点",
        "enabled": True,
        "knowledge_base_ids": [kb_id],
        "tools": list(READ_TOOL_NAMES),
        "default_agent_id": "",
    }
    endpoint = _dict_data(
        _request(
            client, "POST", f"{_API_PREFIX}/mcp-endpoints", token=token, body=body,
        ),
        expected=(201,),
    )
    if not _endpoint_matches(endpoint, config, kb_id):
        raise BootstrapError("WEKNORA_BOOTSTRAP_ENDPOINT_CONFLICT")
    return endpoint, _endpoint_token(endpoint)


def _rotate_endpoint(client: httpx.Client, token: str, endpoint_id: str) -> tuple[dict, str]:
    endpoint = _dict_data(
        _request(
            client, "POST",
            f"{_API_PREFIX}/mcp-endpoints/{endpoint_id}/rotate-token",
            token=token,
        ),
        expected=(200,),
    )
    return endpoint, _endpoint_token(endpoint)


def _endpoint_token(endpoint: dict) -> str:
    token = endpoint.get("token")
    if not isinstance(token, str) or not _TOKEN_RE.fullmatch(token):
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_RESPONSE")
    return token


def _set_registration_invite_only(client: httpx.Client, token: str) -> None:
    _payload(
        _request(
            client, "PUT",
            f"{_API_PREFIX}/system/admin/settings/{_REGISTRATION_SETTING_KEY}",
            token=token,
            body={"value": _REGISTRATION_INVITE_ONLY},
        ),
        expected=(200,),
    )


# ---------------------------------------------------------------------------
# MCP verification
# ---------------------------------------------------------------------------


def _default_mcp() -> Any:
    # Imported lazily: mcp_client imports app.shared.config, while the init
    # container only guarantees the bootstrap variables.
    from app.super_assistant import mcp_client

    return mcp_client


def _mcp_url(origin: str, endpoint_id: str) -> str:
    return f"{origin}/mcp/{endpoint_id}"


async def _discover_tools(mcp: Any, origin: str, endpoint_id: str,
                          token: str) -> list[dict]:
    client = mcp if mcp is not None else _default_mcp()
    try:
        tools = await client.discover_tools(
            transport="streamable_http",
            url=_mcp_url(origin, endpoint_id),
            headers={"Authorization": f"Bearer {token}"},
            internal_base_url=origin,
        )
    except Exception:
        raise BootstrapError("WEKNORA_BOOTSTRAP_MCP_UNAVAILABLE") from None
    return _validate_tools(tools)


def _decode_tool_result(raw: object) -> dict:
    if not isinstance(raw, str):
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_KNOWLEDGE_BASE_RESPONSE")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_KNOWLEDGE_BASE_RESPONSE") from None
    if not isinstance(payload, dict):
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_KNOWLEDGE_BASE_RESPONSE")
    return payload


def _knowledge_base_ids(raw: object) -> list[str]:
    """Read ids from the MCP ``CallToolResult``, never from loose text.

    ``list_knowledge_bases`` returns ``{"knowledge_bases": [...], "total": N}``
    as structured content, with the same JSON mirrored in one text item.
    Anything else — a plain sentence that happens to mention the id, a nested
    unrelated object, or a malformed row — is rejected instead of being
    treated as proof of scope.
    """
    payload = _decode_tool_result(raw)
    if payload.get("isError") is True:
        raise BootstrapError("WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_UNAVAILABLE")
    structured = payload.get("structuredContent")
    if not isinstance(structured, dict):
        structured = {}
        content = payload.get("content")
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict) or item.get("type") != "text":
                    continue
                text = item.get("text")
                if not isinstance(text, str):
                    continue
                try:
                    nested = json.loads(text)
                except ValueError:
                    continue
                if isinstance(nested, dict) and "knowledge_bases" in nested:
                    structured = nested
                    break
    rows = structured.get("knowledge_bases")
    if not isinstance(rows, list):
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_KNOWLEDGE_BASE_RESPONSE")
    identifiers: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_KNOWLEDGE_BASE_RESPONSE")
        identifier = row.get("id")
        if not isinstance(identifier, str) or not _ID_RE.fullmatch(identifier):
            raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_KNOWLEDGE_BASE_RESPONSE")
        identifiers.append(identifier)
    return identifiers


async def _mcp_lists_knowledge_base(mcp: Any, origin: str, endpoint_id: str,
                                    token: str, kb_id: str) -> None:
    client = mcp if mcp is not None else _default_mcp()
    try:
        raw = await client.call_tool(
            transport="streamable_http",
            url=_mcp_url(origin, endpoint_id),
            headers={"Authorization": f"Bearer {token}"},
            tool_name="list_knowledge_bases",
            arguments={},
            internal_base_url=origin,
        )
    except Exception:
        raise BootstrapError("WEKNORA_BOOTSTRAP_MCP_UNAVAILABLE") from None
    identifiers = _knowledge_base_ids(raw)
    if not identifiers or kb_id not in identifiers:
        raise BootstrapError("WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_UNAVAILABLE")
    if identifiers != [kb_id]:
        raise BootstrapError("WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_SCOPE_CHANGED")


# ---------------------------------------------------------------------------
# State composition and lifecycle
# ---------------------------------------------------------------------------


def _safe_id(value: object) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_RESPONSE")
    return value


def _build_state(config: BootstrapConfig, *, chat: dict, embedding: dict,
                 kb: dict, endpoint_id: str, token: str, tools: list[dict],
                 user_id: str, previous: dict | None,
                 verified: bool) -> dict:
    state = dict(previous or {})
    state.update({
        "version": _STATE_VERSION,
        "base_url": config.base_url,
        "mcp_endpoint_id": _safe_id(endpoint_id),
        "mcp_token": token,
        "knowledge_base_id": _safe_id(kb.get("id")),
        "knowledge_base_name": config.knowledge_base_name,
        "tools": tools,
        "chat_model_id": _safe_id(chat.get("id")),
        "embedding_model_id": _safe_id(embedding.get("id")),
        "admin_user_id": _safe_id(user_id),
        "verified": verified,
    })
    return state


def _model_specs(config: BootstrapConfig) -> tuple[_ModelSpec, _ModelSpec]:
    chat = _ModelSpec(
        name=config.chat_model,
        model_type="KnowledgeQA",
        base_url=config.chat_base_url,
        api_key=config.chat_api_key,
    )
    embedding = _ModelSpec(
        name=config.embedding_model,
        model_type="Embedding",
        base_url=config.embedding_base_url,
        api_key=config.embedding_api_key,
        dimensions=config.embedding_dimensions,
    )
    return chat, embedding


def _state_admin_matches(state: dict, user_id: str) -> None:
    stored = state.get("admin_user_id")
    if stored is not None and stored != user_id:
        raise BootstrapError("WEKNORA_BOOTSTRAP_STATE_INCONSISTENT")


async def initialize(config: BootstrapConfig, *,
                     transport: httpx.BaseTransport | None = None,
                     mcp: Any | None = None) -> dict:
    """Create or resume the integration.  Resource creation is idempotent.

    The state is written with ``verified=false``; only :func:`verify` marks a
    successfully verified integration as trusted.
    """
    if config.bootstrap_base_url is None:
        raise BootstrapError("WEKNORA_BOOTSTRAP_CONFIG_INVALID")
    state = _load_state(config, required=False)
    with _client(config, config.bootstrap_base_url, transport) as client:
        token, user = _authenticate(client, config, register_allowed=state is None)
        user_id, _ = _user_identity(client, token, user)
        if state is not None:
            _state_admin_matches(state, user_id)
        chat_spec, embedding_spec = _model_specs(config)

        models = _list_models(client, token)
        chat = _locate_model(models, chat_spec,
                             state.get("chat_model_id") if state else None)
        if chat is None:
            chat = _create_model(client, token, chat_spec)
            models.append(chat)
        embedding = _locate_model(
            models, embedding_spec,
            state.get("embedding_model_id") if state else None,
        )
        if embedding is None:
            embedding = _create_model(client, token, embedding_spec)
            models.append(embedding)
        embedding_id = _safe_id(embedding.get("id"))
        chat_model_id = _safe_id(chat.get("id"))

        kbs = _list_knowledge_bases(client, token)
        kb = _locate_kb(
            kbs, config, embedding_id,
            state.get("knowledge_base_id") if state else None,
        )
        if kb is None:
            kb = _create_knowledge_base(
                client, token, config, embedding_id, chat_model_id,
            )
            _check_kb(kb, config, embedding_id)
            kbs.append(kb)
        kb_id = _safe_id(kb.get("id"))

        endpoints = _list_endpoints(client, token)
        endpoint = _locate_endpoint(
            endpoints, config, kb_id,
            state.get("mcp_endpoint_id") if state else None,
        )
        if endpoint is None:
            endpoint, endpoint_token = _create_endpoint(client, token, config, kb_id)
            endpoints.append(endpoint)
        elif state is None:
            # A previous attempt created the endpoint and died before the
            # state write.  Recovering its one-time token must rotate the
            # token exactly once; creating another endpoint is forbidden.
            endpoint, endpoint_token = _rotate_endpoint(
                client, token, _safe_id(endpoint.get("id")),
            )
            if not _endpoint_matches(endpoint, config, kb_id):
                raise BootstrapError("WEKNORA_BOOTSTRAP_ENDPOINT_CONFLICT")
        else:
            endpoint_token = state["mcp_token"]

        endpoint_id = _safe_id(endpoint.get("id"))
        tools = await _discover_tools(
            mcp, config.bootstrap_base_url, endpoint_id, endpoint_token,
        )
        new_state = _build_state(
            config,
            chat=chat,
            embedding=embedding,
            kb=kb,
            endpoint_id=endpoint_id,
            token=endpoint_token,
            tools=tools,
            user_id=user_id,
            previous=state,
            verified=False,
        )
        _write_state(config, new_state)
        return new_state


async def verify(config: BootstrapConfig, *,
                 transport: httpx.BaseTransport | None = None,
                 mcp: Any | None = None) -> dict:
    """Verify the formal app after startup and permanently close registration."""
    state = _load_state(config, required=True)
    if state is None:
        raise BootstrapError("WEKNORA_BOOTSTRAP_NOT_INITIALIZED")
    with _client(config, config.base_url, transport) as client:
        token, user = _authenticate(client, config, register_allowed=False)
        user_id, is_system_admin = _user_identity(client, token, user)
        if not is_system_admin:
            raise BootstrapError("WEKNORA_BOOTSTRAP_NOT_SYSTEM_ADMIN")
        _state_admin_matches(state, user_id)

        _set_registration_invite_only(client, token)
        if _auth_config(client) == _REGISTRATION_SELF_SERVE:
            raise BootstrapError("WEKNORA_BOOTSTRAP_REGISTRATION_NOT_CLOSED")

        chat_spec, embedding_spec = _model_specs(config)
        models = _list_models(client, token)
        chat = _locate_model(
            models, chat_spec, state.get("chat_model_id"),
        )
        if chat is None:
            raise BootstrapError("WEKNORA_BOOTSTRAP_MODEL_UNAVAILABLE")
        embedding = _locate_model(
            models, embedding_spec, state.get("embedding_model_id"),
        )
        if embedding is None:
            raise BootstrapError("WEKNORA_BOOTSTRAP_MODEL_UNAVAILABLE")
        embedding_id = _safe_id(embedding.get("id"))
        chat_model_id = _safe_id(chat.get("id"))
        _verify_chat_model(client, token, chat_model_id)
        _verify_embedding_model(
            client, token, embedding_id, config.embedding_dimensions,
        )

        kbs = _list_knowledge_bases(client, token)
        kb = _locate_kb(kbs, config, embedding_id, state["knowledge_base_id"])
        if kb is None:
            raise BootstrapError("WEKNORA_BOOTSTRAP_KNOWLEDGE_BASE_UNAVAILABLE")
        kb_id = _safe_id(kb.get("id"))

        endpoints = _list_endpoints(client, token)
        endpoint = _locate_endpoint(
            endpoints, config, kb_id, state["mcp_endpoint_id"],
        )
        if endpoint is None:
            raise BootstrapError("WEKNORA_BOOTSTRAP_ENDPOINT_UNAVAILABLE")
        endpoint_id = _safe_id(endpoint.get("id"))
        endpoint_token = state["mcp_token"]

        tools = await _discover_tools(
            mcp, config.base_url, endpoint_id, endpoint_token,
        )
        await _mcp_lists_knowledge_base(
            mcp, config.base_url, endpoint_id, endpoint_token, kb_id,
        )
        new_state = _build_state(
            config,
            chat=chat,
            embedding=embedding,
            kb=kb,
            endpoint_id=endpoint_id,
            token=endpoint_token,
            tools=tools,
            user_id=user_id,
            previous=state,
            verified=True,
        )
        _write_state(config, new_state)
        return new_state


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None, *,
         transport: httpx.BaseTransport | None = None,
         mcp: Any | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.super_assistant.weknora.bootstrap",
        description="Initialise or verify the shared WeKnora knowledge base.",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="verify the formal app and permanently close public registration",
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(for_verify=args.verify)
        if args.verify:
            import asyncio

            asyncio.run(verify(config, transport=transport, mcp=mcp))
            print("WEKNORA_BOOTSTRAP_VERIFIED")
        else:
            import asyncio

            asyncio.run(initialize(config, transport=transport, mcp=mcp))
            print("WEKNORA_BOOTSTRAP_READY")
        return 0
    except BootstrapError as exc:
        print(f"WEKNORA_BOOTSTRAP_ERROR {exc.code}", file=sys.stderr)
        return 1
    except Exception:
        # Never print a traceback: it can contain request URLs or library
        # values.  The operator only needs the stable code.
        print("WEKNORA_BOOTSTRAP_ERROR WEKNORA_BOOTSTRAP_INTERNAL", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
