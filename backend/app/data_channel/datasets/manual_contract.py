"""Manual-dataset schemas and validation rules.

These contracts are shared by the HTTP adapters, anonymous sharing workflow,
and asynchronous import worker.  Keeping them outside a router prevents
background/domain code from depending on the transport layer.
"""
from __future__ import annotations

import re

from fastapi import HTTPException
from pydantic import BaseModel


MANUAL_FIELD_CONTRACT_VERSION = 2
MANUAL_FIELD_KEY_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class DatasetResponse(BaseModel):
    id: str
    name: str
    kind: str

    class Config:
        from_attributes = True


class TableColumnDef(BaseModel):
    name: str
    source_key: str = ""
    display_name: str = ""
    type: str = "string"  # CONTRACT_FIELD_TYPES 平台词表；非法值显式拒绝
    nullable: bool = True


class CreateTableRequest(BaseModel):
    name: str
    columns: list[TableColumnDef]
    primary_key: str = ""  # 逗号分隔支持复合主键，可为空（之后仍可声明契约）


class RowEditOp(BaseModel):
    key: dict | None = None     # 主键列→值（update/delete 用加载时的原值定位）
    values: dict | None = None  # update/insert 的列值


class RowEditsRequest(BaseModel):
    base_version_no: int  # 乐观并发：客户端所见的最新版本号
    updates: list[RowEditOp] = []
    inserts: list[RowEditOp] = []
    deletes: list[RowEditOp] = []


class ContractRequest(BaseModel):
    primary_key: str


def build_manual_schema(
    body: CreateTableRequest,
    *,
    origin: str,
) -> tuple[str, dict]:
    """Validate manual-table fields and build the canonical ``schema_json``."""
    from app.data_channel.datasets.lake_gate import (
        CONTRACT_FIELD_TYPES,
        normalize_field_type,
        split_pk,
    )

    name = body.name.strip()
    if not name:
        raise HTTPException(400, "表格名称不能为空")
    if len(name) > 200:
        raise HTTPException(400, "表格名称过长（最多 200 字）")

    explicit_source_keys = [
        definition.source_key.strip()
        for definition in body.columns
        if definition.name.strip()
    ]
    has_explicit_source = any(explicit_source_keys)
    if has_explicit_source and not all(explicit_source_keys):
        raise HTTPException(400, "字段契约中的原始表头必须为每一列完整提供")

    # Every configured file upload uses the stable-key contract. ``source_key``
    # is optional only for legacy English-header clients, where field key and
    # source header are already identical. Empty-table API clients opt in by
    # sending source_key; existing Unicode-key tables remain readable unchanged.
    use_stable_field_contract = origin == "upload" or has_explicit_source

    raw_columns: list[tuple[TableColumnDef, str, str]] = []
    seen: set[str] = set()
    seen_sources: set[str] = set()
    for definition in body.columns:
        column = definition.name.strip()
        if not column:
            continue
        if (
            use_stable_field_contract
            and not MANUAL_FIELD_KEY_RE.fullmatch(column)
        ):
            raise HTTPException(
                400,
                f"字段标识「{column}」不合法：必须以小写字母开头，"
                "且只能包含小写字母、数字和下划线",
            )
        if column in seen:
            raise HTTPException(400, f"字段标识「{column}」重复，请修改后重试")
        source_key = definition.source_key.strip() or column
        if origin != "upload" and source_key != column:
            raise HTTPException(400, "空表字段的原始表头必须与字段标识一致")
        if source_key in seen_sources:
            raise HTTPException(
                400,
                f"原始表头「{source_key}」重复，请修改源文件后重试",
            )
        requested_type = str(definition.type or "").strip().lower()
        if requested_type not in CONTRACT_FIELD_TYPES:
            raise HTTPException(
                400,
                f"列「{column}」的数据类型「{definition.type}」不受支持；"
                f"可选类型：{', '.join(CONTRACT_FIELD_TYPES)}",
            )
        seen.add(column)
        seen_sources.add(source_key)
        raw_columns.append((definition, column, source_key))
    if not raw_columns:
        raise HTTPException(400, "至少需要定义一列（空白列名会被忽略）")

    pk_columns = split_pk(body.primary_key)
    unknown_pk = [column for column in pk_columns if column not in seen]
    if unknown_pk:
        raise HTTPException(400, f"主键列 {unknown_pk} 不在列定义中")

    columns = []
    for definition, column, source_key in raw_columns:
        item = {
            "name": column,
            "display_name": (
                definition.display_name.strip()
                or (
                    source_key
                    if use_stable_field_contract and origin == "upload"
                    else column
                )
            ),
            "type": normalize_field_type(definition.type),
            "nullable": False if column in pk_columns else bool(definition.nullable),
        }
        if use_stable_field_contract:
            item["source_key"] = source_key
        columns.append(item)
    definitions = [
        {
            "source_key": column["source_key"],
            "field_key": column["name"],
            "field_name": column["display_name"],
            "field_type": column["type"],
            "is_primary_key": column["name"] in pk_columns,
            "nullable": column["nullable"],
        }
        for column in columns
    ] if use_stable_field_contract else []
    schema: dict = {
        "columns": [column["name"] for column in columns],
        "columns_typed": columns,
        "field_names": {
            column["name"]: column["display_name"]
            for column in columns
        },
        "types_source": "declared",
        "origin": origin,
    }
    if use_stable_field_contract:
        schema["contract_definitions"] = definitions
        schema["manual_field_contract_version"] = MANUAL_FIELD_CONTRACT_VERSION
    if pk_columns:
        schema["primary_key"] = ",".join(pk_columns)
        schema["pk_source"] = "manual"
    return name, schema


def serialize_manual_contract_rows(
    rows: list[dict],
    columns: list[str],
) -> bytes:
    """Store normalized manual rows with authoritative field-key headers."""
    if rows:
        import json

        def json_default(value) -> str:
            isoformat = getattr(value, "isoformat", None)
            return isoformat() if callable(isoformat) else str(value)

        return json.dumps(
            rows,
            ensure_ascii=False,
            separators=(",", ":"),
            default=json_default,
        ).encode("utf-8")

    import csv
    import io

    output = io.StringIO()
    csv.writer(output).writerow(columns)
    return output.getvalue().encode("utf-8")


def normalize_manual_contract_upload(
    rows: list[dict],
    physical_columns: list[str],
    schema: dict,
    *,
    dataset_name: str,
    scope: str,
    allow_field_key_headers: bool,
) -> tuple[list[dict], bytes]:
    """Validate one manual-file contract and normalize it for the data lake."""
    from app.data_channel.datasets.lake_gate import (
        LakeGateError,
        apply_column_contract,
        normalize_definitions,
    )

    definitions = normalize_definitions(schema.get("contract_definitions"))
    source_columns = [item["source_key"] for item in definitions]
    field_columns = [item["field_key"] for item in definitions]
    if not definitions or not field_columns:
        raise HTTPException(400, "人工数据集字段契约缺失，无法安全导入文件")

    active_definitions = definitions
    if physical_columns == source_columns:
        pass
    elif allow_field_key_headers and physical_columns == field_columns:
        active_definitions = [
            {**item, "source_key": item["field_key"]}
            for item in definitions
        ]
    else:
        accepted = f"原始表头 {source_columns}"
        if allow_field_key_headers and field_columns != source_columns:
            accepted += f" 或字段标识 {field_columns}"
        raise HTTPException(
            400,
            f"{scope}列结构与字段契约不一致（文件：{physical_columns}；"
            f"允许：{accepted}）。不允许混用、新增、缺失或调整字段顺序",
        )

    try:
        normalized_rows, _warnings = apply_column_contract(
            rows,
            active_definitions,
            dataset_name=dataset_name,
        )
    except LakeGateError as exc:
        raise HTTPException(400, str(exc)) from exc
    validate_manual_rows(
        normalized_rows,
        schema,
        dataset_name=dataset_name,
        scope=scope,
    )
    return (
        normalized_rows,
        serialize_manual_contract_rows(normalized_rows, field_columns),
    )


def validate_manual_rows(
    rows: list[dict],
    schema: dict,
    *,
    dataset_name: str,
    scope: str,
) -> None:
    """Validate primary-key, required-field, and declared-type contracts."""
    from app.data_channel.datasets.lake_gate import (
        LakeGateError,
        split_pk,
        validate_declared_types,
        validate_pk,
    )

    try:
        primary_key = split_pk(schema.get("primary_key"))
        if primary_key and rows:
            validate_pk(
                rows,
                primary_key,
                dataset_name=dataset_name,
                scope=scope,
            )
        required = {
            str(column.get("name"))
            for column in (schema.get("columns_typed") or [])
            if (
                isinstance(column, dict)
                and column.get("name")
                and column.get("nullable") is False
            )
        }
        for row_index, row in enumerate(rows):
            for column in sorted(required):
                value = row.get(column)
                if value is None or (
                    isinstance(value, str)
                    and not value.strip()
                ):
                    raise LakeGateError(
                        f"数据集「{dataset_name}」{scope}第 {row_index + 1} "
                        f"行的非空列「{column}」不能为空",
                    )
        validate_declared_types(
            rows,
            schema.get("columns_typed"),
            dataset_name=dataset_name,
        )
    except LakeGateError as exc:
        raise HTTPException(400, str(exc))


def require_manual_dataset(dataset, action: str) -> None:
    """Reject maintenance through the wrong dataset lifecycle."""
    if dataset.kind == "curated":
        raise HTTPException(
            400,
            f"成品数据集不支持{action}：主键契约由入湖闸门维护，"
            "行级修正请走审核编辑",
        )
    if dataset.name.startswith("SYNC::") or dataset.source_connection_id:
        raise HTTPException(
            400,
            f"该数据集由同步任务维护，不支持{action}——"
            "人工改动会在下次同步时被覆盖",
        )

