from app.super_assistant.kernel.migration_report import build_legacy_migration_report


def test_legacy_report_is_explicitly_report_only(db):
    report = build_legacy_migration_report(db)
    assert report["mutated"] is False
    assert report["schema_version"] == "kernel.v1.legacy-disposition.v1"
    assert {row["source"] for row in report["rows"]} == {
        "delegation", "memory", "palace_file", "mcp_server", "skill",
    }
    memory = next(row for row in report["rows"] if row["source"] == "memory")
    assert memory["readonly"] == memory["total"]
    assert "risk=unknown" in memory["rule"]
