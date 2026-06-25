"""Compatibility wrapper for :mod:`probe_station_gui.stage.controller`."""

from probe_station_gui.stage.controller import *  # noqa: F401,F403
from probe_station_gui.stage.fluidnc_protocol import (  # noqa: F401
    parse_fluidnc_axis_max_feedrates,
)
