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
