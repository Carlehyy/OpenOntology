"""assistant_hub 注册表契约测试：唯一清单来源、零硬编码名单的根基。"""
from __future__ import annotations

from app.assistant_hub import registry
from app.auth.permissions import ALL_MENU_KEYS


def test_registry_keys_unique_and_menu_keys_valid():
    keys = [assistant.spec().key for assistant in registry.list_assistants()]
    assert len(keys) == len(set(keys))
    # 守卫规则 5：menu key 必须是合法菜单键，防键名漂移静默失效
    for assistant in registry.list_assistants():
        assert assistant.spec().menu_keys, assistant.spec().key
        for menu_key in assistant.spec().menu_keys:
            assert menu_key in ALL_MENU_KEYS, (assistant.spec().key, menu_key)


def test_registry_never_registers_super_assistant_itself():
    """防自递归委派：超级助手不得出现在委派目录里。"""
    keys = {assistant.spec().key for assistant in registry.list_assistants()}
    assert "super_assistant" not in keys


def test_get_assistant_roundtrip():
    for assistant in registry.list_assistants():
        assert registry.get_assistant(assistant.spec().key) is assistant
    assert registry.get_assistant("no_such_assistant") is None


def test_delegation_tool_schema_enum_follows_registry(db, admin_user):
    permitted = registry.permitted_assistants(db, admin_user)
    assert [a.spec().key for a in permitted] == [
        a.spec().key for a in registry.list_assistants()
    ]
    schema = registry.delegation_tool_schema(permitted)
    assert schema is not None
    assert schema["name"] == registry.DELEGATION_TOOL_NAME
    enum = schema["parameters"]["properties"]["assistant"]["enum"]
    assert enum == [a.spec().key for a in permitted]
    # 工具目录 = 注册表 ∩ 用户权限：schema 内容必须来自注册表元数据
    for assistant in permitted:
        assert assistant.spec().label in schema["description"]


def test_permitted_assistants_empty_for_user_without_menus(db):
    """custom 角色默认只有 overview 菜单：一个助手都不可委派。"""
    import uuid

    from app.models.user import User
    from app.services.auth_service import hash_password

    user = User(
        id=str(uuid.uuid4()), username="customer", email="c@test.com",
        password_hash=hash_password("custom123"), role="custom",
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    assert registry.permitted_assistants(db, user) == []
    assert registry.delegation_tool_schema([]) is None
