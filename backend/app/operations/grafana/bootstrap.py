"""Grafana bootstrap 入口（compose ``grafana-bootstrap`` 服务）。

用法：``python -m app.operations.grafana.bootstrap [--verify]``

窄环境独立可执行：本入口**只**读取自己的六个 ``GRAFANA_*`` 环境变量
（BASE_URL/ADMIN_USER/ADMIN_PASSWORD/TIMEOUT_SECONDS/FOLDER_UID/FOLDER_TITLE，
默认值与平台 shared.config 的冻结默认一致），显式传全量参数构造
``GrafanaClient``，全程不导入 ``app.shared.config``——prod/local 的
grafana-bootstrap 容器因此无需平台 DATABASE_URL/REDIS_URL/N8N 等无关配置
即可导入执行（服务/探针仍经 ``build_client()`` 使用平台 settings，注入
语义不变）。

- 等待有界健康：``/grafana/api/health`` 必须 ``database=ok``；
- basic ``/grafana/api/user`` 必须是共享管理员（``isGrafanaAdmin=true``）；
- ensure 文件夹（UID/标题来自环境，UID 固定 ``openontology``）；
- ``--verify`` 只读确认，不发起任何写请求。

稳定错误码 + 非零 exit；不回显任何上游响应体/凭据/URL/异常原文（仅固定
中文说明）。重复执行不重置密码、不创建其他用户、不加载其他服务秘密
（``GF_SECURITY_ADMIN_PASSWORD`` 只在首次卷初始化生效；原生改密后需运维
同步配置并重启需要该凭据的 backend/bootstrap，本入口不能也不试图用 env
重置旧卷密码）。
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from app.operations.grafana import client as grafana_client

WAIT_TIMEOUT_SECONDS = 60.0
WAIT_INTERVAL_SECONDS = 1.0

# 与平台 shared.config 冻结默认一致的窄环境默认值。
DEFAULT_BASE_URL = "http://grafana:3000"
DEFAULT_ADMIN_USER = "admin"
DEFAULT_TIMEOUT_SECONDS = 15.0
DEFAULT_FOLDER_UID = "openontology"
DEFAULT_FOLDER_TITLE = "OpenOntology 监控看板"


def _bootstrap_config() -> dict:
    """读取本入口所需的六个 GRAFANA_* 环境变量（冻结默认值/timeout 3..60）。

    只读环境变量，不读平台配置/工作树 .env：grafana-bootstrap 容器只有
    这些窄环境。timeout 范围与 shared.config 的 ge=3/le=60 一致，越界或
    非数值一律拒绝（ValueError，由调用方映射为稳定错误码）。
    """
    timeout_raw = os.environ.get("GRAFANA_TIMEOUT_SECONDS", "").strip()
    if not timeout_raw:
        timeout = DEFAULT_TIMEOUT_SECONDS
    else:
        try:
            timeout = float(timeout_raw)
        except ValueError:
            raise ValueError(
                "GRAFANA_TIMEOUT_SECONDS must be a number in 3..60"
            ) from None
        if not 3.0 <= timeout <= 60.0:
            raise ValueError("GRAFANA_TIMEOUT_SECONDS must be in 3..60")
    return {
        "base_url": os.environ.get("GRAFANA_BASE_URL", "").strip() or DEFAULT_BASE_URL,
        "user": os.environ.get("GRAFANA_ADMIN_USER", "").strip() or DEFAULT_ADMIN_USER,
        # 密码不 strip、无默认：空值即未配置，构造客户端时 fail-closed。
        "password": os.environ.get("GRAFANA_ADMIN_PASSWORD", ""),
        "timeout": timeout,
        "folder_uid": os.environ.get("GRAFANA_FOLDER_UID", "").strip()
        or DEFAULT_FOLDER_UID,
        "folder_title": os.environ.get("GRAFANA_FOLDER_TITLE", "").strip()
        or DEFAULT_FOLDER_TITLE,
    }


def _wait_for_health(client: grafana_client.GrafanaClient) -> None:
    """有界等待健康；缺配置立即失败，不无限重试。

    启动窗口内 Grafana 可能短暂 5xx/不可达：在有限截止时间内重试，
    超时报 ``GRAFANA_HEALTH_TIMEOUT``，不无限等待。
    """
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            if client.health_database_ok():
                return
        except (
            grafana_client.GrafanaUnavailable,
            grafana_client.GrafanaOperationFailed,
        ):
            pass
        time.sleep(WAIT_INTERVAL_SECONDS)
    raise RuntimeError("GRAFANA_HEALTH_TIMEOUT")


def run_bootstrap(
    *, verify_only: bool = False, transport=None
) -> tuple[str, str]:
    """执行 bootstrap/verify；返回 (错误码, 中文说明)，``OK`` 表示成功。

    说明文本固定，不包含上游响应体、凭据、地址或异常原文。
    """
    try:
        config = _bootstrap_config()
    except ValueError:
        return (
            "GRAFANA_CONFIG_INVALID",
            "GRAFANA_TIMEOUT_SECONDS 必须是 3..60 范围内的数值",
        )
    try:
        client = grafana_client.GrafanaClient(
            base_url=config["base_url"],
            user=config["user"],
            password=config["password"],
            timeout=config["timeout"],
            folder_uid=config["folder_uid"],
            folder_title=config["folder_title"],
            transport=transport,
        )
    except grafana_client.GrafanaNotConfigured:
        return ("GRAFANA_NOT_CONFIGURED", "Grafana 共享管理员密码未配置")
    try:
        _wait_for_health(client)
        if not client.current_user_is_admin():
            return (
                "GRAFANA_ADMIN_REJECTED",
                "Grafana 共享管理员校验失败：请核对配置的共享管理员凭据",
            )
        if verify_only:
            if not client.folder_exists():
                return ("GRAFANA_VERIFY_FAILED", "监控看板文件夹不存在")
        else:
            client.ensure_folder()
        return ("OK", "")
    except grafana_client.GrafanaUnavailable:
        # 含上游身份拒绝（401/403）：稳定安全码，不回显原始异常。注意
        # GrafanaUnavailable 是 RuntimeError 子类，必须先于通用 RuntimeError
        # 处理，否则健康超时分支会将其原样上抛（错误密码 → traceback）。
        return ("GRAFANA_UNREACHABLE", "Grafana 服务不可用或凭据被拒绝")
    except grafana_client.GrafanaOperationFailed:
        return ("GRAFANA_OPERATION_FAILED", "Grafana 返回非预期响应")
    except RuntimeError as exc:
        # 仅 _wait_for_health 的有界超时，消息为固定错误码。
        if str(exc) != "GRAFANA_HEALTH_TIMEOUT":
            raise
        return ("GRAFANA_HEALTH_TIMEOUT", "等待 Grafana 健康超时")


def main(argv: list[str] | None = None, *, transport=None) -> int:
    parser = argparse.ArgumentParser(
        prog="app.operations.grafana.bootstrap",
        description="Grafana 监控看板 bootstrap（固定 13.3.1 镜像）",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="只读确认健康、共享管理员与文件夹，不做任何写操作",
    )
    args = parser.parse_args(argv)
    code, message = run_bootstrap(verify_only=args.verify, transport=transport)
    if code == "OK":
        print("grafana bootstrap verified" if args.verify else "grafana bootstrap ok")
        return 0
    print(f"grafana bootstrap failed: {code} {message}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
