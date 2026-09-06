"""Independent Super Assistant runtime.

This package deliberately depends only on platform primitives (auth, database,
model configuration and encryption) plus the platform-assistant registry in
``app.assistant_hub``. It must not import ontology or exploration business
modules directly: invoking the platform's other assistants goes through
assistant_hub adapters only (enforced, with the single grandfathered exception,
by tests/architecture/test_assistant_delegation_boundaries.py).
"""
