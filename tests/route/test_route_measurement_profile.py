from __future__ import annotations

import json
from pathlib import Path

import pytest


pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.dialogs.route_measurement_defaults import (
    DEFAULT_KEITHLEY_MEASUREMENT_VOLTAGE_V,
    DEFAULT_ROUTE_FOLLOWUP_MEASUREMENT_COUNT,
    DEFAULT_ROUTE_INITIAL_MEASUREMENT_COUNT,
)
from probe_station_gui.dialogs.route_measurement_dialog import RouteMeasurementDialog
from probe_station_gui.instruments.meters.lcr import ROUTE_METER_KEITHLEY
from probe_station_gui.route.measurement import ROUTE_OPERATION_PHOTO_THEN_MEASURE


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _dialog(tmp_path: Path) -> RouteMeasurementDialog:
    return RouteMeasurementDialog(
        route_name="route-a",
        route_point_count=4,
        default_csv_path=str(tmp_path / "default.csv"),
        settings_path=tmp_path / "route-measurement-settings.json",
    )


def test_route_measurement_profile_round_trips_dialog_state(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    profile = {
        "version": 7,
        "csv_path": str(tmp_path / "measure.csv"),
        "previous_csv_path": str(tmp_path / "previous.csv"),
        "operation_mode": ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        "photo_output_dir": str(tmp_path / "photos"),
        "photo_settle_s": 0.75,
        "photo_autofocus_enabled": True,
        "photo_autofocus_range_mm": 0.04,
        "initial_measurement_count": 3,
        "followup_measurement_count": 7,
        "current_point": 2,
        "measurement_session_active": True,
        "max_relative_rms": 0.125,
        "contact_settle_s": 0.5,
        "contact_seek_range_mm": 0.02,
        "contact_seek_step_mm": 0.002,
        "contact_quality_limits": {
            "max_mad_sigma_ohm": 11.0,
            "max_p95_abs_step_ohm": 22.0,
            "max_relative_mad_sigma": 0.03,
            "max_relative_p95_abs_step": 0.04,
        },
        "previous_ok_only": True,
        "meter": {
            "meter_type": ROUTE_METER_KEITHLEY,
            "keithley": {
                "measurement_voltage_v": 0.04,
                "source_voltage_range_v": 0.2,
                "voltmeter_range_v": 0.1,
                "current_range_a": 5e-6,
                "compliance_current_a": 6e-6,
                "nplc": 2.5,
                "terminals": "front",
                "trigger_delay_s": 0.1,
                "use_buffer": False,
                "use_trigger_link": False,
            },
        },
    }

    assert dialog._apply_profile_data(profile) is False

    saved = dialog._profile_data()
    assert saved["csv_path"] == str(tmp_path / "measure.csv")
    assert saved["previous_csv_path"] == str(tmp_path / "previous.csv")
    assert saved["operation_mode"] == ROUTE_OPERATION_PHOTO_THEN_MEASURE
    assert saved["initial_measurement_count"] == 3
    assert saved["followup_measurement_count"] == 7
    assert saved["current_point"] == 2
    assert saved["measurement_session_active"] is True
    assert saved["measurement_pending"] is True
    assert saved["contact_quality_limits"]["max_mad_sigma_ohm"] == pytest.approx(11.0)
    assert saved["contact_quality_limits"]["max_relative_mad_sigma"] == pytest.approx(
        0.03
    )
    assert saved["meter"]["meter_type"] == ROUTE_METER_KEITHLEY
    assert saved["meter"]["keithley"]["measurement_voltage_v"] == pytest.approx(0.04)
    assert saved["meter"]["keithley"]["use_buffer"] is False

    configuration = dialog.current_configuration()

    assert configuration.csv_path == str(tmp_path / "measure.csv")
    assert configuration.initial_measurement_count == 3
    assert configuration.followup_measurement_count == 7
    assert configuration.current_point == 2
    assert configuration.contact_quality_limits.max_p95_abs_step_ohm == pytest.approx(
        22.0
    )

    settings_data = json.loads(
        (tmp_path / "route-measurement-settings.json").read_text(encoding="utf-8")
    )
    assert settings_data["csv_path"] == str(tmp_path / "measure.csv")


def test_legacy_keithley_profile_migrates_route_defaults(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    _ = qt_app
    dialog = _dialog(tmp_path)
    legacy_profile = {
        "version": 5,
        "initial_measurement_count": 1,
        "followup_measurement_count": 2,
        "meter": {
            "meter_type": ROUTE_METER_KEITHLEY,
            "keithley": {
                "measurement_voltage_v": 0.5,
                "use_buffer": False,
                "use_trigger_link": False,
            },
        },
    }

    assert dialog._apply_profile_data(legacy_profile) is True

    saved = dialog._profile_data()
    assert saved["initial_measurement_count"] == DEFAULT_ROUTE_INITIAL_MEASUREMENT_COUNT
    assert saved["followup_measurement_count"] == DEFAULT_ROUTE_FOLLOWUP_MEASUREMENT_COUNT
    assert saved["meter"]["keithley"]["measurement_voltage_v"] == pytest.approx(
        DEFAULT_KEITHLEY_MEASUREMENT_VOLTAGE_V
    )
    assert saved["meter"]["keithley"]["use_buffer"] is True
    assert saved["meter"]["keithley"]["use_trigger_link"] is True
