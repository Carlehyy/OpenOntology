#!/usr/bin/env python3
"""真实 LLM 的业务探索端到端与极限场景验收。

脚本只从环境变量读取 DeepSeek 配置：

    DEEPSEEK_API_KEY
    DEEPSEEK_BASE_URL
    DEEPSEEK_MODEL

运行过程使用独立临时 SQLite 和上传目录。API key 仅在当前 Python 进程内存中
注入 LLM 调用参数，不进入 argv、模型配置、数据库、报告或仓库文件；结束前还会
扫描临时目录，断言密钥原始字节未落盘。

覆盖范围：

* 真实 ``run_exploration_turn`` 生成业务画布并通过十道质量门（零流程时流程编排门 vacuous pass）；
* 多轮对已有对象做稀疏增量更新，已确认属性/关系与子项 ID 不丢失；
* 超长用户附件后段事实可检索，Agent 草稿正文不冒充用户证据；
* 真实 LLM 叙述 + 确定性骨架生成需求文档；
* 文档 stale 阻断、显式越权留痕、文档到草稿成功转换；
* ready 画布中的不可无损语义默认阻断，显式越权留痕。

成功时 stdout 只输出一份不含敏感值的 JSON 报告；失败时同样输出 JSON 且退出非 0。
进度信息写到 stderr，可用 ``--quiet`` 关闭。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
from typing import Any, Callable
from urllib.parse import urlsplit
import uuid


BACKEND_ROOT = Path(__file__).resolve().parents[1]
_SECRET_PATTERN = re.compile(
    r"(?i)(?:bearer\s+)?(?:sk|api|token|secret)[-_][A-Za-z0-9._~+/=-]{10,}"
)
_REQUIRED_ENV = ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL")


class LiveCheckError(AssertionError):
    """A failed live acceptance assertion."""


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="在隔离临时环境中运行真实 DeepSeek 业务探索 E2E/极限测试。",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
        help="单次 LLM HTTP 请求超时；必须在 10-600 秒之间。",
    )
    parser.add_argument(
        "--context-tokens",
        type=int,
        default=64_000,
        help="传给业务探索上下文预算的 token 上限。",
    )
    parser.add_argument(
        "--output-tokens",
        type=int,
        default=4_096,
        help="传给业务探索单次输出预算的 token 上限。",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="关闭 stderr 的阶段进度 JSON，仅保留 stdout 最终报告。",
    )
    args = parser.parse_args()
    if not 10 <= args.timeout_seconds <= 600:
        parser.error("--timeout-seconds 必须在 10-600 之间")
    if args.context_tokens < 8_192:
        parser.error("--context-tokens 不能小于 8192")
    if not 1_024 <= args.output_tokens <= args.context_tokens // 2:
        parser.error("--output-tokens 必须在 1024 到 context-tokens/2 之间")
    return args


def _redact(value: Any, api_key: str) -> str:
    text = str(value)
    if api_key:
        text = text.replace(api_key, "***")
    return _SECRET_PATTERN.sub("***", text)


class _RedactingFormatter(logging.Formatter):
    def __init__(self, api_key: str):
        super().__init__("%(levelname)s %(name)s: %(message)s")
        self._api_key = api_key

    def format(self, record: logging.LogRecord) -> str:
        return _redact(super().format(record), self._api_key)


def _configure_logging(api_key: str) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_RedactingFormatter(api_key))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.ERROR)


def _progress(quiet: bool, stage: str, **metrics: Any) -> None:
    if quiet:
        return
    payload = {"stage": stage, **metrics}
    print(json.dumps(payload, ensure_ascii=False, default=str), file=sys.stderr, flush=True)


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise LiveCheckError(message)


def _environment() -> tuple[str, str, str]:
    values = {name: (os.environ.get(name) or "").strip() for name in _REQUIRED_ENV}
    missing = [name for name, value in values.items() if not value]
    if missing:
        raise LiveCheckError("缺少环境变量：" + ", ".join(missing))

    base_url = values["DEEPSEEK_BASE_URL"].rstrip("/")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise LiveCheckError(
            "DEEPSEEK_BASE_URL 必须是不含账号、密码、查询串和片段的 http(s) API 地址"
        )
    return values["DEEPSEEK_API_KEY"], base_url, values["DEEPSEEK_MODEL"]


def _configure_isolation(root: Path) -> tuple[Path, Path]:
    if "app.shared.database" in sys.modules:
        raise LiveCheckError("数据库模块已提前导入，无法保证本次运行使用独立临时 SQLite")
    database = root / "exploration-live.db"
    uploads = root / "uploads"
    os.environ.update({
        "ENVIRONMENT": "test",
        "DATABASE_URL": f"sqlite:///{database}",
        "UPLOADS_DIR": str(uploads),
        "SECRET_KEY": secrets.token_urlsafe(48),
        # 不允许开发机 .env 把本脚本带到真实基础设施。
        "REDIS_URL": "redis://127.0.0.1:1/15",
        "NEO4J_URI": "bolt://127.0.0.1:1",
        "SENTINEL_SCAN_ENABLED": "0",
    })
    sys.path.insert(0, str(BACKEND_ROOT))
    return database, uploads


def _files_containing(root: Path, needle: bytes) -> list[str]:
    if not needle:
        return []
    found: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if needle in path.read_bytes():
                found.append(str(path.relative_to(root)))
        except OSError:
            continue
    return sorted(found)


def _event_usage(events: list[dict[str, Any]]) -> dict[str, int]:
    answer = next(
        (event for event in reversed(events) if event.get("type") == "answer"), {}
    )
    usage = answer.get("usage") or {}
    return {
        "inputTokens": int(usage.get("inputTokens") or 0),
        "outputTokens": int(usage.get("outputTokens") or 0),
    }


def _semantic_blocking_canvas(canvas_module: Any) -> dict[str, Any]:
    """A readiness-green canvas that intentionally has one lossy semantic."""
    canvas = canvas_module.empty_canvas()
    canvas, _, errors = canvas_module.upsert_elements(canvas, "object", [
        {
            "name": "Order",
            "displayName": "订单",
            "keyAttribute": "order_id",
            "attributes": [
                {
                    "name": "order_id",
                    "displayName": "订单号",
                    "typeHint": "文本",
                    "required": True,
                },
                {
                    "name": "status",
                    "displayName": "状态",
                    "typeHint": "枚举",
                    "enum": ["待确认", "已确认", "已取消"],
                },
            ],
            "relations": [{
                "target": "Customer",
                "displayName": "下单客户",
                "cardinality": "many-to-one",
            }],
        },
        {
            "name": "Customer",
            "displayName": "客户",
            "keyAttribute": "customer_id",
            "attributes": [{
                "name": "customer_id",
                "displayName": "客户编号",
                "typeHint": "文本",
                "required": True,
            }],
        },
    ])
    _require(not errors, f"构造 semantic 画布 object 失败: {errors}")
    canvas, _, errors = canvas_module.upsert_elements(canvas, "actor", [{
        "name": "Operator",
        "displayName": "运营",
        "kind": "role",
        "responsibilities": ["确认订单", "处理取消"],
    }])
    _require(not errors, f"构造 semantic 画布 actor 失败: {errors}")
    canvas, _, errors = canvas_module.upsert_elements(canvas, "behavior", [
        {
            "name": "confirm_order",
            "displayName": "确认订单",
            "actor": "Operator",
            "object": "Order",
            "trigger": "收到付款",
            "outcome": "订单从待确认变为已确认",
        },
        {
            "name": "cancel_order",
            "displayName": "取消订单",
            "actor": "Operator",
            "object": "Order",
            "trigger": "客户取消",
            "outcome": "订单从待确认变为已取消",
        },
    ])
    _require(not errors, f"构造 semantic 画布 behavior 失败: {errors}")
    canvas, _, errors = canvas_module.upsert_elements(canvas, "rule", [{
        "name": "object_approval",
        "displayName": "订单对象审批",
        "kind": "approval",
        "appliesTo": "Order",
        "statement": "订单金额 >= 50000 元需要审批",
    }])
    _require(not errors, f"构造 semantic 画布 rule 失败: {errors}")
    canvas, _, errors = canvas_module.upsert_elements(canvas, "event", [{
        "name": "confirmed",
        "displayName": "订单已确认",
        "source": "confirm_order",
        "payload": ["order_id"],
        "consequences": ["通知客户"],
    }])
    _require(not errors, f"构造 semantic 画布 event 失败: {errors}")
    canvas, _, errors = canvas_module.upsert_elements(canvas, "scenario", [{
        "name": "order_flow",
        "displayName": "订单处理流程",
        "goal": "完成订单处理",
        "actors": ["Operator"],
        "steps": ["运营确认订单", "如果确认成功则完成，否则取消", "订单处理结束"],
        "objects": ["Order", "Customer"],
        "behaviors": ["confirm_order", "cancel_order"],
        "branches": [
            {
                "fromStep": 2,
                "toStep": 3,
                "condition": "确认成功，状态 = 已确认",
            },
            {
                "fromStep": 2,
                "toStep": 3,
                "condition": "客户取消，状态 = 已取消",
            },
        ],
        "expectedOutcome": "订单状态明确",
    }])
    _require(not errors, f"构造 semantic 画布 scenario 失败: {errors}")
    return canvas


_BASE_IMPORT_PROMPT = """这是一次访谈完成后的批量导入，下面每个字段都已由业务负责人逐项确认，
不存在待澄清口径。请在本回合调用 upsert_elements，把下列六类模型完整写入画布（本次不导入流程模型）。可以在同一响应
并行调用六次工具，但并行写入时必须省略 expected_canvas_version；如果使用乐观锁，就一次只写
一类并以上一次工具返回的新版本继续。不要用自然语言复述代替工具，不要 raise_questions、
不要出图，也不要增添未给出的概念。

1. object
- Order/订单：业务主键 order_no；属性 order_no(文本,必填)、amount(金额)、
  status(枚举: 待支付/已支付/已取消)；到 Customer 的关系显示名“下单客户”，
  cardinality=many-to-one。
- Customer/客户：业务主键 customer_no；属性 customer_no(文本,必填)。
2. actor
- Sales/销售，kind=role，职责“确认支付”和“取消订单”。
3. behavior
- confirm_pay/确认支付：actor=Sales，object=Order，trigger=收到银行回单，
  outcome=订单从待支付变为已支付。
- cancel_order/取消订单：actor=Sales，object=Order，trigger=客户在支付前取消，
  outcome=订单从待支付变为已取消。
4. rule
- big_amount/大额审批：kind=approval，applies_to=confirm_pay，
  statement=金额 >= 50000 元需要财务总监审批。
5. event
- order_paid/订单已支付：source=confirm_pay，payload=[order_no,amount]，
  consequences=[通知仓库发货]。
6. scenario
- pay_flow/支付流程：goal=完成订单支付，actors=[Sales]，
  steps=[销售确认回单, 如果金额 >= 50000 元则走审批, 订单变为已支付]，
  objects=[Order,Customer]，behaviors=[confirm_pay,cancel_order]，
  branches=[{from_step:2,to_step:3,condition:"金额 >= 50000 元且审批通过"},
            {from_step:2,to_step:3,condition:"金额 < 50000 元"}]，
  expected_outcome=订单进入已支付状态。
"""


def _execute(
    root: Path,
    api_key: str,
    base_url: str,
    model_name: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    database_path, _ = _configure_isolation(root)

    # App imports must happen only after the isolated environment is installed.
    from fastapi import HTTPException

    from app.auth.models import User
    from app.database import Base, SessionLocal, engine
    from app.exploration import canvas as C
    from app.exploration import converter as CV
    from app.exploration import readiness as R
    from app.exploration import router as exploration_router
    from app.exploration import schemas as S
    from app.exploration import workspace as W
    from app.exploration.document import (
        _NARRATIVE_FALLBACK,
        document_source_state,
        generate_document,
    )
    from app.exploration.models import (
        ExplorationAttachment,
        ExplorationDocument,
        ExplorationDraft,
        ExplorationMessage,
        ExplorationSession,
    )
    from app.exploration.orchestrator import run_exploration_turn
    import app.exploration.orchestrator as orchestrator
    from app.model_configs.models import ModelCallLog, ModelConfig

    tables = [
        User.__table__,
        ModelConfig.__table__,
        ModelCallLog.__table__,
        ExplorationSession.__table__,
        ExplorationMessage.__table__,
        ExplorationDocument.__table__,
        ExplorationDraft.__table__,
        ExplorationAttachment.__table__,
    ]
    Base.metadata.create_all(bind=engine, tables=tables)

    db = SessionLocal()
    started = time.monotonic()
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "status": "running",
        "provider": {
            "host": (urlsplit(base_url).hostname or "").lower(),
            "model": _redact(model_name, api_key),
            "apiKeySource": "environment",
            "apiKeyPersisted": False,
        },
        "isolation": {
            "temporarySQLite": True,
            "temporaryUploads": True,
            "secretScanPassed": False,
        },
        "checks": {},
        "metrics": {},
    }

    execution_error: Exception | None = None
    try:
        user = User(
            id=str(uuid.uuid4()),
            username=f"exploration-live-{uuid.uuid4().hex[:10]}",
            email=f"exploration-live-{uuid.uuid4().hex[:10]}@invalid.local",
            password_hash="live-e2e-no-login",
            role="admin",
            is_active=True,
        )
        model_config = ModelConfig(
            id=str(uuid.uuid4()),
            name="DeepSeek exploration live E2E",
            provider="compatible",
            config_type="llm",
            api_base=base_url,
            # Deliberately empty: the live key is never persisted, even encrypted.
            api_key_encrypted=None,
            models=[model_name],
            options={
                "max_context_tokens": args.context_tokens,
                "max_output_tokens": args.output_tokens,
            },
            enabled=True,
            is_default=True,
            created_by=user.id,
        )
        session = ExplorationSession(
            id=str(uuid.uuid4()),
            user_id=user.id,
            title="真实 LLM 业务探索验收",
            canvas=C.empty_canvas(),
        )
        db.add_all([user, model_config, session])
        db.commit()
        db.refresh(session)

        def live_call_kwargs(config: Any) -> dict[str, Any] | None:
            if config is None:
                return None
            return {
                "model_config_id": config.id,
                "provider": "compatible",
                "api_key": api_key,
                "api_base": base_url,
                "model": model_name,
                "timeout_seconds": args.timeout_seconds,
                "max_context_tokens": args.context_tokens,
                "max_output_tokens": args.output_tokens,
            }

        # Keep production selection and orchestration; only replace persisted-key
        # decoding with an in-memory credential provider for this isolated process.
        orchestrator.llm_call_kwargs = live_call_kwargs
        exploration_router.llm_call_kwargs = live_call_kwargs

        turns: list[dict[str, Any]] = []

        def run_turn(label: str, message: str) -> tuple[list[dict[str, Any]], str]:
            turn_started = time.monotonic()
            events = list(run_exploration_turn(
                db,
                session.id,
                user,
                message,
                model_id=model_config.id,
                web_search=False,
            ))
            error = next(
                (event for event in events if event.get("type") == "error"), None
            )
            _require(error is None, f"{label} 回合失败: {(error or {}).get('message')}")
            _require(events and events[-1].get("type") == "done", f"{label} 缺少 done 事件")
            answer = next(
                (event for event in reversed(events) if event.get("type") == "answer"),
                None,
            )
            _require(answer is not None, f"{label} 缺少 answer 事件")
            content = str((answer or {}).get("content") or "").strip()
            _require(content, f"{label} 返回空答复")
            db.refresh(session)
            usage = _event_usage(events)
            steps = [event for event in events if event.get("type") == "step"]
            turns.append({
                "label": label,
                "durationMs": int((time.monotonic() - turn_started) * 1000),
                "stepCount": len(steps),
                "tools": [str(step.get("tool") or "") for step in steps],
                **usage,
            })
            _progress(
                args.quiet,
                label,
                durationMs=turns[-1]["durationMs"],
                steps=len(steps),
                canvasVersion=session.canvas_version,
            )
            return events, content

        # 1) Real model -> the six import canvas kinds (process persistence is step 7).
        run_turn("six_kind_import", _BASE_IMPORT_PROMPT)
        expected_names = {
            "objects": {"order", "customer"},
            "actors": {"sales"},
            "behaviors": {"confirm_pay", "cancel_order"},
            "events": {"order_paid"},
            "rules": {"big_amount"},
            "scenarios": {"pay_flow"},
        }

        def missing_expected() -> dict[str, list[str]]:
            canonical = C._ensure_canvas(session.canvas)
            missing: dict[str, list[str]] = {}
            for key, expected in expected_names.items():
                actual = {C.norm_name(item.get("name", "")) for item in canonical[key]}
                gap = sorted(
                    original
                    for original in expected
                    if C.norm_name(original) not in actual
                )
                if gap:
                    missing[key] = gap
            return missing

        missing = missing_expected()
        if missing:
            run_turn(
                "six_kind_repair",
                "上一回合仍有已确认模型没有进入 canonical 画布："
                + json.dumps(missing, ensure_ascii=False)
                + "。请再次按下面完整清单，仅补齐缺失项；必须调用 upsert_elements，"
                  "不要提问、不要改动已存在项。\n"
                + _BASE_IMPORT_PROMPT,
            )
            missing = missing_expected()
        _require(not missing, f"导入画布仍缺少指定元素: {missing}")

        canvas = C._ensure_canvas(session.canvas)
        readiness = R.evaluate(canvas)
        _require(
            readiness["ready"],
            "真实模型生成的导入画布未通过质量门: "
            + "；".join(
                item
                for gate in readiness["gates"]
                for item in gate["blockingItems"]
            )[:1_500],
        )
        counts = {key: len(canvas[key]) for key in expected_names}
        _require(all(value > 0 for value in counts.values()), "导入画布存在空类别")
        order = next(item for item in canvas["objects"] if C.norm_name(item["name"]) == "order")
        old_attributes = {
            C.norm_name(item.get("name", "")): str(item.get("id") or "")
            for item in order.get("attributes") or []
        }
        old_relations = {
            (
                C.norm_name(item.get("target", "")),
                C.norm_name(item.get("name") or item.get("display_name") or ""),
            ): str(item.get("id") or "")
            for item in order.get("relations") or []
        }
        _require(
            {C.norm_name(name) for name in ("order_no", "amount", "status")}
            <= set(old_attributes),
            f"Order 初始属性不完整: {sorted(old_attributes)}",
        )
        _require(old_relations, "Order 初始关系未进入 canonical")
        report["checks"]["sixKindCanvas"] = {
            "passed": True,
            "counts": counts,
            "canvasVersion": int(session.canvas_version or 0),
            "readiness": {
                key: readiness[key]
                for key in (
                    "ready",
                    "gatesPassed",
                    "gatesTotal",
                    "blockingCount",
                    "advisoryCount",
                )
            },
        }

        # 2) Long-tail attachment retrieval and source isolation.
        user_marker = f"USER-{secrets.token_hex(8).upper()}"
        agent_marker = f"AGENT-{secrets.token_hex(8).upper()}"
        filler_line = "一般背景：本段仅用于拉长资料，不包含任何有效专项口径。\n"
        filler = filler_line * ((36_000 // len(filler_line)) + 1)
        user_text = (
            filler
            + "\n# 客户升级口径\n"
            + f"客户升级代码：{user_marker}\n"
            + "该代码由业务负责人确认，是本资料唯一有效值。\n"
        )
        marker_offset = user_text.index(user_marker)
        _require(marker_offset > 25_000, "用户附件唯一事实没有位于 25000 字之后")
        user_file = W.create_text(
            db,
            session,
            "evidence/customer-policy.txt",
            user_text,
            source="upload",
        )
        W.create_text(
            db,
            session,
            "drafts/unconfirmed-agent-note.md",
            "# AI 未确认草稿\n"
            f"客户升级代码：{agent_marker}\n"
            "此值是 AI 猜测，未得到用户确认。",
            source="agent",
        )
        _, attachment_answer = run_turn(
            "long_attachment_retrieval",
            "只根据“用户提供的参考资料（业务事实证据）”回答："
            "客户升级代码的精确值是什么？只输出一句结论，不修改画布或文件，"
            "不要引用、比较或复述 AI 工作草稿中的值。",
        )
        _require(
            user_marker in attachment_answer,
            "真实模型没有从超长用户附件后段找回唯一事实",
        )
        _require(
            agent_marker not in attachment_answer,
            "真实模型把 Agent 未确认草稿值带入了事实答复",
        )
        report["checks"]["attachmentSourceIsolation"] = {
            "passed": True,
            "userMarkerOffset": marker_offset,
            "storedChars": len(user_file.extracted_text or ""),
            "originalChars": int(user_file.char_count or 0),
            "userMarkerSha256": hashlib.sha256(user_marker.encode()).hexdigest(),
            "agentMarkerExcluded": True,
        }

        # 3) Real narrative + deterministic document skeleton.
        live_kwargs = live_call_kwargs(model_config)
        document = generate_document(db, session, live_kwargs)
        _require(
            _NARRATIVE_FALLBACK not in document.content_md
            and "未配置 LLM 或叙述生成失败" not in document.content_md,
            "真实 LLM 文档叙述调用降级成了占位文本",
        )
        for heading in (
            "## 3. 主体模型",
            "## 4. 对象模型",
            "## 5. 行为模型",
            "## 6. 事件模型",
            "## 7. 规则模型",
            "## 8. 流程模型",
            "## 9. 场景模型",
            "## 11. 质量门检查",
        ):
            _require(heading in document.content_md, f"需求文档缺少章节: {heading}")
        for expected in ("Order", "Sales", "confirm_pay", "order_paid", "big_amount", "pay_flow"):
            _require(expected in document.content_md, f"需求文档未保留画布元素: {expected}")
        source_state = document_source_state(document, session)
        _require(not source_state["is_stale"], "刚生成的文档不应 stale")
        source_version = int(source_state["source_canvas_version"] or 0)
        report["checks"]["documentFidelity"] = {
            "passed": True,
            "version": int(document.version or 0),
            "sourceCanvasVersion": source_version,
            "fingerprint": source_state["source_canvas_fingerprint"],
            "markdownChars": len(document.content_md or ""),
            "liveNarrative": True,
        }
        _progress(
            args.quiet,
            "document_generation",
            sourceCanvasVersion=source_version,
            markdownChars=len(document.content_md or ""),
        )

        # 4) A real second turn must read canonical state before sparse nested update.
        incremental_events, _ = run_turn(
            "incremental_update",
            "这是已确认的增量变更：先调用 get_canvas_elements 读取 Order 的完整 canonical "
            "attributes/relations 和最新 canvasVersion；随后只给 Order 稀疏补丁，新增属性 "
            "currency（显示名=币种，type_hint=枚举，enum=[CNY,USD]，required=true）。"
            "不要重发、删除或清空任何既有属性和关系，也不要修改其它模型元素。",
        )
        incremental_tools = [
            str(event.get("tool") or "")
            for event in incremental_events
            if event.get("type") == "step"
        ]
        _require(
            "get_canvas_elements" in incremental_tools
            and "upsert_elements" in incremental_tools
            and incremental_tools.index("get_canvas_elements")
            < incremental_tools.index("upsert_elements"),
            f"增量更新没有先读 canonical 再写入: {incremental_tools}",
        )
        canvas = C._ensure_canvas(session.canvas)
        order_after = next(
            item for item in canvas["objects"] if C.norm_name(item["name"]) == "order"
        )
        new_attributes = {
            C.norm_name(item.get("name", "")): str(item.get("id") or "")
            for item in order_after.get("attributes") or []
        }
        new_relations = {
            (
                C.norm_name(item.get("target", "")),
                C.norm_name(item.get("name") or item.get("display_name") or ""),
            ): str(item.get("id") or "")
            for item in order_after.get("relations") or []
        }
        _require("currency" in new_attributes, "增量属性 currency 未进入 canonical")
        _require(
            set(old_attributes) <= set(new_attributes),
            f"增量更新丢失已有属性: {sorted(set(old_attributes) - set(new_attributes))}",
        )
        _require(
            all(new_attributes[name] == item_id for name, item_id in old_attributes.items()),
            "增量更新重建了已有属性，子项 ID 稳定性被破坏",
        )
        _require(new_relations == old_relations, "增量更新修改或丢失了既有关系")
        report["checks"]["incrementalMerge"] = {
            "passed": True,
            "canonicalReadBeforeWrite": True,
            "attributesBefore": len(old_attributes),
            "attributesAfter": len(new_attributes),
            "preservedAttributeIds": len(old_attributes),
            "preservedRelationIds": len(old_relations),
            "canvasVersion": int(session.canvas_version or 0),
        }

        # 5) The old document is now stale: default block, forced path is audited.
        stale_state = document_source_state(document, session)
        _require(stale_state["is_stale"], "画布变化后旧文档没有标记 stale")

        def expect_http(
            fn: Callable[[], Any],
            status_code: int,
            code: str,
        ) -> dict[str, Any]:
            try:
                fn()
            except HTTPException as exc:
                _require(
                    exc.status_code == status_code,
                    f"预期 HTTP {status_code}，实际 {exc.status_code}",
                )
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                _require(detail.get("code") == code, f"预期错误 {code}，实际 {detail}")
                return detail
            raise LiveCheckError(f"预期 {code} 阻断，但调用成功")

        stale_detail = expect_http(
            lambda: exploration_router.create_draft(
                document.id,
                S.GenerateDraftRequest(force=False),
                db,
                user,
            ),
            409,
            "stale_document",
        )
        stale_forced = exploration_router.create_draft(
            document.id,
            S.GenerateDraftRequest(force=True),
            db,
            user,
        )["data"]
        stale_report = stale_forced["report"]
        _require(
            stale_report.get("staleDocumentOverride") is True
            and (stale_report.get("sourceDocument") or {}).get("isStale") is True,
            "强制使用 stale 文档生成草稿时没有留下来源越权记录",
        )
        stale_draft = stale_forced["draft"]
        _require(
            stale_draft.get("objectTypes")
            and stale_draft.get("actions")
            and stale_draft.get("linkTypes"),
            "文档到草稿链路没有生成核心本体元素",
        )
        report["checks"]["staleDocumentGate"] = {
            "passed": True,
            "defaultStatus": 409,
            "defaultCode": stale_detail["code"],
            "sourceCanvasVersion": stale_state["source_canvas_version"],
            "currentCanvasVersion": stale_state["current_canvas_version"],
            "forcedAudit": True,
            "draftCounts": {
                key: len(stale_draft.get(key) or [])
                for key in (
                    "objectTypes",
                    "linkTypes",
                    "actions",
                    "functions",
                    "sentinels",
                )
            },
        }

        # A fresh document from the current canvas must traverse the default,
        # non-forced document -> draft path with no gate override.
        current_document = generate_document(db, session, None)
        current_draft = exploration_router.create_draft(
            current_document.id,
            S.GenerateDraftRequest(force=False),
            db,
            user,
        )["data"]
        current_report = current_draft["report"]
        _require(
            (current_report.get("readiness") or {}).get("ready") is True,
            "当前文档到草稿的默认链路没有保留 readiness=ready",
        )
        _require(
            not current_report.get("gateOverride")
            and not current_report.get("staleDocumentOverride")
            and not current_report.get("semanticOverride"),
            "当前文档到草稿的默认链路不应带任何越权标记",
        )
        _require(
            (current_report.get("semanticFidelity") or {}).get("readyToApply") is True,
            "当前文档到草稿存在未处理的 blocking semantic issue",
        )
        report["checks"]["documentToDraft"] = {
            "passed": True,
            "forced": False,
            "readinessReady": True,
            "semanticReady": True,
            "draftCounts": {
                key: len((current_draft.get("draft") or {}).get(key) or [])
                for key in (
                    "objectTypes",
                    "linkTypes",
                    "actions",
                    "functions",
                    "sentinels",
                )
            },
        }

        # 6) A readiness-green document with object-level approval is lossy today.
        semantic_canvas = _semantic_blocking_canvas(C)
        semantic_readiness = R.evaluate(semantic_canvas)
        _require(
            semantic_readiness["ready"],
            "semantic 阻断夹具必须先通过 readiness，避免被质量门提前截断",
        )
        semantic_session = ExplorationSession(
            id=str(uuid.uuid4()),
            user_id=user.id,
            title="语义阻断验收",
            canvas=semantic_canvas,
            canvas_version=1,
        )
        db.add(semantic_session)
        db.commit()
        semantic_document = generate_document(db, semantic_session, None)
        semantic_detail = expect_http(
            lambda: exploration_router.create_draft(
                semantic_document.id,
                S.GenerateDraftRequest(force=False),
                db,
                user,
            ),
            422,
            "semantic_conversion_blocked",
        )
        semantic_codes = sorted({
            str(item.get("code") or "")
            for item in semantic_detail.get("semanticIssues") or []
        })
        _require(
            "object_approval_unsupported" in semantic_codes,
            f"semantic 阻断没有指出对象级审批: {semantic_codes}",
        )
        semantic_forced = exploration_router.create_draft(
            semantic_document.id,
            S.GenerateDraftRequest(force=True),
            db,
            user,
        )["data"]
        semantic_report = semantic_forced["report"]
        _require(
            semantic_report.get("semanticOverride") is True,
            "semantic 强制生成没有留下越权记录",
        )
        _require(
            int((semantic_report.get("semanticFidelity") or {}).get("blockingCount") or 0)
            >= 1,
            "semantic 强制草稿报告没有保留 blocking 计数",
        )
        report["checks"]["semanticConversionGate"] = {
            "passed": True,
            "readinessReady": True,
            "defaultStatus": 422,
            "defaultCode": semantic_detail["code"],
            "blockingCodes": semantic_codes,
            "forcedAudit": True,
        }

        # 7) P0 process model: persistence + 10-gate readiness + draft coverage.
        process_canvas = _semantic_blocking_canvas(C)
        process_canvas, _, process_errors = C.upsert_elements(process_canvas, "process", [{
            "name": "order_handling",
            "displayName": "订单处理主流程",
            "goal": "完成订单从确认到关闭的处理",
            "trigger": "客户提交订单",
            "steps": [
                {"seq": 1, "name": "运营确认订单", "actor": "Operator",
                 "behavior": "confirm_order"},
                {"seq": 2, "name": "客户取消收尾", "actor": "Operator",
                 "behavior": "cancel_order"},
            ],
            "branches": [
                {"fromStep": 1, "toStep": 2, "condition": "客户取消或确认失败",
                 "kind": "exception"},
                {"fromStep": 1, "toStep": None, "condition": "确认成功"},
            ],
            "objects": ["Order"],
            "metrics": [{
                "name": "confirm_lead_time", "displayName": "确认时效",
                "formula": "订单确认时长 ≤ 2 小时", "sourceObjects": ["Order"],
                "target": "≤ 1 小时",
            }],
            "expectedOutcome": "订单状态明确闭环",
        }])
        _require(not process_errors, f"流程模型落库失败: {process_errors}")
        process_readiness = R.evaluate(process_canvas)
        _require(
            process_readiness["ready"],
            "含流程画布未通过质量门: "
            + "；".join(
                item
                for gate in process_readiness["gates"]
                for item in gate["blockingItems"]
            )[:1_500],
        )
        _require(process_readiness["gatesTotal"] == 10, "质量门总数不是 10")
        process_gate = next(
            gate for gate in process_readiness["gates"] if gate["id"] == "processes"
        )
        _require(process_gate["passed"], f"流程门未通过: {process_gate['blockingItems']}")
        _, process_draft_report = CV.build_draft(process_canvas)
        _require(
            not any(
                "process" in entry for entry in process_draft_report["scenarioCoverage"]
            ),
            "流程引用完整时 coverage 不应出现 process 条目",
        )
        # 引用缺失时 coverage 追加判别式 process 条目（形状锁定）。
        broken_canvas, _, broken_errors = C.upsert_elements(process_canvas, "process", [{
            "name": "order_handling",
            "objects": ["Order", "GhostObject"],
            "steps": [{"seq": 2, "name": "客户取消收尾", "behavior": "ghost_behavior"}],
        }])
        _require(not broken_errors, f"流程增量补丁失败: {broken_errors}")
        _, broken_report = CV.build_draft(broken_canvas)
        process_entry = next(
            (entry for entry in broken_report["scenarioCoverage"] if "process" in entry),
            None,
        )
        _require(process_entry is not None, "流程引用缺失时 coverage 没有 process 条目")
        _require(
            set(process_entry) == {"process", "missingObjects", "missingBehaviors"}
            and process_entry["missingObjects"] == ["GhostObject"]
            and process_entry["missingBehaviors"] == ["ghost_behavior"],
            f"process 覆盖条目形状不符: {process_entry}",
        )
        report["checks"]["processModelPersistence"] = {
            "passed": True,
            "gatesTotal": int(process_readiness["gatesTotal"]),
            "processGatePassed": True,
            "coverageDiscriminator": True,
        }

        db.expire_all()
        stored_config = db.query(ModelConfig).filter(ModelConfig.id == model_config.id).one()
        _require(
            not stored_config.api_key_encrypted,
            "隔离模型配置不应保存 API key（包括加密密文）",
        )
        logs = (
            db.query(ModelCallLog)
            .filter(ModelCallLog.model_config_id == model_config.id)
            .order_by(ModelCallLog.created_at.asc())
            .all()
        )
        _require(logs, "没有模型调用审计，无法证明真实 LLM 路径被执行")
        _require(
            all(log.status == "success" for log in logs),
            "存在失败的真实模型调用: "
            + ", ".join(str(log.status) for log in logs),
        )
        assistant_messages = (
            db.query(ExplorationMessage)
            .filter(
                ExplorationMessage.session_id == session.id,
                ExplorationMessage.role == "assistant",
            )
            .all()
        )
        persisted_usage = {
            "inputTokens": sum(
                int((message.token_usage or {}).get("inputTokens") or 0)
                for message in assistant_messages
            ),
            "outputTokens": sum(
                int((message.token_usage or {}).get("outputTokens") or 0)
                for message in assistant_messages
            ),
        }
        _require(
            persisted_usage["inputTokens"] > 0 and persisted_usage["outputTokens"] > 0,
            "持久化消息缺少正数 token usage",
        )
        report["metrics"] = {
            "durationMs": int((time.monotonic() - started) * 1000),
            "llmCalls": len(logs),
            "turns": turns,
            "persistedUsage": persisted_usage,
            "modelCallStatuses": {
                status: sum(1 for log in logs if log.status == status)
                for status in sorted({str(log.status) for log in logs})
            },
        }
        _require(
            all(bool(check.get("passed")) for check in report["checks"].values()),
            "存在未通过检查",
        )
        report["status"] = "passed"
    except Exception as exc:  # noqa: BLE001 - scan for key bytes before propagating
        execution_error = exc
    finally:
        db.close()
        engine.dispose()

    leak_files = _files_containing(root, api_key.encode())
    _require(not leak_files, "检测到 API key 原始字节落盘: " + ", ".join(leak_files))
    if execution_error is not None:
        raise execution_error.with_traceback(execution_error.__traceback__)
    report["isolation"]["secretScanPassed"] = True
    report["isolation"]["filesScanned"] = sum(
        1 for path in root.rglob("*") if path.is_file()
    )
    report["isolation"]["databaseBytes"] = (
        database_path.stat().st_size if database_path.exists() else 0
    )
    return report


def main() -> int:
    args = _args()
    api_key = ""
    try:
        api_key, base_url, model_name = _environment()
        _configure_logging(api_key)
        with tempfile.TemporaryDirectory(
            prefix="openontology-exploration-live-e2e-"
        ) as temp_root:
            report = _execute(
                Path(temp_root),
                api_key,
                base_url,
                model_name,
                args,
            )
        api_key = ""
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        return 0
    except Exception as exc:  # noqa: BLE001 - failure must become non-zero JSON
        safe_error = _redact(exc, api_key)[:1_500]
        failed = {
            "schemaVersion": 1,
            "status": "failed",
            "error": {
                "type": type(exc).__name__,
                "message": safe_error,
            },
        }
        api_key = ""
        print(json.dumps(failed, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
