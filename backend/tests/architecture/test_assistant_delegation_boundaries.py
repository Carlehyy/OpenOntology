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


# 规则 3：adapter 只许 import 白名单符号——防腐化成对各助手域内部的
# 任意调用面；新增依赖 = 显式修改本白名单并经域 owner review。
_ADAPTER_IMPORT_WHITELIST: dict[str, set[str]] = {
    "assistant_hub/adapters/ontology_agent.py": {
        "app.assistant_hub.contract",
        "app.auth.permissions",
        "app.ontologies.access",
        "app.ontologies.agent_runtime.chat_cancel",
        "app.ontologies.agent_runtime.models",
        "app.ontologies.agent_runtime.orchestrator",
    },
    "assistant_hub/adapters/exploration.py": {
        "app.assistant_hub.contract",
        "app.auth.permissions",
        "app.exploration.orchestrator",
        "app.exploration.schemas",
        "app.exploration.session_service",
    },
    "assistant_hub/registry.py": {
        "app.assistant_hub.adapters",
        "app.assistant_hub.adapters.exploration",
        "app.assistant_hub.adapters.ontology_agent",
        "app.assistant_hub.contract",
        "app.auth.permissions",
    },
}


def test_assistant_hub_adapters_import_whitelist_only():
    hub_dir = APP_DIR / "assistant_hub"
    assert hub_dir.exists(), "assistant_hub 域必须存在（见 AGENTS.md 域表）"
    for relative_path, allowed in _ADAPTER_IMPORT_WHITELIST.items():
        path = hub_dir / relative_path.split("/", 1)[1]
        assert path.exists(), f"缺少 {relative_path}"
        violations = [
            f"{path.relative_to(BACKEND_DIR)}:{line} imports {module}"
            for line, module in _absolute_imports(path)
            if module.startswith("app.") and module not in allowed
        ]
        assert not violations, (
            f"{relative_path} 只允许 import 白名单符号；新增依赖需显式修改"
            " _ADAPTER_IMPORT_WHITELIST 并经域 owner review:\n"
            + "\n".join(violations)
        )


def test_assistant_hub_does_not_import_super_assistant():
    """hub 保持中立：反向依赖会让"委派映射归 super_assistant"的归属失效。"""
    files = sorted((APP_DIR / "assistant_hub").rglob("*.py"))
    violations = _forbidden_imports(files, ("app.super_assistant",))

    assert not violations, (
        "assistant_hub is the neutral delegation registry and must not "
        "depend on super_assistant:\n" + "\n".join(violations)
    )


def test_assistant_domains_do_not_import_assistant_hub():
    """被委派域不得反向感知委派枢纽（依赖方向单向：hub → 域）。"""
    for domain in ("ontologies", "exploration", "scenes", "data_channel"):
        files = sorted((APP_DIR / domain).rglob("*.py"))
        violations = _forbidden_imports(files, ("app.assistant_hub",))
        assert not violations, (
            f"{domain} must not depend on app.assistant_hub "
            "(delegation is caller-side only):\n" + "\n".join(violations)
        )
