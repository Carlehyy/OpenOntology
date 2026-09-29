"""/api/v2/test-data 只允许在非 production 环境注册。

它是 Docker 内 REST Connector 演示用的固定认证 fixture，不是业务数据源。
生产不注册的契约由两层测试固定：
- AST 层：main.py 里的 include_router 必须被 ``settings.environment != "production"``
  守卫（防止后续改动把守卫拆掉）；
- 运行时层：当前（非生产）进程的 app 必须仍暴露该路由。
"""

from __future__ import annotations

import ast
from pathlib import Path

MAIN_PATH = (
    Path(__file__).resolve().parents[2] / "app" / "main.py"
)


def _test_data_guarded_by_environment() -> bool:
    tree = ast.parse(MAIN_PATH.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        # 允许 `settings.environment != "production"` 或反向写法
        is_neq_production = (
            isinstance(test, ast.Compare)
            and isinstance(test.ops[0], ast.NotEq)
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "production"
        )
        if not is_neq_production:
            continue
        for stmt in node.body:
            for call in ast.walk(stmt):
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "include_router"
                    and any(
                        isinstance(arg, ast.Attribute)
                        and "test_data" in ast.unparse(arg)
                        for arg in call.args
                    )
                ):
                    return True
    return False


def test_test_data_router_registration_is_gated_by_environment():
    assert _test_data_guarded_by_environment(), (
        "app/main.py 中 /api/v2/test-data 的 include_router 必须保留在 "
        "`if settings.environment != \"production\":` 守卫内——它是演示 "
        "fixture，生产进程不得注册"
    )


def test_test_data_router_present_in_non_production_app():
    from app.main import app

    paths = {getattr(route, "path", "") for route in app.routes}
    assert any(path.startswith("/api/v2/test-data") for path in paths), (
        "非 production 环境应保留 /api/v2/test-data 供 REST Connector 演示试拉；"
        "当前进程 environment 非 production 却未注册"
    )
