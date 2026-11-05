from types import SimpleNamespace

from app.services.model_config_selector import is_vlm_config, select_llm_model_config


class FakeQuery:
    def __init__(self, configs):
        self.configs = configs

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self.configs

    def first(self):
        return self.configs[0] if self.configs else None


class FakeDB:
    def __init__(self, configs):
        self.configs = configs

    def query(self, _model):
        return FakeQuery(self.configs)


def cfg(name, models, tags=None, provider="compatible"):
    return SimpleNamespace(
        id=name,
        name=name,
        config_type="llm",
        provider=provider,
        api_base="https://example.test/v1",
        api_key_encrypted="",
        models=models,
        options={"usage_tags": tags or []},
    )


def test_text_llm_selection_skips_vlm_config_by_default():
    mimo = cfg("mimo-omni", ["mimo-v2-omni"], ["VLM提取"])
    deepseek = cfg("deepseek-v4", ["deepseek-v4-flash"])

    selected = select_llm_model_config(FakeDB([mimo, deepseek]), allow_vlm=False)

    assert selected is deepseek


def test_vlm_selection_prefers_vlm_tag():
    mimo = cfg("mimo-omni", ["mimo-v2-omni"], ["VLM提取"])
    deepseek = cfg("deepseek-v4", ["deepseek-v4-flash"])

    selected = select_llm_model_config(FakeDB([deepseek, mimo]), purpose_tags=("VLM提取",), allow_vlm=True)

    assert selected is mimo
    assert is_vlm_config(selected) is True


def test_ontology_assistant_timeout_option_does_not_override_other_callers():
    from app.model_configs.selector import llm_call_kwargs

    model = cfg("timeout-model", ["m"])
    model.options["ontology_assistant_timeout_seconds"] = "45"
    kwargs = llm_call_kwargs(model)
    assert kwargs["ontology_assistant_timeout_seconds"] == 45
    assert "timeout_seconds" not in kwargs
    assert "retry_max_attempts" not in kwargs
    model.options["ontology_assistant_timeout_seconds"] = 10000
    assert llm_call_kwargs(model)["ontology_assistant_timeout_seconds"] == 600
    for invalid in (None, "invalid", 0, -1):
        model.options["ontology_assistant_timeout_seconds"] = invalid
        assert "ontology_assistant_timeout_seconds" not in llm_call_kwargs(model)


def test_vlm_selection_prefers_omni_model_name_without_tag():
    deepseek = cfg("deepseek-v4", ["deepseek-v4-flash"])
    mimo = cfg("mimo-omni", ["mimo-v2-omni"])

    selected = select_llm_model_config(FakeDB([deepseek, mimo]), purpose_tags=("VLM提取",), allow_vlm=True)

    assert selected is mimo
