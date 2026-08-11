from __future__ import annotations

import inspect
import os
from pathlib import Path

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.model import DesignDocument
from probe_station_gui.views.design_document_controls import DesignDocumentControls
from probe_station_gui.views.design_navigator_enablement import (
    DesignNavigatorEnablement,
)
from probe_station_gui.views.design_navigator_panel import DesignNavigatorPanel
from probe_station_gui.views.design_registration_controls import (
    DesignRegistrationControls,
)
from probe_station_gui.views.design_route_controls import DesignRouteControls
from probe_station_gui.views.design_route_run_controls import DesignRouteRunControls
from probe_station_gui.views.design_tool_controls import DesignToolControls


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _enablement(
    controls: DesignRouteRunControls,
    *,
    route_running: bool = True,
) -> DesignNavigatorEnablement:
    return DesignNavigatorEnablement(
        has_document=True,
        has_route=True,
        route_saved=True,
        route_has_points=True,
        has_route_selection=True,
        has_current_design_position=True,
        route_running=route_running,
        design_registration_active=True,
        route_control=controls.presentation,
    )


def _document() -> DesignDocument:
    return DesignDocument(
        path=Path("design.gds"),
        library=object(),
        top_cell=object(),
        top_cell_name="TOP",
        cell_names=("TOP", "ALT"),
        dbu=0.001,
        user_unit=1.0,
        bounds=(0.0, 0.0, 10.0, 10.0),
        polygons_by_layer={(1, 0): ()},
        visible_layers=frozenset({(1, 0)}),
    )


def test_document_controls_do_not_echo_programmatic_render(
    qt_app: QApplication,
) -> None:
    controls = DesignDocumentControls()
    top_cells: list[str] = []
    layers: list[tuple[int, int, bool]] = []
    controls.top_cell_changed.connect(top_cells.append)
    controls.layer_visibility_changed.connect(
        lambda layer, datatype, visible: layers.append((layer, datatype, visible))
    )

    controls.set_document(_document())

    assert top_cells == []
    assert layers == []
    controls.top_cell_combo.setCurrentText("ALT")
    controls.layer_list.item(0).setCheckState(Qt.Unchecked)
    assert top_cells == ["ALT"]
    assert layers == [(1, 0, False)]
    controls.deleteLater()


def test_route_run_controls_own_pause_interrupt_resume_state(
    qt_app: QApplication,
) -> None:
    controls = DesignRouteRunControls()
    actions: list[str] = []
    controls.pause_requested.connect(lambda: actions.append("pause"))
    controls.interrupt_requested.connect(lambda: actions.append("interrupt"))
    controls.resume_requested.connect(lambda: actions.append("resume"))

    controls.set_running(True)
    controls.apply_enablement(_enablement(controls))
    controls.pause_button.click()

    assert actions == ["pause"]
    assert controls.pause_button.text() == "Interrupt"
    assert controls.pause_button.isEnabled()

    controls.pause_button.click()

    assert actions == ["pause", "interrupt"]
    assert controls.pause_button.text() == "Interrupt"
    assert not controls.pause_button.isEnabled()

    controls.set_interrupt_request_pending(False)
    controls.set_pause_request_pending(False)
    controls.set_waiting(True, "paused")
    controls.apply_enablement(_enablement(controls))
    controls.pause_button.click()

    assert actions == ["pause", "interrupt", "resume"]
    controls.deleteLater()


def test_route_run_controls_wait_for_pause_ack_before_showing_resume(
    qt_app: QApplication,
) -> None:
    controls = DesignRouteRunControls()
    controls.set_running(True)
    controls.apply_enablement(_enablement(controls))

    controls.pause_button.click()
    assert controls.pause_button.text() == "Interrupt"

    controls.set_waiting(True, "paused")
    controls.apply_enablement(_enablement(controls))
    assert controls.pause_button.text() == "Resume"
    controls.deleteLater()


def test_route_run_controls_external_waiting_interrupts_and_stop_resets(
    qt_app: QApplication,
) -> None:
    controls = DesignRouteRunControls()
    actions: list[str] = []
    controls.interrupt_requested.connect(lambda: actions.append("interrupt"))
    controls.set_running(True)
    controls.set_waiting(True, "external_measurement")
    controls.apply_enablement(_enablement(controls))

    assert controls.pause_button.text() == "Interrupt"
    controls.pause_button.click()
    assert actions == ["interrupt"]
    assert not controls.pause_button.isEnabled()

    controls.set_running(False)
    controls.apply_enablement(_enablement(controls, route_running=False))
    assert not controls.running
    assert not controls.waiting
    assert controls.pause_button.text() == "Pause"
    assert not controls.pause_button.isEnabled()
    controls.deleteLater()


def test_panel_delegates_extracted_document_registration_and_run_sections(
    qt_app: QApplication,
) -> None:
    panel = DesignNavigatorPanel()

    assert isinstance(panel.document_controls, DesignDocumentControls)
    assert isinstance(panel.registration_controls, DesignRegistrationControls)
    assert isinstance(panel.route_controls, DesignRouteControls)
    assert isinstance(panel.route_run_controls, DesignRouteRunControls)
    assert isinstance(panel.tool_controls, DesignToolControls)
    assert "_route_run_control_state" not in vars(panel)
    assert "_tool_session" not in vars(panel)

    panel.deleteLater()


def test_panel_source_no_longer_owns_extracted_widget_policy() -> None:
    source = inspect.getsource(DesignNavigatorPanel)

    assert 'QGroupBox("Design"' not in source
    assert 'QGroupBox("Registration"' not in source
    assert "RouteRunControlState(" not in source
    assert "_route_measurement_control_state" not in source
    assert "_emit_route_measurement_pause_or_resume" not in source
    assert "_route_table" not in source
    assert "_tool_session" not in source
    assert (
        Path(inspect.getfile(DesignNavigatorPanel)).name == "design_navigator_panel.py"
    )
