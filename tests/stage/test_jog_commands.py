from probe_station_gui.stage.jog_commands import (
    absolute_axis_targets_jog_command,
    format_gcode_value,
    jog_command_feedrate,
    move_vector_from_axis_distances,
    move_vector_from_jog_command,
    relative_jog_command_to_absolute,
)
from probe_station_gui.stage.types import MoveVector


AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}


def test_format_gcode_value_trims_trailing_zeroes() -> None:
    assert format_gcode_value(10.0) == "10"
    assert format_gcode_value(0.125, decimals=4) == "0.125"
    assert format_gcode_value(0.0) == "0"


def test_move_vector_from_axis_distances_normalizes_and_sums_axes() -> None:
    move = move_vector_from_axis_distances(
        (("z", 1.5), ("Z", -0.25), ("unknown", 99.0)),
        axis_index=AXIS_INDEX,
    )

    assert move == MoveVector(z=1.25)


def test_move_vector_from_jog_command_parses_axis_words() -> None:
    move = move_vector_from_jog_command(
        "$J=G91 G21 X1.5 y-2.25 F10",
        axis_index=AXIS_INDEX,
    )

    assert move == MoveVector(x=1.5, y=-2.25)
    assert move_vector_from_jog_command("G1 X1", axis_index=AXIS_INDEX) is None


def test_jog_command_feedrate_uses_last_finite_feedrate_with_minimum() -> None:
    assert jog_command_feedrate("$J=G91 X1 F0.1", min_feedrate=1.0) == 1.0
    assert jog_command_feedrate("$J=G91 X1 F0.1 F12", min_feedrate=1.0) == 12.0
    assert jog_command_feedrate("$J=G91 X1", min_feedrate=1.0) is None


def test_absolute_axis_targets_jog_command_orders_axes_and_uses_machine_mode() -> None:
    command = absolute_axis_targets_jog_command(
        {"Z": 6.0, "X": 1.2500004},
        10.0,
        axis_order=AXIS_INDEX,
        machine_position_mode=True,
    )

    assert command == "$J=G90 G21 G53 X1.25 Z6 F10"


def test_absolute_axis_targets_jog_command_preserves_micron_precision() -> None:
    command = absolute_axis_targets_jog_command(
        {"X": 1.2345674},
        10.0,
        axis_order=AXIS_INDEX,
        machine_position_mode=False,
    )

    assert command == "$J=G90 G21 X1.234567 F10"


def test_relative_jog_command_to_absolute_uses_cached_position() -> None:
    command = relative_jog_command_to_absolute(
        "$J=G91 G21 Z5.000 F10",
        MoveVector(z=5.0),
        position=(0.0, 0.0, 1.0),
        axis_index=AXIS_INDEX,
        min_feedrate=1.0,
        machine_position_mode=True,
        axis_skip_reason=lambda _axis, _position: None,
    )

    assert command == "$J=G90 G21 G53 Z6 F10"


def test_relative_jog_command_to_absolute_preserves_command_when_axis_is_skipped() -> None:
    original = "$J=G91 G21 Z5.000 F10"

    command = relative_jog_command_to_absolute(
        original,
        MoveVector(z=5.0),
        position=(0.0, 0.0, 1.0),
        axis_index=AXIS_INDEX,
        min_feedrate=1.0,
        machine_position_mode=True,
        axis_skip_reason=lambda _axis, _position: "unknown limit",
    )

    assert command == original
