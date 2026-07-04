from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.dialogs.settings.measurement import (
    MeasurementSettingsWidget,
    measurement_control_state,
)
from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.needle_calibration_config import (
    LCR_METER_TYPE_GWINSTEK,
    LCR_METER_TYPE_KEITHLEY,
    LCR_METER_TYPE_KEITHLEY_2400,
    NeedleCalibrationSettings,
    SavedStagePositionSettings,
)


def _qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _set_combo_data(combo, data: str) -> None:
    index = combo.findData(data)
    assert index >= 0
    combo.setCurrentIndex(index)


def test_measurement_control_state_matches_existing_lcr_cases() -> None:
    gwinstek_ac_auto = measurement_control_state(
        meter_type=LCR_METER_TYPE_GWINSTEK,
        measurement_function="R-X",
        range_mode="AUTO",
        level_mode="VOLTAGE",
        bias_enabled=True,
    )

    assert gwinstek_ac_auto.visa_resource
    assert not gwinstek_ac_auto.keithley_source_resource
    assert not gwinstek_ac_auto.keithley_voltmeter_resource
    assert gwinstek_ac_auto.frequency
    assert gwinstek_ac_auto.voltage_level
    assert not gwinstek_ac_auto.current_level
    assert gwinstek_ac_auto.bias_level
    assert not gwinstek_ac_auto.impedance_range
    assert not gwinstek_ac_auto.dcr_range

    gwinstek_dcr_fixed = measurement_control_state(
        meter_type=LCR_METER_TYPE_GWINSTEK,
        measurement_function="DCR",
        range_mode="HOLD",
        level_mode="CURRENT",
        bias_enabled=True,
    )

    assert gwinstek_dcr_fixed.dcr_range
    assert not gwinstek_dcr_fixed.impedance_range
    assert not gwinstek_dcr_fixed.frequency
    assert not gwinstek_dcr_fixed.level_mode
    assert not gwinstek_dcr_fixed.bias
    assert not gwinstek_dcr_fixed.monitor

    keithley = measurement_control_state(
        meter_type=LCR_METER_TYPE_KEITHLEY,
        measurement_function="R-X",
        range_mode="HOLD",
        level_mode="VOLTAGE",
        bias_enabled=True,
    )

    assert not keithley.visa_resource
    assert keithley.keithley_source_resource
    assert keithley.keithley_voltmeter_resource
    assert not keithley.function
    assert not keithley.range_mode
    assert not keithley.frequency
    assert not keithley.bias

    keithley_2400 = measurement_control_state(
        meter_type=LCR_METER_TYPE_KEITHLEY_2400,
        measurement_function="R-X",
        range_mode="HOLD",
        level_mode="VOLTAGE",
        bias_enabled=True,
    )

    assert not keithley_2400.visa_resource
    assert keithley_2400.keithley_source_resource
    assert not keithley_2400.keithley_voltmeter_resource
    assert not keithley_2400.function
    assert not keithley_2400.range_mode


def test_measurement_widget_applies_existing_enabled_states() -> None:
    _qt_app()
    widget = MeasurementSettingsWidget(
        NeedleCalibrationSettings(
            meter_type=LCR_METER_TYPE_GWINSTEK,
            measurement_function="R-X",
            range_mode="AUTO",
            level_mode="VOLTAGE",
            bias_enabled=True,
        )
    )

    assert widget._visa_resource_edit.isEnabled()
    assert not widget._keithley_source_resource_edit.isEnabled()
    assert not widget._keithley_voltmeter_resource_edit.isEnabled()
    assert widget._frequency_spin.isEnabled()
    assert widget._voltage_level_spin.isEnabled()
    assert not widget._current_level_spin.isEnabled()
    assert widget._bias_checkbox.isEnabled()
    assert widget._bias_level_spin.isEnabled()
    assert not widget._impedance_range_spin.isEnabled()
    assert not widget._dcr_range_spin.isEnabled()

    widget._function_combo.setCurrentText("DCR")
    _set_combo_data(widget._range_mode_combo, "HOLD")

    assert widget._dcr_range_spin.isEnabled()
    assert not widget._impedance_range_spin.isEnabled()
    assert not widget._frequency_spin.isEnabled()
    assert not widget._bias_checkbox.isEnabled()
    assert not widget._bias_level_spin.isEnabled()
    assert not widget._monitor1_combo.isEnabled()
    assert not widget._monitor2_combo.isEnabled()

    _set_combo_data(widget._meter_type_combo, LCR_METER_TYPE_KEITHLEY)

    assert not widget._visa_resource_edit.isEnabled()
    assert widget._keithley_source_resource_edit.isEnabled()
    assert widget._keithley_voltmeter_resource_edit.isEnabled()
    assert not widget._function_combo.isEnabled()
    assert not widget._range_mode_combo.isEnabled()
    assert not widget._trigger_source_combo.isEnabled()

    _set_combo_data(widget._meter_type_combo, LCR_METER_TYPE_KEITHLEY_2400)

    assert not widget._visa_resource_edit.isEnabled()
    assert widget._keithley_source_resource_edit.isEnabled()
    assert not widget._keithley_voltmeter_resource_edit.isEnabled()

    widget.deleteLater()


def test_measurement_widget_to_settings_preserves_unrelated_needle_fields() -> None:
    _qt_app()
    settings = Settings()
    settings.needle_calibration.contact_zone_mm = 1.25
    settings.needle_calibration.feedrate_mm_min = 22.0
    settings.needle_calibration.chip_position = SavedStagePositionSettings(
        1.0, 2.0, 3.0, True
    )
    widget = MeasurementSettingsWidget(settings.needle_calibration)

    widget._visa_resource_edit.setText(" ASRL5::INSTR ")
    widget._function_combo.setCurrentText("Cp-D")
    _set_combo_data(widget._range_mode_combo, "AUTO")
    widget._impedance_range_spin.setValue(6)
    widget._frequency_spin.setValue(1234.0)
    _set_combo_data(widget._level_mode_combo, "CURRENT")
    widget._current_level_spin.setValue(0.0025)
    widget._bias_checkbox.setChecked(True)
    widget._short_threshold_spin.setValue(42.5)
    widget._poll_interval_spin.setValue(750)

    widget.to_settings(settings)

    needle_settings = settings.needle_calibration
    assert needle_settings.contact_zone_mm == 1.25
    assert needle_settings.feedrate_mm_min == 22.0
    assert needle_settings.chip_position == SavedStagePositionSettings(
        1.0, 2.0, 3.0, True
    )
    assert needle_settings.visa_resource == "ASRL5::INSTR"
    assert needle_settings.measurement_function == "Cp-D"
    assert needle_settings.range_mode == "AUTO"
    assert needle_settings.auto_range_enabled
    assert needle_settings.impedance_range == 6
    assert needle_settings.frequency_hz == 1234.0
    assert needle_settings.level_mode == "CURRENT"
    assert needle_settings.current_level_a == 0.0025
    assert needle_settings.bias_enabled
    assert needle_settings.short_threshold_ohm == 42.5
    assert needle_settings.poll_interval_ms == 750

    widget.deleteLater()
