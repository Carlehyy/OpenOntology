import ipaddress
from urllib.parse import urlsplit

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.shared.env_files import (
    LEGACY_BACKEND_ENV_FILE,
    LOCAL_CONFIG_ENV_FILE,
)


class Settings(BaseSettings):
    environment: str = "development"
    # Local launch settings are deliberately separate from deployment ports.
    # Docker and production continue to own their Uvicorn command line.
    local_backend_host: str = "127.0.0.1"
    local_backend_port: int = Field(default=8000, ge=1, le=65535)
    local_frontend_host: str = "127.0.0.1"
    local_frontend_port: int = Field(default=5173, ge=1, le=65535)

    # The configuration center provisions the required n8n integration into
    # its database-backed runtime record during startup. LLM providers are
    # intentionally configured later through the model-management UI.
    n8n_api_url: str = ""
    n8n_api_key: str = ""
    n8n_timeout_seconds: int = Field(default=30, ge=3, le=120)

    # Python 脚本流水线的执行网关（Jupyter Kernel Gateway）。可选引擎：
    # 留空不阻断启动，仅在执行/保存脚本时返回明确错误。网关以内核方式运行
    # 用户脚本，必须部署为无平台凭据环境变量的独立服务（见 compose）。
    python_kernel_gateway_url: str = ""
    python_kernel_gateway_auth_token: str = ""
    python_script_timeout_seconds: int = Field(default=120, ge=5, le=1800)
    # 自研 MCP（插件社区「开发 MCP」）工具执行的时限。独立于脚本流水线
    # 默认值：agent 会话内的工具调用对时延更敏感，默认收紧到 60 秒。
    mcp_dev_tool_timeout_seconds: int = Field(default=60, ge=5, le=1800)

    # SQLite is retained only for the explicit test environment. Every normal
    # application startup validates PostgreSQL before importing the app.
    database_url: str = "sqlite:////tmp/ontoprompt.db"
    redis_url: str = "redis://localhost:6379/0"
    # 任务池读接口缓存（fail-open 加速层，可整体关闭；与 Celery 无关，
    # 键落 db 1）。TTL 均小于前端轮询间隔，感知新鲜度与直查一致。
    pipeline_task_cache_enabled: bool = True
    pipeline_task_list_cache_ttl_seconds: int = Field(default=4, ge=1, le=60)
    pipeline_task_stats_cache_ttl_seconds: int = Field(default=5, ge=1, le=60)
    pipeline_task_options_cache_ttl_seconds: int = Field(default=30, ge=1, le=300)
    # 本体详情页读接口缓存（fail-open 加速层，可整体关闭；与 Celery 无关，
    # 键落 db 1）。TTL 均短于前端轮询/停留节奏，写路径 bump 版本立即失效；
    # executor/外部灌数进程侧的数据推进无法事件失效，由短 TTL 兜底。
    ontology_detail_cache_enabled: bool = True
    ontology_detail_cache_ttl_seconds: int = Field(default=60, ge=1, le=300)
    ontology_overview_cache_ttl_seconds: int = Field(default=20, ge=1, le=60)
    ontology_pending_cache_ttl_seconds: int = Field(default=15, ge=1, le=60)
    # 本体网络读接口缓存（fail-open 加速层，可整体关闭；键落 db 1）。
    # /overview 与 /graph 为跨本体全局聚合：项目身份与发布指针变更走
    # 版本键失效，后台灌数的实例计数漂移由短 TTL 兜底；fresh 完全绕过。
    ontology_network_cache_enabled: bool = True
    ontology_network_cache_ttl_seconds: int = Field(default=60, ge=1, le=300)
    # 世界模型读接口缓存（fail-open 加速层，可整体关闭；键落 db 1）。
    # 项目/脚本/服务/调用记录的写路径均在 Web 进程内 service 层提交点
    # bump 版本键；后台进程侧无写入，短 TTL 仅作键空间回收兜底。
    world_model_cache_enabled: bool = True
    world_model_cache_ttl_seconds: int = Field(default=30, ge=1, le=300)
    # 本体版本树读接口缓存（fail-open 加速层，可整体关闭；键落 db 1）。
    # 版本行增删与发布/回滚指针变更在写路径 bump 版本键换键；试跑状态
    # 可能由后台推进，无法事件失效，由短 TTL 兜底（与 pending 同语义）。
    ontology_version_tree_cache_enabled: bool = True
    ontology_version_tree_cache_ttl_seconds: int = Field(default=15, ge=1, le=60)
    # 本体列表读接口缓存（fail-open 加速层，可整体关闭；与 Celery 无关，
    # 键落 db 1）。列表卡片指标来自不可变发布快照，按 release 分键长 TTL；
    # 列表响应键随写路径（创建/导入/更新/删除/发布）bump 版本换键，回滚等
    # 少数指针变更路径与详情缓存一致地由短 TTL 兜底。
    ontology_list_cache_enabled: bool = True
    ontology_list_cache_ttl_seconds: int = Field(default=60, ge=1, le=300)
    # 本体发布态业务文档每日对账（04:00 重放全部发布文档事件，宫殿消费侧
    # 按指纹幂等：正常时 no-op、漂移时自愈；关闭后仅失去兜底自愈能力）
    ontology_published_documents_reconcile_enabled: bool = True
    # 数据资产湖读缓存（fail-open 加速层，可整体关闭；键落 db 1）。
    # 版本级数据键携带 version id 自然换键；总览用短 TTL + 写路径 bump 失效。
    dataset_cache_enabled: bool = True
    # 平台概览统计读缓存（fail-open 加速层，可整体关闭；键落 db 1）。
    # 全局只读聚合看板，无写路径联动，短 TTL 兜底新鲜度。
    platform_stats_cache_enabled: bool = True
    platform_stats_cache_ttl_seconds: int = Field(default=30, ge=1, le=300)

    # NATS JetStream 消息队列：流水线任务派发通道（PR-3 接入执行器）
    nats_url: str = ""
    # 流水线 executor 进程内同时执行的任务数上限；每条任务在独立线程执行，
    # 超过上限的消息积压在 JetStream 等下一轮拉取。
    pipeline_executor_concurrency: int = 2
    # 流水线执行对账器的运行周期（秒）：只收口租约已过期的中断执行，
    # 租约仍有效的长任务不受影响。
    pipeline_run_reconcile_interval_seconds: int = 300
    # ── 数据流水线读接口 Redis 缓存（fail-open 加速层，键落 db 1，与
    # Celery 无关，Celery 下线后不受影响）。开关可整体关闭；TTL 均小于
    # 前端轮询间隔或远端状态自然漂移窗口。
    pipeline_read_cache_enabled: bool = True
    sync_task_list_cache_ttl_seconds: int = Field(default=4, ge=1, le=60)
    sync_task_stats_cache_ttl_seconds: int = Field(default=5, ge=1, le=60)
    sync_task_source_cache_ttl_seconds: int = Field(default=60, ge=10, le=600)
    steward_n8n_cache_ttl_seconds: int = Field(default=10, ge=2, le=120)
    pipeline_runs_cache_ttl_seconds: int = Field(default=2, ge=1, le=60)
    pipeline_dryrun_cache_ttl_seconds: int = Field(default=1800, ge=60, le=86400)
    pipeline_dryrun_cache_max_bytes: int = Field(
        default=5_000_000, ge=100_000, le=100_000_000
    )
    secret_key: str = "dev-secret-key"
    encryption_key: str = ""
    cors_allowed_origins: str = "*"
    first_admin_user: str = "admin"
    first_admin_password: str = "admin123"
    uploads_dir: str = "./uploads"
    access_token_expire_minutes: int = 1440

    # Formal-ontology actions can call an external webhook.  Keep the request
    # budget small because actions execute on the API request/worker thread;
    # the dispatcher retries only transient failures and sends an idempotency
    # key with every attempt.
    formal_action_webhook_timeout_seconds: int = 15
    formal_action_webhook_max_attempts: int = 2
    formal_action_webhook_max_body_bytes: int = 1_000_000

    # Super Assistant is intentionally isolated from ontology/exploration
    # runtimes. Skills are real directories rooted below this path; an empty
    # value resolves to <uploads_dir>/super-assistant/skills.
    super_assistant_skill_root: str = ""
    super_assistant_max_skill_archive_mb: int = 20
    super_assistant_max_skill_files: int = 500
    super_assistant_max_skill_file_mb: int = 5
    super_assistant_max_tool_rounds: int = 25
    # 自主 agent 模式（PLAN→EXECUTE→VERIFY，对标 small-rust-hermes run_agent）的
    # 迭代上限；独立于普通对话的 max_tool_rounds，仅在 agent_mode=True 时生效
    super_assistant_agent_max_iterations: int = 50
    super_assistant_tool_result_chars: int = 30000
    super_assistant_approval_timeout_seconds: int = 180
    # 自我进化（反思/记忆，对标 small-rust-hermes）：micro 反思每
    # super_assistant_reflect_interval 轮触发一次，显式教学关键词绕过冷却；
    # 低于 auto_accept_min_confidence 或无冲突校验不通过的记忆候选进入人工审批。
    super_assistant_reflect_enabled: bool = True
    super_assistant_reflect_interval: int = 3
    super_assistant_auto_accept_min_confidence: str = "medium"
    # 记忆注入：索引/每轮相关条数上限；30 天半衰期衰减
    super_assistant_memory_index_cap: int = 50
    super_assistant_relevant_memory_cap: int = 3
    # 上下文压缩：估算超过 model_limit*(1-headroom) 时，把最旧消息（保留最近
    # super_assistant_compaction_keep_recent 条）交给 LLM 摘要
    super_assistant_context_headroom: float = 0.18
    super_assistant_compaction_keep_recent: int = 8
    # 工具权限规则：逗号分隔的工具名 glob（fnmatch 语义），deny 优先；空为不限制
    super_assistant_tool_allow: str = ""
    super_assistant_tool_deny: str = ""
    # 子代理：隔离子上下文的工具轮次上限（深度固定 1）
    super_assistant_subagent_max_rounds: int = 8
    # 平台助手委派（delegate_to_assistant）：并发舱壁=同时在等待委派结果的
    # 请求数上限（每委派占 1 等待线程 + 1 子回合独立会话），超限立即拒绝不
    # 排队；单回合超时。超时/取消 = 停止等待并释放额度，不可协作取消的子
    # 助手线程在后台跑到终态，由工作线程收尾委派行
    super_assistant_delegation_max_concurrent: int = 2
    super_assistant_delegation_timeout_seconds: int = 600
    # web 工具：fetch 默认开启（SSRF 校验复用 MCP 规则）；search 需显式配置后端
    super_assistant_web_fetch_enabled: bool = True
    super_assistant_web_fetch_max_chars: int = 20000
    super_assistant_web_search_backend: str = ""  # 空=关闭；tavily / brave
    super_assistant_web_search_tavily_api_key: str = ""
    super_assistant_web_search_brave_api_key: str = ""
    # stdio launches a process inside the backend container. Keep it opt-in and
    # require an explicit executable allowlist because this is equivalent to
    # granting server-side code execution to assistant configurators.
    super_assistant_mcp_stdio_enabled: bool = False
    super_assistant_mcp_stdio_allowed_commands: str = ""
    # Anthropic prompt caching：给 system 与 tools 末位元素加 ephemeral 缓存断点，
    # 降低重复前缀的计费与时延；DeepSeek 等 anthropic 兼容端点不支持时应关闭。
    super_assistant_prompt_cache_enabled: bool = True

    # Data-steward conversation workspace and its isolated browser runtime.
    # Empty workspace root resolves to <uploads_dir>/steward-sessions.
    steward_workspace_root: str = ""
    # Super-assistant conversation attachments reuse the steward workspace
    # implementation. Empty root resolves to <uploads_dir>/super-assistant-sessions.
    super_assistant_workspace_root: str = ""
    # Memory-palace user file library (cross-conversation long-term knowledge)
    # reuses the same workspace implementation with its own root. Empty root
    # resolves to <uploads_dir>/super-assistant-palace.
    super_assistant_palace_workspace_root: str = ""
    # 记忆宫殿配额与保护（均可经环境变量覆盖）：单用户文件数上限、总存储
    # 上限（MB）、在途抽取数上限（owner 的 pending+building 文件数）、每小时
    # 抽取次数上限（palace builds 表最近 1 小时行数）、单次 ZIP 批量导入的
    # 文件数上限。超限统一在 palace_service._check_quotas 抛 429。
    # 文件夹同步上线时上调文件数/存储/每小时三项默认值（原 500/2048/60），
    # 首次整夹同步不再撞顶；在途与并发是 LLM 成本护栏，维持不变。
    super_assistant_palace_max_files_per_user: int = 2000
    super_assistant_palace_max_total_mb: int = 10240
    super_assistant_palace_max_in_flight: int = 20
    super_assistant_palace_max_builds_per_hour: int = 300
    super_assistant_palace_batch_max_files: int = 100
    # 图谱抽取并发（进程级，与 executor 全局信号量隔离，reflect 始终有保底名额）
    super_assistant_palace_extract_concurrency: int = 1
    # 每天 03:00 的实体聚类合并定时任务开关（手动触发端点不受此开关影响）
    super_assistant_palace_consolidate_enabled: bool = True
    # 记忆宫殿图谱视图缓存（fail-open 加速层，键落 db 1）：弹窗打开/轮询期间
    # 的 GET /palace/graph 整体短 TTL 缓存；抽取完成、文件删除、聚类合并与
    # 本体文档建图在写侧 bump 版本换键，executor 进程经共享 Redis 同样生效。
    super_assistant_palace_graph_cache_enabled: bool = True
    super_assistant_palace_graph_cache_ttl_seconds: int = Field(default=30, ge=2, le=300)
    super_assistant_palace_graph_cache_max_bytes: int = Field(
        default=2_000_000, ge=100_000, le=20_000_000
    )
    steward_browser_cdp_url: str = "http://localhost:9222"
    steward_browser_timeout_seconds: int = 30
    steward_browser_max_captures: int = 300
    steward_browser_frame_interval_ms: int = 250
    # WebSocket-blocked clients fall back to authenticated HTTP frame polling.
    # A short renewable lease preserves manual-takeover semantics without
    # leaving the Agent paused forever when a tab or network disappears.
    steward_browser_http_lease_seconds: int = 30
    steward_browser_http_frame_interval_ms: int = 500
    # Human input in the shared live browser temporarily takes priority.  The
    # Agent waits for this short quiet window instead of treating an observer
    # connection as a permanent manual takeover.
    steward_browser_user_activity_seconds: int = 3
    # One Chromium process is shared, while every active conversation owns an
    # isolated BrowserContext.  Bound those contexts and suspend idle ones so a
    # user cannot exhaust the sidecar simply by creating conversations.
    steward_browser_max_sessions: int = 8
    steward_browser_max_sessions_per_user: int = 3
    steward_browser_idle_timeout_seconds: int = 900
    steward_browser_reaper_interval_seconds: int = 30
    steward_browser_allow_private_networks: bool = True
    # URL used inside generated n8n workflows to reach this backend.
    steward_internal_proxy_base_url: str = "http://backend:8000/api-hub/internal/interfaces"
    # Header Auth credential already configured in n8n.  The data steward only
    # stores its id/name reference in workflow JSON, never the secret value.
    steward_proxy_credential_name: str = "API Hub Internal Proxy"

    max_upload_mb: int = 200
    allowed_upload_extensions: str = "csv,xlsx,xls,json,xml,pdf,docx,doc,pptx,ppt,md,txt"
    # 事件附件只做安全落盘与下载，不进入文档解析链路，因此默认兼容任意扩展名。
    # 如部署方需要收紧，可通过 EVENT_ATTACHMENT_EXTENSIONS 配置逗号分隔白名单。
    event_attachment_extensions: str = "*"
    # 工单附件与事件附件同构：仅安全落盘与下载，默认兼容任意扩展名；
    # 部署方可通过 TICKET_ATTACHMENT_EXTENSIONS 收紧白名单。
    ticket_attachment_extensions: str = "*"
    # 可选 OfficeCLI 适配器。核心会话空间不依赖它；配置后才向探索 Agent 暴露
    # docx/xlsx/pptx 的结构化增删改工具，避免生产镜像隐式下载第三方二进制。
    exploration_officecli_path: str = ""
    # 数据集版本元数据 + 行级变更集（changeset）的保留窗口；0 = 不清理。
    # curated 数据集的行数据已迁入 lake_ds_* 物理表，版本行承载元数据与变更集，
    # 历史整份快照（data_blob）不再是新版版本的存储形态。活跃消费方只需
    # 「最新 + 前一版（审核 diff）」，被审核/媒体钉住的版本由 _prune_versions
    # 永久豁免，因此 5 已覆盖全部真实需求并给存储留足余量。
    dataset_version_keep: int = 5
    # Immutable DatasetVersion outbox poll interval.  The worker uses database
    # claims, so multiple API replicas may poll safely.
    dataset_event_poll_seconds: int = 2
    dataset_event_claim_timeout_seconds: int = 3600
    dataset_event_batch_size: int = 20
    # 版本事件处理执行模式：
    #   async —— API 进程 drain 只派发 Celery 任务（重活在独立 worker 进程
    #            完成映射对账/整图重建/哨兵屏障并确认 durable 事件），
    #            Celery 派发失败时降级为内联执行（fail-open，自动化不中断）；
    #   sync  —— 沿用 drain 线程内联执行（降级开关；测试环境默认）。
    dataset_event_dispatch_mode: str = "async"
    # Trial materialization runs synchronously but persists its running claim
    # first.  Expired claims are terminalized and can no longer block retry or
    # deletion; late completion is fenced by the per-run claim token.
    ontology_trial_lease_seconds: int = 3600
    # Current transform engine is list-based.  Refuse oversized production
    # inputs explicitly instead of risking process OOM; raise as deployments
    # gain memory or replace with a streaming executor.
    pipeline_max_in_memory_rows: int = 500_000
    # 源行数达到上限该比例时提前预警（仅生产；超过上限即拒绝执行）。
    # 留给运维拆资产或调参的窗口，避免某天突然硬失败。
    pipeline_source_warn_ratio: float = 0.8

    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "ontoprompt123"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_use_ssl: bool = False
    # 超级助手内置 MinIO MCP 的工作区桶：工具只能读写这一个桶，与平台数据桶隔离。
    minio_mcp_bucket: str = "assistant-workspace"
    # 默认不允许助手经 MCP 删除/移动对象；确有需求再在部署环境放开。
    minio_mcp_allow_delete: bool = False
    # Retained only so legacy local:// objects can be migrated/read. New writes
    # always require MinIO and never select this directory as a fallback.
    storage_local_dir: str = "storage"

    # n8n never receives long-lived MinIO credentials.  Every invocation gets
    # a short-lived, pipeline-scoped upload token and calls this platform file
    # gateway instead.  The URL must be reachable from the n8n runtime (the
    # Docker service name is the production-compose default).
    pipeline_file_gateway_base_url: str = "http://backend:8000/api/v2/file-transfer"
    # Browser-visible origins are intentionally independent from the n8n file
    # gateway: n8n may use a private network address while people opening a
    # FileRef need public frontend and API addresses.
    pipeline_file_public_app_base_url: str = "http://localhost:5173"
    pipeline_file_public_api_base_url: str = "http://localhost:8000"
    # 远程助手邀请函/回连契约里下发给远端 agent 的平台 API 公开地址
    super_assistant_public_api_base_url: str = "http://localhost:8000"
    pipeline_file_upload_token_minutes: int = 15
    pipeline_file_max_upload_mb: int = 100
    pipeline_file_preview_retention_hours: int = 24
    pipeline_file_pending_retention_hours: int = 2
    # Opportunistic cleanup also runs during uploads and startup.  This worker
    # bounds physical-object retention even when a quiet deployment receives
    # no new pipeline traffic for a long time.
    pipeline_file_cleanup_interval_seconds: int = 300
    pipeline_file_allowed_extensions: str = (
        "csv,xlsx,xls,json,xml,pdf,docx,doc,pptx,ppt,md,txt,"
        "png,jpg,jpeg,gif,webp,svg,zip,gz,tar"
    )

    # API 性能监控（平台运行健康度）。默认开启；出现问题时可用
    # API_PERF_ENABLED=false 整体关闭。慢阈值按部署环境可调。
    api_perf_enabled: bool = True
    api_perf_slow_threshold_ms: int = Field(default=1000, ge=100, le=600000)
    api_perf_flush_interval_seconds: int = Field(default=30, ge=5, le=3600)
    api_perf_slow_retention_days: int = Field(default=7, ge=1, le=90)
    api_perf_agg_retention_days: int = Field(default=30, ge=1, le=365)
    api_perf_buffer_max_rows: int = Field(default=10000, ge=100, le=1000000)

    model_config = SettingsConfigDict(
        # Later dotenv files override earlier ones.  Real process environment
        # variables still have the highest pydantic-settings priority.
        env_file=(LEGACY_BACKEND_ENV_FILE, LOCAL_CONFIG_ENV_FILE),
        env_file_encoding="utf-8",
        extra="ignore",
    )


def required_dependency_config_errors(current: Settings) -> list[str]:
    """Validate the required runtime stack for every non-test startup."""
    errors: list[str] = []
    try:
        database = urlsplit(current.database_url)
        database.port
    except ValueError:
        database = None
    if (
        database is None
        or database.scheme != "postgresql"
        or not database.hostname
        or not database.username
        or not database.password
        or database.path in {"", "/"}
    ):
        errors.append(
            "DATABASE_URL must use postgresql:// and reference an "
            "authenticated PostgreSQL database"
        )

    try:
        redis_url = urlsplit(current.redis_url)
        redis_url.port
    except ValueError:
        redis_url = None
    if (
        redis_url is None
        or redis_url.scheme not in {"redis", "rediss"}
        or not redis_url.hostname
        or redis_url.port is None
        or not redis_url.password
    ):
        errors.append(
            "REDIS_URL must reference authenticated Redis with an explicit port"
        )

    try:
        neo4j = urlsplit(current.neo4j_uri)
        neo4j.port
    except ValueError:
        neo4j = None
    if (
        neo4j is None
        or neo4j.scheme not in {
            "bolt", "bolt+s", "bolt+ssc", "neo4j", "neo4j+s", "neo4j+ssc"
        }
        or not neo4j.hostname
        or neo4j.port is None
        or not current.neo4j_user
        or not current.neo4j_password
    ):
        errors.append("NEO4J_URI/NEO4J credentials must reference Neo4j")

    raw_minio = str(current.minio_endpoint or "").strip()
    try:
        minio = urlsplit(f"//{raw_minio}")
        minio_port = minio.port
    except ValueError:
        minio = None
        minio_port = None
    if (
        not raw_minio
        or "://" in raw_minio
        or minio is None
        or not minio.hostname
        or minio_port is None
        or minio_port == 9001
        or not current.minio_access_key
        or not current.minio_secret_key
    ):
        errors.append(
            "MINIO_ENDPOINT/MINIO credentials must reference the S3 API, "
            "not the browser console"
        )

    try:
        browser = urlsplit(str(current.steward_browser_cdp_url or "").strip())
        browser.port
    except ValueError:
        browser = None
    if (
        browser is None
        or browser.scheme not in {"http", "https"}
        or not browser.hostname
        or browser.username is not None
        or browser.password is not None
        or browser.path not in {"", "/"}
        or browser.query
        or browser.fragment
    ):
        errors.append(
            "STEWARD_BROWSER_CDP_URL must be an absolute HTTP(S) origin/root URL"
        )

    try:
        n8n = urlsplit(str(current.n8n_api_url or "").strip())
        n8n.port
    except ValueError:
        n8n = None
    if (
        n8n is None
        or n8n.scheme not in {"http", "https"}
        or not n8n.hostname
        or n8n.username is not None
        or n8n.password is not None
        or n8n.query
        or n8n.fragment
        or not str(current.n8n_api_key or "").strip()
    ):
        errors.append("N8N_API_URL/N8N_API_KEY must configure n8n")
    return errors


def production_config_errors(current: Settings) -> list[str]:
    """Return fail-closed production configuration errors.

    ``ENCRYPTION_KEY`` intentionally remains optional for existing deployments:
    historical ciphertext was encrypted with a Fernet key derived from
    ``SECRET_KEY``. Requiring a new independent key without re-encrypting those
    rows makes every stored connection credential unreadable.
    """
    _insecure = []
    if (
        current.secret_key in {"dev-secret-key", "change-me-to-a-random-32-char-string"}
        or len(current.secret_key) < 32
    ):
        _insecure.append("SECRET_KEY")
    if current.first_admin_password in {"admin123", "change-me"} or len(current.first_admin_password) < 12:
        _insecure.append("FIRST_ADMIN_PASSWORD")
    if current.minio_access_key == "minioadmin" or current.minio_secret_key == "minioadmin":
        _insecure.append("MINIO_ACCESS_KEY/MINIO_SECRET_KEY")
    if "ontoprompt:ontoprompt@" in current.database_url:
        _insecure.append("DATABASE_URL credentials")
    if urlsplit(current.database_url).scheme.lower() != "postgresql":
        _insecure.append("DATABASE_URL must use canonical postgresql://")
    if current.neo4j_password == "ontoprompt123":
        _insecure.append("NEO4J_PASSWORD")
    # Python kernel gateway 以共享 token 鉴权，空值或公开默认值（compose 兜底
    # 与 .env.example 示例）都意味着内网任意调用方可执行任意 Python；部署脚本
    # 会对缺失/默认值自动生成强随机值，此处兜住绕过脚本裸跑 compose 的场景。
    if (
        not str(current.python_kernel_gateway_auth_token or "").strip()
        or current.python_kernel_gateway_auth_token in {
            "dev-python-gateway-token",
            "change-me-python-gateway-token",
        }
    ):
        _insecure.append("PYTHON_KERNEL_GATEWAY_AUTH_TOKEN")
    # compose 对 REDIS_URL 也有弱兜底；生产要求显式带密码（deploy 脚本同口径）。
    if (
        "ontopromptredis123" in current.redis_url
        or not urlsplit(current.redis_url).password
    ):
        _insecure.append("REDIS_URL credentials")
    if current.encryption_key:
        try:
            from cryptography.fernet import Fernet
            Fernet(current.encryption_key.encode())
        except Exception:
            _insecure.append("ENCRYPTION_KEY must be a valid Fernet key")
    origins = [item.strip() for item in current.cors_allowed_origins.split(",") if item.strip()]
    # Empty means same-origin only and is safe. Wildcard remains forbidden.
    if "*" in origins:
        _insecure.append("CORS_ALLOWED_ORIGINS")
    _insecure.extend(required_dependency_config_errors(current))
    for key, value in (
        ("PIPELINE_FILE_PUBLIC_APP_BASE_URL",
         current.pipeline_file_public_app_base_url),
        ("PIPELINE_FILE_PUBLIC_API_BASE_URL",
         current.pipeline_file_public_api_base_url),
    ):
        raw = str(value or "").strip()
        try:
            parsed = urlsplit(raw)
            hostname = parsed.hostname
            # Accessing ``port`` also validates malformed/non-numeric ports.
            parsed.port
        except ValueError:
            parsed = None
            hostname = None
        invalid = (
            parsed is None
            or parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.query)
            or bool(parsed.fragment)
            or parsed.path not in {"", "/"}
            or any(character.isspace() for character in raw)
        )
        if invalid:
            _insecure.append(
                f"{key} must be an absolute HTTP(S) origin without credentials, "
                "path, query, or fragment"
            )
            continue
        is_local = hostname.lower() in {"localhost", "backend"}
        try:
            address = ipaddress.ip_address(hostname)
            is_local = is_local or address.is_loopback or address.is_unspecified
        except ValueError:
            pass
        if is_local:
            _insecure.append(
                f"{key} must use a browser-reachable public host in production"
            )
    return _insecure


settings = Settings()

if settings.environment != "test":
    _dependency_errors = required_dependency_config_errors(settings)
    if _dependency_errors:
        raise RuntimeError(
            "平台必需运行时依赖配置无效: "
            f"{', '.join(_dependency_errors)}"
        )

if settings.environment == "production":
    _insecure = production_config_errors(settings)
    if _insecure:
        raise RuntimeError(
            "ENVIRONMENT=production 检测到不安全或旧版配置: "
            f"{', '.join(_insecure)}"
        )
