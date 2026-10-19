"""缺陷-3 契约：本体工具 schema 键与 provider 组装契约一致。"""
from app.super_assistant import ontology_tools, runtime


def test_ontology_tool_schemas_use_provider_parameters_key():
    for schema in ontology_tools.ontology_tool_schemas():
        assert "parameters" in schema, schema["name"]
        assert "input_schema" not in schema, schema["name"]
        assert isinstance(schema["parameters"], dict)


def test_runtime_builtin_catalog_provider_compatible():
    tools = runtime._builtin_tools(False)
    for tool in tools:
        assert "parameters" in tool, tool.get("name")
