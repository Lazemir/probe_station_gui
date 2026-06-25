from types import SimpleNamespace

from probe_station_gui.route_operation_modes import (
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_PHOTO,
)
from probe_station_gui.route_runtime_settings import (
    route_external_runtime_settings,
    route_measurement_runtime_settings,
    route_runtime_requires_meter_configuration,
    route_waiting_restart_required,
)


class FakeMeter:
    def nplc_label(self) -> str:
        return "1"

    def measurement_type_label(self) -> str:
        return "4wire"


def _configuration(**overrides):
    data = {
        "measurement_count": 7,
        "initial_measurement_count": 3,
        "max_relative_rms": 0.02,
        "contact_quality_limits": "limits",
        "contact_seek_step_mm": 0.001,
        "contact_seek_range_mm": 0.05,
        "contact_settle_s": 0.2,
        "photo_settle_s": 0.4,
        "photo_autofocus_enabled": True,
        "csv_path": "route.csv",
        "operation_mode": ROUTE_OPERATION_MEASURE,
        "meter": FakeMeter(),
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_route_external_runtime_settings_use_common_settings_only() -> None:
    assert route_external_runtime_settings(_configuration()) == {
        "measurement_count": 7,
        "initial_measurement_count": 3,
        "max_relative_rms": 0.02,
        "contact_quality_limits": "limits",
        "auto_contact_seek_step_mm": 0.001,
        "auto_contact_seek_max_total_mm": 0.05,
        "contact_settle_s": 0.2,
        "photo_settle_s": 0.4,
    }


def test_route_measurement_runtime_settings_include_meter_and_output_settings() -> None:
    assert route_measurement_runtime_settings(_configuration()) == {
        "measurement_count": 7,
        "initial_measurement_count": 3,
        "max_relative_rms": 0.02,
        "contact_quality_limits": "limits",
        "auto_contact_seek_step_mm": 0.001,
        "auto_contact_seek_max_total_mm": 0.05,
        "contact_settle_s": 0.2,
        "photo_settle_s": 0.4,
        "photo_focus_enabled": True,
        "csv_path": "route.csv",
        "nplc_label": "1",
        "measurement_type": "4wire",
    }


def test_route_runtime_requires_meter_configuration_tracks_operation_mode() -> None:
    assert route_runtime_requires_meter_configuration(_configuration()) is True
    assert (
        route_runtime_requires_meter_configuration(
            _configuration(operation_mode=ROUTE_OPERATION_PHOTO)
        )
        is False
    )


def test_route_waiting_restart_required_only_for_changed_waiting_gui_run() -> None:
    assert (
        route_waiting_restart_required(
            external_session=False,
            waiting=True,
            setup_changed=True,
        )
        is True
    )
    assert (
        route_waiting_restart_required(
            external_session=True,
            waiting=True,
            setup_changed=True,
        )
        is False
    )
    assert (
        route_waiting_restart_required(
            external_session=False,
            waiting=False,
            setup_changed=True,
        )
        is False
    )
    assert (
        route_waiting_restart_required(
            external_session=False,
            waiting=True,
            setup_changed=False,
        )
        is False
    )
