"""View widgets for the probe station GUI."""

from __future__ import annotations

from importlib import import_module
from typing import Any


_LAZY_EXPORTS = {
    "DesignNavigatorPanel": (
        "probe_station_gui.views.design_navigator_panel",
        "DesignNavigatorPanel",
    ),
    "JoystickWindow": ("probe_station_gui.views.joystick_window", "JoystickWindow"),
    "MicroscopeView": ("probe_station_gui.views.microscope_view", "MicroscopeView"),
    "SerialTerminalWindow": (
        "probe_station_gui.views.serial_terminal_window",
        "SerialTerminalWindow",
    ),
    "StagePositionPanel": (
        "probe_station_gui.views.stage_position_panel",
        "StagePositionPanel",
    ),
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
