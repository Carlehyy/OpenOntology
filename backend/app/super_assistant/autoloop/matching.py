"""Rule matching semantics, shared by routing and the API layer.

A leaf module on purpose: routing imports the API layer, so the API layer can
only ask whether a rule matches everything if the predicate lives below both.
``is_universal`` is derived from ``matches`` and must move with it.
"""
from __future__ import annotations

# An exact term is ignored only when it is None. An empty string is a real
# constraint which, in practice, matches no event.
EXACT_TERMS = ("sourceKind", "sourceSystem", "ingestKeyId", "channel")
# A substring term is ignored whenever it is falsy (None or an empty string).
SUBSTRING_TERMS = (("titleContains", "title"), ("bodyContains", "body"))


def _field(payload, path):
    value = payload
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def matches(rule, event, receipt):
    payload = receipt.payload if receipt else event.data
    conditions = rule.match
    actual = {"sourceKind": event.source_kind, "sourceSystem": event.source_system,
        "ingestKeyId": receipt.ingest_key_id if receipt else None,
        "channel": payload.get("channel")}
    for key, value in actual.items():
        if conditions.get(key) is not None and conditions[key] != value:
            return False
    for key, field in (("titleContains", "title"), ("bodyContains", "body")):
        if conditions.get(key) and conditions[key] not in str(payload.get(field) or ""):
            return False
    return all(_field(payload, item["field"]) == item["value"] for item in conditions.get("fieldEquals", []))


def is_universal(conditions) -> bool:
    """True exactly when ``matches`` accepts every event.

    ``conditions`` is the stored ``match`` object. Binding never narrows a match.
    """
    return (all(conditions.get(key) is None for key in EXACT_TERMS)
            and all(not conditions.get(key) for key, _ in SUBSTRING_TERMS)
            and not conditions.get("fieldEquals"))
