from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.dialogs.settings.axis_settings import AxisSettingsWidget
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.precision_approach import PrecisionApproachProfile


def _qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _widget(settings: Settings) -> AxisSettingsWidget:
    _qt_app()
    return AxisSettingsWidget(
        settings.axis_a_calibration,
        settings.axis_z_calibration,
        settings.precision_approach,
    )


def test_axis_settings_selector_and_per_axis_content() -> None:
    settings = Settings()
    widget = _widget(settings)

    assert [widget._axis_list.item(index).text() for index in range(6)] == [
        "X",
        "Y",
        "Z",
        "A",
        "B",
        "C",
    ]
    assert widget.selected_axis() == "Z"
    assert tuple(widget._pages) == ("X", "Y", "Z", "A", "B", "C")
    assert widget._rows["A"].backlash_spin.suffix() == " mm"
    assert widget._rows["B"].backlash_spin.suffix() == " °"
    assert widget._rows["Z"].enabled_checkbox.isChecked()
    assert widget._rows["Z"].preview_label.text() == (
        "Target − 0.030 mm → target"
    )
    assert widget._calibration_messages["X"].text() == (
        "No calibration curve for this axis."
    )
    assert tuple(widget._calibration_checkboxes) == ("Z", "A")

    widget.deleteLater()


def test_axis_switch_preserves_unapplied_precision_values() -> None:
    settings = Settings()
    widget = _widget(settings)
    b_row = widget._rows["B"]

    b_row.enabled_checkbox.setChecked(True)
    b_row.backlash_spin.setValue(1.25)
    b_row.direction_combo.setCurrentIndex(
        b_row.direction_combo.findData(-1)
    )
    widget.select_axis("X")
    widget.select_axis("B")

    assert b_row.preview_label.text() == "Target + 1.250 ° → target"
    widget.to_settings(settings)
    assert settings.precision_approach.profiles["B"] == PrecisionApproachProfile(
        True,
        1.25,
        -1,
    )

    widget.deleteLater()


def test_axis_calibration_controls_preserve_read_only_metadata() -> None:
    settings = Settings()
    settings.axis_a_calibration.configured = True
    settings.axis_z_calibration.configured = True
    original_a = settings.axis_a_calibration.clone()
    original_z = settings.axis_z_calibration.clone()
    widget = _widget(settings)

    assert widget._calibration_sources["A"].text() == original_a.source
    assert widget._calibration_sources["Z"].text() == original_z.source
    assert "RMSE" in widget._calibration_errors["A"].text()
    assert "max" in widget._calibration_errors["Z"].text()

    widget._calibration_checkboxes["A"].setChecked(False)
    widget.to_settings(settings)

    assert settings.axis_a_calibration.configured is False
    assert settings.axis_z_calibration.configured is True
    assert settings.axis_a_calibration.source == original_a.source
    assert settings.axis_a_calibration.amplitude_mm == original_a.amplitude_mm
    assert settings.axis_z_calibration.source == original_z.source
    assert settings.axis_z_calibration.coefficients_mm == original_z.coefficients_mm

    widget.deleteLater()
