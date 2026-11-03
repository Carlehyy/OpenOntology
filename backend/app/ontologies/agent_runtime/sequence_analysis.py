"""Pure, bounded sequence predicates — no IO, no query engine.

The business question is group-level: within a module/group, find any contiguous
time segment whose adjacent event gap satisfies the policy, then count distinct
subjects inside that segment. A gap resets the segment; we search all segments,
not only one greedy prefix. Duplicate subject events never count twice.

Ordered mode is subject-level: every possible start event is tried so an early
incomplete start cannot hide a later valid ordered sequence.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterable


@dataclass(frozen=True)
class SequenceEvent:
    subject: str
    at: datetime
    values: dict = field(default_factory=dict)


def segments(events: Iterable[SequenceEvent], *, max_gap_seconds: int,
             min_gap_seconds: int = 0) -> list[list[SequenceEvent]]:
    ordered = sorted(events, key=lambda event: (event.at, event.subject))
    result: list[list[SequenceEvent]] = []
    current: list[SequenceEvent] = []
    previous: SequenceEvent | None = None
    for event in ordered:
        if previous is None:
            current = [event]
        else:
            gap = (event.at - previous.at).total_seconds()
            if gap < 0 or gap > max_gap_seconds or gap < min_gap_seconds:
                result.append(current)
                current = [event]
            else:
                current.append(event)
        previous = event
    if current:
        result.append(current)
    return result


def best_segment(events: Iterable[SequenceEvent], *, max_gap_seconds: int,
                 min_gap_seconds: int = 0,
                 threshold: int) -> dict:
    best: dict | None = None
    for segment in segments(
            events, max_gap_seconds=max_gap_seconds,
            min_gap_seconds=min_gap_seconds):
        subjects = sorted({event.subject for event in segment})
        candidate = {
            "distinctSubjects": len(subjects),
            "segmentEventCount": len(segment),
            "segmentStart": segment[0].at.isoformat(),
            "segmentEnd": segment[-1].at.isoformat(),
            "sampleSubjects": subjects[:50],
            "qualifies": len(subjects) >= threshold,
        }
        if best is None or candidate["distinctSubjects"] > best["distinctSubjects"]:
            best = candidate
        if candidate["qualifies"]:
            return candidate
    return best or {
        "distinctSubjects": 0, "segmentEventCount": 0,
        "segmentStart": None, "segmentEnd": None,
        "sampleSubjects": [], "qualifies": False,
    }


def ordered_subjects(events: Iterable[SequenceEvent],
                     stages: list[Callable[[dict], bool]], *,
                     within_seconds: int | None = None) -> set[str]:
    by_subject: dict[str, list[SequenceEvent]] = {}
    for event in events:
        by_subject.setdefault(event.subject, []).append(event)
    matched: set[str] = set()
    for subject, subject_events in by_subject.items():
        ordered = sorted(subject_events, key=lambda event: event.at)
        for start in range(len(ordered)):
            progress = 0
            first_time: datetime | None = None
            last_time: datetime | None = None
            for event in ordered[start:]:
                if progress >= len(stages):
                    break
                if stages[progress](event.values):
                    progress += 1
                    first_time = first_time or event.at
                    last_time = event.at
                    if progress == len(stages):
                        break
            if progress != len(stages):
                continue
            if (within_seconds is not None and first_time is not None
                    and last_time is not None
                    and (last_time - first_time).total_seconds()
                    > within_seconds):
                continue
            matched.add(subject)
            break
    return matched


def group_sequence_result(events: Iterable[SequenceEvent], *,
                          max_gap_seconds: int, min_gap_seconds: int = 0,
                          threshold: int) -> dict:
    return best_segment(
        events, max_gap_seconds=max_gap_seconds,
        min_gap_seconds=min_gap_seconds, threshold=threshold)
