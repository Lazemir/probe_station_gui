"""Pure helpers for planning needle A-axis motion speed segments."""

from __future__ import annotations


NeedleMotionSegment = tuple[float, float, bool]


def build_needle_motion_profile_segments(
    *,
    current_lowering: float,
    target_lowering: float,
    boundary_lowering: float | None,
    fast_feedrate: float,
    slow_feedrate: float,
    min_feedrate: float,
) -> list[NeedleMotionSegment]:
    """Return physical lowering targets, feedrates, and slow-zone markers."""

    current = float(current_lowering)
    target = float(target_lowering)
    fast = max(float(min_feedrate), float(fast_feedrate))
    slow = max(float(min_feedrate), float(slow_feedrate))
    segments: list[NeedleMotionSegment] = []

    def append_segment(
        segment_target_lowering: float,
        segment_feedrate: float,
        slow_zone: bool,
    ) -> None:
        nonlocal current
        segment_target_lowering = float(segment_target_lowering)
        if abs(segment_target_lowering - current) < 1e-9:
            return
        segments.append(
            (
                segment_target_lowering,
                max(float(min_feedrate), float(segment_feedrate)),
                bool(slow_zone),
            )
        )
        current = segment_target_lowering

    if boundary_lowering is None:
        append_segment(target, fast, False)
        return segments

    boundary = float(boundary_lowering)
    if target > current:
        if current < boundary:
            append_segment(min(boundary, target), fast, False)
        append_segment(target, slow, True)
    else:
        if current > boundary:
            append_segment(max(boundary, target), slow, True)
        append_segment(target, fast, False)
    return segments
