from __future__ import annotations

import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from probe_station_gui.route.model import (  # noqa: E402
    MeasurementRoute,
    RouteDesignBinding,
    RoutePoint,
)
from probe_station_gui.views.design_navigator_panel import (  # noqa: E402
    DesignNavigatorPanel,
)



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


def _set_document_available(panel: DesignNavigatorPanel) -> None:
    panel.document_controls._document = object()
    panel._replace_tool_context()
    panel._update_enabled_state()


def test_design_navigator_disables_document_and_route_controls_without_design(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel._update_enabled_state()
    panel.tool_controls._ruler_tool_button.click()

    panel.set_document(None)

    assert not panel.document_controls.unload_button.isEnabled()
    assert not panel.document_controls.top_cell_combo.isEnabled()
    assert not panel.document_controls.layer_list.isEnabled()
    assert not panel._snap_checkbox.isEnabled()
    assert not panel.route_controls.new_button.isEnabled()
    assert not panel.route_controls.open_button.isEnabled()
    assert not panel.tool_controls._select_tool_button.isEnabled()
    assert not panel.tool_controls._ruler_tool_button.isEnabled()
    assert not panel.tool_controls._array_tool_button.isEnabled()
    assert not panel.tool_controls._rotate_tool_button.isEnabled()
    assert panel.tool_controls._select_tool_button.isChecked()

    panel.deleteLater()


def test_focus_reference_controls_use_finished_product_copy_and_readiness(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel.set_design_registration_active(True)

    panel.set_focus_reference_state(z_ready=False, a_ready=False)

    assert panel.registration_controls.find_focus_button.text() == "Find focus reference"
    assert panel.registration_controls.use_selected_focus_button.text() == "Use selected point"
    assert panel.registration_controls.reset_focus_button.text() == "Reset focus reference"
    assert panel.registration_controls.find_focus_button.isEnabled()
    assert not panel.registration_controls.use_selected_focus_button.isEnabled()
    assert not panel.registration_controls.reset_focus_button.isEnabled()

    panel.set_focus_selection_available(True)
    assert panel.registration_controls.use_selected_focus_button.isEnabled()

    panel.set_focus_reference_state(z_ready=True, a_ready=True)

    assert not panel.registration_controls.find_focus_button.isEnabled()
    assert panel.registration_controls.reset_focus_button.isEnabled()
    assert panel.registration_controls.focus_status_label.text() == "Focus and contact references ready."
    panel.deleteLater()


def test_registration_instance_selector_uses_uuid_and_obeys_edit_guard(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    selected: list[str] = []
    created: list[bool] = []
    panel.registration_instance_selected.connect(selected.append)
    panel.new_registration_requested.connect(lambda: created.append(True))

    panel.set_registration_instances(
        (
            ("11111111-1111-4111-8111-111111111111", "loaded"),
            ("22222222-2222-4222-8222-222222222222", "loaded (2)"),
        ),
        selected_frame_id="11111111-1111-4111-8111-111111111111",
    )

    assert selected == []
    assert panel.registration_controls.instance_combo.itemData(0) == (
        "11111111-1111-4111-8111-111111111111"
    )
    assert panel.registration_controls.instance_combo.itemData(1) == (
        "22222222-2222-4222-8222-222222222222"
    )
    panel.registration_controls.instance_combo.setCurrentIndex(1)
    assert selected == ["22222222-2222-4222-8222-222222222222"]
    panel.registration_controls.new_button.click()
    assert created == [True]

    panel.set_route_measurement_running(True)
    assert not panel.registration_controls.instance_combo.isEnabled()
    assert not panel.registration_controls.new_button.isEnabled()
    panel.deleteLater()


def test_design_navigator_enables_idle_route_controls_with_route_selection(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel.set_route(_route(point_count=2), selected_route_point_index=0)
    panel.set_current_position((1.0, 2.0), (3.0, 4.0))

    assert panel.route_controls.new_button.isEnabled()
    assert panel.route_controls.open_button.isEnabled()
    assert not panel.route_controls.save_button.isEnabled()
    assert panel.route_controls.save_as_button.isEnabled()
    assert panel.tool_controls._select_tool_button.isEnabled()
    assert panel.tool_controls._ruler_tool_button.isEnabled()
    assert panel.tool_controls._array_tool_button.isEnabled()
    assert panel.tool_controls._rotate_tool_button.isEnabled()
    assert panel.route_controls.add_current_button.isEnabled()
    assert panel.route_controls.remove_button.isEnabled()
    assert panel.route_controls.clear_button.isEnabled()
    assert panel.route_run_controls.measure_button.isEnabled()
    assert panel.route_run_controls.move_selected_button.isEnabled()
    assert not panel.route_run_controls.stop_button.isEnabled()
    assert not panel.route_run_controls.save_shift_button.isEnabled()
    assert not panel.route_run_controls.jump_selected_button.isEnabled()

    panel.deleteLater()


def test_design_navigator_locks_old_design_controls_while_new_design_loads(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel.set_route(_route(point_count=2), selected_route_point_index=0)
    panel.set_current_position((1.0, 2.0), (3.0, 4.0))

    panel.set_design_load_pending(True)

    assert not panel.document_controls.unload_button.isEnabled()
    assert not panel.document_controls.top_cell_combo.isEnabled()
    assert not panel.document_controls.layer_list.isEnabled()
    assert not panel._snap_checkbox.isEnabled()
    assert not panel.route_controls.new_button.isEnabled()
    assert not panel.route_controls.open_button.isEnabled()
    assert not panel.route_controls.save_as_button.isEnabled()
    assert not panel.route_controls.route_table.isEnabled()
    assert not panel.route_controls.add_current_button.isEnabled()
    assert not panel.route_controls.remove_button.isEnabled()
    assert not panel.route_controls.clear_button.isEnabled()
    assert not panel.route_run_controls.measure_button.isEnabled()
    assert not panel.route_run_controls.move_selected_button.isEnabled()
    assert not panel.tool_controls._markup_visibility_button.isEnabled()
    assert not panel.tool_controls._delete_shortcut.isEnabled()

    panel.deleteLater()
