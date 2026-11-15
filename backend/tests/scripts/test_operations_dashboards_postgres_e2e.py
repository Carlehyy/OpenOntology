"""监控看板真实 PG 验收脚本的可测边界（guard/脱敏报告/写盘句柄/清理与退出码）。

脚本本身的数据库/并发验收（P01-P07）由 t6 在隔离 PostgreSQL 上以显式
DSN 执行；这里固化入口防线与报告/清理语义：

- 哨兵与环境守卫拒绝缺省/非 PG 目标，报告 schema 固定且敏感值
  （连接串/密码/库名）无法进入产物；
- ``_write_report`` 的平台语义（T6-PG-WINDOWS-REPORT-001 回归）：
  无 ``os.fchmod`` 平台（Windows）可成功原子写盘；POSIX 仍强制
  0600；任何受控失败（fchmod/fdopen 写入/fsync/replace/脱敏拒绝）都
  先释放已创建句柄（fdopen 接管前关裸 fd、接管后关文件对象）再清理
  临时文件，原异常不被掩盖、失败不产出误导的报告文件；
- t13 清理与最终结果可信度（T6-PG 清理误报回归）：一次性库用合法
  ``WITH (FORCE)`` 语法清除且只删本脚本生成库名；清理失败如实
  fail/cleanup；pass 只在清理确认后形成；最终写盘失败非零退出且不
  误用磁盘上残留的旧 pass 工件；全部用例 pass 但清理未确认（false/
  null）一律非零退出；诊断只回显固定非敏感标识（异常类型名）。
"""
from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "operations_dashboards_postgres_e2e.py"
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "operations_dashboards_postgres_e2e", _SCRIPT
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pass_report(module):
    report = module._report_template()
    report["result"] = "pass"
    report["stage"] = "complete"
    report["database_dropped"] = True
    return report


def test_guard_requires_sentinel_and_test_environment(monkeypatch):
    module = _load()
    monkeypatch.delenv(module.SENTINEL, raising=False)
    monkeypatch.setenv("ENVIRONMENT", "test")
    with pytest.raises(ValueError, match="must be 1"):
        module._guard_database_url()


def test_guard_rejects_non_postgres_default_and_credentialless_targets(monkeypatch):
    module = _load()
    monkeypatch.setenv(module.SENTINEL, "1")
    monkeypatch.setenv("ENVIRONMENT", "test")
    # 缺 DATABASE_URL / 非 PG / 无凭据 / 带 query 一律拒绝。
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(ValueError, match="must use PostgreSQL"):
        module._guard_database_url()
    for url in (
        "sqlite:///tmp/ontoprompt.db",
        "postgresql://user@pg.example.com:5432/db",
        "postgresql://user:pass@pg.example.com:5432/db?sslmode=require",
    ):
        monkeypatch.setenv("DATABASE_URL", url)
        with pytest.raises(ValueError):
            module._guard_database_url()


def test_guard_accepts_explicit_credentialled_postgres(monkeypatch):
    module = _load()
    monkeypatch.setenv(module.SENTINEL, "1")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://user:pass@pg.example.com:5432/throwaway"
    )
    assert module._guard_database_url().startswith("postgresql://")


def test_report_rejects_sensitive_values_and_schema_drift():
    module = _load()
    report = module._report_template()
    report["result"] = "pass"
    report["stage"] = "complete"
    payload = module._assert_sanitized_report(report, set())
    assert json.loads(payload)["result"] == "pass"

    sensitive = {
        "postgresql://user:secret@pg.example.com:5432/oo_dashboards_e2e_x",
        "secret-password",
        "oo_dashboards_e2e_x",
    }
    clean = module._report_template()
    clean["result"] = "pass"
    clean["stage"] = "complete"
    with pytest.raises(ValueError, match="sensitive value"):
        # 报告模板本身不含敏感值；注入一个模拟泄漏以证明守卫有效。
        clean["stub_authorized"] = 0
        clean["counts"] = {"leak": "postgresql://user:secret@pg.example.com:5432/oo_dashboards_e2e_x"}
        module._assert_sanitized_report(clean, sensitive)

    drifted = module._report_template()
    drifted["result"] = "pass"
    drifted["stage"] = "complete"
    drifted.pop("lock_wait_ms")
    with pytest.raises(ValueError, match="schema mismatch"):
        module._assert_sanitized_report(drifted, set())


def test_stub_gives_unauthorized_without_basic_auth():
    module = _load()
    with module.StubGrafanaServer() as stub:
        import httpx

        response = httpx.get(
            f"{stub.base_url}/grafana/api/user", trust_env=False, timeout=5
        )
        assert response.status_code == 401
        health = httpx.get(
            f"{stub.base_url}/grafana/api/health", trust_env=False, timeout=5
        )
        assert health.status_code == 200
        assert health.json()["database"] == "ok"


# ---------------------------------------------------------------------------
# _write_report 平台语义与资源释放（T6-PG-WINDOWS-REPORT-001 回归）
# ---------------------------------------------------------------------------


def test_write_report_success_is_atomic_sanitized_and_leaves_no_temp(tmp_path):
    module = _load()
    target = tmp_path / "nested" / "dashboards-postgres.json"
    module._write_report(target, _pass_report(module), set())
    # 结构正确且脱敏的完整报告；目录中仅剩目标文件（临时文件已替换）。
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["result"] == "pass"
    assert data["stage"] == "complete"
    assert data["database_dropped"] is True
    assert set(data) == module.REPORT_FIELDS
    assert [item.name for item in target.parent.iterdir()] == [target.name]
    # 同名目标可重复写盘（原子替换语义保持）。
    module._write_report(target, _pass_report(module), set())
    assert [item.name for item in target.parent.iterdir()] == [target.name]


def test_write_report_works_without_fchmod_platform(tmp_path, monkeypatch):
    """Windows 语义回归：没有 os.fchmod 的平台也能原子写盘（脱敏不变）。"""
    module = _load()
    monkeypatch.delattr(os, "fchmod", raising=False)
    target = tmp_path / "dashboards-postgres.json"
    module._write_report(target, _pass_report(module), set())
    assert json.loads(target.read_text(encoding="utf-8"))["result"] == "pass"
    assert [item.name for item in tmp_path.iterdir()] == [target.name]


@pytest.mark.skipif(os.name != "posix", reason="POSIX 0600 强制仅限 POSIX")
def test_write_report_posix_enforces_0600(tmp_path):
    module = _load()
    target = tmp_path / "dashboards-postgres.json"
    module._write_report(target, _pass_report(module), set())
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert [item.name for item in tmp_path.iterdir()] == [target.name]


def _counting_close(monkeypatch, closed: list[int]):
    """包装 os.close 以证明裸 fd 在接管失败前被显式关闭。"""
    real_close = os.close

    def counting(fd_value):
        closed.append(fd_value)
        real_close(fd_value)

    monkeypatch.setattr(os, "close", counting)


def test_write_report_pre_fdopen_failure_closes_raw_fd_and_cleans_temp(
    tmp_path, monkeypatch
):
    """T6-PG-WINDOWS-REPORT-001：fdopen 接管前的失败必须先关裸 fd。

    注入会在进入 fdopen 前抛错的 os.fchmod（Windows 上以 monkeypatch
    补出该属性，等价复现原事故形态）：fd 泄漏会让清理 unlink 报
    WinError 32 并遗留临时文件。
    """
    module = _load()
    target = tmp_path / "dashboards-postgres.json"

    def broken_fchmod(fd_value, mode):
        raise RuntimeError("injected fchmod failure")

    monkeypatch.setattr(os, "fchmod", broken_fchmod, raising=False)
    closed: list[int] = []
    _counting_close(monkeypatch, closed)

    with pytest.raises(RuntimeError, match="injected fchmod failure"):
        module._write_report(target, _pass_report(module), set())

    # 原异常如实向上抛（不被清理掩盖）；裸 fd 被显式关闭一次。
    assert len(closed) == 1
    # 失败不产出误导的报告文件，也不遗留任何临时文件（句柄已释放，
    # 否则 Windows 上 unlink 会被未关闭句柄挡下并留下临时文件）。
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_write_report_fdopen_failure_closes_raw_fd_and_cleans_temp(
    tmp_path, monkeypatch
):
    """fdopen 接管失败：fd 仍由 mkstemp 持有，必须被本函数关闭。"""
    module = _load()
    target = tmp_path / "dashboards-postgres.json"

    def broken_fdopen(fd_value, mode):
        # 刻意不关闭 fd：所有权仍在本函数。
        raise OSError("injected fdopen failure")

    monkeypatch.setattr(os, "fdopen", broken_fdopen)
    closed: list[int] = []
    _counting_close(monkeypatch, closed)

    with pytest.raises(OSError, match="injected fdopen failure"):
        module._write_report(target, _pass_report(module), set())

    assert len(closed) == 1
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_write_report_write_failure_releases_handle_and_cleans_temp(
    tmp_path, monkeypatch
):
    """fdopen 已接管后的写入失败：文件对象被关闭，无残留、无误导报告。"""
    module = _load()
    target = tmp_path / "dashboards-postgres.json"

    def broken_fsync(fd_value):
        raise OSError("injected fsync failure")

    monkeypatch.setattr(os, "fsync", broken_fsync)

    with pytest.raises(OSError, match="injected fsync failure"):
        module._write_report(target, _pass_report(module), set())

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
    # 句柄已释放：同一路径可立即成功重写（无悬挂句柄/锁）。
    monkeypatch.undo()
    module._write_report(target, _pass_report(module), set())
    assert json.loads(target.read_text(encoding="utf-8"))["result"] == "pass"


def test_write_report_replace_failure_propagates_and_cleans_temp(
    tmp_path, monkeypatch
):
    """原子替换失败：原 IO 异常向上抛，临时文件被清理，目标不产出。"""
    module = _load()
    target = tmp_path / "dashboards-postgres.json"

    def broken_replace(source, destination):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", broken_replace)

    with pytest.raises(OSError, match="injected replace failure"):
        module._write_report(target, _pass_report(module), set())

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
    monkeypatch.undo()
    module._write_report(target, _pass_report(module), set())
    assert json.loads(target.read_text(encoding="utf-8"))["result"] == "pass"


def test_write_report_sanitization_failure_leaves_no_files(tmp_path):
    """脱敏拒绝发生在建临时文件之前：目录保持为空，报告不产出。"""
    module = _load()
    target = tmp_path / "dashboards-postgres.json"
    leaky = _pass_report(module)
    leaky["counts"] = {"leak": "secret-password"}
    with pytest.raises(ValueError, match="sensitive value"):
        module._write_report(target, leaky, {"secret-password"})
    assert list(tmp_path.iterdir()) == []
    # schema 漂移同样在建盘前被拒。
    drifted = _pass_report(module)
    drifted.pop("lock_wait_ms")
    with pytest.raises(ValueError, match="schema mismatch"):
        module._write_report(target, drifted, set())
    assert list(tmp_path.iterdir()) == []


# ---------------------------------------------------------------------------
# t13：一次性库清理与最终结果可信度（T6-PG 清理误报回归）
# ---------------------------------------------------------------------------


class _RecordingAdminEngine:
    """最小假 admin 引擎：记录 exec_driver_sql 语句，可注入受控失败。"""

    def __init__(self, error: Exception | None = None):
        self.statements: list[str] = []
        self.error = error

    def connect(self):
        engine = self

        class _Connection:
            def exec_driver_sql(self, statement):
                if engine.error is not None:
                    raise engine.error
                engine.statements.append(statement)

        class _Context:
            def __enter__(self):
                return _Connection()

            def __exit__(self, *_exc):
                return False

        return _Context()


def _all_pass_report(module):
    """矩阵全部用例通过 + result=pass 的报告（cleanup 状态由调用方设定）。"""
    report = _pass_report(module)
    report["cases"] = {name: True for name in module.REPORT_CASES}
    report["database_created"] = True
    return report


def test_drop_throwaway_database_uses_legal_with_force_syntax():
    """清理成功路径：合法 WITH (FORCE) 语法，且只删除本脚本生成的库名。"""
    module = _load()
    engine = _RecordingAdminEngine()
    run_database = "oo_dashboards_e2e_ab12cd34ef56"
    module._drop_throwaway_database(engine, run_database)
    assert engine.statements == [
        f'DROP DATABASE "{run_database}" WITH (FORCE)'
    ]
    # 语句内不含 DSN/凭据形态；删除目标只有生成库名（不扩大）。
    assert "://" not in engine.statements[0]
    assert engine.statements[0].count("DROP DATABASE") == 1


def test_drop_throwaway_database_propagates_cleanup_failure():
    """清理失败路径：原始异常如实向上抛（由 _run finally 脱敏记录）。"""
    module = _load()
    engine = _RecordingAdminEngine(
        error=RuntimeError(
            "postgresql://user:secret@pg.example.com:5432/db secret-password"
        )
    )
    with pytest.raises(RuntimeError):
        module._drop_throwaway_database(engine, "oo_dashboards_e2e_x")
    assert engine.statements == []


def test_finalize_report_forms_pass_only_after_cleanup_confirmed():
    module = _load()
    # 矩阵全绿 + 清理确认 → pass/complete
    report = _all_pass_report(module)
    module._finalize_report(report, matrix_succeeded=True, database_dropped=True)
    assert (report["result"], report["stage"]) == ("pass", "complete")
    module._assert_sanitized_report(report, set())

    # 矩阵全绿但清理失败 → fail/cleanup（不得发布误导的 pass）
    report = _all_pass_report(module)
    module._finalize_report(report, matrix_succeeded=True, database_dropped=False)
    assert (report["result"], report["stage"]) == ("fail", "cleanup")
    module._assert_sanitized_report(report, set())

    # 矩阵未成功：保持失败现场，不改写 stage
    report = _all_pass_report(module)
    report["result"] = "fail"
    report["stage"] = "concurrency_p05"
    module._finalize_report(report, matrix_succeeded=False, database_dropped=True)
    assert (report["result"], report["stage"]) == ("fail", "concurrency_p05")


def test_main_requires_cleanup_before_zero_exit(tmp_path, monkeypatch, capsys):
    """全部 17 用例 pass + result=pass 但清理未确认（false/null）→ 非零。"""
    module = _load()
    report_path = tmp_path / "dashboards-postgres.json"
    for dropped, expected in ((False, 1), (None, 1), (True, 0)):
        report = _all_pass_report(module)
        report["database_dropped"] = dropped
        monkeypatch.setattr(module, "_run", lambda _path, result=report: result)
        capsys.readouterr()
        assert module.main(["--report", str(report_path)]) == expected
        captured = capsys.readouterr()
        assert captured.out.startswith("result=")
        assert "://" not in captured.out + captured.err

    # 用例失败时即使清理确认也非零退出。
    report = _all_pass_report(module)
    report["database_dropped"] = True
    report["cases"]["single_head"] = False
    monkeypatch.setattr(module, "_run", lambda _path, result=report: result)
    assert module.main(["--report", str(report_path)]) == 1

    # result 未到 pass 时即使清理确认也非零退出。
    report = _all_pass_report(module)
    report["database_dropped"] = True
    report["result"] = "fail"
    monkeypatch.setattr(module, "_run", lambda _path, result=report: result)
    assert module.main(["--report", str(report_path)]) == 1


def test_main_final_write_failure_is_nonzero_and_ignores_stale_pass(
    tmp_path, monkeypatch, capsys
):
    """最终写盘失败：非零退出；磁盘上残留的旧 pass 工件不得被误用。"""
    module = _load()
    report_path = tmp_path / "dashboards-postgres.json"
    # 磁盘残留上一轮的旧 pass 工件（全部用例 pass + 清理确认）。
    stale = _all_pass_report(module)
    stale["database_dropped"] = True
    module._write_report(report_path, stale, set())

    def failing_run(_path):
        raise OSError(
            "injected final write failure"
            " postgresql://user:secret@pg.example.com:5432/db secret-password"
        )

    monkeypatch.setattr(module, "_run", failing_run)
    assert module.main(["--report", str(report_path)]) == 1
    captured = capsys.readouterr()
    # 固定非敏感失败标识：只有异常类型名。
    assert "OSError" in captured.err
    assert "injected final write failure" not in captured.out + captured.err
    assert "://" not in captured.out + captured.err
    assert "secret" not in captured.out + captured.err
