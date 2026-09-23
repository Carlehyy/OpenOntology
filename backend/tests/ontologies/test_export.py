import copy

from app.models.ontology_version import OntologyVersion


def _formal_url(ontology_id: str) -> str:
    return f"/api/v2/formal/ontologies/{ontology_id}"


def _seed_portable_structure(client, auth_headers, ontology_id: str) -> dict:
    payload = {
        "objectTypes": [
            {
                "id": "ot-order",
                "name": "Order",
                "displayName": "订单",
                "primaryKey": "order_no",
                "properties": [
                    {
                        "id": "p-order-no",
                        "name": "order_no",
                        "displayName": "订单号",
                        "type": "string",
                        "required": True,
                    },
                    {
                        "id": "p-total",
                        "name": "total",
                        "displayName": "订单总额",
                        "type": "number",
                        "required": False,
                        "source": "computed",
                        "computed": True,
                        "functionId": "fn-total",
                        "dataBinding": {"mappingId": "source-only-mapping"},
                    },
                    {
                        "id": "p-supplier",
                        "name": "supplier_id",
                        "displayName": "供应商",
                        "type": "reference",
                        "required": False,
                        "referenceType": "ot-supplier",
                    },
                ],
                "positionX": 20,
                "positionY": 40,
            },
            {
                "id": "ot-supplier",
                "name": "Supplier",
                "displayName": "供应商",
                "primaryKey": "supplier_no",
                "properties": [
                    {
                        "id": "p-supplier-no",
                        "name": "supplier_no",
                        "displayName": "供应商编号",
                        "type": "string",
                        "required": True,
                    },
                ],
                "positionX": 360,
                "positionY": 40,
            },
        ],
        "linkTypes": [
            {
                "id": "lt-supplied-by",
                "name": "suppliedBy",
                "displayName": "由供应商供货",
                "sourceObjectTypeId": "ot-order",
                "targetObjectTypeId": "ot-supplier",
                "cardinality": "many-to-one",
                "properties": [],
            },
        ],
        "actions": [
            {
                "id": "act-review",
                "name": "reviewOrder",
                "displayName": "复核订单",
                "objectTypeId": "ot-order",
                "parameters": [],
                "validationFunctionId": "fn-validate",
                "requiresApproval": True,
                "rules": [
                    {
                        "id": "rule-link",
                        "type": "create_link",
                        "name": "关联供应商",
                        "enabled": True,
                        "order": 1,
                        "config": {
                            "type": "create_link",
                            "linkTypeId": "lt-supplied-by",
                            "targetSource": "parameter",
                            "targetValue": "supplier_id",
                        },
                    },
                    {
                        "id": "rule-function",
                        "type": "function",
                        "name": "计算总额",
                        "enabled": True,
                        "order": 2,
                        "config": {
                            "type": "function",
                            "functionId": "fn-total",
                            "parameterMappings": [],
                        },
                    },
                ],
            },
        ],
        "functions": [
            {
                "id": "fn-total",
                "name": "calculateTotal",
                "displayName": "计算订单总额",
                "functionType": "object",
                "language": "expression",
                "targetObjectTypeId": "ot-order",
                "parameters": [],
                "returnType": "number",
                "body": "0",
                "enabled": True,
            },
            {
                "id": "fn-validate",
                "name": "validateReview",
                "displayName": "校验订单复核",
                "functionType": "action_validation",
                "language": "expression",
                "targetActionId": "act-review",
                "parameters": [],
                "returnType": "validation_result",
                "body": "true",
                "enabled": True,
            },
        ],
        "instances": [
            {
                "id": "order-1",
                "objectTypeId": "ot-order",
                "properties": {"order_no": "PO-001", "supplier_id": "SUP-001"},
                "computed": {"total": 1200},
                "source": "manual",
            },
            {
                "id": "supplier-1",
                "objectTypeId": "ot-supplier",
                "properties": {"supplier_no": "SUP-001"},
                "computed": {},
                "source": "manual",
            },
        ],
        "linkInstances": [
            {
                "id": "link-1",
                "linkTypeId": "lt-supplied-by",
                "sourceObjectId": "order-1",
                "targetObjectId": "supplier-1",
                "properties": {},
            },
        ],
    }
    response = client.put(
        f"{_formal_url(ontology_id)}/full",
        json=payload,
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    return payload


def _export(client, auth_headers, ontology_id: str) -> tuple[dict, object]:
    response = client.get(
        f"/api/v1/ontologies/{ontology_id}/export",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    return response.json(), response


def test_export_json_contains_current_formal_structure_only(client, auth_headers, ontology):
    ontology_id = ontology["id"]
    _seed_portable_structure(client, auth_headers, ontology_id)

    package, response = _export(client, auth_headers, ontology_id)

    assert response.headers["content-type"].startswith("application/json")
    assert ".json" in response.headers["content-disposition"]
    assert package["format"] == "ontology-structure"
    assert package["formatVersion"] == 1
    assert package["ontology"]["id"] == ontology_id
    assert package["ontology"]["sourceVersion"] == ontology["version"]
    assert len(package["structure"]["objectTypes"]) == 2
    assert len(package["structure"]["linkTypes"]) == 1
    assert len(package["structure"]["actions"]) == 1
    assert len(package["structure"]["functions"]) == 2
    exported_order = next(
        item for item in package["structure"]["objectTypes"] if item["name"] == "Order"
    )
    exported_total = next(
        item for item in exported_order["properties"] if item["name"] == "total"
    )
    assert "dataBinding" not in exported_total
    assert "instances" not in package["structure"]
    assert "linkInstances" not in package["structure"]
    assert "executionLogs" not in package["structure"]


def test_export_rejects_legacy_non_json_format(client, auth_headers, ontology):
    response = client.get(
        f"/api/v1/ontologies/{ontology['id']}/export?format=ttl",
        headers=auth_headers,
    )
    assert response.status_code == 422


def test_export_import_round_trip_remaps_references_and_publishes_v0(
    client, auth_headers, ontology,
):
    source_id = ontology["id"]
    _seed_portable_structure(client, auth_headers, source_id)
    package, _ = _export(client, auth_headers, source_id)

    response = client.post(
        "/api/v1/ontologies/import",
        json=package,
        headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    imported = response.json()["data"]
    imported_id = imported["ontology"]["id"]
    assert imported_id != source_id
    assert imported["ontology"]["status"] == "published"
    assert imported["ontology"]["version"] == "v0"
    assert imported["ontology"]["current_release_id"] == imported["version"]["id"]
    assert imported["ontology"]["current_release_version"] == "v0"
    assert imported["version"]["version_number"] == "v0"
    assert imported["version"]["node_kind"] == "release"
    assert imported["version"]["lifecycle_status"] == "released"
    assert len(imported["version"]["snapshot_hash"]) == 64
    assert imported["version"]["published_at"]
    assert imported["counts"] == {
        "objectTypes": 2,
        "linkTypes": 1,
        "actions": 1,
        "functions": 2,
    }

    full = client.get(
        f"{_formal_url(imported_id)}/full",
        headers=auth_headers,
    ).json()["data"]
    assert full["instances"] == []
    assert full["linkInstances"] == []

    objects = {item["name"]: item for item in full["objectTypes"]}
    functions = {item["name"]: item for item in full["functions"]}
    action = full["actions"][0]
    link = full["linkTypes"][0]

    assert objects["Order"]["id"] != "ot-order"
    assert link["sourceObjectTypeId"] == objects["Order"]["id"]
    assert link["targetObjectTypeId"] == objects["Supplier"]["id"]
    assert functions["calculateTotal"]["targetObjectTypeId"] == objects["Order"]["id"]
    assert functions["validateReview"]["targetActionId"] == action["id"]
    assert action["objectTypeId"] == objects["Order"]["id"]
    assert action["validationFunctionId"] == functions["validateReview"]["id"]
    assert action["rules"][0]["config"]["linkTypeId"] == link["id"]
    assert action["rules"][1]["config"]["functionId"] == functions["calculateTotal"]["id"]

    order_properties = {item["name"]: item for item in objects["Order"]["properties"]}
    assert order_properties["total"]["functionId"] == functions["calculateTotal"]["id"]
    assert "dataBinding" not in order_properties["total"]
    assert order_properties["supplier_id"]["referenceType"] == objects["Supplier"]["id"]

    versions = client.get(
        f"/api/v2/ontologies/{imported_id}/versions",
        headers=auth_headers,
    ).json()
    assert versions["total"] == 1
    assert versions["current_release_id"] == imported["version"]["id"]
    assert versions["current_release_version"] == "v0"
    assert versions["data"][0]["version_number"] == "v0"
    assert versions["data"][0]["node_kind"] == "release"
    assert versions["data"][0]["lifecycle_status"] == "released"
    assert versions["data"][0]["snapshot_hash"] == imported["version"]["snapshot_hash"]

    project = client.get(
        f"/api/v1/ontologies/{imported_id}", headers=auth_headers,
    ).json()["data"]
    assert project["current_release_id"] == imported["version"]["id"]
    assert project["current_release_version"] == "v0"

    detail = client.get(
        f"/api/v2/ontologies/{imported_id}/versions/{imported['version']['id']}",
        headers=auth_headers,
    ).json()["data"]
    assert detail["snapshot"]["formal"]["objectTypes"]
    assert "instances" not in detail["snapshot"]["formal"]
    assert "linkInstances" not in detail["snapshot"]["formal"]


def test_import_auto_creates_missing_domain(client, auth_headers, ontology):
    _seed_portable_structure(client, auth_headers, ontology["id"])
    package, _ = _export(client, auth_headers, ontology["id"])
    package["ontology"]["domain"] = "跨境风险"

    response = client.post(
        "/api/v1/ontologies/import",
        json=package,
        headers=auth_headers,
    )

    assert response.status_code == 201, response.text
    assert response.json()["data"]["ontology"]["domain"] == "跨境风险"
    domains = client.get(
        "/api/v1/domains",
        headers=auth_headers,
    ).json()["data"]
    imported_domain = next(item for item in domains if item["name"] == "跨境风险")
    assert imported_domain["description"] == "由本体本地导入自动创建"


def test_import_rejects_dangling_structure_without_partial_project(
    client, auth_headers, ontology,
):
    source_id = ontology["id"]
    _seed_portable_structure(client, auth_headers, source_id)
    package, _ = _export(client, auth_headers, source_id)
    broken = copy.deepcopy(package)
    broken["ontology"]["domain"] = "校验失败不应创建"
    broken["structure"]["actions"][0]["rules"][0]["config"]["linkTypeId"] = "missing-link"

    before = client.get(
        "/api/v1/ontologies?page_size=1000",
        headers=auth_headers,
    ).json()["data"]["total"]
    response = client.post(
        "/api/v1/ontologies/import",
        json=broken,
        headers=auth_headers,
    )
    after = client.get(
        "/api/v1/ontologies?page_size=1000",
        headers=auth_headers,
    ).json()["data"]["total"]

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "ontology_import_validation_failed"
    assert after == before
    domains = client.get(
        "/api/v1/domains",
        headers=auth_headers,
    ).json()["data"]
    assert "校验失败不应创建" not in {item["name"] for item in domains}


def test_import_rejects_runtime_data_outside_structure_contract(
    client, auth_headers, ontology,
):
    _seed_portable_structure(client, auth_headers, ontology["id"])
    package, _ = _export(client, auth_headers, ontology["id"])
    package["structure"]["instances"] = []

    response = client.post(
        "/api/v1/ontologies/import",
        json=package,
        headers=auth_headers,
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


def test_importing_same_package_twice_uses_distinct_names_and_ids(
    client, auth_headers, ontology,
):
    _seed_portable_structure(client, auth_headers, ontology["id"])
    package, _ = _export(client, auth_headers, ontology["id"])

    first = client.post(
        "/api/v1/ontologies/import", json=package, headers=auth_headers,
    )
    second = client.post(
        "/api/v1/ontologies/import", json=package, headers=auth_headers,
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    first_ontology = first.json()["data"]["ontology"]
    second_ontology = second.json()["data"]["ontology"]
    assert first_ontology["id"] != second_ontology["id"]
    assert first_ontology["name"].endswith("（导入）")
    assert second_ontology["name"].endswith("（导入 2）")


# ============ 画布布局随包迁移 ============


def _seed_release_layout(client, auth_headers, db, ontology_id: str) -> tuple[dict, dict]:
    """把一组画布布局写到当前发布版本行上，模拟用户此前排版已保存。

    ``PUT /full`` 播种只推进 draft 快照，当前发布版仍是空基线，布局
    写入接口（layout API）会以"节点不属于该版本"拒绝；而用户真实路径
    是在已发布结构上排版，等价于发布版本行上已存在合法布局。布局写入
    校验本身由 test_ontology_evolution.py 覆盖，这里直接落行。
    返回（保存的布局，fixture 名 → 实际 ID 映射）。
    """
    full = client.get(
        f"{_formal_url(ontology_id)}/full", headers=auth_headers,
    ).json()["data"]
    objects = {item["name"]: item["id"] for item in full["objectTypes"]}
    action = full["actions"][0]
    order_id = objects["Order"]
    positions = {
        f"l1:{order_id}": {"x": 10.0, "y": 20.0},
        f"l2:{order_id}": {"x": 30.0, "y": 40.0},
        # 小数坐标：导出按亚像素无意义取整（50.7 → 51.0）
        objects["Supplier"]: {"x": 50.7, "y": 60.4},
        f"l2:property:{order_id}:p-order-no": {"x": 70.0, "y": 80.0},
        f"l2:action:{action['id']}": {"x": 90.0, "y": 100.0},
    }
    release = db.query(OntologyVersion).filter_by(
        id=client.get(
            f"/api/v1/ontologies/{ontology_id}", headers=auth_headers,
        ).json()["data"]["current_release_id"],
    ).one()
    release.canvas_layout = positions
    db.commit()
    return positions, {"ot-order": order_id, "ot-supplier": objects["Supplier"], "act-review": action["id"]}


def _workspace(client, auth_headers, ontology_id: str) -> dict:
    response = client.get(
        f"/api/v2/ontologies/{ontology_id}/current-release/workspace",
        headers=auth_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_export_includes_current_release_canvas_layout(
    client, auth_headers, ontology, db,
):
    ontology_id = ontology["id"]
    _seed_portable_structure(client, auth_headers, ontology_id)
    positions, _ = _seed_release_layout(client, auth_headers, db, ontology_id)

    package, _ = _export(client, auth_headers, ontology_id)

    # 小数坐标被取整为整数像素，其余原样携带
    expected = {**positions, **{
        key: {"x": float(round(value["x"])), "y": float(round(value["y"]))}
        for key, value in positions.items()
    }}
    assert package["canvasLayout"] == expected


def test_export_omits_layout_when_none_saved(client, auth_headers, ontology):
    ontology_id = ontology["id"]
    _seed_portable_structure(client, auth_headers, ontology_id)

    package, _ = _export(client, auth_headers, ontology_id)

    assert package["canvasLayout"] is None


def test_import_round_trip_carries_canvas_layout_with_remapped_ids(
    client, auth_headers, ontology, db,
):
    source_id = ontology["id"]
    _seed_portable_structure(client, auth_headers, source_id)
    _, source_ids = _seed_release_layout(client, auth_headers, db, source_id)
    package, _ = _export(client, auth_headers, source_id)

    response = client.post(
        "/api/v1/ontologies/import", json=package, headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    imported_id = response.json()["data"]["ontology"]["id"]

    workspace = _workspace(client, auth_headers, imported_id)
    objects = {item["name"]: item["id"] for item in workspace["objectTypes"]}
    action_id = workspace["actions"][0]["id"]
    assert objects["Order"] != source_ids["ot-order"]

    assert workspace["canvasLayout"] == {
        "l1:{}".format(objects["Order"]): {"x": 10.0, "y": 20.0},
        "l2:{}".format(objects["Order"]): {"x": 30.0, "y": 40.0},
        objects["Supplier"]: {"x": 51.0, "y": 60.0},
        "l2:property:{}:p-order-no".format(objects["Order"]): {"x": 70.0, "y": 80.0},
        "l2:action:{}".format(action_id): {"x": 90.0, "y": 100.0},
    }


def test_import_without_canvas_layout_field_falls_back_to_auto_layout(
    client, auth_headers, ontology,
):
    source_id = ontology["id"]
    _seed_portable_structure(client, auth_headers, source_id)
    package, _ = _export(client, auth_headers, source_id)
    package.pop("canvasLayout")

    response = client.post(
        "/api/v1/ontologies/import", json=package, headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    imported_id = response.json()["data"]["ontology"]["id"]

    workspace = _workspace(client, auth_headers, imported_id)
    assert workspace["canvasLayout"] == {}


def test_import_prunes_stale_or_foreign_layout_entries(
    client, auth_headers, ontology,
):
    from app.ontologies.export.service import _finite_position
    # bool 是 int 子类：DB 直读路径（导出端）须显式拒绝而非转成 0/1；
    # 导入路径上 Pydantic 已把它宽松转换为 1.0，属既定呈现层宽松语义。
    assert _finite_position({"x": True, "y": 1.0}) is None
    assert _finite_position({"x": 1e8, "y": 1.0}) is None
    assert _finite_position({"x": 1e7, "y": -1e7}) == {"x": 1e7, "y": -1e7}

    source_id = ontology["id"]
    _seed_portable_structure(client, auth_headers, source_id)
    package, _ = _export(client, auth_headers, source_id)
    package_objects = {
        item["name"]: item["id"]
        for item in package["structure"]["objectTypes"]
    }
    order_id = package_objects["Order"]
    supplier_id = package_objects["Supplier"]
    package["canvasLayout"] = {
        f"l1:{order_id}": {"x": 1.0, "y": 2.0},
        # 未知结构 ID：重映射无着落
        "l1:ghost-object": {"x": 3.0, "y": 4.0},
        # 数据映射画布 key：依赖 mappings，包内不迁移
        f"object:{order_id}": {"x": 5.0, "y": 6.0},
        "dataset:ds-legacy": {"x": 7.0, "y": 8.0},
        # 引用不存在属性的组合 key：重映射后与新快照不自洽
        f"l2:property:{order_id}:p-missing": {"x": 9.0, "y": 10.0},
        # 坐标非法：呈现层数据不合法
        supplier_id: {"x": "NaN", "y": 1.0},
        # 超出画布坐标幅度的有限值：落库会让前端视口计算失效
        f"l2:{supplier_id}": {"x": 1e8, "y": 1.0},
    }

    response = client.post(
        "/api/v1/ontologies/import", json=package, headers=auth_headers,
    )
    assert response.status_code == 201, response.text
    imported_id = response.json()["data"]["ontology"]["id"]

    workspace = _workspace(client, auth_headers, imported_id)
    objects = {item["name"]: item["id"] for item in workspace["objectTypes"]}
    assert workspace["canvasLayout"] == {
        "l1:{}".format(objects["Order"]): {"x": 1.0, "y": 2.0},
    }
