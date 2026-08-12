from __future__ import annotations

import inspect

from probe_station_gui.stage.axis_coordinates import (
    StageControllerAxisCoordinatesMixin,
)
from probe_station_gui.stage.connection_state import StageControllerConnectionMixin
from probe_station_gui.stage.controller import StageController
from probe_station_gui.stage.status_io import StageControllerStatusIOMixin


AXIS_COORDINATE_METHODS = {
    "apply_coordinate_system_configuration",
    "active_coordinate_system",
    "coordinate_display_name",
    "axis_display_limits",
    "set_current_axis_work_coordinate",
    "_set_current_axis_work_coordinate_locked",
}

STATUS_IO_METHODS = {
    "_poll_status_once",
    "_run_status_refresh",
    "current_stage_position",
    "run_external_current_stage_position",
    "run_external_current_physical_machine_coordinates",
    "request_machine_coordinate_snapshot",
    "_run_machine_coordinate_snapshot_request",
    "_current_physical_machine_coordinates_locked",
    "_parse_raw_machine_status_line",
    "_physical_machine_coordinates_from_status",
    "_query_current_stage_position_status",
    "_stage_position_from_status",
    "request_status_refresh",
    "_query_synced_status_for_absolute_motion",
}

CACHED_OBSERVATION_METHODS = {
    "latest_stage_position",
    "latest_machine_position",
    "latest_synchronized_machine_position",
    "latest_machine_coordinate_snapshot",
    "latest_motion_coordinate_snapshot",
    "axis_calibration_preview_position",
    "latest_a_position",
    "latest_axis_a_lowering",
    "latest_stage_state",
    "last_status_timestamp",
    "last_jog_write_timestamp",
    "_update_cached_positions",
}


def _assert_direct_owner(owner: type, method_names: set[str]) -> None:
    for name in method_names:
        assert name not in StageController.__dict__
        assert getattr(StageController, name) is owner.__dict__[name]


def test_stage_controller_keeps_exact_existing_direct_bases() -> None:
    assert tuple(base.__name__ for base in StageController.__bases__) == (
        "StageControllerConnectionMixin",
        "StageControllerHomingStartupMixin",
        "StageControllerSerialWriteQueueMixin",
        "StageControllerStatusIOMixin",
        "StageControllerFluidNCConfigIOMixin",
        "StageControllerNeedleStatusMixin",
        "StageControllerNeedleActionsMixin",
        "StageControllerJogQueueMixin",
        "StageControllerMotionCommandsMixin",
        "StageControllerAxisCoordinatesMixin",
        "StageControllerPrecisionMotionMixin",
        "StageControllerSafetyStateMixin",
        "StageControllerClickMoveMixin",
        "StageControllerAutofocusMixin",
        "QObject",
    )


def test_axis_coordinate_owner_is_direct_and_canonical() -> None:
    _assert_direct_owner(StageControllerAxisCoordinatesMixin, AXIS_COORDINATE_METHODS)


def test_status_io_owner_is_direct_and_canonical() -> None:
    _assert_direct_owner(StageControllerStatusIOMixin, STATUS_IO_METHODS)


def test_cached_observation_owner_is_direct_and_canonical() -> None:
    _assert_direct_owner(
        StageControllerConnectionMixin,
        CACHED_OBSERVATION_METHODS,
    )


def test_moved_public_method_signatures_are_stable() -> None:
    expected = {
        "active_coordinate_system": "(self) -> 'str | None'",
        "coordinate_display_name": "(self) -> 'str'",
        "axis_display_limits": "(self, axis: 'str') -> 'tuple[float, float] | None'",
        "set_current_axis_work_coordinate": (
            "(self, axis: 'str', value: 'float' = 0.0) -> 'None'"
        ),
        "current_stage_position": "(self) -> 'tuple[float, ...]'",
        "run_external_current_stage_position": "(self) -> 'tuple[float, ...]'",
        "latest_stage_position": "(self) -> 'tuple[float, ...] | None'",
        "latest_a_position": "(self) -> 'float | None'",
        "latest_axis_a_lowering": "(self) -> 'float | None'",
        "latest_stage_state": "(self) -> 'str | None'",
        "last_status_timestamp": "(self) -> 'float | None'",
        "last_jog_write_timestamp": "(self) -> 'float | None'",
        "request_status_refresh": "(self) -> 'None'",
    }
    assert {
        name: str(inspect.signature(getattr(StageController, name)))
        for name in expected
    } == expected
