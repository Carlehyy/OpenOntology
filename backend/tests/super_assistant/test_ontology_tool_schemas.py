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


def test_query_schema_supports_two_level_subquery_sources():
    """subquery 派生源两层展开：outer→mid→inner，inner 仅叶子三元组。"""
    query = next(
        schema for schema in ontology_tools.ontology_tool_schemas()
        if schema["name"] == "ontology_query")
    defs = query["parameters"]["$defs"]

    outer = query["parameters"]["properties"]["sources"]["items"]["anyOf"]
    object_variant, derived_variant = outer
    assert sorted(object_variant["required"]) == [
        "alias", "objectTypeId", "ontologyId", "releaseId"]
    assert sorted(derived_variant["required"]) == ["alias", "subquery"]
    assert derived_variant["properties"]["subquery"] == {
        "$ref": "#/$defs/queryRequestMid"}

    mid = defs["queryRequestMid"]["properties"]["sources"]["items"]["anyOf"]
    assert len(mid) == 2
    assert mid[1]["properties"]["subquery"] == {
        "$ref": "#/$defs/queryRequestInner"}

    # 最内层派生请求只允许叶子对象源：不允许第三层嵌套
    inner_items = defs["queryRequestInner"]["properties"]["sources"]["items"]
    assert "anyOf" not in inner_items
    assert sorted(inner_items["required"]) == [
        "alias", "objectTypeId", "ontologyId", "releaseId"]
    # 公共查询属性在各层齐备（join/filter/select/group/agg 同一套）
    for def_name in ("queryRequestMid", "queryRequestInner"):
        properties = defs[def_name]["properties"]
        for key in ("joins", "sourceFilters", "where", "select",
                    "groupBy", "aggregates", "orderBy", "pageSize"):
            assert key in properties, (def_name, key)


def test_catalog_tool_schema_present_and_read_only():
    schemas = {schema["name"]: schema
               for schema in ontology_tools.ontology_tool_schemas()}
    assert "ontology_catalog" in schemas
    catalog = schemas["ontology_catalog"]
    assert set(catalog["parameters"]["properties"]) == {
        "operation", "query", "cursor", "ontologyId", "releaseId"}
    assert catalog["parameters"]["properties"]["operation"]["enum"] == [
        "object_types"]
    assert "ontology_catalog" in runtime._READ_ONLY_BUILTIN_TOOLS
    assert "ontology_catalog" in ontology_tools.ONTOLOGY_TOOL_NAMES
