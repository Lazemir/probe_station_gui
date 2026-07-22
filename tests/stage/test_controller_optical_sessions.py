from __future__ import annotations

import threading
from types import SimpleNamespace

import numpy as np
import pytest

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
        enter_error: BaseException | None = None,
        close_error: BaseException | None = None,
        suppress: bool = False,
    ) -> None:
        self.events = events
        self.enter_error = enter_error
        self.close_error = close_error
        self.suppress = suppress
        self.token = "session-token"

    def __enter__(self):
        if self.enter_error is not None:
            self.events.append("enter")
            raise self.enter_error
        self.events.append("exposure ready")
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> bool:
        self.events.append("close")
        if self.close_error is not None:
            raise self.close_error
        return self.suppress


class _FakeSessionManager:
    def __init__(
        self,
        events: list[object],
        *,
        open_error: BaseException | None = None,
        enter_error: BaseException | None = None,
        close_error: BaseException | None = None,
        suppress: bool = False,
    ) -> None:
        self.events = events
        self.open_error = open_error
        self.enter_error = enter_error
        self.close_error = close_error
        self.suppress = suppress

    def open(self, operation: str, parent_token: str | None = None) -> _FakeLease:
        self.events.append(("open", operation, parent_token))
        if self.open_error is not None:
            raise self.open_error
        return _FakeLease(
            self.events,
            enter_error=self.enter_error,
            close_error=self.close_error,
            suppress=self.suppress,
        )


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


def test_reserved_position_read_is_available_only_to_external_task_owner(
    controller,
) -> None:
    controller._query_current_stage_position_status = lambda: SimpleNamespace(
        display_position=(10.0, 20.0, 3.0),
        state="Idle",
    )
    controller._ensure_b_axis_zero_reference = lambda _status: None

    with pytest.raises(StageControllerError, match="external stage task"):
        controller.run_external_current_stage_position()

    controller.begin_external_task("calibration")
    try:
        with pytest.raises(StageControllerError, match="Stage is busy"):
            controller.current_stage_position()
        assert controller.run_external_current_stage_position() == (
            10.0,
            20.0,
            3.0,
        )
    finally:
        controller.finish_external_task()


def test_reserved_physical_machine_read_is_owner_only_and_uses_raw_mpos(
    controller,
) -> None:
    masks: list[int] = []
    controller._serial = _FakeSerial()
    controller._position_reporting_mode = "work"
    controller._current_status_report_mask = 2
    controller._ensure_status_report_mask = lambda mask, **_kwargs: masks.append(mask)
    controller._read_status_frame = lambda _serial, **kwargs: kwargs[
        "parse_status_line"
    ]("<Idle|MPos:10.0,20.0,2.0,3.0|WCO:100.0,100.0,100.0,100.0>")
    controller._axis_calibration_mapper = lambda: SimpleNamespace(
        controller_to_physical=lambda axis, value: (
            float(value) * 2.0 if axis == "Z" else float(value) + 1.0
        )
    )

    with pytest.raises(StageControllerError, match="external stage task"):
        controller.run_external_current_physical_machine_coordinates(("Z", "A"))

    controller.begin_external_task("contact")
    try:
        coordinates = controller.run_external_current_physical_machine_coordinates(
            ("Z", "A")
        )
    finally:
        controller.finish_external_task()

    assert coordinates == {"Z": 4.0, "A": 4.0}
    assert masks == [3, 2]


def test_blocked_external_position_query_does_not_hold_task_lock(
    controller,
) -> None:
    query_started = threading.Event()
    release_query = threading.Event()
    busy_done = threading.Event()
    cancel_done = threading.Event()
    busy_results: list[bool] = []
    owner_errors: list[BaseException] = []
    controller._serial = _FakeSerial()
    controller._ensure_b_axis_zero_reference = lambda _status: None
    controller.status_message = SimpleNamespace(emit=lambda *_args: None)
    controller.queue_jog_stop = lambda: None

    def blocked_query(*, min_axes: int):
        assert min_axes == 3
        query_started.set()
        if not release_query.wait(5.0):
            raise RuntimeError("test did not release status query")
        return SimpleNamespace(
            display_position=(10.0, 20.0, 3.0),
            state="Idle",
        )

    controller._query_synced_status_for_absolute_motion = blocked_query

    def read_position_as_owner() -> None:
        controller.begin_external_task("scan")
        try:
            controller.run_external_current_stage_position()
        except BaseException as exc:  # pragma: no cover - asserted below
            owner_errors.append(exc)
        finally:
            controller.finish_external_task()

    owner = threading.Thread(target=read_position_as_owner)
    owner.start()
    assert query_started.wait(1.0)

    busy_reader = threading.Thread(
        target=lambda: (
            busy_results.append(controller.is_busy()),
            busy_done.set(),
        )
    )
    canceller = threading.Thread(
        target=lambda: (
            controller.cancel_active_task("cancel blocked position query"),
            cancel_done.set(),
        )
    )
    busy_reader.start()
    canceller.start()
    try:
        assert busy_done.wait(1.0)
        assert cancel_done.wait(1.0)
        assert busy_results == [True]
        assert controller._cancel_event.is_set()
    finally:
        release_query.set()
        owner.join(timeout=2.0)
        busy_reader.join(timeout=2.0)
        canceller.join(timeout=2.0)

    assert not owner.is_alive()
    assert not busy_reader.is_alive()
    assert not canceller.is_alive()
    assert owner_errors == []


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
            open_error=RuntimeError("camera adjustment failed"),
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


def test_gui_autofocus_enter_failure_prevents_movement_and_capture(controller) -> None:
    events: list[object] = []
    controller._serial = _FakeSerial()
    controller.set_optical_session_manager(
        _FakeSessionManager(
            events,
            enter_error=RuntimeError("camera lease failed"),
        )
    )
    _install_result_signals(controller, events)
    controller._run_autofocus_locked = lambda: events.append("autofocus work")

    controller._run_autofocus()

    assert events[:2] == [("open", "autofocus", None), "enter"]
    assert "movement started" not in events
    assert "autofocus work" not in events
    assert events[-1][0:2] == ("autofocus result", False)
    assert "camera lease failed" in events[-1][2]


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
            close_error=RuntimeError("camera restore failed"),
        )
    )
    _install_result_signals(controller, events)
    result = SimpleNamespace(summary=lambda: "Local focus complete.")
    controller._run_local_autofocus_locked = lambda **_kwargs: result

    with pytest.raises(StageControllerError, match="camera restore failed"):
        controller.run_external_local_autofocus(range_mm=0.03)

    assert not any(
        isinstance(event, tuple)
        and event[0] in {"autofocus result", "movement result"}
        and event[1] is True
        for event in events
    )
    assert events[-2][0:2] == ("autofocus result", False)
    assert events[-1][0:2] == ("movement result", False)


def test_body_stage_error_remains_original_after_normal_session_close(
    controller,
) -> None:
    events: list[object] = []
    controller._serial = _FakeSerial()
    controller.set_optical_session_manager(_FakeSessionManager(events))
    _install_result_signals(controller, events)
    body_error = StageControllerError("autofocus body failed")

    def _fail_body(**_kwargs):
        raise body_error

    controller._run_local_autofocus_locked = _fail_body

    with pytest.raises(StageControllerError) as caught:
        controller.run_external_local_autofocus(range_mm=0.03)

    assert caught.value is body_error
    assert "close" in events


def test_optical_session_preserves_lease_suppression(controller) -> None:
    events: list[object] = []
    controller.set_optical_session_manager(
        _FakeSessionManager(events, suppress=True)
    )

    with controller._open_optical_session("test operation"):
        events.append("body")
        raise StageControllerError("suppressed body error")

    assert events == [
        ("open", "test operation", None),
        "exposure ready",
        "body",
        "close",
    ]


@pytest.mark.parametrize(
    ("phase", "boundary_error"),
    [
        ("open", KeyboardInterrupt()),
        ("enter", SystemExit()),
        ("exit", KeyboardInterrupt()),
    ],
)
def test_optical_session_does_not_normalize_base_exceptions(
    controller,
    phase: str,
    boundary_error: BaseException,
) -> None:
    events: list[object] = []
    error_argument = {
        "open": "open_error",
        "enter": "enter_error",
        "exit": "close_error",
    }[phase]
    manager_kwargs = {error_argument: boundary_error}
    controller.set_optical_session_manager(
        _FakeSessionManager(events, **manager_kwargs)
    )

    with pytest.raises(type(boundary_error)) as caught:
        with controller._open_optical_session("test operation"):
            pass

    assert caught.value is boundary_error


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
    controller._wait_for_idle = lambda timeout=10.0: None

    def _restore(target_z: float) -> None:
        events.append("restore start Z")
        current_z[0] = float(target_z)

    controller._move_to_autofocus_final_z_locked = _restore

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
    controller._query_current_status_with_required_coordinates = (
        lambda **_kwargs: SimpleNamespace(display_position=(0.0, 0.0, 0.0))
    )
    controller._execute_precision_axis_targets_locked = (
        lambda *_args, **_kwargs: events.append("move")
    )
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
            open_error=RuntimeError("camera adjustment failed"),
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


def test_alignment_point_resolution_runs_and_publishes_from_stage_worker(
    controller,
) -> None:
    main_thread_id = threading.get_ident()
    resolver_started = threading.Event()
    resolver_release = threading.Event()
    resolver_threads: list[int] = []
    completion_calls: list[tuple[object, ...]] = []
    completion_threads: list[int] = []
    controller.clicked_point_resolved = SimpleNamespace(
        emit=lambda *args: (
            completion_threads.append(threading.get_ident()),
            completion_calls.append(args),
        )
    )

    def resolve(dx_pixels: float, dy_pixels: float):
        resolver_threads.append(threading.get_ident())
        resolver_started.set()
        assert resolver_release.wait(2.0)
        return (1.0, 2.0), (1.0 + dx_pixels, 2.0 + dy_pixels)

    controller._resolve_clicked_point_xy_for_active_task = resolve

    assert controller.request_clicked_point_resolution(
        "capture-1",
        0.25,
        -0.5,
    ) is True
    assert resolver_started.wait(1.0)
    assert resolver_threads[0] != main_thread_id
    assert completion_calls == []

    resolver_release.set()
    assert controller.wait_for_active_task(timeout_s=1.0)
    assert completion_threads == resolver_threads
    assert completion_calls == [
        ("capture-1", True, (1.0, 2.0), (1.25, 1.5), "")
    ]


def test_cancelled_alignment_point_resolution_never_reports_success(controller) -> None:
    resolver_started = threading.Event()
    completion_calls: list[tuple[object, ...]] = []
    controller.clicked_point_resolved = SimpleNamespace(
        emit=lambda *args: completion_calls.append(args)
    )

    def resolve(_dx_pixels: float, _dy_pixels: float):
        resolver_started.set()
        controller._cancel_event.wait(2.0)
        controller._check_cancelled()

    controller._resolve_clicked_point_xy_for_active_task = resolve

    assert controller.request_clicked_point_resolution(
        "capture-cancelled",
        0.0,
        0.0,
    ) is True
    assert resolver_started.wait(1.0)
    assert controller.cancel_clicked_point_resolution(
        "capture-cancelled",
        "Alignment capture cancelled.",
    ) is True

    assert controller.wait_for_active_task(timeout_s=1.0)
    assert len(completion_calls) == 1
    request_id, success, center_xy, clicked_xy, message = completion_calls[0]
    assert request_id == "capture-cancelled"
    assert success is False
    assert center_xy is None
    assert clicked_xy is None
    assert "cancel" in message.lower()


def test_stale_alignment_cancel_does_not_cancel_newer_stage_task(controller) -> None:
    completion_calls: list[tuple[object, ...]] = []
    controller.clicked_point_resolved = SimpleNamespace(
        emit=lambda *args: completion_calls.append(args)
    )
    controller._resolve_clicked_point_xy_for_active_task = (
        lambda _dx, _dy: ((1.0, 2.0), (1.0, 2.0))
    )

    assert controller.request_clicked_point_resolution(
        "capture-old",
        0.0,
        0.0,
    ) is True
    assert controller.wait_for_active_task(timeout_s=1.0)

    newer_started = threading.Event()
    newer_release = threading.Event()

    def newer_task() -> None:
        newer_started.set()
        assert newer_release.wait(2.0)

    assert controller._start_background_task(
        target=newer_task,
        busy_message="busy",
    ) is True
    assert newer_started.wait(1.0)

    assert controller.cancel_clicked_point_resolution(
        "capture-old",
        "stale cancellation",
    ) is False
    assert controller._cancel_event.is_set() is False

    newer_release.set()
    assert controller.wait_for_active_task(timeout_s=1.0)


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
