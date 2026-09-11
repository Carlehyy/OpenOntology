"""
世界模型 — HTTP 层业务逻辑。

「执行」= 内核试跑（用户脚本 + 注入的 simulate 调用收尾），不落库；
「保存」= 双重保障的服务端一侧：重新执行复核，通过才把脚本写入项目
并冻结一个历史版本（与 Python 脚本流水线同一纪律：脚本变更必须重新
验证才能保存）。
"""
from __future__ import annotations

import json
import logging

from datetime import datetime, time, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.data_channel.pipelines.python_engine.client import (
    PythonEngineError,
    execute_code,
    extract_payload,
    tail_stdout,
)
from app.world_model import cache as wm_cache
from app.world_model import schemas
from app.world_model.models import (
    ENGINE_TYPES,
    SCRIPT_VERSION_KEEP,
    SERVICE_STATUS_DRAFT,
    SERVICE_STATUS_OFFLINE,
    SERVICE_STATUS_ONLINE,
    STATUS_DRAFT,
    STATUS_PUBLISHED,
    WorldModelCallRecord,
    WorldModelProject,
    WorldModelScriptVersion,
    WorldModelService,
)

logger = logging.getLogger(__name__)

# 新建项目的脚本模板：声明平台统一的推演入口契约
SCRIPT_TEMPLATE = '''def simulate(context, actions, horizon):
    """
    推演模型入口函数（平台统一契约）。

    参数：
        context: dict  — 当前状态快照（来自数字孪生/知识图谱的业务对象状态）
        actions: list  — 候选行动列表；无干预推演（纯预测）时为空列表
        horizon: int   — 推演时域（步数/期数，语义由模型自行定义）

    返回：
        dict（JSON 可序列化），建议包含：
          - trajectory: list  — 各时点的状态/指标轨迹
          - confidence: float — 置信度（0~1）
          - boundary:   str   — 结果适用边界说明
    """
    # 示例：简单的趋势外推占位实现，请替换为你的推演逻辑
    base = (context or {}).get("current_value", 0)
    trajectory = [base for _ in range(int(horizon or 1))]
    return {
        "trajectory": trajectory,
        "confidence": None,
        "boundary": "占位实现，未建模",
    }
'''

# 时序推演示例模板：ITSM 式 ARIMA / SARIMA 建模与预测（依赖 statsmodels）。
# 该模板是开发页「时序示例」按钮的权威内容，前后端不得各自复制副本。
TIME_SERIES_TEMPLATE = '''def simulate(context, actions, horizon):
    """时序推演示例（ITSM 式流程）：ARIMA / SARIMA 拟合与预测。

    context 约定：
      series : list[float] — 历史时序观测值（按时间顺序，建议 >= 24 个点）
      period : int（可选）  — 季节周期（如月度数据传 12）；数据足够时启用 SARIMA
    actions（可选）：[{"step": int, "delta": float}] — 干预动作，
      在预测轨迹第 step 步（从 0 起）叠加 delta，用于情景干预推演

    流程：ADF 平稳性检验定差分阶数 d → ACF/PACF 显著滞后定 p/q 候选 →
    AIC 小网格选优 → 拟合 ARIMA/SARIMA → 预测 horizon 步，
    输出 trajectory / confidence / boundary / model_summary。
    """
    import warnings

    import numpy as np
    import pandas as pd
    from statsmodels.tsa.arima.model import ARIMA
    from statsmodels.tsa.statespace.sarimax import SARIMAX
    from statsmodels.tsa.stattools import acf, adfuller, pacf

    def _fail(message):
        return {
            "trajectory": [],
            "confidence": 0.0,
            "boundary": message,
            "model_summary": None,
        }

    series = (context or {}).get("series")
    if not isinstance(series, (list, tuple)) or len(series) < 12:
        return _fail("context.series 必须是长度 >= 12 的数值列表（历史时序观测值）。")
    try:
        values = np.asarray(series, dtype=float)
    except (TypeError, ValueError):
        return _fail("context.series 含有无法转为数值的元素。")
    if not np.all(np.isfinite(values)):
        return _fail("context.series 含 NaN/Inf，请先清洗数据。")

    horizon = int(horizon or 1)
    horizon = max(1, min(horizon, max(1, len(values))))
    period = (context or {}).get("period")
    use_seasonal = (
        isinstance(period, int) and period >= 2 and len(values) >= 2 * period
    )
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")

            # 1) 差分阶数 d：ADF 不平稳则差分（最多 2 阶）
            d = 0
            working = pd.Series(values)
            p_values = []
            p_value = float(adfuller(working)[1])
            p_values.append(p_value)
            while d < 2 and p_value > 0.05:
                working = working.diff().dropna()
                d += 1
                p_value = float(adfuller(working)[1])
                p_values.append(p_value)

            # 2) p / q 候选：ACF/PACF 首个显著滞后（95% 置信带），上限 5
            n = len(working)
            threshold = 1.96 / np.sqrt(n) if n > 0 else 1.0
            max_lag = max(1, min(5, n // 2))
            acf_values = acf(working, nlags=max_lag)
            pacf_values = pacf(working, nlags=max_lag)
            q0 = next(
                (k for k in range(1, len(acf_values))
                 if abs(acf_values[k]) > threshold), 0)
            p0 = next(
                (k for k in range(1, len(pacf_values))
                 if abs(pacf_values[k]) > threshold), 0)

            # 3) AIC 小网格选优（候选邻域 + 朴素基准，控制拟合次数）
            candidates = []
            for p in (p0, p0 + 1, 1, 0):
                for q in (q0, q0 + 1, 1, 0):
                    pair = (min(p, 5), d, min(q, 5))
                    if pair not in candidates:
                        candidates.append(pair)
            best = None
            best_aic = float("inf")
            for p, dd, q in candidates:
                try:
                    fit = ARIMA(values, order=(p, dd, q)).fit()
                    aic = float(fit.aic)
                    if aic < best_aic:
                        best, best_aic = fit, aic
                except Exception:
                    continue
            if best is None:
                return _fail("ARIMA 拟合失败：候选阶数均未收敛，请检查数据质量。")

            # 4) 预测 horizon 步（含 95% 置信区间）
            order = (int(best.model.order[0]), d, int(best.model.order[2]))
            method = "ARIMA"
            seasonal_order = None
            if use_seasonal:
                try:
                    seasonal = SARIMAX(
                        values,
                        order=order,
                        seasonal_order=(1, 0, 1, period),
                        enforce_stationarity=False,
                    ).fit()
                    method = "SARIMA"
                    seasonal_order = [1, 0, 1, period]
                except Exception:
                    seasonal = None
            fitted = seasonal if use_seasonal and seasonal is not None else best
            forecast = fitted.get_forecast(horizon)
            mean = np.asarray(forecast.predicted_mean, dtype=float)
            interval = np.asarray(forecast.conf_int(alpha=0.05), dtype=float)

            # 5) 干预动作：在轨迹上叠加 delta
            for action in actions or []:
                if not isinstance(action, dict):
                    continue
                step = action.get("step")
                delta = action.get("delta")
                if isinstance(step, int) and 0 <= step < len(mean):
                    try:
                        mean[step] += float(delta)
                    except (TypeError, ValueError):
                        continue

            half_width = (interval[:, 1] - interval[:, 0]) / 2.0
            step_conf = 1.0 - half_width / (
                np.abs(mean) + half_width + 1e-9)
            confidence = float(np.clip(np.mean(step_conf), 0.0, 1.0))

            return {
                "trajectory": [round(float(x), 6) for x in mean],
                "confidence": round(confidence, 4),
                "boundary": (
                    f"{method}({order[0]},{order[1]},{order[2]})"
                    f"{' 含季节项周期 ' + str(period) if use_seasonal and seasonal is not None else ''}"
                    f"，基于 {len(values)} 个历史点外推 {horizon} 步；"
                    "适用于平稳可建模的时序，结构突变与突发事件需人工复核。"
                ),
                "model_summary": {
                    "method": method,
                    "order": list(order),
                    "seasonal_order": seasonal_order,
                    "aic": round(float(fitted.aic), 4),
                    "bic": round(float(fitted.bic), 4),
                    "n_obs": int(len(values)),
                    "adf_pvalues": [round(float(x), 4) for x in p_values],
                    "acf_lags": [round(float(x), 4) for x in acf_values[1:]],
                    "pacf_lags": [round(float(x), 4) for x in pacf_values[1:]],
                    "candidates": [[int(x) for x in c] for c in candidates],
                },
            }
    except Exception as exc:  # noqa: BLE001 — 模板需兜底返回可读信息
        return _fail(f"时序建模执行异常：{type(exc).__name__}: {exc}")
'''


# 时序示例的默认测试入参：确定性生成 36 点“趋势 + 年度季节 + 扰动”序列
def _default_ts_series():
    import math

    return [
        round(100.0 + 2.0 * i + 8.0 * math.sin(2.0 * math.pi * i / 12.0)
              + 3.0 * ((i * 17) % 5 - 2), 2)
        for i in range(36)
    ]


def _default_ts_test_input() -> dict:
    return {
        "context": {"series": _default_ts_series(), "period": 12},
        "actions": [],
        "horizon": 6,
    }

# 调试执行注入的收尾代码：调用入口函数并把返回值序列化到输出标记之间。
# 输出标记与 result 行提取共用（python_engine.client 的 __OB_RESULT_*__）。
# 注意：{test_input} 必须以 Python 字符串字面量形式嵌入（repr），
# 直接嵌入 JSON 文本会把 true/false/null 带进 Python 表达式导致 NameError。
_DEBUG_EPILOGUE_TEMPLATE = '''

# ── OpenOntology 世界模型调试执行（自动注入，请勿删除） ──
import json as _ob_json
if "simulate" not in globals():
    raise NameError("脚本未定义入口函数 simulate(context, actions, horizon)")
_ob_test_input = _ob_json.loads({test_input})
_ob_payload = simulate(
    context=_ob_test_input.get("context") or {{}},
    actions=_ob_test_input.get("actions") or [],
    horizon=_ob_test_input.get("horizon") or 1,
)
print()
print("__OB_RESULT_BEGIN__")
print(_ob_json.dumps(_ob_payload, ensure_ascii=False, default=str))
print("__OB_RESULT_END__")
'''

# 测试入参 JSON 序列化后的长度上限（防止把超大状态快照塞进内核代码）
_TEST_INPUT_CHARS = 100_000


def _load_project(db: Session, project_id: str) -> WorldModelProject:
    project = db.get(WorldModelProject, project_id)
    if project is None:
        raise HTTPException(404, "推演模型不存在或已被删除。")
    return project


# ──────────────────────────── 官方脚本模板 ────────────────────────────


def get_time_series_template() -> schemas.TemplateOut:
    """时序推演示例模板（ITSM 式 ARIMA/SARIMA），供开发页一键插入。"""
    return schemas.TemplateOut(
        key="time-series",
        name="时序推演示例（ARIMA / SARIMA）",
        description=(
            "ITSM 式统计时序建模：ADF 平稳性检验定差分阶数 → ACF/PACF 定阶 → "
            "AIC 网格选优 → 拟合 ARIMA/SARIMA → horizon 步预测。"
            "context.series 传历史序列（>= 12 点），context.period 可选季节周期。"
        ),
        script=TIME_SERIES_TEMPLATE,
        test_input=_default_ts_test_input(),
    )


# ──────────────────────────── 项目 CRUD ────────────────────────────


def list_projects(
    db: Session,
    *,
    keyword: str = "",
    engine_type: str = "",
    page: int = 1,
    size: int = 100,
) -> schemas.ProjectListResponse:
    query = db.query(WorldModelProject)
    if keyword.strip():
        like = f"%{keyword.strip()}%"
        query = query.filter(
            WorldModelProject.name.like(like)
            | WorldModelProject.description.like(like))
    if engine_type:
        if engine_type not in ENGINE_TYPES:
            raise HTTPException(400, f"未知引擎类型：{engine_type}")
        query = query.filter(WorldModelProject.engine_type == engine_type)
    total = query.count()
    rows = (
        query.order_by(WorldModelProject.updated_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    version_counts = dict(
        db.query(
            WorldModelScriptVersion.project_id,
            func.count(WorldModelScriptVersion.id),
        )
        .group_by(WorldModelScriptVersion.project_id)
        .all()
    )
    # 页内项目的服务摘要（状态/名称/端点/冻结版本号），批量取数避免逐卡请求。
    # 多本体发布后一个项目可有 N 个服务：摘要取最近更新的一个，service_count 给出总数。
    service_rows = (
        db.query(WorldModelService)
        .filter(WorldModelService.project_id.in_([row.id for row in rows]))
        .order_by(WorldModelService.updated_at.desc())
        .all()
    ) if rows else []
    services_by_project: dict[str, WorldModelService] = {}
    service_counts: dict[str, int] = {}
    for svc in service_rows:
        pid = str(svc.project_id)
        service_counts[pid] = service_counts.get(pid, 0) + 1
        services_by_project.setdefault(pid, svc)  # 已按 updated_at 降序，首个即最新
    version_ids = [svc.version_id for svc in service_rows if svc.version_id]
    service_version_nos = dict(
        db.query(WorldModelScriptVersion.id, WorldModelScriptVersion.version_no)
        .filter(WorldModelScriptVersion.id.in_(version_ids))
        .all()
    ) if version_ids else {}
    items = []
    for row in rows:
        svc = services_by_project.get(row.id)
        items.append(schemas.ProjectSummary(
            id=row.id,
            name=row.name,
            description=row.description or "",
            engine_type=row.engine_type,
            status=row.status,
            version_count=int(version_counts.get(row.id, 0)),
            service_count=service_counts.get(row.id, 0),
            service_status=svc.status if svc else None,
            service_name=svc.name if svc else None,
            service_endpoint=svc.endpoint_path if svc else None,
            service_version_no=(
                service_version_nos.get(svc.version_id)
                if svc and svc.version_id else None
            ),
            created_at=row.created_at,
            updated_at=row.updated_at,
        ))
    return schemas.ProjectListResponse(items=items, total=total)


def create_project(
    db: Session, body: schemas.ProjectCreate, current_user,
) -> WorldModelProject:
    project = WorldModelProject(
        name=body.name.strip(),
        description=body.description.strip(),
        engine_type=body.engine_type,
        script=SCRIPT_TEMPLATE,
        status=STATUS_DRAFT,
        created_by=getattr(current_user, "id", None),
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    wm_cache.invalidate_world_model()
    return project


def get_project(db: Session, project_id: str) -> WorldModelProject:
    return _load_project(db, project_id)


def update_project(
    db: Session, project_id: str, body: schemas.ProjectUpdate,
) -> WorldModelProject:
    project = _load_project(db, project_id)
    if body.name is not None:
        project.name = body.name.strip()
    if body.description is not None:
        project.description = body.description.strip()
    if body.engine_type is not None:
        project.engine_type = body.engine_type
    db.commit()
    db.refresh(project)
    wm_cache.invalidate_world_model()
    return project


def delete_project(db: Session, project_id: str) -> None:
    """删除项目：在线推演服务须先下线；显式清理子表（SQLite 默认不启用外键级联，与 PG 行为对齐）。

    版本与推演服务随项目删除；调用记录属于审计数据，保留但解除项目/服务关联。
    """
    project = _load_project(db, project_id)
    services = (
        db.query(WorldModelService)
        .filter(WorldModelService.project_id == project.id)
        .all()
    )
    online = [s for s in services if s.status == SERVICE_STATUS_ONLINE]
    if online:
        raise HTTPException(
            409,
            f"该模型存在 {len(online)} 个在线推演服务（如「{online[0].name}」），"
            "请先在「推演服务」页将服务下线后再删除。",
        )
    db.query(WorldModelScriptVersion).filter(
        WorldModelScriptVersion.project_id == project.id).delete()
    db.query(WorldModelCallRecord).filter(
        WorldModelCallRecord.project_id == project.id).update(
        {WorldModelCallRecord.project_id: None,
         WorldModelCallRecord.service_id: None})
    for svc in services:
        db.delete(svc)
    db.delete(project)
    db.commit()
    wm_cache.invalidate_world_model()


# ──────────────────────────── 调试执行与保存 ────────────────────────────


def _normalize_test_input(test_input: dict) -> dict:
    """测试入参归一：只保留契约三键，缺失时补默认值。"""
    raw = test_input or {}
    if not isinstance(raw, dict):
        raise HTTPException(400, "测试入参必须是 JSON 对象。")
    return {
        "context": raw.get("context") or {},
        "actions": raw.get("actions") or [],
        "horizon": raw.get("horizon") or 1,
    }


def _build_debug_code(script: str, test_input: dict) -> str:
    normalized = _normalize_test_input(test_input)
    test_input_json = json.dumps(normalized, ensure_ascii=False, default=str)
    if len(test_input_json) > _TEST_INPUT_CHARS:
        raise HTTPException(
            400,
            f"测试入参过大（{_TEST_INPUT_CHARS // 1000}K 字符上限）："
            "请裁剪 context 快照后再执行。",
        )
    # repr() 产出合法 Python 字符串字面量，避免 JSON 的 true/false/null
    # 进入内核代码后被当成 Python 标识符（NameError）
    return script + _DEBUG_EPILOGUE_TEMPLATE.format(test_input=repr(test_input_json))


def _run_debug(script: str, test_input: dict) -> schemas.ScriptExecutionResult:
    if not script.strip():
        raise HTTPException(400, "脚本内容为空，无法执行。")
    code = _build_debug_code(script, test_input)
    try:
        # full_stdout：输出标记的结果块可能超过尾部截断上限，
        # 必须在完整 stdout 上解析，解析后再截回尾部回传
        execution = execute_code(code, full_stdout=True)
    except PythonEngineError as exc:
        # 网关未配置/不可达等基础设施失败：话术与数据通道保持一致
        raise HTTPException(502, str(exc)) from exc
    payload = None
    error = execution.error
    if not error:
        try:
            payload = extract_payload(execution.stdout)
        except PythonEngineError as exc:
            error = str(exc)
    return schemas.ScriptExecutionResult(
        ok=error is None,
        payload=payload,
        stdout=tail_stdout(execution.stdout),
        error=error,
        traceback=execution.traceback,
        duration_ms=execution.duration_ms,
        kernel_id=execution.kernel_id,
    )


def execute_project_script(
    db: Session, project_id: str, body: schemas.ScriptExecuteRequest,
) -> schemas.ScriptExecutionResult:
    """调试执行：内核试跑并返回 simulate 的输出，不落库。"""
    _load_project(db, project_id)
    return _run_debug(body.script, body.test_input)


def save_project_script(
    db: Session, project_id: str, body: schemas.ScriptSaveRequest, current_user,
) -> schemas.ScriptSaveResult:
    """保存脚本：服务端重新执行复核，通过才落库并冻结版本。"""
    project = _load_project(db, project_id)
    execution = _run_debug(body.script, body.test_input)
    if not execution.ok:
        return schemas.ScriptSaveResult(ok=False, execution=execution)

    project.script = body.script
    next_version_no = (
        db.query(func.max(WorldModelScriptVersion.version_no))
        .filter(WorldModelScriptVersion.project_id == project.id)
        .scalar()
        or 0
    ) + 1
    version = WorldModelScriptVersion(
        project_id=project.id,
        version_no=next_version_no,
        script=body.script,
        test_input=_normalize_test_input(body.test_input),
        duration_ms=execution.duration_ms,
        created_by=getattr(current_user, "id", None),
    )
    db.add(version)
    db.flush()

    # 修剪历史版本：只保留最近 SCRIPT_VERSION_KEEP 版
    stale = (
        db.query(WorldModelScriptVersion)
        .filter(WorldModelScriptVersion.project_id == project.id)
        .order_by(WorldModelScriptVersion.version_no.desc())
        .offset(SCRIPT_VERSION_KEEP)
        .all()
    )
    for row in stale:
        db.delete(row)

    db.commit()
    wm_cache.invalidate_world_model()
    return schemas.ScriptSaveResult(
        ok=True, execution=execution, version_no=next_version_no)


def list_script_versions(
    db: Session, project_id: str,
) -> list[schemas.ScriptVersionItem]:
    _load_project(db, project_id)
    rows = (
        db.query(WorldModelScriptVersion)
        .filter(WorldModelScriptVersion.project_id == project_id)
        .order_by(WorldModelScriptVersion.version_no.desc())
        .all()
    )
    return [
        schemas.ScriptVersionItem(
            id=row.id,
            version_no=row.version_no,
            test_input=row.test_input,
            duration_ms=row.duration_ms,
            created_by=row.created_by,
            created_at=row.created_at,
        )
        for row in rows
    ]


def get_script_version(
    db: Session, project_id: str, version_id: str,
) -> schemas.ScriptVersionDetail:
    _load_project(db, project_id)
    row = db.get(WorldModelScriptVersion, version_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(404, "脚本版本不存在。")
    return schemas.ScriptVersionDetail(
        id=row.id,
        version_no=row.version_no,
        script=row.script,
        test_input=row.test_input,
        duration_ms=row.duration_ms,
        created_by=row.created_by,
        created_at=row.created_at,
    )


# ──────────────────────────── 调用记录（只读） ────────────────────────────


def list_call_records(
    db: Session,
    *,
    keyword: str = "",
    result: str = "all",
    service_id: str | None = None,
    start=None,
    end=None,
    page: int = 1,
    size: int = 20,
) -> schemas.CallRecordListResponse:
    query = db.query(WorldModelCallRecord)
    if service_id:
        query = query.filter(WorldModelCallRecord.service_id == service_id)
    if keyword.strip():
        like = f"%{keyword.strip()}%"
        query = query.filter(
            WorldModelCallRecord.service_name.like(like)
            | WorldModelCallRecord.caller.like(like))
    if result == "failed":
        query = query.filter(WorldModelCallRecord.ok.is_(False))
    elif result != "all":
        raise HTTPException(400, f"未知结果筛选：{result}")
    if start is not None:
        query = query.filter(WorldModelCallRecord.created_at >= start)
    if end is not None:
        query = query.filter(WorldModelCallRecord.created_at <= end)
    total = query.count()
    rows = (
        query.order_by(WorldModelCallRecord.created_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    return schemas.CallRecordListResponse(
        items=[
            schemas.CallRecordItem(
                id=row.id,
                project_id=row.project_id,
                service_name=row.service_name,
                caller=row.caller,
                ok=row.ok,
                duration_ms=row.duration_ms,
                error=row.error,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
    )


def get_call_record(db: Session, record_id: str) -> WorldModelCallRecord:
    row = db.get(WorldModelCallRecord, record_id)
    if row is None:
        raise HTTPException(404, "调用记录不存在。")
    return row


def call_records_overview(db: Session) -> schemas.CallRecordOverview:
    total = db.query(func.count(WorldModelCallRecord.id)).scalar() or 0
    failed = (
        db.query(func.count(WorldModelCallRecord.id))
        .filter(WorldModelCallRecord.ok.is_(False))
        .scalar()
        or 0
    )
    avg_duration = (
        db.query(func.coalesce(func.avg(WorldModelCallRecord.duration_ms), 0))
        .scalar()
        or 0
    )
    return schemas.CallRecordOverview(
        total=int(total),
        failed=int(failed),
        avg_duration_ms=int(avg_duration),
    )


def call_records_daily(
    db: Session, *, days: int = 14,
) -> list[schemas.CallRecordDailyBucket]:
    """近 N 天按日分桶的调用统计：缺失日期补零，按日期升序返回。"""
    today = datetime.now(timezone.utc).date()
    start_day = today - timedelta(days=days - 1)
    range_start = datetime.combine(start_day, time.min, tzinfo=timezone.utc)
    day_expr = func.date(WorldModelCallRecord.created_at)
    rows = (
        db.query(
            day_expr,
            func.count(WorldModelCallRecord.id),
            func.count(WorldModelCallRecord.id)
            .filter(WorldModelCallRecord.ok.is_(False)),
            func.coalesce(func.avg(WorldModelCallRecord.duration_ms), 0),
        )
        .filter(WorldModelCallRecord.created_at >= range_start)
        .group_by(day_expr)
        .all()
    )
    by_day = {
        str(day): (int(total), int(failed), round(float(avg)))
        for day, total, failed, avg in rows
    }
    buckets: list[schemas.CallRecordDailyBucket] = []
    for offset in range(days):
        day = (start_day + timedelta(days=offset)).isoformat()
        total, failed, avg = by_day.get(day, (0, 0, 0))
        buckets.append(schemas.CallRecordDailyBucket(
            date=day, total=total, failed=failed, avg_duration_ms=avg))
    return buckets


# ──────────────────────────── 推演服务（发布 / 状态 / 调用） ────────────────────────────


def count_versions(db: Session, project_id: str) -> int:
    return (
        db.query(func.count(WorldModelScriptVersion.id))
        .filter(WorldModelScriptVersion.project_id == project_id)
        .scalar()
        or 0
    )


def get_project_service(
    db: Session, project_id: str,
) -> WorldModelService | None:
    """兼容入口：返回该项目"代表性"服务（最近更新的一个）。

    多本体发布上线后一个项目可有 N 个服务（每个绑定一个本体）；
    单服务项目行为与历史完全一致。需要完整列表用 list_project_services。
    """
    _load_project(db, project_id)
    return (
        db.query(WorldModelService)
        .filter(WorldModelService.project_id == project_id)
        .order_by(WorldModelService.updated_at.desc())
        .first()
    )


def list_project_services(
    db: Session, project_id: str,
) -> list[WorldModelService]:
    _load_project(db, project_id)
    return (
        db.query(WorldModelService)
        .filter(WorldModelService.project_id == project_id)
        .order_by(WorldModelService.updated_at.desc())
        .all()
    )


def publish_service(
    db: Session, project_id: str, body: schemas.ServicePublishRequest, current_user,
) -> WorldModelService:
    """发布为推演服务：冻结版本 + 本体语义注册，生成调用端点并上线。

    一个项目可发布多个服务，每个服务绑定恰好一个本体（模型复用、注册实例隔离）：
    对同一本体重发布 = 覆盖更新该本体对应的服务，不影响发布到其他本体的服务。
    """
    project = _load_project(db, project_id)
    if body.version_id:
        version = db.get(WorldModelScriptVersion, body.version_id)
        if version is None or version.project_id != project.id:
            raise HTTPException(404, "指定的脚本版本不存在。")
    else:
        version = (
            db.query(WorldModelScriptVersion)
            .filter(WorldModelScriptVersion.project_id == project.id)
            .order_by(WorldModelScriptVersion.version_no.desc())
            .first()
        )
        if version is None:
            raise HTTPException(
                400, "尚未保存任何脚本版本：请先在开发页执行通过并保存，再发布。")

    services = (
        db.query(WorldModelService)
        .filter(WorldModelService.project_id == project.id)
        .all()
    )
    service = next(
        (s for s in services
         if isinstance(s.applicable_object_types, dict)
         and s.applicable_object_types.get("ontology_id") == body.applicable_ontology_id),
        None,
    )
    if service is None:
        service = WorldModelService(
            project_id=project.id,
            name=body.name.strip(),
            created_by=getattr(current_user, "id", None),
        )
        db.add(service)
        db.flush()  # 先取 id 以生成端点路径
    service.name = body.name.strip()
    service.description = body.description.strip()
    service.version_id = version.id
    service.status = SERVICE_STATUS_ONLINE  # 发布即上线
    service.endpoint_path = f"/api/v2/world-model/services/{service.id}/invoke"
    # 语义注册：值引用本体概念（本体 id + 对象类型 id），供 Agent 结构化检索
    service.applicable_object_types = {
        "ontology_id": body.applicable_ontology_id,
        "object_type_ids": body.applicable_object_type_ids,
    }
    service.preconditions = [item.model_dump() for item in body.preconditions]
    project.status = STATUS_PUBLISHED
    db.commit()
    db.refresh(service)
    wm_cache.invalidate_world_model()
    return service


def _apply_service_status(
    db: Session, service: WorldModelService, status: str,
) -> WorldModelService:
    service.status = status
    db.commit()
    db.refresh(service)
    wm_cache.invalidate_world_model()
    return service


def set_service_status(
    db: Session, project_id: str, status: str,
) -> WorldModelService:
    """项目级状态切换：批量作用于该项目全部服务（多本体发布后为 N 个）。

    单服务项目行为与历史一致；返回代表性服务（最近更新）。
    """
    services = list_project_services(db, project_id)
    if not services:
        raise HTTPException(404, "该项目尚未发布推演服务。")
    for svc in services:
        svc.status = status
    db.commit()
    db.refresh(services[0])
    wm_cache.invalidate_world_model()
    return services[0]


def set_service_status_by_id(
    db: Session, service_id: str, status: str,
) -> WorldModelService:
    return _apply_service_status(db, get_service_by_id(db, service_id), status)


def invoke_service(
    db: Session, service_id: str, body: schemas.InvokeRequest, current_user,
) -> schemas.InvokeResult:
    """调用推演服务：执行冻结版本的脚本并写入调用记录（审计闭环）。"""
    service = db.get(WorldModelService, service_id)
    if service is None:
        raise HTTPException(404, "推演服务不存在或已被删除。")
    if service.status != SERVICE_STATUS_ONLINE:
        raise HTTPException(409, "推演服务未在线，无法调用（请先上线）。")
    version = (
        db.get(WorldModelScriptVersion, service.version_id)
        if service.version_id else None
    )
    if version is None:
        raise HTTPException(409, "推演服务未绑定可用的脚本版本。")

    caller = getattr(current_user, "username", "") or ""
    record = WorldModelCallRecord(
        project_id=service.project_id,
        service_id=service.id,
        service_name=service.name,
        caller=caller,
        ok=False,
        request_payload=body.model_dump(),
    )
    try:
        result = _run_debug(version.script, body.model_dump())
    except HTTPException as exc:
        # 网关不可达等基础设施失败：也留痕（审计），然后原样抛出
        record.error = str(exc.detail)
        db.add(record)
        db.commit()
        wm_cache.invalidate_world_model()
        raise
    record.ok = result.ok
    record.duration_ms = result.duration_ms
    record.error = result.error
    if result.ok:
        record.response_payload = {"result": result.payload}
    db.add(record)
    db.commit()
    wm_cache.invalidate_world_model()
    return schemas.InvokeResult(
        ok=result.ok,
        payload=result.payload if result.ok else None,
        error=result.error,
        duration_ms=result.duration_ms,
        call_id=record.id,
    )


def service_out(db: Session, service: WorldModelService) -> schemas.ServiceOut:
    """服务输出（含版本号解析）。"""
    version_no = None
    if service.version_id:
        version = db.get(WorldModelScriptVersion, service.version_id)
        version_no = version.version_no if version else None
    return schemas.ServiceOut(
        id=service.id,
        project_id=service.project_id,
        version_id=service.version_id,
        version_no=version_no,
        name=service.name,
        description=service.description or "",
        status=service.status,
        endpoint_path=service.endpoint_path,
        applicable_object_types=service.applicable_object_types,
        preconditions=service.preconditions,
        created_at=service.created_at,
        updated_at=service.updated_at,
    )


# ──────────────────────────── 推演服务注册表（跨项目） ────────────────────────────


def get_service_by_id(db: Session, service_id: str) -> WorldModelService:
    service = db.get(WorldModelService, service_id)
    if service is None:
        raise HTTPException(404, "推演服务不存在或已被删除。")
    return service


def list_services(
    db: Session,
    *,
    keyword: str = "",
    status: str = "",
    page: int = 1,
    size: int = 20,
) -> schemas.ServiceListResponse:
    """跨项目服务注册表：服务 + 所属模型名 + 冻结版本号 + 调用统计。"""
    query = (
        db.query(WorldModelService, WorldModelProject.name)
        .join(WorldModelProject, WorldModelService.project_id == WorldModelProject.id)
    )
    if keyword.strip():
        like = f"%{keyword.strip()}%"
        query = query.filter(
            WorldModelService.name.like(like)
            | WorldModelService.description.like(like))
    if status:
        if status not in (
            SERVICE_STATUS_DRAFT, SERVICE_STATUS_ONLINE, SERVICE_STATUS_OFFLINE,
        ):
            raise HTTPException(400, f"未知服务状态：{status}")
        query = query.filter(WorldModelService.status == status)
    total = query.count()
    rows = (
        query.order_by(WorldModelService.updated_at.desc())
        .offset((page - 1) * size)
        .limit(size)
        .all()
    )
    services = [row[0] for row in rows]
    project_names = {row[0].id: row[1] for row in rows}

    version_nos: dict[str, int | None] = {}
    version_ids = [s.version_id for s in services if s.version_id]
    if version_ids:
        version_nos = dict(
            db.query(WorldModelScriptVersion.id, WorldModelScriptVersion.version_no)
            .filter(WorldModelScriptVersion.id.in_(version_ids))
            .all()
        )
    call_stats: dict[str, tuple[int, int]] = {}
    if services:
        stats = (
            db.query(
                WorldModelCallRecord.service_id,
                func.count(WorldModelCallRecord.id),
                func.count(WorldModelCallRecord.id).filter(
                    WorldModelCallRecord.ok.is_(False)),
            )
            .filter(WorldModelCallRecord.service_id.in_([s.id for s in services]))
            .group_by(WorldModelCallRecord.service_id)
            .all()
        )
        call_stats = {sid: (int(cnt), int(failed)) for sid, cnt, failed in stats}

    return schemas.ServiceListResponse(
        items=[
            schemas.ServiceSummary(
                id=service.id,
                project_id=service.project_id,
                project_name=project_names.get(service.id, ""),
                version_id=service.version_id,
                version_no=version_nos.get(service.version_id),
                name=service.name,
                description=service.description or "",
                status=service.status,
                endpoint_path=service.endpoint_path,
                applicable_object_types=service.applicable_object_types,
                preconditions=service.preconditions,
                call_count=call_stats.get(service.id, (0, 0))[0],
                failed_count=call_stats.get(service.id, (0, 0))[1],
                created_at=service.created_at,
                updated_at=service.updated_at,
            )
            for service in services
        ],
        total=total,
    )


def services_overview(db: Session) -> schemas.ServiceOverview:
    """推演服务页概览：服务状态计数 + 全局调用统计，与分页筛选无关。"""
    status_rows = dict(
        db.query(WorldModelService.status, func.count(WorldModelService.id))
        .group_by(WorldModelService.status)
        .all()
    )
    calls = call_records_overview(db)
    return schemas.ServiceOverview(
        total=sum(int(count) for count in status_rows.values()),
        online=int(status_rows.get(SERVICE_STATUS_ONLINE, 0)),
        offline=int(status_rows.get(SERVICE_STATUS_OFFLINE, 0)),
        call_total=calls.total,
        call_failed=calls.failed,
        avg_duration_ms=calls.avg_duration_ms,
    )


def service_summary_out(
    db: Session, service: WorldModelService,
) -> schemas.ServiceSummary:
    """单个服务的注册表条目输出（与列表字段口径一致）。"""
    project = db.get(WorldModelProject, service.project_id)
    version_no = None
    if service.version_id:
        version = db.get(WorldModelScriptVersion, service.version_id)
        version_no = version.version_no if version else None
    total = (
        db.query(func.count(WorldModelCallRecord.id))
        .filter(WorldModelCallRecord.service_id == service.id)
        .scalar() or 0
    )
    failed = (
        db.query(func.count(WorldModelCallRecord.id))
        .filter(WorldModelCallRecord.service_id == service.id)
        .filter(WorldModelCallRecord.ok.is_(False))
        .scalar() or 0
    )
    return schemas.ServiceSummary(
        id=service.id,
        project_id=service.project_id,
        project_name=project.name if project else "",
        version_id=service.version_id,
        version_no=version_no,
        name=service.name,
        description=service.description or "",
        status=service.status,
        endpoint_path=service.endpoint_path,
        applicable_object_types=service.applicable_object_types,
        preconditions=service.preconditions,
        call_count=int(total),
        failed_count=int(failed),
        created_at=service.created_at,
        updated_at=service.updated_at,
    )
