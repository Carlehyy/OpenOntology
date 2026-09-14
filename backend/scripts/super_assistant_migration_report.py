"""Inspect or explicitly backfill/rollback legacy Super Assistant facts.

The default invocation is read-only.  ``--apply`` is required for either
backfill or rollback; no legacy table is rewritten by this command.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# This is an operator-facing script documented as being invoked from
# ``backend``. Bootstrap the repository root before importing ``app`` so that
# the documented command works without requiring callers to set PYTHONPATH.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner-id", default=None)
    parser.add_argument("--migration-id", default=None)
    parser.add_argument("--apply", action="store_true", help="perform the explicitly requested mutation")
    parser.add_argument("--rollback", action="store_true", help="rollback rows tagged with --migration-id")
    args = parser.parse_args()
    if args.rollback and not args.migration_id:
        parser.error("--rollback requires --migration-id")
    if args.apply and not args.migration_id:
        parser.error("--apply requires --migration-id so the mutation is auditable")
    # Import the application only after argparse has handled ``--help`` and
    # argument errors.  Operators can inspect this entry point without a live
    # database, while real report/mutation modes still use normal app config.
    from app.shared.database import SessionLocal
    from app.super_assistant.kernel.migration_report import (
        backfill_legacy_data,
        build_legacy_migration_report,
        rollback_legacy_backfill,
    )

    db = SessionLocal()
    try:
        if args.rollback:
            payload = rollback_legacy_backfill(db, migration_id=args.migration_id, owner_id=args.owner_id, apply=args.apply)
        elif args.apply:
            payload = backfill_legacy_data(db, owner_id=args.owner_id, migration_id=args.migration_id, apply=True)
        else:
            payload = build_legacy_migration_report(db, owner_id=args.owner_id)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == "__main__":
    main()
