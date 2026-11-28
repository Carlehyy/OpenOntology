"""Offline direct-key validation shared by remote queries and discovery."""
from __future__ import annotations

from app.ontologies.agent_query.errors import QueryError
from app.ontologies.mappings.remote_binding import (
    get_remote_binding, snapshot_link_binding_mode,
)


def published_relation_keys(snapshot, link, *, reverse: bool = False) -> tuple[str, str, str]:
    """Return the left/right property keys and cardinality for an authorized link.

    Callers pin the snapshot and enforce permissions. Physical source checks
    are performed by the existing query port at execution time.
    """
    source = get_remote_binding(snapshot, link.source_object_type_id)
    target = get_remote_binding(snapshot, link.target_object_type_id)
    if source is None or target is None:
        raise QueryError(422, "remote_relation_unsupported",
                         "混合查询的声明关系需要远程键映射；本地关系仍走原本体查询")
    mappings = [item for item in snapshot.get("linkMappings", [])
                if snapshot_link_binding_mode(item) == "remote"
                and item.get("linkTypeId") == link.id
                and item.get("srcDatasetId") == source["datasetId"]
                and item.get("tgtDatasetId") == target["datasetId"]]
    if len(mappings) != 1 or mappings[0].get("edgeDatasetId"):
        raise QueryError(422, "remote_relation_unmapped", "声明关系缺少唯一的已发布直接键映射")
    mapping = mappings[0]
    try:
        left = source["fieldMap"][mapping["srcKey"]]
        right = target["fieldMap"][mapping["tgtKey"]]
    except KeyError as exc:
        raise QueryError(422, "remote_relation_unmapped", "声明关系键未映射到发布属性") from exc
    cardinality = {"one-to-one": "1:1", "one-to-many": "1:N",
                   "many-to-one": "N:1", "many-to-many": "N:M"}.get(link.cardinality)
    if cardinality is None:
        raise QueryError(422, "invalid_parameter", "声明关系基数无效")
    if reverse:
        return right, left, {"1:N": "N:1", "N:1": "1:N"}.get(cardinality, cardinality)
    return left, right, cardinality
