"""Dialog for configuring and controlling a route measurement run."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QLocale, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.lcr_meter import (
    GWInstekRouteMeterSettings,
    KeithleyRouteMeterSettings,
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY,
    ROUTE_METER_LABELS,
    ROUTE_METER_TYPES,
    RouteMeterConfiguration,
)
from probe_station_gui.settings_manager import (
    LCR_APERTURE_RATES,
    LCR_LEVEL_MODES,
    LCR_MEASUREMENT_FUNCTIONS,
    LCR_RANGE_MODES,
    LCR_SOURCE_RESISTANCES_OHM,
)


@dataclass(frozen=True)
class RouteMeasurementRunConfiguration:
    """Complete per-run route measurement configuration from the dialog."""

    csv_path: str
    measurement_count: int
    contact_settle_s: float
    meter: RouteMeterConfiguration


class RouteMeasurementDialog(QDialog):
    """Non-modal route measurement setup and Next/Cancel control window."""

    run_requested = Signal(object)
    next_requested = Signal()
    cancel_requested = Signal()

    def __init__(
        self,
        *,
        route_name: str,
        route_point_count: int,
        default_csv_path: str,
        default_meter_type: str = ROUTE_METER_KEITHLEY,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Route Measurement")
        self.setModal(False)
        self._running = False
        self._waiting = False

        layout = QVBoxLayout(self)

        common_group = QGroupBox("Run", self)
        common_layout = QFormLayout(common_group)
        common_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self._route_combo = QComboBox(common_group)
        self._route_combo.addItem(f"{route_name} ({route_point_count} points)")
        common_layout.addRow(QLabel("Route", common_group), self._route_combo)

        csv_row = QHBoxLayout()
        self._csv_path_edit = QLineEdit(common_group)
        self._csv_path_edit.setText(default_csv_path)
        self._csv_path_edit.setPlaceholderText("measurement_results.csv")
        self._csv_browse_button = QPushButton("Browse", common_group)
        csv_row.addWidget(self._csv_path_edit, 1)
        csv_row.addWidget(self._csv_browse_button)
        common_layout.addRow(QLabel("CSV", common_group), csv_row)

        self._measurement_count_spin = QSpinBox(common_group)
        self._measurement_count_spin.setRange(1, 1000)
        self._measurement_count_spin.setValue(5)
        common_layout.addRow(
            QLabel("Readings per point", common_group),
            self._measurement_count_spin,
        )

        self._contact_settle_spin = QDoubleSpinBox(common_group)
        self._contact_settle_spin.setLocale(QLocale.c())
        self._contact_settle_spin.setDecimals(3)
        self._contact_settle_spin.setRange(0.0, 60.0)
        self._contact_settle_spin.setSingleStep(0.05)
        self._contact_settle_spin.setSuffix(" s")
        self._contact_settle_spin.setValue(0.2)
        common_layout.addRow(
            QLabel("Contact settle", common_group),
            self._contact_settle_spin,
        )
        layout.addWidget(common_group)

        meter_group = QGroupBox("Instrument", self)
        meter_layout = QVBoxLayout(meter_group)
        meter_form = QFormLayout()
        self._meter_combo = QComboBox(meter_group)
        for meter_type in ROUTE_METER_TYPES:
            self._meter_combo.addItem(ROUTE_METER_LABELS[meter_type], meter_type)
        meter_index = self._meter_combo.findData(default_meter_type)
        if meter_index < 0:
            meter_index = self._meter_combo.findData(ROUTE_METER_KEITHLEY)
        if meter_index >= 0:
            self._meter_combo.setCurrentIndex(meter_index)
        meter_form.addRow(QLabel("Type", meter_group), self._meter_combo)
        meter_layout.addLayout(meter_form)

        self._meter_stack = QStackedWidget(meter_group)
        self._gwinstek_page = self._build_gwinstek_page()
        self._keithley_page = self._build_keithley_page()
        self._meter_stack.addWidget(self._gwinstek_page)
        self._meter_stack.addWidget(self._keithley_page)
        meter_layout.addWidget(self._meter_stack)
        layout.addWidget(meter_group)

        self._status_label = QLabel("Idle.", self)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        button_row = QHBoxLayout()
        self._run_button = QPushButton("Run", self)
        self._next_button = QPushButton("Next", self)
        self._cancel_button = QPushButton("Cancel", self)
        self._close_button = QPushButton("Close", self)
        button_row.addWidget(self._run_button)
        button_row.addWidget(self._next_button)
        button_row.addWidget(self._cancel_button)
        button_row.addStretch(1)
        button_row.addWidget(self._close_button)
        layout.addLayout(button_row)

        self._csv_browse_button.clicked.connect(self._choose_csv_path)
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
        self._run_button.clicked.connect(self._emit_run_requested)
        self._next_button.clicked.connect(self.next_requested.emit)
        self._cancel_button.clicked.connect(self.cancel_requested.emit)
        self._close_button.clicked.connect(self.close)
        self._update_meter_page()
        self.set_running(False)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._running:
            self.set_status("Cancel the route measurement before closing.")
            event.ignore()
            return
        super().closeEvent(event)

    def set_status(self, message: str) -> None:
        self._status_label.setText(message or "Idle.")

    def set_running(self, running: bool) -> None:
        self._running = bool(running)
        for widget in (
            self._route_combo,
            self._csv_path_edit,
            self._csv_browse_button,
            self._measurement_count_spin,
            self._contact_settle_spin,
            self._meter_combo,
            self._gwinstek_page,
            self._keithley_page,
        ):
            widget.setEnabled(not self._running)
        self._run_button.setEnabled(not self._running)
        self._cancel_button.setEnabled(self._running)
        self._close_button.setEnabled(not self._running)
        self.set_waiting(False)

    def set_waiting(self, waiting: bool) -> None:
        self._waiting = bool(waiting)
        self._next_button.setEnabled(self._running and self._waiting)

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

        self._gw_frequency_spin = QDoubleSpinBox(page)
        self._gw_frequency_spin.setLocale(QLocale.c())
        self._gw_frequency_spin.setDecimals(3)
        self._gw_frequency_spin.setRange(10.0, 300_000.0)
        self._gw_frequency_spin.setSuffix(" Hz")
        self._gw_frequency_spin.setValue(50.0)
        layout.addRow(QLabel("AC frequency", page), self._gw_frequency_spin)

        self._gw_level_mode_combo = QComboBox(page)
        for mode in LCR_LEVEL_MODES:
            self._gw_level_mode_combo.addItem(mode.title(), mode)
        layout.addRow(QLabel("AC level mode", page), self._gw_level_mode_combo)

        self._gw_voltage_spin = QDoubleSpinBox(page)
        self._gw_voltage_spin.setLocale(QLocale.c())
        self._gw_voltage_spin.setDecimals(6)
        self._gw_voltage_spin.setRange(0.000001, 2.0)
        self._gw_voltage_spin.setSuffix(" V")
        self._gw_voltage_spin.setValue(0.03)
        layout.addRow(QLabel("AC voltage", page), self._gw_voltage_spin)

        self._gw_current_spin = QDoubleSpinBox(page)
        self._gw_current_spin.setLocale(QLocale.c())
        self._gw_current_spin.setDecimals(9)
        self._gw_current_spin.setRange(0.000000001, 0.02)
        self._gw_current_spin.setSuffix(" A")
        self._gw_current_spin.setValue(0.0001)
        layout.addRow(QLabel("AC current", page), self._gw_current_spin)

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

        self._gw_bias_spin = QDoubleSpinBox(page)
        self._gw_bias_spin.setLocale(QLocale.c())
        self._gw_bias_spin.setDecimals(3)
        self._gw_bias_spin.setRange(-2.5, 2.5)
        self._gw_bias_spin.setSuffix(" V")
        layout.addRow(QLabel("DC bias", page), self._gw_bias_spin)

        self._update_gwinstek_state()
        return page

    def _build_keithley_page(self) -> QWidget:
        page = QWidget(self)
        layout = QFormLayout(page)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._keithley_voltage_spin = QDoubleSpinBox(page)
        self._keithley_voltage_spin.setLocale(QLocale.c())
        self._keithley_voltage_spin.setDecimals(6)
        self._keithley_voltage_spin.setRange(0.000001, 10.0)
        self._keithley_voltage_spin.setSingleStep(0.001)
        self._keithley_voltage_spin.setSuffix(" V")
        self._keithley_voltage_spin.setValue(0.03)
        layout.addRow(QLabel("+/- voltage", page), self._keithley_voltage_spin)

        self._keithley_range_spin = QDoubleSpinBox(page)
        self._keithley_range_spin.setLocale(QLocale.c())
        self._keithley_range_spin.setDecimals(6)
        self._keithley_range_spin.setRange(0.000001, 210.0)
        self._keithley_range_spin.setSingleStep(0.01)
        self._keithley_range_spin.setSuffix(" V")
        self._keithley_range_spin.setValue(0.21)
        layout.addRow(QLabel("Source range", page), self._keithley_range_spin)

        self._keithley_compliance_spin = QDoubleSpinBox(page)
        self._keithley_compliance_spin.setLocale(QLocale.c())
        self._keithley_compliance_spin.setDecimals(9)
        self._keithley_compliance_spin.setRange(0.000000001, 1.0)
        self._keithley_compliance_spin.setSingleStep(0.000001)
        self._keithley_compliance_spin.setSuffix(" A")
        self._keithley_compliance_spin.setValue(500e-6)
        layout.addRow(
            QLabel("Compliance current", page), self._keithley_compliance_spin
        )

        self._keithley_nplc_spin = QDoubleSpinBox(page)
        self._keithley_nplc_spin.setLocale(QLocale.c())
        self._keithley_nplc_spin.setDecimals(2)
        self._keithley_nplc_spin.setRange(0.01, 50.0)
        self._keithley_nplc_spin.setSingleStep(1.0)
        self._keithley_nplc_spin.setValue(10.0)
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
        self._keithley_delay_spin.setValue(0.01)
        layout.addRow(QLabel("Trigger delay", page), self._keithley_delay_spin)

        return page

    def _choose_csv_path(self) -> None:
        current = self._csv_path_edit.text().strip()
        start = current or str(Path.cwd() / "probe_route_measurements.csv")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Route Measurements",
            start,
            "CSV files (*.csv);;All files (*)",
        )
        if path:
            self._csv_path_edit.setText(path)

    def _update_meter_page(self) -> None:
        meter_type = str(self._meter_combo.currentData() or ROUTE_METER_KEITHLEY)
        self._meter_stack.setCurrentWidget(
            self._gwinstek_page
            if meter_type == ROUTE_METER_GWINSTEK
            else self._keithley_page
        )

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

    def _emit_run_requested(self) -> None:
        csv_path = self._csv_path_edit.text().strip()
        if not csv_path:
            self.set_status("Choose a CSV path before running.")
            return
        self.run_requested.emit(
            RouteMeasurementRunConfiguration(
                csv_path=csv_path,
                measurement_count=int(self._measurement_count_spin.value()),
                contact_settle_s=float(self._contact_settle_spin.value()),
                meter=self._meter_configuration(),
            )
        )

    def _meter_configuration(self) -> RouteMeterConfiguration:
        meter_type = str(self._meter_combo.currentData() or ROUTE_METER_KEITHLEY)
        return RouteMeterConfiguration(
            meter_type=meter_type,
            gwinstek=GWInstekRouteMeterSettings(
                measurement_function=self._gw_function_combo.currentText(),
                range_mode=str(self._gw_range_mode_combo.currentData() or "AUTO"),
                impedance_range=int(self._gw_impedance_range_spin.value()),
                dcr_range=int(self._gw_dcr_range_spin.value()),
                frequency_hz=float(self._gw_frequency_spin.value()),
                level_mode=str(self._gw_level_mode_combo.currentData() or "VOLTAGE"),
                voltage_level_v=float(self._gw_voltage_spin.value()),
                current_level_a=float(self._gw_current_spin.value()),
                source_resistance_ohm=int(
                    self._gw_source_resistance_combo.currentData() or 100
                ),
                aperture_rate=self._gw_aperture_combo.currentText(),
                aperture_averages=int(self._gw_averages_spin.value()),
                trigger_delay_s=float(self._gw_trigger_delay_spin.value()),
                bias_enabled=self._gw_bias_checkbox.isChecked(),
                bias_level_v=float(self._gw_bias_spin.value()),
            ),
            keithley=KeithleyRouteMeterSettings(
                measurement_voltage_v=float(self._keithley_voltage_spin.value()),
                source_voltage_range_v=float(self._keithley_range_spin.value()),
                compliance_current_a=float(self._keithley_compliance_spin.value()),
                nplc=float(self._keithley_nplc_spin.value()),
                terminals=str(self._keithley_terminals_combo.currentData() or "rear"),
                trigger_delay_s=float(self._keithley_delay_spin.value()),
            ),
        )
