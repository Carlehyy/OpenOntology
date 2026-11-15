"""固定版本 Grafana（13.3.1 digest）HTTP 客户端。

端点全部经 ``{GRAFANA_BASE_URL}/grafana/api/…``（SERVE_FROM_SUB_PATH 部署，
PoC 已实测子路径形态）。接口形态来自 t1 固定镜像 PoC 实测：

- 空看板创建：``POST /api/dashboards/db``，``panels=[]``、预生成 uid、
  ``schemaVersion=40``、``overwrite=false``、``folderUid`` 指向平台文件夹；
- 同 folder 同标题允许（13.3.1 实测），平台不做标题冲突映射；
- 搜索对账：``GET /api/search?type=dash-db`` 必须用**重复** ``dashboardUIDs``
  参数并显式带 ``limit >= 分块大小``（逗号串会静默返回空，属硬性坑）；
- 删除 ``DELETE /api/dashboards/uid/{uid}``：404 视为已删除（幂等成功）；
- basic auth 与禁用登录表单并存，匿名关闭。

错误类型与 HTTP 映射（错误消息不携带 URL、凭据或上游响应体）：

- ``GrafanaNotConfigured`` → 503 ``grafana_not_configured``（使用时 fail-closed）；
- ``GrafanaUnavailable`` → 502 ``grafana_unavailable``（连接/超时/401/403）；
- ``GrafanaOperationFailed`` → 502 ``grafana_operation_failed``（非预期上游
  响应，仅携带安全的 HTTP 状态码）。
"""
from __future__ import annotations

from collections.abc import Iterable

import httpx


def _settings():
    """惰性解析平台 settings 单例（仅参数缺省回退时调用）。

    grafana-bootstrap 容器只有 GRAFANA_* 窄环境（无平台 DATABASE_URL/
    REDIS_URL/N8N 配置），``app.shared.config`` 在导入期就会做全局依赖
    校验并抛错——因此这里不做模块级导入。服务/探针经 ``build_client()``
    走参数缺省回退，仍解析同一 settings 单例：现有配置/monkeypatch 注入
    语义不变；bootstrap 显式传全量参数，全程不触碰平台配置。
    """
    from app.shared.config import settings

    return settings


# 对账分块大小：每块显式 limit（≥块大小）；limit 会截断 dashboardUIDs 查询，
# 因此 limit 必须 ≥ 块内 UID 数，恰好等于块数的结果才是完整的。
SEARCH_CHUNK_SIZE = 100
# 固定镜像实测可创建的空看板 schemaVersion。
DASHBOARD_SCHEMA_VERSION = 40


class GrafanaNotConfigured(RuntimeError):
    """共享管理员密码缺失；调用方 fail-closed 503。"""


class GrafanaUnavailable(RuntimeError):
    """连接失败/超时或上游 401/403；调用方 502。"""


class GrafanaOperationFailed(RuntimeError):
    """非预期上游响应；仅携带安全的 HTTP 状态码，调用方 502。"""

    def __init__(self, status: int):
        super().__init__(f"grafana_operation_failed:{status}")
        self.status = status


class GrafanaClient:
    """仅覆盖平台所需的固定 API 面（PoC 实测），不引入额外抽象。"""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        user: str | None = None,
        password: str | None = None,
        timeout: float | None = None,
        folder_uid: str | None = None,
        folder_title: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ):
        # 仅缺参回退时才惰性解析平台 settings 单例（一次获取）；bootstrap
        # 显式传全量六项参数，全程不导入 shared.config。
        if any(
            value is None
            for value in (base_url, user, password, timeout, folder_uid, folder_title)
        ):
            settings = _settings()
            if base_url is None:
                base_url = settings.grafana_base_url
            if user is None:
                user = settings.grafana_admin_user
            if password is None:
                password = settings.grafana_admin_password
            if timeout is None:
                timeout = settings.grafana_timeout_seconds
            if folder_uid is None:
                folder_uid = settings.grafana_folder_uid
            if folder_title is None:
                folder_title = settings.grafana_folder_title
        self._base = str(base_url or "").strip().rstrip("/")
        self._user = str(user or "admin")
        self._password = password if password is not None else ""
        self._timeout = float(timeout if timeout is not None else 15)
        self._folder_uid = str(folder_uid or "")
        self._folder_title = str(folder_title or "")
        self._transport = transport
        if not self._password:
            # 只在使用时 fail-closed；普通测试/worker 允许空值导入。
            raise GrafanaNotConfigured("grafana_admin_password is empty")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json_body: dict | None = None,
        allow: tuple[int, ...] = (),
    ) -> httpx.Response:
        url = f"{self._base}/grafana/api{path}"
        try:
            # trust_env=False：不经系统代理（内网私网服务名直连；同 Kaneo/WeKnora
            # 先例，避免部署环境代理劫持或泄漏凭据给代理）。
            with httpx.Client(
                transport=self._transport,
                timeout=self._timeout,
                auth=(self._user, self._password),
                trust_env=False,
            ) as client:
                response = client.request(method, url, params=params, json=json_body)
        except httpx.HTTPError as exc:
            raise GrafanaUnavailable("grafana_unreachable") from exc
        if response.status_code in (401, 403):
            raise GrafanaUnavailable("grafana_auth_rejected")
        if response.status_code >= 400 and response.status_code not in allow:
            raise GrafanaOperationFailed(response.status_code)
        return response

    def _json(
        self,
        method: str,
        path: str,
        *,
        params: list[tuple[str, str]] | None = None,
        json_body: dict | None = None,
        allow: tuple[int, ...] = (),
    ):
        response = self._request(
            method, path, params=params, json_body=json_body, allow=allow
        )
        try:
            return response.json()
        except ValueError as exc:
            raise GrafanaOperationFailed(response.status_code) from exc

    # ------------------------------------------------------------------
    # 健康与身份（readiness/bootstrap 探针共用）
    # ------------------------------------------------------------------

    def health_database_ok(self) -> bool:
        payload = self._json("GET", "/health")
        return isinstance(payload, dict) and payload.get("database") == "ok"

    def current_user_is_admin(self) -> bool:
        payload = self._json("GET", "/user")
        return isinstance(payload, dict) and payload.get("isGrafanaAdmin") is True

    # ------------------------------------------------------------------
    # 文件夹（bootstrap ensure；平台不限制使用者在原生界面移动看板）
    # ------------------------------------------------------------------

    def folder_exists(self) -> bool:
        response = self._request("GET", f"/folders/{self._folder_uid}", allow=(404,))
        return response.status_code == 200

    def ensure_folder(self) -> None:
        response = self._request("GET", f"/folders/{self._folder_uid}", allow=(404,))
        if response.status_code == 200:
            return
        # 404 → 创建；并发/半初始化导致 uid 已被占用（409/412）时以重新读取
        # 确认为准，不视为失败。
        self._request(
            "POST",
            "/folders",
            json_body={"uid": self._folder_uid, "title": self._folder_title},
            allow=(409, 412),
        )
        confirm = self._request("GET", f"/folders/{self._folder_uid}", allow=(404,))
        if confirm.status_code != 200:
            raise GrafanaOperationFailed(confirm.status_code)

    # ------------------------------------------------------------------
    # 看板创建/删除/对账
    # ------------------------------------------------------------------

    def create_dashboard(self, uid: str, title: str) -> None:
        self._request(
            "POST",
            "/dashboards/db",
            json_body={
                "dashboard": {
                    "uid": uid,
                    "title": title,
                    "panels": [],
                    "schemaVersion": DASHBOARD_SCHEMA_VERSION,
                },
                "folderUid": self._folder_uid,
                "overwrite": False,
            },
        )

    def delete_dashboard(self, uid: str) -> bool:
        """删除；404 视为已删除（幂等成功）。其余失败抛异常。"""
        response = self._request("DELETE", f"/dashboards/uid/{uid}", allow=(404,))
        return response.status_code in (200, 404)

    def search_uids(self, uids: Iterable[str]) -> set[str]:
        """按 UID 精确对账：重复 dashboardUIDs 参数 + 显式 limit（≥块大小）。"""
        uid_list = list(uids)
        if not uid_list:
            return set()
        params: list[tuple[str, str]] = [("type", "dash-db")]
        params.extend(("dashboardUIDs", uid) for uid in uid_list)
        params.append(("limit", max(len(uid_list), SEARCH_CHUNK_SIZE)))
        payload = self._json("GET", "/search", params=params)
        if not isinstance(payload, list):
            raise GrafanaOperationFailed(200)
        found: set[str] = set()
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("uid"), str):
                raise GrafanaOperationFailed(200)
            found.add(item["uid"])
        return found

    def search_available(self) -> bool:
        """空 UID 列表时的独立可用性判定（最小 search，不带 UID 过滤）。"""
        payload = self._json(
            "GET", "/search", params=[("type", "dash-db"), ("limit", "1")]
        )
        return isinstance(payload, list)


def build_client(*, transport: httpx.BaseTransport | None = None) -> GrafanaClient:
    """按当前配置构建客户端（测试经 transport 注入 MockTransport）。"""
    return GrafanaClient(transport=transport)
