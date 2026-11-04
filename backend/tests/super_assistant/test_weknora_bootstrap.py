"""Account-only initialization against the pinned upstream REST contract."""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.super_assistant.weknora import bootstrap


PASSWORD = "SyntheticAdmin2026!"
ENV = {
    "WEKNORA_BASE_URL": "http://weknora-app:8080",
    "WEKNORA_BOOTSTRAP_BASE_URL": "http://weknora-app-init:8080",
    "WEKNORA_ADMIN_USER": "admin",
    "WEKNORA_ADMIN_EMAIL": "admin@example.com",
    "WEKNORA_ADMIN_PASSWORD": PASSWORD,
}


class AccountApi:
    def __init__(self):
        self.exists = False
        self.admin = False
        self.mode = "self_serve"
        self.calls = []
        self.register_count = 0
        self.password = PASSWORD

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/api/v1/auth/config":
            return httpx.Response(200, json={"registration_mode": self.mode})
        if path == "/api/v1/auth/login":
            if not self.exists or body["password"] != self.password:
                return httpx.Response(401, json={"success": False})
            return httpx.Response(200, json={
                "success": True, "token": "synthetic-login-token",
                "user": {"email": ENV["WEKNORA_ADMIN_EMAIL"], "is_system_admin": self.admin},
            })
        if path == "/api/v1/auth/register":
            self.register_count += 1
            if self.exists:
                return httpx.Response(409, json={"success": False})
            self.exists = True
            assert body == {"username": "admin", "email": ENV["WEKNORA_ADMIN_EMAIL"], "password": PASSWORD}
            return httpx.Response(201, json={"success": True, "user": {"email": body["email"]}})
        if path == "/api/v1/system/admin/settings/auth.registration_mode":
            assert request.method == "PUT"
            assert request.headers["Authorization"] == "Bearer synthetic-login-token"
            assert body == {"value": "invite_only"}
            self.mode = body["value"]
            return httpx.Response(200, json={"key": "auth.registration_mode", "value": self.mode})
        raise AssertionError(f"Account bootstrap attempted unexpected API: {path}")


def test_account_initialization_and_verification_need_no_model_inputs():
    api = AccountApi()
    config = bootstrap.build_config(ENV)
    transport = httpx.MockTransport(api)
    asyncio.run(bootstrap.initialize(config, transport=transport))
    asyncio.run(bootstrap.initialize(config, transport=transport))
    assert api.register_count == 1
    api.admin = True  # The formal app's upstream startup hook promotes this account.
    asyncio.run(bootstrap.verify(bootstrap.build_config(ENV, for_verify=True), transport=transport))
    assert api.mode == "invite_only"
    assert PASSWORD not in repr(config)
    assert all(path.startswith("/api/v1/auth/") or path.startswith("/api/v1/system/admin/") for _, path in api.calls)


def test_existing_wrong_password_is_never_reset():
    api = AccountApi()
    api.exists = True
    api.password = "DifferentExisting2026!"
    with pytest.raises(bootstrap.BootstrapError, match="LOGIN_FAILED"):
        asyncio.run(bootstrap.initialize(bootstrap.build_config(ENV), transport=httpx.MockTransport(api)))
    assert api.password == "DifferentExisting2026!"
    assert not any("reset" in path for _, path in api.calls)


@pytest.mark.parametrize("admin", [False, True])
def test_verify_requires_system_admin_and_never_registers(admin):
    api = AccountApi()
    api.exists = True
    api.admin = admin
    config = bootstrap.build_config(ENV, for_verify=True)
    if admin:
        asyncio.run(bootstrap.verify(config, transport=httpx.MockTransport(api)))
    else:
        with pytest.raises(bootstrap.BootstrapError, match="NOT_SYSTEM_ADMIN"):
            asyncio.run(bootstrap.verify(config, transport=httpx.MockTransport(api)))
    assert api.register_count == 0
    assert api.mode == ("invite_only" if admin else "self_serve")


@pytest.mark.parametrize("override", [
    {"WEKNORA_ADMIN_PASSWORD": "short"},
    {"WEKNORA_ADMIN_EMAIL": "invalid"},
    {"WEKNORA_BASE_URL": "http://user:password@weknora-app:8080"},
    {"WEKNORA_TIMEOUT_SECONDS": "2"},
])
def test_invalid_account_configuration_fails_with_safe_code(override):
    with pytest.raises(bootstrap.BootstrapError) as error:
        bootstrap.build_config({**ENV, **override})
    assert str(error.value) == "WEKNORA_BOOTSTRAP_CONFIG_INVALID"
    assert PASSWORD not in str(error.value)


def test_cli_does_not_log_password_token_or_http_error_body(monkeypatch, capsys):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
    def unavailable(request):
        return httpx.Response(500, text=f"{PASSWORD} synthetic-login-token")
    assert bootstrap.main([], transport=httpx.MockTransport(unavailable)) == 1
    output = capsys.readouterr()
    assert "WEKNORA_BOOTSTRAP_HTTP_FAILED" in output.err
    assert PASSWORD not in output.out + output.err
    assert "synthetic-login-token" not in output.out + output.err


def test_account_initialization_refuses_registration_when_closed():
    api = AccountApi()
    api.mode = "invite_only"
    with pytest.raises(bootstrap.BootstrapError, match="REGISTRATION_CLOSED"):
        asyncio.run(bootstrap.initialize(bootstrap.build_config(ENV), transport=httpx.MockTransport(api)))
    assert api.register_count == 0
