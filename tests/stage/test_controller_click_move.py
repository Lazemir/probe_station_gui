from __future__ import annotations

from types import SimpleNamespace

import numpy as np

from probe_station_gui.settings.precision_approach import (
    PrecisionApproachProfile,
    PrecisionApproachSettings,
)
from probe_station_gui.stage.coordinate_confidence import AxisCoordinateConfidence

try:
    from .controller_test_support import StageController, _FakeSerial
except ImportError:
    from controller_test_support import StageController, _FakeSerial


def test_click_move_resolves_pixel_delta_to_absolute_xy_target() -> None:
    controller = StageController()
    controller._pixels_to_mm = np.eye(2)
    controller._ensure_calibration = lambda **_kwargs: (False, None)
    controller._get_frame_snapshot = lambda **_kwargs: (object(), 17)
    controller._query_current_status_with_required_coordinates = lambda **_kwargs: (
        SimpleNamespace(
            display_position=(10.0, 20.0, 0.0),
        )
    )
    moves: list[tuple[dict[str, float], dict[str, object]]] = []
    controller._execute_precision_axis_targets_locked = lambda targets, **kwargs: (
        moves.append((dict(targets), dict(kwargs)))
    )
    controller._send_relative_move = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("click-to-move used a relative command")
    )
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    before_counter = controller._prepare_click_move_without_status_locked(2.0, -3.0)

    assert before_counter == 17
    assert moves == [
        (
            {"X": 8.0, "Y": 23.0},
            {"feedrate": None, "allow_unhomed": False},
        )
    ]
    controller.shutdown()


def test_click_target_after_calibration_uses_absolute_precision_target() -> None:
    controller = StageController()
    controller._pixels_to_mm = np.eye(2)
    controller._frame_counter = 8
    controller._query_current_status_with_required_coordinates = lambda **_kwargs: (
        SimpleNamespace(display_position=(9.0, 19.0, 0.0))
    )
    controller._position_for_configured_mode = lambda status: status.display_position
    controller._require_homed_axes = lambda *_args, **_kwargs: None
    moves: list[dict[str, float]] = []
    controller._execute_precision_axis_targets_locked = lambda targets, **_kwargs: (
        moves.append(dict(targets))
    )
    controller._send_relative_move = lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("calibrated click target used a relative command")
    )
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    handled, before_counter = controller._move_from_calibration_to_target(
        (10.0, 20.0, 0.0),
        np.array([2.0, -3.0]),
    )

    assert handled
    assert before_counter == 8
    assert moves == [{"X": 8.0, "Y": 23.0}]
    controller.shutdown()


def test_move_to_xy_uses_shared_precision_executor() -> None:
    controller = StageController()
    status = SimpleNamespace(
        display_position=(1.0, 2.0, 0.0),
        position=(1.0, 2.0, 0.0),
        work_position=(1.0, 2.0, 0.0),
        homed_axes={"X", "Y"},
    )
    controller._query_synced_status_for_absolute_motion = lambda **_kwargs: status
    controller._require_homed_axes = lambda *_args, **_kwargs: None
    controller._require_position_for_absolute_motion = lambda *_args, **_kwargs: (
        status.display_position
    )
    controller._status_matches_axis_targets = lambda *_args, **_kwargs: False
    moves: list[tuple[dict[str, float], dict[str, object]]] = []
    controller._execute_precision_axis_targets_locked = lambda targets, **kwargs: (
        moves.append((dict(targets), dict(kwargs)))
    )
    controller.absolute_xy_move_started = SimpleNamespace(emit=lambda *_args: None)
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    message = controller._move_to_xy_locked(4.0, 5.0, feedrate=12.0)

    assert moves == [
        (
            {"X": 4.0, "Y": 5.0},
            {"feedrate": 12.0, "allow_unhomed": False},
        )
    ]
    assert "Arrived" in message
    controller.shutdown()


def test_request_move_to_xy_reports_rejected_background_start() -> None:
    controller = StageController()
    submitted: dict[str, object] = {}

    def reject_start(**kwargs: object) -> bool:
        submitted.update(kwargs)
        return False

    controller._start_background_task = reject_start

    try:
        accepted = controller.request_move_to_xy(4.0, 5.0)

        assert accepted is False
        assert submitted["target"] == controller._run_move_to_xy
        assert submitted["args"] == (4.0, 5.0)
    finally:
        controller.shutdown()


def test_token_bound_xy_move_reports_only_its_own_target_and_result() -> None:
    controller = StageController()
    events: list[object] = []
    token = object()
    controller.movement_started = SimpleNamespace(emit=lambda: events.append("started"))
    controller.movement_finished = SimpleNamespace(
        emit=lambda *_args: events.append("global-finished")
    )
    controller._serial_session = lambda: _NullContext()
    controller._move_safety_check = lambda: None
    controller._move_to_xy_locked = lambda x, y: f"arrived:{x}:{y}"

    controller._run_token_bound_move_to_xy(
        token,
        4.0,
        5.0,
        lambda *args: events.append(args),
    )

    assert events == ["started", (token, (4.0, 5.0), True, "arrived:4.0:5.0")]
    controller.shutdown()


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None


def test_external_route_move_at_target_executes_approximate_precision_axis() -> None:
    controller = StageController()
    controller._serial = _FakeSerial()
    settings = PrecisionApproachSettings()
    settings.profiles["X"] = PrecisionApproachProfile(True, 0.1, 1)
    controller.apply_precision_approach_configuration(settings)
    status = SimpleNamespace(
        display_position=(1.0, 2.0, 0.0),
        position=(1.0, 2.0, 0.0),
        work_position=(1.0, 2.0, 0.0),
        homed_axes={"X", "Y"},
    )
    controller._query_synced_status_for_absolute_motion = lambda **_kwargs: status
    controller._require_homed_axes = lambda *_args, **_kwargs: None
    controller._require_position_for_absolute_motion = lambda *_args, **_kwargs: (
        status.display_position
    )
    controller._status_matches_axis_targets = lambda *_args, **_kwargs: True
    moves: list[dict[str, float]] = []
    controller._execute_precision_axis_targets_locked = lambda targets, **_kwargs: (
        moves.append(dict(targets))
    )
    controller.absolute_xy_move_started = SimpleNamespace(emit=lambda *_args: None)
    controller.movement_started = SimpleNamespace(emit=lambda: None)
    controller.movement_finished = SimpleNamespace(emit=lambda *_args: None)
    controller._move_safety_check = lambda: None
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    message = controller.run_external_move_to_xy(1.0, 2.0)

    assert moves == [{"X": 1.0, "Y": 2.0}]
    assert "Arrived" in message
    controller.shutdown()


def test_saved_xyz_uses_precision_for_xy_and_final_z_but_not_safe_transit() -> None:
    controller = StageController()
    status = SimpleNamespace(
        display_position=(1.0, 2.0, 3.0),
        position=(1.0, 2.0, 3.0),
        work_position=(1.0, 2.0, 3.0),
        homed_axes={"X", "Y", "Z"},
    )
    controller._query_synced_status_for_absolute_motion = lambda **_kwargs: status
    controller._require_homed_axes = lambda *_args, **_kwargs: None
    controller._require_position_for_absolute_motion = lambda *_args, **_kwargs: (
        status.display_position
    )
    transit: list[dict[str, float]] = []
    precise: list[dict[str, float]] = []
    controller._send_absolute_axis_targets_move = lambda targets, **_kwargs: (
        transit.append(dict(targets))
    )
    controller._execute_precision_axis_targets_locked = lambda targets, **_kwargs: (
        precise.append(dict(targets))
    )
    controller._wait_for_idle = lambda *_args, **_kwargs: None
    controller._query_current_status = lambda: None
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    controller._move_to_xyz_locked(
        4.0,
        5.0,
        2.0,
        transit_z_mm=6.0,
        label="saved position",
    )

    assert transit == [{"Z": 6.0}]
    assert precise == [{"X": 4.0, "Y": 5.0}, {"Z": 2.0}]
    controller.shutdown()


def test_saved_xyz_without_separate_transit_still_finishes_z_precisely() -> None:
    controller = StageController()
    controller.apply_precision_approach_configuration(PrecisionApproachSettings())
    status = SimpleNamespace(
        display_position=(1.0, 2.0, 3.0),
        position=(1.0, 2.0, 3.0),
        work_position=(1.0, 2.0, 3.0),
        homed_axes={"X", "Y", "Z"},
    )
    controller._query_synced_status_for_absolute_motion = lambda **_kwargs: status
    controller._require_homed_axes = lambda *_args, **_kwargs: None
    controller._require_position_for_absolute_motion = lambda *_args, **_kwargs: (
        status.display_position
    )
    transit: list[dict[str, float]] = []
    precise: list[dict[str, float]] = []
    controller._send_absolute_axis_targets_move = lambda targets, **_kwargs: (
        transit.append(dict(targets))
    )
    controller._execute_precision_axis_targets_locked = lambda targets, **_kwargs: (
        precise.append(dict(targets))
    )
    controller._wait_for_idle = lambda *_args, **_kwargs: None
    controller._query_current_status = lambda: None
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    controller._move_to_xyz_locked(
        4.0,
        5.0,
        2.0,
        transit_z_mm=None,
        label="saved position",
    )

    assert transit == [{"Z": 2.0}]
    assert precise == [{"X": 4.0, "Y": 5.0}, {"Z": 2.0}]
    controller.shutdown()


def test_saved_xyz_at_target_executes_approximate_xy_precision() -> None:
    controller = StageController()
    settings = PrecisionApproachSettings()
    settings.profiles["X"] = PrecisionApproachProfile(True, 0.1, 1)
    settings.profiles["Z"] = PrecisionApproachProfile()
    controller.apply_precision_approach_configuration(settings)
    status = SimpleNamespace(
        display_position=(1.0, 2.0, 3.0),
        position=(1.0, 2.0, 3.0),
        work_position=(1.0, 2.0, 3.0),
        homed_axes={"X", "Y", "Z"},
    )
    controller._query_synced_status_for_absolute_motion = lambda **_kwargs: status
    controller._require_homed_axes = lambda *_args, **_kwargs: None
    controller._require_position_for_absolute_motion = lambda *_args, **_kwargs: (
        status.display_position
    )
    precise: list[dict[str, float]] = []
    controller._execute_precision_axis_targets_locked = lambda targets, **_kwargs: (
        precise.append(dict(targets))
    )
    controller._wait_for_idle = lambda *_args, **_kwargs: None
    controller._query_current_status = lambda: None
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    controller._move_to_xyz_locked(
        1.0,
        2.0,
        3.0,
        transit_z_mm=None,
        label="saved position",
    )

    assert precise == [{"X": 1.0, "Y": 2.0}]
    controller.shutdown()


def test_saved_xyz_zero_backlash_profile_preserves_at_target_noop() -> None:
    controller = StageController()
    settings = PrecisionApproachSettings()
    settings.profiles["Z"] = PrecisionApproachProfile(True, 0.0, 1)
    controller.apply_precision_approach_configuration(settings)
    status = SimpleNamespace(
        display_position=(1.0, 2.0, 3.0),
        position=(1.0, 2.0, 3.0),
        work_position=(1.0, 2.0, 3.0),
        homed_axes={"X", "Y", "Z"},
    )
    controller._query_synced_status_for_absolute_motion = lambda **_kwargs: status
    controller._require_homed_axes = lambda *_args, **_kwargs: None
    controller._require_position_for_absolute_motion = lambda *_args, **_kwargs: (
        status.display_position
    )
    precise: list[dict[str, float]] = []
    controller._execute_precision_axis_targets_locked = lambda targets, **_kwargs: (
        precise.append(dict(targets))
    )
    controller._wait_for_idle = lambda *_args, **_kwargs: None
    controller._query_current_status = lambda: None
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    message = controller._move_to_xyz_locked(
        1.0,
        2.0,
        3.0,
        transit_z_mm=None,
        label="saved position",
    )

    assert precise == []
    assert "already reached" in message
    controller.shutdown()


def test_saved_xyz_exact_precision_z_preserves_at_target_noop() -> None:
    controller = StageController()
    settings = PrecisionApproachSettings()
    settings.profiles["Z"] = PrecisionApproachProfile(True, 0.03, 1)
    controller.apply_precision_approach_configuration(settings)
    controller._coordinate_confidence["Z"] = (
        AxisCoordinateConfidence().after_precision_final(
            coordinate=3.0,
            final_direction=1,
        )
    )
    status = SimpleNamespace(
        display_position=(1.0, 2.0, 3.0),
        position=(1.0, 2.0, 3.0),
        work_position=(1.0, 2.0, 3.0),
        homed_axes={"X", "Y", "Z"},
    )
    controller._query_synced_status_for_absolute_motion = lambda **_kwargs: status
    controller._require_homed_axes = lambda *_args, **_kwargs: None
    controller._require_position_for_absolute_motion = lambda *_args, **_kwargs: (
        status.display_position
    )
    precise: list[dict[str, float]] = []
    controller._execute_precision_axis_targets_locked = lambda targets, **_kwargs: (
        precise.append(dict(targets))
    )
    controller._wait_for_idle = lambda *_args, **_kwargs: None
    controller._query_current_status = lambda: None
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    message = controller._move_to_xyz_locked(
        1.0,
        2.0,
        3.0,
        transit_z_mm=None,
        label="saved position",
    )

    assert precise == []
    assert "already reached" in message
    controller.shutdown()
