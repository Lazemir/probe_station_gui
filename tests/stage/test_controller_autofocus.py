from __future__ import annotations

from types import SimpleNamespace

import pytest

try:
    from .controller_test_support import (
        AutofocusContext,
        StageController,
        StageControllerError,
    )
except ImportError:
    from controller_test_support import (
        AutofocusContext,
        StageController,
        StageControllerError,
    )


def test_autofocus_final_z_uses_shared_precision_executor() -> None:
    controller = StageController()
    moves: list[tuple[dict[str, float], dict[str, object]]] = []
    controller._execute_precision_axis_targets_locked = (
        lambda targets, **kwargs: moves.append((dict(targets), dict(kwargs)))
    )

    controller._move_to_autofocus_final_z_locked(2.5)

    assert moves == [
        (
            {"Z": 2.5},
            {"feedrate": None, "allow_unhomed": False},
        )
    ]
    controller.shutdown()


def test_cancelled_autofocus_restores_start_z_through_precision_executor() -> None:
    controller = StageController()
    controller._cancel_event.set()
    controller._write_realtime_payload = lambda *_args: None
    controller._wait_for_idle = lambda **_kwargs: None
    controller._query_current_status_with_required_coordinates = (
        lambda **_kwargs: SimpleNamespace(display_position=(0.0, 0.0, 2.0))
    )
    controller._position_for_configured_mode = lambda status: status.display_position
    restored: list[float] = []
    controller._move_to_autofocus_final_z_locked = restored.append
    controller.status_message = SimpleNamespace(emit=lambda _message: None)
    context = AutofocusContext(
        objective_name="X20",
        start_z=3.0,
        min_z=0.0,
        max_z=10.0,
        lower_z=2.0,
        upper_z=4.0,
        local_range_mm=1.0,
        fine_step_mm=0.02,
    )

    controller._restore_autofocus_start_z_after_cancel_locked(context)

    assert restored == [3.0]
    assert controller._cancel_event.is_set()
    controller.shutdown()


def _cancelled_autofocus_context() -> AutofocusContext:
    return AutofocusContext(
        objective_name="X20",
        start_z=0.0,
        min_z=0.0,
        max_z=10.0,
        lower_z=0.0,
        upper_z=1.0,
        local_range_mm=1.0,
        fine_step_mm=0.02,
    )


def test_cancelled_autofocus_uses_validated_direct_restore_when_precision_is_impossible(
) -> None:
    controller = StageController()
    controller._cancel_event.set()
    controller._write_realtime_payload = lambda *_args: None
    controller._wait_for_idle = lambda **_kwargs: None
    controller._move_to_autofocus_final_z_locked = (
        lambda _target: (_ for _ in ()).throw(
            StageControllerError("Z precision target cannot be represented")
        )
    )
    events: list[object] = []
    controller._validate_absolute_axis_targets_move = (
        lambda targets, **_kwargs: events.append(("validated", dict(targets)))
    )
    controller._send_absolute_axis_targets_move = (
        lambda targets, **_kwargs: events.append(("sent", dict(targets)))
    )
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    controller._restore_autofocus_start_z_after_cancel_locked(
        _cancelled_autofocus_context()
    )

    assert events == [("validated", {"Z": 0.0}), ("sent", {"Z": 0.0})]
    assert controller._cancel_event.is_set()
    controller.shutdown()


def test_cancelled_autofocus_reports_direct_safe_restore_failure() -> None:
    controller = StageController()
    controller._cancel_event.set()
    controller._write_realtime_payload = lambda *_args: None
    controller._wait_for_idle = lambda **_kwargs: None
    controller._move_to_autofocus_final_z_locked = (
        lambda _target: (_ for _ in ()).throw(
            StageControllerError("Z precision target cannot be represented")
        )
    )
    controller._validate_absolute_axis_targets_move = lambda *_args, **_kwargs: None
    controller._send_absolute_axis_targets_move = (
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            StageControllerError("direct Z restore rejected")
        )
    )
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    with pytest.raises(StageControllerError, match="direct Z restore rejected"):
        controller._restore_autofocus_start_z_after_cancel_locked(
            _cancelled_autofocus_context()
        )

    assert controller._cancel_event.is_set()
    controller.shutdown()


def test_cancelled_near_limit_autofocus_restores_start_and_stops_flow() -> None:
    controller = StageController()
    context = _cancelled_autofocus_context()
    events: list[object] = []
    controller._prepare_autofocus_context_locked = lambda **_kwargs: context

    def cancel_refinement(*_args: object, **_kwargs: object) -> object:
        events.append("refinement-cancelled")
        controller._cancel_event.set()
        raise StageControllerError("Operation cancelled.")

    controller._run_static_focus_refinement_locked = cancel_refinement
    controller._write_realtime_payload = lambda *_args: None
    controller._wait_for_idle = lambda **_kwargs: None
    controller._move_to_autofocus_final_z_locked = (
        lambda _target: (_ for _ in ()).throw(
            StageControllerError("Z precision target cannot be represented")
        )
    )
    controller._validate_absolute_axis_targets_move = (
        lambda targets, **_kwargs: events.append(("validated", dict(targets)))
    )
    controller._send_absolute_axis_targets_move = (
        lambda targets, **_kwargs: events.append(("restored", dict(targets)))
    )
    controller.status_message = SimpleNamespace(emit=lambda _message: None)

    with pytest.raises(StageControllerError, match="Operation cancelled"):
        controller._run_local_autofocus_locked(range_mm=1.0)

    assert events == [
        "refinement-cancelled",
        ("validated", {"Z": 0.0}),
        ("restored", {"Z": 0.0}),
    ]
    assert controller._cancel_event.is_set()
    controller.shutdown()


def test_registration_autofocus_returns_its_token_and_physical_z_only_on_success() -> None:
    controller = StageController()
    token = object()
    results: list[tuple[object, bool, float | None, str]] = []
    controller._open_optical_session = lambda *_args, **_kwargs: _NullContext()
    controller._serial_session = lambda: _NullContext()
    controller.movement_started = SimpleNamespace(emit=lambda: None)
    controller.autofocus_finished = SimpleNamespace(emit=lambda *_args: None)
    captured: list[tuple[str, ...]] = []
    controller._current_physical_machine_coordinates_locked = lambda axes: (
        captured.append(tuple(axes)) or {"Z": 12.5}
    )

    def run_locked(**_kwargs) -> str:
        return "Focused."

    controller._run_autofocus_locked = run_locked

    controller._run_registration_autofocus(
        token,
        lambda *args: results.append(args),
    )

    assert results == [(token, True, 12.5, "Focused.")]
    assert captured == [("Z",)]
    controller.shutdown()


def test_registration_autofocus_failure_carries_no_z_reference() -> None:
    controller = StageController()
    token = object()
    results: list[tuple[object, bool, float | None, str]] = []
    controller._open_optical_session = lambda *_args, **_kwargs: _NullContext()
    controller._serial_session = lambda: _NullContext()
    controller.movement_started = SimpleNamespace(emit=lambda: None)
    controller.autofocus_finished = SimpleNamespace(emit=lambda *_args: None)
    controller._run_autofocus_locked = (
        lambda **_kwargs: (_ for _ in ()).throw(StageControllerError("focus failed"))
    )

    controller._run_registration_autofocus(
        token,
        lambda *args: results.append(args),
    )

    assert results == [(token, False, None, "focus failed")]
    controller.shutdown()


class _NullContext:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_args: object) -> None:
        return None
