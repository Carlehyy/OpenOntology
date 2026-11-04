"""Initialize only the native administrator; knowledge configuration belongs to Web UI."""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
import os
import re
import sys
from typing import Mapping
from urllib.parse import urlsplit

import httpx


class BootstrapError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class BootstrapConfig:
    base_url: str
    admin_username: str
    admin_email: str
    admin_password: str = field(repr=False)
    timeout_seconds: int = 15
    bootstrap_base_url: str | None = None


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    parsed.port
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or any(char.isspace() for char in value)):
        raise ValueError("invalid origin")
    return value.rstrip("/")


def build_config(env: Mapping[str, str], *, for_verify: bool = False) -> BootstrapConfig:
    required = ["WEKNORA_BASE_URL", "WEKNORA_ADMIN_USER", "WEKNORA_ADMIN_EMAIL", "WEKNORA_ADMIN_PASSWORD"]
    if not for_verify:
        required.append("WEKNORA_BOOTSTRAP_BASE_URL")
    if any(not env.get(key, "").strip() for key in required):
        raise BootstrapError("WEKNORA_BOOTSTRAP_CONFIG_MISSING")
    try:
        username = env["WEKNORA_ADMIN_USER"].strip()
        email = env["WEKNORA_ADMIN_EMAIL"].strip()
        password = env["WEKNORA_ADMIN_PASSWORD"]
        timeout = int(env.get("WEKNORA_TIMEOUT_SECONDS") or "15")
        if (not 2 <= len(username) <= 50 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
                or not 8 <= len(password) <= 32 or not re.search(r"[A-Za-z]", password)
                or not re.search(r"[0-9]", password) or not 3 <= timeout <= 60
                or any(char.isspace() or not char.isprintable() for char in username + password)):
            raise ValueError("invalid account")
        return BootstrapConfig(
            base_url=_origin(env["WEKNORA_BASE_URL"]),
            bootstrap_base_url=None if for_verify else _origin(env["WEKNORA_BOOTSTRAP_BASE_URL"]),
            admin_username=username, admin_email=email, admin_password=password,
            timeout_seconds=timeout,
        )
    except ValueError:
        raise BootstrapError("WEKNORA_BOOTSTRAP_CONFIG_INVALID") from None


def _request(client: httpx.Client, method: str, path: str, *, body=None, token=None,
             expected=(200,)) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        response = client.request(method, f"/api/v1{path}", json=body, headers=headers)
    except httpx.HTTPError:
        raise BootstrapError("WEKNORA_BOOTSTRAP_HTTP_UNAVAILABLE") from None
    if response.status_code == 401 and path == "/auth/login":
        return {}
    if response.status_code not in expected:
        raise BootstrapError("WEKNORA_BOOTSTRAP_HTTP_FAILED")
    try:
        payload = response.json()
    except ValueError:
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_RESPONSE") from None
    if not isinstance(payload, dict):
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_RESPONSE")
    return payload


def _login(client: httpx.Client, config: BootstrapConfig) -> dict:
    payload = _request(client, "POST", "/auth/login", body={
        "email": config.admin_email, "password": config.admin_password,
    })
    if not payload:
        return {}
    if (payload.get("success") is not True or not isinstance(payload.get("token"), str)
            or not payload["token"] or not isinstance(payload.get("user"), dict)):
        raise BootstrapError("WEKNORA_BOOTSTRAP_INVALID_RESPONSE")
    return payload


def _client(config, base_url, transport):
    return httpx.Client(base_url=base_url, timeout=config.timeout_seconds,
                        follow_redirects=False, trust_env=False, transport=transport)


async def initialize(config: BootstrapConfig, *, transport=None) -> None:
    if config.bootstrap_base_url is None:
        raise BootstrapError("WEKNORA_BOOTSTRAP_CONFIG_INVALID")
    with _client(config, config.bootstrap_base_url, transport) as client:
        if _login(client, config):
            return
        mode = _request(client, "GET", "/auth/config").get("registration_mode")
        if mode != "self_serve":
            raise BootstrapError("WEKNORA_BOOTSTRAP_REGISTRATION_CLOSED")
        # Existing accounts are never reset; a mismatched password must fail.
        _request(client, "POST", "/auth/register", expected=(201, 400, 409), body={
            "username": config.admin_username, "email": config.admin_email,
            "password": config.admin_password,
        })
        if not _login(client, config):
            raise BootstrapError("WEKNORA_BOOTSTRAP_LOGIN_FAILED")


async def verify(config: BootstrapConfig, *, transport=None) -> None:
    with _client(config, config.base_url, transport) as client:
        login = _login(client, config)
        if not login:
            raise BootstrapError("WEKNORA_BOOTSTRAP_LOGIN_FAILED")
        if login["user"].get("is_system_admin") is not True:
            raise BootstrapError("WEKNORA_BOOTSTRAP_NOT_SYSTEM_ADMIN")
        result = _request(client, "PUT", "/system/admin/settings/auth.registration_mode",
                          token=login["token"], body={"value": "invite_only"})
        if result.get("key") != "auth.registration_mode" or result.get("value") != "invite_only":
            raise BootstrapError("WEKNORA_BOOTSTRAP_REGISTRATION_NOT_CLOSED")
        if _request(client, "GET", "/auth/config").get("registration_mode") != "invite_only":
            raise BootstrapError("WEKNORA_BOOTSTRAP_REGISTRATION_NOT_CLOSED")


def main(argv=None, *, transport=None) -> int:
    parser = argparse.ArgumentParser(description="Initialize or verify the native WeKnora administrator")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = build_config(os.environ, for_verify=args.verify)
        asyncio.run((verify if args.verify else initialize)(config, transport=transport))
        print("WEKNORA_BOOTSTRAP_VERIFIED" if args.verify else "WEKNORA_BOOTSTRAP_READY")
        return 0
    except BootstrapError as exc:
        print(f"WEKNORA_BOOTSTRAP_ERROR {exc.code}", file=sys.stderr)
    except Exception:
        # HTTP libraries can embed credentials in exceptions; never print them.
        print("WEKNORA_BOOTSTRAP_ERROR WEKNORA_BOOTSTRAP_INTERNAL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
