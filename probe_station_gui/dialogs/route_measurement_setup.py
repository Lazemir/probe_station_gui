"""Route measurement setup editing and validation."""

from __future__ import annotations

import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PySide6.QtCore import QLocale, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.dialogs.route_measurement_defaults import (
    DEFAULT_ROUTE_CONTACT_QUALITY_LIMITS,
    DEFAULT_ROUTE_CONTACT_SEEK_RANGE_MM,
    DEFAULT_ROUTE_CONTACT_SEEK_STEP_MM,
    DEFAULT_ROUTE_FOLLOWUP_MEASUREMENT_COUNT,
    DEFAULT_ROUTE_INITIAL_MEASUREMENT_COUNT,
    DEFAULT_ROUTE_PHOTO_AUTOFOCUS_RANGE_MM,
    RESISTANCE_PREFIXES,
)
from probe_station_gui.dialogs.route_measurement_meter import (
    RouteMeasurementMeterEditor,
)
from probe_station_gui.dialogs.route_measurement_operation_state import (
    route_measurement_operation_state,
)
from probe_station_gui.dialogs.route_measurement_widgets import (
    SIPrefixSpinBox,
    apply_profile_numeric_value,
)
from probe_station_gui.route.contact_quality import RouteContactQualityLimits
from probe_station_gui.route.measurement_config import (
    RouteMeasurementRunConfiguration,
    route_measurement_count_profile,
)
from probe_station_gui.route.operation_modes import (
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_PHOTO,
    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
    route_operation_measure_enabled,
    route_operation_photo_enabled,
)
from probe_station_gui.shared.wheel_guard import (
    GuardedComboBox as QComboBox,
    GuardedDoubleSpinBox as QDoubleSpinBox,
    GuardedSpinBox as QSpinBox,
)


class RouteMeasurementSetupEditor(QWidget):
    """Own editable route setup and produce validated run configurations."""

    measure_requested = Signal(object)
    status_changed = Signal(str)
    settings_changed = Signal()
    load_profile_requested = Signal()
    save_profile_requested = Signal()

    def __init__(
        self,
        *,
        route_name: str,
        route_point_count: int,
        default_csv_path: str,
        default_photo_dir: str,
        default_meter_type: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._default_csv_path = default_csv_path
        self._default_photo_dir = default_photo_dir
        self._running = False
        self._waiting = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        common_group = QGroupBox("Measurement", self)
        form = QFormLayout(common_group)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._route_combo = QComboBox(common_group)
        self._route_combo.addItem(f"{route_name} ({route_point_count} points)")
        form.addRow(QLabel("Route", common_group), self._route_combo)

        self._csv_path_edit, self._csv_browse_button = self._path_row(
            form,
            common_group,
            label="CSV",
            text=default_csv_path,
            placeholder="measurement_results.csv",
        )
        self._previous_csv_path_edit, self._previous_csv_browse_button = self._path_row(
            form,
            common_group,
            label="Previous CSV",
            text=default_csv_path,
            placeholder="previous measurement CSV",
        )
        self._previous_ok_only_checkbox = QCheckBox("Only previous OK", common_group)
        self._previous_ok_only_checkbox.setToolTip(
            "Use the latest row for each structure in the previous CSV."
        )
        form.addRow(
            QLabel("Point filter", common_group), self._previous_ok_only_checkbox
        )

        self._operation_combo = QComboBox(common_group)
        self._operation_combo.addItem("Measure only", ROUTE_OPERATION_MEASURE)
        self._operation_combo.addItem("Photo only", ROUTE_OPERATION_PHOTO)
        self._operation_combo.addItem(
            "Photo then measure", ROUTE_OPERATION_PHOTO_THEN_MEASURE
        )
        form.addRow(QLabel("Route mode", common_group), self._operation_combo)

        self._photo_dir_edit, self._photo_browse_button = self._path_row(
            form,
            common_group,
            label="Photos",
            text=default_photo_dir,
            placeholder="route photo output directory",
        )
        self._photo_settle_spin = self._decimal_row(
            form, common_group, "Photo settle", 3, 0.0, 10.0, 0.05, " s", 0.2
        )
        self._photo_autofocus_checkbox = QCheckBox(
            "Autofocus before each point", common_group
        )
        form.addRow(QLabel("Autofocus", common_group), self._photo_autofocus_checkbox)
        self._photo_autofocus_range_spin = self._decimal_row(
            form,
            common_group,
            "AF range",
            4,
            0.001,
            0.2,
            0.005,
            " mm",
            DEFAULT_ROUTE_PHOTO_AUTOFOCUS_RANGE_MM,
        )

        self._initial_measurement_count_spin = self._integer_row(
            form,
            common_group,
            "Initial samples",
            1,
            DEFAULT_ROUTE_INITIAL_MEASUREMENT_COUNT,
        )
        self._followup_measurement_count_spin = self._integer_row(
            form,
            common_group,
            "Follow-up samples",
            0,
            DEFAULT_ROUTE_FOLLOWUP_MEASUREMENT_COUNT,
        )
        self._max_relative_rms_spin = self._decimal_row(
            form, common_group, "Max rel RMS", 3, 0.001, 100.0, 0.1, " %", 1.0
        )
        self._contact_settle_spin = self._decimal_row(
            form, common_group, "Contact settle", 3, 0.0, 60.0, 0.05, " s", 0.2
        )
        self._contact_seek_range_spin = self._decimal_row(
            form,
            common_group,
            "Contact seek range",
            4,
            0.0,
            1.0,
            0.001,
            " mm",
            DEFAULT_ROUTE_CONTACT_SEEK_RANGE_MM,
        )
        self._contact_seek_step_spin = self._decimal_row(
            form,
            common_group,
            "Contact seek step",
            4,
            0.0001,
            1.0,
            0.0005,
            " mm",
            DEFAULT_ROUTE_CONTACT_SEEK_STEP_MM,
        )

        self._contact_max_mad_sigma_spin = self._resistance_row(
            form,
            common_group,
            "MAD limit",
            DEFAULT_ROUTE_CONTACT_QUALITY_LIMITS.max_mad_sigma_ohm,
        )
        self._contact_max_p95_step_spin = self._resistance_row(
            form,
            common_group,
            "P95 step limit",
            DEFAULT_ROUTE_CONTACT_QUALITY_LIMITS.max_p95_abs_step_ohm,
        )
        self._contact_max_relative_mad_spin = self._decimal_row(
            form,
            common_group,
            "Rel MAD limit",
            3,
            0.0,
            100.0,
            0.1,
            " %",
            DEFAULT_ROUTE_CONTACT_QUALITY_LIMITS.max_relative_mad_sigma * 100,
        )
        self._contact_max_relative_p95_step_spin = self._decimal_row(
            form,
            common_group,
            "Rel P95 step limit",
            3,
            0.0,
            100.0,
            0.1,
            " %",
            DEFAULT_ROUTE_CONTACT_QUALITY_LIMITS.max_relative_p95_abs_step * 100,
        )

        profile_row = QHBoxLayout()
        self._load_profile_button = QPushButton("Load Profile", common_group)
        self._save_profile_button = QPushButton("Save Profile", common_group)
        profile_row.addWidget(self._load_profile_button)
        profile_row.addWidget(self._save_profile_button)
        profile_row.addStretch(1)
        form.addRow(QLabel("Profile", common_group), profile_row)
        layout.addWidget(common_group)
        self._meter_editor = RouteMeasurementMeterEditor(
            default_meter_type=default_meter_type,
            parent=self,
        )
        layout.addWidget(self._meter_editor)

        self._csv_browse_button.clicked.connect(self._choose_csv_path)
        self._previous_csv_browse_button.clicked.connect(self._choose_previous_csv_path)
        self._photo_browse_button.clicked.connect(self._choose_photo_dir)
        self._operation_combo.currentIndexChanged.connect(self.refresh_state)
        self._photo_autofocus_checkbox.toggled.connect(self.refresh_state)
        self._previous_ok_only_checkbox.toggled.connect(self.refresh_state)
        self._load_profile_button.clicked.connect(self.load_profile_requested.emit)
        self._save_profile_button.clicked.connect(self.save_profile_requested.emit)
        self.refresh_state()

    def request_measure(self, current_point: int) -> None:
        mode = self.operation_mode()
        if route_operation_measure_enabled(mode) and not self.csv_path():
            self.status_changed.emit("Choose a CSV path before measuring.")
            return
        if (
            route_operation_measure_enabled(mode)
            and self._previous_ok_only_checkbox.isChecked()
            and not self.previous_csv_path()
        ):
            self.status_changed.emit("Choose a previous CSV before filtering.")
            return
        if route_operation_photo_enabled(mode) and not self.photo_output_dir():
            self.status_changed.emit("Choose a photo directory before capturing.")
            return
        self.measure_requested.emit(self.configuration(current_point))

    def configuration(self, current_point: int) -> RouteMeasurementRunConfiguration:
        mode = self.operation_mode()
        return RouteMeasurementRunConfiguration(
            csv_path=self.csv_path(),
            previous_csv_path=self.previous_csv_path(),
            operation_mode=mode,
            photo_output_dir=self.photo_output_dir(),
            photo_settle_s=float(self._photo_settle_spin.value()),
            photo_autofocus_enabled=self._photo_autofocus_checkbox.isChecked(),
            photo_autofocus_range_mm=float(self._photo_autofocus_range_spin.value()),
            initial_measurement_count=int(self._initial_measurement_count_spin.value()),
            followup_measurement_count=int(
                self._followup_measurement_count_spin.value()
            ),
            current_point=int(current_point),
            max_relative_rms=float(self._max_relative_rms_spin.value()) / 100.0,
            contact_settle_s=float(self._contact_settle_spin.value()),
            contact_seek_range_mm=float(self._contact_seek_range_spin.value()),
            contact_seek_step_mm=float(self._contact_seek_step_spin.value()),
            previous_ok_only=(
                self._previous_ok_only_checkbox.isChecked()
                and route_operation_measure_enabled(mode)
            ),
            meter=self._meter_editor.configuration(),
            contact_quality_limits=self.contact_quality_limits(),
        )

    def set_route(
        self,
        *,
        route_name: str,
        route_point_count: int,
        default_csv_path: str,
        default_photo_dir: str | None,
    ) -> None:
        self._route_combo.setItemText(0, f"{route_name} ({route_point_count} points)")
        if self._running:
            return
        if not self.csv_path() or self.csv_path() == self._default_csv_path:
            self._csv_path_edit.setText(default_csv_path)
        if (
            not self.previous_csv_path()
            or self.previous_csv_path() == self._default_csv_path
        ):
            self._previous_csv_path_edit.setText(default_csv_path)
        self._default_csv_path = default_csv_path
        if default_photo_dir is not None:
            if (
                not self.photo_output_dir()
                or self.photo_output_dir() == self._default_photo_dir
            ):
                self._photo_dir_edit.setText(default_photo_dir)
            self._default_photo_dir = default_photo_dir

    def set_run_state(self, *, running: bool, waiting: bool) -> None:
        self._running = bool(running)
        self._waiting = bool(waiting)
        idle = not self._running
        self._route_combo.setEnabled(idle)
        self._load_profile_button.setEnabled(idle)
        self._save_profile_button.setEnabled(idle)
        self._operation_combo.setEnabled(idle or self._waiting)
        self.refresh_state()

    def refresh_state(self, *_args: object) -> None:
        state = route_measurement_operation_state(
            mode=self.operation_mode(),
            running=self._running,
            waiting=self._waiting,
            autofocus_checked=self._photo_autofocus_checkbox.isChecked(),
            previous_ok_only_checked=self._previous_ok_only_checkbox.isChecked(),
        )
        for widget in (
            self._photo_dir_edit,
            self._photo_browse_button,
            self._photo_settle_spin,
        ):
            widget.setEnabled(state.photo_controls_enabled)
        self._photo_autofocus_checkbox.setEnabled(state.photo_autofocus_enabled)
        self._photo_autofocus_range_spin.setEnabled(state.photo_autofocus_range_enabled)
        for widget in (
            self._csv_path_edit,
            self._csv_browse_button,
            self._previous_ok_only_checkbox,
            self._meter_editor,
            *self._measurement_widgets(),
        ):
            widget.setEnabled(state.measurement_controls_enabled)
        self._previous_csv_path_edit.setEnabled(state.previous_csv_enabled)
        self._previous_csv_browse_button.setEnabled(state.previous_csv_enabled)

    def profile_data(self) -> dict[str, Any]:
        configuration = self.configuration(current_point=1)
        data = asdict(configuration)
        del data["current_point"]
        data["measurement_count"] = configuration.measurement_count
        data["previous_ok_only"] = self._previous_ok_only_checkbox.isChecked()
        return data

    def apply_profile_data(self, data: dict[str, Any]) -> None:
        csv_path = self._profile_text(data.get("csv_path"))
        if csv_path is not None:
            self._csv_path_edit.setText(csv_path)
        previous_csv = self._profile_text(data.get("previous_csv_path"))
        if previous_csv is not None:
            self._previous_csv_path_edit.setText(previous_csv)
        elif csv_path is not None:
            self._previous_csv_path_edit.setText(csv_path)
        self._set_combo_data(self._operation_combo, data.get("operation_mode"))
        photo_dir = self._profile_text(data.get("photo_output_dir"))
        if photo_dir is not None:
            self._photo_dir_edit.setText(photo_dir)
        apply_profile_numeric_value(self._photo_settle_spin, data.get("photo_settle_s"))
        self._photo_autofocus_checkbox.setChecked(
            bool(data.get("photo_autofocus_enabled", False))
        )
        apply_profile_numeric_value(
            self._photo_autofocus_range_spin,
            data.get("photo_autofocus_range_mm"),
        )
        initial, followup = route_measurement_count_profile(
            data,
            default_initial_count=DEFAULT_ROUTE_INITIAL_MEASUREMENT_COUNT,
        )
        if initial is not None:
            self._initial_measurement_count_spin.setValue(initial)
        if followup is not None:
            self._followup_measurement_count_spin.setValue(followup)
        self._set_ratio(self._max_relative_rms_spin, data.get("max_relative_rms"))
        self._previous_ok_only_checkbox.setChecked(
            bool(data.get("previous_ok_only", False))
        )
        apply_profile_numeric_value(
            self._contact_settle_spin, data.get("contact_settle_s")
        )
        apply_profile_numeric_value(
            self._contact_seek_range_spin, data.get("contact_seek_range_mm")
        )
        apply_profile_numeric_value(
            self._contact_seek_step_spin, data.get("contact_seek_step_mm")
        )
        self._apply_contact_quality_profile(data)
        self._meter_editor.apply_profile(data.get("meter"))
        self.refresh_state()

    def apply_default_keithley_settings(self) -> None:
        self._initial_measurement_count_spin.setValue(
            DEFAULT_ROUTE_INITIAL_MEASUREMENT_COUNT
        )
        self._followup_measurement_count_spin.setValue(
            DEFAULT_ROUTE_FOLLOWUP_MEASUREMENT_COUNT
        )
        self._meter_editor.apply_default_keithley_settings()

    def csv_path(self) -> str:
        return self._csv_path_edit.text().strip()

    def previous_csv_path(self) -> str:
        return self._previous_csv_path_edit.text().strip()

    def photo_output_dir(self) -> str:
        return self._photo_dir_edit.text().strip()

    def operation_mode(self) -> str:
        return str(self._operation_combo.currentData() or ROUTE_OPERATION_MEASURE)

    def total_measurement_count(self) -> int:
        return int(self._initial_measurement_count_spin.value()) + int(
            self._followup_measurement_count_spin.value()
        )

    def contact_quality_limits(self) -> RouteContactQualityLimits:
        return RouteContactQualityLimits(
            max_mad_sigma_ohm=self._contact_max_mad_sigma_spin.base_value(),
            max_p95_abs_step_ohm=self._contact_max_p95_step_spin.base_value(),
            max_relative_mad_sigma=(
                float(self._contact_max_relative_mad_spin.value()) / 100.0
            ),
            max_relative_p95_abs_step=(
                float(self._contact_max_relative_p95_step_spin.value()) / 100.0
            ),
        ).normalized()

    def profile_start_directory(self, settings_path: Path | None) -> Path:
        csv_path = Path(self.csv_path() or ".").expanduser()
        if csv_path.parent != Path("."):
            return csv_path.parent
        return settings_path.parent if settings_path is not None else Path.cwd()

    def _measurement_widgets(self) -> tuple[QWidget, ...]:
        return (
            self._initial_measurement_count_spin,
            self._followup_measurement_count_spin,
            self._max_relative_rms_spin,
            self._contact_settle_spin,
            self._contact_seek_range_spin,
            self._contact_seek_step_spin,
            self._contact_max_mad_sigma_spin,
            self._contact_max_p95_step_spin,
            self._contact_max_relative_mad_spin,
            self._contact_max_relative_p95_step_spin,
        )

    def _choose_csv_path(self) -> None:
        start = self.csv_path() or str(Path.cwd() / "probe_route_measurements.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Route Measurements", start, "CSV files (*.csv);;All files (*)"
        )
        if path:
            self._csv_path_edit.setText(path)
            self.settings_changed.emit()

    def _choose_previous_csv_path(self) -> None:
        start = self.previous_csv_path() or self.csv_path()
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
            self.settings_changed.emit()

    def _choose_photo_dir(self) -> None:
        start = self.photo_output_dir() or self._default_photo_dir or str(Path.cwd())
        path = QFileDialog.getExistingDirectory(self, "Route Photo Directory", start)
        if path:
            self._photo_dir_edit.setText(path)
            self.settings_changed.emit()

    def _apply_contact_quality_profile(self, data: dict[str, Any]) -> None:
        nested = data.get("contact_quality_limits", data.get("contact_quality"))
        values = nested if isinstance(nested, dict) else {}
        fields = (
            (
                self._contact_max_mad_sigma_spin,
                "max_mad_sigma_ohm",
                "contact_max_mad_sigma_ohm",
                False,
            ),
            (
                self._contact_max_p95_step_spin,
                "max_p95_abs_step_ohm",
                "contact_max_p95_abs_step_ohm",
                False,
            ),
            (
                self._contact_max_relative_mad_spin,
                "max_relative_mad_sigma",
                "contact_max_relative_mad_sigma",
                True,
            ),
            (
                self._contact_max_relative_p95_step_spin,
                "max_relative_p95_abs_step",
                "contact_max_relative_p95_abs_step",
                True,
            ),
        )
        for widget, nested_key, legacy_key, ratio in fields:
            value = values.get(nested_key, data.get(nested_key, data.get(legacy_key)))
            apply_value = self._set_ratio if ratio else apply_profile_numeric_value
            apply_value(widget, value)

    @staticmethod
    def _path_row(
        form: QFormLayout,
        parent: QWidget,
        *,
        label: str,
        text: str,
        placeholder: str,
    ) -> tuple[QLineEdit, QPushButton]:
        row = QHBoxLayout()
        line_edit = QLineEdit(parent)
        line_edit.setText(text)
        line_edit.setPlaceholderText(placeholder)
        button = QPushButton("Browse", parent)
        row.addWidget(line_edit, 1)
        row.addWidget(button)
        form.addRow(QLabel(label, parent), row)
        return line_edit, button

    @staticmethod
    def _decimal_row(
        form: QFormLayout,
        parent: QWidget,
        label: str,
        decimals: int,
        minimum: float,
        maximum: float,
        step: float,
        suffix: str,
        value: float,
    ) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(parent)
        spin.setLocale(QLocale.c())
        spin.setDecimals(decimals)
        spin.setRange(minimum, maximum)
        spin.setSingleStep(step)
        spin.setSuffix(suffix)
        spin.setValue(value)
        form.addRow(QLabel(label, parent), spin)
        return spin

    @staticmethod
    def _integer_row(
        form: QFormLayout,
        parent: QWidget,
        label: str,
        minimum: int,
        value: int,
    ) -> QSpinBox:
        spin = QSpinBox(parent)
        spin.setRange(minimum, 1000)
        spin.setValue(value)
        form.addRow(QLabel(label, parent), spin)
        return spin

    @staticmethod
    def _resistance_row(
        form: QFormLayout,
        parent: QWidget,
        label: str,
        value: float,
    ) -> SIPrefixSpinBox:
        spin = SIPrefixSpinBox(
            prefixes=RESISTANCE_PREFIXES,
            base_minimum=0.0,
            base_maximum=1e12,
            base_value=value,
            parent=parent,
        )
        form.addRow(QLabel(label, parent), spin)
        return spin

    @staticmethod
    def _profile_text(value: object) -> str | None:
        if not isinstance(value, str):
            return None
        stripped = value.strip()
        return stripped or None

    @staticmethod
    def _set_ratio(spinbox: QWidget, value: object) -> None:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        if math.isfinite(numeric):
            spinbox.setValue(numeric * 100.0)  # type: ignore[attr-defined]

    @staticmethod
    def _set_combo_data(combo: QComboBox, value: object) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)


__all__ = ["RouteMeasurementSetupEditor"]
