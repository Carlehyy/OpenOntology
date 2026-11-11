"""Initialize only the native Kaneo administrator; projects/tasks belong to Web UI.

Upstream contract (Kaneo 2.49.0, better-auth 1.7.14) — verified against the
pinned snapshot and a live canary:

- ``GET  /api/instance/status``  public and DB-backed → ``{"hasUsers": bool,
  "hasAdmin": bool}`` (``/api/health`` is a static 200 and proves nothing);
- ``POST /api/auth/sign-up/email`` ``{name, email, password}`` → 200
  ``{token, user}``; the first non-anonymous signup is auto-promoted to
  instance admin by the upstream after-create hook, but the sign-up response
  still reports the pre-promotion role;
- ``POST /api/auth/sign-in/email`` ``{email, password}`` → 200
  ``{user, session, token}`` (bearer plugin token), 401 on bad credentials;
- ``GET  /api/auth/get-session`` with ``Authorization: Bearer <token>`` →
  200 ``{session, user}`` where ``user.role`` is a comma-separated instance
  role list; ``admin`` membership is the administrator proof;
- ``GET  /api/admin/settings`` with the admin bearer token → 200 instance
  settings whose ``registrationEnabled`` must be false for a verified
  deployment (locked by ``DISABLE_REGISTRATION`` or the admin Web).

better-auth's origin check only applies to cookie-bearing requests. This
bootstrap never sends cookies — authenticated calls use the bearer token —
and still pins ``Origin`` to the deployment's public web origin (which must
match Kaneo's ``KANEO_CLIENT_URL`` trusted origin) on every POST.

Credentials live only in the deployment bootstrap service environment; they
must never be added to platform runtime settings. Existing accounts are
never reset: sign-up is attempted only on a user-less instance, and a
mismatched password fails with ``KANEO_BOOTSTRAP_LOGIN_FAILED``.
"""
from __future__ import annotations

import argparse
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
    web_url: str
    admin_username: str
    admin_email: str
    admin_password: str = field(repr=False)
    timeout_seconds: int = 15


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    parsed.port
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or any(char.isspace() for char in value)):
        raise ValueError("invalid origin")
    return value.rstrip("/")


def build_config(env: Mapping[str, str]) -> BootstrapConfig:
    required = [
        "KANEO_BASE_URL", "KANEO_WEB_URL", "KANEO_ADMIN_USER",
        "KANEO_ADMIN_EMAIL", "KANEO_ADMIN_PASSWORD",
    ]
    if any(not env.get(key, "").strip() for key in required):
        raise BootstrapError("KANEO_BOOTSTRAP_CONFIG_MISSING")
    try:
        username = env["KANEO_ADMIN_USER"].strip()
        email = env["KANEO_ADMIN_EMAIL"].strip()
        password = env["KANEO_ADMIN_PASSWORD"]
        timeout = int(env.get("KANEO_TIMEOUT_SECONDS") or "15")
        # better-auth 1.7.14 defaults: password 8..128 chars; Kaneo's "name"
        # is free display text but the bootstrap account keeps it bounded.
        if (not 2 <= len(username) <= 50
                or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
                or not 8 <= len(password) <= 128
                or not re.search(r"[A-Za-z]", password)
                or not re.search(r"[0-9]", password) or not 3 <= timeout <= 60
                or any(char.isspace() or not char.isprintable() for char in username + password)):
            raise ValueError("invalid account")
        return BootstrapConfig(
            base_url=_origin(env["KANEO_BASE_URL"]),
            web_url=_origin(env["KANEO_WEB_URL"]),
            admin_username=username, admin_email=email, admin_password=password,
            timeout_seconds=timeout,
        )
    except ValueError:
        raise BootstrapError("KANEO_BOOTSTRAP_CONFIG_INVALID") from None


def _client(config: BootstrapConfig, transport) -> httpx.Client:
    return httpx.Client(base_url=config.base_url, timeout=config.timeout_seconds,
                        follow_redirects=False, trust_env=False, transport=transport)


SIGNUP_STATUS_CODES = {
    403: "KANEO_BOOTSTRAP_REGISTRATION_CLOSED",
    400: "KANEO_BOOTSTRAP_SIGNUP_REJECTED",
    409: "KANEO_BOOTSTRAP_SIGNUP_REJECTED",
    422: "KANEO_BOOTSTRAP_SIGNUP_REJECTED",
}


def _request(client: httpx.Client, method: str, path: str, *, config=None,
             body=None, token=None, expected=(200,),
             status_codes: Mapping[int, str] | None = None) -> dict | None:
    headers: dict[str, str] = {}
    if method != "GET":
        # First-party browser semantics for private better-auth POSTs; the
        # value must equal Kaneo's trusted KANEO_CLIENT_URL origin.
        headers["Origin"] = config.web_url
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = client.request(method, path, json=body, headers=headers)
    except httpx.HTTPError:
        raise BootstrapError("KANEO_BOOTSTRAP_HTTP_UNAVAILABLE") from None
    if response.status_code == 401:
        return None
    if response.status_code not in expected:
        raise BootstrapError(
            (status_codes or {}).get(
                response.status_code, "KANEO_BOOTSTRAP_HTTP_FAILED",
            )
        )
    try:
        payload = response.json()
    except ValueError:
        raise BootstrapError("KANEO_BOOTSTRAP_INVALID_RESPONSE") from None
    if payload is not None and not isinstance(payload, dict):
        raise BootstrapError("KANEO_BOOTSTRAP_INVALID_RESPONSE")
    return payload


def _sign_in(client: httpx.Client, config: BootstrapConfig) -> dict:
    payload = _request(client, "POST", "/api/auth/sign-in/email", config=config, body={
        "email": config.admin_email, "password": config.admin_password,
    })
    if payload is None:
        return {}
    if (not isinstance(payload.get("token"), str) or not payload["token"]
            or not isinstance(payload.get("user"), dict)):
        raise BootstrapError("KANEO_BOOTSTRAP_INVALID_RESPONSE")
    return payload


def _instance_has_users(client: httpx.Client) -> bool:
    payload = _request(client, "GET", "/api/instance/status")
    if not isinstance(payload, dict) or not isinstance(payload.get("hasUsers"), bool):
        raise BootstrapError("KANEO_BOOTSTRAP_INVALID_RESPONSE")
    return payload["hasUsers"]


def _is_admin(role: object) -> bool:
    # Mirror upstream hasInstanceAdminRole: user.role is a comma-separated
    # instance role list; membership of the built-in "admin" role is the proof.
    return isinstance(role, str) and "admin" in role.split(",")


def initialize(config: BootstrapConfig, *, transport=None) -> None:
    with _client(config, transport) as client:
        if _sign_in(client, config):
            return
        # Only a user-less instance accepts the bootstrap signup (and only
        # that signup is auto-promoted to instance admin by upstream). An
        # instance with existing users whose bootstrap credentials fail to
        # sign in is a deployment mismatch: fail without touching accounts.
        if _instance_has_users(client):
            raise BootstrapError("KANEO_BOOTSTRAP_LOGIN_FAILED")
        _request(client, "POST", "/api/auth/sign-up/email", config=config,
                 status_codes=SIGNUP_STATUS_CODES, body={
                     "name": config.admin_username,
                     "email": config.admin_email,
                     "password": config.admin_password,
                 })
        if not _sign_in(client, config):
            raise BootstrapError("KANEO_BOOTSTRAP_LOGIN_FAILED")


def verify(config: BootstrapConfig, *, transport=None) -> None:
    with _client(config, transport) as client:
        login = _sign_in(client, config)
        if not login:
            raise BootstrapError("KANEO_BOOTSTRAP_LOGIN_FAILED")
        session = _request(client, "GET", "/api/auth/get-session",
                           token=login["token"])
        if (not isinstance(session, dict)
                or not isinstance(session.get("session"), dict)
                or not isinstance(session.get("user"), dict)):
            raise BootstrapError("KANEO_BOOTSTRAP_INVALID_RESPONSE")
        if not _is_admin(session["user"].get("role")):
            raise BootstrapError("KANEO_BOOTSTRAP_NOT_ADMIN")
        settings = _request(client, "GET", "/api/admin/settings",
                            token=login["token"])
        if not isinstance(settings, dict) or not isinstance(
            settings.get("registrationEnabled"), bool,
        ):
            raise BootstrapError("KANEO_BOOTSTRAP_INVALID_RESPONSE")
        if settings["registrationEnabled"]:
            raise BootstrapError("KANEO_BOOTSTRAP_REGISTRATION_OPEN")


def main(argv=None, *, transport=None) -> int:
    parser = argparse.ArgumentParser(
        description="Initialize or verify the native Kaneo administrator",
    )
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = build_config(os.environ)
        (verify if args.verify else initialize)(config, transport=transport)
        print("KANEO_BOOTSTRAP_VERIFIED" if args.verify else "KANEO_BOOTSTRAP_READY")
        return 0
    except BootstrapError as exc:
        print(f"KANEO_BOOTSTRAP_ERROR {exc.code}", file=sys.stderr)
    except Exception:
        # HTTP libraries can embed credentials in exceptions; never print them.
        print("KANEO_BOOTSTRAP_ERROR KANEO_BOOTSTRAP_INTERNAL", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
