from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path

import pytest

from probe_station_gui.design.markup import GuideSegment, MarkupDocument
from probe_station_gui.design.navigation_bounds import (
    build_navigation_bounds,
    clamp_view_bounds,
    content_bounds,
    fit_bounds_to_aspect,
)
from probe_station_gui.route.model import (
    MeasurementRoute,
    NeedleOffset,
    RouteDesignBinding,
    RoutePoint,
)


def _route() -> MeasurementRoute:
    return MeasurementRoute(
        name="Route",
        design=RouteDesignBinding(
            path="chip.gds",
            sha256="hash",
            top_cell_name="TOP",
            bounds=(0.0, 0.0, 100.0, 50.0),
            dbu=0.001,
        ),
        points=[
            RoutePoint("p1", "P1", (20.0, 10.0), enabled=True),
            RoutePoint("p2", "P2", (240.0, -30.0), enabled=False),
        ],
        needle_offsets=[NeedleOffset("N1", "Needle 1", 5.0, -7.0)],
    )


def _hidden_markup(tmp_path: Path) -> MarkupDocument:
    source = tmp_path / "chip.gds"
    source.write_bytes(b"gds")
    return MarkupDocument.empty(source, visible=False).append_guide(
        (-80.0, 12.0),
        (10.0, 300.0),
        guide_id="far-guide",
    )


def test_content_union_includes_disabled_route_hits_and_hidden_markup(
    tmp_path: Path,
) -> None:
    bounds, ignored = content_bounds(
        (0.0, 0.0, 100.0, 50.0),
        _route(),
        _hidden_markup(tmp_path),
    )

    assert bounds == pytest.approx((-80.0, -37.0, 245.0, 300.0))
    assert ignored == 0


def test_nonfinite_content_is_ignored_and_counted(tmp_path: Path) -> None:
    route = _route()
    route.points.append(RoutePoint("bad", "Bad", (math.inf, 4.0)))

    bounds, ignored = content_bounds(
        (0.0, 0.0, 100.0, 50.0),
        route,
        _hidden_markup(tmp_path),
    )

    assert bounds == pytest.approx((-80.0, -37.0, 245.0, 300.0))
    assert ignored == 2  # invalid center and its invalid needle hit


def test_structurally_malformed_route_and_markup_are_ignored(tmp_path: Path) -> None:
    route = _route()
    route.points = [RoutePoint("bad", "Bad", (1.0,))]  # type: ignore[arg-type]
    markup = _hidden_markup(tmp_path)
    markup = replace(
        markup,
        guides=(GuideSegment("bad-guide", (1.0,), (2.0, 3.0)),),
    )

    bounds, ignored = content_bounds(
        (0.0, 0.0, 100.0, 50.0),
        route,
        markup,
    )

    assert bounds == (0.0, 0.0, 100.0, 50.0)
    assert ignored == 3  # invalid center, derived hit, and guide start


def test_gds_only_bounds_receive_proportional_padding() -> None:
    result = build_navigation_bounds(
        (0.0, 0.0, 100.0, 50.0),
        None,
        None,
        viewport_size=(400.0, 200.0),
    )

    assert result.content_bounds == (0.0, 0.0, 100.0, 50.0)
    assert result.frame == pytest.approx((-5.0, -2.5, 105.0, 52.5))


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((400.0, 200.0), (-5.0, -2.5, 105.0, 52.5)),
        ((200.0, 400.0), (-5.0, -85.0, 105.0, 135.0)),
    ],
)
def test_aspect_fit_expands_only_the_short_axis(size, expected) -> None:
    assert fit_bounds_to_aspect((-5.0, -2.5, 105.0, 52.5), size) == pytest.approx(
        expected
    )


def test_clamp_preserves_valid_view_and_translates_invalid_view() -> None:
    frame = (0.0, 0.0, 100.0, 50.0)
    assert clamp_view_bounds((10.0, 5.0, 40.0, 20.0), frame) == (
        10.0,
        5.0,
        40.0,
        20.0,
    )
    assert clamp_view_bounds((90.0, 40.0, 120.0, 55.0), frame) == pytest.approx(
        (70.0, 35.0, 100.0, 50.0)
    )


def test_clamp_reduces_oversized_view_without_changing_aspect() -> None:
    assert clamp_view_bounds((-50.0, -50.0, 150.0, 50.0), (0.0, 0.0, 100.0, 50.0)) == pytest.approx(
        (0.0, 0.0, 100.0, 50.0)
    )


def test_build_navigation_bounds_adds_named_padding(tmp_path: Path) -> None:
    result = build_navigation_bounds(
        (0.0, 0.0, 100.0, 50.0),
        _route(),
        _hidden_markup(tmp_path),
        viewport_size=(800.0, 600.0),
    )

    assert result.content_bounds == pytest.approx((-80.0, -37.0, 245.0, 300.0))
    assert result.frame[0] < result.content_bounds[0]
    assert result.frame[1] < result.content_bounds[1]
    assert result.frame[2] > result.content_bounds[2]
    assert result.frame[3] > result.content_bounds[3]
