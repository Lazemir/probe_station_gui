from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.views.design_navigator_panel import DesignNavigatorPanel


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


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
