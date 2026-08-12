"""Focused route-measurement meter configuration editor."""

from __future__ import annotations

from PySide6.QtCore import QLocale
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.dialogs.route_measurement_defaults import (
    CURRENT_PREFIXES,
    DEFAULT_KEITHLEY_COMPLIANCE_CURRENT_A,
    DEFAULT_KEITHLEY_CURRENT_RANGE_A,
    DEFAULT_KEITHLEY_MEASUREMENT_VOLTAGE_V,
    DEFAULT_KEITHLEY_NPLC,
    DEFAULT_KEITHLEY_SOURCE_RANGE_V,
    DEFAULT_KEITHLEY_TRIGGER_DELAY_S,
    DEFAULT_KEITHLEY_USE_BUFFER,
    DEFAULT_KEITHLEY_USE_TRIGGER_LINK,
    DEFAULT_KEITHLEY_VOLTMETER_RANGE_V,
    FREQUENCY_PREFIXES,
    VOLTAGE_PREFIXES,
)
from probe_station_gui.dialogs.route_measurement_widgets import (
    SIPrefixSpinBox,
    apply_profile_numeric_value,
)
from probe_station_gui.route.meter_config import (
    GWInstekRouteMeterSettings,
    KeithleyRouteMeterSettings,
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY,
    ROUTE_METER_KEITHLEY_2400,
    ROUTE_METER_LABELS,
    ROUTE_METER_TYPES,
    RouteMeterConfiguration,
)
from probe_station_gui.settings.needle_calibration_config import (
    LCR_APERTURE_RATES,
    LCR_LEVEL_MODES,
    LCR_MEASUREMENT_FUNCTIONS,
    LCR_RANGE_MODES,
    LCR_SOURCE_RESISTANCES_OHM,
)
from probe_station_gui.shared.wheel_guard import (
    GuardedComboBox as QComboBox,
    GuardedDoubleSpinBox as QDoubleSpinBox,
    GuardedSpinBox as QSpinBox,
)


class RouteMeasurementMeterEditor(QGroupBox):
    """Own meter-specific widgets, profile mapping, and configuration DTOs."""

    def __init__(
        self,
        *,
        default_meter_type: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("Instrument", parent)
        layout = QVBoxLayout(self)
        meter_form = QFormLayout()
        self._meter_combo = QComboBox(self)
        for meter_type in ROUTE_METER_TYPES:
            self._meter_combo.addItem(ROUTE_METER_LABELS[meter_type], meter_type)
        meter_index = self._meter_combo.findData(default_meter_type)
        if meter_index < 0:
            meter_index = self._meter_combo.findData(ROUTE_METER_KEITHLEY)
        if meter_index >= 0:
            self._meter_combo.setCurrentIndex(meter_index)
        meter_form.addRow(QLabel("Type", self), self._meter_combo)
        layout.addLayout(meter_form)

        self._meter_stack = QStackedWidget(self)
        self._gwinstek_page = self._build_gwinstek_page()
        self._keithley_page = self._build_keithley_page()
        self._meter_stack.addWidget(self._gwinstek_page)
        self._meter_stack.addWidget(self._keithley_page)
        layout.addWidget(self._meter_stack)

        self._meter_combo.currentIndexChanged.connect(self._update_meter_page)
        self._gw_function_combo.currentTextChanged.connect(
            lambda _text: self._update_gwinstek_state()
        )
        self._gw_range_mode_combo.currentIndexChanged.connect(
            lambda _index: self._update_gwinstek_state()
        )
        self._gw_bias_checkbox.toggled.connect(
            lambda _checked: self._update_gwinstek_state()
        )
        self._update_meter_page()

    def configuration(self) -> RouteMeterConfiguration:
        meter_type = str(self._meter_combo.currentData() or ROUTE_METER_KEITHLEY)
        source_only = meter_type == ROUTE_METER_KEITHLEY_2400
        return RouteMeterConfiguration(
            meter_type=meter_type,
            gwinstek=GWInstekRouteMeterSettings(
                measurement_function=self._gw_function_combo.currentText(),
                range_mode=str(self._gw_range_mode_combo.currentData() or "AUTO"),
                impedance_range=int(self._gw_impedance_range_spin.value()),
                dcr_range=int(self._gw_dcr_range_spin.value()),
                frequency_hz=self._gw_frequency_spin.base_value(),
                level_mode=str(self._gw_level_mode_combo.currentData() or "VOLTAGE"),
                voltage_level_v=self._gw_voltage_spin.base_value(),
                current_level_a=self._gw_current_spin.base_value(),
                source_resistance_ohm=int(
                    self._gw_source_resistance_combo.currentData() or 100
                ),
                aperture_rate=self._gw_aperture_combo.currentText(),
                aperture_averages=int(self._gw_averages_spin.value()),
                trigger_delay_s=float(self._gw_trigger_delay_spin.value()),
                bias_enabled=self._gw_bias_checkbox.isChecked(),
                bias_level_v=self._gw_bias_spin.base_value(),
            ),
            keithley=KeithleyRouteMeterSettings(
                measurement_voltage_v=self._keithley_voltage_spin.base_value(),
                source_voltage_range_v=self._keithley_range_spin.base_value(),
                voltmeter_range_v=self._keithley_voltmeter_range_spin.base_value(),
                current_range_a=self._keithley_current_range_spin.base_value(),
                compliance_current_a=self._keithley_compliance_spin.base_value(),
                nplc=float(self._keithley_nplc_spin.value()),
                terminals=str(self._keithley_terminals_combo.currentData() or "rear"),
                trigger_delay_s=float(self._keithley_delay_spin.value()),
                use_buffer=False
                if source_only
                else self._keithley_buffer_checkbox.isChecked(),
                use_trigger_link=False
                if source_only
                else self._keithley_trigger_link_checkbox.isChecked(),
            ),
        )

    def apply_profile(self, data: object) -> None:
        if not isinstance(data, dict):
            return
        meter_type = data.get("meter_type")
        if isinstance(meter_type, str):
            self._set_combo_data(self._meter_combo, meter_type)
        self._apply_gwinstek_profile(data.get("gwinstek"))
        self._apply_keithley_profile(data.get("keithley"))
        self._update_meter_page()
        self._update_gwinstek_state()

    def apply_default_keithley_settings(self) -> None:
        self._keithley_voltage_spin.set_base_value(
            DEFAULT_KEITHLEY_MEASUREMENT_VOLTAGE_V
        )
        self._keithley_range_spin.set_base_value(DEFAULT_KEITHLEY_SOURCE_RANGE_V)
        self._keithley_voltmeter_range_spin.set_base_value(
            DEFAULT_KEITHLEY_VOLTMETER_RANGE_V
        )
        self._keithley_current_range_spin.set_base_value(
            DEFAULT_KEITHLEY_CURRENT_RANGE_A
        )
        self._keithley_compliance_spin.set_base_value(
            DEFAULT_KEITHLEY_COMPLIANCE_CURRENT_A
        )
        self._keithley_nplc_spin.setValue(DEFAULT_KEITHLEY_NPLC)
        self._keithley_delay_spin.setValue(DEFAULT_KEITHLEY_TRIGGER_DELAY_S)
        self._keithley_buffer_checkbox.setChecked(DEFAULT_KEITHLEY_USE_BUFFER)
        self._keithley_trigger_link_checkbox.setChecked(
            DEFAULT_KEITHLEY_USE_TRIGGER_LINK
        )

    def _build_gwinstek_page(self) -> QWidget:
        page = QWidget(self)
        layout = QFormLayout(page)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._gw_function_combo = QComboBox(page)
        self._gw_function_combo.addItems(LCR_MEASUREMENT_FUNCTIONS)
        self._gw_function_combo.setCurrentText("DCR")
        layout.addRow(QLabel("Function", page), self._gw_function_combo)

        self._gw_range_mode_combo = QComboBox(page)
        for mode in LCR_RANGE_MODES:
            label = "Fixed range" if mode == "HOLD" else "Auto range"
            self._gw_range_mode_combo.addItem(label, mode)
        self._gw_range_mode_combo.setCurrentIndex(
            self._gw_range_mode_combo.findData("AUTO")
        )
        layout.addRow(QLabel("Range mode", page), self._gw_range_mode_combo)

        self._gw_impedance_range_spin = QSpinBox(page)
        self._gw_impedance_range_spin.setRange(0, 8)
        self._gw_impedance_range_spin.setValue(3)
        layout.addRow(QLabel("Impedance range", page), self._gw_impedance_range_spin)

        self._gw_dcr_range_spin = QSpinBox(page)
        self._gw_dcr_range_spin.setRange(0, 8)
        self._gw_dcr_range_spin.setValue(4)
        layout.addRow(QLabel("DCR range", page), self._gw_dcr_range_spin)

        self._gw_frequency_spin = self._prefix_row(
            layout, page, "AC frequency", FREQUENCY_PREFIXES, 10.0, 300_000.0, 50.0
        )

        self._gw_level_mode_combo = QComboBox(page)
        for mode in LCR_LEVEL_MODES:
            self._gw_level_mode_combo.addItem(mode.title(), mode)
        layout.addRow(QLabel("AC level mode", page), self._gw_level_mode_combo)

        self._gw_voltage_spin = self._prefix_row(
            layout, page, "AC voltage", VOLTAGE_PREFIXES, 0.01, 2.0, 0.03
        )

        self._gw_current_spin = self._prefix_row(
            layout, page, "AC current", CURRENT_PREFIXES, 100e-6, 20e-3, 100e-6
        )

        self._gw_source_resistance_combo = QComboBox(page)
        for resistance in LCR_SOURCE_RESISTANCES_OHM:
            self._gw_source_resistance_combo.addItem(f"{resistance} ohm", resistance)
        self._gw_source_resistance_combo.setCurrentIndex(
            self._gw_source_resistance_combo.findData(100)
        )
        layout.addRow(
            QLabel("Source resistance", page), self._gw_source_resistance_combo
        )

        self._gw_aperture_combo = QComboBox(page)
        self._gw_aperture_combo.addItems(LCR_APERTURE_RATES)
        self._gw_aperture_combo.setCurrentText("SLOW")
        layout.addRow(QLabel("Speed", page), self._gw_aperture_combo)

        self._gw_averages_spin = QSpinBox(page)
        self._gw_averages_spin.setRange(1, 256)
        self._gw_averages_spin.setValue(1)
        layout.addRow(QLabel("Averages", page), self._gw_averages_spin)

        self._gw_trigger_delay_spin = QDoubleSpinBox(page)
        self._gw_trigger_delay_spin.setLocale(QLocale.c())
        self._gw_trigger_delay_spin.setDecimals(3)
        self._gw_trigger_delay_spin.setRange(0.0, 60.0)
        self._gw_trigger_delay_spin.setSuffix(" s")
        layout.addRow(QLabel("Trigger delay", page), self._gw_trigger_delay_spin)

        self._gw_bias_checkbox = QCheckBox("Enable DC bias", page)
        layout.addRow(self._gw_bias_checkbox)
        self._gw_bias_spin = self._prefix_row(
            layout, page, "DC bias", VOLTAGE_PREFIXES, -2.5, 2.5, 0.0
        )
        self._update_gwinstek_state()
        return page

    def _build_keithley_page(self) -> QWidget:
        page = QWidget(self)
        layout = QFormLayout(page)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._keithley_voltage_spin = self._prefix_row(
            layout,
            page,
            "+/- voltage",
            VOLTAGE_PREFIXES,
            1e-6,
            10.0,
            DEFAULT_KEITHLEY_MEASUREMENT_VOLTAGE_V,
        )
        self._keithley_range_spin = self._prefix_row(
            layout,
            page,
            "Source range",
            VOLTAGE_PREFIXES,
            0.2,
            210.0,
            DEFAULT_KEITHLEY_SOURCE_RANGE_V,
        )
        self._keithley_compliance_spin = self._prefix_row(
            layout,
            page,
            "Compliance current",
            CURRENT_PREFIXES,
            1e-6,
            1.0,
            DEFAULT_KEITHLEY_COMPLIANCE_CURRENT_A,
        )
        self._keithley_current_range_spin = self._prefix_row(
            layout,
            page,
            "Current range",
            CURRENT_PREFIXES,
            1e-6,
            1.0,
            DEFAULT_KEITHLEY_CURRENT_RANGE_A,
        )
        self._keithley_voltmeter_range_spin = self._prefix_row(
            layout,
            page,
            "Voltmeter range",
            VOLTAGE_PREFIXES,
            0.01,
            100.0,
            DEFAULT_KEITHLEY_VOLTMETER_RANGE_V,
        )

        self._keithley_nplc_spin = QDoubleSpinBox(page)
        self._keithley_nplc_spin.setLocale(QLocale.c())
        self._keithley_nplc_spin.setDecimals(2)
        self._keithley_nplc_spin.setRange(0.01, 50.0)
        self._keithley_nplc_spin.setSingleStep(1.0)
        self._keithley_nplc_spin.setValue(DEFAULT_KEITHLEY_NPLC)
        layout.addRow(QLabel("NPLC", page), self._keithley_nplc_spin)
        self._keithley_terminals_combo = QComboBox(page)
        self._keithley_terminals_combo.addItem("Rear", "rear")
        self._keithley_terminals_combo.addItem("Front", "front")
        layout.addRow(QLabel("2400 terminals", page), self._keithley_terminals_combo)
        self._keithley_delay_spin = QDoubleSpinBox(page)
        self._keithley_delay_spin.setLocale(QLocale.c())
        self._keithley_delay_spin.setDecimals(3)
        self._keithley_delay_spin.setRange(0.0, 10.0)
        self._keithley_delay_spin.setSingleStep(0.01)
        self._keithley_delay_spin.setSuffix(" s")
        self._keithley_delay_spin.setValue(DEFAULT_KEITHLEY_TRIGGER_DELAY_S)
        layout.addRow(QLabel("Trigger delay", page), self._keithley_delay_spin)
        self._keithley_buffer_checkbox = QCheckBox("Use buffer", page)
        self._keithley_buffer_checkbox.setChecked(DEFAULT_KEITHLEY_USE_BUFFER)
        layout.addRow(self._keithley_buffer_checkbox)
        self._keithley_trigger_link_checkbox = QCheckBox("Use Trigger Link", page)
        self._keithley_trigger_link_checkbox.setChecked(
            DEFAULT_KEITHLEY_USE_TRIGGER_LINK
        )
        layout.addRow(self._keithley_trigger_link_checkbox)
        return page

    def _update_meter_page(self) -> None:
        meter_type = str(self._meter_combo.currentData() or ROUTE_METER_KEITHLEY)
        self._meter_stack.setCurrentWidget(
            self._gwinstek_page
            if meter_type == ROUTE_METER_GWINSTEK
            else self._keithley_page
        )
        source_only = meter_type == ROUTE_METER_KEITHLEY_2400
        for widget in (
            self._keithley_voltmeter_range_spin,
            self._keithley_buffer_checkbox,
            self._keithley_trigger_link_checkbox,
        ):
            widget.setEnabled(not source_only)

    def _update_gwinstek_state(self) -> None:
        function = self._gw_function_combo.currentText()
        range_mode = str(self._gw_range_mode_combo.currentData() or "AUTO")
        dcr_mode = function == "DCR"
        fixed_range = range_mode == "HOLD"
        self._gw_dcr_range_spin.setEnabled(dcr_mode and fixed_range)
        self._gw_impedance_range_spin.setEnabled(not dcr_mode and fixed_range)
        for widget in (
            self._gw_frequency_spin,
            self._gw_level_mode_combo,
            self._gw_voltage_spin,
            self._gw_current_spin,
            self._gw_source_resistance_combo,
            self._gw_bias_checkbox,
            self._gw_bias_spin,
        ):
            widget.setEnabled(not dcr_mode)
        self._gw_bias_spin.setEnabled(
            not dcr_mode and self._gw_bias_checkbox.isChecked()
        )

    def _apply_gwinstek_profile(self, data: object) -> None:
        if not isinstance(data, dict):
            return
        self._set_combo_text(self._gw_function_combo, data.get("measurement_function"))
        self._set_combo_data(self._gw_range_mode_combo, data.get("range_mode"))
        apply_profile_numeric_value(
            self._gw_impedance_range_spin, data.get("impedance_range")
        )
        apply_profile_numeric_value(self._gw_dcr_range_spin, data.get("dcr_range"))
        apply_profile_numeric_value(self._gw_frequency_spin, data.get("frequency_hz"))
        self._set_combo_data(self._gw_level_mode_combo, data.get("level_mode"))
        apply_profile_numeric_value(self._gw_voltage_spin, data.get("voltage_level_v"))
        apply_profile_numeric_value(self._gw_current_spin, data.get("current_level_a"))
        self._set_combo_data(
            self._gw_source_resistance_combo, data.get("source_resistance_ohm")
        )
        self._set_combo_text(self._gw_aperture_combo, data.get("aperture_rate"))
        apply_profile_numeric_value(
            self._gw_averages_spin, data.get("aperture_averages")
        )
        apply_profile_numeric_value(
            self._gw_trigger_delay_spin, data.get("trigger_delay_s")
        )
        if isinstance(data.get("bias_enabled"), bool):
            self._gw_bias_checkbox.setChecked(bool(data["bias_enabled"]))
        apply_profile_numeric_value(self._gw_bias_spin, data.get("bias_level_v"))

    def _apply_keithley_profile(self, data: object) -> None:
        if not isinstance(data, dict):
            return
        fields = (
            (self._keithley_voltage_spin, "measurement_voltage_v"),
            (self._keithley_range_spin, "source_voltage_range_v"),
            (self._keithley_voltmeter_range_spin, "voltmeter_range_v"),
            (self._keithley_current_range_spin, "current_range_a"),
            (self._keithley_compliance_spin, "compliance_current_a"),
            (self._keithley_nplc_spin, "nplc"),
            (self._keithley_delay_spin, "trigger_delay_s"),
        )
        for widget, key in fields:
            apply_profile_numeric_value(widget, data.get(key))
        self._set_combo_data(self._keithley_terminals_combo, data.get("terminals"))
        use_buffer = data.get("use_buffer")
        if isinstance(use_buffer, bool):
            self._keithley_buffer_checkbox.setChecked(use_buffer)
        use_trigger_link = data.get("use_trigger_link")
        if isinstance(use_trigger_link, bool):
            self._keithley_trigger_link_checkbox.setChecked(use_trigger_link)

    @staticmethod
    def _prefix_row(
        layout: QFormLayout,
        parent: QWidget,
        label: str,
        prefixes: tuple[tuple[str, float], ...],
        minimum: float,
        maximum: float,
        value: float,
    ) -> SIPrefixSpinBox:
        spin = SIPrefixSpinBox(
            prefixes=prefixes,
            base_minimum=minimum,
            base_maximum=maximum,
            base_value=value,
            parent=parent,
        )
        layout.addRow(QLabel(label, parent), spin)
        return spin

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    @staticmethod
    def _set_combo_text(combo: QComboBox, value: object) -> None:
        if not isinstance(value, str):
            return
        index = combo.findText(value)
        if index >= 0:
            combo.setCurrentIndex(index)


__all__ = ["RouteMeasurementMeterEditor"]
