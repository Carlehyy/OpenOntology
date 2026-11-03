from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from nats.js.errors import NotFoundError

from scripts.maintenance import retire_palace_storage as script


def _workspace(root: Path, *, filename: str = "note.txt", content: str = "abc") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    entry = root / str(uuid.uuid4())
    (entry / ".meta").mkdir(parents=True)
    (entry / ".meta" / "manifest.json").write_text("[]", encoding="utf-8")
    (entry / filename).write_text(content, encoding="utf-8")
    return root


class FakeGraph:
    available = True

    def __init__(self, *, nodes=3, shared=1, indexes=None):
        self.nodes = nodes
        self.shared = shared
        self.indexes = list(indexes or ["palace_entity_name"])
        self.calls: list[str] = []

    def run_cypher(self, query: str, params=None):
        self.calls.append(query)
        if "SHOW INDEXES" in query:
            return [{"name": name} for name in self.indexes]
        if "MATCH (n:PalaceEntity:OntologyEntity)" in query:
            return [{"count": self.shared}]
        if "NOT n:OntologyEntity" in query and "RETURN count(n) AS count" in query:
            return [{"count": self.nodes}]
        if "DETACH DELETE" in query:
            deleted = self.nodes
            self.nodes = 0
            return [{"deleted": deleted}]
        if query.startswith("DROP INDEX"):
            name = query.split("`", 2)[1]
            self.indexes = [item for item in self.indexes if item != name]
            return []
        raise AssertionError(f"unexpected Cypher: {query}")


class FakeQueryGraph:
    available = True

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def run_cypher(self, query: str, params=None):
        self.calls.append(query)
        return self.responses.pop(0)


class FakeRedis:
    def __init__(self, keys):
        self.keys = list(keys)
        self.scan_calls: list[tuple] = []
        self.delete_calls: list[tuple] = []

    def ping(self):
        return True

    def scan(self, *, cursor, match, count):
        self.scan_calls.append((cursor, match, count))
        if int(cursor or 0) == 0:
            return 7, list(self.keys)
        return 0, []

    def delete(self, *keys):
        self.delete_calls.append(tuple(keys))
        wanted = set(keys)
        before = len(self.keys)
        self.keys = [
            item for item in self.keys
            if script._decode_key(item) not in wanted
        ]
        return before - len(self.keys)


class FakeJS:
    def __init__(self, subjects=None, consumers=None, *, missing=False):
        self.subjects = list(subjects or [
            "pipeline.task.execute",
            *script.NATS_SUBJECTS,
            "integrated.sync",
        ])
        names = list(consumers or [
            "pipeline-executor",
            *script.NATS_DURABLES,
        ])
        self.consumers = {name: SimpleNamespace(name=name) for name in names}
        self.missing = missing
        self.calls: list[tuple] = []

    async def stream_info(self, name):
        self.calls.append(("stream_info", name))
        if self.missing:
            raise NotFoundError()
        return SimpleNamespace(config=SimpleNamespace(subjects=list(self.subjects)))

    async def consumers_info(self, name):
        self.calls.append(("consumers_info", name))
        return list(self.consumers.values())

    async def delete_consumer(self, stream, consumer):
        self.calls.append(("delete_consumer", stream, consumer))
        self.consumers.pop(consumer, None)
        return True

    async def purge_stream(self, stream, subject):
        self.calls.append(("purge_stream", stream, subject))
        return True

    async def update_stream(self, config):
        self.calls.append(("update_stream", config.name if hasattr(config, "name") else None))
        self.subjects = list(config.subjects)
        return SimpleNamespace(config=config)


def test_default_root_is_fixed_child_and_rejects_shared_root(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    assert script.resolve_palace_root(uploads) == (uploads / script.PALACE_DIR_NAME).resolve()

    with pytest.raises(script.SafetyError):
        script.resolve_palace_root("/")


def test_custom_root_requires_explicit_acknowledgement(tmp_path):
    base = tmp_path.resolve()
    uploads = base / "uploads"
    custom = base / "legacy-palace"
    uploads.mkdir()
    custom.mkdir()

    with pytest.raises(script.SafetyError):
        script.resolve_palace_root(uploads, custom_root=custom)
    assert script.resolve_palace_root(
        uploads, custom_root=custom, allow_custom_root=True,
    ) == custom


def test_custom_root_rejects_shared_root_ancestor_and_unrelated_uploads_workspace(tmp_path):
    base = tmp_path.resolve()
    uploads = base / "uploads"
    uploads.mkdir()
    for candidate in (
        uploads,
        base,
        uploads / "steward-sessions",
    ):
        with pytest.raises(script.SafetyError):
            script.resolve_palace_root(
                uploads, custom_root=candidate, allow_custom_root=True,
            )


def test_custom_root_rejects_symlinked_parent(tmp_path):
    base = tmp_path.resolve()
    uploads = base / "uploads"
    uploads.mkdir()
    real_parent = base / "real-parent"
    real_parent.mkdir()
    linked_parent = base / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    custom = linked_parent / "legacy-palace"
    custom.mkdir()

    with pytest.raises(script.SafetyError):
        script.resolve_palace_root(
            uploads, custom_root=custom, allow_custom_root=True,
        )


def test_symlink_root_is_rejected(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    outside = _workspace(tmp_path / "outside")
    link = uploads / script.PALACE_DIR_NAME
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(script.SafetyError):
        script.resolve_palace_root(uploads)
    assert script.retire_uploads(uploads, execute=True)["status"] == "unsafe"
    assert outside.exists()


def test_missing_root_is_noop_and_does_not_create_it(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    result = script.retire_uploads(uploads, execute=True)
    assert result["status"] == "missing"
    assert result["deleted"] is False
    assert not (uploads / script.PALACE_DIR_NAME).exists()


def test_workspace_content_validation_rejects_unexpected_entries(tmp_path):
    root = tmp_path / "palace"
    root.mkdir()
    (root / "loose.txt").write_text("x", encoding="utf-8")
    with pytest.raises(script.SafetyError):
        script.inspect_workspace(root)

    loose_dir = tmp_path / "loose-dir"
    (loose_dir / "not-a-uuid" / ".meta").mkdir(parents=True)
    (loose_dir / "not-a-uuid" / ".meta" / "manifest.json").write_text("[]")
    with pytest.raises(script.SafetyError):
        script.inspect_workspace(loose_dir)

    missing_manifest = tmp_path / "missing-manifest"
    (missing_manifest / str(uuid.uuid4()) / ".meta").mkdir(parents=True)
    with pytest.raises(script.SafetyError):
        script.inspect_workspace(missing_manifest)


def test_workspace_symlink_inside_root_is_rejected(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    root = _workspace(uploads / script.PALACE_DIR_NAME)
    entry = next(path for path in root.iterdir() if path.is_dir())
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    (entry / "escape.txt").symlink_to(outside)

    with pytest.raises(script.SafetyError):
        script.inspect_workspace(root)
    assert script.retire_uploads(uploads, execute=True)["status"] == "unsafe"


def test_inspect_and_delete_workspace_returns_redacted_counts(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    root = _workspace(uploads / script.PALACE_DIR_NAME, content="hello")

    stats = script.inspect_workspace(root)
    assert stats.exists is True
    assert stats.files == 2
    assert stats.dirs == 2
    assert stats.bytes == len("[]") + len("hello")

    dry = script.retire_uploads(uploads)
    assert dry == {
        "status": "ok", "exists": True, "files": 2, "dirs": 2,
        "bytes": len("[]") + len("hello"), "deleted": False,
    }
    assert root.exists()

    applied = script.retire_uploads(uploads, execute=True)
    assert applied["deleted"] is True
    assert not root.exists()


def test_neo4j_only_deletes_palace_nodes_without_shared_label(tmp_path):
    graph = FakeGraph(
        nodes=5, shared=2,
        indexes=["palace_entity_name", "palace_entity_owner_id"],
    )
    dry = script.retire_neo4j(graph)
    assert dry["nodes_to_delete"] == 5
    assert dry["shared_label_nodes_skipped"] == 2
    assert dry["indexes_to_drop"] == 2
    assert dry["deleted_nodes"] == 0

    applied = script.retire_neo4j(graph, execute=True)
    assert applied["deleted_nodes"] == 5
    assert applied["dropped_indexes"] == 2
    assert graph.indexes == []
    deletes = [query for query in graph.calls if "DETACH DELETE" in query]
    assert len(deletes) == 1
    assert "MATCH (n:PalaceEntity)" in deletes[0]
    assert "NOT n:OntologyEntity" in deletes[0]
    assert "MATCH (n) " not in deletes[0]
    drops = [query for query in graph.calls if query.startswith("DROP INDEX")]
    assert drops and all("palace_entity_" in query for query in drops)
    assert all("ontology_entity_" not in query for query in drops)


@pytest.mark.parametrize(
    "responses",
    [
        [],
        [{"count": "3"}],
        [{"count": None}],
        [{"count": 1}, {"count": 1}, [{"bad": "row"}]],
    ],
)
def test_neo4j_rejects_empty_or_invalid_count_results(responses):
    graph = FakeQueryGraph(responses)
    result = script.retire_neo4j(graph, execute=True)
    assert result["status"] == "error"
    assert result["deleted_nodes"] == 0


def test_neo4j_drop_must_verify_index_removed():
    class NoDropGraph(FakeGraph):
        def run_cypher(self, query: str, params=None):
            if query.startswith("DROP INDEX"):
                self.calls.append(query)
                return []
            return super().run_cypher(query, params=params)

    graph = NoDropGraph(indexes=["palace_entity_name"])
    result = script.retire_neo4j(graph, execute=True)
    assert result["status"] == "error"
    assert result["dropped_indexes"] == 0


def test_neo4j_session_adapter_consumes_results():
    class FakeResult:
        def __init__(self):
            self.consumed = False

        def __iter__(self):
            return iter([{"count": 1}])

        def consume(self):
            self.consumed = True

    class FakeSession:
        def __init__(self, result):
            self.result = result

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **params):
            return self.result

    result_obj = FakeResult()

    class FakeDriver:
        def session(self):
            return FakeSession(result_obj)

    graph = script.StrictNeo4jGraph(FakeDriver())
    assert graph.run_cypher("RETURN 1") == [{"count": 1}]
    assert result_obj.consumed is True


def test_neo4j_unavailable_is_reported_without_mutation():
    class Unavailable:
        available = False

        def run_cypher(self, query, params=None):
            raise AssertionError("must not query unavailable graph")

    result = script.retire_neo4j(Unavailable(), execute=True)
    assert result["status"] == "unavailable"
    assert result["deleted_nodes"] == 0


def test_redis_scans_exact_namespace_and_deletes_only_retired_keys():
    client = FakeRedis([
        "ob:palace:graph:ver",
        "ob:palace:graph:v0:user-1",
        b"ob:palace:graph:v1:user-2",
        "ontology:cache:list",
    ])
    dry = script.retire_redis(client)
    assert dry == {"status": "ok", "keys": 3, "deleted": 0}
    assert client.delete_calls == []
    assert {call[1] for call in client.scan_calls} == {"ob:palace:*"}

    applied = script.retire_redis(client, execute=True)
    assert applied == {"status": "ok", "keys": 3, "deleted": 3}
    assert client.keys == ["ontology:cache:list"]
    assert all(
        key.startswith("ob:palace:") for call in client.delete_calls for key in call
    )


def test_redis_unavailable_is_reported():
    assert script.retire_redis(None)["status"] == "unavailable"


@pytest.mark.asyncio
async def test_nats_dry_run_reports_only_retired_consumers_and_subjects():
    js = FakeJS()
    result = await script.retire_nats(js)
    assert result["old_subjects_present"] == 4
    assert result["old_consumers_present"] == 4
    assert result["deleted_consumers"] == 0
    assert not any(call[0] in {"delete_consumer", "purge_stream", "update_stream"} for call in js.calls)


@pytest.mark.asyncio
async def test_nats_execute_removes_only_exact_retired_state_and_preserves_rest():
    js = FakeJS()
    result = await script.retire_nats(js, execute=True)
    assert result["deleted_consumers"] == 4
    assert result["purged_subjects"] == 4
    assert result["removed_subjects"] == 4
    assert set(js.subjects) == {"pipeline.task.execute", "integrated.sync"}
    assert set(js.consumers) == {"pipeline-executor"}
    purged = [call[2] for call in js.calls if call[0] == "purge_stream"]
    assert set(purged) == set(script.NATS_SUBJECTS)
    assert all("*" not in subject and ">" not in subject for subject in purged)
    assert not any(call[0] == "delete_stream" for call in js.calls)


@pytest.mark.asyncio
async def test_nats_is_idempotent_after_first_execute():
    js = FakeJS()
    await script.retire_nats(js, execute=True)
    mutations = len([
        call for call in js.calls
        if call[0] in {"delete_consumer", "purge_stream", "update_stream"}
    ])
    second = await script.retire_nats(js, execute=True)
    assert second["status"] == "ok"
    assert second["deleted_consumers"] == 0
    assert second["purged_subjects"] == 0
    assert second["removed_subjects"] == 0
    assert len([
        call for call in js.calls
        if call[0] in {"delete_consumer", "purge_stream", "update_stream"}
    ]) == mutations


@pytest.mark.asyncio
async def test_nats_unconfirmed_mutation_is_an_error():
    class UnconfirmedJS(FakeJS):
        async def delete_consumer(self, stream, consumer):
            self.calls.append(("delete_consumer", stream, consumer))
            return False

    result = await script.retire_nats(UnconfirmedJS(), execute=True)
    assert result["status"] == "error"
    assert result["deleted_consumers"] == 0


@pytest.mark.asyncio
async def test_nats_subject_update_must_verify_removal():
    class StuckStreamJS(FakeJS):
        async def update_stream(self, config):
            self.calls.append(("update_stream", None))
            return SimpleNamespace(config=config)

    result = await script.retire_nats(StuckStreamJS(), execute=True)
    assert result["status"] == "error"
    assert result["removed_subjects"] == 0


@pytest.mark.asyncio
async def test_nats_missing_stream_is_a_noop():
    result = await script.retire_nats(FakeJS(missing=True), execute=True)
    assert result["status"] == "stream_missing"
    assert result["deleted_consumers"] == 0


@pytest.mark.asyncio
async def test_retire_all_dry_run_aggregates_counts_without_leaking_paths(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    _workspace(uploads / script.PALACE_DIR_NAME)
    graph = FakeGraph(nodes=1, shared=0)
    redis_client = FakeRedis(["ob:palace:graph:ver"])
    js = FakeJS()

    result = await script.retire_all(
        execute=False,
        uploads_dir=uploads,
        graph=graph,
        redis_client=redis_client,
        nats_js=js,
    )
    assert result["mode"] == "dry-run"
    assert result["errors"] == []
    assert result["aborted"] is False
    assert result["partial"] is False
    assert result["execution"] is None
    assert result["inspection"]["uploads"]["files"] == 2
    assert result["inspection"]["neo4j"]["nodes_to_delete"] == 1
    assert result["inspection"]["redis"]["keys"] == 1
    assert result["inspection"]["nats"]["old_subjects_present"] == 4
    assert str(tmp_path) not in json.dumps(result, ensure_ascii=False)
    assert "palace" not in json.dumps(result, ensure_ascii=False)


@pytest.mark.asyncio
async def test_retire_all_aborts_every_source_when_preflight_blocks(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    root = _workspace(uploads / script.PALACE_DIR_NAME)
    graph = FakeGraph(nodes=1, shared=0)
    redis_client = FakeRedis(["ob:palace:graph:ver"])

    result = await script.retire_all(
        execute=True,
        uploads_dir=uploads,
        graph=graph,
        redis_client=redis_client,
        nats_js=None,
    )
    assert result["aborted"] is True
    assert result["partial"] is False
    assert result["execution"] is None
    assert result["errors"] == ["nats:not_configured"]
    assert root.exists()
    assert graph.nodes == 1
    assert redis_client.keys == ["ob:palace:graph:ver"]
    assert not any("DETACH DELETE" in query for query in graph.calls)
    assert redis_client.delete_calls == []


@pytest.mark.asyncio
async def test_retire_all_aborts_on_unsafe_uploads_without_touching_other_sources(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    outside = _workspace(tmp_path / "outside")
    (uploads / script.PALACE_DIR_NAME).symlink_to(outside, target_is_directory=True)
    graph = FakeGraph(nodes=1, shared=0)
    redis_client = FakeRedis(["ob:palace:graph:ver"])
    js = FakeJS()

    result = await script.retire_all(
        execute=True,
        uploads_dir=uploads,
        graph=graph,
        redis_client=redis_client,
        nats_js=js,
    )
    assert result["aborted"] is True
    assert result["execution"] is None
    assert result["errors"] == ["uploads:unsafe"]
    assert outside.exists()
    assert not any("DETACH DELETE" in query for query in graph.calls)
    assert redis_client.delete_calls == []
    assert not any(
        call[0] in {"delete_consumer", "purge_stream", "update_stream"}
        for call in js.calls
    )


@pytest.mark.asyncio
async def test_retire_all_aborts_when_neo4j_is_unavailable(tmp_path):
    class UnavailableGraph:
        available = False

    uploads = tmp_path / "uploads"
    uploads.mkdir()
    root = _workspace(uploads / script.PALACE_DIR_NAME)
    redis_client = FakeRedis(["ob:palace:graph:ver"])
    js = FakeJS()

    result = await script.retire_all(
        execute=True,
        uploads_dir=uploads,
        graph=UnavailableGraph(),
        redis_client=redis_client,
        nats_js=js,
    )
    assert result["aborted"] is True
    assert result["execution"] is None
    assert result["errors"] == ["neo4j:unavailable"]
    assert root.exists()
    assert redis_client.delete_calls == []
    assert not any(
        call[0] in {"delete_consumer", "purge_stream", "update_stream"}
        for call in js.calls
    )


@pytest.mark.asyncio
async def test_retire_all_execute_revalidates_then_mutates_all_sources(tmp_path):
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    root = _workspace(uploads / script.PALACE_DIR_NAME)
    graph = FakeGraph(nodes=1, shared=0)
    redis_client = FakeRedis(["ob:palace:graph:ver"])
    js = FakeJS()

    result = await script.retire_all(
        execute=True,
        uploads_dir=uploads,
        graph=graph,
        redis_client=redis_client,
        nats_js=js,
    )
    assert result["aborted"] is False
    assert result["partial"] is False
    assert result["errors"] == []
    assert result["execution"]["uploads"]["deleted"] is True
    assert result["execution"]["neo4j"]["deleted_nodes"] == 1
    assert result["execution"]["redis"]["deleted"] == 1
    assert result["execution"]["nats"]["deleted_consumers"] == 4
    assert not root.exists()
    assert graph.nodes == 0
    assert redis_client.keys == []
    assert set(js.subjects) == {"pipeline.task.execute", "integrated.sync"}


def test_redis_cache_url_matches_retired_db1_cache():
    assert script._redis_cache_url("") == ""
    assert script._redis_cache_url("redis://host:6379") == "redis://host:6379/1"
    assert script._redis_cache_url("rediss://host:6380/0") == "rediss://host:6380/1"
    assert script._redis_cache_url("redis://host:6379/9") == "redis://host:6379/1"


def test_cli_parser_defaults_to_dry_run():
    args = script.build_parser().parse_args([])
    assert args.execute is False
    assert args.palace_root is None
    assert args.allow_custom_root is False
