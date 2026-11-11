"""Kaneo service canary: synthetic workspace/project/task fixtures over the official HTTP API.

Upstream contract (Kaneo 2.49.0 snapshot, confirmed against the pinned source;
base path is ``/api`` from ``app.route("/api", api)`` in ``apps/api/src/index.ts``):

- ``GET  /api/health``           public static probe → ``{"status": "ok"}``;
- ``GET  /api/instance/status``  public, DB-backed → ``{"hasUsers", "hasAdmin"}``;
- ``POST /api/auth/sign-in/email`` ``{email, password}`` → ``{user, session,
  token}``; the bearer token authenticates API routes
  (``authenticate-api-request.ts`` accepts session bearer tokens);
- ``GET  /api/auth/get-session`` with ``Authorization: Bearer`` → ``{session,
  user}``; ``user.role`` is a comma-separated instance role list;
- ``POST /api/auth/sign-up/email`` → 403 ``REGISTRATION_NOT_ALLOWED`` (or
  ``PASSWORD_REGISTRATION_DISABLED``) once the instance has users and
  registration is closed (``auth.ts`` database-user ``create.before`` hook);
- ``POST /api/auth/organization/create`` ``{name, slug, ...}`` creates a
  workspace: the better-auth organization plugin maps ``organization`` onto
  the ``workspace`` table (``modelName: "workspace"`` in ``auth.ts``);
- ``GET  /api/workspace``                       → workspace array;
- ``POST /api/project`` ``{name, workspaceId, icon, slug}`` → project
  (slug is the task ticket prefix; default column slugs seeded per project
  are ``to-do``/``in-progress``/``in-review``/``done``);
- ``GET  /api/project?workspaceId=...``         → project array;
- ``POST /api/task/{projectId}`` ``{title, description, priority, status}``
  → task; priorities are ``no-priority|low|medium|high|urgent`` and
  ``status`` is the target column slug;
- ``GET  /api/task/{id}`` / ``PUT /api/task/{id}`` ``{title, description,
  priority, status, projectId, position}`` → task detail / updated task;
- every authenticated route without credentials → 401 ``Unauthorized``.

Runs in two stages so the main thread can restart the stack in between and
prove DB persistence: ``--create`` builds the fixtures and records their IDs
(``KANEO_E2E_FIXTURES``, by default next to the report); ``--verify-persisted``
re-reads them after the restart. Fixtures are intentionally left in place for
the persistence stage and belong to the disposable canary.

Not covered (do not claim): attachment/S3 upload paths (optional S3 is not
configured) and the gateway-level HTTP→HTTPS redirect (outside the private
API base this script targets).

Credentials come only from the process environment (KANEO_ADMIN_* plus
KANEO_BASE_URL/KANEO_WEB_URL, validated by the shared bootstrap module);
tokens, passwords and cookies are never printed or written to artifacts.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import sys
from typing import Mapping

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.super_assistant.kaneo import bootstrap

REPORT_ENV = "KANEO_E2E_REPORT"
REPORT_DEFAULT = "/canary/report.json"
FIXTURES_ENV = "KANEO_E2E_FIXTURES"
FIXTURES_NAME = "kaneo-e2e-fixtures.json"

TASK_PRIORITY_CREATED = "medium"
TASK_PRIORITY_UPDATED = "high"
TASK_STATUS_CREATED = "to-do"
TASK_STATUS_UPDATED = "in-progress"


class E2EError(RuntimeError):
    """Failure identified by a safe UPPER_SNAKE code; messages stay internal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _require_opt_in(env: Mapping[str, str]) -> None:
    if env.get("ENVIRONMENT") != "test" or env.get("OPENONTOLOGY_KANEO_E2E") != "1":
        raise E2EError("KANEO_E2E_NOT_OPTED_IN")


def _build_config() -> bootstrap.BootstrapConfig:
    try:
        return bootstrap.build_config(os.environ)
    except bootstrap.BootstrapError as exc:
        raise E2EError(f"KANEO_E2E_CONFIG_{exc.code}") from None


def _client(config: bootstrap.BootstrapConfig) -> httpx.Client:
    # Private-network first-party client: no proxy env, no redirect following
    # (the HTTP→HTTPS hop belongs to the public gateway, not this base URL).
    return httpx.Client(base_url=config.base_url, timeout=config.timeout_seconds,
                        follow_redirects=False, trust_env=False)


def _request(client: httpx.Client, method: str, path: str, *, config=None,
             body=None, token=None, expected=(200,)) -> httpx.Response:
    headers: dict[str, str] = {}
    if method != "GET":
        # Same private better-auth POST semantics as the bootstrap: pin Origin
        # to the deployment's public web origin (KANEO_CLIENT_URL trusted).
        headers["Origin"] = config.web_url
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        response = client.request(method, path, json=body, headers=headers)
    except httpx.HTTPError:
        raise E2EError("KANEO_E2E_HTTP_UNAVAILABLE") from None
    if response.status_code not in expected:
        raise E2EError(f"KANEO_E2E_HTTP_{response.status_code}")
    return response


def _json(response: httpx.Response, stage: str) -> dict:
    try:
        payload = response.json()
    except ValueError:
        raise E2EError("KANEO_E2E_INVALID_RESPONSE") from None
    if not isinstance(payload, dict):
        raise E2EError("KANEO_E2E_INVALID_RESPONSE")
    return payload


def _json_list(response: httpx.Response) -> list:
    try:
        payload = response.json()
    except ValueError:
        raise E2EError("KANEO_E2E_INVALID_RESPONSE") from None
    if not isinstance(payload, list):
        raise E2EError("KANEO_E2E_INVALID_RESPONSE")
    return payload


def _check_health(client: httpx.Client, report: dict) -> None:
    response = _request(client, "GET", "/api/health")
    if _json(response, "health").get("status") != "ok":
        raise E2EError("KANEO_E2E_INVALID_RESPONSE")
    report["health_static_ok"] = True
    status = _json(_request(client, "GET", "/api/instance/status"), "instance_status")
    has_users = status.get("hasUsers")
    has_admin = status.get("hasAdmin")
    if not isinstance(has_users, bool) or not isinstance(has_admin, bool):
        raise E2EError("KANEO_E2E_INVALID_RESPONSE")
    report["instance_has_users"] = has_users
    report["instance_has_admin"] = has_admin
    # The registration probe below must never run against a user-less instance:
    # there the first sign-up is legal and would create a real account.
    if not has_users:
        raise E2EError("KANEO_E2E_INSTANCE_EMPTY")


def _check_unauthenticated_read(client: httpx.Client, report: dict,
                                path: str, key: str) -> None:
    response = _request(client, "GET", path, expected=(401,))
    report[key] = response.status_code == 401


def _sign_in_admin(client: httpx.Client, config: bootstrap.BootstrapConfig,
                   report: dict) -> str:
    login = _json(_request(client, "POST", "/api/auth/sign-in/email",
                           config=config, body={
                               "email": config.admin_email,
                               "password": config.admin_password,
                           }), "sign_in")
    token = login.get("token")
    if not isinstance(token, str) or not token or not isinstance(login.get("user"), dict):
        raise E2EError("KANEO_E2E_LOGIN_FAILED")
    session = _json(_request(client, "GET", "/api/auth/get-session", token=token),
                    "get_session")
    user = session.get("user")
    role = user.get("role") if isinstance(user, dict) else None
    # Mirror upstream hasInstanceAdminRole: comma-separated instance roles.
    report["admin_session_verified"] = isinstance(role, str) and "admin" in role.split(",")
    if not report["admin_session_verified"]:
        raise E2EError("KANEO_E2E_NOT_ADMIN")
    return token


def _check_registration_closed(client: httpx.Client, config: bootstrap.BootstrapConfig,
                               report: dict, suffix: str) -> None:
    response = _request(client, "POST", "/api/auth/sign-up/email", config=config, body={
        "name": f"OO E2E Closed Probe {suffix}",
        "email": f"kaneo-e2e-closed-{suffix}@example.invalid",
        # Random single-use secret; never stored or printed.
        "password": secrets.token_urlsafe(24),
    }, expected=(403,))
    report["registration_closed_rejected"] = response.status_code == 403
    try:
        code = response.json().get("code")
    except ValueError:
        code = None
    if isinstance(code, str) and code:
        report["registration_rejection_code"] = code


def _create_workspace(client: httpx.Client, config: bootstrap.BootstrapConfig,
                      token: str, suffix: str, report: dict) -> dict:
    name = f"OO E2E Workspace {suffix}"
    slug = f"oo-e2e-ws-{suffix}"
    created = _json(_request(client, "POST", "/api/auth/organization/create",
                             config=config, token=token, body={
                                 "name": name, "slug": slug,
                                 "keepCurrentActiveOrganization": True,
                             }), "create_workspace")
    organization = created.get("organization")
    created_id = (organization or {}).get("id") if isinstance(organization, dict) \
        else created.get("id")
    workspaces = _json_list(_request(client, "GET", "/api/workspace", token=token))
    matches = [item for item in workspaces
               if isinstance(item, dict) and item.get("slug") == slug]
    if not matches:
        matches = [item for item in workspaces
                   if isinstance(item, dict) and item.get("name") == name]
    if len(matches) != 1 or not isinstance(matches[0].get("id"), str):
        raise E2EError("KANEO_E2E_WORKSPACE_NOT_LISTED")
    workspace = matches[0]
    if created_id is not None and workspace["id"] != created_id:
        raise E2EError("KANEO_E2E_INVALID_RESPONSE")
    report["workspace_created"] = True
    report["workspace_role"] = workspace.get("role")
    return {"id": workspace["id"], "name": name, "slug": slug}


def _create_project(client: httpx.Client, config: bootstrap.BootstrapConfig,
                    token: str, workspace_id: str, suffix: str,
                    report: dict) -> dict:
    name = f"OO E2E Project {suffix}"
    slug = f"ooe2e-{suffix}"
    project = _json(_request(client, "POST", "/api/project", config=config,
                             token=token, body={
                                 "name": name, "workspaceId": workspace_id,
                                 "icon": "box", "slug": slug,
                             }), "create_project")
    if (not isinstance(project.get("id"), str) or project.get("slug") != slug
            or project.get("name") != name
            or project.get("workspaceId") != workspace_id):
        raise E2EError("KANEO_E2E_INVALID_RESPONSE")
    report["project_created"] = True
    listed = _json_list(_request(
        client, "GET", f"/api/project?workspaceId={workspace_id}", token=token))
    report["project_read_back"] = any(
        isinstance(item, dict) and item.get("id") == project["id"] for item in listed)
    if not report["project_read_back"]:
        raise E2EError("KANEO_E2E_PROJECT_NOT_LISTED")
    return {"id": project["id"], "name": name, "slug": slug,
            "workspace_id": workspace_id}


def _assert_task(task: dict, *, title: str, priority: str, status: str,
                 project_id: str) -> None:
    if (not isinstance(task.get("id"), str) or task.get("title") != title
            or task.get("priority") != priority or task.get("status") != status
            or task.get("projectId") != project_id):
        raise E2EError("KANEO_E2E_INVALID_RESPONSE")


def _create_and_update_task(client: httpx.Client, config: bootstrap.BootstrapConfig,
                            token: str, project: dict, suffix: str,
                            report: dict) -> dict:
    title = f"OO E2E Task {suffix}"
    updated_title = f"{title} updated"
    task = _json(_request(client, "POST", f"/api/task/{project['id']}",
                          config=config, token=token, body={
                              "title": title,
                              "description": "synthetic e2e fixture",
                              "priority": TASK_PRIORITY_CREATED,
                              "status": TASK_STATUS_CREATED,
                          }), "create_task")
    _assert_task(task, title=title, priority=TASK_PRIORITY_CREATED,
                 status=TASK_STATUS_CREATED, project_id=project["id"])
    report["task_created"] = True
    position = task.get("position")
    updated = _json(_request(client, "PUT", f"/api/task/{task['id']}",
                             config=config, token=token, body={
                                 "title": updated_title,
                                 "description": "synthetic e2e fixture updated",
                                 "priority": TASK_PRIORITY_UPDATED,
                                 "status": TASK_STATUS_UPDATED,
                                 "projectId": project["id"],
                                 "position": position if isinstance(position, int)
                                 and position >= 0 else 0,
                             }), "update_task")
    _assert_task(updated, title=updated_title, priority=TASK_PRIORITY_UPDATED,
                 status=TASK_STATUS_UPDATED, project_id=project["id"])
    report["task_updated"] = True
    detail = _json(_request(client, "GET", f"/api/task/{task['id']}", token=token),
                   "read_task")
    _assert_task(detail, title=updated_title, priority=TASK_PRIORITY_UPDATED,
                 status=TASK_STATUS_UPDATED, project_id=project["id"])
    report["task_read_back"] = True
    return {"id": task["id"], "title": updated_title,
            "priority": TASK_PRIORITY_UPDATED, "status": TASK_STATUS_UPDATED,
            "project_id": project["id"]}


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    path.chmod(0o600)


def _load_fixtures(path: Path) -> dict:
    try:
        fixtures = json.loads(path.read_text())
    except (OSError, ValueError):
        raise E2EError("KANEO_E2E_FIXTURES_UNREADABLE") from None
    if not isinstance(fixtures, dict) or not isinstance(fixtures.get("workspace"), dict) \
            or not isinstance(fixtures.get("project"), dict) \
            or not isinstance(fixtures.get("task"), dict):
        raise E2EError("KANEO_E2E_FIXTURES_UNREADABLE")
    return fixtures


def _target(env: Mapping[str, str]) -> tuple[Path, Path]:
    report = Path(env.get(REPORT_ENV, "") or REPORT_DEFAULT)
    fixtures = Path(env.get(FIXTURES_ENV, "") or report.parent / FIXTURES_NAME)
    return report, fixtures


def _stage_create(config: bootstrap.BootstrapConfig, report: dict,
                  fixtures_path: Path) -> None:
    suffix = secrets.token_hex(3)
    with _client(config) as client:
        _check_health(client, report)
        _check_unauthenticated_read(client, report, "/api/workspace",
                                    "unauthenticated_workspace_read_rejected")
        token = _sign_in_admin(client, config, report)
        _check_registration_closed(client, config, report, suffix)
        workspace = _create_workspace(client, config, token, suffix, report)
        project = _create_project(client, config, token, workspace["id"], suffix, report)
        task = _create_and_update_task(client, config, token, project, suffix, report)
        _check_unauthenticated_read(client, report, f"/api/task/{task['id']}",
                                    "unauthenticated_task_read_rejected")
    if not all((report.get("unauthenticated_workspace_read_rejected"),
                report.get("unauthenticated_task_read_rejected"),
                report.get("registration_closed_rejected"))):
        raise E2EError("KANEO_E2E_ACCESS_CONTROL_FAILED")
    _write_json(fixtures_path, {
        "version": 1, "base_url": config.base_url,
        "workspace": workspace, "project": project, "task": task,
    })
    report["fixture_ids_recorded"] = True
    report["workspace_id"] = workspace["id"]
    report["project_id"] = project["id"]
    report["task_id"] = task["id"]


def _stage_verify_persisted(config: bootstrap.BootstrapConfig, report: dict,
                            fixtures_path: Path) -> None:
    fixtures = _load_fixtures(fixtures_path)
    workspace = fixtures["workspace"]
    project = fixtures["project"]
    task = fixtures["task"]
    with _client(config) as client:
        _check_health(client, report)
        _check_unauthenticated_read(client, report, "/api/workspace",
                                    "unauthenticated_workspace_read_rejected")
        token = _sign_in_admin(client, config, report)
        workspaces = _json_list(_request(client, "GET", "/api/workspace", token=token))
        persisted_workspace = next((item for item in workspaces if isinstance(item, dict)
                                    and item.get("id") == workspace["id"]), None)
        if persisted_workspace is None or persisted_workspace.get("slug") != workspace["slug"]:
            raise E2EError("KANEO_E2E_WORKSPACE_NOT_PERSISTED")
        report["workspace_persisted"] = True
        listed = _json_list(_request(
            client, "GET", f"/api/project?workspaceId={workspace['id']}", token=token))
        persisted_project = next((item for item in listed if isinstance(item, dict)
                                  and item.get("id") == project["id"]), None)
        if persisted_project is None or persisted_project.get("slug") != project["slug"]:
            raise E2EError("KANEO_E2E_PROJECT_NOT_PERSISTED")
        report["project_persisted"] = True
        detail = _json(_request(client, "GET", f"/api/task/{task['id']}", token=token),
                       "read_task")
        _assert_task(detail, title=task["title"], priority=task["priority"],
                     status=task["status"], project_id=task["project_id"])
        report["task_persisted"] = True
        report["db_persistence_verified"] = True
    report["workspace_id"] = workspace["id"]
    report["project_id"] = project["id"]
    report["task_id"] = task["id"]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Synthetic Kaneo service canary over the official HTTP API")
    stage = parser.add_mutually_exclusive_group(required=True)
    stage.add_argument("--create", action="store_true",
                       help="create fixtures and record their IDs for the persistence stage")
    stage.add_argument("--verify-persisted", action="store_true",
                       help="re-read recorded fixtures (run after a stack restart)")
    args = parser.parse_args(argv)

    report: dict = {"result": "fail", "mode":
                    "create" if args.create else "verify-persisted",
                    "synthetic_only": True,
                    "not_covered": ["attachment_upload (optional S3 not configured)",
                                    "gateway_http_to_https_redirect (outside private API base)"]}
    status = 1
    try:
        _require_opt_in(os.environ)
        config = _build_config()
        report["base_url"] = config.base_url
        report_path, fixtures_path = _target(os.environ)
        if args.create:
            _stage_create(config, report, fixtures_path)
        else:
            _stage_verify_persisted(config, report, fixtures_path)
        report["result"] = "pass"
        status = 0
    except E2EError as exc:
        report["error_code"] = exc.code
    except Exception:
        # httpx/app exceptions can embed credentials; never surface messages.
        report["error_type"] = "internal"
    try:
        report_path, _ = _target(os.environ)
        _write_json(report_path, report)
    except OSError:
        print("KANEO_E2E_ERROR KANEO_E2E_REPORT_UNWRITABLE", file=sys.stderr)
    print(json.dumps(report))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
