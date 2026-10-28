"""Additive query contracts for source-side preaggregation."""
import pytest
from pydantic import ValidationError

from app.ontologies.agent_query.schemas import QueryRequest, QuerySource


def _leaf(alias="a"):
    return {"alias": alias, "ontologyId": "ontology", "releaseId": "release",
            "objectTypeId": "customer"}


def _query(source):
    return {"sources": [source],
            "select": [{"source": source["alias"], "property": "id", "as": "id"}]}


def test_object_source_still_requires_complete_published_identity():
    for missing in ("ontologyId", "releaseId", "objectTypeId"):
        source = _leaf()
        del source[missing]
        with pytest.raises(ValidationError):
            QuerySource.model_validate(source)


def test_derived_source_is_exclusive_with_object_source():
    source = {**_leaf(), "subquery": _query(_leaf())}
    with pytest.raises(ValidationError):
        QuerySource.model_validate(source)


def test_two_levels_of_source_side_preaggregation_are_supported():
    request = _query({"alias": "customer_totals", "subquery": _query({
        "alias": "per_order", "subquery": _query(_leaf())})})
    parsed = QueryRequest.model_validate(request)
    assert parsed.sources[0].subquery.sources[0].subquery.sources[0].ontology_id == "ontology"


def test_third_derived_level_is_rejected_before_source_resolution():
    request = _query(_leaf())
    for index in range(3):
        request = _query({"alias": f"derived{index}", "subquery": request})
    with pytest.raises(ValidationError, match="depth"):
        QueryRequest.model_validate(request)


def test_total_leaf_budget_is_not_reset_at_subquery_boundaries():
    four_leaves = {"sources": [_leaf(f"s{i}") for i in range(4)],
                   "select": [{"source": "s0", "property": "id", "as": "id"}]}
    nested = {"sources": [{"alias": f"d{i}", "subquery": four_leaves} for i in range(4)],
              "select": [{"source": "d0", "property": "id", "as": "id"}]}
    request = {"sources": [{"alias": "left", "subquery": nested},
                           {"alias": "right", "subquery": four_leaves}],
               "select": [{"source": "left", "property": "id", "as": "id"}]}
    with pytest.raises(ValidationError, match="16 object sources"):
        QueryRequest.model_validate(request)
