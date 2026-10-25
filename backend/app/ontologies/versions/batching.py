"""Keyset batches for release/trial state; never retain an entire projection."""
from __future__ import annotations

from sqlalchemy.orm import Query


RELEASE_BATCH_SIZE = 200


def query_batches(query: Query, key, *, batch_size: int = RELEASE_BATCH_SIZE):
    """Yield bounded ORM batches, including when callers update/delete rows.

    Only the unique, immutable key is used as a cursor. OFFSET is deliberately
    avoided: changing the projection during promotion must not skip rows.
    """
    if not 1 <= batch_size <= 1000:
        raise ValueError("Release batches must contain between 1 and 1000 rows")
    after = None
    while True:
        page_query = query.order_by(None)
        if after is not None:
            page_query = page_query.filter(key > after)
        page = page_query.order_by(key).limit(batch_size).all()
        if not page:
            return
        after = getattr(page[-1], key.key)
        yield page
