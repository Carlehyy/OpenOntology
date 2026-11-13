#!/usr/bin/env python3
"""Minimal PostgreSQL logical-decoding-message probe (Debezium 3.7.1.Final).

Protocol exploration only — never a production pass. This script owns the
labeled probe containers and the source table ``public.msg_probe``; it does not
touch the acceptance fixtures and it drops everything it creates. It answers:

* does ``pg_logical_emit_message(false, prefix, token)`` reach the HTTP sink;
* what envelope/op/source/transaction metadata does the pinned connector emit;
* is a transactional marker distinguishable from a non-transactional one;
* is the marker ordered against ordinary row changes in the same stream.

Evidence is written to ``.artifacts/replica-message-probe/``. Any failure is
reported as-is; this probe proves nothing about replica publication.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[2]
ART = ROOT / ".artifacts" / "replica-message-probe"
NETWORK = "oo-replica-spike-20261112"
SOURCE = "oo-replica-spike-20261112-source-pg-1"
IMAGE = "openontology-cdc-executor:canary"
DBZ = "quay.io/debezium/server:3.7.1.Final@sha256:8c4e6c7efc392f9adfd283ede2e75cd1a3594ef1126b014248054a5e718bfbd5"
RECEIVER = "oo-replica-msgprobe-receiver"
CONNECTOR = "oo-replica-msgprobe-dbz"
LABEL = "openontology.replica-msgprobe=20261113"
PREFIX = "replica_probe_msg"
USER = "replica_fixture"
DATABASE = "replica_source"


def run(args, *, stdin=None, timeout=90, check=True):
    result = subprocess.run(args, input=stdin, text=True,
                            capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(args[:6])}\n"
            f"stdout: {result.stdout[-1500:]}\nstderr: {result.stderr[-1500:]}")
    return result


def sql(statement, *, check=True):
    return run(["docker", "exec", "-i", SOURCE, "psql", "-X", "-qAt",
                "-v", "ON_ERROR_STOP=1", "-U", USER, "-d", DATABASE],
               stdin=statement, check=check).stdout.strip()


def label_of(name):
    result = subprocess.run(
        ["docker", "inspect", "--format",
         '{{index .Config.Labels "openontology.replica-msgprobe"}}', name],
        capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def owned_remove():
    for name in (CONNECTOR, RECEIVER):
        if label_of(name) == "20261113":
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)


def wait(check, *, seconds, what, interval=0.25):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(interval)
    raise RuntimeError(f"timeout waiting for {what}; last={last!r}")


RAW_RECEIVER = r'''
import argparse, json, os, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.RLock()

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *args):
        pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        record = {"path": self.path,
                  "content_type": self.headers.get("Content-Type"),
                  "body": body.decode("utf-8", "replace")}
        with LOCK:
            with open(os.path.join(ARGS.data, "bodies.jsonl"), "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        raw = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
    def do_GET(self):
        with LOCK:
            path = os.path.join(ARGS.data, "bodies.jsonl")
            count = sum(1 for _ in open(path, encoding="utf-8")) if os.path.exists(path) else 0
        raw = json.dumps({"bodies": count}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

parser = argparse.ArgumentParser()
parser.add_argument("--data", default="/data")
ARGS = parser.parse_args()
os.makedirs(ARGS.data, exist_ok=True)
ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
'''


def properties(path: Path) -> Path:
    items = {
        "quarkus.http.host": "127.0.0.1",
        "quarkus.http.port": "18086",
        "debezium.sink.type": "http",
        "debezium.sink.http.url": f"http://{RECEIVER}:8080/ingest/msgprobe",
        "debezium.sink.http.timeout.ms": "10000",
        "debezium.sink.http.retries": "1000",
        "debezium.sink.http.retry.interval.ms": "200",
        "debezium.sink.http.batch.enabled": "false",
        "debezium.format.key": "json",
        "debezium.format.value": "json",
        "debezium.format.key.schemas.enable": "true",
        "debezium.format.value.schemas.enable": "true",
        "debezium.source.connector.class":
            "io.debezium.connector.postgresql.PostgresConnector",
        "debezium.source.database.hostname": "source-pg",
        "debezium.source.database.port": "5432",
        "debezium.source.database.user": USER,
        "debezium.source.database.password": USER,
        "debezium.source.database.dbname": DATABASE,
        "debezium.source.topic.prefix": "msgprobe",
        "debezium.source.plugin.name": "pgoutput",
        "debezium.source.slot.name": "replica_msg_probe",
        "debezium.source.publication.name": "replica_msg_probe",
        "debezium.source.publication.autocreate.mode": "filtered",
        "debezium.source.snapshot.mode": "no_data",
        "debezium.source.table.include.list": "public.msg_probe",
        "debezium.source.message.prefix.include.list": PREFIX,
        "debezium.source.provide.transaction.metadata": "true",
        # No heartbeat: the offset file must keep the final message offset
        # instead of being overwritten by an idle liveness heartbeat.
        "debezium.source.tombstones.on.delete": "false",
        "debezium.source.offset.storage":
            "org.apache.kafka.connect.storage.FileOffsetBackingStore",
        "debezium.source.offset.storage.file.filename": "/data/offsets.dat",
        "debezium.source.offset.flush.interval.ms": "1000",
        "debezium.source.max.batch.size": "256",
        "debezium.source.max.queue.size": "1024",
        "debezium.source.poll.interval.ms": "50",
        "debezium.source.decimal.handling.mode": "string",
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(k + "=" + v for k, v in items.items()) + "\n")
    return path


def read_bodies(path: Path) -> list[dict]:
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            records.append(json.loads(line))
        except ValueError:
            continue
    return records


def flatten_payloads(value, path=""):
    """Yield (path, candidate) for every JSON object carrying an envelope."""
    if isinstance(value, dict):
        if isinstance(value.get("payload"), dict):
            yield path, value
        for key, child in value.items():
            yield from flatten_payloads(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from flatten_payloads(child, f"{path}[{index}]")


def summarize(bodies: list[dict]) -> list[dict]:
    found = []
    for entry in bodies:
        try:
            parsed = json.loads(entry["body"])
        except (ValueError, KeyError):
            continue
        for path, envelope in flatten_payloads(parsed):
            payload = envelope["payload"]
            source = payload.get("source") or {}
            transaction = payload.get("transaction")
            message = payload.get("message") or {}
            found.append({
                "path": path,
                "index": len(found),
                "schema_name": (envelope.get("schema") or {}).get("name"),
                "op": payload.get("op"),
                "status": payload.get("status"),
                "id": payload.get("id"),
                "prefix": message.get("prefix"),
                "content": message.get("content"),
                "message": message,
                "source": {k: source.get(k) for k in
                           ("name", "lsn", "txId", "sequence", "snapshot", "ts_ms")},
                "transaction": transaction,
                "after_id": (payload.get("after") or {}).get("id"),
                "keys": sorted(payload.keys()),
            })
    return found


def decoded_content(record):
    content = record.get("content")
    if not isinstance(content, str):
        return None
    try:
        return base64.b64decode(content, validate=True).decode()
    except (ValueError, UnicodeDecodeError):
        return None


def marker_seen(records, token):
    return next((record for record in records
                 if record.get("op") == "m"
                 and record.get("prefix") == PREFIX
                 and decoded_content(record) == token), None)


def end_record(records, transaction_id):
    if not transaction_id:
        return None
    prefix = str(transaction_id).split(":")[0] + ":"
    return next((record for record in records
                 if record.get("status") == "END"
                 and str(record.get("id", "")).startswith(prefix)), None)


def sequence_pair(record):
    raw = (record.get("source") or {}).get("sequence")
    if isinstance(raw, str):
        try:
            value = json.loads(raw)
        except ValueError:
            return None
        return value if isinstance(value, list) else None
    return raw if isinstance(raw, list) else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    if ART.exists():
        shutil.rmtree(ART)
    receiver_dir = ART / "receiver"
    config_dir = ART / "config"
    receiver_dir.mkdir(parents=True)
    config_dir.mkdir(parents=True)
    receiver_dir.chmod(0o777)
    raw_script = ART / "raw_receiver.py"
    raw_script.write_text(RAW_RECEIVER, encoding="utf-8")
    properties(config_dir / "application.properties")
    nonce = secrets.token_hex(8)
    non_tx_token = f"nontx-{nonce}"
    tx_token = f"tx-{nonce}"
    report = {"probe": "pg-logical-message-debezium-3.7.1.Final",
              "network": NETWORK, "prefix": PREFIX,
              "markers": {"non_transactional": non_tx_token,
                          "transactional": tx_token},
              "gates": {}, "notes": []}

    def capture_offsets():
        """Copy the live connector offset file (tmpfs is invisible to cp)."""
        target = ART / "offsets.dat"
        data = b""
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            copied = subprocess.run(
                ["docker", "exec", CONNECTOR, "cat", "/data/offsets.dat"],
                capture_output=True)
            if copied.returncode == 0 and copied.stdout:
                data = copied.stdout
                break
            time.sleep(0.5)
        if not data:
            report["offset_error"] = "offset file never appeared"
            return
        target.write_bytes(data)
        jar = ROOT / ".artifacts" / "replica-java" / "replica-smt.jar"
        read = run(["docker", "run", "--rm",
                    "-v", f"{jar}:/opt/debezium/lib/replica-smt.jar:ro",
                    "-v", f"{target}:/evidence/offsets.dat:ro",
                    "--entrypoint", "/opt/java/openjdk/bin/java", IMAGE,
                    "-cp", "/opt/debezium/lib/*",
                    "io.openontology.replica.ReadOffsets",
                    "/evidence/offsets.dat"], check=False)
        report["offset_read_exit"] = read.returncode
        try:
            offsets = json.loads(read.stdout)
        except ValueError:
            offsets = None
            report["offset_read_stderr"] = read.stderr[-1000:]
        report["offset_entries"] = offsets
        if not isinstance(offsets, list):
            return
        plain = [entry.get("offset") for entry in offsets
                 if isinstance(entry, dict) and isinstance(entry.get("offset"), dict)]
        message_offsets = [value for value in plain
                           if value.get("messageType") == "MESSAGE"]
        report["message_offsets"] = message_offsets
        report["gates"]["offset_records_message_type"] = bool(message_offsets)
        report["gates"]["message_offset_carries_lsn_triplet"] = bool(
            message_offsets) and all(
            value.get("lsn") == value.get("lsn_proc") == value.get("lsn_commit")
            for value in message_offsets)

    try:
        owned_remove()
        sql("""SELECT pg_terminate_backend(active_pid) FROM pg_replication_slots
               WHERE slot_name = 'replica_msg_probe';
               SELECT pg_drop_replication_slot('replica_msg_probe')
               WHERE EXISTS (SELECT 1 FROM pg_replication_slots
                             WHERE slot_name = 'replica_msg_probe');
               DROP PUBLICATION IF EXISTS replica_msg_probe;
               DROP TABLE IF EXISTS public.msg_probe;
               CREATE TABLE public.msg_probe(id integer PRIMARY KEY, note text NOT NULL);
               ALTER TABLE public.msg_probe REPLICA IDENTITY FULL;
               INSERT INTO public.msg_probe VALUES (1, 'before-messages');""")
        run(["docker", "run", "-d", "--name", RECEIVER, "--network", NETWORK,
             "--memory", "192m", "--cpus", "1", "--label", LABEL,
             "-v", f"{raw_script}:/tmp/raw_receiver.py:ro",
             "-v", f"{receiver_dir}:/data",
             "--entrypoint", "python3", IMAGE,
             "-I", "/tmp/raw_receiver.py", "--data", "/data"])
        wait(lambda: run(["docker", "inspect", "-f", "{{.State.Running}}",
                          RECEIVER]).stdout.strip() == "true",
             seconds=20, what="raw receiver start")
        time.sleep(0.5)
        run(["docker", "run", "-d", "--name", CONNECTOR, "--network", NETWORK,
             "--memory", "512m", "--cpus", "1", "--label", LABEL,
             "-e", "JAVA_OPTS=-Xms32m -Xmx256m -XX:ActiveProcessorCount=2",
             "-v", f"{config_dir / 'application.properties'}:"
                   "/debezium/config/application.properties:ro",
             "--tmpfs", "/data:size=64m,mode=1777", DBZ])
        slot_poll = report["slot_poll"] = []

        def slot_active():
            running = run(["docker", "inspect", "-f", "{{.State.Running}}",
                           CONNECTOR]).stdout.strip()
            if running != "true":
                logs = run(["docker", "logs", "--tail", "120", CONNECTOR],
                           check=False)
                raise RuntimeError(
                    "probe connector exited before slot activation:\n"
                    + (logs.stdout + logs.stderr)[-6000:])
            slots = sql("SELECT slot_name || ':' || active FROM pg_replication_slots;")
            streaming = sql("SELECT count(*) FROM pg_stat_replication"
                            " WHERE state='streaming';")
            if len(slot_poll) < 8:
                slot_poll.append({"slots": slots, "streaming": streaming})
            return slots.find("replica_msg_probe:true") >= 0 or streaming not in {"", "0"}

        wait(slot_active, seconds=90, what="probe slot active")
        # Baseline row change first, so message ordering can be compared.
        sql("INSERT INTO public.msg_probe VALUES (2, 'baseline');")
        # 1) Non-transactional logical decoding message.
        sql(f"SELECT pg_logical_emit_message(false, '{PREFIX}', '{non_tx_token}');")
        # 2) Transactional message sharing a transaction with a row change.
        sql("BEGIN;"
            f"SELECT pg_logical_emit_message(true, '{PREFIX}', '{tx_token}');"
            "INSERT INTO public.msg_probe VALUES (3, 'with-message');"
            "COMMIT;")
        # 3) Ordinary change after the messages.
        sql("INSERT INTO public.msg_probe VALUES (4, 'after-messages');")
        # 4) A long transaction (row + marker, held open) and a later
        # transaction that commits first. The wire must follow commit order,
        # not WAL position order.
        long_token, first_token = f"long-{nonce}", f"first-{nonce}"
        long_sql = ("BEGIN;"
                    "INSERT INTO public.msg_probe VALUES (5, 'long-tx');"
                    f"SELECT pg_logical_emit_message(true, '{PREFIX}', '{long_token}');"
                    "SELECT pg_sleep(2);"
                    "COMMIT;")
        failure = {}

        def run_long():
            try:
                result = subprocess.run(
                    ["docker", "exec", "-i", SOURCE, "psql", "-X", "-qAt",
                     "-v", "ON_ERROR_STOP=1", "-U", USER, "-d", DATABASE],
                    input=long_sql, text=True, capture_output=True, timeout=30)
                if result.returncode:
                    failure["long"] = result.stderr[-2000:]
            except Exception as exc:  # noqa: BLE001
                failure["long"] = f"{type(exc).__name__}: {exc}"

        thread = threading.Thread(target=run_long)
        thread.start()
        time.sleep(0.5)
        sql("BEGIN;"
            "INSERT INTO public.msg_probe VALUES (6, 'first-commit');"
            f"SELECT pg_logical_emit_message(true, '{PREFIX}', '{first_token}');"
            "COMMIT;")
        thread.join(timeout=20)
        if thread.is_alive() or failure:
            raise RuntimeError(f"long transaction failed: {failure!r}")

        # 5) Final non-transactional marker after every other record, so the
        # persisted offset is the MESSAGE offset (no later heartbeat).
        final_token = f"final-{nonce}"
        sql(f"SELECT pg_logical_emit_message(false, '{PREFIX}', '{final_token}');")

        bodies_path = receiver_dir / "bodies.jsonl"

        def collected():
            records = summarize(read_bodies(bodies_path))
            tokens = {non_tx_token, tx_token, long_token, first_token, final_token}
            seen = {decoded_content(rec) for rec in records if rec.get("op") == "m"}
            rows = {rec.get("after_id") for rec in records if rec.get("op") == "c"}
            ends = {str(rec.get("id", "")).split(":")[0]
                    for rec in records if rec.get("status") == "END"}
            return records if tokens <= seen and {2, 3, 4, 5, 6} <= rows \
                and len(ends) >= 4 else None

        records = wait(collected, seconds=90, what="logical message envelopes")
        report["records"] = records
        report["record_count"] = len(records)
        time.sleep(3)
        capture_offsets()
        non_tx = marker_seen(records, non_tx_token)
        tx = marker_seen(records, tx_token)
        long_marker = marker_seen(records, long_token)
        first_marker = marker_seen(records, first_token)
        row3 = next((rec for rec in records if rec.get("op") == "c"
                     and rec.get("after_id") == 3), None)
        row5 = next((rec for rec in records if rec.get("op") == "c"
                     and rec.get("after_id") == 5), None)
        row6 = next((rec for rec in records if rec.get("op") == "c"
                     and rec.get("after_id") == 6), None)
        tx_id = (tx or {}).get("transaction", {}) or {}
        long_tx_id = (long_marker or {}).get("transaction", {}) or {}
        first_tx_id = (first_marker or {}).get("transaction", {}) or {}
        tx_end = end_record(records, tx_id.get("id"))
        long_end = end_record(records, long_tx_id.get("id"))
        first_end = end_record(records, first_tx_id.get("id"))
        non_tx_seq = sequence_pair(non_tx) if non_tx else None
        non_tx_lsn = ((non_tx or {}).get("source") or {}).get("lsn")

        gates = report["gates"]
        gates["nontransactional_message_observed"] = bool(non_tx)
        gates["transactional_message_observed"] = bool(tx)
        gates["long_transaction_message_observed"] = bool(long_marker)
        gates["later_transaction_message_observed"] = bool(first_marker)
        gates["message_schema_is_message_value"] = all(
            (rec or {}).get("schema_name")
            == "io.debezium.connector.postgresql.MessageValue"
            for rec in (non_tx, tx, long_marker, first_marker) if rec)
        gates["message_prefix_matches"] = all(
            (rec or {}).get("prefix") == PREFIX
            for rec in (non_tx, tx, long_marker, first_marker) if rec)
        gates["message_content_base64_decodes_to_marker"] = all(
            decoded_content(rec) == token for rec, token in
            ((non_tx, non_tx_token), (tx, tx_token),
             (long_marker, long_token), (first_marker, first_token)) if rec)
        gates["message_carries_source_lsn"] = all(
            bool(((rec or {}).get("source") or {}).get("lsn"))
            for rec in (non_tx, tx, long_marker, first_marker) if rec)
        gates["message_has_op_m"] = all(
            (rec or {}).get("op") == "m"
            for rec in (non_tx, tx, long_marker, first_marker) if rec)
        gates["nontransactional_has_no_tx_id"] = bool(
            non_tx) and not (non_tx.get("transaction") or {}).get("id") \
            and not (non_tx.get("source") or {}).get("txId")
        try:
            sequence_lsns = [int(value) for value in (non_tx_seq or [])]
        except (TypeError, ValueError):
            sequence_lsns = []
        gates["nontransactional_sequence_lsn_pair_equal"] = bool(
            len(sequence_lsns) == 2 and non_tx_lsn is not None
            and sequence_lsns[0] == sequence_lsns[1] == int(non_tx_lsn))
        gates["transactional_has_tx_id"] = bool(
            tx) and bool(tx_id.get("id") or (tx.get("source") or {}).get("txId"))
        gates["transactional_message_shares_tx_with_row"] = bool(
            row3 and tx_id.get("id")
            and (row3.get("transaction") or {}).get("id") == tx_id.get("id"))
        gates["transactional_end_after_message_and_row"] = bool(
            tx_end and row3 and tx_end["index"] > row3["index"] > tx["index"])
        gates["transactional_message_between_begin_and_end"] = bool(
            tx_end and tx_end["index"] > tx["index"] > 0)
        gates["long_tx_commit_order_streaming"] = bool(
            first_marker and long_marker and row6 and row5
            and first_marker["index"] < long_marker["index"]
            and row6["index"] < row5["index"])
        gates["long_tx_end_after_first_commit_end"] = bool(
            first_end and long_end and long_end["index"] > first_end["index"])
        report["ordering"] = {
            "first_commit_tx": first_tx_id.get("id"),
            "long_tx": long_tx_id.get("id"),
            "first_commit_end": (first_end or {}).get("id"),
            "long_end": (long_end or {}).get("id"),
            "indices": {name: (record or {}).get("index") for name, record in
                        (("nontx_marker", non_tx), ("tx_marker", tx),
                         ("tx_row3", row3), ("tx_end", tx_end),
                         ("first_marker", first_marker), ("first_row6", row6),
                         ("first_end", first_end), ("long_marker", long_marker),
                         ("long_row5", row5), ("long_end", long_end))},
            "nontransactional_sequence": non_tx_seq,
        }
        tx_ids = {(rec.get("transaction") or {}).get("id")
                  or (rec.get("source") or {}).get("txId") for rec in records}
        report["observed_transaction_ids"] = sorted(
            {value for value in tx_ids if value})
        report["observed_ops"] = sorted(
            {rec.get("op") for rec in records if rec.get("op")})
        gates["row_changes_present"] = any(
            rec.get("op") in {"c", "u", "d"} for rec in records)
        gates["messages_and_rows_share_stream"] = bool(
            non_tx and tx and gates["row_changes_present"])
        gates["transaction_metadata_begin_end_observed"] = bool(
            any(rec.get("status") == "BEGIN" for rec in records)
            and any(rec.get("status") == "END" for rec in records))
        report["pass"] = all(gates.values())
        (ART / "records.json").write_text(
            json.dumps(records, ensure_ascii=False, indent=2) + "\n")
    except Exception as exc:  # noqa: BLE001 - evidence over convenience
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["pass"] = False
    finally:
        if not report.get("offset_entries") and label_of(CONNECTOR) == "20261113":
            try:
                capture_offsets()
            except Exception as exc:  # noqa: BLE001
                report["offset_error"] = f"{type(exc).__name__}: {exc}"
        if args.keep:
            report["containers_left"] = [CONNECTOR, RECEIVER]
        else:
            owned_remove()
            sql("""SELECT pg_terminate_backend(active_pid) FROM pg_replication_slots
                   WHERE slot_name = 'replica_msg_probe';
                   SELECT pg_drop_replication_slot('replica_msg_probe')
                   WHERE EXISTS (SELECT 1 FROM pg_replication_slots
                                 WHERE slot_name = 'replica_msg_probe');
                   DROP PUBLICATION IF EXISTS replica_msg_probe;
                   DROP TABLE IF EXISTS public.msg_probe;""", check=False)
        # Keep raw bodies for inspection before the receiver directory moves.
        bodies = read_bodies(receiver_dir / "bodies.jsonl")
        (ART / "bodies.json").write_text(
            json.dumps(bodies, ensure_ascii=False, indent=2) + "\n")
        (ART / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("pass") else 1


if __name__ == "__main__":
    sys.exit(main())
