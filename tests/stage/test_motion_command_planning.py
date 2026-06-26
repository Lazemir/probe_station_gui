import math

import pytest

from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.motion_command_planning import (
    absolute_axis_g1_command,
    absolute_axis_target_limit_error,
    clamped_motion_feedrate,
    ordered_absolute_axis_targets,
)


AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}


def test_ordered_absolute_axis_targets_uses_controller_axis_order() -> None:
    ordered = ordered_absolute_axis_targets(
        {"Y": "-2", "C": 0, "X": 1.25},
        axis_order=AXIS_INDEX,
    )

    assert ordered == {"X": 1.25, "Y": -2.0, "C": 0.0}


def test_ordered_absolute_axis_targets_rejects_invalid_values_and_axes() -> None:
    with pytest.raises(StageControllerError, match="Unsupported target for X: nan"):
        ordered_absolute_axis_targets({"X": math.nan}, axis_order=AXIS_INDEX)

    with pytest.raises(StageControllerError, match="Unsupported axis: Q"):
        ordered_absolute_axis_targets({"Q": object()}, axis_order=AXIS_INDEX)


def test_ordered_absolute_axis_targets_uses_exact_axis_keys() -> None:
    assert ordered_absolute_axis_targets({"x": 1.0}, axis_order=AXIS_INDEX) == {}


def test_clamped_motion_feedrate_uses_default_or_minimum() -> None:
    assert clamped_motion_feedrate(None, default_feedrate=600.0, min_feedrate=1.0) == 600.0
    assert clamped_motion_feedrate(0.25, default_feedrate=600.0, min_feedrate=1.0) == 1.0
    assert clamped_motion_feedrate(12.5, default_feedrate=600.0, min_feedrate=1.0) == 12.5


def test_absolute_axis_g1_command_formats_targets_and_feedrate() -> None:
    command = absolute_axis_g1_command({"X": 10.0, "Y": -5.0}, 123.4)

    assert command == "G1 X10.0000 Y-5.0000 F123.4"


def test_absolute_axis_target_limit_error_matches_controller_text() -> None:
    assert (
        absolute_axis_target_limit_error("Z", -0.1, (0.0, 20.0))
        == "Z target -0.100 exceeds limits (0.000, 20.000)."
    )
    assert absolute_axis_target_limit_error("Z", 10.0, (0.0, 20.0)) is None
