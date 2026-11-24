"""HTTP contract for the Multica one-click entry route and the profile flag.

「打开 Multica」在部署提供了一键直达时先领一次性跳转地址，再经
multica-gateway 抵达 consume。入口只对平台默认 profile 生效，中继账号与
固定验证码只存在于服务端：不出现在任何响应里。
"""
from __future__ import annotations

import pytest

from app.shared.config import Settings, settings
from app.super_assistant import multica_sso as sso
from app.super_assistant import multica_service
from app.super_assistant.models import SuperAssistantMulticaProfile


ENTRY_PATH = "/api/v2/super-assistant/multica/entry"
SSO_EMAIL = "admin@multica.example.com"
FIXED_CODE = "246810"


@pytest.fixture
def multica_web(monkeypatch):
    monkeypatch.setattr(
        settings, "multica_default_web_url", "https://multica.example.com",
    )
    return "https://multica.example.com"


@pytest.fixture
def multica_sso_settings(monkeypatch, multica_web):
    monkeypatch.setattr(settings, "multica_sso_email", SSO_EMAIL)
    monkeypatch.setattr(settings, "multica_dev_verification_code", FIXED_CODE)
    return multica_web


def test_entry_returns_one_time_redirect_without_leaking_credentials(
    client, auth_headers, admin_user, multica_sso_settings,
):
    response = client.post(ENTRY_PATH, headers=auth_headers)

    assert response.status_code == 200
    assert set(response.json()) == {"redirect_url"}
    redirect_url = response.json()["redirect_url"]
    prefix = "https://multica.example.com/sso/entry?ticket="
    assert redirect_url.startswith(prefix)
    # 票据绑定签发它的平台用户，且能被 consume 侧验签
    claims = sso._decode_ticket(redirect_url[len(prefix):])
    assert claims is not None
    assert claims["uid"] == str(admin_user.id)
    assert FIXED_CODE not in response.text
    assert SSO_EMAIL not in response.text


def test_entry_issues_a_fresh_ticket_per_request(
    client, auth_headers, multica_sso_settings,
):
    first = client.post(ENTRY_PATH, headers=auth_headers).json()["redirect_url"]
    second = client.post(ENTRY_PATH, headers=auth_headers).json()["redirect_url"]

    assert first != second


def test_entry_requires_authentication(client, multica_sso_settings):
    assert client.post(ENTRY_PATH).status_code in {401, 403}


def test_entry_requires_super_assistant_menu(
    client, db, admin_user, auth_headers, multica_sso_settings,
):
    from app.auth.models import RoleMenuPermission

    admin_user.role = "editor"
    db.add(RoleMenuPermission(role="editor", menu_keys=["overview"]))
    db.commit()

    response = client.post(ENTRY_PATH, headers=auth_headers)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "MENU_ACCESS_DENIED"


@pytest.mark.parametrize(
    ("field", "value"),
    [("multica_sso_email", ""), ("multica_dev_verification_code", "")],
    ids=["no-account", "no-fixed-code"],
)
def test_entry_fails_closed_when_relay_is_not_configured(
    client, auth_headers, monkeypatch, multica_sso_settings, field, value,
):
    # 部署未提供中继账号/固定验证码：503，前端据此走原生 URL 直开兜底。
    monkeypatch.setattr(settings, field, value)

    response = client.post(ENTRY_PATH, headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"] == "Multica 一键进入未配置"


@pytest.mark.parametrize("web_url", [
    "javascript:alert(1)",
    "https://user:secret@multica.example.com",
    "https://multica.example.com/board",
    "https://multica.example.com/?token=leaked",
    "https://multica.example.com/#fragment",
    "not-a-url",
])
def test_entry_fails_closed_on_invalid_web_origin_without_reflection(
    client, auth_headers, monkeypatch, multica_sso_settings, web_url,
):
    monkeypatch.setattr(settings, "multica_default_web_url", web_url)

    response = client.post(ENTRY_PATH, headers=auth_headers)

    assert response.status_code == 503
    assert response.json()["detail"] == "Multica 工作台入口未配置"
    assert web_url not in response.text
    assert "secret" not in response.text


def test_runtime_settings_hold_the_relay_credentials_out_of_repr():
    assert Settings.model_fields["multica_dev_verification_code"].repr is False
    assert Settings.model_fields["multica_dev_verification_code"].default == ""
    assert Settings.model_fields["multica_sso_email"].default == ""


@pytest.mark.parametrize("code", ["12345", "12a456", "1234567", "１２３４５６"])
def test_fixed_code_must_be_exactly_six_ascii_digits(code):
    with pytest.raises(ValueError, match="exactly 6 digits"):
        Settings(_env_file=None, multica_dev_verification_code=code)


def test_blank_fixed_code_is_accepted_as_disabled():
    assert Settings(_env_file=None, multica_dev_verification_code="").multica_dev_verification_code == ""


def _profile(kind: str) -> SuperAssistantMulticaProfile:
    # 主键默认值在 flush 时才生成，纯对象需显式给出。
    return SuperAssistantMulticaProfile(
        id="profile-1", owner_id="owner-1", name="工作台", kind=kind,
        is_active=False,
    )


def test_profile_view_marks_only_the_platform_default_as_one_click(
    monkeypatch, multica_sso_settings,
):
    assert multica_service.profile_view(
        _profile(multica_service.PROFILE_KIND_PLATFORM_DEFAULT),
    ).sso_entry is True
    assert multica_service.profile_view(
        _profile(multica_service.PROFILE_KIND_CUSTOM),
    ).sso_entry is False


def test_profile_view_disables_one_click_without_relay_settings(
    monkeypatch, multica_web,
):
    monkeypatch.setattr(settings, "multica_sso_email", "")
    monkeypatch.setattr(settings, "multica_dev_verification_code", "")

    assert multica_service.profile_view(
        _profile(multica_service.PROFILE_KIND_PLATFORM_DEFAULT),
    ).sso_entry is False
