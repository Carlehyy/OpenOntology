"""Formal integration-test worker: exit immediately after a durable claim commit.

Invoked as a subprocess by the AutoLoop recovery test. It never contacts external
business services, acknowledges a message, or changes non-test resource names.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

CRASH_EXIT = 73


async def main() -> None:
    import nats
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import sessionmaker
    from app.model_registry import import_all_models
    from app.super_assistant.autoloop import store
    from app.super_assistant.autoloop.models import AutoLoopDispatch

    import_all_models()
    raw = os.environ["AUTOLOOP_CRASH_DATABASE_URL"]
    url = make_url(raw)
    schema = url.query.get("options", "").removeprefix("-csearch_path=")
    if (url.get_backend_name() != "postgresql" or url.host not in {"127.0.0.1", "localhost", "::1"}
            or not schema.startswith("al_it_") or not schema[6:].isalnum()):
        raise ValueError("crash worker requires its parent-owned loopback schema")
    nats_url = os.environ["TEST_NATS_URL"]
    if urlsplit(nats_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("crash worker requires loopback NATS")
    nc = await nats.connect(nats_url, connect_timeout=3)
    js = nc.jetstream()
    subscription = await js.pull_subscribe(os.environ["AUTOLOOP_CRASH_SUBJECT"],
        durable=os.environ["AUTOLOOP_CRASH_DURABLE"], stream="PIPELINE_TASKS")
    [message] = await subscription.fetch(1, timeout=10)
    identity = json.loads(message.data)["dispatch_id"]
    engine = create_engine(url, connect_args={"connect_timeout": 10})
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions() as db:
        claim = store.claim_dispatch(db, identity, lease_seconds=-1)
        assert claim is not None
        token = claim.lease_token
        db.commit()
        assert db.get(AutoLoopDispatch, identity).state == "claimed"
    engine.dispose()
    Path(os.environ["AUTOLOOP_CRASH_WITNESS"]).write_text(json.dumps({
        "pid": os.getpid(), "dispatchId": identity, "leaseToken": token,
        "sequence": message.metadata.sequence.stream,
    }), encoding="utf-8")
    # Deliberately bypass asyncio/NATS cleanup to exercise the broker ACK window.
    os._exit(CRASH_EXIT)


if __name__ == "__main__":
    asyncio.run(main())
