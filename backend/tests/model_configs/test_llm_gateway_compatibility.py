from unittest.mock import patch


def test_legacy_llm_bridge_is_the_canonical_gateway_module():
    from app.model_configs import llm_gateway
    from app.ontologies.agent_runtime import llm_bridge

    assert llm_bridge is llm_gateway

    shared_symbols = (
        "chat",
        "LLMError",
        "_safe_error_message",
        "_failure_status",
        "_record_call",
        "_strip_think",
        "_chat_openai",
        "_chat_anthropic",
    )
    for symbol in shared_symbols:
        assert getattr(llm_bridge, symbol) is getattr(llm_gateway, symbol)


def test_patching_legacy_chat_replaces_the_canonical_gateway_chat():
    from app.model_configs import llm_gateway
    from app.ontologies.agent_runtime import llm_bridge

    original_chat = llm_gateway.chat
    expected = {"content": "PONG", "tool_calls": [], "usage": None}

    with patch(
        "app.ontologies.agent_runtime.llm_bridge.chat",
        return_value=expected,
    ) as patched_chat:
        assert llm_bridge.chat is patched_chat
        assert llm_gateway.chat is patched_chat
        assert llm_gateway.chat({}, [], []) == expected

    assert llm_gateway.chat is original_chat


def test_strip_think_unified_semantics():
    """think 清洗统一语义：精确标签任意位置取其后；变体仅清开头残留。

    负例锁定两个已知边界：正文中部出现的变体字面量（教程/JSON 场景）
    与大写变体均不截断——这两类是生产误伤面，不随实现顺手放宽。
    """
    from app.model_configs.llm_gateway import strip_think_content

    cases = {
        # 精确 </think>：完整思考块，任意位置取其后（历史语义）
        "<think>推理中</think>正文": "正文",
        "序言<think>x</think>答": "答",
        # 命名空间变体：仅 content 开头的残留闭合标签（GLM/mm 系生产形态）
        "</mm:think>正文": "正文",
        "  </mm:think>  正文  ": "正文",
        "</a-b.think9:think>正文": "正文",
        # 负例：正文中部变体不截断（讨论标签的教程/JSON 字面量）
        "说明：闭合标签写作 </mm:think> 即可": "说明：闭合标签写作 </mm:think> 即可",
        "前半</x:think>后半": "前半</x:think>后半",
        # 负例：大小写敏感（无大写变体生产证据，避免误伤正文）
        "</THINK>正文": "</THINK>正文",
        # 无标签：原样透传
        "普通回答，没有思考标签": "普通回答，没有思考标签",
    }
    for content, expected in cases.items():
        assert strip_think_content(content) == expected, content


def test_strip_think_boundaries():
    """边界：None/非字符串 content 不动，仅闭合标签清洗为空串。"""
    from app.model_configs.llm_gateway import _strip_think

    assert _strip_think({"content": None, "tool_calls": [], "usage": None})["content"] is None
    assert _strip_think({"content": ["x"], "tool_calls": [], "usage": None})["content"] == ["x"]
    assert _strip_think({"content": "</mm:think>", "tool_calls": [], "usage": None})["content"] == ""


def test_provider_reuses_gateway_think_stripping():
    """超级助手 provider 与网关共用同一 think 清洗实现（不允许两套语义漂移）。"""
    from app.model_configs import llm_gateway
    from app.super_assistant import provider

    assert provider.strip_think_content is llm_gateway.strip_think_content
