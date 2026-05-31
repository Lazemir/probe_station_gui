"""Dialog for configuring and controlling a route measurement run."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from PySide6.QtCore import QLocale, QPointF, QRectF, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
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
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
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
from probe_station_gui.route_measurement import (
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_PHOTO,
    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
)
from probe_station_gui.settings_manager import (
    LCR_APERTURE_RATES,
    LCR_LEVEL_MODES,
    LCR_MEASUREMENT_FUNCTIONS,
    LCR_RANGE_MODES,
    LCR_SOURCE_RESISTANCES_OHM,
)


ROUTE_MEASUREMENT_PROFILE_VERSION = 6
DEFAULT_ROUTE_INITIAL_MEASUREMENT_COUNT = 10
DEFAULT_ROUTE_FOLLOWUP_MEASUREMENT_COUNT = 240
DEFAULT_ROUTE_PHOTO_AUTOFOCUS_RANGE_MM = 0.030
DEFAULT_KEITHLEY_MEASUREMENT_VOLTAGE_V = 0.03
DEFAULT_KEITHLEY_SOURCE_RANGE_V = 0.21
DEFAULT_KEITHLEY_VOLTMETER_RANGE_V = 0.1
DEFAULT_KEITHLEY_CURRENT_RANGE_A = 10e-6
DEFAULT_KEITHLEY_COMPLIANCE_CURRENT_A = 10e-6
DEFAULT_KEITHLEY_NPLC = 1.0
DEFAULT_KEITHLEY_TRIGGER_DELAY_S = 0.0
DEFAULT_KEITHLEY_USE_BUFFER = True
DEFAULT_KEITHLEY_USE_TRIGGER_LINK = True
DEFAULT_ROUTE_CONTACT_SEEK_RANGE_MM = 0.010
DEFAULT_ROUTE_CONTACT_SEEK_STEP_MM = 0.001

VOLTAGE_PREFIXES = (
    ("uV", 1e-6),
    ("mV", 1e-3),
    ("V", 1.0),
)
CURRENT_PREFIXES = (
    ("uA", 1e-6),
    ("mA", 1e-3),
    ("A", 1.0),
)
RESISTANCE_PREFIXES = (
    ("mohm", 1e-3),
    ("ohm", 1.0),
    ("kohm", 1e3),
    ("Mohm", 1e6),
    ("Gohm", 1e9),
)
FREQUENCY_PREFIXES = (
    ("Hz", 1.0),
    ("kHz", 1e3),
)


@dataclass(frozen=True)
class RouteMeasurementRunConfiguration:
    """Complete per-run route measurement configuration from the dialog."""

    csv_path: str
    previous_csv_path: str
    operation_mode: str
    photo_output_dir: str
    photo_settle_s: float
    photo_autofocus_enabled: bool
    photo_autofocus_range_mm: float
    initial_measurement_count: int
    followup_measurement_count: int
    current_point: int
    max_relative_rms: float
    contact_settle_s: float
    contact_seek_range_mm: float
    contact_seek_step_mm: float
    previous_ok_only: bool
    meter: RouteMeterConfiguration

    @property
    def measurement_count(self) -> int:
        """Maximum readings per point after both measurement phases."""

        return max(1, int(self.initial_measurement_count)) + max(
            0,
            int(self.followup_measurement_count),
        )

    @property
    def start_point(self) -> int:
        """Backward-compatible alias for older callers."""

        return self.current_point


class _SIPrefixSpinBox(QWidget):
    """Numeric editor that stores values in base SI units."""

    def __init__(
        self,
        *,
        prefixes: tuple[tuple[str, float], ...],
        base_minimum: float,
        base_maximum: float,
        base_value: float,
        decimals: int = 3,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._prefixes = tuple(prefixes)
        self._base_minimum = float(base_minimum)
        self._base_maximum = float(base_maximum)
        self._base_value = float(base_value)
        self._updating = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._spin = QDoubleSpinBox(self)
        self._spin.setLocale(QLocale.c())
        self._spin.setDecimals(decimals)
        self._spin.setKeyboardTracking(False)
        self._spin.setMinimumWidth(110)
        self._prefix_combo = QComboBox(self)
        for label, factor in self._prefixes:
            self._prefix_combo.addItem(label, float(factor))
        layout.addWidget(self._spin, 1)
        layout.addWidget(self._prefix_combo)

        self._prefix_combo.currentIndexChanged.connect(
            lambda _index: self._on_prefix_changed()
        )
        self._spin.valueChanged.connect(lambda _value: self._on_display_value_changed())
        self._spin.editingFinished.connect(self._normalize_prefix)
        self.set_base_value(base_value)

    def base_value(self) -> float:
        return float(self._base_value)

    def set_base_value(self, value: object) -> None:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(numeric):
            return
        self._base_value = self._clamp_base_value(numeric)
        self._set_prefix_for_value(self._base_value)
        self._refresh_display()

    def _on_display_value_changed(self) -> None:
        if self._updating:
            return
        factor = self._current_factor()
        self._base_value = self._clamp_base_value(self._spin.value() * factor)

    def _on_prefix_changed(self) -> None:
        if self._updating:
            return
        self._refresh_display()

    def _normalize_prefix(self) -> None:
        self._on_display_value_changed()
        self._set_prefix_for_value(self._base_value)
        self._refresh_display()

    def _refresh_display(self) -> None:
        factor = self._current_factor()
        self._updating = True
        try:
            spin_blocker = QSignalBlocker(self._spin)
            self._spin.setRange(
                self._base_minimum / factor,
                self._base_maximum / factor,
            )
            self._spin.setValue(self._base_value / factor)
            del spin_blocker
        finally:
            self._updating = False

    def _set_prefix_for_value(self, value: float) -> None:
        index = self._best_prefix_index(value)
        if index == self._prefix_combo.currentIndex():
            return
        blocker = QSignalBlocker(self._prefix_combo)
        self._prefix_combo.setCurrentIndex(index)
        del blocker

    def _best_prefix_index(self, value: float) -> int:
        absolute = abs(float(value))
        if absolute <= 0.0:
            return self._unit_prefix_index()
        best = 0
        for index, (_label, factor) in enumerate(self._prefixes):
            scaled = absolute / factor
            if 1.0 <= scaled < 1000.0:
                return index
            if scaled >= 1.0:
                best = index
        return best

    def _unit_prefix_index(self) -> int:
        for index, (_label, factor) in enumerate(self._prefixes):
            if factor == 1.0:
                return index
        return 0

    def _current_factor(self) -> float:
        factor = self._prefix_combo.currentData()
        try:
            numeric = float(factor)
        except (TypeError, ValueError):
            numeric = 1.0
        return numeric if numeric > 0.0 else 1.0

    def _clamp_base_value(self, value: float) -> float:
        return max(self._base_minimum, min(self._base_maximum, float(value)))


class RouteMeasurementDialog(QDialog):
    """Non-modal route measurement setup and control window."""

    measure_requested = Signal(object)
    start_session_requested = Signal()
    cancel_session_requested = Signal()
    next_requested = Signal()
    remeasure_requested = Signal()
    skip_requested = Signal()
    save_shift_requested = Signal()
    interrupt_requested = Signal()
    pause_requested = Signal()
    jump_requested = Signal(int)
    current_point_changed = Signal(int)

    def __init__(
        self,
        *,
        route_name: str,
        route_point_count: int,
        default_csv_path: str,
        default_photo_dir: str | None = None,
        default_meter_type: str = ROUTE_METER_KEITHLEY,
        settings_path: str | Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Route Measurement")
        self.setModal(False)
        self._running = False
        self._waiting = False
        self._route_point_count = max(1, int(route_point_count))
        self._default_csv_path = default_csv_path
        self._default_photo_dir = (
            default_photo_dir
            if default_photo_dir is not None
            else str(Path(default_csv_path).with_suffix("")) + "-photos"
        )
        self._settings_path = Path(settings_path).expanduser() if settings_path else None
        self._last_raw_samples: tuple[object, ...] = ()
        self._measurement_pending = False
        self._measurement_session_active = False

        outer_layout = QVBoxLayout(self)
        scroll_area = QScrollArea(self)
        scroll_area.setWidgetResizable(True)
        scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll_content = QWidget(scroll_area)
        layout = QVBoxLayout(scroll_content)
        scroll_area.setWidget(scroll_content)
        outer_layout.addWidget(scroll_area, 1)

        common_group = QGroupBox("Measurement", self)
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

        previous_csv_row = QHBoxLayout()
        self._previous_csv_path_edit = QLineEdit(common_group)
        self._previous_csv_path_edit.setText(default_csv_path)
        self._previous_csv_path_edit.setPlaceholderText("previous measurement CSV")
        self._previous_csv_browse_button = QPushButton("Browse", common_group)
        previous_csv_row.addWidget(self._previous_csv_path_edit, 1)
        previous_csv_row.addWidget(self._previous_csv_browse_button)
        common_layout.addRow(QLabel("Previous CSV", common_group), previous_csv_row)

        self._previous_ok_only_checkbox = QCheckBox(
            "Only previous OK",
            common_group,
        )
        self._previous_ok_only_checkbox.setToolTip(
            "Use the latest row for each structure in the previous CSV."
        )
        common_layout.addRow(
            QLabel("Point filter", common_group),
            self._previous_ok_only_checkbox,
        )

        self._operation_combo = QComboBox(common_group)
        self._operation_combo.addItem("Measure only", ROUTE_OPERATION_MEASURE)
        self._operation_combo.addItem("Photo only", ROUTE_OPERATION_PHOTO)
        self._operation_combo.addItem(
            "Photo then measure",
            ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        )
        common_layout.addRow(QLabel("Route mode", common_group), self._operation_combo)

        photo_row = QHBoxLayout()
        self._photo_dir_edit = QLineEdit(common_group)
        self._photo_dir_edit.setText(self._default_photo_dir)
        self._photo_dir_edit.setPlaceholderText("route photo output directory")
        self._photo_browse_button = QPushButton("Browse", common_group)
        photo_row.addWidget(self._photo_dir_edit, 1)
        photo_row.addWidget(self._photo_browse_button)
        common_layout.addRow(QLabel("Photos", common_group), photo_row)

        self._photo_settle_spin = QDoubleSpinBox(common_group)
        self._photo_settle_spin.setLocale(QLocale.c())
        self._photo_settle_spin.setDecimals(3)
        self._photo_settle_spin.setRange(0.0, 10.0)
        self._photo_settle_spin.setSingleStep(0.05)
        self._photo_settle_spin.setSuffix(" s")
        self._photo_settle_spin.setValue(0.2)
        common_layout.addRow(
            QLabel("Photo settle", common_group),
            self._photo_settle_spin,
        )

        self._photo_autofocus_checkbox = QCheckBox(
            "Autofocus before each point",
            common_group,
        )
        common_layout.addRow(QLabel("Autofocus", common_group), self._photo_autofocus_checkbox)

        self._photo_autofocus_range_spin = QDoubleSpinBox(common_group)
        self._photo_autofocus_range_spin.setLocale(QLocale.c())
        self._photo_autofocus_range_spin.setDecimals(4)
        self._photo_autofocus_range_spin.setRange(0.001, 0.200)
        self._photo_autofocus_range_spin.setSingleStep(0.005)
        self._photo_autofocus_range_spin.setSuffix(" mm")
        self._photo_autofocus_range_spin.setValue(
            DEFAULT_ROUTE_PHOTO_AUTOFOCUS_RANGE_MM
        )
        common_layout.addRow(
            QLabel("AF range", common_group),
            self._photo_autofocus_range_spin,
        )

        self._initial_measurement_count_spin = QSpinBox(common_group)
        self._initial_measurement_count_spin.setRange(1, 1000)
        self._initial_measurement_count_spin.setValue(
            DEFAULT_ROUTE_INITIAL_MEASUREMENT_COUNT
        )
        common_layout.addRow(
            QLabel("Initial samples", common_group),
            self._initial_measurement_count_spin,
        )

        self._followup_measurement_count_spin = QSpinBox(common_group)
        self._followup_measurement_count_spin.setRange(0, 1000)
        self._followup_measurement_count_spin.setValue(
            DEFAULT_ROUTE_FOLLOWUP_MEASUREMENT_COUNT
        )
        common_layout.addRow(
            QLabel("Follow-up samples", common_group),
            self._followup_measurement_count_spin,
        )

        self._max_relative_rms_spin = QDoubleSpinBox(common_group)
        self._max_relative_rms_spin.setLocale(QLocale.c())
        self._max_relative_rms_spin.setDecimals(3)
        self._max_relative_rms_spin.setRange(0.001, 100.0)
        self._max_relative_rms_spin.setSingleStep(0.1)
        self._max_relative_rms_spin.setSuffix(" %")
        self._max_relative_rms_spin.setValue(1.0)
        common_layout.addRow(
            QLabel("Max rel RMS", common_group),
            self._max_relative_rms_spin,
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

        self._contact_seek_range_spin = QDoubleSpinBox(common_group)
        self._contact_seek_range_spin.setLocale(QLocale.c())
        self._contact_seek_range_spin.setDecimals(4)
        self._contact_seek_range_spin.setRange(0.0, 1.0)
        self._contact_seek_range_spin.setSingleStep(0.001)
        self._contact_seek_range_spin.setSuffix(" mm")
        self._contact_seek_range_spin.setValue(DEFAULT_ROUTE_CONTACT_SEEK_RANGE_MM)
        common_layout.addRow(
            QLabel("Contact seek range", common_group),
            self._contact_seek_range_spin,
        )

        self._contact_seek_step_spin = QDoubleSpinBox(common_group)
        self._contact_seek_step_spin.setLocale(QLocale.c())
        self._contact_seek_step_spin.setDecimals(4)
        self._contact_seek_step_spin.setRange(0.0001, 1.0)
        self._contact_seek_step_spin.setSingleStep(0.0005)
        self._contact_seek_step_spin.setSuffix(" mm")
        self._contact_seek_step_spin.setValue(DEFAULT_ROUTE_CONTACT_SEEK_STEP_MM)
        common_layout.addRow(
            QLabel("Contact seek step", common_group),
            self._contact_seek_step_spin,
        )

        profile_row = QHBoxLayout()
        self._load_profile_button = QPushButton("Load Profile", common_group)
        self._save_profile_button = QPushButton("Save Profile", common_group)
        profile_row.addWidget(self._load_profile_button)
        profile_row.addWidget(self._save_profile_button)
        profile_row.addStretch(1)
        common_layout.addRow(QLabel("Profile", common_group), profile_row)
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
        self._result_label = QLabel("Last result: none.", self)
        self._result_label.setWordWrap(True)
        layout.addWidget(self._result_label)

        analysis_row = QHBoxLayout()
        self._histogram_mode_combo = QComboBox(self)
        self._histogram_mode_combo.addItem("Differential dV/dI", "differential")
        self._histogram_mode_combo.addItem("Polarity V/I", "polarity")
        self._raw_data_button = QPushButton("Raw Data", self)
        self._raw_data_button.setEnabled(False)
        analysis_row.addWidget(QLabel("Histogram", self))
        analysis_row.addWidget(self._histogram_mode_combo)
        analysis_row.addWidget(self._raw_data_button)
        analysis_row.addStretch(1)
        layout.addLayout(analysis_row)
        self._histogram_widget = _RouteMeasurementHistogram(self)
        self._histogram_widget.setMinimumHeight(170)
        layout.addWidget(self._histogram_widget)

        jump_row = QHBoxLayout()
        self._current_point_spin = QSpinBox(self)
        self._current_point_spin.setRange(1, self._route_point_count)
        self._current_point_spin.setValue(1)
        self._jump_point_spin = self._current_point_spin
        self._jump_button = QPushButton("Go To", self)
        jump_row.addWidget(QLabel("Current point", self))
        jump_row.addWidget(self._current_point_spin)
        jump_row.addWidget(self._jump_button)
        jump_row.addStretch(1)
        layout.addLayout(jump_row)

        button_row = QHBoxLayout()
        self._start_session_button = QPushButton("Start Session", self)
        self._cancel_session_button = QPushButton("Cancel Session", self)
        self._measure_button = QPushButton("Measure", self)
        self._pause_button = QPushButton("Pause", self)
        self._interrupt_button = QPushButton("Interrupt", self)
        self._save_shift_button = QPushButton("Save Shift", self)
        self._remeasure_button = QPushButton("Remeasure", self)
        self._skip_button = QPushButton("Skip", self)
        self._next_button = QPushButton("Next", self)
        self._close_button = QPushButton("Close", self)
        button_row.addWidget(self._start_session_button)
        button_row.addWidget(self._cancel_session_button)
        button_row.addWidget(self._measure_button)
        button_row.addWidget(self._pause_button)
        button_row.addWidget(self._interrupt_button)
        button_row.addWidget(self._save_shift_button)
        button_row.addWidget(self._remeasure_button)
        button_row.addWidget(self._skip_button)
        button_row.addWidget(self._next_button)
        button_row.addStretch(1)
        button_row.addWidget(self._close_button)
        outer_layout.addLayout(button_row)

        self._csv_browse_button.clicked.connect(self._choose_csv_path)
        self._previous_csv_browse_button.clicked.connect(
            self._choose_previous_csv_path
        )
        self._photo_browse_button.clicked.connect(self._choose_photo_dir)
        self._operation_combo.currentIndexChanged.connect(
            lambda _index: self._update_operation_state()
        )
        self._photo_autofocus_checkbox.toggled.connect(
            lambda _checked: self._update_operation_state()
        )
        self._previous_ok_only_checkbox.toggled.connect(
            lambda _checked: self._update_operation_state()
        )
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
        self._current_point_spin.valueChanged.connect(self._on_current_point_changed)
        self._start_session_button.clicked.connect(self.start_session_requested.emit)
        self._cancel_session_button.clicked.connect(self.cancel_session_requested.emit)
        self._measure_button.clicked.connect(self._emit_measure_requested)
        self._load_profile_button.clicked.connect(self._load_profile)
        self._save_profile_button.clicked.connect(self._save_profile)
        self._histogram_mode_combo.currentIndexChanged.connect(
            lambda _index: self._update_histogram_mode()
        )
        self._raw_data_button.clicked.connect(self._show_raw_data)
        self._pause_button.clicked.connect(self._emit_pause_requested)
        self._interrupt_button.clicked.connect(self.interrupt_requested.emit)
        self._save_shift_button.clicked.connect(self.save_shift_requested.emit)
        self._remeasure_button.clicked.connect(self.remeasure_requested.emit)
        self._skip_button.clicked.connect(self.skip_requested.emit)
        self._next_button.clicked.connect(self.next_requested.emit)
        self._jump_button.clicked.connect(
            lambda _checked=False: self.jump_requested.emit(
                int(self._jump_point_spin.value())
            )
        )
        self._close_button.clicked.connect(self.close)
        self._load_settings_file()
        self._update_meter_page()
        self._update_operation_state()
        self.set_running(False)
        self._resize_to_available_screen()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._save_settings_file()
        super().closeEvent(event)

    def _resize_to_available_screen(self) -> None:
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            self.resize(820, 720)
            return
        available = screen.availableGeometry()
        target_width = max(640, min(820, available.width() - 80))
        target_height = max(420, min(760, available.height() - 120))
        self.resize(target_width, target_height)

    def set_status(self, message: str) -> None:
        self._status_label.setText(message or "Idle.")

    def set_result(
        self,
        record: object,
        position: int,
        total: int,
        saved: bool,
    ) -> None:
        resistance = getattr(record, "resistance_ohm", math.nan)
        rms = getattr(record, "resistance_rms_ohm", math.nan)
        relative_rms = getattr(record, "relative_rms", math.nan)
        status = str(getattr(record, "status", ""))
        prefix = "Saved" if saved else "Not saved"
        contact = getattr(record, "contact_quality", None)
        contact_text = ""
        if contact is not None and bool(getattr(contact, "assessed", False)):
            contact_text = (
                f", contact={getattr(contact, 'status', 'unknown')} "
                f"(median={_format_ohm(float(getattr(contact, 'median_ohm', math.nan)))}, "
                f"MAD={_format_ohm(float(getattr(contact, 'mad_sigma_ohm', math.nan)))})"
            )
        self._result_label.setText(
            f"{prefix} point {position}/{total}: "
            f"R={_format_ohm(float(resistance))}, "
            f"RMS={_format_ohm(float(rms))}, "
            f"rel={_format_percent(float(relative_rms))}, "
            f"status={status or 'unknown'}{contact_text}."
        )
        self._last_raw_samples = tuple(getattr(record, "raw_samples", ()) or ())
        self._histogram_widget.set_samples(self._last_raw_samples)
        self._raw_data_button.setEnabled(bool(self._last_raw_samples))

    def set_current_point(self, point_number: int, *, save: bool = True) -> None:
        value = min(max(1, int(point_number)), self._route_point_count)
        with QSignalBlocker(self._current_point_spin):
            self._current_point_spin.setValue(value)
        if not self._waiting:
            with QSignalBlocker(self._jump_point_spin):
                self._jump_point_spin.setValue(value)
        if save:
            self._save_settings_file()

    def set_resume_point(self, point_number: int, *, save: bool = True) -> None:
        self.set_current_point(point_number, save=save)

    def set_measurement_pending(self, pending: bool, *, save: bool = True) -> None:
        self.set_measurement_session_active(pending, save=save)

    def set_measurement_session_active(
        self,
        active: bool,
        *,
        save: bool = True,
    ) -> None:
        self._measurement_session_active = bool(active)
        self._measurement_pending = bool(active)
        self._update_session_buttons()
        if save:
            self._save_settings_file()

    def measurement_pending(self) -> bool:
        return bool(self._measurement_session_active)

    def measurement_session_active(self) -> bool:
        return bool(self._measurement_session_active)

    def set_route(
        self,
        *,
        route_name: str,
        route_point_count: int,
        default_csv_path: str,
        default_photo_dir: str | None = None,
    ) -> None:
        self._route_point_count = max(1, int(route_point_count))
        self._route_combo.setItemText(
            0,
            f"{route_name} ({route_point_count} points)",
        )
        for spinbox in (self._current_point_spin, self._jump_point_spin):
            value = min(max(1, int(spinbox.value())), self._route_point_count)
            spinbox.setRange(1, self._route_point_count)
            spinbox.setValue(value)
        if not self._running:
            current_csv = self._csv_path_edit.text().strip()
            if not current_csv or current_csv == self._default_csv_path:
                self._csv_path_edit.setText(default_csv_path)
            current_previous_csv = self._previous_csv_path_edit.text().strip()
            if (
                not current_previous_csv
                or current_previous_csv == self._default_csv_path
            ):
                self._previous_csv_path_edit.setText(default_csv_path)
            self._default_csv_path = default_csv_path
            if default_photo_dir is not None:
                current_photo_dir = self._photo_dir_edit.text().strip()
                if not current_photo_dir or current_photo_dir == self._default_photo_dir:
                    self._photo_dir_edit.setText(default_photo_dir)
                self._default_photo_dir = default_photo_dir

    def set_running(self, running: bool) -> None:
        self._running = bool(running)
        for widget in (
            self._route_combo,
            self._csv_path_edit,
            self._csv_browse_button,
            self._previous_csv_path_edit,
            self._previous_csv_browse_button,
            self._previous_ok_only_checkbox,
            self._operation_combo,
            self._photo_dir_edit,
            self._photo_browse_button,
            self._photo_autofocus_checkbox,
            self._photo_autofocus_range_spin,
            self._current_point_spin,
            self._meter_combo,
            self._gwinstek_page,
            self._keithley_page,
            self._load_profile_button,
            self._save_profile_button,
        ):
            widget.setEnabled(not self._running)
        self._set_runtime_settings_enabled(not self._running)
        self._measure_button.setEnabled(not self._running)
        self._pause_button.setEnabled(self._running and not self._waiting)
        self._close_button.setEnabled(True)
        self.set_waiting(False)
        self._update_session_buttons()

    def set_waiting(self, waiting: bool) -> None:
        self._waiting = bool(waiting)
        can_confirm = self._running and self._waiting
        self._pause_button.setEnabled(self._running and not self._waiting)
        self._interrupt_button.setEnabled(self._running and not self._waiting)
        self._save_shift_button.setEnabled(self._running and self._waiting)
        self._remeasure_button.setEnabled(can_confirm)
        self._skip_button.setEnabled(can_confirm)
        self._next_button.setEnabled(can_confirm)
        self._jump_point_spin.setEnabled((not self._running) or can_confirm)
        self._jump_button.setEnabled(can_confirm)
        self._set_runtime_settings_enabled((not self._running) or can_confirm)
        self._update_session_buttons()

    def _update_session_buttons(self) -> None:
        can_change_session = not self._running
        self._start_session_button.setEnabled(
            can_change_session and not self._measurement_session_active
        )
        self._cancel_session_button.setEnabled(
            can_change_session and self._measurement_session_active
        )
        self._measure_button.setEnabled(
            (not self._running) and self._measurement_session_active
        )

    def _set_runtime_settings_enabled(self, enabled: bool) -> None:
        for widget in (
            self._initial_measurement_count_spin,
            self._followup_measurement_count_spin,
            self._max_relative_rms_spin,
            self._contact_settle_spin,
            self._contact_seek_range_spin,
            self._contact_seek_step_spin,
            self._photo_settle_spin,
        ):
            widget.setEnabled(bool(enabled))
        self._update_operation_state()

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

        self._gw_frequency_spin = _SIPrefixSpinBox(
            prefixes=FREQUENCY_PREFIXES,
            base_minimum=10.0,
            base_maximum=300_000.0,
            base_value=50.0,
            parent=page,
        )
        layout.addRow(QLabel("AC frequency", page), self._gw_frequency_spin)

        self._gw_level_mode_combo = QComboBox(page)
        for mode in LCR_LEVEL_MODES:
            self._gw_level_mode_combo.addItem(mode.title(), mode)
        layout.addRow(QLabel("AC level mode", page), self._gw_level_mode_combo)

        self._gw_voltage_spin = _SIPrefixSpinBox(
            prefixes=VOLTAGE_PREFIXES,
            base_minimum=0.01,
            base_maximum=2.0,
            base_value=0.03,
            parent=page,
        )
        layout.addRow(QLabel("AC voltage", page), self._gw_voltage_spin)

        self._gw_current_spin = _SIPrefixSpinBox(
            prefixes=CURRENT_PREFIXES,
            base_minimum=100e-6,
            base_maximum=20e-3,
            base_value=100e-6,
            parent=page,
        )
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

        self._gw_bias_spin = _SIPrefixSpinBox(
            prefixes=VOLTAGE_PREFIXES,
            base_minimum=-2.5,
            base_maximum=2.5,
            base_value=0.0,
            parent=page,
        )
        layout.addRow(QLabel("DC bias", page), self._gw_bias_spin)

        self._update_gwinstek_state()
        return page

    def _build_keithley_page(self) -> QWidget:
        page = QWidget(self)
        layout = QFormLayout(page)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._keithley_voltage_spin = _SIPrefixSpinBox(
            prefixes=VOLTAGE_PREFIXES,
            base_minimum=1e-6,
            base_maximum=10.0,
            base_value=DEFAULT_KEITHLEY_MEASUREMENT_VOLTAGE_V,
            parent=page,
        )
        layout.addRow(QLabel("+/- voltage", page), self._keithley_voltage_spin)

        self._keithley_range_spin = _SIPrefixSpinBox(
            prefixes=VOLTAGE_PREFIXES,
            base_minimum=0.2,
            base_maximum=210.0,
            base_value=DEFAULT_KEITHLEY_SOURCE_RANGE_V,
            parent=page,
        )
        layout.addRow(QLabel("Source range", page), self._keithley_range_spin)

        self._keithley_compliance_spin = _SIPrefixSpinBox(
            prefixes=CURRENT_PREFIXES,
            base_minimum=1e-6,
            base_maximum=1.0,
            base_value=DEFAULT_KEITHLEY_COMPLIANCE_CURRENT_A,
            parent=page,
        )
        layout.addRow(
            QLabel("Compliance current", page), self._keithley_compliance_spin
        )

        self._keithley_current_range_spin = _SIPrefixSpinBox(
            prefixes=CURRENT_PREFIXES,
            base_minimum=1e-6,
            base_maximum=1.0,
            base_value=DEFAULT_KEITHLEY_CURRENT_RANGE_A,
            parent=page,
        )
        layout.addRow(QLabel("Current range", page), self._keithley_current_range_spin)

        self._keithley_voltmeter_range_spin = _SIPrefixSpinBox(
            prefixes=VOLTAGE_PREFIXES,
            base_minimum=0.01,
            base_maximum=100.0,
            base_value=DEFAULT_KEITHLEY_VOLTMETER_RANGE_V,
            parent=page,
        )
        layout.addRow(
            QLabel("Voltmeter range", page),
            self._keithley_voltmeter_range_spin,
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
            self._save_settings_file()

    def _choose_previous_csv_path(self) -> None:
        current = self._previous_csv_path_edit.text().strip()
        start = current or self._csv_path_edit.text().strip()
        if not start:
            start = str(Path.cwd() / "probe_route_measurements.csv")
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Previous Route Measurement CSV",
            start,
            "CSV files (*.csv);;All files (*)",
        )
        if path:
            self._previous_csv_path_edit.setText(path)
            self._save_settings_file()

    def _choose_photo_dir(self) -> None:
        current = self._photo_dir_edit.text().strip()
        start = current or self._default_photo_dir or str(Path.cwd())
        path = QFileDialog.getExistingDirectory(
            self,
            "Route Photo Directory",
            start,
        )
        if path:
            self._photo_dir_edit.setText(path)
            self._save_settings_file()

    def _update_operation_state(self) -> None:
        mode = str(self._operation_combo.currentData() or ROUTE_OPERATION_MEASURE)
        photo_enabled = mode in {
            ROUTE_OPERATION_PHOTO,
            ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        }
        measure_enabled = mode in {
            ROUTE_OPERATION_MEASURE,
            ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        }
        route_active = photo_enabled or measure_enabled
        can_edit = not self._running or self._waiting
        self._photo_dir_edit.setEnabled(photo_enabled and not self._running)
        self._photo_browse_button.setEnabled(photo_enabled and not self._running)
        self._photo_settle_spin.setEnabled(photo_enabled and can_edit)
        self._photo_autofocus_checkbox.setEnabled(route_active and not self._running)
        self._photo_autofocus_range_spin.setEnabled(
            route_active
            and self._photo_autofocus_checkbox.isChecked()
            and not self._running
        )
        self._csv_path_edit.setEnabled(measure_enabled and not self._running)
        self._csv_browse_button.setEnabled(measure_enabled and not self._running)
        self._previous_csv_path_edit.setEnabled(
            measure_enabled
            and self._previous_ok_only_checkbox.isChecked()
            and not self._running
        )
        self._previous_csv_browse_button.setEnabled(
            measure_enabled
            and self._previous_ok_only_checkbox.isChecked()
            and not self._running
        )
        self._previous_ok_only_checkbox.setEnabled(
            measure_enabled and not self._running
        )
        self._meter_combo.setEnabled(measure_enabled and not self._running)
        self._gwinstek_page.setEnabled(measure_enabled and not self._running)
        self._keithley_page.setEnabled(measure_enabled and not self._running)
        for widget in (
            self._initial_measurement_count_spin,
            self._followup_measurement_count_spin,
            self._max_relative_rms_spin,
            self._contact_settle_spin,
            self._contact_seek_range_spin,
            self._contact_seek_step_spin,
        ):
            widget.setEnabled(measure_enabled and can_edit)

    def _load_profile(self) -> None:
        start = str(self._profile_start_directory() / "route-measurement-profile.json")
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Route Measurement Profile",
            start,
            "JSON files (*.json);;All files (*)",
        )
        if not path:
            return
        try:
            with Path(path).open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            self._apply_profile_data(data)
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            self.set_status(f"Unable to load profile: {exc}")
            return
        self._save_settings_file()
        self.set_status(f"Loaded profile {path}.")

    def _save_profile(self) -> None:
        start = str(self._profile_start_directory() / "route-measurement-profile.json")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save Route Measurement Profile",
            start,
            "JSON files (*.json);;All files (*)",
        )
        if not path:
            return
        try:
            self._write_profile(Path(path), self._profile_data())
        except OSError as exc:
            self.set_status(f"Unable to save profile: {exc}")
            return
        self._save_settings_file()
        self.set_status(f"Saved profile {path}.")

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

    def _update_histogram_mode(self) -> None:
        mode = str(self._histogram_mode_combo.currentData() or "differential")
        self._histogram_widget.set_mode(mode)

    def _show_raw_data(self) -> None:
        if not self._last_raw_samples:
            self.set_status("No raw measurement data yet.")
            return
        dialog = RouteMeasurementRawDataDialog(self._last_raw_samples, self)
        dialog.exec()

    def _emit_measure_requested(self) -> None:
        if not self._measurement_session_active:
            self.set_status("Start a measurement session before measuring.")
            return
        mode = str(self._operation_combo.currentData() or ROUTE_OPERATION_MEASURE)
        csv_path = self._csv_path_edit.text().strip()
        if mode in {ROUTE_OPERATION_MEASURE, ROUTE_OPERATION_PHOTO_THEN_MEASURE} and not csv_path:
            self.set_status("Choose a CSV path before measuring.")
            return
        if (
            mode in {ROUTE_OPERATION_MEASURE, ROUTE_OPERATION_PHOTO_THEN_MEASURE}
            and self._previous_ok_only_checkbox.isChecked()
            and not self._previous_csv_path_edit.text().strip()
        ):
            self.set_status("Choose a previous CSV before filtering.")
            return
        photo_dir = self._photo_dir_edit.text().strip()
        if mode in {ROUTE_OPERATION_PHOTO, ROUTE_OPERATION_PHOTO_THEN_MEASURE} and not photo_dir:
            self.set_status("Choose a photo directory before capturing.")
            return
        self.measure_requested.emit(self.current_configuration())

    def current_configuration(self) -> RouteMeasurementRunConfiguration:
        """Return the current dialog configuration without changing UI state."""

        self._save_settings_file()
        operation_mode = str(
            self._operation_combo.currentData() or ROUTE_OPERATION_MEASURE
        )
        return RouteMeasurementRunConfiguration(
            csv_path=self._csv_path_edit.text().strip(),
            previous_csv_path=self._previous_csv_path_edit.text().strip(),
            operation_mode=operation_mode,
            photo_output_dir=self._photo_dir_edit.text().strip(),
            photo_settle_s=float(self._photo_settle_spin.value()),
            photo_autofocus_enabled=bool(self._photo_autofocus_checkbox.isChecked()),
            photo_autofocus_range_mm=float(self._photo_autofocus_range_spin.value()),
            initial_measurement_count=int(
                self._initial_measurement_count_spin.value()
            ),
            followup_measurement_count=int(
                self._followup_measurement_count_spin.value()
            ),
            current_point=int(self._current_point_spin.value()),
            max_relative_rms=float(self._max_relative_rms_spin.value()) / 100.0,
            contact_settle_s=float(self._contact_settle_spin.value()),
            contact_seek_range_mm=float(self._contact_seek_range_spin.value()),
            contact_seek_step_mm=float(self._contact_seek_step_spin.value()),
            previous_ok_only=(
                bool(self._previous_ok_only_checkbox.isChecked())
                and operation_mode
                in {
                    ROUTE_OPERATION_MEASURE,
                    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
                }
            ),
            meter=self._meter_configuration(),
        )

    def _emit_pause_requested(self) -> None:
        self._pause_button.setEnabled(False)
        self.pause_requested.emit()

    def _on_current_point_changed(self, value: int) -> None:
        point_number = min(max(1, int(value)), self._route_point_count)
        if not self._waiting:
            with QSignalBlocker(self._jump_point_spin):
                self._jump_point_spin.setValue(point_number)
        self._save_settings_file()
        self.current_point_changed.emit(point_number)

    def _profile_start_directory(self) -> Path:
        csv_path = Path(self._csv_path_edit.text().strip() or ".").expanduser()
        if csv_path.parent != Path("."):
            return csv_path.parent
        if self._settings_path is not None:
            return self._settings_path.parent
        return Path.cwd()

    def _load_settings_file(self) -> None:
        if self._settings_path is None or not self._settings_path.exists():
            return
        try:
            with self._settings_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            migrated = self._apply_profile_data(data)
            if migrated:
                self._save_settings_file()
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return

    def _save_settings_file(self) -> None:
        if self._settings_path is None:
            return
        try:
            self._write_profile(self._settings_path, self._profile_data())
        except OSError:
            return

    def _write_profile(self, path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)

    def _profile_data(self) -> dict[str, Any]:
        meter = self._meter_configuration()
        return {
            "version": ROUTE_MEASUREMENT_PROFILE_VERSION,
            "csv_path": self._csv_path_edit.text().strip(),
            "previous_csv_path": self._previous_csv_path_edit.text().strip(),
            "operation_mode": str(
                self._operation_combo.currentData() or ROUTE_OPERATION_MEASURE
            ),
            "photo_output_dir": self._photo_dir_edit.text().strip(),
            "photo_settle_s": float(self._photo_settle_spin.value()),
            "photo_autofocus_enabled": bool(
                self._photo_autofocus_checkbox.isChecked()
            ),
            "photo_autofocus_range_mm": float(
                self._photo_autofocus_range_spin.value()
            ),
            "measurement_count": self._total_measurement_count(),
            "initial_measurement_count": int(
                self._initial_measurement_count_spin.value()
            ),
            "followup_measurement_count": int(
                self._followup_measurement_count_spin.value()
            ),
            "current_point": int(self._current_point_spin.value()),
            "measurement_session_active": bool(self._measurement_session_active),
            "measurement_pending": bool(self._measurement_pending),
            "max_relative_rms": float(self._max_relative_rms_spin.value()) / 100.0,
            "contact_settle_s": float(self._contact_settle_spin.value()),
            "contact_seek_range_mm": float(self._contact_seek_range_spin.value()),
            "contact_seek_step_mm": float(self._contact_seek_step_spin.value()),
            "previous_ok_only": bool(self._previous_ok_only_checkbox.isChecked()),
            "meter": {
                "meter_type": meter.meter_type,
                "gwinstek": asdict(meter.gwinstek),
                "keithley": asdict(meter.keithley),
            },
        }

    def _apply_profile_data(self, data: object) -> bool:
        if not isinstance(data, dict):
            raise ValueError("Profile JSON root must be an object.")
        migrate_keithley_defaults = self._should_migrate_keithley_defaults(data)
        csv_path = data.get("csv_path")
        if isinstance(csv_path, str) and csv_path.strip():
            self._csv_path_edit.setText(csv_path.strip())
        previous_csv_path = data.get("previous_csv_path")
        if isinstance(previous_csv_path, str) and previous_csv_path.strip():
            self._previous_csv_path_edit.setText(previous_csv_path.strip())
        elif isinstance(csv_path, str) and csv_path.strip():
            self._previous_csv_path_edit.setText(csv_path.strip())
        operation_mode = data.get("operation_mode")
        if isinstance(operation_mode, str):
            self._set_combo_data(self._operation_combo, operation_mode)
        photo_output_dir = data.get("photo_output_dir")
        if isinstance(photo_output_dir, str) and photo_output_dir.strip():
            self._photo_dir_edit.setText(photo_output_dir.strip())
        self._set_spinbox_value(self._photo_settle_spin, data.get("photo_settle_s"))
        self._photo_autofocus_checkbox.setChecked(
            bool(data.get("photo_autofocus_enabled", False))
        )
        self._set_spinbox_value(
            self._photo_autofocus_range_spin,
            data.get("photo_autofocus_range_mm"),
        )
        self._apply_measurement_count_profile(data)
        session_active = data.get(
            "measurement_session_active",
            data.get("measurement_pending", False),
        )
        self._measurement_session_active = bool(session_active)
        self._measurement_pending = bool(session_active)
        current_point = data.get("current_point", data.get("start_point"))
        self._set_spinbox_value(self._current_point_spin, current_point)
        max_relative_rms = data.get("max_relative_rms")
        try:
            max_relative_rms_percent = float(max_relative_rms) * 100.0
        except (TypeError, ValueError):
            max_relative_rms_percent = math.nan
        if math.isfinite(max_relative_rms_percent):
            self._max_relative_rms_spin.setValue(max_relative_rms_percent)
        self._previous_ok_only_checkbox.setChecked(
            bool(data.get("previous_ok_only", False))
        )
        self._set_spinbox_value(self._contact_settle_spin, data.get("contact_settle_s"))
        self._set_spinbox_value(
            self._contact_seek_range_spin,
            data.get("contact_seek_range_mm"),
        )
        self._set_spinbox_value(
            self._contact_seek_step_spin,
            data.get("contact_seek_step_mm"),
        )
        meter = data.get("meter")
        if isinstance(meter, dict):
            meter_type = meter.get("meter_type")
            if isinstance(meter_type, str):
                self._set_combo_data(self._meter_combo, meter_type)
            self._apply_gwinstek_profile(meter.get("gwinstek"))
            self._apply_keithley_profile(meter.get("keithley"))
        if migrate_keithley_defaults:
            self._apply_default_keithley_route_settings()
        self._update_meter_page()
        self._update_gwinstek_state()
        self._update_operation_state()
        return migrate_keithley_defaults

    def _should_migrate_keithley_defaults(self, data: dict[str, Any]) -> bool:
        try:
            version = int(data.get("version", 0))
        except (TypeError, ValueError):
            version = 0
        if version >= ROUTE_MEASUREMENT_PROFILE_VERSION:
            return False
        meter = data.get("meter")
        if not isinstance(meter, dict):
            return False
        return meter.get("meter_type") == ROUTE_METER_KEITHLEY

    def _apply_default_keithley_route_settings(self) -> None:
        self._initial_measurement_count_spin.setValue(
            DEFAULT_ROUTE_INITIAL_MEASUREMENT_COUNT
        )
        self._followup_measurement_count_spin.setValue(
            DEFAULT_ROUTE_FOLLOWUP_MEASUREMENT_COUNT
        )
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

    def _apply_measurement_count_profile(self, data: dict[str, Any]) -> None:
        initial = self._positive_int_or_none(data.get("initial_measurement_count"))
        followup = self._nonnegative_int_or_none(
            data.get("followup_measurement_count")
        )
        total = self._positive_int_or_none(data.get("measurement_count"))
        if initial is None and followup is None and total is not None:
            initial = min(total, DEFAULT_ROUTE_INITIAL_MEASUREMENT_COUNT)
            followup = max(0, total - initial)
        if initial is not None:
            self._initial_measurement_count_spin.setValue(initial)
        if followup is not None:
            self._followup_measurement_count_spin.setValue(followup)

    def _total_measurement_count(self) -> int:
        return int(self._initial_measurement_count_spin.value()) + int(
            self._followup_measurement_count_spin.value()
        )

    def _apply_gwinstek_profile(self, data: object) -> None:
        if not isinstance(data, dict):
            return
        self._set_combo_text(self._gw_function_combo, data.get("measurement_function"))
        self._set_combo_data(self._gw_range_mode_combo, data.get("range_mode"))
        self._set_spinbox_value(self._gw_impedance_range_spin, data.get("impedance_range"))
        self._set_spinbox_value(self._gw_dcr_range_spin, data.get("dcr_range"))
        self._set_spinbox_value(self._gw_frequency_spin, data.get("frequency_hz"))
        self._set_combo_data(self._gw_level_mode_combo, data.get("level_mode"))
        self._set_spinbox_value(self._gw_voltage_spin, data.get("voltage_level_v"))
        self._set_spinbox_value(self._gw_current_spin, data.get("current_level_a"))
        self._set_combo_data(
            self._gw_source_resistance_combo,
            data.get("source_resistance_ohm"),
        )
        self._set_combo_text(self._gw_aperture_combo, data.get("aperture_rate"))
        self._set_spinbox_value(self._gw_averages_spin, data.get("aperture_averages"))
        self._set_spinbox_value(
            self._gw_trigger_delay_spin,
            data.get("trigger_delay_s"),
        )
        if isinstance(data.get("bias_enabled"), bool):
            self._gw_bias_checkbox.setChecked(bool(data["bias_enabled"]))
        self._set_spinbox_value(self._gw_bias_spin, data.get("bias_level_v"))

    def _apply_keithley_profile(self, data: object) -> None:
        if not isinstance(data, dict):
            return
        self._set_spinbox_value(
            self._keithley_voltage_spin,
            data.get("measurement_voltage_v"),
        )
        self._set_spinbox_value(
            self._keithley_range_spin,
            data.get("source_voltage_range_v"),
        )
        self._set_spinbox_value(
            self._keithley_voltmeter_range_spin,
            data.get("voltmeter_range_v"),
        )
        self._set_spinbox_value(
            self._keithley_current_range_spin,
            data.get("current_range_a"),
        )
        self._set_spinbox_value(
            self._keithley_compliance_spin,
            data.get("compliance_current_a"),
        )
        self._set_spinbox_value(self._keithley_nplc_spin, data.get("nplc"))
        self._set_combo_data(self._keithley_terminals_combo, data.get("terminals"))
        self._set_spinbox_value(
            self._keithley_delay_spin,
            data.get("trigger_delay_s"),
        )
        use_buffer = data.get("use_buffer")
        if isinstance(use_buffer, bool):
            self._keithley_buffer_checkbox.setChecked(use_buffer)
        use_trigger_link = data.get("use_trigger_link")
        if isinstance(use_trigger_link, bool):
            self._keithley_trigger_link_checkbox.setChecked(use_trigger_link)

    @staticmethod
    def _set_spinbox_value(spinbox, value: object) -> None:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        if math.isfinite(numeric):
            if isinstance(spinbox, _SIPrefixSpinBox):
                spinbox.set_base_value(numeric)
            elif isinstance(spinbox, QSpinBox):
                spinbox.setValue(int(round(numeric)))
            else:
                spinbox.setValue(numeric)

    @staticmethod
    def _positive_int_or_none(value: object) -> int | None:
        try:
            numeric = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        return numeric if numeric > 0 else None

    @staticmethod
    def _nonnegative_int_or_none(value: object) -> int | None:
        try:
            numeric = int(round(float(value)))
        except (TypeError, ValueError):
            return None
        return numeric if numeric >= 0 else None

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

    def _meter_configuration(self) -> RouteMeterConfiguration:
        meter_type = str(self._meter_combo.currentData() or ROUTE_METER_KEITHLEY)
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
                use_buffer=self._keithley_buffer_checkbox.isChecked(),
                use_trigger_link=self._keithley_trigger_link_checkbox.isChecked(),
            ),
        )


class _RouteMeasurementHistogram(QWidget):
    """Small histogram preview for the raw samples of the latest point."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._samples: tuple[object, ...] = ()
        self._mode = "differential"

    def set_samples(self, samples: tuple[object, ...]) -> None:
        self._samples = tuple(samples)
        self.update()

    def set_mode(self, mode: str) -> None:
        self._mode = "polarity" if mode == "polarity" else "differential"
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        _ = event
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.palette().base())
        series = self._series()
        values = [value for _label, _color, data in series for value in data]
        if not values:
            painter.setPen(self.palette().mid().color())
            painter.drawText(self.rect(), Qt.AlignCenter, "No raw data")
            return

        minimum = min(values)
        maximum = max(values)
        if minimum == maximum:
            span = abs(minimum) * 0.1 or 1.0
            minimum -= span
            maximum += span
        bin_count = max(1, min(20, int(math.sqrt(max(1, len(values)))) + 1))
        counts = [
            _histogram_counts(data, bin_count, minimum, maximum)
            for _label, _color, data in series
        ]
        max_count = max((max(item) if item else 0 for item in counts), default=1)
        max_count = max(1, max_count)

        metrics = painter.fontMetrics()
        left_margin = max(64, metrics.horizontalAdvance(str(max_count)) + 34)
        top_margin = 26 if len(series) > 1 else 18
        bottom_margin = 42
        plot = self.rect().adjusted(left_margin, top_margin, -12, -bottom_margin)
        if plot.width() <= 0 or plot.height() <= 0:
            return
        scale, unit = _resistance_axis_unit(values)
        x_ticks = (minimum, minimum + (maximum - minimum) * 0.5, maximum)
        x_decimals = _axis_tick_decimals((maximum - minimum) / scale)
        bar_width = plot.width() / bin_count
        axis_color = self.palette().mid().color()
        grid_color = QColor(axis_color)
        grid_color.setAlpha(90)

        painter.setPen(axis_color)
        painter.drawLine(plot.bottomLeft(), plot.bottomRight())
        painter.drawLine(plot.bottomLeft(), plot.topLeft())

        for tick in _count_axis_ticks(max_count):
            y = plot.bottom() - plot.height() * (tick / max_count)
            painter.setPen(grid_color if tick > 0 else axis_color)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(axis_color)
            label_rect = QRectF(
                20,
                y - metrics.height() / 2,
                plot.left() - 26,
                metrics.height(),
            )
            painter.drawText(label_rect, Qt.AlignRight | Qt.AlignVCenter, str(tick))

        painter.save()
        painter.setPen(axis_color)
        painter.translate(4, plot.bottom())
        painter.rotate(-90)
        painter.drawText(
            QRectF(0, 0, plot.height(), metrics.height()),
            Qt.AlignCenter,
            "samples",
        )
        painter.restore()

        painter.setPen(axis_color)
        for tick in x_ticks:
            x = plot.left() + plot.width() * ((tick - minimum) / (maximum - minimum))
            painter.drawLine(
                int(round(x)),
                plot.bottom(),
                int(round(x)),
                plot.bottom() + 4,
            )
            label = f"{tick / scale:.{x_decimals}f}"
            label_width = max(56, metrics.horizontalAdvance(label) + 8)
            if tick == minimum:
                label_x = plot.left()
                alignment = Qt.AlignLeft | Qt.AlignVCenter
            elif tick == maximum:
                label_x = plot.right() - label_width
                alignment = Qt.AlignRight | Qt.AlignVCenter
            else:
                label_x = x - label_width / 2
                alignment = Qt.AlignCenter
            painter.drawText(
                QRectF(label_x, plot.bottom() + 5, label_width, metrics.height()),
                alignment,
                label,
            )
        x_axis_title_rect = QRectF(
            plot.left(),
            self.height() - metrics.height() - 2,
            plot.width(),
            metrics.height(),
        )
        painter.drawText(x_axis_title_rect, Qt.AlignCenter, self._x_axis_label(unit))

        if len(series) > 1:
            legend_x = plot.right()
            for label, color, _data in reversed(series):
                text_width = metrics.horizontalAdvance(label)
                legend_x -= text_width + 22
                painter.setPen(QColor(color).darker(125))
                painter.setBrush(QColor(color))
                painter.drawRect(QRectF(legend_x, 6, 10, 10))
                painter.setPen(axis_color)
                painter.drawText(
                    QRectF(legend_x + 14, 3, text_width + 4, metrics.height()),
                    Qt.AlignLeft | Qt.AlignVCenter,
                    label,
                )
                legend_x -= 8

        for series_index, (_label, color, _data) in enumerate(series):
            count_row = counts[series_index]
            color = QColor(color)
            color.setAlpha(150 if len(series) > 1 else 190)
            painter.setPen(color.darker(125))
            painter.setBrush(color)
            for index, count in enumerate(count_row):
                if count <= 0:
                    continue
                height = plot.height() * (count / max_count)
                if len(series) > 1:
                    width = max(1.0, bar_width / len(series))
                    x = plot.left() + index * bar_width + series_index * width
                else:
                    width = max(1.0, bar_width - 2.0)
                    x = plot.left() + index * bar_width + 1.0
                rect = QRectF(
                    x,
                    plot.bottom() - height,
                    width,
                    height,
                )
                painter.drawRect(rect)
        painter.setPen(axis_color)
        painter.drawLine(plot.bottomLeft(), plot.bottomRight())
        painter.drawLine(plot.bottomLeft(), plot.topLeft())

    def _x_axis_label(self, unit: str) -> str:
        if self._mode == "polarity":
            return f"V/I resistance ({unit})"
        return f"dV/dI resistance ({unit})"

    def _series(self) -> list[tuple[str, QColor, list[float]]]:
        if self._mode == "polarity":
            return [
                (
                    "negative",
                    QColor(200, 52, 60),
                    _sample_values(self._samples, "negative_resistance_ohm"),
                ),
                (
                    "positive",
                    QColor(36, 100, 210),
                    _sample_values(self._samples, "positive_resistance_ohm"),
                ),
            ]
        return [
            (
                "differential",
                QColor(43, 140, 96),
                _sample_values(self._samples, "differential_resistance_ohm"),
            )
        ]


class RouteMeasurementRawDataDialog(QDialog):
    """Copyable table of raw samples for one route measurement point."""

    HEADERS = (
        "sample",
        "polarity",
        "source_v",
        "measured_v",
        "current_a",
        "v_over_i_ohm",
        "differential_ohm",
        "compliance",
    )

    def __init__(self, samples: tuple[object, ...], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Raw Measurement Data")
        self.resize(840, 420)
        self._rows = _raw_data_rows(samples)

        layout = QVBoxLayout(self)
        self._table = QTableWidget(len(self._rows), len(self.HEADERS), self)
        self._table.setHorizontalHeaderLabels(self.HEADERS)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self._table.setSelectionBehavior(QAbstractItemView.SelectItems)
        for row_index, row in enumerate(self._rows):
            for column_index, value in enumerate(row):
                self._table.setItem(
                    row_index,
                    column_index,
                    QTableWidgetItem(value),
                )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        layout.addWidget(self._table)

        button_row = QHBoxLayout()
        copy_button = QPushButton("Copy All", self)
        close_button = QPushButton("Close", self)
        button_row.addWidget(copy_button)
        button_row.addStretch(1)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

        copy_button.clicked.connect(self._copy_all)
        close_button.clicked.connect(self.accept)

    def _copy_all(self) -> None:
        lines = ["\t".join(self.HEADERS)]
        lines.extend("\t".join(row) for row in self._rows)
        clipboard = QApplication.clipboard()
        clipboard.setText("\n".join(lines))


def _sample_values(samples: tuple[object, ...], attribute: str) -> list[float]:
    values: list[float] = []
    for sample in samples:
        try:
            value = float(getattr(sample, attribute))
        except (AttributeError, TypeError, ValueError):
            continue
        if math.isfinite(value):
            values.append(value)
    return values


def _histogram_counts(
    values: list[float],
    bin_count: int,
    minimum: float,
    maximum: float,
) -> list[int]:
    counts = [0 for _index in range(bin_count)]
    span = maximum - minimum
    if span <= 0.0:
        return counts
    for value in values:
        index = int((value - minimum) / span * bin_count)
        index = max(0, min(bin_count - 1, index))
        counts[index] += 1
    return counts


def _resistance_axis_unit(values: list[float]) -> tuple[float, str]:
    finite_values = [abs(value) for value in values if math.isfinite(value)]
    reference = max(finite_values, default=1.0)
    for scale, label in (
        (1e9, "GOhm"),
        (1e6, "MOhm"),
        (1e3, "kOhm"),
        (1.0, "Ohm"),
        (1e-3, "mOhm"),
        (1e-6, "uOhm"),
    ):
        if reference >= scale:
            return scale, label
    return 1.0, "Ohm"


def _axis_tick_decimals(scaled_span: float) -> int:
    span = abs(float(scaled_span))
    if not math.isfinite(span) or span <= 0.0:
        return 3
    tick_step = span / 2.0
    decimals = int(math.ceil(-math.log10(tick_step))) + 1
    return max(0, min(6, decimals))


def _count_axis_ticks(max_count: int) -> list[int]:
    if max_count <= 1:
        return [0, 1]
    middle = max(1, max_count // 2)
    return sorted({0, middle, max_count})


def _raw_data_rows(samples: tuple[object, ...]) -> list[list[str]]:
    rows: list[list[str]] = []
    for sample in samples:
        sample_index = str(getattr(sample, "sample_index", ""))
        differential = _format_number(
            getattr(sample, "differential_resistance_ohm", math.nan)
        )
        compliance = "yes" if bool(getattr(sample, "compliance_hit", False)) else ""
        added = False
        for polarity in ("negative", "positive"):
            values = (
                getattr(sample, f"{polarity}_source_voltage_v", None),
                getattr(sample, f"{polarity}_measured_voltage_v", None),
                getattr(sample, f"{polarity}_current_a", None),
                getattr(sample, f"{polarity}_resistance_ohm", None),
            )
            if all(value is None for value in values):
                continue
            rows.append(
                [
                    sample_index,
                    polarity,
                    _format_number(values[0]),
                    _format_number(values[1]),
                    _format_number(values[2]),
                    _format_number(values[3]),
                    differential,
                    compliance,
                ]
            )
            added = True
        if not added:
            rows.append(
                [
                    sample_index,
                    "differential",
                    "",
                    "",
                    "",
                    "",
                    differential,
                    compliance,
                ]
            )
    return rows


def _format_number(value: object) -> str:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(numeric):
        return ""
    return f"{numeric:.12g}"


def _format_ohm(value: float) -> str:
    if not math.isfinite(value):
        return "nan Ohm"
    abs_value = abs(value)
    for scale, unit in (
        (1e9, "GOhm"),
        (1e6, "MOhm"),
        (1e3, "kOhm"),
        (1.0, "Ohm"),
        (1e-3, "mOhm"),
        (1e-6, "uOhm"),
    ):
        if abs_value >= scale:
            return f"{value / scale:.3g} {unit}"
    return f"{value:.3g} Ohm"


def _format_percent(value: float) -> str:
    if not math.isfinite(value):
        return "nan%"
    return f"{value * 100.0:.3g}%"
