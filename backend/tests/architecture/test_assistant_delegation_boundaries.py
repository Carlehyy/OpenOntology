"""助手委派边界守卫。

编码 AGENTS.md 与 ``super_assistant/__init__.py`` 的依赖红线：超级助手
不得直接 import 本体/探索业务模块，对平台内其他助手的调用只能经
``app.assistant_hub`` 注册表（规则 2-5 随 assistant_hub 域落地启用）。
"""
from __future__ import annotations

import ast
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[2]
APP_DIR = BACKEND_DIR / "app"


def _absolute_imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imports.append((node.lineno, node.module))
    return imports


def _excepted(violation: str) -> tuple[str, str]:
    """把 "path:line imports module" 归一为 (path, module) 例外键。"""
    head, _, module = violation.partition(" imports ")
    path = head.split(":", 1)[0]
    return (path, module)


def _forbidden_imports(
    paths: list[Path],
    forbidden_prefixes: tuple[str, ...],
) -> list[str]:
    violations: list[str] = []
    for path in paths:
        for line, imported_module in _absolute_imports(path):
            if any(
                imported_module == prefix
                or imported_module.startswith(f"{prefix}.")
                for prefix in forbidden_prefixes
            ):
                violations.append(
                    f"{path.relative_to(BACKEND_DIR)}:{line} imports "
                    f"{imported_module}"
                )
    return violations


# 存量例外台账：守卫落地前已存在的直连。neo4j_service 是图存储基础设施
# 适配（连接/驱动），非本体业务逻辑；长期方向是下沉为平台原语或经端口注入，
# 在那之前禁止追加任何新例外。
_SUPER_ASSISTANT_IMPORT_EXCEPTIONS = {
    ("app/super_assistant/palace_graph.py", "app.ontologies.graph.neo4j_service"),
}


def test_super_assistant_does_not_import_ontology_or_exploration():
    """规则 1：超级助手包不得直接 import 本体/探索业务模块。

    对平台内其他助手的委派必须经 ``app.assistant_hub``（见
    ``super_assistant/__init__.py`` 包注释），直连会绕过注册表的白名单
    守卫与统一的权限/会话复用语义。存量例外见上方台账，只减不增。
    """
    files = sorted((APP_DIR / "super_assistant").rglob("*.py"))
    violations = [
        item
        for item in _forbidden_imports(
            files, ("app.ontologies", "app.exploration"))
        if _excepted(item) not in _SUPER_ASSISTANT_IMPORT_EXCEPTIONS
    ]

    assert not violations, (
        "super_assistant must stay independent of ontology/exploration "
        "business modules; delegate platform assistants via "
        "app.assistant_hub instead:\n" + "\n".join(violations)
    )
