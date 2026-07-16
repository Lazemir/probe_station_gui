from __future__ import annotations

import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QItemSelectionModel, Qt
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.selection_geometry import PointGeometry, SegmentGeometry
from probe_station_gui.design.selection_model import (
    EntityOwner,
    SelectableDesignEntity,
    SelectionModel,
    markup_entity_id,
    route_entity_id,
)
from probe_station_gui.route.model import MeasurementRoute, RouteDesignBinding, RoutePoint
from probe_station_gui.views.design_navigator_panel import DesignNavigatorPanel


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _route(*, path: str | None = None, point_count: int = 1) -> MeasurementRoute:
    route = MeasurementRoute(
        name="test route",
        design=RouteDesignBinding(
            path="design.gds",
            sha256="0" * 64,
            top_cell_name="TOP",
            bounds=(0.0, 0.0, 100.0, 100.0),
            dbu=0.001,
        ),
        points=[
            RoutePoint(
                id=f"p{index + 1:03d}",
                label=f"P{index + 1:03d}",
                camera_center=(float(index), float(index)),
            )
            for index in range(point_count)
        ],
    )
    if path is not None:
        route.path = Path(path)
    return route


def test_design_navigator_disables_document_and_route_controls_without_design(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    panel._route_pick_mode = "array_origin"
    panel._active_design_tool = "ruler"

    panel._update_enabled_state()

    assert not panel._unload_design_button.isEnabled()
    assert not panel._top_cell_combo.isEnabled()
    assert not panel._layer_list.isEnabled()
    assert not panel._snap_checkbox.isEnabled()
    assert not panel._route_new_button.isEnabled()
    assert not panel._route_open_button.isEnabled()
    assert not panel._select_tool_button.isEnabled()
    assert not panel._ruler_tool_button.isEnabled()
    assert not panel._array_tool_button.isEnabled()
    assert not panel._rotate_tool_button.isEnabled()
    assert panel._route_pick_mode is None
    assert panel._active_design_tool == "select"

    panel.deleteLater()


def test_design_navigator_enables_idle_route_controls_with_route_selection(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel.set_route(_route(point_count=2), selected_route_point_index=0)
    panel.set_current_position((1.0, 2.0), (3.0, 4.0))

    assert panel._route_new_button.isEnabled()
    assert panel._route_open_button.isEnabled()
    assert not panel._route_save_button.isEnabled()
    assert panel._route_save_as_button.isEnabled()
    assert panel._select_tool_button.isEnabled()
    assert panel._ruler_tool_button.isEnabled()
    assert panel._array_tool_button.isEnabled()
    assert panel._rotate_tool_button.isEnabled()
    assert panel._route_add_current_button.isEnabled()
    assert panel._route_remove_button.isEnabled()
    assert panel._route_clear_button.isEnabled()
    assert panel._route_run_button.isEnabled()
    assert panel._route_move_selected_button.isEnabled()
    assert not panel._route_stop_button.isEnabled()
    assert not panel._route_save_shift_button.isEnabled()
    assert not panel._route_jump_selected_button.isEnabled()

    panel.deleteLater()


def test_design_navigator_locks_old_design_controls_while_new_design_loads(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel.set_route(_route(point_count=2), selected_route_point_index=0)
    panel.set_current_position((1.0, 2.0), (3.0, 4.0))

    panel.set_design_load_pending(True)

    assert not panel._unload_design_button.isEnabled()
    assert not panel._top_cell_combo.isEnabled()
    assert not panel._layer_list.isEnabled()
    assert not panel._snap_checkbox.isEnabled()
    assert not panel._route_new_button.isEnabled()
    assert not panel._route_open_button.isEnabled()
    assert not panel._route_save_as_button.isEnabled()
    assert not panel._route_table.isEnabled()
    assert not panel._route_add_current_button.isEnabled()
    assert not panel._route_remove_button.isEnabled()
    assert not panel._route_clear_button.isEnabled()
    assert not panel._route_run_button.isEnabled()
    assert not panel._route_move_selected_button.isEnabled()
    assert not panel._markup_visibility_button.isEnabled()
    assert not panel._delete_shortcut.isEnabled()

    panel.deleteLater()


def test_design_tools_are_exclusive_and_markup_eye_is_independent(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel._update_enabled_state()
    tools: list[str] = []
    panel.active_design_tool_changed.connect(tools.append)

    assert panel._select_tool_button.isChecked()
    assert panel._markup_visibility_button.isChecked()
    panel._point_tool_button.click()
    assert panel._point_tool_button.isChecked()
    assert not panel._select_tool_button.isChecked()
    panel._guide_tool_button.click()
    assert panel._guide_tool_button.isChecked()
    assert not panel._point_tool_button.isChecked()
    panel._markup_visibility_button.click()

    assert panel._guide_tool_button.isChecked()
    assert tools[-2:] == ["point", "guide"]
    panel.deleteLater()


def test_point_guide_and_mutations_disable_while_route_is_running(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel.set_route(_route(point_count=1), selected_route_point_index=0)
    panel.set_selectable_entities(
        [
            SelectableDesignEntity(
                route_entity_id("p001"),
                EntityOwner.ROUTE,
                "p001",
                PointGeometry((0.0, 0.0)),
                route_index=0,
            ),
            SelectableDesignEntity(
                markup_entity_id("g1"),
                EntityOwner.MARKUP,
                "g1",
                SegmentGeometry((0.0, 0.0), (1.0, 0.0)),
            ),
        ]
    )
    panel.set_selection(SelectionModel(frozenset({route_entity_id("p001")})))

    assert panel._point_tool_button.isEnabled()
    assert panel._guide_tool_button.isEnabled()
    assert panel._selection_delete_button.isEnabled()
    assert panel._route_array_create_button.isEnabled()

    panel.set_route_measurement_running(True)

    assert not panel._point_tool_button.isEnabled()
    assert not panel._guide_tool_button.isEnabled()
    assert not panel._selection_delete_button.isEnabled()
    assert not panel._guide_clear_button.isEnabled()
    assert not panel._guide_undo_button.isEnabled()
    assert not panel._route_array_create_button.isEnabled()
    panel.deleteLater()


def test_array_page_is_selection_driven_without_origin_extent_or_replace(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()

    assert not hasattr(panel, "_route_array_origin_x_spin")
    assert not hasattr(panel, "_route_array_origin_y_spin")
    assert not hasattr(panel, "_route_array_pick_origin_button")
    assert not hasattr(panel, "_route_array_pick_extent1_button")
    assert not hasattr(panel, "_route_array_pick_extent2_button")
    assert not hasattr(panel, "_route_array_replace_checkbox")
    panel.deleteLater()


def test_array_create_requires_one_selected_visible_entity(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    panel._document = object()
    guide = SelectableDesignEntity(
        markup_entity_id("g1"),
        EntityOwner.MARKUP,
        "g1",
        SegmentGeometry((0.0, 0.0), (1.0, 0.0)),
    )
    panel.set_selectable_entities([guide])
    panel._update_enabled_state()

    assert not panel._route_array_create_button.isEnabled()
    panel.set_selection(SelectionModel(frozenset({guide.id})))
    assert panel._route_array_create_button.isEnabled()
    panel.set_markup_visible(False)
    assert not panel._route_array_create_button.isEnabled()
    panel.deleteLater()


def test_hidden_markup_can_still_be_cleared_when_guides_exist(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    panel._document = object()

    panel.set_markup_state(visible=False, guide_count=2)

    assert panel._guide_clear_button.isEnabled()
    assert not panel._route_array_create_button.isEnabled()
    panel.deleteLater()


def test_route_table_selection_and_shared_selection_are_bidirectional(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    route = _route(point_count=2)
    panel._document = object()
    panel.set_route(route, selected_route_point_index=0)
    entities = [
        SelectableDesignEntity(
            route_entity_id(point.id),
            EntityOwner.ROUTE,
            point.id,
            PointGeometry(point.camera_center),
            route_index=index,
        )
        for index, point in enumerate(route.points)
    ]
    panel.set_selectable_entities(entities)
    panel.set_selection(
        SelectionModel(frozenset(entity.id for entity in entities))
    )

    assert panel._selected_route_row_indices() == [0, 1]
    requests: list[tuple[set[str], str]] = []
    panel.selection_requested.connect(
        lambda ids, mode: requests.append((set(ids), mode))
    )
    panel._route_table.clearSelection()
    selection_model = panel._route_table.selectionModel()
    selection_model.select(
        panel._route_table.model().index(1, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows,
    )

    assert requests[-1] == ({route_entity_id("p002")}, "replace")
    panel.deleteLater()


def test_route_table_shift_add_and_ctrl_invert_preserve_guide_selection(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    route = _route(point_count=2)
    panel._document = object()
    panel.set_route(route, selected_route_point_index=0)
    entities = [
        SelectableDesignEntity(
            route_entity_id(point.id),
            EntityOwner.ROUTE,
            point.id,
            PointGeometry(point.camera_center),
            route_index=index,
        )
        for index, point in enumerate(route.points)
    ]
    guide_id = markup_entity_id("guide")
    entities.append(
        SelectableDesignEntity(
            guide_id,
            EntityOwner.MARKUP,
            "guide",
            SegmentGeometry((0.0, 0.0), (1.0, 0.0)),
        )
    )
    panel.set_selectable_entities(entities)
    panel.set_selection(
        SelectionModel(frozenset({guide_id, route_entity_id("p001")}))
    )
    requests: list[tuple[set[str], str]] = []
    panel.selection_requested.connect(
        lambda ids, mode: requests.append((set(ids), mode))
    )
    selection_model = panel._route_table.selectionModel()

    panel._route_selection_modifiers = lambda: Qt.ShiftModifier
    selection_model.select(
        panel._route_table.model().index(1, 0),
        QItemSelectionModel.Select | QItemSelectionModel.Rows,
    )
    assert requests[-1] == (
        {route_entity_id("p001"), route_entity_id("p002")},
        "add",
    )

    panel.set_selection(
        SelectionModel(
            frozenset(
                {
                    guide_id,
                    route_entity_id("p001"),
                    route_entity_id("p002"),
                }
            )
        )
    )
    panel._route_selection_modifiers = lambda: Qt.ControlModifier
    selection_model.select(
        panel._route_table.model().index(0, 0),
        QItemSelectionModel.Deselect | QItemSelectionModel.Rows,
    )
    assert requests[-1] == ({route_entity_id("p001")}, "invert")
    panel.deleteLater()


def test_select_and_guide_actions_emit_laconic_commands(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel._guide_undo_available = True
    panel.set_selectable_entities(
        [
            SelectableDesignEntity(
                route_entity_id("p001"),
                EntityOwner.ROUTE,
                "p001",
                PointGeometry((0.0, 0.0)),
                route_index=0,
            ),
            SelectableDesignEntity(
                markup_entity_id("g1"),
                EntityOwner.MARKUP,
                "g1",
                SegmentGeometry((0.0, 0.0), (1.0, 0.0)),
            ),
        ]
    )
    panel.set_selection(SelectionModel(frozenset({"route:p001"})))
    panel.set_markup_state(visible=True, guide_count=1)
    panel._update_enabled_state()
    commands: list[str] = []
    panel.delete_selection_requested.connect(lambda: commands.append("delete"))
    panel.guide_undo_requested.connect(lambda: commands.append("undo"))
    panel.guide_clear_requested.connect(lambda: commands.append("clear"))

    panel._selection_delete_button.click()
    panel._guide_undo_button.click()
    panel._guide_clear_button.click()

    assert commands == ["delete", "undo", "clear"]
    panel.deleteLater()


def test_design_navigator_running_route_controls_preserve_confirmation_states(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel.set_route(_route(path="route.json"), selected_route_point_index=0)

    panel.set_route_measurement_running(True)

    assert not panel._route_new_button.isEnabled()
    assert not panel._route_open_button.isEnabled()
    assert not panel._route_save_button.isEnabled()
    assert not panel._route_save_as_button.isEnabled()
    assert panel._route_table.isEnabled()
    assert panel._route_run_button.isEnabled()
    assert panel._route_stop_button.isEnabled()
    assert panel._route_pause_button.text() == "Pause"
    assert panel._route_pause_button.isEnabled()
    assert not panel._route_move_selected_button.isEnabled()

    panel.set_route_measurement_waiting(True, "contact_confirmation")

    assert panel._route_pause_button.text() == "Resume"
    assert panel._route_pause_button.isEnabled()
    assert panel._route_save_shift_button.isEnabled()
    assert panel._route_remeasure_button.isEnabled()
    assert panel._route_skip_button.isEnabled()
    assert panel._route_next_button.isEnabled()
    assert panel._route_move_selected_button.isEnabled()
    assert panel._route_jump_selected_button.isEnabled()

    panel.set_route_measurement_waiting(True, "external_measurement")

    assert panel._route_pause_button.text() == "Interrupt"
    assert panel._route_pause_button.isEnabled()
    assert not panel._route_save_shift_button.isEnabled()
    assert not panel._route_remeasure_button.isEnabled()
    assert not panel._route_skip_button.isEnabled()
    assert not panel._route_next_button.isEnabled()
    assert not panel._route_move_selected_button.isEnabled()
    assert not panel._route_jump_selected_button.isEnabled()

    panel.deleteLater()


def test_route_pause_button_preserves_pause_interrupt_resume_flow(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    calls: list[object] = []
    panel.route_measurement_pause_requested.connect(lambda: calls.append("pause"))
    panel.route_measurement_interrupt_requested.connect(lambda: calls.append("interrupt"))
    panel.route_measurement_confirmation_requested.connect(calls.append)

    panel.set_route_measurement_running(True)
    panel._route_pause_button.click()

    assert calls == ["pause"]
    assert panel._route_pause_button.text() == "Interrupt"
    assert panel._route_pause_button.isEnabled()

    panel._route_pause_button.click()

    assert calls == ["pause", "interrupt"]
    assert panel._route_pause_button.text() == "Interrupt"
    assert not panel._route_pause_button.isEnabled()

    panel.set_route_measurement_interrupt_request_pending(False)
    panel.set_route_measurement_pause_request_pending(False)
    panel.set_route_measurement_waiting(True, "paused")
    panel._route_pause_button.click()

    assert calls == ["pause", "interrupt", "next"]

    panel.deleteLater()
