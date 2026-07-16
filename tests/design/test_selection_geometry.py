from __future__ import annotations

import pytest

from probe_station_gui.design.selection_geometry import (
    PointGeometry,
    SegmentGeometry,
    SelectionRect,
    guide_snap_candidates,
    segment_intersection,
    segment_intersects_rect,
)


def test_selection_rect_normalizes_drag_and_includes_boundary() -> None:
    rect = SelectionRect.from_drag((10.0, 8.0), (-2.0, 3.0))

    assert rect == SelectionRect(left=-2.0, right=10.0, bottom=3.0, top=8.0)
    assert rect.contains((-2.0, 8.0))
    assert not rect.contains((-2.01, 8.0))


def test_point_hit_test_and_selection_use_design_coordinates() -> None:
    point = PointGeometry((4.0, 5.0))
    rect = SelectionRect.from_drag((3.0, 4.0), (5.0, 6.0))

    assert point.hit_test((4.3, 5.4), tolerance=0.5)
    assert not point.hit_test((4.4, 5.4), tolerance=0.5)
    assert point.contained_by(rect)
    assert point.crosses(rect)


def test_segment_box_requires_both_endpoints_but_cross_accepts_touch() -> None:
    rect = SelectionRect.from_drag((0.0, 0.0), (10.0, 10.0))
    inside = SegmentGeometry((2.0, 2.0), (8.0, 8.0))
    crossing = SegmentGeometry((-2.0, 5.0), (5.0, 5.0))
    outside = SegmentGeometry((-2.0, -2.0), (-1.0, -1.0))

    assert inside.contained_by(rect)
    assert inside.crosses(rect)
    assert not crossing.contained_by(rect)
    assert crossing.crosses(rect)
    assert not outside.contained_by(rect)
    assert not outside.crosses(rect)


def test_segment_hit_test_uses_finite_segment_distance() -> None:
    segment = SegmentGeometry((0.0, 0.0), (10.0, 0.0))

    assert segment.hit_test((5.0, 0.49), tolerance=0.5)
    assert not segment.hit_test((5.0, 0.51), tolerance=0.5)
    assert not segment.hit_test((11.0, 0.0), tolerance=0.5)


def test_geometries_translate_without_mutating_sources() -> None:
    point = PointGeometry((1.0, 2.0))
    segment = SegmentGeometry((1.0, 2.0), (3.0, 4.0))

    assert point.translated(5.0, -2.0) == PointGeometry((6.0, 0.0))
    assert segment.translated(5.0, -2.0) == SegmentGeometry(
        (6.0, 0.0),
        (8.0, 2.0),
    )
    assert point == PointGeometry((1.0, 2.0))
    assert segment == SegmentGeometry((1.0, 2.0), (3.0, 4.0))


def test_segment_intersection_is_finite_and_endpoint_inclusive() -> None:
    diagonal_a = SegmentGeometry((0.0, 0.0), (10.0, 10.0))
    diagonal_b = SegmentGeometry((0.0, 10.0), (10.0, 0.0))
    endpoint_touch = SegmentGeometry((10.0, 10.0), (12.0, 8.0))
    line_only_intersection = SegmentGeometry((20.0, 0.0), (20.0, 10.0))

    assert segment_intersection(diagonal_a, diagonal_b) == pytest.approx((5.0, 5.0))
    assert segment_intersection(diagonal_a, endpoint_touch) == pytest.approx(
        (10.0, 10.0)
    )
    assert segment_intersection(diagonal_a, line_only_intersection) is None


def test_parallel_and_collinear_segments_have_no_unique_intersection() -> None:
    horizontal = SegmentGeometry((0.0, 0.0), (4.0, 0.0))
    parallel = SegmentGeometry((0.0, 1.0), (4.0, 1.0))
    overlap = SegmentGeometry((2.0, 0.0), (6.0, 0.0))

    assert segment_intersection(horizontal, parallel) is None
    assert segment_intersection(horizontal, overlap) is None


def test_intersection_and_cross_selection_are_translation_invariant() -> None:
    horizontal = SegmentGeometry((0.0, 0.0), (1.0, 0.0))
    vertical = SegmentGeometry((0.5, -0.5), (0.5, 0.5))
    crossing = SegmentGeometry((-1.0, 0.5), (2.0, 0.5))
    rect = SelectionRect(0.0, 1.0, 0.0, 1.0)
    offset = 1_000_000.0
    translated_rect = SelectionRect(
        offset,
        offset + 1.0,
        offset,
        offset + 1.0,
    )

    assert segment_intersection(horizontal, vertical) == pytest.approx((0.5, 0.0))
    assert segment_intersection(
        horizontal.translated(offset, offset),
        vertical.translated(offset, offset),
    ) == pytest.approx((offset + 0.5, offset))
    assert crossing.crosses(rect)
    assert crossing.translated(offset, offset).crosses(translated_rect)


def test_translated_collinear_near_miss_does_not_become_an_intersection() -> None:
    first = SegmentGeometry((0.0, 0.0), (1.0, 0.0))
    near_miss = SelectionRect(1.01, 2.0, -0.5, 0.5)
    offset = 1_000_000_000_000.0
    translated_rect = SelectionRect(
        offset + near_miss.left,
        offset + near_miss.right,
        offset + near_miss.bottom,
        offset + near_miss.top,
    )

    assert not segment_intersects_rect(first, near_miss)
    assert not segment_intersects_rect(
        first.translated(offset, offset),
        translated_rect,
    )


def test_guide_candidates_include_endpoints_midpoints_and_intersection() -> None:
    segments = (
        SegmentGeometry((0.0, 0.0), (10.0, 10.0)),
        SegmentGeometry((0.0, 10.0), (10.0, 0.0)),
        SegmentGeometry((20.0, 0.0), (30.0, 0.0)),
    )

    candidates = guide_snap_candidates(segments)
    by_mode = {(candidate.point, candidate.mode) for candidate in candidates}

    assert ((0.0, 0.0), "guide_end") in by_mode
    assert ((10.0, 0.0), "guide_end") in by_mode
    assert ((25.0, 0.0), "guide_center") in by_mode
    assert ((5.0, 5.0), "guide_intersection") in by_mode
    assert len([candidate for candidate in candidates if candidate.point == (5.0, 5.0)]) == 1


def test_coincident_candidates_are_deduplicated_by_point_and_precedence() -> None:
    segments = (
        SegmentGeometry((0.0, 0.0), (2.0, 0.0)),
        SegmentGeometry((2.0, 0.0), (2.0, 2.0)),
    )

    candidates = guide_snap_candidates(segments)
    at_touch = [candidate for candidate in candidates if candidate.point == (2.0, 0.0)]

    assert len(at_touch) == 1
    assert at_touch[0].mode == "guide_intersection"
