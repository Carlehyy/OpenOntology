"""Only receiver evidence can authorize a stored connector resume position."""
from types import SimpleNamespace

import pytest

from app.data_channel.integrated import replica_offsets as offsets


def _capture(**kwargs):
    return SimpleNamespace(**{"id": "capture", "generation": 2, "source_offset": None,
                             "committed_position": None, "coverage_probe": None, **kwargs})


def _covers(capture, required, dialect, runs=()):
    return offsets.received_offset_covers(
        offsets.received_positions(capture, runs, dialect), required, dialect)


def test_postgres_counts_only_rows_at_the_resume_lsn():
    row = {"lsn": 100, "lsn_proc": 100, "lsn_commit": 90, "messageType": "INSERT",
           "lsn_events_processed": 2, "txId": 7}
    capture = _capture(source_offset=row)
    assert offsets.resumable(row, "postgresql")
    for invalid in ({"lsn_events_processed": 0}, {"lsn_proc": 90}):
        assert not offsets.resumable({**row, **invalid}, "postgresql")
    assert _covers(capture, {**row, "lsn_events_processed": 1}, "postgresql")
    assert not _covers(capture, {**row, "lsn_events_processed": 3}, "postgresql")
    offsets.record_received(capture, {**row, "lsn_events_processed": 1}, "postgresql")
    assert capture.source_offset == row

    # A logical message can inherit a row count from another LSN. A previous
    # commit can also share a position with the next transaction's first row.
    capture.source_offset = {**row, "messageType": "MESSAGE"}
    assert not _covers(capture, row, "postgresql")
    capture.committed_position = {"dialect": "postgresql", "commit_lsn": 100}
    assert not _covers(capture, {**row, "lsn_commit": 100}, "postgresql")


def test_postgres_interleaved_rows_require_evidence_in_their_commit_window():
    # T1 writes at 90, T2 commits at 110, then T1 is delivered. Neither the
    # preceding END nor a received probe before T1's END proves any T1 rows.
    row = {"lsn": 90, "lsn_proc": 90, "lsn_commit": 110, "messageType": "INSERT",
           "lsn_events_processed": 1, "txId": 7}
    end2 = {"lsn": 110, "lsn_proc": 110, "lsn_commit": 110, "messageType": "COMMIT", "txId": 8}
    capture = _capture(source_offset=end2, committed_position={
        "dialect": "postgresql", "commit_lsn": 110, "offset": end2})
    assert not _covers(capture, row, "postgresql")
    capture.coverage_probe = {"pending": {"generation": capture.generation,
                                          "observed_position": {"lsn": 115}}}
    assert not _covers(capture, row, "postgresql")
    offsets.record_received(capture, row, "postgresql")
    assert capture.source_offset == row
    assert _covers(capture, row, "postgresql")
    assert not _covers(capture, {**row, "txId": 9}, "postgresql")
    assert not _covers(capture, {**row, "lsn_commit": 115}, "postgresql")
    offsets.record_received(capture, end2, "postgresql")
    assert capture.source_offset == row

    # Debezium can emit a COMMIT heartbeat before its metadata END. Only
    # the END proves the full transaction and permits skipping that boundary.
    end1 = {**end2, "lsn": 120, "lsn_proc": 120, "lsn_commit": 120,
            "txId": 7, "transaction_id": "7"}
    capture.source_offset = end1  # also check a pre-upgrade heartbeat row
    assert not _covers(capture, end1, "postgresql")
    assert not _covers(capture, row, "postgresql")
    capture.committed_position = {"dialect": "postgresql", "commit_lsn": 120, "offset": end1}
    assert _covers(capture, row, "postgresql")
    assert _covers(capture, end1, "postgresql")
    assert not _covers(capture, {**row, "lsn": 125, "lsn_proc": 125}, "postgresql")


def test_postgres_commit_proof_and_observed_cut_are_separate_receive_evidence():
    capture = _capture(committed_position={"dialect": "postgresql", "commit_lsn": 110,
                                           "offset": {"lsn": 100}})
    assert _covers(capture, {"lsn_proc": 105}, "postgresql")
    assert not _covers(capture, {"lsn": 111}, "postgresql")

    pending = {"generation": capture.generation, "frontier": {"lsn": 130}}
    capture.coverage_probe = {"pending": pending, "confirmed": ["invalid"]}
    assert not _covers(capture, {"lsn": 120}, "postgresql")
    pending["observed_position"] = {"lsn": 120}
    assert _covers(capture, {"lsn": 120}, "postgresql")
    pending["generation"] -= 1
    assert not _covers(capture, {"lsn": 120}, "postgresql")


def test_empty_snapshot_proof_covers_the_initial_offset():
    capture = _capture(committed_position=["invalid"], coverage_probe=["invalid"])
    run = SimpleNamespace(snapshot_proof={"complete": True, "row_count": 0,
                                         "snapshot_id": capture.id, "offset": {"lsn": 100}})
    assert _covers(capture, {"lsn": 100}, "postgresql", [run])
    run.snapshot_proof["snapshot_id"] = "previous-capture"
    assert not _covers(capture, {"lsn": 100}, "postgresql", [run])


@pytest.mark.parametrize("snapshot,expected", [
    ({"snapshot": "INITIAL"}, False),
    ({"snapshot": True, "snapshot_completed": False}, False),
    ({"snapshot": "INITIAL", "last_snapshot_record": True}, True),
    ({"snapshot": "INITIAL", "snapshot_completed": True}, True),
    ({"snapshot": "unknown", "snapshot_completed": True}, False),
    ({"snapshot": 1, "snapshot_completed": True}, False),
])
def test_postgres_requires_a_loadable_snapshot_marker(snapshot, expected):
    assert offsets.resumable({"lsn": 100, **snapshot}, "postgresql") is expected


_GTID_SOURCE = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_OTHER_SOURCE = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_MYSQL_OFFSET = {"file": "binlog.10", "pos": 20, "event": 2, "row": 3,
                 "gtids": f"{_GTID_SOURCE}:1-8"}


@pytest.mark.parametrize("change,expected", [
    ({"file": "binlog.9", "pos": 900}, True),
    ({"file": "newbinlog.9"}, False),
    ({"file": "binlog.11"}, False),
    ({"event": 1, "row": 99}, True),
    ({"event": 3, "row": 0}, False),
    ({"row": 4}, False),
    ({"gtids": f"{_GTID_SOURCE}:1-3:5"}, True),
    ({"gtids": f"{_GTID_SOURCE}:1-9"}, False),
    ({"gtids": f"{_OTHER_SOURCE}:1"}, False),
])
def test_mysql_resume_requires_both_binlog_and_gtid_coverage(change, expected):
    capture = _capture(source_offset=_MYSQL_OFFSET)
    assert _covers(capture, {**_MYSQL_OFFSET, **change}, "mysql") is expected


def test_mysql_rejects_unloadable_resume_positions():
    assert offsets.resumable(_MYSQL_OFFSET, "mysql")
    for change in ({"file": 42}, {"pos": 3}, {"row": -1}, {"event": True},
                   {"gtids": "invalid"}, {"snapshot": "INITIAL", "snapshot_completed": True}):
        assert not offsets.resumable({**_MYSQL_OFFSET, **change}, "mysql")
