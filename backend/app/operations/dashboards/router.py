"""监控看板卡片 HTTP 边界（固定路径先于 ``/{dashboard_id}`` 注册）。

- 业务路由挂 ``menu_guard("operations.dashboard")``：进入/列表/签发看板会话
  需要该菜单授权；写操作另在 service 层校验 admin/editor；
- ``POST /grafana-session`` 续签/签发独立看板 cookie（不要求写角色）；
- ``GET /grafana-auth`` 是 nginx auth_request / Vite dev 代理的校验端点，
  **只依赖看板 cookie**（浏览器对 ``/api/…`` 请求不会携带 ``Path=/grafana/``
  cookie，代理必须显式转发原始 ``Cookie`` 头），不挂 Bearer/菜单/DB 依赖，
  因此挂在单独的 ``auth_router`` 上。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.orm import Session

from app.deps import get_current_user, get_db
from app.operations.dashboards import grafana_session, schemas, service
from app.shared.config import settings

router = APIRouter()
auth_router = APIRouter()


def _ok(data):
    return {"data": data}


def _request_is_https(request: Request) -> bool:
    """HTTPS 判断只信任平台既有可信代理契约（uvicorn behind nginx）。

    直连 scheme 或可信代理声明的 ``X-Forwarded-Proto``；不接受任意其他
    客户端头弱化 Secure 属性。
    """
    forwarded = (
        (request.headers.get("x-forwarded-proto") or "")
        .split(",", 1)[0]
        .strip()
        .lower()
    )
    return request.url.scheme == "https" or forwarded == "https"


@router.get("")
def list_dashboards(db: Session = Depends(get_db), _=Depends(get_current_user)):
    return _ok(service.list_dashboards(db))


@router.post("", status_code=201)
def create_dashboard(
    body: schemas.DashboardCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    row = service.create_dashboard(db, body, user)
    # create 成功即远端空看板存在，直接返回 ok，不再探测。
    return _ok(service.dashboard_card(row, service.STATUS_OK))


@router.post("/reorder")
def reorder_dashboards(
    body: schemas.DashboardReorder,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    rows = service.reorder_dashboards(db, body, user)
    # 重排不依赖 Grafana 可用：本地提交已完成，探测失败仅影响三态展示。
    _available, statuses = service.grafana_availability_and_statuses(
        [row.grafana_uid for row in rows]
    )
    return _ok({
        "items": [
            service.dashboard_card(
                row, statuses.get(row.grafana_uid, service.STATUS_UNKNOWN)
            )
            for row in rows
        ]
    })


@router.post("/grafana-session", status_code=204)
def issue_grafana_session(
    request: Request,
    response: Response,
    user=Depends(get_current_user),
):
    """签发/续签独立看板会话 cookie（有效期默认 8 小时）。

    续签走同一入口：要求当前平台 Bearer 与菜单授权（router 级守卫），
    不要求写角色（viewer/custom 获得菜单授权后可签发）。
    """
    token, _expires_at = grafana_session.issue_session(
        user_id=str(user.id),
        role=str(user.role or "viewer"),
        ttl_seconds=settings.grafana_session_ttl_seconds,
    )
    response.set_cookie(
        grafana_session.COOKIE_NAME,
        token,
        max_age=settings.grafana_session_ttl_seconds,
        httponly=True,
        samesite="lax",
        path=grafana_session.COOKIE_PATH,
        secure=_request_is_https(request),
    )
    return None


@auth_router.get("/grafana-auth", status_code=204)
def grafana_auth(request: Request):
    """看板 cookie 校验（nginx auth_request / Vite dev 代理调用）。

    成功 204 + ``X-Grafana-User``（共享管理员用户名）；缺失/非法/过期
    cookie 一律 401；错误响应不吐 token 或用户秘密。平台登录本身不能
    单独绕过 cookie 校验（本端点不读取 Bearer/DB）。
    """
    claims = grafana_session.verify_session(
        request.cookies.get(grafana_session.COOKIE_NAME) or ""
    )
    if claims is None:
        raise HTTPException(
            401,
            detail={
                "code": "grafana_session_invalid",
                "message": "看板会话缺失或已失效",
            },
        )
    return Response(status_code=204, headers={"X-Grafana-User": settings.grafana_admin_user})


@router.patch("/{dashboard_id}")
def update_dashboard(
    dashboard_id: str,
    body: schemas.DashboardUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    row = service.update_dashboard(db, dashboard_id, body, user)
    # 元数据更新不依赖 Grafana 可用：已提交本地修改，探测失败仅返回 unknown。
    return _ok(service.dashboard_card(row, service.dashboard_status(row.grafana_uid)))


@router.delete("/{dashboard_id}")
def delete_dashboard(
    dashboard_id: str,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    service.delete_dashboard(db, dashboard_id, user)
    return _ok({"status": "deleted", "id": dashboard_id})
