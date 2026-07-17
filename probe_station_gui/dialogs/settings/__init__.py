"""Settings dialog widget modules."""

from probe_station_gui.dialogs.settings.controls import (
    ControlsSettingsWidget,
    KeyBindingListEditor,
    KeyCaptureDialog,
)
from probe_station_gui.dialogs.settings.coordinate_system import (
    CoordinateSystemSettingsWidget,
)
from probe_station_gui.dialogs.settings.feedrates import (
    FeedrateGroupEditor,
    FeedrateSettingsWidget,
)
from probe_station_gui.dialogs.settings.jog import JogSettingsWidget
from probe_station_gui.dialogs.settings.measurement import MeasurementSettingsWidget
from probe_station_gui.dialogs.settings.objectives import ObjectivesSettingsWidget
from probe_station_gui.dialogs.settings.precision_approach import (
    PrecisionApproachSettingsWidget,
)

__all__ = [
    "ControlsSettingsWidget",
    "CoordinateSystemSettingsWidget",
    "FeedrateGroupEditor",
    "FeedrateSettingsWidget",
    "JogSettingsWidget",
    "KeyBindingListEditor",
    "KeyCaptureDialog",
    "MeasurementSettingsWidget",
    "ObjectivesSettingsWidget",
    "PrecisionApproachSettingsWidget",
]
