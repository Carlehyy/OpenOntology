"""Single source of truth for the repository's newest Alembic head.

Every migration test that pins the single head imports this constant instead
of duplicating a literal. Update it only when a new migration becomes the
linear head; the tests still assert the exact revision and chain, so a broken
or forked migration graph cannot pass by accident.
"""
LATEST_HEAD = "0149_integrated_replica_snapshot"
