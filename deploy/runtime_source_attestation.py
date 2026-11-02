#!/usr/bin/env python3
"""Exact-HEAD backend runtime source manifest and running-container verification.

The manifest is generated from Git objects at one exact commit, not from an
arbitrary archive worktree.  The verifier reads raw file bytes from the active
backend container and compares them with the manifest that travelled inside the
digest-verified deployment archive.

Scope is deliberately narrow: on-disk production source under ``backend/app``
inside the running container.  It does not claim to verify loaded Python memory
or the contents of the virtual environment; the recorded image ID is the
identity binding for the image itself.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import signal
import stat
import subprocess
import sys
from typing import Any

SCHEMA = "openontology-runtime-source-manifest/v1"
ALGORITHM = "sha256"
SOURCE_PREFIX = "backend/app"
SOURCE_ROOT = "/app/app"
CACHE_DIR_NAMES = frozenset({"__pycache__"})
CACHE_SUFFIXES = (".pyc", ".pyo")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{12,64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
STARTED_AT_RE = re.compile(r"^[0-9T:.\-+Z]+$")
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
DETAIL_LIMIT = 20


class AttestationError(RuntimeError):
    """A fail-closed manifest or verification error."""


def _git(repo: str, *args: str) -> bytes:
    try:
        return subprocess.run(
            ["git", "-C", repo, *args],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ).stdout
    except FileNotFoundError as exc:
        raise AttestationError("git is not installed") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", "replace").strip()
        message = f"git {' '.join(args)} failed"
        if detail:
            message = f"{message}: {detail.splitlines()[-1]}"
        raise AttestationError(message) from exc


def _validate_commit(value: str, label: str) -> str:
    if not isinstance(value, str) or not COMMIT_RE.fullmatch(value):
        raise AttestationError(f"{label} must be a full 40-character lowercase hex commit")
    return value


def _validate_path(path: Any) -> str:
    if not isinstance(path, str) or not path:
        raise AttestationError("manifest paths must be non-empty strings")
    if path.startswith("/") or "\\" in path:
        raise AttestationError(f"manifest path is not relative POSIX: {path!r}")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
        raise AttestationError(f"manifest path contains control characters: {path!r}")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise AttestationError(f"manifest path contains traversal or empty components: {path!r}")
    if not path.startswith(SOURCE_PREFIX + "/"):
        raise AttestationError(f"manifest path is outside the allowed source prefix: {path!r}")
    return path


def _is_cache_path(path: str) -> bool:
    parts = path.split("/")
    if any(part in CACHE_DIR_NAMES for part in parts[:-1]):
        return True
    return parts[-1].endswith(CACHE_SUFFIXES)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AttestationError("source file is unreadable") from exc
    return digest.hexdigest()


def _file_set_digest(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for path in sorted(files, key=lambda value: value.encode("utf-8")):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[path].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _require_clean_source_tree(repo: str, commit: str) -> None:
    """Fail when the archived allowlisted worktree is not the exact commit tree."""
    head = _git(repo, "rev-parse", "HEAD").decode().strip()
    if head != commit:
        raise AttestationError(
            f"manifest generation must use the checked-out HEAD {head}, not {commit}"
        )
    status = _git(
        repo,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--",
        SOURCE_PREFIX,
    ).decode("utf-8", "replace")
    if status.strip():
        first = status.splitlines()[0]
        raise AttestationError(
            f"tracked or untracked worktree changes exist under {SOURCE_PREFIX}; "
            f"refusing to mix archive bytes with Git commit {commit}: {first}"
        )


def generate_manifest(repo: str, commit: str, output: str) -> dict[str, Any]:
    commit = _validate_commit(commit, "commit")
    head = _git(repo, "rev-parse", "HEAD").decode().strip()
    if head != commit:
        raise AttestationError(
            f"manifest generation must use the checked-out HEAD {head}, not {commit}"
        )
    resolved_commit = _git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    if resolved_commit != commit:
        raise AttestationError("requested commit does not resolve to itself")
    tree = _git(repo, "rev-parse", "--verify", f"{commit}^{{tree}}").decode().strip()
    _validate_commit(tree, "tree")
    _require_clean_source_tree(repo, commit)

    listing = _git(
        repo,
        "ls-tree",
        "-r",
        "-z",
        "--full-tree",
        commit,
        "--",
        SOURCE_PREFIX,
    )
    files: dict[str, str] = {}
    for entry in listing.split(b"\0"):
        if not entry:
            continue
        header, separator, raw_path = entry.partition(b"\t")
        if not separator:
            raise AttestationError("git ls-tree returned an entry without a path")
        fields = header.split(b" ")
        if len(fields) != 3:
            raise AttestationError("git ls-tree returned an unexpected entry header")
        mode, object_type, object_id = fields
        try:
            path = raw_path.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AttestationError("git ls-tree returned a non-UTF-8 path") from exc
        _validate_path(path)
        if object_type != b"blob":
            raise AttestationError(f"unsupported production source object type at {path}")
        if mode not in (b"100644", b"100755"):
            raise AttestationError(f"unsupported production source mode {mode!r} at {path}")
        if _is_cache_path(path):
            continue
        blob = _git(repo, "cat-file", "blob", object_id.decode("ascii"))
        files[path] = _sha256_bytes(blob)

    if not files:
        raise AttestationError(f"no production source files found under {SOURCE_PREFIX}")

    payload = {
        "schema": SCHEMA,
        "algorithm": ALGORITHM,
        "source_prefix": SOURCE_PREFIX,
        "commit": commit,
        "tree": tree,
        "file_count": len(files),
        "file_set_sha256": _file_set_digest(files),
        "files": files,
    }
    encoded = (
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    parent = os.path.dirname(os.path.abspath(output))
    os.makedirs(parent, exist_ok=True)
    temporary = f"{output}.tmp.{os.getpid()}"
    try:
        with open(temporary, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    _ = _validate_manifest_payload(payload)
    print(
        "runtime source manifest generated: "
        f"schema={SCHEMA} commit={commit} tree={tree} files={len(files)} "
        f"file_set_sha256={payload['file_set_sha256']} manifest_sha256={_sha256_bytes(encoded)}"
    )
    return payload


def _no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AttestationError(f"manifest contains a duplicate key: {key!r}")
        result[key] = value
    return result


def _read_manifest_bytes(args: argparse.Namespace) -> bytes:
    if args.manifest_stdin:
        raw = sys.stdin.buffer.read(MAX_MANIFEST_BYTES + 1)
    elif args.manifest_b64 is not None:
        try:
            raw = base64.b64decode(args.manifest_b64, validate=True)
        except (ValueError, TypeError) as exc:
            raise AttestationError("manifest base64 is malformed") from exc
    else:
        path = args.manifest
        if not os.path.isfile(path) or os.path.islink(path):
            raise AttestationError("manifest is missing or is not a regular file")
        try:
            with open(path, "rb") as handle:
                raw = handle.read(MAX_MANIFEST_BYTES + 1)
        except OSError as exc:
            raise AttestationError("manifest is unreadable") from exc
    if not raw:
        raise AttestationError("manifest is empty")
    if len(raw) > MAX_MANIFEST_BYTES:
        raise AttestationError("manifest exceeds the size limit")
    return raw


def _validate_manifest_payload(payload: Any) -> dict[str, str]:
    if not isinstance(payload, dict):
        raise AttestationError("manifest root must be an object")
    if payload.get("schema") != SCHEMA:
        raise AttestationError("manifest schema is unsupported")
    if payload.get("algorithm") != ALGORITHM:
        raise AttestationError("manifest algorithm is unsupported")
    if payload.get("source_prefix") != SOURCE_PREFIX:
        raise AttestationError("manifest source prefix is unsupported")
    _validate_commit(payload.get("commit"), "manifest commit")
    _validate_commit(payload.get("tree"), "manifest tree")
    file_set_sha256 = payload.get("file_set_sha256")
    if not isinstance(file_set_sha256, str) or not SHA256_RE.fullmatch(file_set_sha256):
        raise AttestationError("manifest file_set_sha256 is malformed")
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise AttestationError("manifest files must be a non-empty object")
    if payload.get("file_count") != len(files):
        raise AttestationError("manifest file_count does not match files")
    normalized: dict[str, str] = {}
    for path, digest in files.items():
        _validate_path(path)
        if not isinstance(digest, str) or not SHA256_RE.fullmatch(digest):
            raise AttestationError(f"manifest hash is malformed at {path}")
        normalized[path] = digest
    if _file_set_digest(normalized) != file_set_sha256:
        raise AttestationError("manifest file_set_sha256 does not match its file entries")
    return normalized


def _walk_actual(source_root: str) -> dict[str, str]:
    if os.path.islink(source_root):
        raise AttestationError("source root must not be a symlink")
    if not os.path.isdir(source_root):
        raise AttestationError("source root is missing or is not a directory")

    actual: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(source_root, followlinks=False):
        retained_dirs: list[str] = []
        for dirname in sorted(dirnames):
            full = os.path.join(dirpath, dirname)
            entry = os.lstat(full)
            if stat.S_ISLNK(entry.st_mode):
                raise AttestationError("source tree contains a symlinked directory")
            if not stat.S_ISDIR(entry.st_mode):
                raise AttestationError("source tree contains a non-directory directory entry")
            if dirname in CACHE_DIR_NAMES:
                continue
            retained_dirs.append(dirname)
        dirnames[:] = retained_dirs
        for filename in sorted(filenames):
            full = os.path.join(dirpath, filename)
            entry = os.lstat(full)
            if stat.S_ISLNK(entry.st_mode):
                raise AttestationError("source tree contains a symlink")
            if not stat.S_ISREG(entry.st_mode):
                raise AttestationError("source tree contains a non-regular file")
            if filename.endswith(CACHE_SUFFIXES):
                continue
            relative = os.path.relpath(full, source_root).replace(os.sep, "/")
            manifest_path = f"{SOURCE_PREFIX}/{relative}"
            _validate_path(manifest_path)
            actual[manifest_path] = _sha256_file(full)
    return actual


def _sorted_paths(paths: set[str]) -> list[str]:
    return sorted(paths, key=lambda value: value.encode("utf-8"))


def _emit_mismatch(
    *,
    manifest: dict[str, Any],
    manifest_sha256: str,
    expected: dict[str, str],
    actual: dict[str, str],
    missing: list[str],
    extra: list[str],
    changed: list[str],
) -> None:
    print(
        "runtime source attestation: status=mismatch "
        f"commit={manifest['commit']} tree={manifest['tree']} "
        f"manifest_sha256={manifest_sha256} source_prefix={SOURCE_PREFIX} "
        f"expected_files={len(expected)} actual_files={len(actual)} "
        f"missing={len(missing)} extra={len(extra)} changed={len(changed)}",
        file=sys.stderr,
    )
    for label, paths in (("missing_path", missing), ("extra_path", extra)):
        for path in paths[:DETAIL_LIMIT]:
            print(f"{label}={path}", file=sys.stderr)
        if len(paths) > DETAIL_LIMIT:
            print(f"{label}_truncated={len(paths) - DETAIL_LIMIT}", file=sys.stderr)
    for path in changed[:DETAIL_LIMIT]:
        print(
            f"changed_path={path} expected_sha256={expected[path]} actual_sha256={actual[path]}",
            file=sys.stderr,
        )
    if len(changed) > DETAIL_LIMIT:
        print(f"changed_path_truncated={len(changed) - DETAIL_LIMIT}", file=sys.stderr)


def _raise_verification_timeout(signum: int, frame: Any) -> None:
    raise AttestationError("verification timed out")


def verify_manifest(args: argparse.Namespace) -> None:
    expected_commit = _validate_commit(args.expected_commit, "expected commit")
    timeout_seconds = args.timeout_seconds
    if timeout_seconds < 1 or timeout_seconds > 900:
        raise AttestationError("timeout seconds must be between 1 and 900")
    signal.signal(signal.SIGALRM, _raise_verification_timeout)
    signal.alarm(timeout_seconds)
    if not CONTAINER_ID_RE.fullmatch(args.container_id):
        raise AttestationError("container id is malformed")
    if not IMAGE_ID_RE.fullmatch(args.image_id):
        raise AttestationError("image id is malformed")
    if not STARTED_AT_RE.fullmatch(args.started_at):
        raise AttestationError("container StartedAt is malformed")

    raw = _read_manifest_bytes(args)
    manifest_sha256 = _sha256_bytes(raw)
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AttestationError("manifest is not valid UTF-8 JSON") from exc
    expected = _validate_manifest_payload(payload)
    if payload["commit"] != expected_commit:
        raise AttestationError(
            f"manifest commit {payload['commit']} does not match expected commit {expected_commit}"
        )

    actual = _walk_actual(args.source_root)
    expected_paths = set(expected)
    actual_paths = set(actual)
    missing = _sorted_paths(expected_paths - actual_paths)
    extra = _sorted_paths(actual_paths - expected_paths)
    changed = _sorted_paths(
        path for path in expected_paths & actual_paths if expected[path] != actual[path]
    )
    if missing or extra or changed:
        _emit_mismatch(
            manifest=payload,
            manifest_sha256=manifest_sha256,
            expected=expected,
            actual=actual,
            missing=missing,
            extra=extra,
            changed=changed,
        )
        raise SystemExit(1)

    print(
        "status=ok scope=running_container_files_not_loaded_memory "
        f"commit={payload['commit']} tree={payload['tree']} "
        f"manifest_sha256={manifest_sha256} file_set_sha256={payload['file_set_sha256']} "
        f"source_prefix={SOURCE_PREFIX} files={len(expected)} "
        f"container_id={args.container_id} image_id={args.image_id} "
        f"started_at={args.started_at}"
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate a manifest from one exact Git commit")
    generate.add_argument("--repo", required=True)
    generate.add_argument("--commit", required=True)
    generate.add_argument("--output", required=True)
    generate.set_defaults(handler=lambda args: generate_manifest(args.repo, args.commit, args.output))

    verify = subparsers.add_parser("verify", help="verify a running container source tree")
    manifest_group = verify.add_mutually_exclusive_group(required=True)
    manifest_group.add_argument("--manifest")
    manifest_group.add_argument("--manifest-b64")
    manifest_group.add_argument("--manifest-stdin", action="store_true")
    verify.add_argument("--source-root", required=True)
    verify.add_argument("--expected-commit", required=True)
    verify.add_argument("--container-id", required=True)
    verify.add_argument("--image-id", required=True)
    verify.add_argument("--started-at", required=True)
    verify.add_argument("--timeout-seconds", type=int, default=120)
    verify.set_defaults(handler=verify_manifest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except AttestationError as exc:
        print(f"runtime source attestation: status=error reason={exc}", file=sys.stderr)
        return 1
    finally:
        signal.alarm(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
