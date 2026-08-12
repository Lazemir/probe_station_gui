from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from probe_station_gui.design.document_snap import SnapGeometry
from probe_station_gui.design.model import DesignDocument


def test_document_snap_owns_its_interface_and_replaced_helpers() -> None:
    from probe_station_gui.design import document_snap

    assert SnapGeometry.__module__ == "probe_station_gui.design.document_snap"
    assert document_snap.__all__ == ["SnapGeometry"]
    assert not hasattr(document_snap, "LayerKey")
    assert not hasattr(document_snap, "Point2D")
    assert not hasattr(document_snap, "_project_point_to_segment")
    for name in (
        "_snap_candidate_indices",
        "_build_snap_geometry",
        "_build_snap_spatial_index",
        "_snap_grid_key",
        "_indices_from_bins",
        "_project_point_to_segment",
    ):
        assert not hasattr(DesignDocument, name)


def test_build_closes_contours_and_indexes_long_segments() -> None:
    geometry = SnapGeometry.build(
        polygons_by_layer={
            (1, 0): (np.asarray([[0.0, 0.0], [1_000.0, 0.0]], dtype=float),)
        },
        visible_layers={(1, 0)},
        bounds=(0.0, 0.0, 1_000.0, 1.0),
        grid_divisions=256,
        max_segment_cells=64,
    )

    assert geometry.vertices.tolist() == [
        [0.0, 0.0],
        [1_000.0, 0.0],
        [0.0, 0.0],
    ]
    assert geometry.segment_starts.tolist() == [
        [0.0, 0.0],
        [1_000.0, 0.0],
    ]
    assert geometry.segment_ends.tolist() == [
        [1_000.0, 0.0],
        [0.0, 0.0],
    ]
    assert geometry.long_segment_indices == (0, 1)


def test_from_arrays_retains_ownership_and_builds_the_missing_index() -> None:
    vertices = np.asarray([[0.0, 0.0], [10.0, 0.0]], dtype=float)
    starts = np.asarray([[0.0, 0.0]], dtype=float)
    ends = np.asarray([[1.0, 0.0]], dtype=float)

    geometry = SnapGeometry.from_arrays(
        vertices,
        starts,
        ends,
        bounds=(0.0, 0.0, 10.0, 10.0),
        grid_divisions=256,
        max_segment_cells=64,
    )

    assert geometry.vertices is vertices
    assert geometry.segment_starts is starts
    assert geometry.segment_ends is ends
    assert geometry.cell_size > 0.0
    assert geometry.vertex_bins
    assert geometry.segment_bins


def test_nearest_preserves_midpoint_vertex_segment_and_free_priorities() -> None:
    geometry = SnapGeometry.build(
        polygons_by_layer={
            (1, 0): (
                np.asarray(
                    [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
                    dtype=float,
                ),
            )
        },
        visible_layers={(1, 0)},
        bounds=(0.0, 0.0, 10.0, 10.0),
        grid_divisions=256,
        max_segment_cells=64,
    )

    midpoint = geometry.nearest(
        (5.0, 0.2),
        max_distance=1.0,
        vertex_priority_ratio=1.8,
    )
    vertex = geometry.nearest(
        (0.2, 0.2),
        max_distance=None,
        vertex_priority_ratio=1.8,
    )
    segment = geometry.nearest(
        (4.0, 1.0),
        max_distance=None,
        vertex_priority_ratio=1.8,
    )
    empty_points = np.empty((0, 2), dtype=float)
    empty = SnapGeometry.from_arrays(
        empty_points,
        empty_points,
        empty_points,
        bounds=(0.0, 0.0, 1.0, 1.0),
        grid_divisions=256,
        max_segment_cells=64,
    ).nearest(
        (3.0, 7.0),
        max_distance=1.0,
        vertex_priority_ratio=1.8,
    )

    assert (midpoint.mode, midpoint.point, midpoint.distance) == (
        "segment_center",
        (5.0, 0.0),
        0.2,
    )
    assert (vertex.mode, vertex.point) == ("vertex", (0.0, 0.0))
    assert (segment.mode, segment.point) == ("segment", (4.0, 0.0))
    assert (empty.mode, empty.point, empty.distance) == (
        "free",
        (3.0, 7.0),
        0.0,
    )


def test_design_document_queries_the_canonical_snap_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[float, float]] = []
    original = SnapGeometry.nearest

    def observe(
        self: SnapGeometry,
        point: tuple[float, float],
        **kwargs: object,
    ):
        calls.append(point)
        return original(self, point, **kwargs)

    monkeypatch.setattr(SnapGeometry, "nearest", observe)
    document = DesignDocument(
        path=Path("synthetic.gds"),
        library=object(),
        top_cell=object(),
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-6,
        user_unit=1e-9,
        bounds=(0.0, 0.0, 10.0, 10.0),
        polygons_by_layer={
            (1, 0): (np.asarray([[0.0, 0.0], [10.0, 0.0]], dtype=float),)
        },
        visible_layers=frozenset({(1, 0)}),
    )

    result = document.snap_point_info((4.0, 1.0))

    assert result.point == (4.0, 0.0)
    assert calls == [(4.0, 1.0)]
