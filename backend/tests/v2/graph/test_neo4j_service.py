"""Neo4jService 단위 테스트 — 실제 Neo4j 없이 mock 사용"""
import pytest
from unittest.mock import MagicMock, patch


def make_mock_driver():
    driver = MagicMock()
    driver.verify_connectivity.return_value = None
    session = MagicMock()
    driver.session.return_value.__enter__ = MagicMock(return_value=session)
    driver.session.return_value.__exit__ = MagicMock(return_value=False)
    return driver, session


def test_neo4j_service_available_on_connect():
    """Neo4j 연결 성공 시 available=True"""
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_driver, _ = make_mock_driver()
        mock_gdb.driver.return_value = mock_driver

        from app.services.v2.graph.neo4j_service import Neo4jService
        svc = Neo4jService(uri="bolt://localhost:7687", user="neo4j", password="test")
        assert svc.available is True


def test_neo4j_service_unavailable_on_error():
    """Neo4j 연결 실패 시 available=False"""
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_gdb.driver.side_effect = Exception("Connection refused")

        from app.services.v2.graph.neo4j_service import Neo4jService
        svc = Neo4jService(uri="bolt://bad:9999", user="x", password="x")
        assert svc.available is False


def test_upsert_entity_unavailable_returns_none():
    """Neo4j 미연결 시 upsert_entity는 None 반환"""
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_gdb.driver.side_effect = Exception("offline")

        from app.services.v2.graph.neo4j_service import Neo4jService
        svc = Neo4jService(uri="bolt://x", user="x", password="x")
        result = svc.upsert_entity("Entity", {"id": "e1"})
        assert result is None


def test_get_graph_data_unavailable_returns_empty():
    """Neo4j 미연결 시 빈 그래프 반환"""
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_gdb.driver.side_effect = Exception("offline")

        from app.services.v2.graph.neo4j_service import Neo4jService
        svc = Neo4jService(uri="bolt://x", user="x", password="x")
        result = svc.get_graph_data("ontology-1")
        assert result == {"nodes": [], "edges": []}


def test_run_cypher_unavailable_returns_empty():
    """Neo4j 미연결 시 빈 리스트 반환"""
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_gdb.driver.side_effect = Exception("offline")

        from app.services.v2.graph.neo4j_service import Neo4jService
        svc = Neo4jService(uri="bolt://x", user="x", password="x")
        result = svc.run_cypher("MATCH (n) RETURN n")
        assert result == []


def test_cypher_builder_label_validation():
    """유효하지 않은 레이블은 ValueError"""
    from app.services.v2.graph.cypher_builder import validate_label
    assert validate_label("Entity") == "Entity"
    assert validate_label("Supply_Chain") == "Supply_Chain"
    with pytest.raises(ValueError):
        validate_label("Bad Label")
    with pytest.raises(ValueError):
        validate_label("1BadLabel")
    with pytest.raises(ValueError):
        validate_label("'; DROP DATABASE")


def test_cypher_builder_build_match():
    from app.services.v2.graph.cypher_builder import build_match_by_id
    query, params = build_match_by_id("Entity", "e1")
    assert "MATCH" in query
    assert params["id"] == "e1"


def test_batch_upsert_unavailable_returns_zero():
    """Neo4j 미연결 시 batch_upsert_entities는 0 반환"""
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_gdb.driver.side_effect = Exception("offline")

        from app.services.v2.graph.neo4j_service import Neo4jService
        svc = Neo4jService(uri="bolt://x", user="x", password="x")
        count = svc.batch_upsert_entities("Entity", [{"id": "e1"}])
        assert count == 0


@pytest.mark.parametrize(
    ("replace_properties", "expected_assignment", "unexpected_assignment"),
    [
        (False, "SET n += e.props", "SET n = e.props"),
        (True, "SET n = e.props", "SET n += e.props"),
    ],
)
def test_batch_upsert_property_assignment_is_explicit(
    replace_properties,
    expected_assignment,
    unexpected_assignment,
):
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_driver, session = make_mock_driver()
        session.run.return_value.single.return_value = {"written": 1}
        mock_gdb.driver.return_value = mock_driver

        from app.services.v2.graph.neo4j_service import Neo4jService

        svc = Neo4jService(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="test",
        )
        assert svc.batch_upsert_entities(
            "Entity",
            [{
                "id": "e1",
                "name": "replacement",
                "updated_at": "2026-07-01T02:03:04Z",
            }],
            replace_properties=replace_properties,
        ) == 1

    query = " ".join(session.run.call_args.args[0].split())
    assert (
        f"{expected_assignment} SET n.updated_at = "
        "coalesce(e.props.updated_at, datetime())"
    ) in query
    assert unexpected_assignment not in query
    assert session.run.call_args.kwargs["batch"] == [{
        "key": "e1",
        "props": {
            "id": "e1",
            "name": "replacement",
            "updated_at": "2026-07-01T02:03:04Z",
        },
    }]


def test_single_entity_upsert_preserves_authoritative_updated_at():
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_driver, session = make_mock_driver()
        session.run.return_value.single.return_value = {"eid": "node-1"}
        mock_gdb.driver.return_value = mock_driver

        from app.services.v2.graph.neo4j_service import Neo4jService

        svc = Neo4jService(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="test",
        )
        assert svc.upsert_entity("Entity", {
            "id": "e1",
            "updated_at": "2026-07-01T02:03:04Z",
        }) == "node-1"

    query = " ".join(session.run.call_args.args[0].split())
    assert (
        "n.updated_at = coalesce($props.updated_at, datetime())" in query
    )
    assert "n.updated_at = datetime()" not in query
    assert session.run.call_args.kwargs["props"]["updated_at"] == (
        "2026-07-01T02:03:04Z"
    )


def test_relation_upsert_preserves_authoritative_updated_at():
    with patch("app.services.v2.graph.neo4j_service.GraphDatabase") as mock_gdb:
        mock_driver, session = make_mock_driver()
        session.run.return_value.single.return_value = {"r": object()}
        mock_gdb.driver.return_value = mock_driver

        from app.services.v2.graph.neo4j_service import Neo4jService

        svc = Neo4jService(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="test",
        )
        assert svc.upsert_relation(
            "Entity",
            "e1",
            "Entity",
            "e2",
            "OWNS",
            {"id": "r1", "updated_at": "2026-07-02T02:03:04Z"},
        ) is True

    query = " ".join(session.run.call_args.args[0].split())
    assert (
        "r.updated_at = coalesce($props.updated_at, datetime())" in query
    )
    assert "r.updated_at = datetime()" not in query
    assert session.run.call_args.kwargs["props"]["updated_at"] == (
        "2026-07-02T02:03:04Z"
    )


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("upsert_entity", ("Entity`) DETACH DELETE n //", {"id": "e1"})),
        (
            "batch_upsert_entities",
            ("Entity", [{"id": "e1"}], "id}) DETACH DELETE n //"),
        ),
        (
            "upsert_relation",
            ("Entity", "e1", "Entity", "e2", "REL`) DELETE r //"),
        ),
    ],
)
def test_write_identifiers_reject_cypher_injection(method, args):
    from app.services.v2.graph.neo4j_service import Neo4jService

    svc = object.__new__(Neo4jService)
    svc._available = True
    svc._driver = MagicMock()

    with pytest.raises(ValueError, match="Invalid Neo4j label"):
        getattr(svc, method)(*args)
    svc._driver.session.assert_not_called()
