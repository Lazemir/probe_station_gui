import tempfile
import types
from pathlib import Path

from tests.app.main_coordinate_feedrate_support import (
    Main,
    RouteMeasurementRunner,
    _make_main,
)


def _ordinary_runner(tmpdir: str, stage_controller: object) -> RouteMeasurementRunner:
    return RouteMeasurementRunner(
        points=[],
        csv_path=Path(tmpdir) / "route.csv",
        stage_controller=stage_controller,
        lcr_controller=types.SimpleNamespace(),
        needle_feedrate=None,
    )


def test_waiting_ordinary_route_interrupt_does_not_cancel_stage() -> None:
    window, stage_controller, _joystick, _timer, _statuses = _make_main()
    refreshes: list[tuple[int, ...]] = []
    window._route_measurement_waiting = True
    window._stage_motion_axes = {"X"}
    window._schedule_status_refreshes = lambda delays: refreshes.append(tuple(delays))

    with tempfile.TemporaryDirectory() as tmpdir:
        runner = _ordinary_runner(tmpdir, stage_controller)
        runner._run_control.set_waiting(True)

        Main._interrupt_route_measurement_runner(
            window,
            runner,
            reason="Route measurement interrupt requested.",
        )

    assert runner.current_point_correction_requested() is True
    assert stage_controller.cancelled_tasks == []
    assert window._stage_motion_axes == {"X"}
    assert refreshes == []


def test_active_ordinary_route_interrupt_cancels_stage() -> None:
    window, stage_controller, _joystick, _timer, _statuses = _make_main()
    refreshes: list[tuple[int, ...]] = []
    window._route_measurement_waiting = False
    window._stage_motion_axes = {"X"}
    window._schedule_status_refreshes = lambda delays: refreshes.append(tuple(delays))

    with tempfile.TemporaryDirectory() as tmpdir:
        runner = _ordinary_runner(tmpdir, stage_controller)

        Main._interrupt_route_measurement_runner(
            window,
            runner,
            reason="Route measurement interrupt requested.",
        )

    assert runner.current_point_correction_requested() is True
    assert stage_controller.cancelled_tasks == [
        "Route measurement interrupt requested."
    ]
    assert window._stage_motion_axes == set()
    assert refreshes == [tuple(Main.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)]


def test_external_waiting_payload_prevents_stage_cancel() -> None:
    window, stage_controller, _joystick, _timer, _statuses = _make_main()
    corrections: list[bool] = []
    refreshes: list[tuple[int, ...]] = []
    window._route_measurement_waiting = False
    window._stage_motion_axes = {"X"}
    window._schedule_status_refreshes = lambda delays: refreshes.append(tuple(delays))
    runner = types.SimpleNamespace(
        status_payload=lambda: {
            "state": "waiting_external_measurement",
            "waiting": True,
            "waiting_reason": "external_measurement",
        },
        request_current_point_correction=lambda: corrections.append(True),
    )

    Main._interrupt_route_measurement_runner(
        window,
        runner,
        reason="Route measurement interrupt requested.",
    )

    assert corrections == [True]
    assert stage_controller.cancelled_tasks == []
    assert window._stage_motion_axes == {"X"}
    assert refreshes == []
