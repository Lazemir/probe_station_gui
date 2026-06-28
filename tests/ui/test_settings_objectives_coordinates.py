from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.dialogs.settings.coordinate_system import (
    CoordinateSystemSettingsWidget,
)
from probe_station_gui.dialogs.settings.objectives import ObjectivesSettingsWidget
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.objective_config import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
)
from probe_station_gui.settings.sections import CoordinateSystemSettings


def _qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _set_combo_data(combo, data: str) -> None:
    index = combo.findData(data)
    assert index >= 0
    combo.setCurrentIndex(index)


def test_objectives_widget_saves_active_profile_edits() -> None:
    _qt_app()
    settings = Settings()
    settings.objectives = ObjectivesSettings(
        active_name="X5",
        apply_offsets_on_change=True,
        objectives={
            "X5": ObjectiveCalibrationSettings(name="X5", magnification=5.0),
            "X20": ObjectiveCalibrationSettings(
                name="X20",
                magnification=20.0,
                pixels_to_mm=[[1.0, 0.0], [0.0, 1.0]],
                xy_calibration_configured=True,
            ),
        },
    )
    widget = ObjectivesSettingsWidget(settings.objectives)

    _set_combo_data(widget._profile_combo, "X20")
    assert widget._xy_calibration_status.text() == "Configured"
    _set_combo_data(widget._active_combo, "X20")
    widget._apply_offsets_checkbox.setChecked(False)
    widget._magnification_spin.setValue(21.5)
    widget._xy_configured_checkbox.setChecked(True)
    widget._z_configured_checkbox.setChecked(True)
    widget._x_offset_spin.setValue(0.1234)
    widget._y_offset_spin.setValue(-0.4321)
    widget._z_offset_spin.setValue(1.25)
    widget._autofocus_range_spin.setValue(0.75)
    widget._autofocus_fine_spin.setValue(0.006)

    widget.to_settings(settings)

    objectives = settings.objectives
    assert objectives.active_name == "X20"
    assert not objectives.apply_offsets_on_change
    assert objectives.objectives["X5"].magnification == 5.0
    x20 = objectives.objectives["X20"]
    assert x20.name == "X20"
    assert x20.magnification == pytest.approx(21.5)
    assert x20.xy_offset_configured
    assert x20.z_offset_configured
    assert x20.xy_offset_x_mm == pytest.approx(0.1234)
    assert x20.xy_offset_y_mm == pytest.approx(-0.4321)
    assert x20.z_offset_mm == pytest.approx(1.25)
    assert x20.autofocus_range_mm == pytest.approx(0.75)
    assert x20.autofocus_fine_step_mm == pytest.approx(0.006)
    assert x20.pixels_to_mm == [[1.0, 0.0], [0.0, 1.0]]
    assert x20.xy_calibration_configured

    widget.deleteLater()


def test_objectives_widget_saves_profile_before_switching() -> None:
    _qt_app()
    settings = Settings()
    settings.objectives = ObjectivesSettings(
        active_name="X5",
        objectives={
            "X5": ObjectiveCalibrationSettings(name="X5", magnification=5.0),
            "X20": ObjectiveCalibrationSettings(name="X20", magnification=20.0),
        },
    )
    widget = ObjectivesSettingsWidget(settings.objectives)

    widget._magnification_spin.setValue(5.5)
    widget._x_offset_spin.setValue(0.5)
    _set_combo_data(widget._profile_combo, "X20")
    widget._magnification_spin.setValue(20.5)
    widget._z_offset_spin.setValue(2.0)

    widget.to_settings(settings)

    assert settings.objectives.objectives["X5"].magnification == pytest.approx(5.5)
    assert settings.objectives.objectives["X5"].xy_offset_x_mm == pytest.approx(0.5)
    assert settings.objectives.objectives["X20"].magnification == pytest.approx(20.5)
    assert settings.objectives.objectives["X20"].z_offset_mm == pytest.approx(2.0)

    widget.deleteLater()


def test_coordinate_system_widget_updates_hint_state_and_settings() -> None:
    _qt_app()
    settings = Settings()
    widget = CoordinateSystemSettingsWidget(
        CoordinateSystemSettings(
            position_mode="work",
            startup_mode="controller",
            preferred_system="G55",
        )
    )

    assert widget._preferred_system_combo.isEnabled()
    assert widget._preferred_system_combo.toolTip() == (
        "Controller-selected WCS will be used."
    )

    _set_combo_data(widget._startup_mode_combo, "fixed")
    assert widget._preferred_system_combo.toolTip() == (
        "This WCS will be sent to the controller on connect."
    )

    _set_combo_data(widget._position_mode_combo, "machine")
    assert not widget._preferred_system_combo.isEnabled()
    assert widget._preferred_system_combo.toolTip() == (
        "Unused in absolute machine-coordinate mode."
    )
    _set_combo_data(widget._preferred_system_combo, "G59.1")

    widget.to_settings(settings)

    assert settings.coordinate_system == CoordinateSystemSettings(
        position_mode="machine",
        startup_mode="fixed",
        preferred_system="G59.1",
    )

    widget.deleteLater()
