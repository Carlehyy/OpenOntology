"""YIYA-36 P2-5：浏览器受保护来源 / kernel 守卫（总闸测试在确认协议套件）。"""
from app.super_assistant import browser_tools
from app.super_assistant.kernel import runtime as kernel_runtime


def test_browser_protected_origin_rejected(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "assistant_browser_protected_origins",
                        "https://mgmt.example.com, internal.corp", raising=False)
    for url in ("https://mgmt.example.com/admin", "http://internal.corp/x"):
        result = browser_tools._protected_origin_denied(url)
        assert result is True, url
    assert browser_tools._protected_origin_denied("https://other.example") is False
    # 空配置：来源识别不猜测（生产写工具由 _guard_browser_write_tool 兜底）
    monkeypatch.setattr(settings, "assistant_browser_protected_origins",
                        "", raising=False)
    assert browser_tools._protected_origin_denied("https://any.example") is False


def test_browser_write_guard_fail_closed_on_empty_config_prod(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "assistant_browser_protected_origins",
                        "", raising=False)
    monkeypatch.setattr(settings, "environment", "production", raising=False)
    guard = browser_tools._guard_browser_write_tool(
        "conv-x", "browser_open", "https://any.example.com")
    assert guard is not None
    assert guard["error"] == "capability_boundary_unenforced"
    # 只读工具不受门禁
    assert browser_tools._guard_browser_write_tool(
        "conv-x", "browser_state", None) is None


def test_browser_write_guard_current_page_hop(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "assistant_browser_protected_origins",
                        "https://mgmt.example.com", raising=False)
    monkeypatch.setattr(
        browser_tools.browser_manager, "state",
        lambda cid, actor="agent": {"url": "https://mgmt.example.com/admin"},
        raising=False)
    guard = browser_tools._guard_browser_write_tool(
        "conv-x", "browser_click_text", None)
    assert guard is not None
    assert guard["error"] == "capability_boundary_unenforced"
    # 普通页面点击放行
    monkeypatch.setattr(
        browser_tools.browser_manager, "state",
        lambda cid, actor="agent": {"url": "https://wiki.example.com/x"},
        raising=False)
    assert browser_tools._guard_browser_write_tool(
        "conv-x", "browser_click_text", None) is None
    # 显式 URL 命中受保护来源直接拒绝（不读会话状态）
    guard = browser_tools._guard_browser_write_tool(
        "conv-x", "browser_navigate", "https://mgmt.example.com/")
    assert guard is not None


def test_kernel_rejects_ontology_write_capability():
    from app.super_assistant.kernel.contracts import ContractError

    for key in ("ontology.action.confirmed_execute",
                "ontology.execute:something", "ontology:execute"):
        try:
            kernel_runtime._reject_ontology_write_capability(key)
            raised = False
        except ContractError:
            raised = True
        assert raised, key
    # 只读能力不受影响
    kernel_runtime._reject_ontology_write_capability("ontology.query")
    kernel_runtime._reject_ontology_write_capability("model.chat")
