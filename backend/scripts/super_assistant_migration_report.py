"""Print the report-only legacy Super Assistant disposition as JSON."""
from __future__ import annotations

import argparse
import json

from app.shared.database import SessionLocal
from app.super_assistant.kernel.migration_report import build_legacy_migration_report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-id", default=None)
    args = parser.parse_args()
    db = SessionLocal()
    try:
        print(json.dumps(build_legacy_migration_report(db, owner_id=args.owner_id), ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
