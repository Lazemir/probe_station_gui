from __future__ import annotations

import math
from pathlib import Path

import pytest

from probe_station_gui.design.klayout_geometry import plan_snap_search, select_snap
from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    SNAP_UNAVAILABLE_EXCESSIVE_COVERAGE,
    SNAP_UNAVAILABLE_INVALID,
    SNAP_UNAVAILABLE_OUTSIDE_BOUNDS,
    forward_rotate_point,
    inverse_rotate_box,
    inverse_rotate_point,
)
from probe_station_gui.design.model import SnapResult


SOURCE_BOUNDS = (0.0, 0.0, 4.0, 2.0)


def _config(turns: int) -> KLayoutConfig:
    display_bounds = SOURCE_BOUNDS if turns % 2 == 0 else (1.0, -1.0, 3.0, 3.0)
    return KLayoutConfig(
        path=Path(__file__),
        top_cell_name="TOP",
        visible_layers=frozenset({(1, 0)}),
        source_bounds=SOURCE_BOUNDS,
        display_bounds=display_bounds,
        rotation_quarter_turns=turns,
        generation=1,
    )


@pytest.mark.parametrize(
    ("turns", "expected"),
    [
        (0, (3.0, 1.0)),
        (1, (2.0, 2.0)),
        (2, (1.0, 1.0)),
        (3, (2.0, 0.0)),
    ],
)
def test_point_rotation_covers_all_quarter_turns(
    turns: int,
    expected: tuple[float, float],
) -> None:
    config = _config(turns)

    displayed = forward_rotate_point((3.0, 1.0), config)

    assert displayed == pytest.approx(expected)
    assert inverse_rotate_point(displayed, config) == pytest.approx((3.0, 1.0))


@pytest.mark.parametrize("turns", range(4))
def test_inverse_rotated_axis_aligned_box_round_trips_all_quarter_turns(
    turns: int,
) -> None:
    config = _config(turns)
    source_box = (2.25, 0.5, 3.25, 1.25)
    left, bottom, right, top = source_box
    displayed_corners = tuple(
        forward_rotate_point(point, config)
        for point in ((left, bottom), (left, top), (right, bottom), (right, top))
    )
    displayed_box = (
        min(point[0] for point in displayed_corners),
        min(point[1] for point in displayed_corners),
        max(point[0] for point in displayed_corners),
        max(point[1] for point in displayed_corners),
    )

    assert inverse_rotate_box(displayed_box, config) == pytest.approx(source_box)


def test_nearest_midpoint_wins_only_inside_radius_and_closer_than_vertex() -> None:
    result = select_snap(
        (5.0, 0.2),
        vertices=((5.0, 0.8),),
        segments=(((0.0, 0.0), (10.0, 0.0)),),
        radius=0.5,
    )

    assert result == SnapResult(
        point=(5.0, 0.0),
        mode="segment_center",
        distance=pytest.approx(0.2),
        segment_start=(0.0, 0.0),
        segment_end=(10.0, 0.0),
    )


def test_vertex_wins_when_midpoint_is_closer_but_outside_radius() -> None:
    result = select_snap(
        (5.0, 0.6),
        vertices=((5.0, 1.0),),
        segments=(((0.0, 0.0), (10.0, 0.0)),),
        radius=0.5,
    )

    assert result.mode == "vertex"
    assert result.point == pytest.approx((5.0, 1.0))
    assert result.distance == pytest.approx(0.4)


@pytest.mark.parametrize(
    ("vertex_x", "expected_mode"),
    [(1.8, "vertex"), (1.800001, "segment")],
)
def test_vertex_uses_exact_legacy_1_8_priority_ratio(
    vertex_x: float,
    expected_mode: str,
) -> None:
    result = select_snap(
        (0.0, 0.0),
        vertices=((vertex_x, 0.0),),
        segments=(((0.0, 1.0), (10.0, 1.0)),),
        radius=2.0,
    )

    assert result.mode == expected_mode


def test_vertex_priority_can_extend_beyond_the_segment_snap_radius() -> None:
    result = select_snap(
        (0.0, 0.0),
        vertices=((1.5, 0.0),),
        segments=(((0.0, 1.0), (10.0, 1.0)),),
        radius=1.1,
    )

    assert result.mode == "vertex"
    assert result.point == pytest.approx((1.5, 0.0))


def test_segment_projection_includes_full_source_segment() -> None:
    result = select_snap(
        (3.0, 1.0),
        vertices=(),
        segments=(((0.0, 0.0), (10.0, 0.0)),),
        radius=2.0,
    )

    assert result == SnapResult(
        point=(3.0, 0.0),
        mode="segment",
        distance=pytest.approx(1.0),
        segment_start=(0.0, 0.0),
        segment_end=(10.0, 0.0),
    )


def test_snap_falls_back_to_raw_float_point_without_candidates() -> None:
    result = select_snap(
        (3, 4),
        vertices=(),
        segments=(),
        radius=0.5,
    )

    assert result == SnapResult(point=(3.0, 4.0), mode="free", distance=0.0)


def test_snap_search_rejects_nonfinite_and_outside_queries() -> None:
    invalid = plan_snap_search((math.inf, 1.0), 2.0, (0.0, 0.0, 100.0, 100.0))
    outside = plan_snap_search((1_000.0, 1_000.0), 2.0, (0.0, 0.0, 100.0, 100.0))
    assert invalid.skip_reason == SNAP_UNAVAILABLE_INVALID
    assert outside.skip_reason == SNAP_UNAVAILABLE_OUTSIDE_BOUNDS


def test_snap_search_rejects_excessive_gds_coverage() -> None:
    plan = plan_snap_search(
        (50.0, 50.0),
        10_000_000.0,
        (0.0, 0.0, 100.0, 100.0),
    )
    assert plan.skip_reason == SNAP_UNAVAILABLE_EXCESSIVE_COVERAGE
    assert plan.coverage_fraction == pytest.approx(1.0)


def test_snap_search_keeps_normal_local_query_and_clips_edge() -> None:
    plan = plan_snap_search((1.0, 50.0), 4.0, (0.0, 0.0, 100.0, 100.0))
    assert plan.skip_reason is None
    assert plan.search_box == pytest.approx((0.0, 46.0, 5.0, 54.0))
