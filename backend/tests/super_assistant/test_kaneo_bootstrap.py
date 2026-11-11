"""Account-only initialization against the pinned Kaneo 2.49.0 contract.

The synthetic API mirrors the upstream wire contract verified on a live
canary: better-auth 1.7.14 endpoints under ``/api/auth`` plus the public
DB-backed ``/api/instance/status``.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.super_assistant.kaneo import bootstrap


PASSWORD = "SyntheticAdmin2026!"
ENV = {
    "KANEO_BASE_URL": "http://127.0.0.1:18090",
    "KANEO_WEB_URL": "https://localhost:8445",
    "KANEO_ADMIN_USER": "bootstrap",
    "KANEO_ADMIN_EMAIL": "kaneo-admin@example.com",
    "KANEO_ADMIN_PASSWORD": PASSWORD,
}


class KaneoApi:
    """Stateful fake of the Kaneo surface the bootstrap is allowed to touch."""

    def __init__(self):
        self.exists = False
        self.password = PASSWORD
        self.role = "admin"  # first non-anonymous signup is auto-promoted
        self.has_users = False
        self.has_admin = False
        self.registration_enabled = False  # deployment posture: closed
        self.signup_mode = "open"  # open | closed | rejected
        self.register_count = 0
        self.calls = []
        self.header_checks = []

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        self.header_checks.append((
            request.headers.get("Origin"),
            request.headers.get("Cookie"),
            request.headers.get("Authorization"),
        ))
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/api/instance/status":
            return httpx.Response(200, json={
                "hasUsers": self.has_users, "hasAdmin": self.has_admin,
            })
        if path == "/api/auth/sign-in/email":
            if not self.exists or body["password"] != self.password:
                return httpx.Response(401, json={"message": "Unauthorized"})
            return httpx.Response(200, json={
                "user": {"email": body["email"], "role": self.role},
                "session": {"id": "synthetic-session"},
                "token": "synthetic-login-token",
            })
        if path == "/api/auth/sign-up/email":
            assert body == {
                "name": ENV["KANEO_ADMIN_USER"],
                "email": ENV["KANEO_ADMIN_EMAIL"],
                "password": PASSWORD,
            }
            self.register_count += 1
            if self.signup_mode == "closed":
                return httpx.Response(403, json={
                    "code": "REGISTRATION_NOT_ALLOWED",
                })
            if self.signup_mode == "rejected":
                return httpx.Response(422, json={"code": "INVALID_PASSWORD"})
            if self.exists:
                return httpx.Response(422, json={"code": "USER_ALREADY_EXISTS"})
            self.exists = True
            self.has_users = True
            self.has_admin = self.role == "admin" or self.has_admin
            # Upstream sign-up response reports the pre-promotion role.
            return httpx.Response(200, json={
                "token": "synthetic-signup-token",
                "user": {"email": body["email"], "role": "user"},
            })
        if path == "/api/auth/get-session":
            if request.headers.get("Authorization") != "Bearer synthetic-login-token":
                return httpx.Response(401, json={"message": "Unauthorized"})
            return httpx.Response(200, json={
                "session": {"id": "synthetic-session"},
                "user": {"email": ENV["KANEO_ADMIN_EMAIL"], "role": self.role},
            })
        if path == "/api/admin/settings":
            if request.headers.get("Authorization") != "Bearer synthetic-login-token":
                return httpx.Response(401, json={"message": "Unauthorized"})
            return httpx.Response(200, json={
                "registrationEnabled": self.registration_enabled,
                "passwordRegistrationEnabled": True,
                "lockedByEnv": (
                    [] if self.registration_enabled else ["registrationEnabled"]
                ),
            })
        raise AssertionError(f"Kaneo bootstrap attempted unexpected API: {path}")


def _posts(api: KaneoApi):
    return [
        (origin, cookie, authorization)
        for (method, _path), (origin, cookie, authorization) in zip(api.calls, api.header_checks)
        if method != "GET"
    ]


def test_account_initialization_is_idempotent_and_verifies_admin():
    api = KaneoApi()
    config = bootstrap.build_config(ENV)
    transport = httpx.MockTransport(api)

    bootstrap.initialize(config, transport=transport)
    bootstrap.initialize(config, transport=transport)

    assert api.register_count == 1
    bootstrap.verify(config, transport=transport)
    assert api.register_count == 1
    assert PASSWORD not in repr(config)
    allowed = ("/api/auth/", "/api/instance/status", "/api/admin/settings")
    assert all(path.startswith(allowed) for _, path in api.calls)
    assert not any("reset" in path or "password" in path for _, path in api.calls)


def test_private_better_auth_posts_carry_web_origin_and_never_cookies():
    api = KaneoApi()
    bootstrap.initialize(bootstrap.build_config(ENV), transport=httpx.MockTransport(api))
    bootstrap.verify(bootstrap.build_config(ENV), transport=httpx.MockTransport(api))

    for origin, cookie, _authorization in _posts(api):
        assert origin == ENV["KANEO_WEB_URL"]
        assert cookie is None
    # Authenticated follow-ups use the bearer token, never a session cookie.
    assert any(
        authorization == "Bearer synthetic-login-token"
        for _origin, _cookie, authorization in api.header_checks
    )


def test_existing_wrong_password_is_never_reset():
    api = KaneoApi()
    api.exists = True
    api.has_users = True
    api.password = "DifferentExisting2026!"
    with pytest.raises(bootstrap.BootstrapError, match="LOGIN_FAILED"):
        bootstrap.initialize(
            bootstrap.build_config(ENV), transport=httpx.MockTransport(api),
        )
    assert api.password == "DifferentExisting2026!"
    assert api.register_count == 0
    assert not any("reset" in path or "password" in path for _, path in api.calls)


@pytest.mark.parametrize("role", ["user", "member", ""])
def test_verify_requires_instance_admin_role_and_never_registers(role):
    api = KaneoApi()
    api.exists = True
    api.has_users = True
    api.role = role or None
    config = bootstrap.build_config(ENV)
    with pytest.raises(bootstrap.BootstrapError, match="NOT_ADMIN"):
        bootstrap.verify(config, transport=httpx.MockTransport(api))
    assert api.register_count == 0


def test_verify_requires_registration_closed():
    api = KaneoApi()
    api.exists = True
    api.has_users = True
    api.registration_enabled = True
    with pytest.raises(bootstrap.BootstrapError, match="REGISTRATION_OPEN"):
        bootstrap.verify(
            bootstrap.build_config(ENV), transport=httpx.MockTransport(api),
        )
    assert api.register_count == 0


@pytest.mark.parametrize("role", ["admin", "user,admin", "admin,custom-role"])
def test_verify_accepts_comma_separated_admin_membership(role):
    api = KaneoApi()
    api.exists = True
    api.has_users = True
    api.role = role
    bootstrap.verify(bootstrap.build_config(ENV), transport=httpx.MockTransport(api))


def test_fresh_instance_with_closed_registration_fails_with_precise_code():
    api = KaneoApi()
    api.signup_mode = "closed"
    with pytest.raises(bootstrap.BootstrapError, match="REGISTRATION_CLOSED"):
        bootstrap.initialize(
            bootstrap.build_config(ENV), transport=httpx.MockTransport(api),
        )
    assert api.register_count == 1
    assert not api.exists


def test_rejected_signup_fails_without_fallback():
    api = KaneoApi()
    api.signup_mode = "rejected"
    with pytest.raises(bootstrap.BootstrapError, match="SIGNUP_REJECTED"):
        bootstrap.initialize(
            bootstrap.build_config(ENV), transport=httpx.MockTransport(api),
        )
    assert not api.exists


@pytest.mark.parametrize("override", [
    {"KANEO_ADMIN_PASSWORD": "short"},
    {"KANEO_ADMIN_EMAIL": "invalid"},
    {"KANEO_BASE_URL": "http://user:password@127.0.0.1:18090"},
    {"KANEO_WEB_URL": "https://pm.example.com/board"},
    {"KANEO_TIMEOUT_SECONDS": "2"},
])
def test_invalid_account_configuration_fails_with_safe_code(override):
    with pytest.raises(bootstrap.BootstrapError) as error:
        bootstrap.build_config({**ENV, **override})
    assert str(error.value) == "KANEO_BOOTSTRAP_CONFIG_INVALID"
    assert PASSWORD not in str(error.value)


def test_missing_configuration_reports_missing_without_leaking_values():
    with pytest.raises(bootstrap.BootstrapError) as error:
        bootstrap.build_config({"KANEO_BASE_URL": ENV["KANEO_BASE_URL"]})
    assert str(error.value) == "KANEO_BOOTSTRAP_CONFIG_MISSING"


def test_cli_does_not_log_password_token_or_http_error_body(monkeypatch, capsys):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)

    def unavailable(request):
        return httpx.Response(500, text=f"{PASSWORD} synthetic-login-token")

    assert bootstrap.main([], transport=httpx.MockTransport(unavailable)) == 1
    output = capsys.readouterr()
    assert "KANEO_BOOTSTRAP_ERROR KANEO_BOOTSTRAP_HTTP_FAILED" in output.err
    assert PASSWORD not in output.out + output.err
    assert "synthetic-login-token" not in output.out + output.err


def test_verify_failure_exits_nonzero_with_safe_code(monkeypatch, capsys):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    api = KaneoApi()
    assert bootstrap.main(["--verify"], transport=httpx.MockTransport(api)) == 1
    output = capsys.readouterr()
    assert "KANEO_BOOTSTRAP_ERROR KANEO_BOOTSTRAP_LOGIN_FAILED" in output.err
