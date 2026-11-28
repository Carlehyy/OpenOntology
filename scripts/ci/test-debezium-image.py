#!/usr/bin/env python3
"""Unit tests for scripts/ci/debezium-image.py.

The skopeo CLI is replaced by a scripted runner, so the whole suite is
deterministic and never reaches a registry.
"""
from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

MODULE_PATH = Path(__file__).resolve().parent / "debezium-image.py"


def load_module():
    spec = importlib.util.spec_from_file_location("debezium_image", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["debezium_image"] = module
    spec.loader.exec_module(module)
    return module


dz = load_module()


def digest_of(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def platform_manifest(variant: str, os_name: str, arch: str):
    """Return (manifest bytes, manifest digest) for one platform."""
    config = json.dumps(
        {"architecture": arch, "os": os_name, "rootfs": {"type": "layers", "diff_ids": []}},
        sort_keys=True,
    ).encode("utf-8")
    layer = f"layer-{variant}-{os_name}-{arch}".encode("utf-8")
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.manifest.v1+json",
            "config": {
                "mediaType": "application/vnd.oci.image.config.v1+json",
                "digest": digest_of(config),
                "size": len(config),
            },
            "layers": [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                    "digest": digest_of(layer),
                    "size": len(layer),
                }
            ],
        },
        sort_keys=True,
    ).encode("utf-8")
    return manifest, digest_of(manifest)


class FakeSkopeo:
    """A scripted skopeo: the registry is a dictionary of raw manifests."""

    def __init__(self, *, platforms=(("linux", "amd64"), ("linux", "arm64")), variant: str = "a"):
        self.platforms = list(platforms)
        self.variant = variant
        self.tags: dict[str, bytes] = {}
        self.blobs: dict[str, bytes] = {}
        self.served: dict[str, bytes] = {}
        self.calls: list[list[str]] = []
        self.copies: list[tuple[str, str]] = []
        self.login_failure = False
        self.index_raw, self.index_digest, self.children = self._build()

    def _build(self):
        entries = []
        children = {}
        for os_name, arch in self.platforms:
            manifest, digest = platform_manifest(self.variant, os_name, arch)
            children[digest] = manifest
            self.blobs[digest] = manifest
            entries.append(
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": digest,
                    "size": len(manifest),
                    "platform": {"architecture": arch, "os": os_name},
                }
            )
        # Build attestations carry an unknown platform, exactly like the real
        # upstream index, and must not be mistaken for a duplicate platform.
        for number in range(2):
            attestation, digest = platform_manifest(
                f"{self.variant}-attestation-{number}", "unknown", "unknown"
            )
            self.blobs[digest] = attestation
            entries.append(
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "digest": digest,
                    "size": len(attestation),
                    "platform": {"architecture": "unknown", "os": "unknown"},
                }
            )
        index = {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": entries,
        }
        raw = json.dumps(index, sort_keys=True).encode("utf-8")
        self.blobs[digest_of(raw)] = raw
        return raw, digest_of(raw), children

    # -- registry state ------------------------------------------------------
    def publish(self, reference: str, raw: bytes | None = None) -> None:
        """Make ``reference`` resolve to the fixture index, or to ``raw``."""
        raw = self.index_raw if raw is None else raw
        self.tags[self._strip(reference)] = raw
        self.blobs[digest_of(raw)] = raw

    def serve(self, reference: str, raw: bytes) -> None:
        """Force a reference to resolve to specific bytes."""
        self.served[self._strip(reference)] = raw

    def withdraw(self, digest: str) -> None:
        """Make a digest unresolvable, as if the registry had dropped it."""
        self.blobs.pop(digest, None)

    @staticmethod
    def _strip(reference: str) -> str:
        """The registry reference without the transport skopeo is given."""
        return reference.removeprefix("docker://")

    def _resolve(self, reference: str):
        reference = self._strip(reference)
        if reference in self.served:
            return self.served[reference]
        if "@" in reference:
            return self.blobs.get(reference.partition("@")[2])
        return self.tags.get(reference)

    # -- runner seam ---------------------------------------------------------
    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        action = cmd[1]
        if action == "inspect":
            return self._inspect(cmd)
        if action == "copy":
            return self._copy(cmd)
        if action == "login":
            return self._login(cmd)
        raise AssertionError("unexpected skopeo invocation: " + " ".join(cmd))

    def _inspect(self, cmd):
        raw = self._resolve(cmd[-1])
        if raw is None:
            return subprocess.CompletedProcess(
                cmd, 1, b"", b"Error retrieving manifest for image: manifest unknown"
            )
        return subprocess.CompletedProcess(cmd, 0, raw, b"")

    def _copy(self, cmd):
        source, destination = cmd[-2], cmd[-1]
        self.copies.append((source, destination))
        raw = self._resolve(source)
        if raw is None:
            return subprocess.CompletedProcess(
                cmd, 1, b"", b"Error copying image: manifest unknown"
            )
        if destination.startswith("oci:"):
            self._write_layout(destination, raw)
        else:
            self.publish(destination, raw)
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    def _login(self, cmd):
        if self.login_failure:
            return subprocess.CompletedProcess(cmd, 1, b"", b"Error logging in: unauthorized")
        auth_file = Path(cmd[cmd.index("--authfile") + 1])
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        auth_file.write_text(json.dumps({"auths": {"ghcr.io": {"auth": "redacted"}}}), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, b"Login Succeeded!", b"")

    def _write_layout(self, destination: str, raw: bytes) -> None:
        path = Path(destination.split(":", 1)[1].rsplit(":", 1)[0])
        blobs = path / "blobs" / "sha256"
        blobs.mkdir(parents=True, exist_ok=True)
        (blobs / digest_of(raw).split(":", 1)[1]).write_bytes(raw)
        (path / "index.json").write_text(
            json.dumps({"schemaVersion": 2, "manifests": [{"digest": digest_of(raw)}]}),
            encoding="utf-8",
        )

    # -- helpers -------------------------------------------------------------
    def inspected(self) -> list[str]:
        return [cmd[-1] for cmd in self.calls if cmd[1] == "inspect"]

    def copy_commands(self) -> list[list[str]]:
        return [cmd for cmd in self.calls if cmd[1] == "copy"]


class RetentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="debezium-image-test-")
        self.tmp = Path(self._tmp.name)
        self.fake = FakeSkopeo()
        self.dockerfile = self.write_dockerfile()
        self.donor = dz.resolve_donor(self.dockerfile)
        self.tag_reference = f"{self.donor.repository}:{self.donor.tag}"
        # The references exactly as skopeo receives them on the command line.
        self.tag_argv = f"docker://{self.tag_reference}"
        self.source_argv = f"docker://{self.donor.source}"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_dockerfile(self, *, value: str | None = None, extra: str = "") -> Path:
        if value is None:
            value = self.pinned_value()
        path = self.tmp / "Dockerfile.cdc"
        path.write_text(f"ARG DEBEZIUM_SERVER_IMAGE={value}\n{extra}", encoding="utf-8")
        return path

    def pinned_value(self, *, digest: str | None = None, repository: str | None = None) -> str:
        digest = digest or self.fake.index_digest.split(":", 1)[1]
        repository = repository or dz.TARGET_REPOSITORY
        return f"{repository}:3.7.1.Final-sha256-{digest}@sha256:{digest}"

    def skopeo(self) -> dz.Skopeo:
        return dz.Skopeo(runner=self.fake, retry_times=2, timeout=30.0)

    def patched_skopeo(self, runner=None):
        """Let the command line entry points run against the fake skopeo."""
        real = dz.Skopeo
        chosen = runner if runner is not None else self.fake

        def factory(**kwargs):
            return real(runner=chosen, **kwargs)

        return mock.patch.object(dz, "Skopeo", factory)

    # -- reference derivation ------------------------------------------------
    def test_reference_encodes_the_dockerfile_digest(self) -> None:
        expected = self.fake.index_digest.split(":", 1)[1]
        self.assertEqual(self.donor.digest, f"sha256:{expected}")
        self.assertEqual(self.donor.version, "3.7.1.Final")
        self.assertEqual(
            self.donor.reference,
            f"ghcr.io/carlehyy/openontology/debezium:3.7.1.Final-sha256-{expected}"
            f"@sha256:{expected}",
        )

    def test_upstream_source_carries_the_same_digest(self) -> None:
        expected = self.fake.index_digest.split(":", 1)[1]
        self.assertEqual(
            self.donor.source, f"quay.io/debezium/server:3.7.1.Final@sha256:{expected}"
        )

    def test_ref_command_prints_the_full_reference(self) -> None:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = dz.main(["ref", "--dockerfile", str(self.dockerfile)])
        self.assertEqual(code, 0)
        self.assertEqual(stdout.getvalue().strip(), self.donor.reference)
        self.assertEqual(stdout.getvalue().count("\n"), 1)

    # -- negative: floating tag ---------------------------------------------
    def test_floating_tag_is_rejected(self) -> None:
        path = self.write_dockerfile(value=f"{dz.TARGET_REPOSITORY}:3.7.1.Final")
        with self.assertRaises(dz.RetentionError) as caught:
            dz.resolve_donor(path)
        self.assertIn("floating tag", str(caught.exception))

    def test_non_digest_pin_is_rejected(self) -> None:
        path = self.write_dockerfile(
            value=f"{dz.TARGET_REPOSITORY}:3.7.1.Final@sha256:deadbeef"
        )
        with self.assertRaises(dz.RetentionError) as caught:
            dz.resolve_donor(path)
        self.assertIn("sha256 digest", str(caught.exception))

    def test_reference_without_a_tag_is_rejected(self) -> None:
        path = self.write_dockerfile(value=f"{dz.TARGET_REPOSITORY}@sha256:{'0' * 64}")
        with self.assertRaises(dz.RetentionError) as caught:
            dz.resolve_donor(path)
        self.assertIn("explicit tag", str(caught.exception))

    # -- negative: upstream Quay pin ----------------------------------------
    def test_upstream_quay_pin_is_not_a_retention_target(self) -> None:
        path = self.write_dockerfile(
            value=self.pinned_value(repository=dz.SOURCE_REPOSITORY)
        )
        with self.assertRaises(dz.RetentionError) as caught:
            dz.resolve_donor(path)
        message = str(caught.exception)
        self.assertIn("must reference the retained image", message)
        self.assertIn("quay.io", message)

    # -- negative: tag and digest disagree ---------------------------------
    def test_tag_that_does_not_encode_the_digest_is_rejected(self) -> None:
        pinned = self.fake.index_digest.split(":", 1)[1]
        value = (
            f"{dz.TARGET_REPOSITORY}:3.7.1.Final-sha256-{'1' * 64}@sha256:{pinned}"
        )
        path = self.write_dockerfile(value=value)
        with self.assertRaises(dz.RetentionError) as caught:
            dz.resolve_donor(path)
        self.assertIn("must encode the pinned digest", str(caught.exception))

    def test_moving_tag_is_rejected(self) -> None:
        digest = self.fake.index_digest.split(":", 1)[1]
        path = self.write_dockerfile(
            value=f"{dz.TARGET_REPOSITORY}:3.7.1.Final@sha256:{digest}"
        )
        with self.assertRaises(dz.RetentionError) as caught:
            dz.resolve_donor(path)
        self.assertIn("must encode the pinned digest", str(caught.exception))

    def test_missing_build_argument_is_rejected(self) -> None:
        path = self.tmp / "Dockerfile.cdc"
        path.write_text("ARG PYTHON_BASE_IMAGE=python:3.12-slim\n", encoding="utf-8")
        with self.assertRaises(dz.RetentionError) as caught:
            dz.resolve_donor(path)
        self.assertIn("does not declare ARG", str(caught.exception))

    def test_conflicting_build_arguments_are_rejected(self) -> None:
        other = self.pinned_value(digest="2" * 64)
        path = self.write_dockerfile(extra=f"ARG {dz.ARG_NAME}={other}\n")
        with self.assertRaises(dz.RetentionError) as caught:
            dz.resolve_donor(path)
        self.assertIn("more than once", str(caught.exception))

    def test_repeated_identical_build_argument_is_rejected(self) -> None:
        path = self.write_dockerfile(extra=f"ARG {dz.ARG_NAME}={self.pinned_value()}\n")
        with self.assertRaises(dz.RetentionError) as caught:
            dz.resolve_donor(path)
        self.assertIn("more than once", str(caught.exception))

    def test_unsupported_donor_version_is_rejected(self) -> None:
        digest = self.fake.index_digest.split(":", 1)[1]
        value = f"{dz.TARGET_REPOSITORY}:3.8.0.Final-sha256-{digest}@sha256:{digest}"
        path = self.write_dockerfile(value=value)
        with self.assertRaises(dz.RetentionError) as caught:
            dz.resolve_donor(path)
        self.assertIn("supported donor version", str(caught.exception))

    # -- check ---------------------------------------------------------------
    def test_check_verifies_the_index_and_both_platforms(self) -> None:
        self.fake.publish(self.tag_reference)
        dz.check(self.donor, self.skopeo(), metadata_only=True)
        inspected = self.fake.inspected()
        self.assertIn(self.tag_argv, inspected)
        index = json.loads(self.fake.index_raw)
        children = {
            (entry["platform"]["os"], entry["platform"]["architecture"]): entry["digest"]
            for entry in index["manifests"]
        }
        for os_name, arch in dz.PLATFORMS:
            digest = children[(os_name, arch)]
            self.assertIn(f"docker://{self.donor.repository}@{digest}", inspected)

    def test_check_tolerates_attestation_entries(self) -> None:
        self.fake.publish(self.tag_reference)
        index = json.loads(self.fake.index_raw)
        attestations = [
            entry for entry in index["manifests"] if entry["platform"]["os"] == "unknown"
        ]
        self.assertEqual(len(attestations), 2)
        dz.check(self.donor, self.skopeo(), metadata_only=True)

    def test_check_never_contacts_the_upstream_registry(self) -> None:
        self.fake.publish(self.tag_reference)
        dz.check(self.donor, self.skopeo(), metadata_only=True)
        self.assertTrue(self.fake.inspected())
        for reference in self.fake.inspected():
            self.assertNotIn("quay.io", reference)

    def test_check_uses_an_explicit_anonymous_auth_file(self) -> None:
        self.fake.publish(self.tag_reference)
        seen: list[tuple[Path, dict]] = []

        def observer(cmd, **kwargs):
            if "--authfile" in cmd:
                path = Path(cmd[cmd.index("--authfile") + 1])
                seen.append((path, json.loads(path.read_text(encoding="utf-8"))))
            return self.fake(cmd, **kwargs)

        dz.check(self.donor, dz.Skopeo(runner=observer, timeout=30.0), metadata_only=True)
        default_auth = Path.home() / ".docker" / "config.json"
        self.assertTrue(seen)
        for path, payload in seen:
            self.assertNotEqual(path, default_auth)
            self.assertEqual(payload, {"auths": {}})
        for command in self.fake.calls:
            self.assertIn("--authfile", command)
            self.assertIn("--no-creds", command)
            self.assertNotIn("--creds", command)
            self.assertNotIn("--registry-token", command)

    def test_check_never_reads_the_user_default_auth(self) -> None:
        self.fake.publish(self.tag_reference)
        dz.check(self.donor, self.skopeo(), metadata_only=True)
        for command in self.fake.calls:
            for index, value in enumerate(command):
                if value == "--authfile":
                    self.assertNotEqual(Path(command[index + 1]), Path.home() / ".docker" / "config.json")

    def test_metadata_only_does_not_pull_blobs(self) -> None:
        self.fake.publish(self.tag_reference)
        dz.check(self.donor, self.skopeo(), metadata_only=True)
        self.assertEqual(self.fake.copy_commands(), [])

    def test_check_pulls_every_blob_into_a_temporary_layout(self) -> None:
        self.fake.publish(self.tag_reference)
        dz.check(self.donor, self.skopeo())
        commands = self.fake.copy_commands()
        self.assertEqual(len(commands), 1)
        source, destination = commands[0][-2], commands[0][-1]
        self.assertEqual(source, f"docker://{self.donor.content}")
        self.assertTrue(destination.startswith("oci:"))
        self.assertIn("--all", commands[0])
        self.assertIn("--preserve-digests", commands[0])
        self.assertIn("--src-no-creds", commands[0])

    def test_check_rejects_an_index_hash_mismatch(self) -> None:
        self.fake.publish(self.tag_reference, b'{"schemaVersion":2,"mediaType":"other"}')
        with self.assertRaises(dz.RetentionError) as caught:
            dz.check(self.donor, self.skopeo(), metadata_only=True)
        message = str(caught.exception)
        self.assertIn("digest mismatch", message)
        self.assertIn(self.donor.digest_hex, message)

    def test_check_rejects_a_tag_pointing_at_another_digest(self) -> None:
        other = FakeSkopeo(variant="b")
        self.fake.publish(self.tag_reference, other.index_raw)
        with self.assertRaises(dz.RetentionError) as caught:
            dz.check(self.donor, self.skopeo(), metadata_only=True)
        self.assertIn("digest mismatch", str(caught.exception))

    def test_check_rejects_a_single_architecture_index(self) -> None:
        single = FakeSkopeo(platforms=(("linux", "arm64"),))
        path = self.write_dockerfile(value=self.pinned_value(
            digest=single.index_digest.split(":", 1)[1]
        ))
        donor = dz.resolve_donor(path)
        single.publish(f"{donor.repository}:{donor.tag}")
        with self.assertRaises(dz.RetentionError) as caught:
            dz.check(donor, dz.Skopeo(runner=single, timeout=30.0), metadata_only=True)
        self.assertIn("linux/amd64", str(caught.exception))

    def test_check_rejects_a_single_platform_manifest(self) -> None:
        manifest = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "config": {"digest": "sha256:" + "0" * 64, "size": 1},
                "layers": [],
            },
            sort_keys=True,
        ).encode("utf-8")
        path = self.write_dockerfile(value=self.pinned_value(
            digest=hashlib.sha256(manifest).hexdigest()
        ))
        donor = dz.resolve_donor(path)
        self.fake.publish(f"{donor.repository}:{donor.tag}", manifest)
        with self.assertRaises(dz.RetentionError) as caught:
            dz.check(donor, self.skopeo(), metadata_only=True)
        self.assertIn("not an image index", str(caught.exception))

    def test_check_rejects_a_platform_manifest_with_other_bytes(self) -> None:
        self.fake.publish(self.tag_reference)
        index = json.loads(self.fake.index_raw)
        child_digest = index["manifests"][0]["digest"]
        # The registry serves bytes that do not hash to the digest the index declares.
        self.fake.serve(f"{self.donor.repository}@{child_digest}", b"tampered manifest")
        with self.assertRaises(dz.RetentionError) as caught:
            dz.check(self.donor, self.skopeo(), metadata_only=True)
        self.assertIn("digest mismatch", str(caught.exception))

    def test_check_reports_a_missing_target(self) -> None:
        with self.assertRaises(dz.RetentionError) as caught:
            dz.check(self.donor, self.skopeo(), metadata_only=True)
        self.assertIn("skopeo inspect failed", str(caught.exception))

    # -- copy ----------------------------------------------------------------
    def test_copy_requires_the_actions_environment(self) -> None:
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": ""}, clear=True):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code = dz.main(["copy", "--dockerfile", str(self.dockerfile)])
        self.assertEqual(code, 1)
        self.assertIn("only runs inside GitHub Actions", stderr.getvalue())

    def test_copy_requires_a_token_and_an_actor(self) -> None:
        for environment in (
            {"GITHUB_ACTIONS": "true", "GITHUB_ACTOR": "octocat"},
            {"GITHUB_ACTIONS": "true", "GITHUB_TOKEN": "secret-token"},
        ):
            with self.subTest(environment=sorted(environment)):
                with mock.patch.dict(os.environ, environment, clear=True):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        code = dz.main(["copy", "--dockerfile", str(self.dockerfile)])
                self.assertEqual(code, 1)
                self.assertIn("GITHUB_TOKEN and GITHUB_ACTOR are required", stderr.getvalue())

    def test_copy_logs_in_with_the_token_on_stdin(self) -> None:
        self.fake.publish(self.donor.source)
        environment = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_TOKEN": "ghp_super_secret_value",
            "GITHUB_ACTOR": "octocat",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.patched_skopeo():
                    code = dz.main(["copy", "--dockerfile", str(self.dockerfile)])
        self.assertEqual(code, 0)
        logins = [cmd for cmd in self.fake.calls if cmd[1] == "login"]
        self.assertEqual(len(logins), 1)
        self.assertIn("--password-stdin", logins[0])
        self.assertIn("--authfile", logins[0])
        self.assertIn("ghcr.io", logins[0])
        self.assertNotIn("ghp_super_secret_value", " ".join(logins[0]))

    def test_copy_uses_the_fixed_source_and_target(self) -> None:
        self.fake.publish(self.donor.source)
        dz.copy(self.donor, self.skopeo(), auth_file=self.tmp / "auth.json")
        commands = self.fake.copy_commands()
        self.assertEqual(len(commands), 1)
        source, destination = commands[0][-2], commands[0][-1]
        self.assertEqual(source, self.source_argv)
        self.assertEqual(
            source, f"docker://quay.io/debezium/server:3.7.1.Final@{self.donor.digest}"
        )
        self.assertEqual(destination, self.tag_argv)
        self.assertTrue(destination.startswith("docker://ghcr.io/carlehyy/openontology/debezium:"))

    def test_copy_command_carries_all_and_preserve_digests(self) -> None:
        self.fake.publish(self.donor.source)
        dz.copy(self.donor, self.skopeo(), auth_file=self.tmp / "auth.json")
        command = self.fake.copy_commands()[0]
        self.assertIn("--all", command)
        self.assertIn("--preserve-digests", command)
        self.assertIn("--retry-times", command)
        self.assertIn("--src-no-creds", command)
        self.assertIn("--dest-authfile", command)

    def test_copy_reads_the_upstream_anonymously(self) -> None:
        self.fake.publish(self.donor.source)
        dz.copy(self.donor, self.skopeo(), auth_file=self.tmp / "auth.json")
        source_calls = [
            cmd for cmd in self.fake.calls
            if cmd[-1].startswith("docker://quay.io/")
        ]
        self.assertTrue(source_calls)
        for command in source_calls:
            self.assertIn("--no-creds", command)
            self.assertNotIn("--dest-authfile", command)
            for index, value in enumerate(command):
                if value == "--authfile":
                    self.assertNotEqual(
                        Path(command[index + 1]), Path.home() / ".docker" / "config.json"
                    )

    def test_copy_reuses_an_identical_existing_target(self) -> None:
        self.fake.publish(self.tag_reference)
        dz.copy(self.donor, self.skopeo(), auth_file=self.tmp / "auth.json")
        self.assertEqual(self.fake.copy_commands(), [])

    def test_copy_refuses_to_overwrite_a_different_tag(self) -> None:
        other = FakeSkopeo(variant="b")
        self.fake.publish(self.tag_reference, other.index_raw)
        with self.assertRaises(dz.RetentionError) as caught:
            dz.copy(self.donor, self.skopeo(), auth_file=self.tmp / "auth.json")
        self.assertIn("refusing to overwrite", str(caught.exception))
        self.assertEqual(self.fake.copy_commands(), [])

    def test_copy_fails_when_the_upstream_index_is_missing(self) -> None:
        self.fake.withdraw(self.donor.digest)
        with self.assertRaises(dz.RetentionError) as caught:
            dz.copy(self.donor, self.skopeo(), auth_file=self.tmp / "auth.json")
        self.assertIn("skopeo inspect failed", str(caught.exception))
        self.assertEqual(self.fake.copy_commands(), [])

    def test_copy_fails_when_the_upstream_index_is_not_the_pinned_digest(self) -> None:
        other = FakeSkopeo(variant="b")
        self.fake.serve(self.donor.source, other.index_raw)
        with self.assertRaises(dz.RetentionError) as caught:
            dz.copy(self.donor, self.skopeo(), auth_file=self.tmp / "auth.json")
        self.assertIn("digest mismatch", str(caught.exception))
        self.assertEqual(self.fake.copy_commands(), [])

    def test_copy_verifies_the_target_afterwards(self) -> None:
        self.fake.publish(self.donor.source)
        dz.copy(self.donor, self.skopeo(), auth_file=self.tmp / "auth.json")
        verified = [
            cmd for cmd in self.fake.calls
            if cmd[1] == "inspect" and cmd[-1] == self.tag_argv
        ]
        self.assertGreaterEqual(len(verified), 2)
        for command in verified:
            self.assertIn("--authfile", command)
            self.assertNotIn("--no-creds", command)

    def test_copy_rejects_a_single_architecture_upstream(self) -> None:
        single = FakeSkopeo(platforms=(("linux", "arm64"),))
        path = self.write_dockerfile(
            value=self.pinned_value(digest=single.index_digest.split(":", 1)[1])
        )
        donor = dz.resolve_donor(path)
        single.publish(donor.source)
        with self.assertRaises(dz.RetentionError) as caught:
            dz.copy(donor, dz.Skopeo(runner=single, timeout=30.0), auth_file=self.tmp / "auth.json")
        self.assertIn("linux/amd64", str(caught.exception))
        self.assertEqual(single.copy_commands(), [])

    def test_copy_never_leaks_the_token(self) -> None:
        environment = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_TOKEN": "ghp_super_secret_value",
            "GITHUB_ACTOR": "octocat",
        }
        self.fake.publish(self.donor.source)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environment, clear=True):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                with self.patched_skopeo():
                    code = dz.main(["copy", "--dockerfile", str(self.dockerfile)])
        self.assertEqual(code, 0)
        self.assertNotIn("ghp_super_secret_value", stdout.getvalue())
        self.assertNotIn("ghp_super_secret_value", stderr.getvalue())
        for command in self.fake.calls:
            self.assertNotIn("ghp_super_secret_value", " ".join(command))

    def test_login_failure_is_reported_without_the_token(self) -> None:
        environment = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_TOKEN": "ghp_super_secret_value",
            "GITHUB_ACTOR": "octocat",
        }
        self.fake.login_failure = True
        with mock.patch.dict(os.environ, environment, clear=True):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.patched_skopeo():
                    code = dz.main(["copy", "--dockerfile", str(self.dockerfile)])
        self.assertEqual(code, 1)
        self.assertIn("skopeo login failed", stderr.getvalue())
        self.assertNotIn("ghp_super_secret_value", stderr.getvalue())

    def test_auth_file_is_removed_after_the_copy(self) -> None:
        environment = {
            "GITHUB_ACTIONS": "true",
            "GITHUB_TOKEN": "ghp_super_secret_value",
            "GITHUB_ACTOR": "octocat",
        }
        self.fake.publish(self.donor.source)
        seen: list[Path] = []

        def observer(cmd, **kwargs):
            for index, value in enumerate(cmd):
                if value == "--authfile":
                    seen.append(Path(cmd[index + 1]))
            return self.fake(cmd, **kwargs)

        with mock.patch.dict(os.environ, environment, clear=True):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.patched_skopeo(runner=observer):
                    code = dz.main(["copy", "--dockerfile", str(self.dockerfile)])
        self.assertEqual(code, 0)
        self.assertTrue(seen)
        for path in seen:
            self.assertFalse(path.exists())

    # -- bounded execution ---------------------------------------------------
    def test_skopeo_calls_are_bounded_by_a_timeout(self) -> None:
        def timing_out(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout", 0))

        skopeo = dz.Skopeo(runner=timing_out, timeout=1.0)
        with self.assertRaises(dz.RetentionError) as caught:
            skopeo.inspect_raw(self.tag_reference)
        self.assertIn("timed out", str(caught.exception))

    def test_missing_skopeo_is_reported(self) -> None:
        def absent(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])

        skopeo = dz.Skopeo(runner=absent, timeout=1.0)
        with self.assertRaises(dz.RetentionError) as caught:
            skopeo.inspect_raw(self.tag_reference)
        self.assertIn("not installed", str(caught.exception))

    def test_retry_times_must_be_positive(self) -> None:
        with self.assertRaises(dz.RetentionError):
            dz.Skopeo(runner=self.fake, retry_times=0)

    def test_anonymous_auth_file_is_owner_only(self) -> None:
        path = dz.write_anonymous_auth_file(self.tmp / "auth.json")
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"auths": {}})


if __name__ == "__main__":
    unittest.main(verbosity=2)
