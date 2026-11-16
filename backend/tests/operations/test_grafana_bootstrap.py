"""Grafana bootstrap（compose grafana-bootstrap 服务）入口测试。

桩上游按固定镜像 PoC 实测 API 面回答；断言：有界等待、健康+共享管理员+
文件夹 ensure、``--verify`` 只读、稳定错误码 + 非零 exit、重复执行不重置
密码（无任何密码/用户写端点被调用）、输出不回显凭据/上游响应体。

t19 追加窄环境独立性与**真实子进程**回归：bootstrap 只读六个 GRAFANA_*
环境变量、显式传参构造客户端、不导入 app.shared.config——净化环境
（无 ENVIRONMENT/DATABASE_URL/REDIS_URL/N8N/GRAFANA_*、独立空 cwd）的
独立 python 子进程 ``python -m app.operations.grafana.bootstrap``：
``--help`` exit 0、缺密码稳定 GRAFANA_NOT_CONFIGURED 无全局依赖 traceback、
非法 timeout 稳定 GRAFANA_CONFIG_INVALID、真实配置在仅绑定 127.0.0.1 的
本地桩上完整 bootstrap exit 0、错误密码稳定 GRAFANA_UNREACHABLE 不回显
原始异常（进程真实执行，非进程内导入）。
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from app.operations.grafana import bootstrap

STUB_PASSWORD = "StubGrafanaBootstrap2026!"
STUB_BASIC = "Basic " + base64.b64encode(f"admin:{STUB_PASSWORD}".encode()).decode()

GRAFANA_ENV_KEYS = (
    "GRAFANA_BASE_URL",
    "GRAFANA_ADMIN_USER",
    "GRAFANA_ADMIN_PASSWORD",
    "GRAFANA_TIMEOUT_SECONDS",
    "GRAFANA_FOLDER_UID",
    "GRAFANA_FOLDER_TITLE",
)


class StubUpstream:
    def __init__(self, *, database_ok=True, admin=True, folder=True, transient=0):
        self.database_ok = database_ok
        self.admin = admin
        self.folder = folder
        self.transient = transient  # 前N次 health 5xx（启动窗口）
        self.calls: list[tuple[str, str]] = []
        self.folder_created = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.method, request.url.path))
        path = request.url.path
        if path == "/grafana/api/health":
            if self.transient > 0:
                self.transient -= 1
                return httpx.Response(503, json={"database": "unavailable"})
            return httpx.Response(
                200,
                json={"database": "ok" if self.database_ok else "unavailable",
                      "version": "13.3.1"},
            )
        if request.headers.get("Authorization") != STUB_BASIC:
            return httpx.Response(401, json={"message": "unauthorized"})
        if path == "/grafana/api/user":
            return httpx.Response(
                200, json={"login": "admin", "isGrafanaAdmin": self.admin}
            )
        if path == "/grafana/api/folders/openontology":
            if self.folder or self.folder_created:
                return httpx.Response(200, json={"uid": "openontology"})
            return httpx.Response(404, json={"message": "not found"})
        if path == "/grafana/api/folders" and request.method == "POST":
            self.folder_created += 1
            return httpx.Response(200, json={"uid": "openontology"})
        raise AssertionError(f"unexpected bootstrap API: {request.method} {path}")


@pytest.fixture
def stub_password(monkeypatch):
    """按 bootstrap 新窄环境语义注入共享管理员凭据（不再经 settings）。"""
    monkeypatch.setenv("GRAFANA_ADMIN_USER", "admin")
    monkeypatch.setenv("GRAFANA_ADMIN_PASSWORD", STUB_PASSWORD)
    for key in GRAFANA_ENV_KEYS:
        if key not in ("GRAFANA_ADMIN_USER", "GRAFANA_ADMIN_PASSWORD"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def no_grafana_env(monkeypatch):
    """净化：不留任何 GRAFANA_* 环境（缺配置分支）。"""
    for key in GRAFANA_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _transport(stub):
    return httpx.MockTransport(stub)


def test_bootstrap_creates_folder_once_and_is_idempotent(stub_password, monkeypatch):
    monkeypatch.setattr(bootstrap, "WAIT_INTERVAL_SECONDS", 0.0)
    stub = StubUpstream(folder=False)
    assert bootstrap.main([], transport=_transport(stub)) == 0
    assert stub.folder_created == 1
    # 重复执行：密码不重置、不创建其他用户，文件夹不再重复创建。
    assert bootstrap.main([], transport=_transport(stub)) == 0
    assert stub.folder_created == 1
    written = [call for call in stub.calls if call[0] == "POST"]
    assert written == [("POST", "/grafana/api/folders")]


def test_bootstrap_waits_through_transient_startup_errors(stub_password, monkeypatch):
    monkeypatch.setattr(bootstrap, "WAIT_INTERVAL_SECONDS", 0.0)
    stub = StubUpstream(transient=2)
    assert bootstrap.main([], transport=_transport(stub)) == 0


def test_verify_mode_is_read_only(stub_password):
    stub = StubUpstream(folder=True)
    assert bootstrap.main(["--verify"], transport=_transport(stub)) == 0
    assert all(call[0] == "GET" for call in stub.calls)


def test_verify_mode_fails_when_folder_missing(stub_password, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "WAIT_INTERVAL_SECONDS", 0.0)
    stub = StubUpstream(folder=False)
    assert bootstrap.main(["--verify"], transport=_transport(stub)) == 1
    output = capsys.readouterr()
    assert "GRAFANA_VERIFY_FAILED" in output.err
    # verify 不得发起写请求。
    assert all(call[0] == "GET" for call in stub.calls)


def test_bootstrap_fails_when_database_not_ok(stub_password, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "WAIT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(bootstrap, "WAIT_INTERVAL_SECONDS", 0.05)
    stub = StubUpstream(database_ok=False)
    assert bootstrap.main([], transport=_transport(stub)) == 1
    output = capsys.readouterr()
    assert "GRAFANA_HEALTH_TIMEOUT" in output.err


def test_bootstrap_fails_when_shared_admin_rejected(stub_password, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "WAIT_INTERVAL_SECONDS", 0.0)
    stub = StubUpstream(admin=False)
    assert bootstrap.main([], transport=_transport(stub)) == 1
    output = capsys.readouterr()
    assert "GRAFANA_ADMIN_REJECTED" in output.err
    # 不重置密码：没有任何密码/用户写端点被调用。
    assert all(call[0] == "GET" for call in stub.calls)


def test_bootstrap_fails_closed_without_password(no_grafana_env, capsys):
    stub = StubUpstream()
    assert bootstrap.main([], transport=_transport(stub)) == 1
    output = capsys.readouterr()
    assert "GRAFANA_NOT_CONFIGURED" in output.err
    assert stub.calls == []  # 未做任何网络请求


def test_bootstrap_rejects_timeout_out_of_range(no_grafana_env, monkeypatch, capsys):
    monkeypatch.setenv("GRAFANA_TIMEOUT_SECONDS", "2")
    assert bootstrap.main([], transport=_transport(StubUpstream())) == 1
    monkeypatch.setenv("GRAFANA_TIMEOUT_SECONDS", "61")
    assert bootstrap.main([], transport=_transport(StubUpstream())) == 1
    monkeypatch.setenv("GRAFANA_TIMEOUT_SECONDS", "not-a-number")
    assert bootstrap.main([], transport=_transport(StubUpstream())) == 1
    output = capsys.readouterr()
    assert "GRAFANA_CONFIG_INVALID" in output.err
    assert "Traceback" not in output.out + output.err


def test_bootstrap_never_logs_password_or_upstream_body(stub_password, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "WAIT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(bootstrap, "WAIT_INTERVAL_SECONDS", 0.05)

    def failing(request):
        if request.url.path == "/grafana/api/health":
            return httpx.Response(
                500, text=f"secret-body-{STUB_PASSWORD}"
            )
        raise AssertionError("health probe must fail before other calls")

    assert bootstrap.main([], transport=httpx.MockTransport(failing)) == 1
    output = capsys.readouterr()
    assert "GRAFANA_HEALTH_TIMEOUT" in output.err
    assert STUB_PASSWORD not in output.out + output.err
    assert "secret-body-" not in output.out + output.err


def test_bootstrap_unreachable_upstream_is_bounded(monkeypatch, capsys):
    monkeypatch.setenv("GRAFANA_ADMIN_USER", "admin")
    monkeypatch.setenv("GRAFANA_ADMIN_PASSWORD", STUB_PASSWORD)
    monkeypatch.setattr(bootstrap, "WAIT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(bootstrap, "WAIT_INTERVAL_SECONDS", 0.05)

    def unavailable(request):
        raise httpx.ConnectError(f"refused {STUB_PASSWORD}")

    assert bootstrap.main([], transport=httpx.MockTransport(unavailable)) == 1
    output = capsys.readouterr()
    assert "GRAFANA_HEALTH_TIMEOUT" in output.err
    assert STUB_PASSWORD not in output.out + output.err


def test_bootstrap_error_codes_are_stable(no_grafana_env):
    assert bootstrap.run_bootstrap() == (
        "GRAFANA_NOT_CONFIGURED",
        "Grafana 共享管理员密码未配置",
    )
    assert bootstrap.run_bootstrap(verify_only=True) == (
        "GRAFANA_NOT_CONFIGURED",
        "Grafana 共享管理员密码未配置",
    )


# ---------------------------------------------------------------------------
# t19：窄环境独立性（真实子进程回归，非进程内导入）
# ---------------------------------------------------------------------------

BACKEND_DIR = Path(__file__).resolve().parents[2]


def _clean_subprocess_env() -> dict[str, str]:
    """从零构造子进程环境：无 ENVIRONMENT/DATABASE_URL/REDIS_URL/N8N/GRAFANA_*。

    仅保留操作系统运行所需的少数键（Windows 的 SystemRoot/PATHEXT/PATH、
    临时目录）；配置类变量一概不继承。
    """
    env = {
        "PYTHONIOENCODING": "utf-8",
        "PYTHONPATH": str(BACKEND_DIR),
    }
    for key in (
        "SystemRoot", "PATHEXT", "PATH", "TMP", "TEMP", "TMPDIR",
        "USERPROFILE", "HOME", "USERNAME",
    ):
        value = os.environ.get(key)
        if value is not None:
            env[key] = value
    for forbidden in (
        "ENVIRONMENT", "DATABASE_URL", "REDIS_URL", "NEO4J_URI",
        "N8N_API_URL", "N8N_API_KEY", *GRAFANA_ENV_KEYS,
    ):
        assert forbidden not in env, forbidden
    return env


def _run_bootstrap_subprocess(
    *args: str, env: dict[str, str], cwd: Path
) -> subprocess.CompletedProcess[str]:
    # 独立空 cwd（不读工作树 .env）+ 真实 python -m 子进程。
    return subprocess.run(
        [sys.executable, "-m", "app.operations.grafana.bootstrap", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )


class _LoopbackStubHandler(BaseHTTPRequestHandler):
    """仅绑定 127.0.0.1 的固定镜像 API 面真实 HTTP 桩（子进程真实访问）。"""

    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        return

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/grafana/api/health":
            self._json(200, {"database": "ok", "version": "13.3.1"})
            return
        if self.headers.get("Authorization") != STUB_BASIC:
            self._json(401, {"message": "unauthorized"})
            return
        if path == "/grafana/api/user":
            self._json(200, {"login": "admin", "isGrafanaAdmin": True})
            return
        if path == "/grafana/api/folders/openontology":
            self._json(200, {"uid": "openontology"})
            return
        self._json(404, {"message": "not found"})

    def do_POST(self):  # noqa: N802
        if self.headers.get("Authorization") != STUB_BASIC:
            self._json(401, {"message": "unauthorized"})
            return
        path = self.path.split("?", 1)[0]
        if path == "/grafana/api/folders":
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            self._json(200, {"uid": body.get("uid")})
            return
        self._json(404, {"message": "not found"})


class _LoopbackStubServer:
    def __enter__(self) -> "_LoopbackStubServer":
        self._server = ThreadingHTTPServer(
            ("127.0.0.1", 0), _LoopbackStubHandler
        )
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True
        )
        self._thread.start()
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}"
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def test_subprocess_clean_env_help_runs_without_platform_config(tmp_path):
    """净化环境 + 空 cwd：--help 约定成功，无全局依赖 traceback。"""
    result = _run_bootstrap_subprocess(
        "--help", env=_clean_subprocess_env(), cwd=tmp_path
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "usage:" in result.stdout
    assert "Traceback" not in result.stdout + result.stderr


def test_subprocess_clean_env_missing_password_is_stable_no_traceback(tmp_path):
    """窄 env 缺密码：稳定 GRAFANA_NOT_CONFIGURED 且非零，非全局依赖崩溃。"""
    result = _run_bootstrap_subprocess(
        env=_clean_subprocess_env(), cwd=tmp_path
    )
    assert result.returncode == 1
    assert "GRAFANA_NOT_CONFIGURED" in result.stderr
    combined = result.stdout + result.stderr
    assert "Traceback" not in combined
    # 不是全局依赖校验崩溃（原缺陷是 import 期 RuntimeError）。
    assert "DATABASE_URL" not in combined
    assert "RuntimeError" not in combined


def test_subprocess_clean_env_invalid_timeout_is_stable_config_invalid(tmp_path):
    env = _clean_subprocess_env()
    env["GRAFANA_TIMEOUT_SECONDS"] = "61"
    result = _run_bootstrap_subprocess(env=env, cwd=tmp_path)
    assert result.returncode == 1
    assert "GRAFANA_CONFIG_INVALID" in result.stderr
    assert "Traceback" not in result.stdout + result.stderr


def test_subprocess_real_config_completes_bootstrap_against_loopback_stub(tmp_path):
    """真实配置 + 真实进程 + 本地回环桩：完整 bootstrap exit 0（含重复幂等）。"""
    for _attempt in range(2):
        with _LoopbackStubServer() as stub:
            env = _clean_subprocess_env()
            env.update({
                "GRAFANA_BASE_URL": stub.base_url,
                "GRAFANA_ADMIN_USER": "admin",
                "GRAFANA_ADMIN_PASSWORD": STUB_PASSWORD,
            })
            result = _run_bootstrap_subprocess(env=env, cwd=tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "grafana bootstrap ok" in result.stdout
        assert STUB_PASSWORD not in result.stdout + result.stderr


def test_subprocess_wrong_password_is_stable_unreachable_no_leak(tmp_path):
    """错误密码（上游 401）：稳定 GRAFANA_UNREACHABLE，不回显原始异常。"""
    with _LoopbackStubServer() as stub:
        env = _clean_subprocess_env()
        env.update({
            "GRAFANA_BASE_URL": stub.base_url,
            "GRAFANA_ADMIN_USER": "admin",
            "GRAFANA_ADMIN_PASSWORD": "WrongPassword2026!",
        })
        result = _run_bootstrap_subprocess(env=env, cwd=tmp_path)
    assert result.returncode == 1
    combined = result.stdout + result.stderr
    assert "GRAFANA_UNREACHABLE" in combined
    assert "Traceback" not in combined
    assert "WrongPassword2026!" not in combined
