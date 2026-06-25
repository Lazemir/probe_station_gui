"""Core package for the probe station GUI."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_LAZY_EXPORTS = {
    "Grabber": ("probe_station_gui.camera_worker", "Grabber"),
    "DesignDocument": ("probe_station_gui.design.model", "DesignDocument"),
    "DesignNavigatorPanel": (
        "probe_station_gui.views.design_navigator_panel",
        "DesignNavigatorPanel",
    ),
    "DesignRegistration": ("probe_station_gui.design.model", "DesignRegistration"),
    "JoystickWindow": ("probe_station_gui.views.joystick_window", "JoystickWindow"),
    "MeasurementTarget": ("probe_station_gui.design.model", "MeasurementTarget"),
    "MicroscopeView": ("probe_station_gui.views.microscope_view", "MicroscopeView"),
    "SerialScannerDialog": (
        "probe_station_gui.dialogs.serial_scanner",
        "SerialScannerDialog",
    ),
    "SerialTerminalWindow": (
        "probe_station_gui.views.serial_terminal_window",
        "SerialTerminalWindow",
    ),
    "MoveVector": ("probe_station_gui.stage.types", "MoveVector"),
    "StageController": ("probe_station_gui.stage_controller", "StageController"),
}

__all__ = list(_LAZY_EXPORTS)


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute_name = _LAZY_EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc
    value = getattr(import_module(module_name), attribute_name)
    globals()[name] = value
    return value
