from __future__ import annotations

from types import SimpleNamespace

try:
    from .controller_test_support import AutofocusContext, StageController
except ImportError:
    from controller_test_support import AutofocusContext, StageController


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
