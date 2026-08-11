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
from probe_station_gui.design.model import DesignDocument, SnapResult
from probe_station_gui.design.plot_interaction import (
    PlotAction,
    SelectionPreview,
    SnapClickIntent,
)
from probe_station_gui.design.snap_protocol import ClickPublication
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
from probe_station_gui.views.design_layout_window import DesignLayoutWindow
from probe_station_gui.design.focus_candidate import FocusCandidate
from probe_station_gui.views.design_plot_pane import _DesignPlotPane


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def pane(qt_app: QApplication, tmp_path: Path) -> _DesignPlotPane:
    widget = _DesignPlotPane()
    widget._viewport.snap_distance = lambda _radius_px: 20.0
    widget.set_document(
        DesignDocument(
            path=tmp_path / "selection-canvas.gds",
            library=None,
            top_cell=None,
            top_cell_name="TOP",
            cell_names=("TOP",),
            dbu=1e-6,
            user_unit=1e-9,
            bounds=(0.0, 0.0, 100.0, 50.0),
            polygons_by_layer={},
            visible_layers=frozenset(),
        )
    )
    yield widget
    widget.shutdown()
    widget.deleteLater()


def _complete_click(
    pane: _DesignPlotPane,
    action: PlotAction,
    result: SnapResult,
    *,
    payload: tuple[object, ...] = (),
    shift: bool = False,
    control: bool = False,
) -> None:
    intent = SnapClickIntent(
        action=action,
        raw_point=result.point,
        payload=payload,
        shift=shift,
        control=control,
        generation=pane._plot_interaction.generation,
    )
    pane._apply_click_publication(ClickPublication(intent, result))


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


def test_markup_overlay_draws_only_guides(
    pane: _DesignPlotPane,
    tmp_path: Path,
) -> None:
    pane.set_markup(_markup(tmp_path))

    assert pane._presentation.plan.tool_sketch_segments == (
        ((0.0, 0.0), (10.0, 10.0)),
        ((0.0, 10.0), (10.0, 0.0)),
    )
    assert pane._renderer.state.sketch_candidate_count == 0


def test_hidden_markup_has_no_overlay_or_entity(
    pane: _DesignPlotPane,
    tmp_path: Path,
) -> None:
    hidden = _markup(tmp_path, visible=False)

    pane.set_markup(hidden)
    pane.set_selectable_entities(project_entities(_route(), hidden))

    assert pane._presentation.plan.tool_sketch_segments == ()
    assert all(
        entity.owner.value == "route"
        for entity in pane._presentation.selectable_entities
    )


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

    assert pane._presentation.plan.selection.ids == frozenset(
        {route_entity_id("p001")}
    )
    assert all(
        entity.owner.value == "route"
        for entity in pane._presentation.selectable_entities
    )


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

    state = pane._renderer.state
    assert state.selected_route_centers == ((2.0, 3.0),)
    assert state.selected_markup_segments == (
        ((0.0, 0.0), (10.0, 10.0)),
    )


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

    assert pane._renderer.state.selected_route_centers == ()


def test_selection_rectangle_matches_solidworks_direction_and_style(
    pane: _DesignPlotPane,
) -> None:
    pane._renderer.set_selection_preview(
        SelectionPreview((0.0, 0.0), (10.0, 5.0), False)
    )

    assert pane._renderer.state.selection_rectangle_mode == "contain"
    assert pane._renderer.state.selection_rectangle_color == "#2196f3"

    pane._renderer.set_selection_preview(
        SelectionPreview((10.0, 5.0), (0.0, 0.0), True)
    )

    assert pane._renderer.state.selection_rectangle_mode == "cross"
    assert pane._renderer.state.selection_rectangle_color == "#4caf50"


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
    assert entities_in_rect(entities, rect, crossing=True) == {
        markup_entity_id("crossing")
    }


def test_guide_click_click_commits_and_stays_active(
    pane: _DesignPlotPane,
) -> None:
    emitted: list[tuple[tuple[float, float], tuple[float, float]]] = []
    pane.guide_requested.connect(lambda start, end: emitted.append((start, end)))
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("guide")

    _complete_click(
        pane,
        PlotAction.GUIDE_POINT,
        SnapResult((1.0, 2.0), "free", 0.0),
    )
    _complete_click(
        pane,
        PlotAction.GUIDE_POINT,
        SnapResult((5.0, 6.0), "free", 0.0),
    )

    assert emitted == [((1.0, 2.0), (5.0, 6.0))]
    assert pane.guide_anchor is None
    assert pane.active_design_tool == "guide"


def test_shift_constrains_guide_preview_and_commit_to_same_endpoint(
    pane: _DesignPlotPane,
) -> None:
    emitted: list[tuple[tuple[float, float], tuple[float, float]]] = []
    pane.guide_requested.connect(lambda start, end: emitted.append((start, end)))
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("guide")
    _complete_click(
        pane,
        PlotAction.GUIDE_POINT,
        SnapResult((1.0, 2.0), "free", 0.0),
    )
    result = SnapResult((6.0, 4.0), "vertex", 0.1)

    pane._apply_interaction_transition(
        pane._plot_interaction.hover(result, shift=True, control=False)
    )

    assert pane._presentation.plan.tool_sketch_points == (
        (1.0, 2.0),
        (6.0, 2.0),
    )
    _complete_click(
        pane,
        PlotAction.GUIDE_POINT,
        result,
        shift=True,
        control=False,
    )
    assert emitted == [((1.0, 2.0), (6.0, 2.0))]


@pytest.mark.parametrize(
    ("shift", "control", "raw", "expected"),
    [
        (False, True, (6.0, 6.0), (5.5, 6.5)),
        (True, True, (6.0, 4.0), (6.0, 4.0)),
    ],
)
def test_guide_uses_ctrl_diagonal_and_shift_ctrl_free(
    pane: _DesignPlotPane,
    shift: bool,
    control: bool,
    raw: tuple[float, float],
    expected: tuple[float, float],
) -> None:
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("guide")
    _complete_click(
        pane,
        PlotAction.GUIDE_POINT,
        SnapResult((1.0, 2.0), "free", 0.0),
    )

    pane._apply_interaction_transition(
        pane._plot_interaction.hover(
            SnapResult(raw, "vertex", 0.1),
            shift=shift,
            control=control,
        )
    )

    assert pane._presentation.plan.tool_sketch_points[-1] == pytest.approx(expected)


def test_escape_cancels_only_unfinished_guide(pane: _DesignPlotPane) -> None:
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("guide")
    _complete_click(
        pane,
        PlotAction.GUIDE_POINT,
        SnapResult((1.0, 2.0), "free", 0.0),
    )

    pane.cancel_active_interaction()

    assert pane.guide_anchor is None
    assert pane.active_design_tool == "guide"


def test_point_action_stays_in_point_tool(pane: _DesignPlotPane) -> None:
    emitted: list[tuple[float, float]] = []
    pane.point_requested.connect(lambda x_value, y_value: emitted.append((x_value, y_value)))
    pane.set_route_edit_enabled(True)
    pane.set_active_design_tool("point")

    _complete_click(
        pane,
        PlotAction.POINT,
        SnapResult((4.0, 5.0), "free", 0.0),
    )

    assert emitted == [(4.0, 5.0)]
    assert pane.active_design_tool == "point"


def test_align_action_emits_snapped_point_and_supports_arbitrary_draft_overlay(
    pane: _DesignPlotPane,
) -> None:
    emitted: list[tuple[float, float]] = []
    pane.alignment_point_requested.connect(
        lambda x_value, y_value: emitted.append((x_value, y_value))
    )
    pane.set_active_design_tool("align")
    pane.set_alignment_draft_points([(1.0, 2.0), (3.0, 4.0), (5.0, 6.0)])

    _complete_click(
        pane,
        PlotAction.ALIGNMENT_POINT,
        SnapResult((7.0, 8.0), "vertex", 0.1),
    )

    assert emitted == [(7.0, 8.0)]
    assert pane._presentation.plan.alignment_draft_points == (
        (1.0, 2.0),
        (3.0, 4.0),
        (5.0, 6.0),
    )
    assert pane.active_design_tool == "align"


def test_align_right_click_does_not_emit_legacy_calibration_point(
    pane: _DesignPlotPane,
) -> None:
    from types import SimpleNamespace

    pane.set_active_design_tool("align")
    emitted: list[object] = []
    pane.calibration_point_selected.connect(lambda *args: emitted.append(args))
    event = SimpleNamespace(
        button=lambda: Qt.RightButton,
        modifiers=lambda: Qt.NoModifier,
    )

    pane._on_mouse_clicked(event)

    assert emitted == []


def test_mixed_array_preview_draws_points_and_dashed_segments(
    pane: _DesignPlotPane,
) -> None:
    pane.set_mixed_array_preview(
        [(1.0, 2.0), (3.0, 4.0)],
        [SegmentGeometry((0.0, 0.0), (5.0, 0.0))],
    )

    assert pane._presentation.plan.mixed_preview_points == (
        (1.0, 2.0),
        (3.0, 4.0),
    )
    assert pane._presentation.plan.mixed_preview_segments == (
        SegmentGeometry((0.0, 0.0), (5.0, 0.0))
        ,
    )


def test_layout_window_applies_one_selection_to_canvas_and_table(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    window = DesignLayoutWindow()
    route = _route()
    markup = _markup(tmp_path)
    window.set_probe_route(route, selected_route_point_index=0)
    window.set_markup(markup)
    changes: list[SelectionModel] = []
    window.selection_changed.connect(changes.append)

    window._apply_selection_request({markup_entity_id("a")}, "replace")

    assert window.selection.ids == frozenset({markup_entity_id("a")})
    assert window._main_view._presentation.plan.selection == window.selection
    assert window.navigator_panel.tool_controls.selection == window.selection
    assert window.navigator_panel.route_controls.selected_rows() == []
    assert changes[-1] == window.selection
    window.close()
    window.deleteLater()


def test_layout_window_toolbar_drives_canvas_tool(
    qt_app: QApplication,
) -> None:
    window = DesignLayoutWindow()
    window.navigator_panel.document_controls._document = object()
    window.navigator_panel._replace_tool_context()
    window.navigator_panel._update_enabled_state()

    window.navigator_panel.tool_controls._point_tool_button.click()
    assert window._main_view.active_design_tool == "point"
    window.navigator_panel.tool_controls._guide_tool_button.click()
    assert window._main_view.active_design_tool == "guide"

    window.close()
    window.deleteLater()


def test_plot_uses_canonical_ruler_tool_token(pane: _DesignPlotPane) -> None:
    pane.set_active_design_tool("ruler")

    assert pane.active_design_tool == "ruler"
    with pytest.raises(ValueError, match="Unknown design tool"):
        pane.set_active_design_tool("measure")


def test_layout_window_enter_accepts_align_draft(
    qt_app: QApplication,
) -> None:
    window = DesignLayoutWindow()
    window.navigator_panel.document_controls._document = object()
    window.navigator_panel._replace_tool_context()
    window.navigator_panel._update_enabled_state()
    accepted: list[object] = []
    window.alignment_draft_accepted.connect(accepted.append)
    window.navigator_panel.tool_controls._align_tool_button.click()
    window.navigator_panel.append_alignment_point(1.0, 2.0)
    window.navigator_panel.append_alignment_point(3.0, 4.0)

    window._accept_alignment_shortcut.activated.emit()

    assert accepted == [((1.0, 2.0), (3.0, 4.0))]
    assert window._main_view.active_design_tool == "select"
    window.close()
    window.deleteLater()


def test_layout_window_escape_cancels_transient_tool_state_and_selects(
    qt_app: QApplication,
) -> None:
    window = DesignLayoutWindow()
    window.navigator_panel.document_controls._document = object()
    window.navigator_panel._replace_tool_context()
    window.navigator_panel._update_enabled_state()
    window.navigator_panel.tool_controls._ruler_tool_button.click()
    window.navigator_panel.apply_route_pick("ruler", 0.0, 0.0)
    window.navigator_panel.apply_route_pick("ruler", 2.0, 0.0)
    window.navigator_panel.tool_controls._guide_tool_button.click()
    window._main_view._apply_interaction_transition(
        window._main_view._plot_interaction.set_context(
            document_present=True,
            preview_active=False,
        )
    )
    _complete_click(
        window._main_view,
        PlotAction.GUIDE_POINT,
        SnapResult((1.0, 2.0), "free", 0.0),
    )
    window._main_view._renderer.apply(
        window._main_view._presentation.set_tool_sketch_segments,
        [((0.0, 0.0), (3.0, 0.0))],
    )

    window._cancel_active_interaction()

    assert window._main_view.guide_anchor is None
    assert window._main_view._presentation.plan.tool_sketch_points == ()
    assert window._main_view._presentation.plan.tool_sketch_segments == (
        ((0.0, 0.0), (3.0, 0.0))
        ,
    )
    assert (
        window.navigator_panel.tool_controls._ruler_length_label.text()
        == "1 measurements"
    )
    assert window._main_view.active_design_tool == "select"
    assert window.navigator_panel.tool_controls._select_tool_button.isChecked()
    window.close()
    window.deleteLater()


def test_layout_window_forwards_modifier_snapshots_to_ruler(
    qt_app: QApplication,
) -> None:
    window = DesignLayoutWindow()
    window.navigator_panel.document_controls._document = object()
    window.navigator_panel._replace_tool_context()
    window.navigator_panel._update_enabled_state()
    window.navigator_panel.tool_controls._ruler_tool_button.click()

    window._main_view.route_pick_requested.emit(
        "ruler",
        1.0,
        2.0,
        False,
        False,
    )
    window._main_view.tool_hover_snap_changed.emit(
        SnapResult((6.0, 4.0), "vertex", 0.1),
        True,
        False,
    )

    assert window._main_view._presentation.plan.tool_measure_points == (
        (1.0, 2.0),
        (6.0, 2.0),
    )
    window._main_view.route_pick_requested.emit(
        "ruler",
        6.0,
        4.0,
        True,
        False,
    )
    assert window._main_view._presentation.plan.tool_measure_segments == (
        ((1.0, 2.0), (6.0, 2.0))
        ,
    )
    window.close()
    window.deleteLater()


def test_alignment_accept_clears_plot_before_forwarding_accept(
    qt_app: QApplication,
) -> None:
    window = DesignLayoutWindow()
    window.navigator_panel.document_controls._document = object()
    window.navigator_panel._replace_tool_context()
    window.navigator_panel._update_enabled_state()
    window.navigator_panel.tool_controls._align_tool_button.click()
    window.navigator_panel.append_alignment_point(1.0, 2.0)
    window.navigator_panel.append_alignment_point(3.0, 4.0)
    observed: list[tuple[str, list[tuple[float, float]], object]] = []
    window.alignment_draft_accepted.connect(
        lambda points: observed.append(
            (
                window._main_view.active_design_tool,
                list(
                    window._main_view._presentation.plan.alignment_draft_points
                ),
                points,
            )
        )
    )

    window.navigator_panel.accept_alignment_draft()

    assert observed == [
        ("select", [], ((1.0, 2.0), (3.0, 4.0)))
    ]
    window.close()
    window.deleteLater()


def test_focus_candidate_and_selected_point_have_distinct_overlays(
    pane: _DesignPlotPane,
) -> None:
    candidate = FocusCandidate(
        center=(5.0, 6.0),
        bounds=(4.0, 5.0, 6.0, 7.0),
        distance_from_design_center=1.0,
    )

    pane.set_focus_candidate(candidate)
    pane.set_selected_focus_point((8.0, 9.0))

    assert pane._renderer.state.focus_candidate_outline == (
        (4.0, 5.0),
        (6.0, 5.0),
        (6.0, 7.0),
        (4.0, 7.0),
        (4.0, 5.0),
    )
    assert pane._renderer.state.selected_focus_points == ((8.0, 9.0),)
    assert pane.selected_focus_point == (8.0, 9.0)
