#!/usr/bin/env python3
"""Retain the Debezium donor image inside the OpenOntology GHCR package.

``backend/Dockerfile.cdc`` is the single source of truth: its build argument
pins ``ghcr.io/carlehyy/openontology/debezium:<version>-sha256-<hex>@sha256:<hex>``
and the upstream source is the very same digest at ``quay.io/debezium/server``.
``skopeo copy --all --preserve-digests`` moves the whole multi-architecture
index, every platform manifest and every blob without changing a digest.
There is no manifest file, no fallback and no automatic digest change.

``check`` verifies the retained image anonymously (index digest, amd64, arm64
and -- unless ``--metadata-only`` -- every blob) and never contacts the
upstream registry. ``copy`` only runs inside GitHub Actions, logs in through
stdin and never prints the token. ``ref`` prints the pinned reference.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

DOCKERFILE = Path(__file__).resolve().parents[2] / "backend" / "Dockerfile.cdc"

ARG_NAME = "DEBEZIUM_SERVER_IMAGE"
TARGET_REPOSITORY = "ghcr.io/carlehyy/openontology/debezium"
SOURCE_REPOSITORY = "quay.io/debezium/server"
# The donor runtime is contractually pinned: this change only retains the
# image, it never upgrades the classpath.
SUPPORTED_VERSION = "3.7.1.Final"
PLATFORMS = (("linux", "amd64"), ("linux", "arm64"))
INDEX_MEDIA_TYPES = frozenset({
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
})
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
TRANSPORT_RE = re.compile(r"^[a-z][a-z0-9.+-]*:")
MISSING_MARKERS = ("manifest unknown", "name unknown")


class RetentionError(RuntimeError):
    """A fail-closed retention error."""


@dataclass(frozen=True)
class Donor:
    """The retained donor image and the upstream source it was copied from."""

    repository: str
    tag: str
    version: str
    digest: str

    @property
    def reference(self) -> str:
        """The canonical reference, identical to the build argument."""
        return f"{self.repository}:{self.tag}@{self.digest}"

    @property
    def digest_hex(self) -> str:
        return self.digest.split(":", 1)[1]

    @property
    def content(self) -> str:
        """The retained content, addressed by digest."""
        return f"{self.repository}@{self.digest}"

    @property
    def source(self) -> str:
        """The upstream image carrying the very same digest."""
        return f"{SOURCE_REPOSITORY}:{self.version}@{self.digest}"


def resolve_donor(dockerfile: Path = DOCKERFILE) -> Donor:
    """Read the pinned donor image from the Dockerfile build argument."""
    try:
        text = Path(dockerfile).read_text(encoding="utf-8")
    except OSError as exc:
        raise RetentionError(f"cannot read {dockerfile}: {exc.strerror}") from exc

    matches = [
        match.group(1)
        for match in (
            re.match(rf"^ARG\s+{ARG_NAME}=(\S+)\s*(?:#.*)?$", line)
            for line in text.splitlines()
        )
        if match
    ]
    if not matches:
        raise RetentionError(f"{dockerfile} does not declare ARG {ARG_NAME}")
    if len(matches) > 1:
        raise RetentionError(f"{dockerfile} declares ARG {ARG_NAME} more than once")
    value = matches[0]

    if "@" not in value:
        raise RetentionError(
            f"ARG {ARG_NAME} must be digest-pinned, got a floating tag: {value}"
        )
    name, _, digest = value.partition("@")
    if not DIGEST_RE.match(digest):
        raise RetentionError(f"ARG {ARG_NAME} must use a sha256 digest: {value}")
    repository, separator, tag = name.rpartition(":")
    if not separator or "/" in tag:
        raise RetentionError(f"ARG {ARG_NAME} must carry an explicit tag: {value}")
    if repository != TARGET_REPOSITORY:
        raise RetentionError(
            f"ARG {ARG_NAME} must reference the retained image {TARGET_REPOSITORY}, got "
            f"{repository}; the upstream Quay pin is not a retention target"
        )
    hex_digest = digest.split(":", 1)[1]
    suffix = f"-sha256-{hex_digest}"
    if not tag.endswith(suffix):
        raise RetentionError(
            f"the retained tag must encode the pinned digest as "
            f"'<version>-sha256-{hex_digest}', got {tag}"
        )
    version = tag[: -len(suffix)]
    if not version:
        raise RetentionError(f"the retained tag {tag} carries no upstream version")
    if version != SUPPORTED_VERSION:
        raise RetentionError(
            f"the retained tag must pin the supported donor version {SUPPORTED_VERSION}, "
            f"got {version}"
        )
    return Donor(repository=repository, tag=tag, version=version, digest=digest)


def transport(reference: str) -> str:
    """The explicit transport skopeo expects for a registry reference.

    References that already carry a transport (``oci:``, ``docker://``) are
    left alone.
    """
    return reference if TRANSPORT_RE.match(reference) else f"docker://{reference}"


def digest_of(raw: bytes) -> str:
    """The digest of manifest bytes, exactly as the registry stores them.

    ``skopeo inspect --raw`` writes the manifest verbatim, without adding a
    trailing newline, so the raw bytes hash to the registry digest as is.
    """
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def require_digest(raw: bytes, expected: str, *, label: str) -> None:
    actual = digest_of(raw)
    if actual != expected:
        raise RetentionError(f"{label} digest mismatch: expected {expected}, got {actual}")


def index_platforms(raw: bytes, *, label: str) -> dict:
    """Map ``(os, architecture)`` to the manifest digest declared by the index.

    Build attestations (SBOM, provenance) carry an ``unknown/unknown``
    platform; they are part of the index but cannot serve a build, so they are
    ignored instead of being reported as a duplicate platform.
    """
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RetentionError(f"{label} is not valid JSON") from exc
    media_type = document.get("mediaType")
    if media_type is not None and media_type not in INDEX_MEDIA_TYPES:
        raise RetentionError(
            f"{label} is a single-platform manifest, not an image index; "
            "one architecture cannot serve both amd64 and arm64"
        )
    platforms = {}
    for entry in document.get("manifests") or []:
        platform = entry.get("platform") or {}
        key = (platform.get("os"), platform.get("architecture"))
        if not all(key) or "unknown" in key:
            continue
        if key in platforms:
            raise RetentionError(f"{label} declares platform {key[0]}/{key[1]} twice")
        platforms[key] = entry.get("digest")
    missing = [
        f"{os_name}/{arch}"
        for os_name, arch in PLATFORMS
        if (os_name, arch) not in platforms
    ]
    if missing:
        raise RetentionError(
            f"{label} is missing required platform(s): {', '.join(missing)}"
        )
    return platforms


def write_anonymous_auth_file(path: Path) -> Path:
    """Write a credential-free auth file.

    Without an explicit ``--authfile`` skopeo reads ``~/.docker/config.json``
    and the other default locations. Pointing it at an empty file keeps the
    anonymous check independent of the operator's local login.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump({"auths": {}}, handle)
    os.chmod(path, 0o600)
    return path


class Skopeo:
    """Thin, timeout-bounded wrapper around the skopeo CLI."""

    def __init__(
        self,
        *,
        retry_times: int = 3,
        timeout: float = 900.0,
        runner=None,
        secret: str | None = None,
    ) -> None:
        if retry_times < 1:
            raise RetentionError("retry times must be at least 1")
        if timeout <= 0:
            raise RetentionError("timeout must be positive")
        self.retry_times = retry_times
        self.timeout = timeout
        self.secret = secret
        self.calls: list[list[str]] = []
        self.runner = runner if runner is not None else subprocess.run
        if runner is None and shutil.which("skopeo") is None:
            raise RetentionError("skopeo is not installed on this machine")

    def _run(self, args, stdin: bytes | None = None):
        command = ["skopeo", *args]
        self.calls.append(list(command))
        options = {"capture_output": True, "timeout": self.timeout}
        if stdin is None:
            options["stdin"] = subprocess.DEVNULL
        else:
            options["input"] = stdin
        try:
            return self.runner(command, **options)
        except subprocess.TimeoutExpired as exc:
            raise RetentionError(
                f"skopeo {args[0]} timed out after {self.timeout:g}s"
            ) from exc
        except FileNotFoundError as exc:
            raise RetentionError("skopeo is not installed on this machine") from exc

    def _output(self, completed) -> str:
        """The redacted stderr of a failed call."""
        text = (completed.stderr or b"").decode("utf-8", "replace").strip()
        if self.secret:
            text = text.replace(self.secret, "***")
        return text or "no output"

    def inspect_raw(
        self,
        reference: str,
        *,
        auth_file: Path | None = None,
        anonymous: bool = False,
        missing_ok: bool = False,
    ) -> bytes | None:
        """Return the raw manifest bytes of ``reference``."""
        args = ["inspect", "--raw", "--retry-times", str(self.retry_times)]
        if anonymous:
            # Explicitly anonymous: no credential from the user default auth
            # file is ever used for this call.
            args.append("--no-creds")
        if auth_file is not None:
            args += ["--authfile", str(auth_file)]
        args.append(transport(reference))
        completed = self._run(args)
        if completed.returncode != 0:
            output = self._output(completed)
            if missing_ok and any(marker in output.lower() for marker in MISSING_MARKERS):
                return None
            raise RetentionError(f"skopeo inspect failed for {reference}: {output}")
        return completed.stdout or b""

    def copy(
        self,
        source: str,
        destination: str,
        *,
        source_auth_file: Path | None = None,
        dest_auth_file: Path | None = None,
        source_anonymous: bool = False,
    ) -> None:
        """Copy every platform, preserving the original digests."""
        args = ["copy", "--all", "--preserve-digests", "--retry-times", str(self.retry_times)]
        if source_anonymous:
            args.append("--src-no-creds")
        if source_auth_file is not None:
            args += ["--src-authfile", str(source_auth_file)]
        if dest_auth_file is not None:
            args += ["--dest-authfile", str(dest_auth_file)]
        args += [transport(source), transport(destination)]
        completed = self._run(args)
        if completed.returncode != 0:
            raise RetentionError(f"skopeo copy failed: {self._output(completed)}")

    def login(self, registry: str, username: str, token: str, *, auth_file: Path) -> None:
        """Log in with the token on stdin; it never reaches the command line."""
        args = [
            "login",
            "--username",
            username,
            "--password-stdin",
            "--authfile",
            str(auth_file),
            registry,
        ]
        completed = self._run(args, stdin=token.encode("utf-8"))
        if completed.returncode != 0:
            raise RetentionError(
                f"skopeo login failed for {registry}: {self._output(completed)}"
            )


def check(donor: Donor, skopeo: Skopeo, *, metadata_only: bool = False) -> None:
    """Verify the retained image. The upstream registry is never contacted."""
    tag_reference = f"{donor.repository}:{donor.tag}"
    with tempfile.TemporaryDirectory(prefix="debezium-image-check-") as workspace:
        root = Path(workspace)
        # Anonymous: an empty auth file plus --no-creds, so neither the user
        # default auth file nor any stored credential is ever consulted.
        auth_file = write_anonymous_auth_file(root / "auth.json")
        raw = skopeo.inspect_raw(tag_reference, auth_file=auth_file, anonymous=True)
        require_digest(raw, donor.digest, label=f"retained tag {tag_reference}")
        platforms = index_platforms(raw, label=f"retained tag {tag_reference}")
        for os_name, arch in PLATFORMS:
            child_digest = platforms[(os_name, arch)]
            child = skopeo.inspect_raw(
                f"{donor.repository}@{child_digest}", auth_file=auth_file, anonymous=True
            )
            require_digest(child, child_digest, label=f"retained {os_name}/{arch} manifest")
        if metadata_only:
            print("debezium-image: index and both platforms verified (--metadata-only)")
            return
        layout = root / "oci"
        layout.mkdir(parents=True, exist_ok=True)
        # skopeo verifies every blob digest while it fills the OCI layout.
        skopeo.copy(
            donor.content,
            f"oci:{layout}:debezium",
            source_auth_file=auth_file,
            source_anonymous=True,
        )
    print("debezium-image: every blob of the pulled OCI layout matches its digest")


def copy(donor: Donor, skopeo: Skopeo, *, auth_file: Path) -> None:
    """Copy the upstream donor image into the retained package."""
    tag_reference = f"{donor.repository}:{donor.tag}"
    existing = skopeo.inspect_raw(tag_reference, auth_file=auth_file, missing_ok=True)
    if existing is not None:
        actual = digest_of(existing)
        if actual != donor.digest:
            raise RetentionError(
                f"the retained tag {tag_reference} already points at {actual}; "
                "refusing to overwrite it with a different digest"
            )
        print(f"debezium-image: {tag_reference} already holds {donor.digest}, nothing to copy")
    else:
        with tempfile.TemporaryDirectory(prefix="debezium-image-anonymous-") as workspace:
            # The upstream read is anonymous: an empty auth file plus
            # --no-creds, never the operator's default auth file.
            anonymous = write_anonymous_auth_file(Path(workspace) / "auth.json")
            source_raw = skopeo.inspect_raw(donor.source, auth_file=anonymous, anonymous=True)
            require_digest(source_raw, donor.digest, label="upstream index")
            # Refuse a single-architecture upstream before anything is copied.
            index_platforms(source_raw, label="upstream index")
            skopeo.copy(
                donor.source,
                tag_reference,
                dest_auth_file=auth_file,
                source_auth_file=anonymous,
                source_anonymous=True,
            )
    final = skopeo.inspect_raw(tag_reference, auth_file=auth_file)
    require_digest(final, donor.digest, label=f"retained tag {tag_reference}")
    index_platforms(final, label=f"retained tag {tag_reference}")
    print(f"debezium-image: retained {tag_reference} holds {donor.digest}")


def command_check(args: argparse.Namespace) -> int:
    donor = resolve_donor(args.dockerfile)
    skopeo = Skopeo(retry_times=args.retry_times, timeout=args.timeout)
    check(donor, skopeo, metadata_only=args.metadata_only)
    print(f"debezium-image: check ok {donor.reference}")
    return 0


def command_copy(args: argparse.Namespace) -> int:
    if os.environ.get("GITHUB_ACTIONS") != "true":
        raise RetentionError("copy only runs inside GitHub Actions (GITHUB_ACTIONS=true)")
    token = os.environ.get("GITHUB_TOKEN")
    actor = os.environ.get("GITHUB_ACTOR")
    if not token or not actor:
        raise RetentionError(
            "GITHUB_TOKEN and GITHUB_ACTOR are required to publish the retained image"
        )
    donor = resolve_donor(args.dockerfile)
    skopeo = Skopeo(retry_times=args.retry_times, timeout=args.timeout, secret=token)
    with tempfile.TemporaryDirectory(prefix="debezium-image-copy-") as workspace:
        auth_file = Path(workspace) / "auth.json"
        skopeo.login(donor.repository.split("/", 1)[0], actor, token, auth_file=auth_file)
        copy(donor, skopeo, auth_file=auth_file)
    print(f"debezium-image: copy ok {donor.reference}")
    return 0


def command_ref(args: argparse.Namespace) -> int:
    print(resolve_donor(args.dockerfile).reference)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="debezium-image.py", description="Debezium donor image retention."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    check_parser = sub.add_parser("check", help="verify the retained donor image")
    check_parser.add_argument("--dockerfile", type=Path, default=DOCKERFILE)
    check_parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="verify the index and both platforms without pulling every blob",
    )
    check_parser.add_argument("--retry-times", type=int, default=3)
    check_parser.add_argument("--timeout", type=float, default=900.0)
    check_parser.set_defaults(handler=command_check)

    copy_parser = sub.add_parser("copy", help="copy the donor image into the retained package")
    copy_parser.add_argument("--dockerfile", type=Path, default=DOCKERFILE)
    copy_parser.add_argument("--retry-times", type=int, default=3)
    copy_parser.add_argument("--timeout", type=float, default=1800.0)
    copy_parser.set_defaults(handler=command_copy)

    ref_parser = sub.add_parser("ref", help="print the pinned image reference")
    ref_parser.add_argument("--dockerfile", type=Path, default=DOCKERFILE)
    ref_parser.set_defaults(handler=command_ref)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except RetentionError as exc:
        print(f"debezium-image: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
