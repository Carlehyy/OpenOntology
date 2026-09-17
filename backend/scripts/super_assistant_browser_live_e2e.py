#!/usr/bin/env python3
"""超级助手浏览器协作 live E2E：真实 uvicorn（TCP HTTP）+ 真实 Chromium CDP。

AGENTS.md 第 5 节强制项：Chromium CDP 与 WebSocket 链路不得用 mock/TestClient
验收。范式同 steward_live_e2e.py：隔离 SQLite + 临时目录 env 注入、
FIRST_ADMIN_PASSWORD 种子管理员、秘密只从环境变量/TTY 读取、finally 全量清理。

与 steward_live_e2e 的差异：本脚本把 uvicorn.Server 跑在本进程的后台线程
（仍是真实 TCP HTTP，非 TestClient 内存态），这样第 6 步「工具直连」能在同一
进程内触达持有浏览器会话的 browser_manager 单例。

运行（在 backend/ 目录）：
    uv run python scripts/super_assistant_browser_live_e2e.py

环境变量（可选）：LLM_API_BASE / LLM_API_KEY / LLM_MODEL —— 三者齐备时追加
真实 LLM 探针（SSE chat 断言 browser_* 工具被调用）；缺任一项则该步 SKIP。
证据 JSON 写入 <repo>/.artifacts/super_assistant_browser_live_e2e_<ts>.json。
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

CDP_CONTAINER = "oo-sa-browser-live-e2e"
CDP_HOST_PORT = 9333
API_PORT = 8100
PAGE_TITLE = "SA-BROWSER-LIVE-E2E"
PAGE_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    f"<title>{PAGE_TITLE}</title></head>"
    "<body><h1>super assistant browser live e2e</h1>"
    "<p>real page served over HTTP for CDP acceptance</p></body></html>"
)
JWT_MIN_SECRET = "sa-browser-live-e2e-secret-key-32-bytes!!"
ADMIN_PASSWORD = "sa-browser-live-e2e-admin-password"


def _lan_ip() -> str:
    """本机局域网 IP（数字地址免 DNS；本机外网 DNS 当前不可用）。"""
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.168.65.254", 80))  # Docker Desktop 宿主机网关
        return str(probe.getsockname()[0])
    finally:
        probe.close()


def _args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-temp", action="store_true")
    return parser.parse_args()


def _configure_process(temp_root: Path) -> None:
    # 隔离 SQLite fixture：这不是正常平台启动或全栈就绪测试。
    # 必须在任何 app 模块导入前完成（settings 是 import 期单例）。
    os.environ["ENVIRONMENT"] = "test"
    os.environ["DATABASE_URL"] = f"sqlite:///{temp_root / 'platform.db'}"
    os.environ["UPLOADS_DIR"] = str(temp_root / "uploads")
    os.environ["STEWARD_WORKSPACE_ROOT"] = str(temp_root / "steward-sessions")
    os.environ["SUPER_ASSISTANT_WORKSPACE_ROOT"] = str(temp_root / "sa-sessions")
    os.environ["API_HUB_DATA_DIR"] = str(temp_root / "api-hub")
    os.environ["STORAGE_LOCAL_DIR"] = str(temp_root / "storage")
    os.environ["SECRET_KEY"] = JWT_MIN_SECRET
    os.environ["FIRST_ADMIN_PASSWORD"] = ADMIN_PASSWORD
    os.environ["STEWARD_BROWSER_CDP_URL"] = f"http://localhost:{CDP_HOST_PORT}"
    # 本脚本只打 loopback/LAN 地址：摘掉（可能存在的）代理环境变量，
    # 防止 websockets/requests 走系统代理设置去连 127.0.0.1。
    for _var in ("ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy",
                 "HTTPS_PROXY", "https_proxy", "SOCKS_PROXY", "socks_proxy"):
        os.environ.pop(_var, None)
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"


def _secret(env_name: str, prompt: str) -> str:
    value = os.getenv(env_name, "").strip()
    if value:
        return value
    if sys.stdin.isatty():
        return getpass.getpass(prompt).strip()
    return ""


def _docker(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", *argv], capture_output=True, text=True, timeout=120)


class _Harness:
    """按步记录断言结果；关键步失败即中止后续（保留清理与证据写出）。"""

    def __init__(self) -> None:
        self.steps: list[dict] = []
        self._failed = False

    def step(self, name: str, fn, detail_fn=None) -> None:
        if self._failed:
            self.steps.append({"name": name, "status": "SKIP",
                               "detail": "blocked by earlier failure"})
            return
        started = time.monotonic()
        try:
            result = fn()
            self.steps.append({
                "name": name, "status": "PASS",
                "durationMs": int((time.monotonic() - started) * 1000),
                "detail": detail_fn(result) if detail_fn else result,
            })
        except Exception as exc:  # noqa: BLE001 — 证据必须记录真实失败
            self._failed = True
            self.steps.append({
                "name": name, "status": "FAIL",
                "durationMs": int((time.monotonic() - started) * 1000),
                "detail": f"{type(exc).__name__}: {exc}",
            })

    def skip(self, name: str, reason: str) -> None:
        self.steps.append({"name": name, "status": "SKIP", "detail": reason})

    @property
    def ok(self) -> bool:
        return not self._failed


def main() -> int:
    args = _args()
    temp_root = Path(tempfile.mkdtemp(prefix="ontology-sa-browser-live-e2e-"))
    _configure_process(temp_root)
    backend_root = Path(__file__).resolve().parents[1]
    repo_root = backend_root.parent
    sys.path.insert(0, str(backend_root))

    import requests  # noqa: E402

    report: dict = {
        "ok": False,
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "tempRoot": str(temp_root) if args.keep_temp else "removed-after-run",
        "cdpContainer": CDP_CONTAINER,
        "cdpPort": CDP_HOST_PORT,
        "apiPort": API_PORT,
        "notes": [
            "本机外网 DNS 不可用（getaddrinfo/curl 均失败），验收目标 URL 替换为"
            "本机真实 HTTP 服务页面（数字 LAN IP，容器可路由；后端 URL 校验依赖"
            " steward_browser_allow_private_networks 默认 True）。",
        ],
        "steps": [],
    }
    harness = _Harness()
    server = None
    server_thread = None
    page_server = None
    container_started = False
    state: dict = {}

    def start_page_server() -> None:
        """真实 HTTP 页面服务（验收目标 URL 的宿主侧）。"""
        nonlocal page_server
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                body = PAGE_HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *argv) -> None:
                return

        page_server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
        threading.Thread(target=page_server.serve_forever, daemon=True).start()
        state["target_url"] = (
            f"http://{_lan_ip()}:{page_server.server_address[1]}/index.html")

    def start_container() -> dict:
        nonlocal container_started
        # 只碰自己名下的容器；9222 上别的 worktree 容器一律不动
        _docker("rm", "-f", CDP_CONTAINER)
        run = _docker(
            "run", "-d", "--name", CDP_CONTAINER,
            "-p", f"{CDP_HOST_PORT}:9222",
            "chromedp/headless-shell:latest",
            "--no-sandbox", "--disable-dev-shm-usage",
        )
        if run.returncode != 0:
            raise RuntimeError(f"docker run failed: {run.stderr.strip()[:400]}")
        container_started = True
        deadline = time.monotonic() + 60
        version = None
        while time.monotonic() < deadline:
            try:
                resp = requests.get(
                    f"http://localhost:{CDP_HOST_PORT}/json/version", timeout=2)
                if resp.ok:
                    version = resp.json()
                    break
            except requests.RequestException:
                pass
            time.sleep(1)
        if version is None:
            raise RuntimeError("Chromium CDP /json/version not ready in 60s")
        return {"browser": version.get("Browser"), "webSocketDebuggerUrl": bool(
            version.get("webSocketDebuggerUrl"))}

    def start_backend() -> dict:
        nonlocal server, server_thread
        import uvicorn
        from app.main import app

        config = uvicorn.Config(
            app, host="127.0.0.1", port=API_PORT,
            log_level="warning", access_log=False)
        server = uvicorn.Server(config)
        server_thread = threading.Thread(target=server.run, daemon=True)
        server_thread.start()
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if not server_thread.is_alive():
                raise RuntimeError("uvicorn thread exited during startup")
            try:
                resp = requests.get(
                    f"http://127.0.0.1:{API_PORT}/health/live", timeout=2)
                if resp.ok:
                    return {"health": resp.json()}
            except requests.RequestException:
                pass
            time.sleep(1)
        raise RuntimeError("backend /health/live not ready in 90s")

    def http() -> requests.Session:
        session = requests.Session()
        session.headers["Authorization"] = f"Bearer {state['token']}"
        return session

    def unwrap(resp: "requests.Response"):
        if not resp.ok:
            raise AssertionError(
                f"{resp.request.method} {resp.request.url} -> "
                f"{resp.status_code}: {resp.text[:300]}")
        body = resp.json()
        return body.get("data", body)

    try:
        start_page_server()
        harness.step("chromium_container", start_container)
        harness.step("backend_boot", start_backend)
        if not harness.ok:
            raise RuntimeError("bootstrap failed")

        def login() -> dict:
            resp = requests.post(
                f"http://127.0.0.1:{API_PORT}/api/v1/auth/login",
                json={"username": "admin", "password": ADMIN_PASSWORD},
                timeout=10)
            data = unwrap(resp)
            token = data.get("access_token")
            assert token, f"login payload without access_token: {data}"
            state["token"] = token
            return {"tokenType": data.get("token_type")}

        harness.step("admin_login", login)
        if not harness.ok:
            raise RuntimeError("login failed")

        def create_conversation() -> dict:
            resp = http().post(
                f"http://127.0.0.1:{API_PORT}/api/v2/super-assistant/conversations",
                json={"title": "浏览器协作 live e2e"}, timeout=10)
            assert resp.status_code == 201, resp.text[:300]
            conv = resp.json()
            assert conv["id"] and "browser_source_id" in conv
            assert conv["browser_source_id"] is None
            state["cid"] = conv["id"]
            return {"conversationId": conv["id"],
                    "browserSourceIdKeyPresent": True}

        harness.step("create_conversation", create_conversation)

        def browser_start() -> dict:
            cid = state["cid"]
            target_url = state["target_url"]
            resp = http().post(
                f"http://127.0.0.1:{API_PORT}/api/v2/super-assistant"
                f"/conversations/{cid}/browser/start",
                json={"url": target_url}, timeout=60)
            data = unwrap(resp)
            assert str(data.get("url")).startswith(target_url), data
            assert data.get("title") == PAGE_TITLE, data.get("title")
            session = unwrap(http().get(
                f"http://127.0.0.1:{API_PORT}/api/v2/super-assistant"
                f"/conversations/{cid}/browser/session", timeout=10))
            assert session.get("active") is True, session
            assert target_url in session.get("url", ""), session
            # 硬证据：页面真的开在本脚本拉起的容器里（不是 9222 的别的实例）
            targets = requests.get(
                f"http://localhost:{CDP_HOST_PORT}/json/list", timeout=5).json()
            assert any(target_url in str(t.get("url")) for t in targets), targets
            return {"url": session["url"], "title": data.get("title"),
                    "active": True, "cdpContainerSeesPage": True}

        harness.step("browser_start", browser_start)

        def workspace_isolation() -> dict:
            cid = state["cid"]
            sa_state = (
                temp_root / "sa-sessions" / cid / ".browser" / "storage-state.json")
            steward_dir = temp_root / "steward-sessions" / cid
            assert sa_state.exists(), f"missing {sa_state}"
            assert not steward_dir.exists(), f"unexpected {steward_dir}"
            return {"saStorageState": sa_state.name,
                    "stewardSessionDirAbsent": True}

        harness.step("workspace_isolation", workspace_isolation)

        def live_ws() -> dict:
            from websockets.sync.client import connect

            cid = state["cid"]
            ticket = unwrap(http().post(
                f"http://127.0.0.1:{API_PORT}/api/v2/super-assistant"
                f"/conversations/{cid}/browser/ticket", timeout=10))["ticket"]

            def wait_msg(ws, pred, timeout=20):
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    try:
                        raw = ws.recv(timeout=max(0.1, deadline - time.monotonic()))
                    except TimeoutError:
                        continue
                    msg = json.loads(raw)
                    if msg.get("type") == "error":
                        raise AssertionError(f"ws error frame: {msg}")
                    if pred(msg):
                        return msg
                raise TimeoutError("timed out waiting for expected ws message")

            outcome: dict = {}
            ws_url = (
                f"ws://127.0.0.1:{API_PORT}/api/v2/super-assistant"
                f"/conversations/{cid}/browser/live?ticket={ticket}")
            with connect(ws_url, open_timeout=10, proxy=None) as ws:
                frame = wait_msg(ws, lambda m: m.get("type") == "frame")
                image = base64.b64decode(frame["data"])
                assert image[:3] == b"\xff\xd8\xff", "frame is not JPEG"
                outcome["frameJpegBytes"] = len(image)
                outcome["frameUrl"] = frame.get("url")

                ws.send(json.dumps({"type": "control", "action": "hold"}))
                held = wait_msg(ws, lambda m: m.get("type") == "collaboration")
                assert held["collaboration"]["controller"] == "user", held
                assert held["collaboration"]["mode"] == "held", held
                outcome["hold"] = held["collaboration"]

                ws.send(json.dumps({"type": "control", "action": "release"}))
                released = wait_msg(ws, lambda m: m.get("type") == "collaboration")
                assert released["collaboration"]["controller"] == "agent", released
                assert released["collaboration"]["mode"] == "observe", released
                outcome["release"] = released["collaboration"]

                ws.send(json.dumps({
                    "type": "mouse", "action": "move", "x": 100, "y": 100}))
                moved = wait_msg(ws, lambda m: m.get("type") == "collaboration")
                assert "collaboration" in moved, moved
                outcome["mouseMoveAccepted"] = True

                # 收尾释放 transient 控制，避免影响后续 agent 侧断言
                ws.send(json.dumps({"type": "control", "action": "release"}))
                wait_msg(ws, lambda m: m.get("type") == "collaboration")
            return outcome

        harness.step("live_ws", live_ws)

        def tool_direct() -> dict:
            from app.database import SessionLocal
            from app.super_assistant import browser_tools

            cid = state["cid"]
            db = SessionLocal()
            try:
                from app.auth.models import User
                admin = db.query(User).filter(User.role == "admin").first()
                payload = json.loads(browser_tools.execute_browser_tool(
                    db, owner_id=admin.id, conversation_id=cid,
                    name="browser_state", arguments={}))
                assert str(payload.get("url")).startswith(state["target_url"]), payload
                assert payload.get("title") == PAGE_TITLE, payload.get("title")
                denied = json.loads(browser_tools.execute_browser_tool(
                    db, owner_id="no-such-user", conversation_id=cid,
                    name="browser_state", arguments={}))
                assert "error" in denied, denied
                return {"browserStateUrl": payload["url"],
                        "wrongOwnerError": denied["error"]}
            finally:
                db.close()

        harness.step("tool_direct", tool_direct)

        def live_http() -> dict:
            cid = state["cid"]
            base = (
                f"http://127.0.0.1:{API_PORT}/api/v2/super-assistant"
                f"/conversations/{cid}/browser/live-http")
            attached = unwrap(http().post(base, timeout=15))
            lease = attached["leaseId"]
            frame = unwrap(http().post(
                f"{base}/frame", json={"leaseId": lease}, timeout=30))
            image = base64.b64decode(frame["data"])
            assert image[:3] == b"\xff\xd8\xff", "http frame is not JPEG"
            accepted = unwrap(http().post(
                f"{base}/input",
                json={"leaseId": lease,
                      "message": {"type": "mouse", "action": "move",
                                  "x": 64, "y": 64}},
                timeout=15))
            assert accepted.get("accepted") is True, accepted
            held = unwrap(http().post(
                f"{base}/control",
                json={"leaseId": lease, "action": "hold"}, timeout=15))
            assert held["collaboration"]["controller"] == "user", held
            released = unwrap(http().post(
                f"{base}/release", json={"leaseId": lease}, timeout=15))
            assert released.get("released") is True, released
            return {"leaseIssued": True, "frameJpegBytes": len(image),
                    "inputAccepted": True, "holdController": "user",
                    "released": True}

        harness.step("live_http", live_http)

        llm_base = _secret("LLM_API_BASE", "LLM_API_BASE: ")
        llm_key = _secret("LLM_API_KEY", "LLM_API_KEY: ")
        llm_model = os.getenv("LLM_MODEL", "").strip()
        if llm_base and llm_key and llm_model:
            def llm_probe() -> dict:
                import uuid

                from app.auth.models import User
                from app.database import SessionLocal
                from app.model_configs.models import ModelConfig
                from app.shared.encryption import encrypt

                db = SessionLocal()
                try:
                    admin = db.query(User).filter(User.role == "admin").first()
                    config = ModelConfig(
                        id=str(uuid.uuid4()), name="sa-browser-live-e2e",
                        config_type="llm", provider="compatible",
                        api_base=llm_base, api_key_encrypted=encrypt(llm_key),
                        models=[llm_model], enabled=True, created_by=admin.id)
                    db.add(config)
                    db.commit()
                    model_id = config.id
                finally:
                    db.close()

                cid = state["cid"]
                seen: list[str] = []
                with http().post(
                    f"http://127.0.0.1:{API_PORT}/api/v2/super-assistant"
                    f"/conversations/{cid}/chat",
                    json={
                        "message": f"请用浏览器打开 {state['target_url']} "
                                   "并告诉我页面标题。",
                        "model_config_id": model_id,
                    },
                    stream=True, timeout=(10, 240),
                ) as resp:
                    assert resp.ok, resp.text[:300]
                    event = None
                    deadline = time.monotonic() + 240
                    for line in resp.iter_lines(decode_unicode=True):
                        if time.monotonic() > deadline:
                            raise TimeoutError("chat stream exceeded 240s")
                        if not line:
                            continue
                        if line.startswith("event: "):
                            event = line[7:].strip()
                            continue
                        if line.startswith("data: ") and event:
                            data = json.loads(line[6:])
                            if event == "tool_start":
                                seen.append(str(data.get("toolName")))
                            if event in {"message_end", "done", "error"}:
                                break
                            event = None
                assert any(name.startswith("browser_") for name in seen), seen
                return {"toolStarts": seen}

            harness.step("llm_probe", llm_probe)
        else:
            missing = [
                name for name, value in (
                    ("LLM_API_BASE", llm_base), ("LLM_API_KEY", llm_key),
                    ("LLM_MODEL", llm_model))
                if not value
            ]
            harness.skip("llm_probe", f"missing env: {', '.join(missing)}")

        def delete_conversation() -> dict:
            cid = state["cid"]
            resp = http().delete(
                f"http://127.0.0.1:{API_PORT}/api/v2/super-assistant"
                f"/conversations/{cid}", timeout=15)
            assert resp.status_code == 204, resp.text[:300]
            # 会话行已删：会话级端点按归属 404 语义回应（不是 200/inactive）
            session_resp = http().get(
                f"http://127.0.0.1:{API_PORT}/api/v2/super-assistant"
                f"/conversations/{cid}/browser/session", timeout=10)
            assert session_resp.status_code == 404, session_resp.text[:300]
            # 真实运行时断言：browser_manager 会话已被 close
            from app.data_channel.steward.browser_runtime import browser_manager
            info = browser_manager.session_info(cid)
            assert info.get("active") is False, info
            assert not (temp_root / "sa-sessions" / cid).exists()
            return {"httpStatusAfterDelete": 404,
                    "managerActive": False, "workspaceRemoved": True}

        harness.step("delete_conversation", delete_conversation)
    except Exception as exc:  # noqa: BLE001
        report["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        cleanup: dict = {"errors": []}
        try:
            from app.data_channel.steward.browser_runtime import browser_manager
            browser_manager.close_all()
        except Exception as exc:  # noqa: BLE001
            cleanup["errors"].append(f"browser close_all: {exc}")
        if server is not None:
            try:
                server.should_exit = True
                if server_thread is not None:
                    server_thread.join(timeout=20)
                cleanup["uvicornStopped"] = not (
                    server_thread and server_thread.is_alive())
            except Exception as exc:  # noqa: BLE001
                cleanup["errors"].append(f"uvicorn: {exc}")
        if container_started:
            rm = _docker("rm", "-f", CDP_CONTAINER)
            cleanup["containerRemoved"] = rm.returncode == 0
            if rm.returncode != 0:
                cleanup["errors"].append(f"docker rm: {rm.stderr.strip()[:200]}")
        if page_server is not None:
            try:
                page_server.shutdown()
                page_server.server_close()
                cleanup["pageServerStopped"] = True
            except Exception as exc:  # noqa: BLE001
                cleanup["errors"].append(f"page server: {exc}")

        def port_free(port: int) -> bool:
            out = subprocess.run(
                ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True)
            return not out.stdout.strip()

        cleanup["port9333Free"] = port_free(CDP_HOST_PORT)
        cleanup["port8100Free"] = port_free(API_PORT)
        if not args.keep_temp:
            shutil.rmtree(temp_root, ignore_errors=True)
        report["cleanup"] = cleanup
        report["cleanup"]["ok"] = not cleanup["errors"]
        report["steps"] = harness.steps
        report["ok"] = harness.ok and report["cleanup"]["ok"]
        report["finishedAt"] = datetime.now(timezone.utc).isoformat()

        artifacts = repo_root / ".artifacts"
        artifacts.mkdir(exist_ok=True)
        evidence = artifacts / (
            "super_assistant_browser_live_e2e_"
            + time.strftime("%Y%m%d-%H%M%S") + ".json")
        evidence.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["evidence"] = str(evidence)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
