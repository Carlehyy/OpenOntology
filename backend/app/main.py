"""
OntoPrompt API v2

架构：FastAPI + PostgreSQL + Neo4j + MinIO + Redis + NATS executor + n8n + Chromium CDP
v2 新增：Pipelines 全链路（Connection→Dataset→Transform→Curated→Mapping）
v1 兼容：仅保留仍在支持范围内的 /api/v1/* 路由

启动：uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from fastapi import FastAPI, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.config import settings
from app.bootstrap import health as bootstrap_health
from app.bootstrap.lifecycle import application_lifespan
from app.bootstrap.seeding import seed_database
from app.routers import auth, ontologies, entities, logic, actions, graph, settings as settings_router, export, domains
from app.model_configs.router import router as model_configs_router
from app.platform.router import router as overview_router
from app.routers import formal as formal_router
from app.routers import sentinel as sentinel_router
from app.routers import collectors as collectors_router
from app.data_channel.connections import router as connections_v2
from app.data_channel.datasets import router as datasets_v2
from app.data_channel.pipelines import router as pipelines_v2
from app.ontologies.graph import v2_router as graph_v2
from app.data_channel.datasets import search_router as search_v2
from app.data_channel.curated import router as curated_v2
from app.ontologies.mappings import router as mappings_v2
from app.data_channel.sync_tasks import incremental_router as incremental_v2
from app.ontologies.logic import v2_router as logic_actions_v2
from app.ontologies.versions import router as versions_v2
from app.ontologies.attribute_schemas import router as attr_schemas_v2
from app.ontologies.inference import router as inference_v2
from app.data_channel.sync_tasks import router as sync_tasks_v2
from app.data_channel.pipeline_tasks import router as pipeline_tasks_v2
from app.data_channel.steward import router as steward_v2
from app.data_channel.steward import browser_ws as steward_browser_ws
from app.data_channel.file_assets import router as pipeline_file_assets
from app.data_channel.transforms import test_data_router as test_data_v2
from app.data_channel.access import asset_lake_access_guard
from app.data_channel.datasets.sharing_router import (
    management_router as manual_sharing_router,
    public_router as public_manual_sharing_router,
)
from app.ontologies.access import legacy_ontology_write_guard

_seed_db = seed_database

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Compatibility wrapper: callers may still monkeypatch app.main._seed_db.
    async with application_lifespan(app, seed_database=_seed_db):
        yield

# Production 下关闭交互文档：外网仅经 Nginx 暴露 /api 等业务路径，
# /docs 不代理本就不可达，此处再关一层防止 backend 端口被误发布；
# 非 production 环境保留便于调试。契约指纹测试走进程内 app.openapi()，不受影响。
_disable_docs = settings.environment == "production"
app = FastAPI(
    title="OntoPrompt API",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if _disable_docs else "/docs",
    redoc_url=None if _disable_docs else "/redoc",
    openapi_url=None if _disable_docs else "/openapi.json",
)

@app.get("/health/live", tags=["health"])
def liveness():
    """Process liveness only; dependency readiness is exposed separately."""
    return bootstrap_health.liveness_payload()


# Compatibility aliases: old imports keep object identity, and patching
# ``app.main.urllib.request.urlopen`` still mutates the module used by the
# canonical probe implementation.
urllib = bootstrap_health.urllib
_probe_http_service = bootstrap_health.probe_http_service

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in settings.cors_allowed_origins.split(",")
        if origin.strip()
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 运行健康度观测：为所有 HTTP 路由记录耗时聚合与慢请求证据。
# 纯 ASGI 中间件，不缓冲 SSE；健康检查/MCP/监控自身按路径豁免。
from app.platform.observability.middleware import PerfMonitoringMiddleware

app.add_middleware(PerfMonitoringMiddleware)

from app.deps import require_admin, require_menu_permission


def menu_guard(menu_key: str, *, read_menu_keys: tuple[str, ...] = ()):
    return [Depends(require_menu_permission(menu_key, read_menu_keys=read_menu_keys))]


overview_guard = menu_guard("overview")
ontology_guard = menu_guard(
    "ontologies",
    read_menu_keys=("agent", "explore", "events", "world_model"),
)
models_guard = menu_guard(
    "models",
    read_menu_keys=(
        "super_assistant",
        "explore",
        "ontologies",
        "agent",
        "data.pipelines",
    ),
)
data_guard = menu_guard("data")
pipeline_guard = menu_guard(
    "data.pipelines",
    read_menu_keys=("data.structured", "data.sync_tasks"),
)
agent_guard = menu_guard("agent")
explore_guard = menu_guard("explore")
assistant_guard = menu_guard("super_assistant")
community_plugins_guard = menu_guard("community.plugins")
events_guard = menu_guard("events")
admin_guard = [Depends(require_admin)]

app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(
    overview_router,
    prefix="/api/v1/overview",
    tags=["overview"],
    dependencies=overview_guard,
)
app.include_router(ontologies.router, prefix="/api/v1/ontologies", tags=["ontologies"], dependencies=ontology_guard)
legacy_write_guard = [Depends(legacy_ontology_write_guard)]
legacy_ontology_guard = [*legacy_write_guard, *ontology_guard]
app.include_router(entities.router, prefix="/api/v1/ontologies/{ontology_id}/entities", tags=["entities"], dependencies=legacy_ontology_guard)
app.include_router(logic.router, prefix="/api/v1/ontologies/{ontology_id}/logic", tags=["logic"], dependencies=legacy_ontology_guard)
app.include_router(actions.router, prefix="/api/v1/ontologies/{ontology_id}/actions", tags=["actions"], dependencies=legacy_ontology_guard)
app.include_router(graph.router, prefix="/api/v1/ontologies/{ontology_id}/graph", tags=["graph"], dependencies=ontology_guard)
app.include_router(export.router, prefix="/api/v1/ontologies/{ontology_id}/export", tags=["export"], dependencies=ontology_guard)
app.include_router(domains.router, prefix="/api/v1/domains", tags=["domains"])
app.include_router(model_configs_router, prefix="/api/v1/models", tags=["models"], dependencies=models_guard)
app.include_router(settings_router.router, prefix="/api/v1/settings", tags=["settings"], dependencies=admin_guard)

# 接口代理（API-Hub）管理面统一沿用平台 JWT；机器调用走两条链路：
# n8n 内部代理（Bearer 服务令牌 + revision 钉定）与公开 HTTP 代理
# （per-caller proxy key + /proxy/<slug>）。
from app.api_hub.routers import backup as api_hub_backup
from app.api_hub.routers import interfaces as api_hub_interfaces
from app.api_hub.routers import proxy as api_hub_proxy
from app.api_hub.routers import http_proxy as api_hub_http_proxy
api_hub_interfaces_guard = menu_guard("api_hub.interfaces")
api_hub_history_guard = menu_guard("api_hub.history")
app.include_router(api_hub_interfaces.router, prefix="/api/api-hub", dependencies=api_hub_interfaces_guard)
app.include_router(api_hub_interfaces.runs_router, prefix="/api/api-hub", dependencies=api_hub_history_guard)
app.include_router(api_hub_backup.router, prefix="/api/api-hub", dependencies=api_hub_interfaces_guard)
app.include_router(
    api_hub_http_proxy.admin_router,
    prefix="/api/api-hub",
    dependencies=api_hub_interfaces_guard,
)
# n8n invocation uses API_HUB_INTERNAL_PROXY_TOKEN (falls back to the
# historical API_HUB_SYSTEM_MCP_TOKEN); the endpoint pins interface revisions
# and validates dynamic parameters against the interface contract.
app.include_router(api_hub_proxy.internal_router)
# Ordinary HTTP consumers use per-caller proxy keys and stable /proxy/<slug> URLs.
app.include_router(api_hub_http_proxy.public_router)
asset_lake_guard = [Depends(asset_lake_access_guard), *data_guard]
app.include_router(connections_v2.router, prefix="/api/v2/connections", tags=["v2-connections"], dependencies=asset_lake_guard)
app.include_router(datasets_v2.router, prefix="/api/v2/datasets", tags=["v2-datasets"], dependencies=asset_lake_guard)
app.include_router(manual_sharing_router, prefix="/api/v2/manual-dataset-sharing", tags=["manual-dataset-sharing"], dependencies=asset_lake_guard)
app.include_router(public_manual_sharing_router, prefix="/api/public/manual-datasets", tags=["public-manual-datasets"])
app.include_router(pipelines_v2.router, prefix="/api/v2/pipelines", tags=["v2-pipelines"], dependencies=pipeline_guard)
app.include_router(
    pipeline_file_assets.upload_router,
    prefix="/api/v2/file-transfer",
    tags=["v2-pipeline-file-transfer"],
)
app.include_router(
    pipeline_file_assets.asset_router,
    prefix="/api/v2/file-assets",
    tags=["v2-pipeline-file-assets"],
    dependencies=[*asset_lake_guard, *pipeline_guard],
)
app.include_router(
    pipeline_file_assets.public_router,
    prefix="/api/public/file-assets",
    tags=["public-pipeline-file-assets"],
)
app.include_router(graph_v2.router, prefix="/api/v2/ontologies", tags=["v2-graph"], dependencies=ontology_guard)
app.include_router(search_v2.router, prefix="/api/v2/ontologies", tags=["v2-search"], dependencies=ontology_guard)
app.include_router(curated_v2.router, prefix="/api/v2/curated", tags=["v2-curated"], dependencies=asset_lake_guard)
app.include_router(mappings_v2.router, prefix="/api/v2/ontologies", tags=["v2-mappings"], dependencies=ontology_guard)
# 草稿映射智能建议（知识库+规则+LLM 概念化裁决，全部进人工确认队列）
from app.ontologies.mappings import suggestion_router as mapping_suggestion_router
app.include_router(mapping_suggestion_router.router, prefix="/api/v2/ontologies", tags=["v2-mappings"], dependencies=ontology_guard)
# 映射自动化订阅（未订阅一览 + 一键订阅）：数据更新默认流入本体的发布契约
from app.ontologies.mappings import automation_subscription as mapping_automation
app.include_router(mapping_automation.automation_router, prefix="/api/v2/ontologies", tags=["v2-mappings"], dependencies=ontology_guard)
app.include_router(incremental_v2.router, prefix="/api/v2/incremental", tags=["v2-incremental"], dependencies=asset_lake_guard)
app.include_router(logic_actions_v2.router, prefix="/api/v2/ontologies", tags=["v2-logic-actions"], dependencies=legacy_ontology_guard)
app.include_router(versions_v2.router, prefix="/api/v2/ontologies", tags=["v2-versions"], dependencies=ontology_guard)
app.include_router(attr_schemas_v2.router, prefix="/api/v2/ontologies", tags=["v2-attributes"], dependencies=ontology_guard)
app.include_router(inference_v2.router, prefix="/api/v2/ontologies", tags=["v2-inference"], dependencies=ontology_guard)
app.include_router(sync_tasks_v2.router, prefix="/api/v2/sync-tasks", tags=["v2-sync-tasks"], dependencies=asset_lake_guard)
app.include_router(pipeline_tasks_v2.router, prefix="/api/v2/pipeline-tasks", tags=["v2-pipeline-tasks"], dependencies=asset_lake_guard)
from app.inbox import router as inbox_router
app.include_router(inbox_router.router, prefix="/api/v2/inbox", tags=["inbox"])
app.include_router(steward_v2.router, prefix="/api/v2/steward", tags=["v2-steward"], dependencies=[*asset_lake_guard, *pipeline_guard])
# WebSocket uses a one-time, user-bound ticket issued by the authenticated
# steward router.  It intentionally sits outside HTTPBearer dependencies because
# browsers cannot attach that header to a native WebSocket handshake.
app.include_router(steward_browser_ws.router, prefix="/api/v2/steward", tags=["v2-steward-browser"])
app.include_router(test_data_v2.router, prefix="/api/v2/test-data", tags=["v2-test-data"], dependencies=asset_lake_guard)

# 正规本体模型 (Palantir 风格) — 平台核心建模 API
app.include_router(formal_router.router, prefix="/api/v2/formal/ontologies", tags=["formal-ontology"], dependencies=ontology_guard)
# 本体智能体 — 授权边界内的 LLM agent（对象/链接/事实/动作是它的全部世界）
from app.ontologies.agent_runtime import router as agent_runtime_router
app.include_router(agent_runtime_router.router, prefix="/api/v2/formal/ontologies", tags=["ontology-agent"], dependencies=agent_guard)
# 本体网络 — 跨本体全局图视图（只读，PG fo_*/发布快照，不依赖 Neo4j 投影就绪）
from app.ontologies.network import router as ontology_network_router_module
app.include_router(
    ontology_network_router_module.router,
    prefix="/api/v2/ontology-network",
    tags=["ontology-network"],
    dependencies=menu_guard("ontology_model.network"),
)
from app.ontologies.decision_simulation import router as decision_simulation_router
app.include_router(
    decision_simulation_router.router,
    prefix="/api/v2/formal/ontologies",
    tags=["ontology-decision-simulation"],
    dependencies=agent_guard,
)
# 业务探索 — 对话式业务建模：六类模型画布 → 需求文档 → 本体草稿（人审落地）
from app.exploration import router as exploration_router
app.include_router(exploration_router.router, prefix="/api/v2/exploration", tags=["exploration"], dependencies=explore_guard)
# 超级助手 — 与本体、业务探索完全解耦的通用 Agent 运行时
from app.super_assistant import router as super_assistant_router
app.include_router(
    super_assistant_router.router,
    prefix="/api/v2/super-assistant",
    tags=["super-assistant"],
    dependencies=assistant_guard,
)
# 会话内容全局搜索：独立子路由（super_assistant/router.py 已贴近架构行数上限），
# 与主路由同前缀同菜单守卫。
from app.super_assistant import search as super_assistant_search
app.include_router(
    super_assistant_search.router,
    prefix="/api/v2/super-assistant",
    tags=["super-assistant"],
    dependencies=assistant_guard,
)
# multica 外部集成（配置/测试连接）：同前缀同守卫的独立子路由
from app.super_assistant import multica as super_assistant_multica
app.include_router(
    super_assistant_multica.router,
    prefix="/api/v2/super-assistant",
    tags=["super-assistant"],
    dependencies=assistant_guard,
)
# 远程助手（声明式注册目录：CRUD/连接测试）：同前缀同守卫的独立子路由
from app.super_assistant import remote_agents as super_assistant_remote_agents
app.include_router(
    super_assistant_remote_agents.router,
    prefix="/api/v2/super-assistant",
    tags=["super-assistant"],
    dependencies=assistant_guard,
)
# 记忆宫殿文件夹同步：浏览器侧（令牌管理/脚本下发）同前缀同守卫；本机
# 脚本侧 /palace/sync/* 以长效令牌鉴权（依赖内校验菜单权限），不挂
# menu_guard——与 events ingest_router 同一挂载先例。
from app.super_assistant import palace_sync as super_assistant_palace_sync
app.include_router(
    super_assistant_palace_sync.management_router,
    prefix="/api/v2/super-assistant",
    tags=["super-assistant"],
    dependencies=assistant_guard,
)
app.include_router(
    super_assistant_palace_sync.sync_router,
    prefix="/api/v2/super-assistant",
    tags=["super-assistant"],
)
# 悬浮助手页面可见范围配置：GET 面向全体登录用户（不受 super_assistant 菜单权限约束），
# PUT 仅管理员，鉴权在路由级声明，故此处不挂 menu_guard。
from app.super_assistant import widget_config as super_assistant_widget_config
app.include_router(
    super_assistant_widget_config.router,
    prefix="/api/v2/super-assistant",
    tags=["super-assistant"],
)
# 开放社区复用超级助手的用户级 MCP 清单，但拥有独立菜单权限边界。
from app.community import router as community_router
app.include_router(
    community_router.router,
    prefix="/api/v2/community",
    tags=["community"],
    dependencies=community_plugins_guard,
)
app.include_router(sentinel_router.router, prefix="/api/v1/ontologies/{ontology_id}/sentinels", tags=["sentinel"], dependencies=ontology_guard)
# 助手评估（系统设置子项，仅 admin）— 基于 OpenJudge 的助手会话质量旁路评估
from app.assistant_evaluation import router as assistant_evaluation_router
app.include_router(
    assistant_evaluation_router.router,
    prefix="/api/v1",
    tags=["assistant-evaluation"],
    dependencies=admin_guard,
)
# 数据采集器 — AI HOT 等真实数据源接入
app.include_router(collectors_router.router, prefix="/api/v2/collectors", tags=["collectors"])
# 事件登记 — 智能助手↔数据通道之间的事件采集入口（平台录入 + 第三方 API Key 上传）
from app.events import router as events_router
app.include_router(events_router.router, prefix="/api/v2/events", tags=["events"], dependencies=events_guard)
app.include_router(events_router.ingest_router, prefix="/api/v2/ingest", tags=["events-ingest"])
# 工单 — 全角色可用的平台使用反馈通道（提交 + 管理员处理）；仅要求登录，不挂菜单权限
from app.tickets import router as tickets_router
app.include_router(tickets_router.router, prefix="/api/v2/tickets", tags=["tickets"])

# 世界模型（演化层）— 一级导航域：推演模型项目开发调试 + 调用记录（发布为推演服务属二期）
world_model_guard = menu_guard("world_model")
from app.world_model import router as world_model_router
app.include_router(
    world_model_router.router,
    prefix="/api/v2/world-model",
    tags=["world-model"],
    dependencies=world_model_guard,
)

# 三维场景 — 白模场景管理与建模：卡片列表 / 详情三标签 / 运行日志（场景助手属阶段二）
scenes_guard = menu_guard("scenes")
from app.scenes import router as scenes_router
app.include_router(
    scenes_router.router,
    prefix="/api/v2/scenes",
    tags=["scenes"],
    dependencies=scenes_guard,
)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.get("/api/health", tags=["health"])
@app.get("/health", tags=["health"])
@app.get("/health/ready", tags=["health"])
def health(db: Session = Depends(get_db)):
    # Resolve the compatibility alias at call time so legacy monkeypatch targets
    # continue to control the readiness endpoint.
    return bootstrap_health.readiness_response(
        db,
        probe=_probe_http_service,
    )
