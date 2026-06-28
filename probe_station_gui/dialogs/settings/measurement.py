"""Measurement-instrument settings widgets for the settings dialog."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtWidgets import QCheckBox, QFormLayout, QLabel, QLineEdit, QWidget

from probe_station_gui.settings.manager import Settings
from probe_station_gui.settings.needle_calibration_config import (
    LCR_APERTURE_RATES,
    LCR_MEASUREMENT_FUNCTIONS,
    LCR_METER_TYPE_GWINSTEK,
    LCR_METER_TYPE_LABELS,
    LCR_METER_TYPES,
    LCR_MONITOR_PARAMETERS,
    LCR_RANGE_MODES,
    LCR_SOURCE_RESISTANCES_OHM,
    LCR_TRIGGER_SOURCES,
    NeedleCalibrationSettings,
)
from probe_station_gui.shared.wheel_guard import (
    GuardedComboBox as QComboBox,
    GuardedDoubleSpinBox as QDoubleSpinBox,
    GuardedSpinBox as QSpinBox,
)


@dataclass(frozen=True)
class MeasurementControlState:
    """Enabled-state policy for measurement-instrument settings controls."""

    visa_resource: bool
    keithley_resources: bool
    function: bool
    range_mode: bool
    aperture: bool
    averages: bool
    trigger_source: bool
    trigger_delay: bool
    dcr_range: bool
    impedance_range: bool
    frequency: bool
    level_mode: bool
    voltage_level: bool
    current_level: bool
    source_resistance: bool
    bias: bool
    bias_level: bool
    monitor: bool
    alc: bool


def measurement_control_state(
    *,
    meter_type: str,
    measurement_function: str,
    range_mode: str,
    level_mode: str,
    bias_enabled: bool,
) -> MeasurementControlState:
    """Return enabled states for measurement controls without touching Qt widgets."""

    gwinstek_meter = meter_type == LCR_METER_TYPE_GWINSTEK
    fixed_range = range_mode == "HOLD"
    dcr_mode = measurement_function == "DCR"
    ac_mode = gwinstek_meter and not dcr_mode
    voltage_level_mode = level_mode == "VOLTAGE"
    current_level_mode = level_mode == "CURRENT"
    return MeasurementControlState(
        visa_resource=gwinstek_meter,
        keithley_resources=not gwinstek_meter,
        function=gwinstek_meter,
        range_mode=gwinstek_meter,
        aperture=gwinstek_meter,
        averages=gwinstek_meter,
        trigger_source=gwinstek_meter,
        trigger_delay=gwinstek_meter,
        dcr_range=gwinstek_meter and fixed_range and dcr_mode,
        impedance_range=gwinstek_meter and fixed_range and not dcr_mode,
        frequency=ac_mode,
        level_mode=ac_mode,
        voltage_level=ac_mode and voltage_level_mode,
        current_level=ac_mode and current_level_mode,
        source_resistance=ac_mode,
        bias=ac_mode,
        bias_level=ac_mode and bias_enabled,
        monitor=ac_mode,
        alc=ac_mode,
    )


class MeasurementSettingsWidget(QWidget):
    """Tab that exposes measurement-instrument settings."""

    def __init__(
        self,
        calibration_settings: NeedleCalibrationSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._meter_type_combo = QComboBox(self)
        for meter_type in LCR_METER_TYPES:
            self._meter_type_combo.addItem(
                LCR_METER_TYPE_LABELS.get(meter_type, meter_type),
                meter_type,
            )
        meter_type_index = self._meter_type_combo.findData(
            calibration_settings.meter_type
        )
        if meter_type_index >= 0:
            self._meter_type_combo.setCurrentIndex(meter_type_index)
        layout.addRow(QLabel("Instrument type", self), self._meter_type_combo)

        self._visa_resource_edit = QLineEdit(self)
        self._visa_resource_edit.setPlaceholderText("COM4 or ASRL4::INSTR")
        self._visa_resource_edit.setText(calibration_settings.visa_resource)
        layout.addRow(QLabel("GW Instek resource", self), self._visa_resource_edit)

        self._keithley_source_resource_edit = QLineEdit(self)
        self._keithley_source_resource_edit.setPlaceholderText("GPIB2::1::INSTR")
        self._keithley_source_resource_edit.setText(
            calibration_settings.keithley_source_resource
        )
        layout.addRow(
            QLabel("Keithley 2400 resource", self),
            self._keithley_source_resource_edit,
        )

        self._keithley_voltmeter_resource_edit = QLineEdit(self)
        self._keithley_voltmeter_resource_edit.setPlaceholderText("GPIB2::2::INSTR")
        self._keithley_voltmeter_resource_edit.setText(
            calibration_settings.keithley_voltmeter_resource
        )
        layout.addRow(
            QLabel("Keithley 2182A resource", self),
            self._keithley_voltmeter_resource_edit,
        )

        self._function_combo = QComboBox(self)
        self._function_combo.addItems(LCR_MEASUREMENT_FUNCTIONS)
        self._function_combo.setCurrentText(calibration_settings.measurement_function)
        layout.addRow(QLabel("Measurement function", self), self._function_combo)

        self._range_mode_combo = QComboBox(self)
        for mode in LCR_RANGE_MODES:
            label = "Fixed range (HOLD)" if mode == "HOLD" else "Auto range"
            self._range_mode_combo.addItem(label, mode)
        range_index = self._range_mode_combo.findData(calibration_settings.range_mode)
        if range_index >= 0:
            self._range_mode_combo.setCurrentIndex(range_index)
        layout.addRow(QLabel("Range mode", self), self._range_mode_combo)

        self._impedance_range_spin = QSpinBox(self)
        self._impedance_range_spin.setRange(0, 8)
        self._impedance_range_spin.setSingleStep(1)
        self._impedance_range_spin.setValue(calibration_settings.impedance_range)
        layout.addRow(QLabel("Impedance range", self), self._impedance_range_spin)

        self._dcr_range_spin = QSpinBox(self)
        self._dcr_range_spin.setRange(0, 8)
        self._dcr_range_spin.setSingleStep(1)
        self._dcr_range_spin.setValue(calibration_settings.dcr_range)
        layout.addRow(QLabel("DCR range", self), self._dcr_range_spin)

        self._frequency_spin = QDoubleSpinBox(self)
        self._frequency_spin.setDecimals(3)
        self._frequency_spin.setRange(10.0, 300_000.0)
        self._frequency_spin.setSingleStep(100.0)
        self._frequency_spin.setSuffix(" Hz")
        self._frequency_spin.setValue(calibration_settings.frequency_hz)
        layout.addRow(QLabel("AC frequency", self), self._frequency_spin)

        self._level_mode_combo = QComboBox(self)
        self._level_mode_combo.addItem("Voltage", "VOLTAGE")
        self._level_mode_combo.addItem("Current", "CURRENT")
        level_mode_index = self._level_mode_combo.findData(
            calibration_settings.level_mode
        )
        if level_mode_index >= 0:
            self._level_mode_combo.setCurrentIndex(level_mode_index)
        layout.addRow(QLabel("AC level mode", self), self._level_mode_combo)

        self._voltage_level_spin = QDoubleSpinBox(self)
        self._voltage_level_spin.setDecimals(4)
        self._voltage_level_spin.setRange(0.01, 2.0)
        self._voltage_level_spin.setSingleStep(0.01)
        self._voltage_level_spin.setSuffix(" V")
        self._voltage_level_spin.setValue(calibration_settings.voltage_level_v)
        layout.addRow(QLabel("AC voltage level", self), self._voltage_level_spin)

        self._current_level_spin = QDoubleSpinBox(self)
        self._current_level_spin.setDecimals(6)
        self._current_level_spin.setRange(0.0001, 0.02)
        self._current_level_spin.setSingleStep(0.0001)
        self._current_level_spin.setSuffix(" A")
        self._current_level_spin.setValue(calibration_settings.current_level_a)
        layout.addRow(QLabel("AC current level", self), self._current_level_spin)

        self._source_resistance_combo = QComboBox(self)
        for resistance_ohm in LCR_SOURCE_RESISTANCES_OHM:
            self._source_resistance_combo.addItem(
                f"{resistance_ohm} ohm", resistance_ohm
            )
        source_index = self._source_resistance_combo.findData(
            calibration_settings.source_resistance_ohm
        )
        if source_index >= 0:
            self._source_resistance_combo.setCurrentIndex(source_index)
        layout.addRow(QLabel("Source resistance", self), self._source_resistance_combo)

        self._aperture_combo = QComboBox(self)
        self._aperture_combo.addItems(LCR_APERTURE_RATES)
        self._aperture_combo.setCurrentText(calibration_settings.aperture_rate)
        layout.addRow(QLabel("Measurement speed", self), self._aperture_combo)

        self._averages_spin = QSpinBox(self)
        self._averages_spin.setRange(1, 256)
        self._averages_spin.setSingleStep(1)
        self._averages_spin.setValue(calibration_settings.aperture_averages)
        layout.addRow(QLabel("Averaging factor", self), self._averages_spin)

        self._trigger_source_combo = QComboBox(self)
        self._trigger_source_combo.addItems(LCR_TRIGGER_SOURCES)
        self._trigger_source_combo.setCurrentText(calibration_settings.trigger_source)
        layout.addRow(QLabel("Trigger source", self), self._trigger_source_combo)

        self._trigger_delay_spin = QDoubleSpinBox(self)
        self._trigger_delay_spin.setDecimals(3)
        self._trigger_delay_spin.setRange(0.0, 60.0)
        self._trigger_delay_spin.setSingleStep(0.01)
        self._trigger_delay_spin.setSuffix(" s")
        self._trigger_delay_spin.setValue(calibration_settings.trigger_delay_s)
        layout.addRow(QLabel("Trigger delay", self), self._trigger_delay_spin)

        self._bias_checkbox = QCheckBox("Enable DC bias", self)
        self._bias_checkbox.setChecked(calibration_settings.bias_enabled)
        layout.addRow(self._bias_checkbox)

        self._bias_level_spin = QDoubleSpinBox(self)
        self._bias_level_spin.setDecimals(3)
        self._bias_level_spin.setRange(-2.5, 2.5)
        self._bias_level_spin.setSingleStep(0.01)
        self._bias_level_spin.setSuffix(" V")
        self._bias_level_spin.setValue(calibration_settings.bias_level_v)
        layout.addRow(QLabel("DC bias level", self), self._bias_level_spin)

        self._monitor1_combo = QComboBox(self)
        self._monitor1_combo.addItems(LCR_MONITOR_PARAMETERS)
        self._monitor1_combo.setCurrentText(calibration_settings.monitor1)
        layout.addRow(QLabel("Monitor 1", self), self._monitor1_combo)

        self._monitor2_combo = QComboBox(self)
        self._monitor2_combo.addItems(LCR_MONITOR_PARAMETERS)
        self._monitor2_combo.setCurrentText(calibration_settings.monitor2)
        layout.addRow(QLabel("Monitor 2", self), self._monitor2_combo)

        self._alc_checkbox = QCheckBox("Enable ALC", self)
        self._alc_checkbox.setChecked(calibration_settings.alc_enabled)
        layout.addRow(self._alc_checkbox)

        self._short_threshold_spin = QDoubleSpinBox(self)
        self._short_threshold_spin.setDecimals(3)
        self._short_threshold_spin.setRange(0.0, 1_000_000.0)
        self._short_threshold_spin.setSingleStep(0.5)
        self._short_threshold_spin.setSuffix(" ohm")
        self._short_threshold_spin.setValue(calibration_settings.short_threshold_ohm)
        layout.addRow(QLabel("Short threshold", self), self._short_threshold_spin)

        self._poll_interval_spin = QDoubleSpinBox(self)
        self._poll_interval_spin.setDecimals(0)
        self._poll_interval_spin.setRange(50, 10_000)
        self._poll_interval_spin.setSingleStep(50)
        self._poll_interval_spin.setSuffix(" ms")
        self._poll_interval_spin.setValue(calibration_settings.poll_interval_ms)
        layout.addRow(QLabel("Polling interval", self), self._poll_interval_spin)

        self._function_combo.currentTextChanged.connect(
            lambda _text: self._update_lcr_control_state()
        )
        self._meter_type_combo.currentIndexChanged.connect(
            lambda _index: self._update_lcr_control_state()
        )
        self._range_mode_combo.currentIndexChanged.connect(
            lambda _index: self._update_lcr_control_state()
        )
        self._level_mode_combo.currentIndexChanged.connect(
            lambda _index: self._update_lcr_control_state()
        )
        self._bias_checkbox.toggled.connect(
            lambda _checked: self._update_lcr_control_state()
        )
        self._update_lcr_control_state()

    def _update_lcr_control_state(self) -> None:
        state = measurement_control_state(
            meter_type=str(
                self._meter_type_combo.currentData() or LCR_METER_TYPE_GWINSTEK
            ),
            measurement_function=self._function_combo.currentText(),
            range_mode=str(self._range_mode_combo.currentData() or "HOLD"),
            level_mode=str(self._level_mode_combo.currentData() or "VOLTAGE"),
            bias_enabled=self._bias_checkbox.isChecked(),
        )
        self._visa_resource_edit.setEnabled(state.visa_resource)
        self._keithley_source_resource_edit.setEnabled(state.keithley_resources)
        self._keithley_voltmeter_resource_edit.setEnabled(state.keithley_resources)
        self._function_combo.setEnabled(state.function)
        self._range_mode_combo.setEnabled(state.range_mode)
        self._aperture_combo.setEnabled(state.aperture)
        self._averages_spin.setEnabled(state.averages)
        self._trigger_source_combo.setEnabled(state.trigger_source)
        self._trigger_delay_spin.setEnabled(state.trigger_delay)
        self._dcr_range_spin.setEnabled(state.dcr_range)
        self._impedance_range_spin.setEnabled(state.impedance_range)
        self._frequency_spin.setEnabled(state.frequency)
        self._level_mode_combo.setEnabled(state.level_mode)
        self._voltage_level_spin.setEnabled(state.voltage_level)
        self._current_level_spin.setEnabled(state.current_level)
        self._source_resistance_combo.setEnabled(state.source_resistance)
        self._bias_checkbox.setEnabled(state.bias)
        self._bias_level_spin.setEnabled(state.bias_level)
        self._monitor1_combo.setEnabled(state.monitor)
        self._monitor2_combo.setEnabled(state.monitor)
        self._alc_checkbox.setEnabled(state.alc)

    def to_settings(self, settings: Settings) -> None:
        """Persist the widget state into the provided settings object."""

        needle_settings = settings.needle_calibration.clone()
        needle_settings.meter_type = str(
            self._meter_type_combo.currentData() or LCR_METER_TYPE_GWINSTEK
        )
        needle_settings.visa_resource = self._visa_resource_edit.text().strip()
        needle_settings.keithley_source_resource = (
            self._keithley_source_resource_edit.text().strip()
        )
        needle_settings.keithley_voltmeter_resource = (
            self._keithley_voltmeter_resource_edit.text().strip()
        )
        needle_settings.measurement_function = self._function_combo.currentText()
        needle_settings.range_mode = str(self._range_mode_combo.currentData() or "HOLD")
        needle_settings.auto_range_enabled = (
            str(self._range_mode_combo.currentData() or "") == "AUTO"
        )
        needle_settings.impedance_range = int(self._impedance_range_spin.value())
        needle_settings.dcr_range = int(self._dcr_range_spin.value())
        needle_settings.frequency_hz = self._frequency_spin.value()
        needle_settings.level_mode = str(
            self._level_mode_combo.currentData() or "VOLTAGE"
        )
        needle_settings.voltage_level_v = self._voltage_level_spin.value()
        needle_settings.current_level_a = self._current_level_spin.value()
        needle_settings.source_resistance_ohm = int(
            self._source_resistance_combo.currentData() or 30
        )
        needle_settings.aperture_rate = self._aperture_combo.currentText()
        needle_settings.aperture_averages = int(self._averages_spin.value())
        needle_settings.trigger_source = self._trigger_source_combo.currentText()
        needle_settings.trigger_delay_s = self._trigger_delay_spin.value()
        needle_settings.bias_enabled = self._bias_checkbox.isChecked()
        needle_settings.bias_level_v = self._bias_level_spin.value()
        needle_settings.monitor1 = self._monitor1_combo.currentText()
        needle_settings.monitor2 = self._monitor2_combo.currentText()
        needle_settings.alc_enabled = self._alc_checkbox.isChecked()
        needle_settings.short_threshold_ohm = self._short_threshold_spin.value()
        needle_settings.poll_interval_ms = int(self._poll_interval_spin.value())
        settings.needle_calibration = needle_settings


__all__ = [
    "MeasurementControlState",
    "MeasurementSettingsWidget",
    "measurement_control_state",
]
