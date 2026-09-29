"""个人变量（隐私变量 + 环境变量）占位符解析与注入的单元测试。

覆盖：
- resolve_personal_refs 的替换/缺 key 报错/非 str 原样返回（privacy 与 env 两类）
- redact_personal_refs 的审计脱敏
- interface_has_personal_refs 对 dict 行与草稿对象的检测
- executor._build_kwargs 在有 actor 时注入明文、snapshot 脱敏
- executor._build_kwargs 无 actor 时占位符原样发出站
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.api_hub import executor
from app.api_hub.executor import RequestOverrides, _build_kwargs
from app.api_hub.personal_ref import (
    ENV_REF_RE,
    PERSONAL_REF_RE,
    PRIVACY_REF_RE,
    interface_has_personal_refs,
    redact_personal_refs,
    resolve_personal_refs,
)


# ---------------------------------------------------------------------------
# 纯函数：resolve_personal_refs / redact_personal_refs
# ---------------------------------------------------------------------------

class TestResolvePersonalRefs:
    """resolve_personal_refs 的替换逻辑（mock 掉 DB 查询）。"""

    def _make_user(self, uid="user-1"):
        return SimpleNamespace(id=uid, role="editor", is_active=True)

    def test_replaces_privacy_placeholder_with_plaintext(self):
        user = self._make_user()
        with patch(
            "app.api_hub.personal_ref._load_privacy_plaintext",
            return_value={"privacy:cookie": "sid=secret123"},
        ), patch(
            "app.api_hub.personal_ref._load_env_plaintext",
            return_value={},
        ):
            result = resolve_personal_refs("Cookie: {{privacy:cookie}}", user)
        assert result == "Cookie: sid=secret123"

    def test_replaces_env_placeholder_with_plaintext(self):
        user = self._make_user()
        with patch(
            "app.api_hub.personal_ref._load_privacy_plaintext",
            return_value={},
        ), patch(
            "app.api_hub.personal_ref._load_env_plaintext",
            return_value={"env:REGION": "cn-north-1"},
        ):
            result = resolve_personal_refs(
                "https://{{env:REGION}}.example.com/api", user
            )
        assert result == "https://cn-north-1.example.com/api"

    def test_replaces_mixed_placeholders_in_one_value(self):
        """同一段文本里混用 {{privacy:}} 与 {{env:}}，一次全部解析。"""
        user = self._make_user()
        with patch(
            "app.api_hub.personal_ref._load_privacy_plaintext",
            return_value={"privacy:token": "abc"},
        ), patch(
            "app.api_hub.personal_ref._load_env_plaintext",
            return_value={"env:key": "xyz"},
        ):
            result = resolve_personal_refs(
                "{{privacy:token}} + {{env:key}}", user
            )
        assert result == "abc + xyz"

    def test_replaces_multiple_placeholders(self):
        user = self._make_user()
        with patch(
            "app.api_hub.personal_ref._load_env_plaintext",
            return_value={"env:token": "abc", "env:key": "xyz"},
        ), patch(
            "app.api_hub.personal_ref._load_privacy_plaintext",
            return_value={},
        ):
            result = resolve_personal_refs(
                "{{env:token}} + {{env:key}}", user
            )
        assert result == "abc + xyz"

    def test_no_placeholder_returns_original(self):
        user = self._make_user()
        with patch(
            "app.api_hub.personal_ref._load_privacy_plaintext",
            return_value={},
        ) as mock_privacy, patch(
            "app.api_hub.personal_ref._load_env_plaintext",
            return_value={},
        ) as mock_env:
            result = resolve_personal_refs("plain string", user)
        assert result == "plain string"
        mock_privacy.assert_not_called()
        mock_env.assert_not_called()

    def test_non_string_returns_original(self):
        user = self._make_user()
        # bytes / dict / None 原样返回
        assert resolve_personal_refs(b"bytes", user) == b"bytes"
        assert resolve_personal_refs({"x": 1}, user) == {"x": 1}
        assert resolve_personal_refs(None, user) is None

    def test_missing_privacy_key_raises_value_error(self):
        """缺 key 绝不静默发空——fail-closed。"""
        user = self._make_user()
        with patch(
            "app.api_hub.personal_ref._load_privacy_plaintext",
            return_value={"privacy:have": "value"},  # 缺 privacy:missing
        ), patch(
            "app.api_hub.personal_ref._load_env_plaintext",
            return_value={},
        ):
            with pytest.raises(ValueError, match="个人变量未配置：privacy:missing"):
                resolve_personal_refs("{{privacy:missing}}", user)

    def test_missing_env_key_raises_value_error(self):
        user = self._make_user()
        with patch(
            "app.api_hub.personal_ref._load_privacy_plaintext",
            return_value={},
        ), patch(
            "app.api_hub.personal_ref._load_env_plaintext",
            return_value={},
        ):
            with pytest.raises(ValueError, match="个人变量未配置：env:missing"):
                resolve_personal_refs("{{env:missing}}", user)

    def test_regex_does_not_match_single_brace(self):
        """单括号 {param} 是 path 参数，不是个人变量引用。"""
        user = self._make_user()
        with patch(
            "app.api_hub.personal_ref._load_privacy_plaintext",
            return_value={},
        ) as mock_load:
            result = resolve_personal_refs("https://x/{id}/detail", user)
        assert result == "https://x/{id}/detail"
        # 不该查 DB
        mock_load.assert_not_called()

    def test_actor_none_skips_resolution(self):
        """无 actor（公开代理/n8n）时占位符原样返回。"""
        assert resolve_personal_refs("{{privacy:cookie}}", None) == "{{privacy:cookie}}"
        assert resolve_personal_refs("{{env:REGION}}", None) == "{{env:REGION}}"

    def test_regex_shapes(self):
        assert PRIVACY_REF_RE.findall("{{privacy:a}} {{privacy:b-c}}") == ["a", "b-c"]
        assert ENV_REF_RE.findall("{{env:a}} {{env:x_y.z}}") == ["a", "x_y.z"]
        assert PERSONAL_REF_RE.findall("{{privacy:a}} {{env:b}}") == [
            ("privacy", "a"),
            ("env", "b"),
        ]


class TestRedactPersonalRefs:
    """审计快照脱敏：占位符 → ***。"""

    def test_redacts_privacy_placeholder(self):
        assert redact_personal_refs("{{privacy:cookie}}") == "***"

    def test_redacts_env_placeholder(self):
        assert redact_personal_refs("prefix-{{env:key}}-suffix") == "prefix-***-suffix"

    def test_redacts_multiple(self):
        assert (
            redact_personal_refs("{{privacy:a}}/{{env:b}}") == "***/***"
        )

    def test_no_placeholder_unchanged(self):
        assert redact_personal_refs("plain") == "plain"

    def test_non_string_unchanged(self):
        assert redact_personal_refs(b"bytes") == b"bytes"
        assert redact_personal_refs(None) is None


class TestInterfaceHasPersonalRefs:
    """发布 / 编排前的占位符检测（dict 行与草稿对象两种形态）。"""

    def test_dict_row_url_header_body(self):
        assert interface_has_personal_refs({"url": "https://x/{{env:A}}"})
        assert interface_has_personal_refs(
            {"url": "", "headers": [{"key": "K", "value": "{{privacy:B}}"}]}
        )
        assert interface_has_personal_refs(
            {"url": "", "body_content": "{{env:C}}"}
        )

    def test_dict_row_clean(self):
        assert not interface_has_personal_refs(
            {
                "url": "https://x/{id}",
                "headers": [{"key": "K", "value": "v"}],
                "body_content": "plain",
                "query_params": [{"key": "q", "value": "{{env:NOT_CHECKED}}"}],
            }
        )
        # query 参数值不在解析范围（与执行器行为一致），不算占位符接口

    def test_draft_object(self):
        draft = SimpleNamespace(
            url="https://x",
            headers=[SimpleNamespace(key="K", value="{{env:D}}")],
            body_content="",
        )
        assert interface_has_personal_refs(draft)

    def test_empty_interface(self):
        assert not interface_has_personal_refs({})
        assert not interface_has_personal_refs(SimpleNamespace())


# ---------------------------------------------------------------------------
# executor._build_kwargs 集成：actor 注入 + snapshot 脱敏
# ---------------------------------------------------------------------------

class TestBuildKwargsPersonalInjection:
    """_build_kwargs 在有/无 actor 时的行为。"""

    def _make_iface(self, **kw):
        iface = {
            "method": "GET",
            "url": "https://up.example/api",
            "query_params": [],
            "headers": [{"key": "X-Trace", "value": "test"}],
            "body_type": "none",
            "body_content": "",
        }
        iface.update(kw)
        return iface

    def _patch_loaders(self, privacy=None, env=None):
        return (
            patch(
                "app.api_hub.personal_ref._load_privacy_plaintext",
                return_value=privacy or {},
            ),
            patch(
                "app.api_hub.personal_ref._load_env_plaintext",
                return_value=env or {},
            ),
        )

    def test_with_actor_resolves_header_and_redacts_snapshot(self):
        user = SimpleNamespace(id="u1", role="editor", is_active=True)
        iface = self._make_iface(
            headers=[{"key": "Cookie", "value": "{{privacy:cookie}}"}],
        )
        overrides = RequestOverrides(source="ui", actor=user)
        p_privacy, p_env = self._patch_loaders(privacy={"privacy:cookie": "sid=secret"})
        with p_privacy, p_env:
            kwargs, snapshot, resolved_url = _build_kwargs(iface, overrides)
        # kwargs（发往上游）含明文
        assert kwargs["headers"]["Cookie"] == "sid=secret"
        # snapshot（审计）脱敏
        snap_headers = {h["key"]: h["value"] for h in snapshot["headers"]}
        assert snap_headers["Cookie"] == "***"

    def test_with_actor_resolves_env_header_and_redacts_snapshot(self):
        user = SimpleNamespace(id="u1", role="editor", is_active=True)
        iface = self._make_iface(
            headers=[{"key": "X-Region", "value": "{{env:REGION}}"}],
        )
        overrides = RequestOverrides(source="ui", actor=user)
        p_privacy, p_env = self._patch_loaders(env={"env:REGION": "cn-north-1"})
        with p_privacy, p_env:
            kwargs, snapshot, resolved_url = _build_kwargs(iface, overrides)
        assert kwargs["headers"]["X-Region"] == "cn-north-1"
        snap_headers = {h["key"]: h["value"] for h in snapshot["headers"]}
        assert snap_headers["X-Region"] == "***"

    def test_with_actor_resolves_body_and_redacts_snapshot(self):
        user = SimpleNamespace(id="u1", role="editor", is_active=True)
        iface = self._make_iface(
            body_type="raw",
            body_content='{"token": "{{env:tk}}"}',
        )
        overrides = RequestOverrides(source="ui", actor=user)
        p_privacy, p_env = self._patch_loaders(env={"env:tk": "real-token"})
        with p_privacy, p_env:
            kwargs, snapshot, resolved_url = _build_kwargs(iface, overrides)
        # body 明文发往上游
        assert b"real-token" in kwargs["data"]
        # snapshot 脱敏
        assert "real-token" not in str(snapshot["body_content"])
        assert "***" in str(snapshot["body_content"])

    def test_without_actor_placeholder_sent_as_is(self):
        """无 actor（公开代理路径）占位符原样发出站，snapshot 仍脱敏。"""
        iface = self._make_iface(
            headers=[{"key": "Cookie", "value": "{{privacy:cookie}}"}],
        )
        overrides = RequestOverrides(source="public_proxy")
        kwargs, snapshot, resolved_url = _build_kwargs(iface, overrides)
        # 占位符原样在上游 header 里（未被解析）
        assert kwargs["headers"]["Cookie"] == "{{privacy:cookie}}"
        # snapshot 里脱敏——不泄露变量名
        snap_headers = {h["key"]: h["value"] for h in snapshot["headers"]}
        assert snap_headers["Cookie"] == "***"

    def test_without_actor_env_placeholder_sent_as_is(self):
        iface = self._make_iface(
            headers=[{"key": "X-Region", "value": "{{env:REGION}}"}],
        )
        overrides = RequestOverrides(source="n8n_internal")
        kwargs, snapshot, resolved_url = _build_kwargs(iface, overrides)
        assert kwargs["headers"]["X-Region"] == "{{env:REGION}}"
        snap_headers = {h["key"]: h["value"] for h in snapshot["headers"]}
        assert snap_headers["X-Region"] == "***"

    def test_with_actor_resolves_url_and_redacts_snapshot_url(self):
        """URL 含占位符：出站地址用解析结果，快照 URL 打码（P0-1 回归）。

        此前 run_interface 误用 snapshot["url"] 当请求地址，URL 含个人变量的
        接口发往上游的是 "***"，必然被出站校验拒绝。
        """
        user = SimpleNamespace(id="u1", role="editor", is_active=True)
        iface = self._make_iface(url="https://{{env:HOST}}/api/orders")
        overrides = RequestOverrides(source="ui", actor=user)
        p_privacy, p_env = self._patch_loaders(env={"env:HOST": "internal.example"})
        with p_privacy, p_env:
            kwargs, snapshot, resolved_url = _build_kwargs(iface, overrides)
        # 出站地址是解析后的真实 URL，不是快照里的 ***
        assert resolved_url == "https://internal.example/api/orders"
        # 快照 URL 打码，且不含变量明文
        assert snapshot["url"] == "***"
        assert "internal.example" not in json.dumps(snapshot, ensure_ascii=False)

    def test_without_actor_placeholder_url_masked_in_snapshot_only(self):
        """无 actor（公开代理路径）：URL 占位符原样发出站，快照仍打码。"""
        iface = self._make_iface(url="https://{{env:HOST}}/api/orders")
        overrides = RequestOverrides(source="public_proxy")
        kwargs, snapshot, resolved_url = _build_kwargs(iface, overrides)
        assert resolved_url == "https://{{env:HOST}}/api/orders"
        assert snapshot["url"] == "***"

    def test_missing_personal_var_raises_in_build_kwargs(self):
        """缺 key 时 _build_kwargs 抛 ValueError（被 run_interface 转成 400）。"""
        user = SimpleNamespace(id="u1", role="editor", is_active=True)
        iface = self._make_iface(
            headers=[{"key": "Cookie", "value": "{{privacy:missing}}"}],
        )
        overrides = RequestOverrides(source="ui", actor=user)
        p_privacy, p_env = self._patch_loaders()
        with p_privacy, p_env:
            with pytest.raises(ValueError, match="个人变量未配置"):
                _build_kwargs(iface, overrides)
