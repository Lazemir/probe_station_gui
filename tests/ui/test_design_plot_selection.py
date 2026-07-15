from __future__ import annotations

import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.model import SnapResult
from probe_station_gui.design.selection_geometry import SegmentGeometry, SelectionRect
from probe_station_gui.design.selection_model import (
    SelectionModel,
    entities_in_rect,
    markup_entity_id,
    project_entities,
    route_entity_id,
)
from probe_station_gui.route.model import (
    MeasurementRoute,
    RouteDesignBinding,
    RoutePoint,
)
from probe_station_gui.views.design_plot_pane import _DesignPlotPane


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def pane(qt_app: QApplication) -> _DesignPlotPane:
    widget = _DesignPlotPane()
    widget._snap_distance_threshold = lambda: 20.0
    yield widget
    widget.shutdown()
    widget.deleteLater()


def _markup(tmp_path: Path, *, visible: bool = True) -> MarkupDocument:
    source = tmp_path / "chip.gds"
    source.write_bytes(b"gds")
    return (
        MarkupDocument.empty(source, visible=visible)
        .append_guide((0.0, 0.0), (10.0, 10.0), guide_id="a")
        .append_guide((0.0, 10.0), (10.0, 0.0), guide_id="b")
    )


def _route() -> MeasurementRoute:
    return MeasurementRoute(
        name="Route",
        design=RouteDesignBinding(
            path="C:/chip.gds",
            sha256="hash",
            top_cell_name="TOP",
            bounds=(0.0, 0.0, 100.0, 100.0),
            dbu=0.001,
        ),
        points=[
            RoutePoint("p001", "P001", (2.0, 3.0)),
            RoutePoint("p002", "P002", (8.0, 9.0)),
        ],
    )


def test_markup_overlay_draws_guides_endpoints_midpoints_and_intersection(
    pane: _DesignPlotPane,
    tmp_path: Path,
) -> None:
    pane.set_markup(_markup(tmp_path))

    assert pane._tool_sketch_segments == [
        ((0.0, 0.0), (10.0, 10.0)),
        ((0.0, 10.0), (10.0, 0.0)),
    ]
    assert pane._markup_snap_candidates
    intersection = [
        candidate
        for candidate in pane._markup_snap_candidates
        if candidate.mode == "guide_intersection"
    ]
    assert [candidate.point for candidate in intersection] == [(5.0, 5.0)]


def test_hidden_markup_has_no_overlay_entity_or_snap_candidate(
    pane: _DesignPlotPane,
    tmp_path: Path,
) -> None:
    hidden = _markup(tmp_path, visible=False)

    pane.set_markup(hidden)
    pane.set_selectable_entities(project_entities(_route(), hidden))

    assert pane._tool_sketch_segments == []
    assert pane._markup_snap_candidates == ()
    assert all(entity.owner.value == "route" for entity in pane._selectable_entities)
    assert pane._best_markup_snap((5.0, 5.0)) is None


def test_hiding_markup_prunes_existing_guide_entities_and_selection(
    pane: _DesignPlotPane,
    tmp_path: Path,
) -> None:
    visible = _markup(tmp_path)
    pane.set_markup(visible)
    pane.set_selectable_entities(project_entities(_route(), visible))
    pane.set_selection(
        SelectionModel(
            frozenset({route_entity_id("p001"), markup_entity_id("a")})
        )
    )

    pane.set_markup(visible.with_visibility(False))

    assert pane._selection.ids == frozenset({route_entity_id("p001")})
    assert all(
        entity.owner.value == "route" for entity in pane._selectable_entities
    )


def test_markup_snap_names_end_center_and_intersection(
    pane: _DesignPlotPane,
    tmp_path: Path,
) -> None:
    markup = MarkupDocument.empty((_markup(tmp_path)).source_path)
    markup = (
        markup.append_guide((0.0, 0.0), (10.0, 0.0), guide_id="horizontal")
        .append_guide((5.0, -5.0), (5.0, 5.0), guide_id="vertical")
    )
    pane.set_markup(markup)

    assert pane._best_markup_snap((0.1, 0.0)).mode == "guide_end"
    assert pane._best_markup_snap((9.9, 0.0)).mode == "guide_end"
    assert pane._best_markup_snap((5.0, 0.1)).mode == "guide_intersection"


def test_selection_highlight_accepts_multiple_route_points_and_guides(
    pane: _DesignPlotPane,
    tmp_path: Path,
) -> None:
    route = _route()
    markup = _markup(tmp_path)
    pane.set_probe_route(route, selected_route_point_index=-1)
    pane.set_markup(markup)
    pane.set_selectable_entities(project_entities(route, markup))

    pane.set_selection(
        SelectionModel(
            frozenset({route_entity_id("p001"), markup_entity_id("a")})
        )
    )

    route_x, route_y = pane._probe_route_selected_item.getData()
    guide_x, guide_y = pane._markup_selected_item.getData()
    assert list(zip(route_x, route_y, strict=True)) == [(2.0, 3.0)]
    assert list(zip(guide_x[:2], guide_y[:2], strict=True)) == [
        (0.0, 0.0),
        (10.0, 10.0),
    ]


def test_guide_only_selection_does_not_keep_legacy_route_highlight(
    pane: _DesignPlotPane,
    tmp_path: Path,
) -> None:
    route = _route()
    markup = _markup(tmp_path)
    pane.set_probe_route(route, selected_route_point_index=0)
    pane.set_markup(markup)
    pane.set_selectable_entities(project_entities(route, markup))

    pane.set_selection(SelectionModel(frozenset({markup_entity_id("a")})))

    route_x, route_y = pane._probe_route_selected_item.getData()
    assert len(route_x) == 0
    assert len(route_y) == 0


def test_selection_rectangle_matches_solidworks_direction_and_style(
    pane: _DesignPlotPane,
) -> None:
    pane._update_selection_rectangle((0.0, 0.0), (10.0, 5.0))
    left_pen = pane._selection_rect_item.opts["pen"]

    assert pane._selection_rect_mode == "contain"
    assert left_pen.color().name() == "#2196f3"
    assert left_pen.style() == Qt.SolidLine

    pane._update_selection_rectangle((10.0, 5.0), (0.0, 0.0))
    right_pen = pane._selection_rect_item.opts["pen"]

    assert pane._selection_rect_mode == "cross"
    assert right_pen.color().name() == "#4caf50"
    assert right_pen.style() == Qt.DashLine


def test_selection_direction_uses_containment_or_crossing(
    pane: _DesignPlotPane,
    tmp_path: Path,
) -> None:
    markup = MarkupDocument.empty((_markup(tmp_path)).source_path).append_guide(
        (-2.0, 5.0),
        (5.0, 5.0),
        guide_id="crossing",
    )
    entities = project_entities(None, markup)
    pane.set_selectable_entities(entities)
    rect = SelectionRect.from_drag((0.0, 0.0), (10.0, 10.0))

    assert entities_in_rect(entities, rect, crossing=False) == set()
    assert pane._selection_entity_ids((10.0, 10.0), (0.0, 0.0)) == {
        markup_entity_id("crossing")
    }


def test_guide_click_click_commits_and_stays_active(
    pane: _DesignPlotPane,
) -> None:
    emitted: list[tuple[tuple[float, float], tuple[float, float]]] = []
    pane.guide_requested.connect(lambda start, end: emitted.append((start, end)))
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("guide")

    pane._execute_click_action("guide_point", (), SnapResult((1.0, 2.0), "free", 0.0))
    pane._execute_click_action("guide_point", (), SnapResult((5.0, 6.0), "free", 0.0))

    assert emitted == [((1.0, 2.0), (5.0, 6.0))]
    assert pane._guide_anchor is None
    assert pane.active_design_tool == "guide"


def test_escape_cancels_only_unfinished_guide(pane: _DesignPlotPane) -> None:
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("guide")
    pane._execute_click_action("guide_point", (), SnapResult((1.0, 2.0), "free", 0.0))

    pane.cancel_active_interaction()

    assert pane._guide_anchor is None
    assert pane.active_design_tool == "guide"


def test_point_action_stays_in_point_tool(pane: _DesignPlotPane) -> None:
    emitted: list[tuple[float, float]] = []
    pane.point_requested.connect(lambda x_value, y_value: emitted.append((x_value, y_value)))
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("point")

    pane._execute_click_action("point", (), SnapResult((4.0, 5.0), "free", 0.0))

    assert emitted == [(4.0, 5.0)]
    assert pane.active_design_tool == "point"


def test_mixed_array_preview_draws_points_and_dashed_segments(
    pane: _DesignPlotPane,
) -> None:
    pane.set_mixed_array_preview(
        [(1.0, 2.0), (3.0, 4.0)],
        [SegmentGeometry((0.0, 0.0), (5.0, 0.0))],
    )

    assert pane._mixed_preview_points == [(1.0, 2.0), (3.0, 4.0)]
    assert pane._mixed_preview_segments == [
        SegmentGeometry((0.0, 0.0), (5.0, 0.0))
    ]
