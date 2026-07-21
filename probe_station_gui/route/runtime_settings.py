"""Runtime settings adapters for route measurement runners."""

from __future__ import annotations

from typing import Any

from probe_station_gui.route.operation_modes import route_operation_measure_enabled


def route_common_runtime_settings(configuration: object) -> dict[str, Any]:
    return {
        "measurement_count": getattr(configuration, "measurement_count"),
        "initial_measurement_count": getattr(
            configuration,
            "initial_measurement_count",
        ),
        "max_relative_rms": getattr(configuration, "max_relative_rms"),
        "contact_quality_limits": getattr(configuration, "contact_quality_limits"),
        "auto_contact_seek_step_mm": getattr(configuration, "contact_seek_step_mm"),
        "auto_contact_seek_max_total_mm": getattr(
            configuration,
            "contact_seek_range_mm",
        ),
        "contact_settle_s": getattr(configuration, "contact_settle_s"),
        "photo_settle_s": getattr(configuration, "photo_settle_s"),
    }


def route_measurement_runtime_settings(configuration: object) -> dict[str, Any]:
    settings = route_common_runtime_settings(configuration)
    meter = getattr(configuration, "meter")
    settings.update(
        {
            "photo_focus_enabled": getattr(
                configuration,
                "photo_autofocus_enabled",
            ),
            "photo_focus_range_mm": getattr(
                configuration,
                "photo_autofocus_range_mm",
            ),
            "photo_output_dir": getattr(configuration, "photo_output_dir"),
            "csv_path": getattr(configuration, "csv_path"),
            "nplc_label": meter.nplc_label(),
            "measurement_type": meter.measurement_type_label(),
        }
    )
    return settings


def route_runtime_requires_meter_configuration(configuration: object) -> bool:
    return route_operation_measure_enabled(getattr(configuration, "operation_mode"))


def route_waiting_restart_required(
    *,
    external_session: bool,
    waiting: bool,
    setup_changed: bool,
) -> bool:
    return bool(not external_session and waiting and setup_changed)


__all__ = [
    "route_common_runtime_settings",
    "route_measurement_runtime_settings",
    "route_runtime_requires_meter_configuration",
    "route_waiting_restart_required",
]
