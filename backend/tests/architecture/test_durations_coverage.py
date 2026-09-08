"""pytest-split 时长表覆盖守卫（本地镜像，与部署工作流同语义）。

CI 按时长表把后端回归分片；新增/删除测试后未重录 ``.test_durations``
会在部署工作流的 Test durations coverage guard 被拦截。此测试把同一
检查带进本地 ``uv run pytest``，消除"本地全绿、CI 拦截"的错位。
"""
import json
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def test_all_collected_tests_have_duration_records():
    collected = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "--ignore",
            "tests/v2/perf",
        ],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    ids = {line.strip() for line in collected.splitlines() if "::" in line}
    known = set(
        json.loads((BACKEND_ROOT / ".test_durations").read_text(encoding="utf-8"))
    )
    missing = sorted(ids - known)
    assert not missing, (
        f"{len(missing)} 个测试缺时长记录：全量跑一次 "
        f"`pytest --store-durations --clean-durations` 再合入。示例：{missing[:5]}"
    )
