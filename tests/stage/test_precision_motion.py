from __future__ import annotations

from types import SimpleNamespace

import pytest

try:
    from .controller_test_support import StageController, StageControllerError, _FakeSerial
except ImportError:
    from controller_test_support import StageController, StageControllerError, _FakeSerial

from probe_station_gui.settings.precision_approach import (
    PrecisionApproachProfile,
    PrecisionApproachSettings,
)
from probe_station_gui.stage.coordinate_confidence import AxisCoordinateConfidence


def _settings(**profiles: PrecisionApproachProfile) -> PrecisionApproachSettings:
    settings = PrecisionApproachSettings()
    for axis in settings.profiles:
        settings.profiles[axis] = PrecisionApproachProfile()
    settings.profiles.update(profiles)
    return settings


def _configure_fake_precision_runtime(
    controller: StageController,
    current: dict[str, float],
) -> tuple[list[dict[str, float]], list[str]]:
    controller._serial = _FakeSerial()
    sent: list[dict[str, float]] = []
    safety: list[str] = []

    def status_for_current(**_kwargs: object) -> SimpleNamespace:
        values = tuple(current.get(axis, 0.0) for axis in controller.AXIS_INDEX)
        return SimpleNamespace(
            state="Idle",
            position=values,
            display_position=values,
            work_position=values,
            work_offset=tuple(0.0 for _axis in controller.AXIS_INDEX),
            coordinate_system="G54",
            homed_axes=set(controller.AXIS_INDEX),
            values=dict(current),
        )

    def send(targets: dict[str, float], **_kwargs: object) -> None:
        sent.append(dict(targets))
        current.update(targets)

    controller._query_current_status_with_required_coordinates = status_for_current
    controller._axis_value_for_configured_mode = (
        lambda status, axis: status.values.get(axis)
    )
    controller._validate_absolute_axis_targets_move = lambda *_args, **_kwargs: None
    controller._send_absolute_axis_targets_move = send
    controller._move_safety_check = lambda: safety.append("checked")
    return sent, safety


def test_coordinate_task_executes_preparation_and_final_as_one_operation() -> None:
    controller = StageController()
    controller.apply_precision_approach_configuration(
        _settings(Z=PrecisionApproachProfile(True, 0.03, 1))
    )
    sent, safety = _configure_fake_precision_runtime(controller, {"Z": 1.0})
    events: list[object] = []
    controller.movement_started = SimpleNamespace(
        emit=lambda: events.append("started")
    )
    controller.movement_finished = SimpleNamespace(
        emit=lambda success, message: events.append((success, message))
    )
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    controller._run_absolute_axis_targets_move({"Z": 1.01}, 20.0)

    assert sent == [{"Z": pytest.approx(0.98)}, {"Z": pytest.approx(1.01)}]
    assert safety == ["checked", "checked"]
    assert events[0] == "started"
    assert len([event for event in events if event == "started"]) == 1
    assert events[-1][0] is True
    assert controller.coordinate_confidence()["Z"].exact
    controller.shutdown()


def test_cancellation_between_segments_prevents_final_and_invalidates_axis() -> None:
    controller = StageController()
    controller.apply_precision_approach_configuration(
        _settings(Z=PrecisionApproachProfile(True, 0.03, 1))
    )
    sent, _safety = _configure_fake_precision_runtime(controller, {"Z": 1.0})
    original_send = controller._send_absolute_axis_targets_move

    def cancel_after_send(targets: dict[str, float], **kwargs: object) -> None:
        original_send(targets, **kwargs)
        controller._cancel_event.set()

    controller._send_absolute_axis_targets_move = cancel_after_send
    events: list[tuple[bool, str]] = []
    controller.movement_started = SimpleNamespace(emit=lambda: None)
    controller.movement_finished = SimpleNamespace(
        emit=lambda success, message: events.append((success, message))
    )
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    controller._run_absolute_axis_targets_move({"Z": 1.01}, 20.0)

    assert sent == [{"Z": pytest.approx(0.98)}]
    assert events[-1][0] is False
    assert "cancelled" in events[-1][1].lower()
    assert not controller.coordinate_confidence()["Z"].exact
    controller.shutdown()


def test_invalid_preparation_is_rejected_before_any_segment_is_sent() -> None:
    controller = StageController()
    controller.apply_precision_approach_configuration(
        _settings(Z=PrecisionApproachProfile(True, 0.03, 1))
    )
    sent, _safety = _configure_fake_precision_runtime(controller, {"Z": 0.01})

    def validate(targets: dict[str, float], **_kwargs: object) -> None:
        if targets["Z"] < 0.0:
            raise StageControllerError("Z preparation is outside limits")

    controller._validate_absolute_axis_targets_move = validate

    with controller._serial_session():
        with pytest.raises(StageControllerError, match="outside limits"):
            controller._execute_precision_axis_targets_locked(
                {"Z": 0.01},
                feedrate=20.0,
                allow_unhomed=False,
            )

    assert sent == []
    controller.shutdown()


def test_send_failure_invalidates_previously_exact_target_axis() -> None:
    controller = StageController()
    controller.apply_precision_approach_configuration(
        _settings(Z=PrecisionApproachProfile(True, 0.03, 1))
    )
    _sent, _safety = _configure_fake_precision_runtime(controller, {"Z": 1.0})
    controller._coordinate_confidence["Z"] = AxisCoordinateConfidence().after_precision_final(
        coordinate=1.0,
        final_direction=1,
    )
    controller._send_absolute_axis_targets_move = (
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            StageControllerError("controller rejected move")
        )
    )

    with controller._serial_session():
        with pytest.raises(StageControllerError, match="rejected"):
            controller._execute_precision_axis_targets_locked(
                {"Z": 1.01},
                feedrate=20.0,
                allow_unhomed=False,
            )

    assert not controller.coordinate_confidence()["Z"].exact
    controller.shutdown()


def test_disabled_profiles_keep_existing_single_segment_path() -> None:
    controller = StageController()
    controller.apply_precision_approach_configuration(_settings())
    sent, safety = _configure_fake_precision_runtime(controller, {"X": 1.0})

    with controller._serial_session():
        controller._execute_precision_axis_targets_locked(
            {"X": 2.0},
            feedrate=20.0,
            allow_unhomed=False,
        )

    assert sent == [{"X": 2.0}]
    assert safety == ["checked"]
    controller.shutdown()


def test_cached_exact_confidence_restores_only_matching_live_axes() -> None:
    profiles = _settings(
        X=PrecisionApproachProfile(True, 0.1, 1),
        Z=PrecisionApproachProfile(True, 0.03, 1),
    )
    source = StageController()
    source.apply_precision_approach_configuration(profiles)
    source._controller_session_marker = 400
    source._last_stage_position = (1.0, 0.0, 3.0, 0.0, 0.0, 0.0)
    source._last_machine_position = (1.0, 0.0, 3.0, 0.0, 0.0, 0.0)
    source._coordinate_confidence["X"] = AxisCoordinateConfidence().after_precision_final(
        coordinate=1.0,
        final_direction=1,
    )
    source._coordinate_confidence["Z"] = AxisCoordinateConfidence().after_precision_final(
        coordinate=3.0,
        final_direction=1,
    )
    cached = source.export_cached_controller_state()
    assert cached is not None

    restored = StageController()
    restored.apply_precision_approach_configuration(profiles)
    restored.import_cached_controller_state(cached)
    restored.stage_position_changed = SimpleNamespace(emit=lambda _position: None)
    restored._update_cached_positions(
        SimpleNamespace(
            state="Idle",
            position=(1.01, 0.0, 3.0, 0.0, 0.0, 0.0),
            display_position=(1.01, 0.0, 3.0, 0.0, 0.0, 0.0),
            coordinate_system=None,
            work_offset=None,
        )
    )

    assert not restored.coordinate_confidence()["X"].exact
    assert restored.coordinate_confidence()["Z"].exact
    source.shutdown()
    restored.shutdown()


def test_profile_fingerprint_mismatch_prevents_exact_restore() -> None:
    source = StageController()
    source.apply_precision_approach_configuration(
        _settings(Z=PrecisionApproachProfile(True, 0.03, 1))
    )
    source._last_stage_position = (0.0, 0.0, 3.0, 0.0, 0.0, 0.0)
    source._last_machine_position = (0.0, 0.0, 3.0, 0.0, 0.0, 0.0)
    source._coordinate_confidence["Z"] = AxisCoordinateConfidence().after_precision_final(
        coordinate=3.0,
        final_direction=1,
    )
    cached = source.export_cached_controller_state()
    assert cached is not None

    restored = StageController()
    restored.apply_precision_approach_configuration(
        _settings(Z=PrecisionApproachProfile(True, 0.05, 1))
    )
    restored.import_cached_controller_state(cached)
    restored.stage_position_changed = SimpleNamespace(emit=lambda _position: None)
    restored._update_cached_positions(
        SimpleNamespace(
            state="Idle",
            position=(0.0, 0.0, 3.0, 0.0, 0.0, 0.0),
            display_position=(0.0, 0.0, 3.0, 0.0, 0.0, 0.0),
            coordinate_system=None,
            work_offset=None,
        )
    )

    assert not restored.coordinate_confidence()["Z"].exact
    source.shutdown()
    restored.shutdown()


def test_live_status_reversal_invalidates_exact_axis_confidence() -> None:
    controller = StageController()
    controller.apply_precision_approach_configuration(
        _settings(Z=PrecisionApproachProfile(True, 0.03, 1))
    )
    controller._coordinate_confidence["Z"] = AxisCoordinateConfidence().after_precision_final(
        coordinate=3.0,
        final_direction=1,
    )
    controller.stage_position_changed = SimpleNamespace(emit=lambda _position: None)

    controller._update_cached_positions(
        SimpleNamespace(
            state="Idle",
            position=(0.0, 0.0, 2.99, 0.0, 0.0, 0.0),
            display_position=(0.0, 0.0, 2.99, 0.0, 0.0, 0.0),
            coordinate_system=None,
            work_offset=None,
        )
    )

    assert not controller.coordinate_confidence()["Z"].exact
    assert controller.coordinate_confidence()["Z"].loaded_direction == -1
    controller.shutdown()


def test_homing_axis_invalidates_its_exact_confidence() -> None:
    controller = StageController()
    controller.apply_precision_approach_configuration(
        _settings(Z=PrecisionApproachProfile(True, 0.03, 1))
    )
    controller._coordinate_confidence["Z"] = AxisCoordinateConfidence().after_precision_final(
        coordinate=3.0,
        final_direction=1,
    )
    controller._serial = _FakeSerial()
    controller._perform_home_command = lambda _command: None
    controller.movement_started = SimpleNamespace(emit=lambda: None)
    controller.movement_finished = SimpleNamespace(emit=lambda *_args: None)
    controller.homing_action_finished = SimpleNamespace(emit=lambda *_args: None)

    controller._run_home("$HZ", "Z")

    assert not controller.coordinate_confidence()["Z"].exact
    controller.shutdown()


def test_requested_controller_reset_invalidates_all_enabled_axes() -> None:
    controller = StageController()
    controller.apply_precision_approach_configuration(
        _settings(
            X=PrecisionApproachProfile(True, 0.1, 1),
            Z=PrecisionApproachProfile(True, 0.03, 1),
        )
    )
    for axis in ("X", "Z"):
        controller._coordinate_confidence[axis] = (
            AxisCoordinateConfidence().after_precision_final(
                coordinate=1.0,
                final_direction=1,
            )
        )
    controller.status_message = SimpleNamespace(emit=lambda _message: None)
    controller.queue_soft_reset = lambda **_kwargs: None

    controller.reset_controller()

    assert not controller.coordinate_confidence()["X"].exact
    assert not controller.coordinate_confidence()["Z"].exact
    controller.shutdown()


def test_b_rotation_resolves_relative_angle_to_precision_absolute_target() -> None:
    controller = StageController()
    controller._serial = _FakeSerial()
    controller._query_current_status_with_required_coordinates = (
        lambda **_kwargs: SimpleNamespace(values={"B": 5.0})
    )
    controller._axis_value_for_configured_mode = (
        lambda status, axis: status.values.get(axis)
    )
    moves: list[tuple[dict[str, float], dict[str, object]]] = []
    controller._execute_precision_axis_targets_locked = (
        lambda targets, **kwargs: moves.append((dict(targets), dict(kwargs)))
    )
    controller.movement_started = SimpleNamespace(emit=lambda: None)
    controller.movement_finished = SimpleNamespace(emit=lambda *_args: None)
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    controller._run_rotate_b(2.5)

    assert moves == [
        (
            {"B": 7.5},
            {"feedrate": None, "allow_unhomed": True},
        )
    ]
    controller.shutdown()
