"""Pure sequence predicates — independent oracle tests (no DB, no engine)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.ontologies.agent_runtime.sequence_analysis import (
    SequenceEvent, best_segment, ordered_subjects, segments,
)

T0 = datetime(2026, 11, 2, 4, 0, tzinfo=timezone.utc)


def event(subject, minutes, **values):
    return SequenceEvent(subject=subject, at=T0 + timedelta(minutes=minutes),
                         values=values)


def test_segment_resets_after_gap_and_counts_distinct_subjects():
    events = [event("a", 0), event("b", 1), event("c", 2),
              event("d", 30), event("e", 31), event("f", 32)]
    result = best_segment(events, max_gap_seconds=60, threshold=3)
    assert result["qualifies"] is True
    assert result["distinctSubjects"] == 3
    assert result["segmentEventCount"] == 3
    result = best_segment(events, max_gap_seconds=60, threshold=4)
    assert result["qualifies"] is False
    assert result["distinctSubjects"] == 3


def test_duplicate_subject_in_segment_counts_once():
    events = [event("a", 0), event("a", 1), event("b", 2)]
    result = best_segment(events, max_gap_seconds=60, threshold=2)
    assert result["distinctSubjects"] == 2
    result = best_segment(events, max_gap_seconds=60, threshold=3)
    assert result["qualifies"] is False


def test_interval_policy_resets_on_too_short_or_too_long_gap():
    events = [event("a", 0), event("b", 2), event("c", 5),
              event("d", 9), event("e", 11)]
    # 2 分钟间隔过短（<3），5 分钟合法，4 分钟合法，2 分钟过短。
    result = best_segment(events, max_gap_seconds=300, min_gap_seconds=180,
                          threshold=3)
    assert result["distinctSubjects"] == 3
    assert result["qualifies"] is True


def test_single_event_segment_requires_threshold_one():
    assert best_segment([event("a", 0)], max_gap_seconds=60,
                        threshold=1)["qualifies"] is True
    assert best_segment([event("a", 0)], max_gap_seconds=60,
                        threshold=2)["qualifies"] is False


def test_segments_are_time_sorted_not_input_sorted():
    events = [event("c", 2), event("a", 0), event("b", 1)]
    assert [item.subject for item in segments(
        events, max_gap_seconds=60)[0]] == ["a", "b", "c"]


def test_ordered_tries_every_start_so_early_start_does_not_hide_later_path():
    events = [event("u", 0, stage="a"), event("u", 100, stage="b"),
              event("u", 90, stage="a"), event("u", 95, stage="b")]
    stages = [lambda values: values["stage"] == "a",
              lambda values: values["stage"] == "b"]
    assert "u" in ordered_subjects(events, stages, within_seconds=600)
    assert "u" not in ordered_subjects(
        events[:2], stages, within_seconds=600)


def test_ordered_requires_stage_order_and_distinct_subjects():
    events = [event("u", 0, stage="b"), event("u", 1, stage="a"),
              event("v", 0, stage="a"), event("v", 1, stage="b"),
              event("w", 0, stage="a")]
    stages = [lambda values: values["stage"] == "a",
              lambda values: values["stage"] == "b"]
    assert ordered_subjects(events, stages) == {"v"}
