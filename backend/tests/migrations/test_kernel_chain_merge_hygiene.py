"""kernel 迁移链与基线 main 撞号的合并卫生契约。

基线 main 在 0107 之上落了 ``0108_super_assistant_mcp_dev`` /
``0109_user_query_keys``；kernel 链必须编排在它们之后（0110-0117），
否则任何已升级到基线 head 的存量库都无法 ``upgrade head``
（``Can't locate revision '0109_user_query_keys'``，审查实测复现）。
"""
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


def _backend() -> Path:
    return Path(__file__).resolve().parents[2]


def _cfg(tmp_path: Path) -> Config:
    db_path = tmp_path / "kernel-chain.db"
    cfg = Config(str(_backend() / "alembic.ini"))
    cfg.set_main_option("script_location", str(_backend() / "alembic"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_single_head_and_kernel_chain_numbered_after_baseline(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    sd = ScriptDirectory(str(_backend() / "alembic"))
    heads = list(sd.get_heads())
    assert len(heads) == 1, f"expected a single alembic head, got {heads}"
    revisions = {rev.revision for rev in sd.walk_revisions()}
    # 基线 main 的两个迁移必须在本链中（合并前置条件），kernel 链不得
    # 再占用 0108/0109 槽位。
    assert "0108_super_assistant_mcp_dev" in revisions
    assert "0109_user_query_keys" in revisions
    assert "0108_super_assistant_kernel" not in revisions
    assert "0109_super_assistant_context_sources" not in revisions
    # kernel 链根必须挂在基线 head 之后，而不是与基线并列分叉。
    kernel = next(rev for rev in sd.walk_revisions() if rev.revision == "0110_super_assistant_kernel")
    assert kernel.down_revision == "0109_user_query_keys"


def test_baseline_stamped_existing_database_upgrades_to_head(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _cfg(tmp_path)
    # 模拟已升级到基线 main head 的存量业务库。
    command.stamp(cfg, "0109_user_query_keys")
    command.upgrade(cfg, "head")


def test_full_chain_upgrades_downgrades_and_replays(tmp_path, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    cfg = _cfg(tmp_path)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0107_mapping_suggestion_queue")
    command.upgrade(cfg, "head")
