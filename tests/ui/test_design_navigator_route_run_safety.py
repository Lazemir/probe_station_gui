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


def test_design_navigator_running_route_controls_preserve_confirmation_states(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()
    _set_document_available(panel)
    panel.set_route(_route(path="route.json"), selected_route_point_index=0)
    confirmations: list[str] = []
    panel.route_measurement_confirmation_requested.connect(confirmations.append)

    panel.set_route_measurement_running(True)

    assert not panel.route_controls.new_button.isEnabled()
    assert not panel.route_controls.open_button.isEnabled()
    assert not panel.route_controls.save_button.isEnabled()
    assert not panel.route_controls.save_as_button.isEnabled()
    assert panel.route_controls.route_table.isEnabled()
    assert panel.route_run_controls.measure_button.isEnabled()
    assert panel.route_run_controls.stop_button.isEnabled()
    assert panel.route_run_controls.pause_button.text() == "Pause"
    assert panel.route_run_controls.pause_button.isEnabled()
    assert not panel.route_run_controls.move_selected_button.isEnabled()

    panel.set_route_measurement_waiting(True, "contact_confirmation")

    assert panel.route_run_controls.pause_button.text() == "Resume"
    assert panel.route_run_controls.pause_button.isEnabled()
    assert panel.route_run_controls.save_shift_button.isEnabled()
    assert panel.route_run_controls.remeasure_button.isEnabled()
    assert panel.route_run_controls.skip_button.isEnabled()
    assert panel.route_run_controls.next_button.isEnabled()
    assert panel.route_run_controls.move_selected_button.isEnabled()
    assert panel.route_run_controls.jump_selected_button.isEnabled()
    panel.route_run_controls.measure_button.click()
    assert confirmations == ["measure"]

    panel.set_route_measurement_waiting(True, "external_measurement")

    assert panel.route_run_controls.pause_button.text() == "Interrupt"
    assert panel.route_run_controls.pause_button.isEnabled()
    assert not panel.route_run_controls.save_shift_button.isEnabled()
    assert not panel.route_run_controls.remeasure_button.isEnabled()
    assert not panel.route_run_controls.skip_button.isEnabled()
    assert not panel.route_run_controls.next_button.isEnabled()
    assert not panel.route_run_controls.move_selected_button.isEnabled()
    assert not panel.route_run_controls.jump_selected_button.isEnabled()

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
    panel.route_run_controls.pause_button.click()

    assert calls == ["pause"]
    assert panel.route_run_controls.pause_button.text() == "Interrupt"
    assert panel.route_run_controls.pause_button.isEnabled()

    panel.route_run_controls.pause_button.click()

    assert calls == ["pause", "interrupt"]
    assert panel.route_run_controls.pause_button.text() == "Interrupt"
    assert not panel.route_run_controls.pause_button.isEnabled()

    panel.set_route_measurement_interrupt_request_pending(False)
    panel.set_route_measurement_pause_request_pending(False)
    panel.set_route_measurement_waiting(True, "paused")
    panel.route_run_controls.pause_button.click()

    assert calls == ["pause", "interrupt", "next"]

    panel.deleteLater()
