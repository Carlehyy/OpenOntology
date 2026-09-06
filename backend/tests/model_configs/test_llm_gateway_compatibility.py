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


def test_strip_think_handles_plain_and_namespaced_closing_tags():
    """think 清洗行为：标准块、命名空间变体块、开头残留闭合标签。"""
    from app.model_configs.llm_gateway import _strip_think

    cases = {
        # 标准形态（DeepSeek-R1 / MiniMax）
        "<think>推理中</think>正文": "正文",
        # 命名空间变体完整块（GLM/mm 系）
        "<think>推理中</mm:think>正文": "正文",
        # 推理体已被上游剥离、仅残留闭合标签（生产实测形态）
        "</mm:think>正文": "正文",
        "  </mm:think>  正文  ": "正文",
        # 无标签：原样透传
        "普通回答，没有思考标签": "普通回答，没有思考标签",
    }
    for content, expected in cases.items():
        result = _strip_think({"content": content, "tool_calls": [], "usage": None})
        assert result["content"] == expected, content
