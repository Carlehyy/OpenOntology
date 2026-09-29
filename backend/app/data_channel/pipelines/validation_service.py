"""Pipeline validation, publish readiness, and release orchestration."""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.config import settings
from app.data_channel.pipelines.contracts import (
    PublishBody,
    ValidateDefinitionsBody,
    ValidateDefinitionsResult,
    ValidateResult,
)
from app.data_channel.pipelines.execution_service import dry_run_uri
from app.data_channel.pipelines.models import Pipeline, PipelineVersion

logger = logging.getLogger(__name__)


def is_n8n_pipeline(pipeline: Pipeline) -> bool:
    """Return whether a pipeline is a steward-managed n8n shadow."""
    return (pipeline.definition or {}).get("engine") == "n8n"


def is_python_pipeline(pipeline: Pipeline) -> bool:
    """Return whether a pipeline is a user-authored Python script pipeline."""
    return (pipeline.definition or {}).get("engine") == "python"


def column_definitions_hash(definitions: list | None) -> str:
    """Canonical contract fingerprint used by publish attestations."""
    from app.data_channel.datasets.lake_gate import normalize_definitions
    from app.data_channel.steward.service import canonical_json_hash

    return canonical_json_hash(normalize_definitions(definitions))


def pipeline_execution_hash(
    *,
    definition: dict | None,
    source_dataset_id: str | None,
    route: str | None,
    spec: dict | None,
) -> str:
    """Bind every pipeline setting that can change pipeline output."""
    from app.data_channel.steward.service import canonical_json_hash

    return canonical_json_hash({
        "definition": definition or {},
        "source_dataset_id": source_dataset_id,
        "route": route,
        "spec": spec or {},
    })


def current_execution_hash(pipeline: Pipeline) -> str:
    return pipeline_execution_hash(
        definition=pipeline.definition,
        source_dataset_id=pipeline.source_dataset_id,
        route=pipeline.route,
        spec=pipeline.spec,
    )


def invalidate_publish_attestation(pipeline: Pipeline) -> None:
    if not is_n8n_pipeline(pipeline):
        pipeline.validation_attestation = None


def require_publish_attestation(
    pipeline: Pipeline,
    db: Session,
) -> None:
    """Verify that the latest full validation still matches staged output."""
    from app.data_channel.steward.service import canonical_json_hash
    from app.services.storage_service import get_storage_service

    attestation = pipeline.validation_attestation or {}
    required = {
        "column_definitions_hash",
        "execution_hash",
        "dry_run_id",
        "output_checksum",
    }
    if any(not attestation.get(field) for field in required):
        logger.warning(
            "发布凭证拒绝[缺凭证]: pipeline=%s 凭证字段不完整", pipeline.id)
        raise HTTPException(
            400,
            "发布前必须完成最近一次执行预览与字段定义的全量校验。"
            "请通过流水线编辑向导重新执行预览并校验字段定义。",
        )
    if attestation["column_definitions_hash"] != column_definitions_hash(
        pipeline.column_definitions
    ):
        logger.warning(
            "发布凭证拒绝[哈希不符]: pipeline=%s 字段定义在最近一次校验后"
            "发生变化，旧凭证已作废", pipeline.id)
        invalidate_publish_attestation(pipeline)
        db.commit()
        raise HTTPException(
            400,
            "字段定义在最近一次校验后发生变化，旧校验结果已失效。请重新校验字段定义。",
        )
    if attestation["execution_hash"] != current_execution_hash(pipeline):
        logger.warning(
            "发布凭证拒绝[哈希不符]: pipeline=%s 编排或数据源在最近一次"
            "校验后发生变化，旧凭证已作废", pipeline.id)
        invalidate_publish_attestation(pipeline)
        db.commit()
        raise HTTPException(
            400,
            "流水线编排或数据源在最近一次校验后发生变化，旧校验结果已失效。"
            "请重新执行预览并校验字段定义。",
        )

    try:
        raw = get_storage_service().get_object(
            dry_run_uri(pipeline.id, str(attestation["dry_run_id"]))
        )
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        logger.warning(
            "发布凭证拒绝[试运行过期]: pipeline=%s dry_run=%s 暂存对象"
            "不存在或读取失败", pipeline.id, attestation.get("dry_run_id"))
        invalidate_publish_attestation(pipeline)
        db.commit()
        raise HTTPException(
            400,
            "最近一次试运行结果不存在或已过期，请重新执行预览并校验字段定义。",
        )

    outputs = payload.get("outputs") or []
    payload_checksum = str(payload.get("output_checksum") or "")
    computed_checksum = canonical_json_hash(outputs)
    if (
        payload.get("pipeline_id") != pipeline.id
        or payload.get("dry_run_id") != attestation["dry_run_id"]
        or payload.get("truncated")
        or payload_checksum != attestation["output_checksum"]
        or computed_checksum != payload_checksum
    ):
        logger.warning(
            "发布凭证拒绝[输出不一致]: pipeline=%s dry_run=%s 暂存输出与"
            "凭证校验和不一致（truncated=%s）", pipeline.id,
            attestation.get("dry_run_id"), bool(payload.get("truncated")))
        invalidate_publish_attestation(pipeline)
        db.commit()
        raise HTTPException(
            400,
            "最近一次试运行校验凭证与暂存输出不一致，平台已拒绝发布。"
            "请重新执行预览并校验字段定义。",
        )


def require_production_executable(pipeline: Pipeline) -> None:
    """Allow production writes only for an immutable, enabled release."""
    if settings.environment != "production":
        return
    if (pipeline.status or "") != "published":
        raise HTTPException(409, "生产环境只允许运行已发布的流水线；草稿请使用 dry-run 预览。")
    if not bool(pipeline.enabled):
        raise HTTPException(409, "流水线当前未启用，生产运行已拒绝。")


def validate_pipeline_definition(
    pipeline_id: str,
    db: Session,
) -> ValidateResult:
    """Validate a pipeline's publish prerequisites for its engine."""
    pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not pipeline:
        raise HTTPException(404, "Pipeline not found")

    errors = []
    warnings = []
    definition = pipeline.definition

    # n8n validate is also a publish prerequisite, so reject workflows that
    # cannot be dispatched through the platform before remote activation.
    if is_n8n_pipeline(pipeline):
        from app.data_channel.steward.service import (
            StewardError,
            record_for_pipeline,
            validate_managed_workflow_contract,
        )

        record = record_for_pipeline(db, pipeline)
        if record is None:
            errors.append({
                "node_id": "",
                "severity": "error",
                "message": "缺少数据管家治理记录，无法运行。请删除后在数据管家重新新建该流水线。",
            })
        else:
            try:
                validate_managed_workflow_contract(record.workflow_snapshot)
            except StewardError as exc:
                errors.append({
                    "node_id": "",
                    "severity": "error",
                    "message": str(exc),
                })
        return ValidateResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    # Python 脚本流水线：只要求已保存脚本，无额外结构校验。
    if is_python_pipeline(pipeline):
        script = ((definition or {}).get("python") or {}).get("script") or ""
        if not script.strip():
            errors.append({
                "node_id": "",
                "severity": "error",
                "message": (
                    "Python 脚本流水线尚未保存脚本，"
                    "请到脚本编辑页编写并保存脚本。"
                ),
            })
        return ValidateResult(
            valid=len(errors) == 0,
            errors=errors,
            warnings=warnings,
        )

    # 系统自定义（canvas）与 route A/B/C 流水线已下线：存量行由迁移 0061
    # 统一归档，运行时按未知引擎失败；校验/发布入口在此明确拒绝。
    errors.append({
        "node_id": "",
        "severity": "error",
        "message": (
            "系统自定义（canvas）与 route A/B/C 流水线已下线，"
            "无法校验或发布；请改用 n8n 流水线或 Python 脚本。"
        ),
    })
    return ValidateResult(
        valid=False,
        errors=errors,
        warnings=warnings,
    )


def validate_column_definitions(
    pipeline_id: str,
    body: ValidateDefinitionsBody,
    dry_run_id: str,
    db: Session,
) -> ValidateDefinitionsResult:
    """Validate the output contract against the full staged dry-run result."""
    from app.data_channel.datasets.lake_gate import (
        FIELD_KEY_RE,
        _cell_type_ok,
        normalize_definitions,
        split_pk,
        validate_contract_structure,
    )
    from app.data_channel.datasets.models import Dataset

    pipeline = db.query(Pipeline).filter(Pipeline.id == pipeline_id).first()
    if not pipeline:
        raise HTTPException(404, "Pipeline not found")

    try:
        from app.services.storage_service import get_storage_service

        raw = get_storage_service().get_object(
            dry_run_uri(pipeline_id, dry_run_id)
        )
        payload = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise HTTPException(400, "非法的 dry_run_id")
    except Exception:
        raise HTTPException(404, "试运行结果不存在或已过期，请回到「执行预览」重新执行流水线")
    if payload.get("pipeline_id") != pipeline_id:
        raise HTTPException(400, "试运行结果与流水线不匹配")
    stored_dry_run_id = payload.get("dry_run_id")
    if (
        stored_dry_run_id not in (None, dry_run_id)
        or (
            is_n8n_pipeline(pipeline)
            and stored_dry_run_id != dry_run_id
        )
    ):
        raise HTTPException(
            400,
            "试运行结果 id 与暂存内容不匹配，平台已拒绝使用该结果",
        )

    outputs = payload.get("outputs") or []
    from app.data_channel.steward import service as steward_service

    output_checksum = str(payload.get("output_checksum") or "")
    computed_output_checksum = steward_service.canonical_json_hash(outputs)
    checksum_invalid = (
        bool(output_checksum)
        and output_checksum != computed_output_checksum
    )
    checksum_required_but_missing = (
        is_n8n_pipeline(pipeline)
        and not output_checksum
    )
    if checksum_invalid or checksum_required_but_missing:
        if is_n8n_pipeline(pipeline):
            record = steward_service.record_for_pipeline(db, pipeline)
            if record is not None:
                steward_service.invalidate_validation_attestation(record)
                db.commit()
        raise HTTPException(
            400,
            "试运行输出校验和缺失或不匹配，暂存内容不能作为发布依据。请重新执行预览。",
        )
    if len(outputs) > 1:
        raise HTTPException(
            400,
            "该流水线单次执行产出多个数据集，流水线级字段契约暂不适用"
            "（主键请在任务/资产湖粒度管理）。",
        )
    rows: list[dict] = [
        row
        for row in (
            (outputs[0].get("rows") or [])
            if outputs
            else []
        )
        if isinstance(row, dict)
    ]

    actual_columns: set[str] = set()
    for row in rows:
        actual_columns.update(str(key) for key in row.keys())

    errors: list[dict] = []
    definitions = normalize_definitions(body.column_definitions)
    if is_n8n_pipeline(pipeline) and not definitions:
        errors.append({
            "field_key": "",
            "severity": "error",
            "message": "n8n 流水线发布前必须定义至少一个输出字段，并完成主键/类型校验",
        })
    if payload.get("truncated"):
        errors.append({
            "field_key": "",
            "severity": "error",
            "message": (
                f"试运行输出超过暂存上限，本次仅有 {len(rows):,} 行，"
                "无法完成全量发布校验"
            ),
        })

    for message in validate_contract_structure(body.column_definitions):
        errors.append({
            "field_key": "",
            "severity": "error",
            "message": message,
        })

    seen_field_keys: set[str] = set()
    for definition in definitions:
        field_key = definition["field_key"]
        if not FIELD_KEY_RE.match(field_key):
            errors.append({
                "field_key": field_key,
                "severity": "error",
                "message": (
                    f"字段标识「{field_key}」不合法：须以字母或下划线开头，"
                    "仅含字母/数字/下划线（入湖列名约束）"
                ),
            })
        if field_key in seen_field_keys:
            errors.append({
                "field_key": field_key,
                "severity": "error",
                "message": f"字段标识「{field_key}」重复：多个列映射到了同一个入湖列名",
            })
        seen_field_keys.add(field_key)
        if definition["source_key"] not in actual_columns:
            errors.append({
                "field_key": field_key,
                "severity": "error",
                "message": f"原始列「{definition['source_key']}」在流水线输出中不存在",
            })

    for definition in definitions:
        expected = definition["field_type"]
        source_key = definition["source_key"]
        if source_key not in actual_columns:
            continue
        invalid = [
            index + 1
            for index, row in enumerate(rows)
            if not _cell_type_ok(row.get(source_key), expected)
        ]
        if invalid:
            errors.append({
                "field_key": definition["field_key"],
                "severity": "error",
                "message": (
                    f"字段类型声明为「{expected}」，但第 {invalid[:5]} 行"
                    "无法按该类型解析"
                ),
            })

    primary_keys = [
        definition
        for definition in definitions
        if (
            definition["is_primary_key"]
            and definition["source_key"] in actual_columns
        )
    ]
    if primary_keys and rows:
        primary_key_display = "、".join(
            definition["field_key"] for definition in primary_keys
        )
        seen_primary_keys: dict[tuple, int] = {}
        for index, row in enumerate(rows):
            values: list[str] = []
            empty_column = None
            for definition in primary_keys:
                value = row.get(definition["source_key"])
                if value is None or str(value).strip() == "":
                    empty_column = definition["field_key"]
                    break
                values.append(str(value).strip())
            if empty_column is not None:
                errors.append({
                    "field_key": empty_column,
                    "severity": "error",
                    "message": (
                        f"主键列「{empty_column}」第 {index + 1} 行为空："
                        "主键值必须全量非空"
                    ),
                })
                break
            key = tuple(values)
            if key in seen_primary_keys:
                errors.append({
                    "field_key": primary_key_display,
                    "severity": "error",
                    "message": (
                        f"主键组合「{primary_key_display}」第 "
                        f"{seen_primary_keys[key] + 1} 行与第 {index + 1} 行重复"
                        f"（全量校验，共 {len(rows)} 行）"
                    ),
                })
                break
            seen_primary_keys[key] = index

    for definition in definitions:
        source_key = definition["source_key"]
        if (
            definition["nullable"]
            or definition["is_primary_key"]
            or source_key not in actual_columns
        ):
            continue
        null_count = sum(
            1
            for row in rows
            if (
                row.get(source_key) is None
                or str(row.get(source_key)).strip() == ""
            )
        )
        if null_count > 0:
            errors.append({
                "field_key": definition["field_key"],
                "severity": "error",
                "message": (
                    f"列「{definition['field_key']}」不允许为空，"
                    f"但全量数据中存在 {null_count} 个空值"
                ),
            })

    primary_key = ",".join(
        definition["field_key"]
        for definition in definitions
        if definition["is_primary_key"]
    )
    if primary_key:
        for curated_id in pipeline.target_curated_ids or []:
            dataset = db.query(Dataset).filter(
                Dataset.id == curated_id
            ).first()
            declared = (
                (dataset.schema_json or {}).get("primary_key") or ""
                if dataset
                else ""
            )
            if (
                declared
                and split_pk(declared) != split_pk(primary_key)
            ):
                errors.append({
                    "field_key": primary_key,
                    "severity": "warning",
                    "message": (
                        f"主键与资产湖数据集「{dataset.name}」已固化的主键"
                        f"（{declared}）不一致：下次「全量覆盖」运行将重写湖中"
                        "声明并重建实例身份；增量/合并（append/upsert）入库"
                        "在对齐前会硬失败"
                    ),
                })

    record = None
    live_evidence = None
    if is_n8n_pipeline(pipeline):
        record = steward_service.record_for_pipeline(db, pipeline)
        if record is None:
            errors.append({
                "field_key": "",
                "severity": "error",
                "message": "平台内部治理记录不完整，无法安全发布",
            })
        else:
            engine_meta = payload.get("engine_meta") or {}
            dry_run_evidence = engine_meta.get("workflow_evidence") or {}
            try:
                client = steward_service.get_n8n_client(db)
                live_workflow = client.get_workflow(
                    record.n8n_workflow_id
                )
                if (
                    not dry_run_evidence
                    and engine_meta.get("workflow_snapshot_hash")
                ):
                    from app.settings.workflows.n8n_client import N8nClient

                    dry_run_evidence = {
                        "revision": N8nClient.workflow_revision(live_workflow),
                        "snapshot_hash": engine_meta[
                            "workflow_snapshot_hash"
                        ],
                    }
                live_evidence = (
                    steward_service.require_workflow_validation_evidence(
                        dry_run_evidence,
                        live_workflow,
                        context="字段定义校验时",
                    )
                )
            except steward_service.StewardError as exc:
                errors.append({
                    "field_key": "",
                    "severity": "error",
                    "message": str(exc),
                })
            except Exception as exc:  # noqa: BLE001
                errors.append({
                    "field_key": "",
                    "severity": "error",
                    "message": (
                        "无法读取当前 n8n workflow，平台内部一致性检查失败："
                        f"{exc}"
                    ),
                })

    has_blocking = any(
        error["severity"] == "error"
        for error in errors
    )
    if record is not None:
        state = dict(record.last_test_result or {})
        if has_blocking or live_evidence is None:
            state.pop("validation_attestation", None)
        else:
            state["validation_attestation"] = {
                "version": 1,
                "column_definitions_hash": column_definitions_hash(
                    body.column_definitions
                ),
                "workflow_revision": live_evidence["revision"],
                "workflow_snapshot_hash": live_evidence["snapshot_hash"],
                "dry_run_id": dry_run_id,
                "output_checksum": output_checksum,
                "dry_run_created_at": payload.get("created_at"),
                "validated_at": datetime.now(timezone.utc).isoformat(),
            }
        record.last_test_result = state
        db.commit()
    elif not is_n8n_pipeline(pipeline):
        if has_blocking:
            invalidate_publish_attestation(pipeline)
        else:
            pipeline.validation_attestation = {
                "version": 1,
                "engine": (pipeline.definition or {}).get("engine"),
                "column_definitions_hash": column_definitions_hash(
                    body.column_definitions
                ),
                "execution_hash": current_execution_hash(pipeline),
                "dry_run_id": dry_run_id,
                "output_checksum": (
                    output_checksum
                    or computed_output_checksum
                ),
                "dry_run_created_at": payload.get("created_at"),
                "validated_at": datetime.now(timezone.utc).isoformat(),
            }
        db.commit()
    return ValidateDefinitionsResult(
        valid=not has_blocking,
        errors=errors,
    )


def publish_pipeline_release(
    pipeline_id: str,
    body: PublishBody | None,
    db: Session,
    current_user: Any,
    *,
    validate_pipeline_fn: Callable[[str, Session], ValidateResult],
) -> dict:
    """Publish a pipeline while preserving remote-side compensation."""
    from app.data_channel.datasets.lake_gate import (
        contract_pk,
        split_pk,
        validate_contract_structure,
    )
    from app.data_channel.datasets.models import Dataset

    pipeline = db.query(Pipeline).filter(
        Pipeline.id == pipeline_id
    ).first()
    if not pipeline:
        raise HTTPException(404, "Pipeline not found")
    if (pipeline.status or "") == "published":
        raise HTTPException(400, "流水线已是已发布状态。")

    validation = validate_pipeline_fn(pipeline_id, db)
    if not validation.valid:
        raise HTTPException(
            400,
            f"Pipeline 校验失败，无法发布: {validation.errors}",
        )

    structure_errors = validate_contract_structure(
        pipeline.column_definitions
    )
    if structure_errors:
        raise HTTPException(
            400,
            f"字段契约结构非法：{'；'.join(structure_errors)}。"
            "请回到「设置主键组」修正。",
        )

    if not is_n8n_pipeline(pipeline):
        require_publish_attestation(pipeline, db)

    desired_enabled = bool(body and body.enable)
    n8n_activation = None
    if is_n8n_pipeline(pipeline):
        from app.data_channel.steward import service as steward_service

        record = steward_service.record_for_pipeline(db, pipeline)
        if record is None:
            raise HTTPException(
                400,
                "缺少数据管家治理记录，无法发布。"
                "请删除后在数据管家重新新建该流水线。",
            )
        attestation = steward_service.validation_attestation(record)
        required_attestation_fields = {
            "column_definitions_hash",
            "workflow_revision",
            "workflow_snapshot_hash",
            "dry_run_id",
            "output_checksum",
        }
        if not attestation or any(
            not attestation.get(field)
            for field in required_attestation_fields
        ):
            evidence_error = str(
                (record.last_test_result or {}).get(
                    "publish_evidence_error"
                )
                or ""
            ).strip()
            evidence_detail = (
                f"内部一致性检查详情：{evidence_error.rstrip('。.!！')}"
                if evidence_error
                else "内部一致性检查尚未完成"
            )
            raise HTTPException(
                400,
                "平台尚未完成最近一次执行预览与字段定义的一致性确认，"
                f"暂时无法安全发布。{evidence_detail}。",
            )
        if attestation[
            "column_definitions_hash"
        ] != column_definitions_hash(pipeline.column_definitions):
            steward_service.invalidate_validation_attestation(record)
            db.commit()
            raise HTTPException(
                400,
                "字段定义在最近一次校验后发生变化，平台已自动使旧校验结果失效。"
                "请重新校验字段定义。",
            )
        try:
            client = steward_service.get_n8n_client(db)
            revision = steward_service.activate_for_publish(
                db,
                record,
                client,
                keep_active=desired_enabled,
                validation_attestation=attestation,
            )
        except steward_service.ValidationAttestationError as exc:
            steward_service.invalidate_validation_attestation(record)
            db.commit()
            raise HTTPException(400, str(exc))
        except steward_service.StewardError as exc:
            raise HTTPException(400, str(exc))
        n8n_activation = (record, client)

    try:
        if n8n_activation is not None:
            record, _client = n8n_activation
            steward_service.ensure_shadow_pipeline(
                db,
                record,
                published_revision=revision,
            )
            release = (pipeline.definition or {}).get("n8n") or {}
            contract = release.get("managed_contract") or {}
            frozen_revision = release.get("revision") or {}
            if (
                not contract.get("output_node_name")
                or not contract.get("webhook_path")
            ):
                raise HTTPException(
                    500,
                    "发布事务未形成完整 n8n 输入/输出契约，发布已中止。",
                )
            if not all(
                frozen_revision.get(field)
                for field in ("versionId", "updatedAt")
            ):
                raise HTTPException(
                    500,
                    "平台未能确认发布版本，发布已安全中止。",
                )

        warnings: list[str] = []
        primary_key = contract_pk(pipeline.column_definitions)
        if primary_key:
            for curated_id in pipeline.target_curated_ids or []:
                dataset = db.query(Dataset).filter(
                    Dataset.id == curated_id
                ).first()
                declared = (
                    (dataset.schema_json or {}).get("primary_key") or ""
                    if dataset
                    else ""
                )
                if (
                    declared
                    and split_pk(declared) != split_pk(primary_key)
                ):
                    warnings.append(
                        f"契约主键（{primary_key}）与资产湖数据集"
                        f"「{dataset.name}」已固化的主键（{declared}）不一致："
                        "下次「全量覆盖」运行将重写湖中声明并重建实例身份；"
                        "增量/合并入库在对齐前会失败。"
                    )

        has_prior_published = db.query(PipelineVersion).filter(
            PipelineVersion.pipeline_id == pipeline_id,
            PipelineVersion.status == "published",
        ).count() > 0
        pipeline.status = "published"
        pipeline.version = (
            (pipeline.version or 1)
            + (1 if has_prior_published else 0)
        )
        pipeline.enabled = desired_enabled
        pipeline.updated_at = datetime.now(timezone.utc)
        db.add(PipelineVersion(
            pipeline_id=pipeline_id,
            version=pipeline.version,
            definition=pipeline.definition,
            column_definitions=pipeline.column_definitions,
            status="published",
            created_by=current_user.id,
        ))
        db.commit()
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        if n8n_activation is not None:
            record, client = n8n_activation
            try:
                steward_service.compensate_failed_publish(record, client)
            except steward_service.StewardError as compensation_error:
                raise HTTPException(
                    500,
                    str(compensation_error),
                ) from exc
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            500,
            "平台发布事务失败，n8n 已恢复为发布前的停用状态："
            f"{exc}",
        ) from exc

    return {
        "id": pipeline.id,
        "status": pipeline.status,
        "version": pipeline.version,
        "enabled": (
            True
            if pipeline.enabled is None
            else bool(pipeline.enabled)
        ),
        "warnings": warnings or None,
    }
