from __future__ import annotations

import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

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
