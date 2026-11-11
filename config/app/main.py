from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .env_file import LocalEnvStore
from .http_transport import loopback_httpx_mounts
from .models import ConfigProfile
from .probes import PROBES, ProbeResult, run_probe
from .security import LocalRequestGuardMiddleware


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STATIC_ROOT = Path(__file__).resolve().parents[1] / "static"
DEFAULT_ENV_PATH = PROJECT_ROOT / "config" / "generated" / "local" / ".env"
DEFAULT_CONFIG_PORT = 8888
RECEIPT_TTL_SECONDS = 15 * 60
# Official nats_executor liveness contract: its main loop refreshes this
# heartbeat file every 5 seconds; compose health checks require an age
# strictly below 30 seconds.
NATS_EXECUTOR_HEARTBEAT_PATH = Path("/tmp/nats_executor.heartbeat")
NATS_EXECUTOR_HEARTBEAT_MAX_AGE_SECONDS = 30
AVAILABLE_SERVICES = tuple(PROBES)
REQUIRED_SERVICES = tuple(
    service for service in AVAILABLE_SERVICES if service != "browser"
)
# Chromium configuration is mandatory in the profile, but reachability is an
# advisory pre-start check: an operator must still be able to start the API and
# inspect diagnostics while the CDP sidecar is unhealthy. Deep readiness stays
# red until the browser becomes reachable. Hindsight long-term memory is an
# opt-in compose profile: it must never gate local start or the core service
# receipts, so its probe stays optional in the same way.
OPTIONAL_SERVICES: tuple[str, ...] = ("browser", "hindsight")


class ConfigCenterState:
    def __init__(self, env_path: Path, project_root: Path):
        self.store = LocalEnvStore(env_path, project_root=project_root)
        self.project_root = project_root
        self.csrf_token = secrets.token_urlsafe(32)
        self.access_token = secrets.token_urlsafe(48)
        self._receipts: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()
        self._write_lock = threading.Lock()

    def remember_success(self, service: str, fingerprint: str) -> None:
        with self._lock:
            self._receipts[service] = (fingerprint, time.monotonic())

    def forget(self, service: str) -> None:
        with self._lock:
            self._receipts.pop(service, None)

    def missing_receipts(self, profile: ConfigProfile) -> list[str]:
        now = time.monotonic()
        with self._lock:
            return [
                service
                for service in REQUIRED_SERVICES
                if service not in self._receipts
                or self._receipts[service][0]
                != service_fingerprint(profile, service)
                or now - self._receipts[service][1] > RECEIPT_TTL_SECONDS
            ]

    def write_profile(self, profile: ConfigProfile):
        with self._write_lock:
            return self.store.write(profile)


class RuntimeCheckRequest(BaseModel):
    profile: ConfigProfile


def create_app(
    *,
    env_path: Path | None = None,
    project_root: Path | None = None,
    static_root: Path | None = None,
) -> FastAPI:
    resolved_project_root = (project_root or PROJECT_ROOT).resolve()
    resolved_env_path = (
        env_path
        or resolved_project_root / "config" / "generated" / "local" / ".env"
    ).resolve()
    try:
        resolved_env_path.relative_to(resolved_project_root)
    except ValueError as exc:
        raise RuntimeError("本地配置文件必须位于项目目录内") from exc
    resolved_static_root = (static_root or STATIC_ROOT).resolve()
    state = ConfigCenterState(resolved_env_path, resolved_project_root)

    app = FastAPI(
        title="OpenOntology Local Configuration Center",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.config_center = state
    app.add_middleware(
        LocalRequestGuardMiddleware,
        csrf_token=state.csrf_token,
        access_token=state.access_token,
    )

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(
        _request: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        # FastAPI's default validation response includes the rejected input.
        # A model-level error would otherwise echo the complete profile,
        # including passwords and API keys, back into the browser.
        safe_errors = [
            {
                key: error[key]
                for key in ("type", "loc", "msg")
                if key in error
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": safe_errors})

    @app.get("/api/bootstrap")
    async def bootstrap() -> dict[str, Any]:
        profile, secrets_present, config_warning = state.store.public_profile()
        return {
            "csrf_token": state.csrf_token,
            "profile": profile.model_dump(),
            "has_config": state.store.exists and config_warning is None,
            "config_file_exists": state.store.exists,
            "config_warning": config_warning,
            "secrets_present": secrets_present,
            "requirements": await asyncio.to_thread(
                inspect_requirements, state.project_root, profile
            ),
            "commands": startup_commands(),
            "guides": service_guides(),
            "required_services": list(REQUIRED_SERVICES),
            "optional_services": list(OPTIONAL_SERVICES),
            "defaults_loaded": state.store.defaults_exists and not state.store.exists,
            "generated_path": "config/generated/local/.env",
            "production_isolated": True,
        }

    @app.post("/api/test/{service}")
    async def test_service(service: str, profile: ConfigProfile) -> dict[str, Any]:
        if service not in AVAILABLE_SERVICES:
            raise HTTPException(status_code=404, detail="未知的测试项目")
        try:
            resolved = state.store.resolve_service_secrets(profile, service)
        except ValueError as exc:
            state.forget(service)
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        fingerprint = service_fingerprint(resolved, service)
        result = await asyncio.to_thread(run_probe, service, resolved)
        if result.ok:
            state.remember_success(service, fingerprint)
        else:
            state.forget(service)
        return _probe_payload(service, result)

    @app.post("/api/generate")
    async def generate(profile: ConfigProfile) -> dict[str, Any]:
        try:
            resolved = state.store.resolve_secrets(profile)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        missing = state.missing_receipts(resolved)
        if missing:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "配置已变化、测试已过期，或仍有依赖未通过测试",
                    "missing_services": missing,
                },
            )

        try:
            result = await asyncio.to_thread(state.write_profile, resolved)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "ok": True,
            "message": "本地配置文件已安全生成",
            "path": _relative_display_path(result.path, state.project_root),
            "backup_created": result.backup_path is not None,
            "backup_path": (
                _relative_display_path(result.backup_path, state.project_root)
                if result.backup_path
                else None
            ),
            "commands": startup_commands(),
            "warnings": [
                "首次管理员密码只在数据库尚无管理员时生效",
                "SECRET_KEY 和 ENCRYPTION_KEY 必须长期保持稳定",
                "请分别启动后端、后台任务 executor 和前端，再执行启动后复检",
            ],
        }

    @app.post("/api/runtime-check")
    async def runtime_check(request_body: RuntimeCheckRequest) -> dict[str, Any]:
        try:
            profile = state.store.resolve_secrets(request_body.profile)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        results = await run_runtime_checks(profile)
        return {
            "ok": all(item["ok"] for item in results),
            "message": (
                "平台完整启动检查已通过"
                if all(item["ok"] for item in results)
                else "仍有进程未就绪，请按提示检查"
            ),
            "results": results,
        }

    @app.get("/")
    async def index() -> FileResponse:
        index_path = resolved_static_root / "index.html"
        if not index_path.is_file():
            raise HTTPException(status_code=503, detail="配置页面静态文件缺失")
        return FileResponse(index_path)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    if resolved_static_root.is_dir():
        app.mount(
            "/static",
            StaticFiles(directory=resolved_static_root),
            name="static",
        )
    return app


def service_fingerprint(profile: ConfigProfile, service: str) -> str:
    section = getattr(profile, service)
    serialized = json.dumps(
        section.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def inspect_requirements(
    project_root: Path,
    profile: ConfigProfile,
) -> list[dict[str, Any]]:
    uv_version = _command_version(["uv", "--version"])
    node_version = _command_version(["node", "--version"])
    node_ok = _node_supported(node_version)
    backend_port_free = _port_is_free(
        profile.platform.backend_host,
        profile.platform.backend_port,
    )
    frontend_port_free = _port_is_free(
        profile.platform.frontend_host,
        profile.platform.frontend_port,
    )
    return [
        {
            "id": "uv",
            "label": "uv Python 工具",
            "ok": uv_version is not None,
            "value": uv_version or "未找到",
            "help": "未安装时访问 https://docs.astral.sh/uv/ 获取官方安装命令",
        },
        {
            "id": "node",
            "label": "Node.js",
            "ok": node_ok,
            "value": node_version or "未找到",
            "help": "前端要求 Node.js 22，或满足 Vite 8 的受支持版本",
        },
        {
            "id": "backend_project",
            "label": "后端源码",
            "ok": (project_root / "backend" / "pyproject.toml").is_file(),
            "value": "backend/pyproject.toml",
            "help": "请从完整的 OpenOntology 项目根目录运行配置中心",
        },
        {
            "id": "frontend_project",
            "label": "前端源码",
            "ok": (project_root / "frontend" / "package.json").is_file(),
            "value": "frontend/package.json",
            "help": "请从完整的 OpenOntology 项目根目录运行配置中心",
        },
        {
            "id": "backend_port",
            "label": "后端端口",
            "ok": True,
            "value": (
                f"{profile.platform.backend_port} 当前空闲"
                if backend_port_free
                else f"{profile.platform.backend_port} 已被占用，可能后端已经启动"
            ),
            "help": "生成前可以占用，启动失败时再确认占用该端口的进程",
        },
        {
            "id": "frontend_port",
            "label": "前端端口",
            "ok": True,
            "value": (
                f"{profile.platform.frontend_port} 当前空闲"
                if frontend_port_free
                else f"{profile.platform.frontend_port} 已被占用，可能前端已经启动"
            ),
            "help": "Vite 使用严格端口，不会自动跳到另一个端口",
        },
    ]


def startup_commands() -> dict[str, list[dict[str, str]]]:
    return {
        "windows": [
            {
                "label": "后端",
                "command": "cd /d backend && uv run python -m app.dev_server",
            },
            {
                "label": "后台任务 executor",
                "command": (
                    "cd /d backend && uv run python -m "
                    "app.data_channel.pipeline_tasks.nats_executor"
                ),
            },
            {
                "label": "前端",
                "command": "cd /d frontend && npm install && npm run dev",
            },
        ],
        "unix": [
            {
                "label": "后端",
                "command": "cd backend && uv run python -m app.dev_server",
            },
            {
                "label": "后台任务 executor",
                "command": (
                    "cd backend && uv run python -m "
                    "app.data_channel.pipeline_tasks.nats_executor"
                ),
            },
            {
                "label": "前端",
                "command": "cd frontend && npm install && npm run dev",
            },
        ],
    }


def service_guides() -> dict[str, dict[str, Any]]:
    return {
        "postgres": {
            "title": "找回或重设 PostgreSQL 账号",
            "summary": "密码无法读取明文时，最稳妥的方法是由数据库管理员重设一个专用账号。",
            "steps": [
                "Windows 可以打开 pgAdmin，查看 Login/Group Roles，并为专用用户设置新密码。",
                "Ubuntu 可以先进入 postgres 系统账号，再打开 psql。",
                "macOS 使用 Homebrew 安装时，可先确认 PostgreSQL 服务已经启动。",
                "不要复用线上数据库账号，本地调试应使用独立数据库。",
            ],
            "commands": [
                "sudo -u postgres psql",
                "ALTER USER openontology WITH PASSWORD '<new-password>';",
                "docker exec -it <postgres-container> psql -U postgres -c \"\\du\"",
            ],
        },
        "redis": {
            "title": "确认 Redis 密码",
            "summary": "完整模式要求 Redis 使用密码，后端与后台任务 executor 也会使用同一地址。",
            "steps": [
                "若使用 Docker，请查看你创建容器时传入的 Redis 启动参数或配置文件。",
                "若没有设置密码，请在本地 Redis 配置中启用 requirepass 后重启服务。",
                "不要把命令输出或真实密码粘贴到公开问题中。",
            ],
            "commands": [
                "redis-cli -h 127.0.0.1 -p 6379 CONFIG GET requirepass",
                "docker inspect <redis-container>",
            ],
        },
        "nats": {
            "title": "启动带 JetStream 的 NATS",
            "summary": "NATS 是流水线任务派发通道，必须以 -js 启用 JetStream；本机默认无认证。",
            "steps": [
                "最简单的本机启动方式是使用官方 Docker 镜像，命令见下方。",
                "未启用 JetStream 时连接测试会明确报错，请在启动参数中加入 -js 后重启。",
                "若服务端启用了令牌认证，请把同一个令牌填写到本页的连接令牌框。",
            ],
            "commands": [
                "docker run -d --name nats -p 4222:4222 -v nats_data:/data nats:alpine -js --store_dir /data -m 8222",
                "docker logs <nats-container>",
            ],
        },
        "neo4j": {
            "title": "确认 Neo4j 登录信息",
            "summary": "Neo4j 首次登录通常会要求修改初始密码，配置中心需要修改后的密码。",
            "steps": [
                "打开 Neo4j Browser，默认页面常见为 http://127.0.0.1:7474。",
                "桌面版可在 DBMS 详情中查看 Bolt 地址。",
                "忘记密码时按 Neo4j 官方说明停止服务并重置认证文件，或联系管理员。",
            ],
            "commands": [
                "cypher-shell -a bolt://127.0.0.1:7687 -u neo4j -p '<password>'",
                "docker logs <neo4j-container>",
            ],
        },
        "minio": {
            "title": "区分 MinIO API 与管理页面",
            "summary": "OpenOntology 连接 S3 API，默认端口常见为 9000，不是管理页面的 9001。",
            "steps": [
                "Docker 部署时查看 MINIO_ROOT_USER 和 MINIO_ROOT_PASSWORD 的来源。",
                "生产凭据无法确认时，请新建权限受限的 Access Key，不要共享根账号。",
                "连接测试只列出桶，不会创建或删除对象。",
            ],
            "commands": [
                "docker inspect <minio-container>",
                "mc alias set local http://127.0.0.1:9000 '<access-key>' '<secret-key>'",
                "mc admin user list local",
            ],
        },
        "browser": {
            "title": "启动 Chromium 远程调试",
            "summary": "数据管家需要单独的浏览器调试会话，建议使用独立用户目录。",
            "steps": [
                "先完全关闭使用同一调试目录的 Chrome 进程。",
                "Windows、Ubuntu 和 macOS 的可执行文件路径不同。",
                "调试端口只应监听本机，不要暴露到公网。",
            ],
            "commands": [
                "chrome.exe --remote-debugging-port=9222 --user-data-dir=\"%TEMP%\\openontology-chrome\"",
                "google-chrome --remote-debugging-port=9222 --user-data-dir=/tmp/openontology-chrome",
                "\"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome\" --remote-debugging-port=9222 --user-data-dir=/tmp/openontology-chrome",
            ],
        },
        "n8n": {
            "title": "创建 n8n API Key",
            "summary": "请在 n8n 用户设置中创建 API Key，登录密码不能代替 API Key。",
            "steps": [
                "登录 n8n，进入 Settings，再进入 n8n API。",
                "创建新的 API Key，并立即复制到本页面。",
                "若看不到 API 设置，请确认当前账号权限和 n8n 版本。",
            ],
            "commands": [
                "docker ps --filter name=n8n",
                "curl http://127.0.0.1:5678/healthz",
            ],
        },
        "weknora": {
            "title": "WeKnora 共享知识库",
            "summary": (
                "WeKnora 是独立的原生 Web 知识库与 MCP 工具源；平台不会伪造"
                "模型或跳过初始化。首次初始化只在公网网关开放前由 Docker "
                "部署脚本自动执行。"
            ),
            "steps": [
                "Docker 首次部署仅自动创建管理员账号并关闭注册，然后开放原生 Web。",
                "模型、知识库、文档和 MCP 端点由管理员登录 WeKnora 原生 Web 配置。",
                "在原生 MCP 端点中限定共享知识库与只读工具，取消 ask 和写入工具。",
                "把内部 MCP 地址及 Bearer token 填入超级助手现有 MCP 管理，"
                "测试成功后启用并由管理员登记受约束。",
                "源码后端使用 loopback app 18080；公网 8089/8444 的 MCP 返回 404。",
                "本页只测试 HTTP 服务与注册保护，不调用模型，也不表示知识检索已就绪。"
                "首次账号密码只在服务器受控终端读取，不进入页面或日志。",
            ],
            "commands": [
                "docker compose -f docker-compose.local.yml up -d",
                "docker compose -f docker-compose.local.yml logs weknora-bootstrap",
                "docker compose -f docker-compose.local.yml up --force-recreate weknora-bootstrap",
            ],
        },
        "hindsight": {
            "title": "Hindsight 长期记忆（可选）",
            "summary": (
                "Hindsight 是可选的长期记忆服务，默认不启动；它故障时只降级"
                "记忆特性，聊天与平台启动不受影响。本页只提供探活地址与超时，"
                "不托管任何密钥。"
            ),
            "steps": [
                "本地启用：docker compose -f docker-compose.local.yml --profile hindsight up -d。",
                "首次启动需 HuggingFace egress 下载 embedding 模型（多语 e5-small），"
                "缓存在 hindsight_model_cache 卷；无 egress 环境先预填该卷并设置"
                "HINDSIGHT_HF_OFFLINE=1。",
                "源码后端探活地址默认 http://127.0.0.1:18888（loopback 端口）；"
                "容器内是 http://hindsight:8888。",
                "HINDSIGHT_API_KEY / HINDSIGHT_SERVICE_TOKEN 由部署环境注入，"
                "不在本页填写或保存。",
                "本测试只验证 API 与专用数据库就绪，不代表记忆写入/检索链路验收。",
            ],
            "commands": [
                "docker compose -f docker-compose.local.yml --profile hindsight up -d hindsight",
                "docker compose -f docker-compose.local.yml --profile hindsight logs hindsight",
                "curl http://127.0.0.1:18888/health/ready",
            ],
        },
    }


async def run_runtime_checks(
    profile: ConfigProfile,
) -> list[dict[str, Any]]:
    backend_origin = (
        f"http://{_http_host(profile.platform.backend_host)}:"
        f"{profile.platform.backend_port}"
    )
    frontend_origin = (
        f"http://{_http_host(profile.platform.frontend_host)}:"
        f"{profile.platform.frontend_port}"
    )
    backend_task = asyncio.to_thread(
        _check_backend_runtime, f"{backend_origin}/health/ready"
    )
    frontend_task = asyncio.to_thread(_check_frontend_runtime, frontend_origin)
    executor_task = asyncio.to_thread(_check_nats_executor)
    return list(await asyncio.gather(backend_task, frontend_task, executor_task))


def _check_backend_runtime(url: str) -> dict[str, Any]:
    try:
        with httpx.Client(
            timeout=8,
            follow_redirects=False,
            mounts=loopback_httpx_mounts(url),
        ) as client:
            response = client.get(url)
            response.raise_for_status()
            payload = response.json()
        unavailable = [str(item) for item in payload.get("unavailable") or []]
        if payload.get("status") != "ok" or unavailable:
            return {
                "id": "backend",
                "label": "后端核心就绪",
                "ok": False,
                "detail": (
                    "后端仍有必选项目未就绪: "
                    + ", ".join(
                        unavailable
                        or [str(payload.get("status") or "unknown")]
                    )
                ),
            }
        return {
            "id": "backend",
            "label": "后端核心就绪",
            "ok": True,
            "detail": "数据库和全部启动依赖均已就绪",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "id": "backend",
            "label": "后端完整就绪",
            "ok": False,
            "detail": _short_runtime_error(exc),
        }


def _check_frontend_runtime(url: str) -> dict[str, Any]:
    try:
        with httpx.Client(
            timeout=8,
            follow_redirects=False,
            mounts=loopback_httpx_mounts(url),
        ) as client:
            response = client.get(url)
            response.raise_for_status()
        if "text/html" not in response.headers.get("content-type", ""):
            raise RuntimeError("前端端口没有返回 HTML 页面")
        return {
            "id": "frontend",
            "label": "前端页面",
            "ok": True,
            "detail": "Vite 页面可以访问",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "id": "frontend",
            "label": "前端页面",
            "ok": False,
            "detail": _short_runtime_error(exc),
        }


def _check_nats_executor() -> dict[str, Any]:
    label = "后台任务 executor"
    try:
        heartbeat_age = (
            time.time() - NATS_EXECUTOR_HEARTBEAT_PATH.stat().st_mtime
        )
    except FileNotFoundError:
        return {
            "id": "nats_executor",
            "label": label,
            "ok": False,
            "detail": (
                "未检测到 executor 心跳，请在独立终端启动"
                " app.data_channel.pipeline_tasks.nats_executor"
            ),
        }
    except OSError as exc:
        return {
            "id": "nats_executor",
            "label": label,
            "ok": False,
            "detail": _short_runtime_error(exc),
        }
    if heartbeat_age >= NATS_EXECUTOR_HEARTBEAT_MAX_AGE_SECONDS:
        return {
            "id": "nats_executor",
            "label": label,
            "ok": False,
            "detail": (
                f"executor 心跳已过期（{heartbeat_age:.0f} 秒前），"
                "请确认进程仍在运行"
            ),
        }
    return {
        "id": "nats_executor",
        "label": label,
        "ok": True,
        "detail": "executor 心跳正常",
    }


def _probe_payload(service: str, result: ProbeResult) -> dict[str, Any]:
    return {
        "service": service,
        "ok": result.ok,
        "message": result.message,
        "detail": result.detail,
        "duration_ms": result.duration_ms,
    }


def _command_version(command: list[str]) -> str | None:
    if shutil.which(command[0]) is None:
        return None
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None
    value = (result.stdout or result.stderr).strip().splitlines()
    return value[0][:120] if value and result.returncode == 0 else None


def _node_supported(version: str | None) -> bool:
    if not version:
        return False
    match = re.search(r"v?(\d+)\.(\d+)\.(\d+)", version)
    if not match:
        return False
    major, minor, patch = map(int, match.groups())
    return (major == 20 and (minor, patch) >= (19, 0)) or major >= 22


def _port_is_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as candidate:
            candidate.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            candidate.bind((host, port))
        return True
    except OSError:
        return False


def _relative_display_path(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.name


def _short_runtime_error(exc: Exception) -> str:
    text = str(exc).strip() or exc.__class__.__name__
    return text[:300]


def _http_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


app = create_app()


def run() -> None:
    import uvicorn

    host = "127.0.0.1"
    port = int(
        os.getenv("OPENONTOLOGY_CONFIG_PORT", str(DEFAULT_CONFIG_PORT))
    )
    if not _port_is_free(host, port):
        raise SystemExit(
            f"Port {port} is already in use. "
            "Close the existing process or set OPENONTOLOGY_CONFIG_PORT."
        )
    url = (
        f"http://{host}:{port}/"
        f"#access_token={app.state.config_center.access_token}"
    )
    if os.getenv("OPENONTOLOGY_CONFIG_NO_BROWSER", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        browser_timer = threading.Timer(1.2, lambda: webbrowser.open(url))
        browser_timer.daemon = True
        browser_timer.start()
    print(f"OpenOntology local configuration center: {url}", flush=True)
    print("Keep this window open while editing configuration.", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
