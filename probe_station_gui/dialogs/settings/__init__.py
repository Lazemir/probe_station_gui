"""Settings dialog widget modules."""

from probe_station_gui.dialogs.settings.controls import (
    ControlsSettingsWidget,
    KeyBindingListEditor,
    KeyCaptureDialog,
)
from probe_station_gui.dialogs.settings.feedrates import (
    FeedrateGroupEditor,
    FeedrateSettingsWidget,
)
from probe_station_gui.dialogs.settings.jog import JogSettingsWidget

__all__ = [
    "ControlsSettingsWidget",
    "FeedrateGroupEditor",
    "FeedrateSettingsWidget",
    "JogSettingsWidget",
    "KeyBindingListEditor",
    "KeyCaptureDialog",
]
