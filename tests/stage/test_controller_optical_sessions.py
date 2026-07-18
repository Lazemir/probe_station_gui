from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

from probe_station_gui.camera.exposure_policy import ExposurePolicyError

try:
    from .controller_test_support import (
        AutofocusContext,
        StageController,
        StageControllerError,
        _FakeSerial,
        _WritableFakeSerial,
    )
except ImportError:
    from controller_test_support import (
        AutofocusContext,
        StageController,
        StageControllerError,
        _FakeSerial,
        _WritableFakeSerial,
    )


class _FakeLease:
    def __init__(
        self,
        events: list[object],
        *,
        close_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.close_error = close_error
        self.token = "session-token"

    def __enter__(self):
        self.events.append("exposure ready")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> bool:
        self.events.append("close")
        if self.close_error is not None:
            raise self.close_error
        return False


class _FakeSessionManager:
    def __init__(
        self,
        events: list[object],
        *,
        open_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.open_error = open_error
        self.close_error = close_error

    def open(self, operation: str, parent_token: str | None = None) -> _FakeLease:
        self.events.append(("open", operation, parent_token))
        if self.open_error is not None:
            raise self.open_error
        return _FakeLease(self.events, close_error=self.close_error)


@pytest.fixture
def controller():
    instance = StageController()
    try:
        yield instance
    finally:
        instance.shutdown()


def _install_result_signals(controller, events: list[object]) -> None:
    controller.status_message = SimpleNamespace(emit=lambda *_args: None)
    controller.movement_started = SimpleNamespace(
        emit=lambda: events.append("movement started")
    )
    controller.autofocus_finished = SimpleNamespace(
        emit=lambda success, message: events.append(
            ("autofocus result", success, message)
        )
    )
    controller.movement_finished = SimpleNamespace(
        emit=lambda success, message: events.append(
            ("movement result", success, message)
        )
    )


def test_gui_autofocus_holds_session_from_before_movement_through_success(
    controller,
) -> None:
    events: list[object] = []
    controller._serial = _FakeSerial()
    controller.set_optical_session_manager(_FakeSessionManager(events))
    _install_result_signals(controller, events)
    controller._run_autofocus_locked = (
        lambda: events.append("autofocus work") or "Focused."
    )

    controller._run_autofocus()

    assert events == [
        ("open", "autofocus", None),
        "exposure ready",
        "movement started",
        "autofocus work",
        "close",
        ("autofocus result", True, "Focused."),
    ]


def test_gui_autofocus_open_failure_prevents_movement_and_capture(controller) -> None:
    events: list[object] = []
    controller._serial = _FakeSerial()
    controller.set_optical_session_manager(
        _FakeSessionManager(
            events,
            open_error=ExposurePolicyError("camera adjustment failed"),
        )
    )
    _install_result_signals(controller, events)
    controller._run_autofocus_locked = lambda: events.append("autofocus work")

    controller._run_autofocus()

    assert events[0] == ("open", "autofocus", None)
    assert "movement started" not in events
    assert "autofocus work" not in events
    assert events[-1][0:2] == ("autofocus result", False)
    assert "camera adjustment failed" in events[-1][2]


def test_gui_autofocus_failure_closes_session_before_failure_result(controller) -> None:
    events: list[object] = []
    controller._serial = _FakeSerial()
    controller.set_optical_session_manager(_FakeSessionManager(events))
    _install_result_signals(controller, events)

    def _fail_autofocus() -> str:
        events.append("autofocus work")
        raise StageControllerError("focus failed")

    controller._run_autofocus_locked = _fail_autofocus

    controller._run_autofocus()

    assert events[-2] == "close"
    assert events[-1] == ("autofocus result", False, "focus failed")


def test_external_local_autofocus_forwards_parent_token(controller) -> None:
    events: list[object] = []
    controller._serial = _FakeSerial()
    controller.set_optical_session_manager(_FakeSessionManager(events))
    _install_result_signals(controller, events)
    result = SimpleNamespace(summary=lambda: "Local focus complete.")
    controller._run_local_autofocus_locked = (
        lambda **_kwargs: events.append("local autofocus work") or result
    )

    returned = controller.run_external_local_autofocus(
        range_mm=0.03,
        parent_token="outer-token",
    )

    assert returned is result
    assert events == [
        ("open", "autofocus", "outer-token"),
        "exposure ready",
        "movement started",
        "local autofocus work",
        "close",
        ("autofocus result", True, "Local focus complete."),
        ("movement result", True, "Local focus complete."),
    ]


def test_external_local_autofocus_translates_session_close_failure(controller) -> None:
    events: list[object] = []
    controller._serial = _FakeSerial()
    controller.set_optical_session_manager(
        _FakeSessionManager(
            events,
            close_error=ExposurePolicyError("camera restore failed"),
        )
    )
    _install_result_signals(controller, events)
    result = SimpleNamespace(summary=lambda: "Local focus complete.")
    controller._run_local_autofocus_locked = lambda **_kwargs: result

    with pytest.raises(StageControllerError, match="camera restore failed"):
        controller.run_external_local_autofocus(range_mm=0.03)

    assert events[-2][0:2] == ("autofocus result", False)
    assert events[-1][0:2] == ("movement result", False)


def test_cancelled_local_autofocus_restores_start_z_before_session_close(
    controller,
) -> None:
    events: list[object] = []
    controller._serial = _WritableFakeSerial()
    controller.set_optical_session_manager(_FakeSessionManager(events))
    _install_result_signals(controller, events)
    current_z = [10.025]
    controller._prepare_autofocus_context_locked = lambda **_kwargs: AutofocusContext(
        objective_name="X20",
        start_z=10.000,
        min_z=0.0,
        max_z=20.0,
        lower_z=9.970,
        upper_z=10.030,
        local_range_mm=0.030,
        fine_step_mm=0.010,
    )

    def _cancel(*_args, **_kwargs):
        controller._cancel_event.set()
        raise StageControllerError("Operation cancelled.")

    controller._run_static_focus_refinement_locked = _cancel
    controller._query_status_with_required_coordinates = (
        lambda _serial, *, axes: SimpleNamespace(
            state="Idle",
            position=(0.0, 0.0, current_z[0]),
            work_position=(0.0, 0.0, current_z[0]),
            display_position=(0.0, 0.0, current_z[0]),
            work_offset=(0.0, 0.0, 0.0),
            homed_axes={"Z"},
        )
    )
    controller._position_for_configured_mode = lambda status: status.display_position
    controller._wait_for_idle = lambda timeout=10.0: None

    def _restore(move, **_kwargs) -> None:
        events.append("restore start Z")
        current_z[0] += move.z

    controller._send_relative_move = _restore

    with pytest.raises(StageControllerError, match="Operation cancelled"):
        controller.run_external_local_autofocus(range_mm=0.03)

    assert current_z[0] == pytest.approx(10.0)
    assert events.index("restore start Z") < events.index("close")


@pytest.mark.parametrize("matrix_present", [False, True])
def test_click_calibration_or_verification_holds_session_through_fresh_frame(
    controller,
    matrix_present: bool,
) -> None:
    events: list[object] = []
    controller._serial = _FakeSerial()
    controller._pixels_to_mm = np.eye(2) if matrix_present else None
    controller._objective_calibration_verified[controller._active_objective_name] = False
    controller.set_optical_session_manager(_FakeSessionManager(events))
    _install_result_signals(controller, events)
    finished = threading.Event()
    controller.movement_finished = SimpleNamespace(
        emit=lambda success, message: (
            events.append(("movement result", success, message)),
            finished.set(),
        )
    )
    controller._move_safety_check = lambda: events.append("safety check")
    controller._ensure_calibration = (
        lambda target_pixels=None: events.append("calibration work") or (True, 7)
    )
    controller._wait_for_new_frame = (
        lambda counter, timeout=4.0: events.append("fresh frame")
        or (object(), counter + 1)
    )

    assert controller.request_move(12.0, -4.0) is True
    assert finished.wait(timeout=1.0)

    assert events == [
        ("open", "click-to-move calibration", None),
        "exposure ready",
        "movement started",
        "safety check",
        "calibration work",
        "fresh frame",
        "close",
        ("movement result", True, "Move complete."),
    ]


def test_verified_click_does_not_open_optical_session(controller) -> None:
    events: list[object] = []
    controller._serial = _FakeSerial()
    controller._pixels_to_mm = np.eye(2) * 0.001
    controller._objective_calibration_verified[controller._active_objective_name] = True
    controller.set_optical_session_manager(_FakeSessionManager(events))
    _install_result_signals(controller, events)
    controller._move_safety_check = lambda: None
    controller._get_frame_snapshot = lambda timeout=3.0: (object(), 4)
    controller._send_relative_move = lambda *_args, **_kwargs: events.append("move")
    controller._wait_for_new_frame = lambda counter, timeout=4.0: (
        object(),
        counter + 1,
    )

    controller._run_move(12.0, -4.0)

    assert not any(isinstance(event, tuple) and event[0] == "open" for event in events)
    assert events[0] == "movement started"
    assert "move" in events
    assert events[-1] == ("movement result", True, "Move complete.")


def test_click_session_open_failure_prevents_movement_and_capture(controller) -> None:
    events: list[object] = []
    controller._serial = _FakeSerial()
    controller.set_optical_session_manager(
        _FakeSessionManager(
            events,
            open_error=ExposurePolicyError("camera adjustment failed"),
        )
    )
    _install_result_signals(controller, events)
    controller._move_safety_check = lambda: events.append("safety check")
    controller._ensure_calibration = lambda **_kwargs: events.append(
        "calibration frame"
    )
    controller._wait_for_new_frame = lambda *_args, **_kwargs: events.append(
        "fresh frame"
    )

    controller._run_move(12.0, -4.0)

    assert events[0] == ("open", "click-to-move calibration", None)
    assert "movement started" not in events
    assert "safety check" not in events
    assert "calibration frame" not in events
    assert "fresh frame" not in events
    assert events[-1][0:2] == ("movement result", False)


@pytest.mark.parametrize("matrix_present", [False, True])
def test_resolve_clicked_point_opens_session_before_serial_or_calibration(
    controller,
    matrix_present: bool,
) -> None:
    events: list[object] = []
    controller._serial = _FakeSerial()
    controller._pixels_to_mm = np.eye(2) * 0.001 if matrix_present else None
    controller._objective_calibration_verified[controller._active_objective_name] = False
    controller.set_optical_session_manager(_FakeSessionManager(events))
    controller._query_current_status_with_required_coordinates = (
        lambda **_kwargs: events.append("serial read")
        or SimpleNamespace(display_position=(1.0, 2.0, 3.0))
    )
    controller._ensure_calibration = (
        lambda: events.append("calibration frame")
        or setattr(controller, "_pixels_to_mm", np.eye(2) * 0.001)
    )

    center, clicked = controller.resolve_clicked_point_xy(10.0, -20.0)

    assert center == (1.0, 2.0)
    assert clicked == pytest.approx((0.99, 2.02))
    assert events == [
        ("open", "click-to-move calibration", None),
        "exposure ready",
        "serial read",
        "calibration frame",
        "close",
    ]


def test_optical_workflow_without_manager_fails_closed(controller) -> None:
    events: list[object] = []
    controller._serial = _FakeSerial()
    _install_result_signals(controller, events)
    controller._run_local_autofocus_locked = (
        lambda **_kwargs: events.append("local autofocus work")
    )

    with pytest.raises(StageControllerError, match="Optical session manager"):
        controller.run_external_local_autofocus(range_mm=0.03)

    assert "movement started" not in events
    assert "local autofocus work" not in events
