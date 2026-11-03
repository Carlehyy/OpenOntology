"""Contracts shared between the run worker and the turn orchestrator.

This module deliberately imports nothing from the runtime package so the
orchestrator can accept a ``TurnExecution`` without creating a circular
dependency on the run ledger or its HTTP layer.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class TurnExecution:
    """Identity of an already-persisted user turn.

    ``user_message_id`` lets the orchestrator skip creating a second user
    message and exclude that message from the verbatim history window;
    ``is_cancelled`` is polled at orchestration checkpoints.  ``stream_model``
    is false for direct/evaluation calls that do not want token streaming.
    """

    user_message_id: str
    is_cancelled: Callable[[], bool]
    stream_model: bool = True
