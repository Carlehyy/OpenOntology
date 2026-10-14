"""Migration coverage for 0128_preset_template_metadata.

0126 种入的三个预置模板 description 被统一写成内部术语
「预置模板（Homerail 移植）」；0128 改写为官方描述、补 is_preset
标记，并同步清洗 revision 的 spec_yaml（去内部代号）与回填
spec_compiled/canonical_hash（否则用户原样重存会把描述回滚，
模板卡「N 节点」徽章对预置模板也永远不显示）。
用 0127 基线（含 0126 种子）的库验证：预置模板被改写标记与回填、
用户自建模板（含与预置同名的）不受影响、降级移除列。
"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

PRESET_NAMES = ("变更评审三仲裁", "发布说明生成", "主题大纲生成")


def _alembic_config(backend: Path, db_path: Path) -> Config:
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def _rows(engine, sql: str, **params):
    with engine.begin() as connection:
        return connection.execute(text(sql), params).fetchall()


def _false(value) -> bool:
    """SQLite 下 server_default 的 false 可能以文本 'false' 落库。"""
    return str(value).strip().lower() in ("0", "false", "f")


def test_preset_metadata_upgrade_and_downgrade(tmp_path, monkeypatch):
    backend = Path(__file__).resolve().parents[2]
    db_path = tmp_path / "preset-template-metadata.db"
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _alembic_config(backend, db_path)

    # 基线：0127（含 0126 种子）+ 两条用户自建模板
    # （其一与预置模板同名但 created_by 非空——防误伤守卫的靶子）
    command.upgrade(cfg, "0127_super_assistant_quick_inputs")
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        for template_id, name, description in (
            ("tpl-custom", "我的自建流程", "自己写的"),
            ("tpl-samename", "发布说明生成", "用户自己的发布说明"),
        ):
            connection.execute(text(
                "INSERT INTO task_templates "
                "(id, name, description, latest_revision_id, created_by, created_at, updated_at) "
                "VALUES (:id, :name, :description, NULL, 'user-1', "
                " '2026-10-12 00:00:00.000000', '2026-10-12 00:00:00.000000')",
            ), {"id": template_id, "name": name, "description": description})
    seeded = _rows(
        engine, "SELECT name, description FROM task_templates WHERE created_by IS NULL")
    assert len(seeded) == 3
    assert all(description == "预置模板（Homerail 移植）"
               for _, description in seeded)

    # 升级到 head
    command.upgrade(cfg, "head")
    engine = create_engine(f"sqlite:///{db_path}")
    assert "is_preset" in {
        column["name"] for column in inspect(engine).get_columns("task_templates")}

    # 预置模板：is_preset=True + 官方描述 + revision YAML 清洗 + 编译回填
    for name in PRESET_NAMES:
        rows = _rows(engine, (
            "SELECT t.is_preset, t.description, r.spec_yaml, r.spec_compiled "
            "FROM task_templates t JOIN task_template_revisions r "
            "ON r.id = t.latest_revision_id "
            "WHERE t.name = :name AND t.created_by IS NULL"), name=name)
        assert len(rows) == 1, f"种子行 {name} 应唯一"
        is_preset, description, spec_yaml, spec_compiled = rows[0]
        assert is_preset in (1, True), f"{name} 应标记为预置"
        assert description and "Homerail" not in description
        assert "Homerail" not in (spec_yaml or ""), f"{name} 的 revision YAML 应已清洗"
        assert spec_compiled, f"{name} 应回填编译产物（节点徽章数据源）"

    # 用户自建模板（含同名）：不被标记、描述不被覆盖
    for template_id, expected in (
        ("tpl-custom", "自己写的"),
        ("tpl-samename", "用户自己的发布说明"),
    ):
        is_preset, description = _rows(engine, (
            "SELECT is_preset, description FROM task_templates WHERE id = :id"
        ), id=template_id)[0]
        assert _false(is_preset), f"{template_id} 不应被误标为预置"
        assert description == expected

    # 降级：is_preset 列移除
    command.downgrade(cfg, "0127_super_assistant_quick_inputs")
    engine = create_engine(f"sqlite:///{db_path}")
    assert "is_preset" not in {
        column["name"] for column in inspect(engine).get_columns("task_templates")}
    engine.dispose()
