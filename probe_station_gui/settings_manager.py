"""Compatibility wrapper for :mod:`probe_station_gui.settings.manager`."""

from probe_station_gui.settings.manager import *  # noqa: F401,F403
from probe_station_gui.settings.objective_config import (  # noqa: F401
    OBJECTIVE_DEFAULTS,
    default_objectives,
)
from probe_station_gui.stage.fluidnc_protocol import FLUIDNC_AXIS_NAMES  # noqa: F401
