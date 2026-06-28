"""Profile persistence helpers for route measurement dialogs."""

from __future__ import annotations

import json
import math
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QFileDialog

from probe_station_gui.dialogs.route_measurement_defaults import (
    DEFAULT_KEITHLEY_COMPLIANCE_CURRENT_A,
    DEFAULT_KEITHLEY_CURRENT_RANGE_A,
    DEFAULT_KEITHLEY_MEASUREMENT_VOLTAGE_V,
    DEFAULT_KEITHLEY_NPLC,
    DEFAULT_KEITHLEY_SOURCE_RANGE_V,
    DEFAULT_KEITHLEY_TRIGGER_DELAY_S,
    DEFAULT_KEITHLEY_USE_BUFFER,
    DEFAULT_KEITHLEY_USE_TRIGGER_LINK,
    DEFAULT_KEITHLEY_VOLTMETER_RANGE_V,
    DEFAULT_ROUTE_FOLLOWUP_MEASUREMENT_COUNT,
    DEFAULT_ROUTE_INITIAL_MEASUREMENT_COUNT,
    KEITHLEY_DEFAULTS_PROFILE_VERSION,
    ROUTE_MEASUREMENT_PROFILE_VERSION,
)
from probe_station_gui.dialogs.route_measurement_widgets import SIPrefixSpinBox
from probe_station_gui.instruments.meters.lcr import ROUTE_METER_KEITHLEY
from probe_station_gui.route.measurement import (
    ROUTE_OPERATION_MEASURE,
    RouteContactQualityLimits,
)
from probe_station_gui.route.measurement_config import route_measurement_count_profile
from probe_station_gui.shared.wheel_guard import (
    GuardedComboBox as QComboBox,
    GuardedSpinBox as QSpinBox,
)


class RouteMeasurementProfileMixin:
    """Profile load/save/apply behavior for the route measurement dialog."""

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
            "contact_quality_limits": self._contact_quality_limits().as_dict(),
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
        self._apply_contact_quality_limits_profile(data)
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
        if version >= KEITHLEY_DEFAULTS_PROFILE_VERSION:
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
        initial, followup = route_measurement_count_profile(
            data,
            default_initial_count=DEFAULT_ROUTE_INITIAL_MEASUREMENT_COUNT,
        )
        if initial is not None:
            self._initial_measurement_count_spin.setValue(initial)
        if followup is not None:
            self._followup_measurement_count_spin.setValue(followup)

    def _total_measurement_count(self) -> int:
        return int(self._initial_measurement_count_spin.value()) + int(
            self._followup_measurement_count_spin.value()
        )

    def _contact_quality_limits(self) -> RouteContactQualityLimits:
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

    def _apply_contact_quality_limits_profile(self, data: dict[str, Any]) -> None:
        nested = data.get("contact_quality_limits", data.get("contact_quality"))
        nested_data = nested if isinstance(nested, dict) else {}
        self._set_spinbox_value(
            self._contact_max_mad_sigma_spin,
            self._first_profile_value(
                nested_data,
                data,
                "max_mad_sigma_ohm",
                "contact_max_mad_sigma_ohm",
            ),
        )
        self._set_spinbox_value(
            self._contact_max_p95_step_spin,
            self._first_profile_value(
                nested_data,
                data,
                "max_p95_abs_step_ohm",
                "contact_max_p95_abs_step_ohm",
            ),
        )
        self._set_ratio_percent_spinbox_value(
            self._contact_max_relative_mad_spin,
            self._first_profile_value(
                nested_data,
                data,
                "max_relative_mad_sigma",
                "contact_max_relative_mad_sigma",
            ),
        )
        self._set_ratio_percent_spinbox_value(
            self._contact_max_relative_p95_step_spin,
            self._first_profile_value(
                nested_data,
                data,
                "max_relative_p95_abs_step",
                "contact_max_relative_p95_abs_step",
            ),
        )

    @staticmethod
    def _first_profile_value(
        primary: dict[str, Any],
        secondary: dict[str, Any],
        *keys: str,
    ) -> object | None:
        for source in (primary, secondary):
            for key in keys:
                if key in source:
                    return source.get(key)
        return None

    def _apply_gwinstek_profile(self, data: object) -> None:
        if not isinstance(data, dict):
            return
        self._set_combo_text(self._gw_function_combo, data.get("measurement_function"))
        self._set_combo_data(self._gw_range_mode_combo, data.get("range_mode"))
        self._set_spinbox_value(
            self._gw_impedance_range_spin,
            data.get("impedance_range"),
        )
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
            if isinstance(spinbox, SIPrefixSpinBox):
                spinbox.set_base_value(numeric)
            elif isinstance(spinbox, QSpinBox):
                spinbox.setValue(int(round(numeric)))
            else:
                spinbox.setValue(numeric)

    @staticmethod
    def _set_ratio_percent_spinbox_value(spinbox, value: object) -> None:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return
        if math.isfinite(numeric):
            spinbox.setValue(numeric * 100.0)

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
