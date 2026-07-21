from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QColor, QImage

from probe_station_gui.camera import optical_calibration_adapters as calibration_adapters
from probe_station_gui.camera.exposure_policy import ExposurePolicyBusyError
from probe_station_gui.camera.optical_calibration_lifecycle import (
    OpticalCalibrationLifecycle,
)
from probe_station_gui.camera.optical_calibration_runtime import (
    FlatFieldCalibrationRequest,
    LensCalibrationArtifact,
    LensDistortionCalibrationRequest,
    OpticalCalibrationOutcome,
    OpticalCalibrationRuntime,
)
from probe_station_gui.camera.optical_calibration_geometry import (
    LensFitLimits,
    flat_field_capture_offsets_mm,
    lens_capture_offsets_mm,
)
from probe_station_gui.camera.optical_calibration_adapters import (
    OpticalCalibrationCameraAdapter,
    OpticalCalibrationEventAdapter,
    OpticalCalibrationSessionAdapter,
    OpticalCalibrationStageAdapter,
    OpticalCalibrationStoreAdapter,
    prepare_lens_completion,
)


class _FailingSession:
    def open(self, operation: str, parent_token: str | None = None):
        raise RuntimeError("Exposure did not converge.")


class _ForbiddenPort:
    def __getattr__(self, name: str):
        raise AssertionError(f"unexpected port call: {name}")


class _Events:
    def __init__(self) -> None:
        self.finished = []
        self.progress_events = []

    def progress(self, event) -> None:
        self.progress_events.append(event)

    def complete(self, outcome) -> None:
        self.finished.append(outcome)

    def warning(self, message: str) -> None:
        self.finished.append(("warning", message))


class _InlineThread:
    errors: list[BaseException] = []

    def __init__(self, *, target, **_kwargs) -> None:
        self._target = target
        self._alive = False

    def start(self) -> None:
        self._alive = True
        try:
            self._target()
        except BaseException as exc:
            self.errors.append(exc)
        finally:
            self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    def join(self, _timeout=None) -> None:
        return None


class _DeferredThread:
    def __init__(self, *, target, **_kwargs) -> None:
        self.target = target

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return False

    def join(self, _timeout=None) -> None:
        return None


@pytest.fixture(autouse=True)
def _surface_inline_worker_errors():
    _InlineThread.errors.clear()
    yield
    assert _InlineThread.errors == []


class _Lease:
    def __init__(
        self,
        events: list[object],
        operation: str,
        token: str,
        *,
        close_warning: str = "",
        close_error: Exception | None = None,
    ) -> None:
        self.events = events
        self.operation = operation
        self.token = token
        self.close_warning = close_warning
        self.close_error = close_error
        self.active = True

    def snapshot(self) -> dict[str, object]:
        self.events.append(("session_snapshot", self.operation))
        return {"operation": self.operation, "fixed_exposure_us": 3200.0}

    def close(self) -> dict[str, object]:
        self.events.append(("session_close", self.operation))
        if self.close_error is not None:
            raise self.close_error
        self.active = False
        result: dict[str, object] = {"accepted": True}
        if self.close_warning:
            result["warning"] = self.close_warning
        return result

    def is_active(self) -> bool:
        return self.active


class _Sessions:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.leases: list[_Lease] = []
        self.close_warning_by_operation: dict[str, str] = {}

    def open(self, operation: str, parent_token: str | None = None) -> _Lease:
        token = f"lease-{len(self.leases) + 1}"
        self.events.append(("session_open", operation, parent_token, token))
        lease = _Lease(
            self.events,
            operation,
            token,
            close_warning=self.close_warning_by_operation.get(operation, ""),
        )
        self.leases.append(lease)
        return lease


class _Stage:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.fail_on: str | None = None
        self.release_error: Exception | None = None

    def reserve(self, operation: str) -> None:
        self.events.append(("stage_reserve", operation))
        if self.fail_on == "reserve":
            raise RuntimeError("reserve failed")

    def start_position(self) -> tuple[float, float]:
        self.events.append(("stage_position",))
        if self.fail_on == "position":
            raise RuntimeError("position failed")
        return 10.0, 20.0

    def raise_needles(self, feedrate: float) -> None:
        self.events.append(("needles", feedrate))

    def move_xy(self, x_mm: float, y_mm: float, feedrate: float) -> None:
        self.events.append(("move", x_mm, y_mm, feedrate))
        if self.fail_on == "move":
            raise RuntimeError("move failed")

    def release(self) -> None:
        self.events.append(("stage_release",))
        if self.release_error is not None:
            raise self.release_error


class _Camera:
    def __init__(self, events: list[object], frames: list[QImage]) -> None:
        self.events = events
        self.frames = list(frames)
        self.counter = 0
        self.restore_warning = ""
        self.restore_error: Exception | None = None

    def lock(self) -> str:
        self.events.append(("camera_lock",))
        return "camera-key"

    def restore(self, key: str) -> str:
        self.events.append(("camera_restore", key))
        if self.restore_error is not None:
            raise self.restore_error
        return self.restore_warning

    def latest_raw_counter(self) -> int:
        self.events.append(("camera_counter", self.counter))
        return self.counter

    def wait_raw(self, *, after_counter: int | None, timeout_s: float) -> QImage | None:
        self.events.append(("camera_frame", after_counter, timeout_s))
        if not self.frames:
            return None
        self.counter += 1
        return self.frames.pop(0)


class _Store:
    def __init__(self, events: list[object]) -> None:
        self.events = events
        self.calls: list[tuple[object, ...]] = []

    def install(self, objective_name, frames, metadata):
        self.events.append(("store", objective_name, len(frames)))
        self.calls.append((objective_name, tuple(frames), metadata))
        return SimpleNamespace(
            current_manifest="current.json", reference_image="flat.png"
        )


def _frame(width: int = 100, height: int = 50) -> QImage:
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(QColor(100, 110, 120))
    return image


def _valid_lens_payload() -> dict[str, object]:
    return {
        "model_version": 1,
        "model_type": "stage_geometry",
        "frame_size": [100, 50],
        "pixels_to_mm": [[-0.001, 0.0002], [-0.0003, -0.002]],
        "calibrated_pixels_to_mm": [[-0.001, 0.0002], [-0.0003, -0.002]],
        "center_px": [50.0, 25.0],
        "k1": 0.0,
        "k2": 0.0,
        "p1": 0.0,
        "p2": 0.0,
        "baseline_residual_mean_px": 1.5,
        "baseline_residual_max_px": 2.5,
        "residual_mean_px": 0.5,
        "residual_max_px": 1.0,
        "feature_count": 20,
        "observation_count": 40,
        "optimizer_success": True,
    }


def _flat_request(**changes) -> FlatFieldCalibrationRequest:
    request = FlatFieldCalibrationRequest(
        run_id="flat-1",
        wizard_run_id=None,
        objective_name="X20",
        magnification=20.0,
        pixels_to_mm=((-0.001, 0.0), (0.0, -0.001)),
        pixel_size_mm=(0.001, 0.002),
        linear_feedrate=120.0,
        needle_feedrate=70.0,
        settle_s=0.0,
    )
    return replace(request, **changes)


def _lens_request(**changes) -> LensDistortionCalibrationRequest:
    request = LensDistortionCalibrationRequest(
        run_id="lens-1",
        wizard_run_id=None,
        objective_name="X20",
        magnification=20.0,
        pixels_to_mm=((-0.001, 0.0002), (0.0003, -0.002)),
        pixel_size_mm=(0.001, 0.002),
        linear_feedrate=120.0,
        needle_feedrate=70.0,
        settle_s=0.0,
    )
    return replace(request, **changes)


def _runtime(events, *, frames, sessions=None, stage=None, store=None, **kwargs):
    event_port = _Events()
    runtime = OpticalCalibrationRuntime(
        stage=stage or _Stage(events),
        camera=_Camera(events, frames),
        sessions=sessions or _Sessions(events),
        store=store or _Store(events),
        events=event_port,
        thread_factory=_InlineThread,
        **kwargs,
    )
    return runtime, event_port


def test_session_open_failure_causes_zero_stage_and_camera_calls() -> None:
    events = _Events()
    runtime = OpticalCalibrationRuntime(
        stage=_ForbiddenPort(),
        camera=_ForbiddenPort(),
        sessions=_FailingSession(),
        store=_ForbiddenPort(),
        events=events,
        thread_factory=_InlineThread,
    )

    decision = runtime.start_flat(
        FlatFieldCalibrationRequest(
            run_id="flat-1",
            wizard_run_id=None,
            objective_name="X20",
            magnification=20.0,
            pixels_to_mm=((-0.001, 0.0), (0.0, -0.001)),
            pixel_size_mm=(0.001, 0.001),
            linear_feedrate=120.0,
            needle_feedrate=70.0,
        )
    )

    assert decision.accepted is True
    assert len(events.finished) == 1
    assert events.finished[0].success is False
    assert events.finished[0].message == (
        "Flat-field calibration failed: Exposure did not converge."
    )


def test_lens_session_or_position_failure_avoids_camera_work() -> None:
    emitted = _Events()
    runtime = OpticalCalibrationRuntime(
        stage=_ForbiddenPort(),
        camera=_ForbiddenPort(),
        sessions=_FailingSession(),
        store=_ForbiddenPort(),
        events=emitted,
        thread_factory=_InlineThread,
    )
    runtime.start_lens(_lens_request())
    assert emitted.finished[0].success is False

    events: list[object] = []
    stage = _Stage(events)
    stage.fail_on = "position"
    runtime, emitted = _runtime(events, frames=[], stage=stage)
    runtime.start_lens(_lens_request())
    assert not any(event[0].startswith("camera_") for event in events)
    assert events[-2:] == [
        ("stage_release",),
        ("session_close", "lens distortion calibration"),
    ]
    assert emitted.finished[0].success is False


def test_flat_capture_uses_raw_serpentine_grid_and_cleanup_order() -> None:
    events: list[object] = []
    store = _Store(events)
    runtime, emitted = _runtime(
        events, frames=[_frame() for _ in range(10)], store=store
    )

    runtime.start_flat(_flat_request())

    offsets = flat_field_capture_offsets_mm((100, 50), (0.001, 0.002), 0.8)
    moves = [event for event in events if event[0] == "move"]
    actual_offsets = [(x - 10.0, y - 20.0) for _, x, y, _feed in moves[:-1]]
    assert len(actual_offsets) == len(offsets)
    for actual, expected in zip(actual_offsets, offsets, strict=True):
        assert actual == pytest.approx(expected)
    assert moves[-1] == ("move", 10.0, 20.0, 120.0)
    assert events[-3:] == [
        ("camera_restore", "camera-key"),
        ("stage_release",),
        ("session_close", "flat-field calibration"),
    ]
    assert store.calls[0][2]["optical_session"]["fixed_exposure_us"] == 3200.0
    assert emitted.finished[0].success is True


@pytest.mark.parametrize("failure", ["position", "move"])
def test_reserved_failure_restores_camera_releases_stage_then_closes_session(
    failure,
) -> None:
    events: list[object] = []
    stage = _Stage(events)
    stage.fail_on = failure
    runtime, emitted = _runtime(
        events, frames=[_frame() for _ in range(10)], stage=stage
    )

    runtime.start_flat(_flat_request())

    assert events.index(("stage_release",)) < events.index(
        ("session_close", "flat-field calibration")
    )
    if failure == "move":
        assert events.index(("camera_restore", "camera-key")) < events.index(
            ("stage_release",)
        )
    assert emitted.finished[0].success is False


@pytest.mark.parametrize("kind", ("flat", "lens"))
def test_capture_two_timeout_returns_to_start_before_cleanup(kind, monkeypatch) -> None:
    events: list[object] = []
    runtime, emitted = _runtime(events, frames=[_frame(), _frame()])
    monkeypatch.setattr(
        "probe_station_gui.camera.optical_calibration_runtime.fit_lens_artifact",
        lambda *_args, **_kwargs: pytest.fail("fit must not run after timeout"),
    )

    if kind == "flat":
        runtime.start_flat(_flat_request())
        child_label = "flat-field calibration"
    else:
        runtime.start_lens(_lens_request())
        child_label = "lens distortion calibration"

    moves = [event for event in events if event[0] == "move"]
    assert len(moves) == 3
    assert moves[-1] == ("move", 10.0, 20.0, 120.0)
    assert events.index(moves[-1]) < events.index(("camera_restore", "camera-key"))
    assert events.index(("camera_restore", "camera-key")) < events.index(
        ("stage_release",)
    )
    assert events.index(("stage_release",)) < events.index(
        ("session_close", child_label)
    )
    assert emitted.finished[0].success is False


@pytest.mark.parametrize("kind", ("flat", "lens"))
def test_cancel_after_first_grid_capture_returns_to_start_and_invalidates_outcome(
    kind,
    monkeypatch,
) -> None:
    events: list[object] = []
    emitted = _Events()
    runtime: OpticalCalibrationRuntime

    class _CancelAfterFirstCapture(_Camera):
        def wait_raw(self, *, after_counter: int | None, timeout_s: float):
            frame = super().wait_raw(after_counter=after_counter, timeout_s=timeout_s)
            if self.counter == 2:
                runtime.cancel()
            return frame

    camera = _CancelAfterFirstCapture(events, [_frame() for _ in range(10)])
    runtime = OpticalCalibrationRuntime(
        stage=_Stage(events),
        camera=camera,
        sessions=_Sessions(events),
        store=_Store(events),
        events=emitted,
        thread_factory=_InlineThread,
    )
    monkeypatch.setattr(
        "probe_station_gui.camera.optical_calibration_runtime.fit_lens_artifact",
        lambda *_args, **_kwargs: pytest.fail("cancelled lens must not fit"),
    )
    request = _flat_request() if kind == "flat" else _lens_request()

    decision = runtime.start_flat(request) if kind == "flat" else runtime.start_lens(request)

    assert decision.accepted is True
    moves = [event for event in events if event[0] == "move"]
    assert moves == [
        ("move", 10.0, 20.0, 120.0),
        ("move", 10.0, 20.0, 120.0),
    ]
    outcome = emitted.finished[0]
    assert outcome.success is False
    assert "stopped by user" in outcome.message
    assert runtime.consume(outcome) is False
    operation = "flat-field calibration" if kind == "flat" else "lens distortion calibration"
    assert events[-3:] == [
        ("camera_restore", "camera-key"),
        ("stage_release",),
        ("session_close", operation),
    ]


def test_real_worker_thread_start_is_nonblocking_and_cancel_wins_initial_capture() -> None:
    events: list[object] = []
    entered_capture = threading.Event()
    release_capture = threading.Event()
    completed = threading.Event()

    class _BlockingCamera(_Camera):
        def wait_raw(self, *, after_counter: int | None, timeout_s: float):
            entered_capture.set()
            assert release_capture.wait(1.0)
            return super().wait_raw(after_counter=after_counter, timeout_s=timeout_s)

    class _ThreadEvents(_Events):
        def complete(self, outcome) -> None:
            super().complete(outcome)
            completed.set()

    emitted = _ThreadEvents()
    runtime = OpticalCalibrationRuntime(
        stage=_Stage(events),
        camera=_BlockingCamera(events, [_frame()]),
        sessions=_Sessions(events),
        store=_Store(events),
        events=emitted,
    )

    decision = runtime.start_flat(_flat_request())

    assert decision.accepted is True
    assert entered_capture.wait(0.5)
    assert runtime.state().active_run_id == "flat-1"
    runtime.cancel("flat-1")
    release_capture.set()
    assert completed.wait(1.0)
    assert emitted.finished[0].success is False
    assert "stopped by user" in emitted.finished[0].message
    assert [event for event in events if event[0] == "move"] == []
    assert runtime.consume(emitted.finished[0]) is False


@pytest.mark.parametrize("blocked_action", ("shutdown", "start"))
def test_real_worker_remains_tracked_until_completion_callback_returns(
    blocked_action,
) -> None:
    events: list[object] = []
    completion_entered = threading.Event()
    release_completion = threading.Event()

    class _BlockingCompletionEvents(_Events):
        def complete(self, outcome) -> None:
            completion_entered.set()
            assert release_completion.wait(2.0)
            super().complete(outcome)

    runtime = OpticalCalibrationRuntime(
        stage=_Stage(events),
        camera=_Camera(events, [_frame() for _ in range(10)]),
        sessions=_Sessions(events),
        store=_Store(events),
        events=_BlockingCompletionEvents(),
    )
    assert runtime.start_flat(_flat_request()).accepted is True
    assert completion_entered.wait(1.0)

    try:
        if blocked_action == "shutdown":
            assert runtime.shutdown(0.0) is False
        else:
            decision = runtime.start_flat(_flat_request(run_id="flat-2"))
            assert decision.accepted is False
    finally:
        release_completion.set()

    assert runtime.shutdown(1.0) is True


def test_cancel_after_finish_defers_parent_close_until_completion_returns() -> None:
    events: list[object] = []
    completion_entered = threading.Event()
    release_completion = threading.Event()
    parent_closed = threading.Event()

    class _ParentLease(_Lease):
        def close(self) -> dict[str, object]:
            result = super().close()
            if self.operation == "optical calibration":
                parent_closed.set()
            return result

    class _ParentSessions(_Sessions):
        def open(self, operation: str, parent_token: str | None = None) -> _Lease:
            token = f"lease-{len(self.leases) + 1}"
            lease = _ParentLease(self.events, operation, token)
            self.leases.append(lease)
            return lease

    class _BlockingCompletionEvents(_Events):
        def complete(self, outcome) -> None:
            completion_entered.set()
            assert release_completion.wait(2.0)
            super().complete(outcome)

    runtime = OpticalCalibrationRuntime(
        stage=_Stage(events),
        camera=_Camera(events, [_frame() for _ in range(10)]),
        sessions=_ParentSessions(events),
        store=_Store(events),
        events=_BlockingCompletionEvents(),
    )
    assert runtime.start_flat(_flat_request(full_wizard=True)).accepted is True
    assert completion_entered.wait(1.0)

    runtime.cancel("flat-1")
    assert parent_closed.wait(0.05) is False
    release_completion.set()

    assert parent_closed.wait(1.0) is True
    assert runtime.shutdown(1.0) is True


@pytest.mark.parametrize("failure", ("camera_restore", "stage_release"))
def test_cleanup_exception_does_not_skip_later_cleanup_or_outcome(failure) -> None:
    events: list[object] = []
    stage = _Stage(events)
    camera = _Camera(events, [_frame() for _ in range(10)])
    if failure == "camera_restore":
        camera.restore_error = RuntimeError("restore exploded")
    else:
        stage.release_error = RuntimeError("release exploded")
    emitted = _Events()
    runtime = OpticalCalibrationRuntime(
        stage=stage,
        camera=camera,
        sessions=_Sessions(events),
        store=_Store(events),
        events=emitted,
        thread_factory=_InlineThread,
    )

    runtime.start_flat(_flat_request())

    assert ("session_close", "flat-field calibration") in events
    assert runtime.state().active_run_id is None
    assert len(emitted.finished) == 1
    assert emitted.finished[0].success is False
    assert failure.split("_")[-1] in emitted.finished[0].message.lower()


def test_flat_capture_rejects_first_frame_that_differs_from_sizing_frame() -> None:
    events: list[object] = []
    runtime, emitted = _runtime(
        events,
        frames=[_frame(100, 50), *[_frame(101, 50) for _ in range(9)]],
    )

    runtime.start_flat(_flat_request())

    assert emitted.finished[0].success is False
    assert "frame size changed" in emitted.finished[0].message.lower()


def test_flat_capture_rejects_non_three_by_three_request_before_stage_work() -> None:
    events: list[object] = []
    runtime, emitted = _runtime(events, frames=[])

    runtime.start_flat(_flat_request(grid_size=4))

    assert not any(event[0].startswith("stage_") for event in events)
    assert emitted.finished[0].success is False
    assert "3x3" in emitted.finished[0].message


def test_full_wizard_reuses_parent_session_across_flat_then_lens(monkeypatch) -> None:
    events: list[object] = []
    sessions = _Sessions(events)
    runtime, emitted = _runtime(
        events,
        frames=[_frame() for _ in range(20)],
        sessions=sessions,
    )
    artifact = LensCalibrationArtifact(_valid_lens_payload(), _frame(), _frame())
    monkeypatch.setattr(
        "probe_station_gui.camera.optical_calibration_runtime.fit_lens_artifact",
        lambda *_args, **_kwargs: artifact,
    )

    runtime.start_flat(_flat_request(full_wizard=True, wizard_run_id=17))
    parent_token = runtime.state().parent_session_token
    assert parent_token == "lease-1"
    runtime.start_lens(
        _lens_request(
            full_wizard=True, wizard_run_id=17, parent_session_token=parent_token
        )
    )

    opens = [event for event in events if event[0] == "session_open"]
    assert opens == [
        ("session_open", "optical calibration", None, "lease-1"),
        ("session_open", "flat-field calibration", "lease-1", "lease-2"),
        ("session_open", "lens distortion calibration", "lease-1", "lease-3"),
    ]
    assert events[-1] == ("session_close", "optical calibration")
    assert [item.success for item in emitted.finished] == [True, True]


def test_retained_parent_rejects_standalone_and_missing_token_continuations() -> None:
    events: list[object] = []
    runtime, _emitted = _runtime(events, frames=[_frame() for _ in range(10)])
    runtime.start_flat(_flat_request(full_wizard=True, wizard_run_id=17))
    parent_token = runtime.state().parent_session_token

    standalone = runtime.start_flat(_flat_request(run_id="standalone"))
    missing_token = runtime.start_lens(
        _lens_request(run_id="missing", full_wizard=True, wizard_run_id=17)
    )

    assert parent_token is not None
    assert standalone.accepted is False
    assert missing_token.accepted is False
    assert runtime.state().parent_session_token == parent_token


def test_cancel_between_full_wizard_phases_closes_parent_session() -> None:
    events: list[object] = []
    runtime, _emitted = _runtime(events, frames=[_frame() for _ in range(10)])
    runtime.start_flat(_flat_request(full_wizard=True, wizard_run_id=17))

    runtime.cancel("flat-1")

    assert events[-1] == ("session_close", "optical calibration")
    assert runtime.state().parent_session_token is None


def test_thread_start_failure_preserves_retained_parent() -> None:
    class _StartFailure(_InlineThread):
        def start(self) -> None:
            raise RuntimeError("thread start failed")

    events: list[object] = []
    emitted = _Events()
    sessions = _Sessions(events)
    runtime = OpticalCalibrationRuntime(
        stage=_Stage(events),
        camera=_Camera(events, []),
        sessions=sessions,
        store=_Store(events),
        events=emitted,
        thread_factory=_StartFailure,
    )

    child = runtime._lifecycle.open_child_session(
        "flat-field calibration", _flat_request(full_wizard=True)
    )
    runtime._lifecycle.close_child(child)
    parent_token = runtime.state().parent_session_token

    decision = runtime.start_lens(
        _lens_request(
            full_wizard=True,
            wizard_run_id=17,
            parent_session_token=parent_token,
        )
    )

    assert decision.accepted is False
    assert decision.status_code == 500
    assert "thread start failed" in decision.message
    assert runtime.state().active_run_id is None
    assert runtime.state().parent_session_token == parent_token


def test_stale_finish_does_not_clear_or_publish_current_run() -> None:
    events: list[object] = []
    lifecycle = OpticalCalibrationLifecycle(
        sessions=_Sessions(events),
        events=_Events(),
        thread_factory=_DeferredThread,
    )
    current = _flat_request(run_id="current")
    stale = _flat_request(run_id="stale")

    assert lifecycle.start(current, "flat", lambda _request: None).accepted is True
    assert lifecycle.finish(stale) is False
    assert lifecycle.state().active_run_id == "current"


def test_queued_outcome_is_single_use_and_new_run_or_cancel_invalidates_it() -> None:
    events: list[object] = []
    emitted = _Events()
    runtime = OpticalCalibrationRuntime(
        stage=_Stage(events),
        camera=_Camera(events, []),
        sessions=_Sessions(events),
        store=_Store(events),
        events=emitted,
        thread_factory=_DeferredThread,
    )
    first = _flat_request(run_id="first")
    first_outcome = OpticalCalibrationOutcome(
        "first", None, "flat", True, "saved", "X20", False, flat_payload={}
    )
    runtime.start_flat(first)
    runtime._finish_run(first, first_outcome)

    assert runtime.consume(first_outcome) is True
    assert runtime.consume(first_outcome) is False

    second = _flat_request(run_id="second")
    second_outcome = replace(first_outcome, run_id="second")
    runtime.start_flat(second)
    runtime._finish_run(second, second_outcome)
    runtime.start_flat(_flat_request(run_id="third"))
    assert runtime.consume(second_outcome) is False

    third_outcome = replace(first_outcome, run_id="third")
    runtime._finish_run(_flat_request(run_id="third"), third_outcome)
    runtime.cancel()
    assert runtime.consume(third_outcome) is False


def test_parent_session_close_retries_exposure_policy_contention() -> None:
    class _RetryLease(_Lease):
        def __init__(self, events, operation, token) -> None:
            super().__init__(events, operation, token)
            self.close_attempts = 0

        def close(self) -> dict[str, object]:
            self.close_attempts += 1
            if self.close_attempts < 3:
                raise ExposurePolicyBusyError("busy")
            return super().close()

    class _RetrySessions(_Sessions):
        def open(self, operation: str, parent_token: str | None = None) -> _Lease:
            token = f"lease-{len(self.leases) + 1}"
            self.events.append(("session_open", operation, parent_token, token))
            lease = (
                _RetryLease(self.events, operation, token)
                if operation == "optical calibration"
                else _Lease(self.events, operation, token)
            )
            self.leases.append(lease)
            return lease

    events: list[object] = []
    sessions = _RetrySessions(events)
    sleeps: list[float] = []
    lifecycle = OpticalCalibrationLifecycle(
        sessions=sessions,
        events=_Events(),
        thread_factory=_InlineThread,
        sleep=sleeps.append,
    )
    request = _flat_request(full_wizard=True)
    child = lifecycle.open_child_session("flat-field calibration", request)

    assert lifecycle.close_child(child) == []
    assert lifecycle.close_parent_session() == ""
    assert sessions.leases[0].close_attempts == 3
    assert sleeps == [0.05, 0.05]


def test_parent_close_thread_start_failure_retains_session_and_warns() -> None:
    class _StartFailure:
        def __init__(self, **_kwargs) -> None:
            pass

        def start(self) -> None:
            raise RuntimeError("thread start failed")

        def is_alive(self) -> bool:
            return False

    events: list[object] = []
    emitted = _Events()
    lifecycle = OpticalCalibrationLifecycle(
        sessions=_Sessions(events),
        events=emitted,
        thread_factory=_StartFailure,
    )
    child = lifecycle.open_child_session(
        "flat-field calibration", _flat_request(full_wizard=True)
    )
    lifecycle.close_child(child)
    parent_token = lifecycle.state().parent_session_token

    lifecycle.cancel()

    assert lifecycle.state().parent_session_token == parent_token
    assert emitted.finished == [
        ("warning", "Exposure policy restore could not start: thread start failed")
    ]


def test_duplicate_parent_race_closes_loser_without_holding_lifecycle_lock() -> None:
    barrier = threading.Barrier(2)
    lifecycle_holder = []

    class _RaceLease(_Lease):
        closed_without_lock = False

        def close(self) -> dict[str, object]:
            probe_finished = threading.Event()

            def probe_state() -> None:
                lifecycle_holder[0].state()
                probe_finished.set()

            probe = threading.Thread(target=probe_state, daemon=True)
            probe.start()
            self.closed_without_lock = probe_finished.wait(0.2)
            probe.join(timeout=1.0)
            if not self.closed_without_lock:
                raise AssertionError("outer lease closed while lifecycle lock was held")
            return super().close()

    class _RaceSessions(_Sessions):
        def open(self, operation: str, parent_token: str | None = None) -> _Lease:
            lease = _RaceLease(
                self.events,
                operation,
                f"lease-{len(self.leases) + 1}",
            )
            self.leases.append(lease)
            barrier.wait(timeout=1.0)
            return lease

    events: list[object] = []
    sessions = _RaceSessions(events)
    lifecycle = OpticalCalibrationLifecycle(
        sessions=sessions,
        events=_Events(),
        thread_factory=threading.Thread,
    )
    lifecycle_holder.append(lifecycle)
    errors = []

    def open_parent() -> None:
        try:
            lifecycle._open_parent_session()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=open_parent) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2.0)

    assert all(not thread.is_alive() for thread in threads)
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    loser = next(lease for lease in sessions.leases if not lease.active)
    assert loser.closed_without_lock is True


def test_lens_rejects_frame_size_change_before_fit(monkeypatch) -> None:
    events: list[object] = []
    runtime, emitted = _runtime(events, frames=[_frame(), _frame(), _frame(101, 50)])
    monkeypatch.setattr(
        "probe_station_gui.camera.optical_calibration_runtime.fit_lens_artifact",
        lambda *_args, **_kwargs: pytest.fail("fit must not run"),
    )

    runtime.start_lens(_lens_request())

    assert emitted.finished[0].success is False
    assert "frame size changed" in emitted.finished[0].message.lower()


def test_lens_artifact_survives_camera_restore_warning(monkeypatch) -> None:
    events: list[object] = []
    camera = _Camera(events, [_frame() for _ in range(10)])
    camera.restore_warning = "gain restore failed"
    emitted = _Events()
    runtime = OpticalCalibrationRuntime(
        stage=_Stage(events),
        camera=camera,
        sessions=_Sessions(events),
        store=_Store(events),
        events=emitted,
        thread_factory=_InlineThread,
    )
    artifact = LensCalibrationArtifact(_valid_lens_payload(), _frame(), _frame())
    monkeypatch.setattr(
        "probe_station_gui.camera.optical_calibration_runtime.fit_lens_artifact",
        lambda *_args, **_kwargs: artifact,
    )

    runtime.start_lens(_lens_request())

    outcome = emitted.finished[0]
    assert outcome.success is False
    assert outcome.lens_artifact is not None
    assert outcome.lens_artifact.payload is artifact.payload
    assert outcome.restore_warning == "gain restore failed"
    assert outcome.message == (
        "Lens distortion calibration complete, but restore failed: "
        "gain restore failed"
    )


def test_lens_success_message_failure_cannot_publish_success(monkeypatch) -> None:
    events: list[object] = []
    runtime, emitted = _runtime(events, frames=[_frame() for _ in range(10)])
    artifact = LensCalibrationArtifact(_valid_lens_payload(), _frame(), _frame())
    monkeypatch.setattr(
        "probe_station_gui.camera.optical_calibration_runtime.fit_lens_artifact",
        lambda *_args, **_kwargs: artifact,
    )

    def fail_message(*_args, **_kwargs):
        raise RuntimeError("payload validation failed")

    monkeypatch.setattr(
        "probe_station_gui.camera.optical_calibration_runtime.lens_success_message",
        fail_message,
    )

    runtime.start_lens(_lens_request())

    outcome = emitted.finished[0]
    assert outcome.success is False
    assert outcome.lens_artifact is not None
    assert "payload validation failed" in outcome.message


def test_lens_offsets_apply_affine_matrix_and_image_y_convention() -> None:
    offsets = lens_capture_offsets_mm(
        (1000, 800),
        ((-0.001, 0.0002), (0.0003, -0.002)),
        (0.001, 0.002),
        grid_size=3,
        fov_fraction=0.35,
    )

    assert offsets[0] == (0.0, 0.0)
    # First serpentine point is image (-350, -280), converted with Y-up delta (-350, 280).
    assert offsets[1] == pytest.approx((0.406, -0.665))


def test_shutdown_timeout_keeps_cancelled_state_and_can_be_retried() -> None:
    class _ControlledThread:
        alive = True

        def __init__(self, *, target, **_kwargs) -> None:
            self.target = target

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return self.alive

        def join(self, _timeout=None) -> None:
            return None

    events: list[object] = []
    runtime = OpticalCalibrationRuntime(
        stage=_Stage(events),
        camera=_Camera(events, []),
        sessions=_Sessions(events),
        store=_Store(events),
        events=_Events(),
        thread_factory=_ControlledThread,
    )
    runtime.start_flat(_flat_request())

    assert runtime.shutdown(0.0) is False
    assert runtime.state().shutdown_requested is True
    runtime._lifecycle._worker.alive = False
    assert runtime.shutdown(0.1) is True


def test_shutdown_timeout_bounds_parent_session_contention_and_can_retry() -> None:
    class _BusyLease(_Lease):
        busy = True

        def close(self) -> dict[str, object]:
            if self.busy:
                raise ExposurePolicyBusyError("busy")
            return super().close()

    class _BusySessions(_Sessions):
        def open(self, operation: str, parent_token: str | None = None) -> _Lease:
            token = f"lease-{len(self.leases) + 1}"
            lease = (
                _BusyLease(self.events, operation, token)
                if operation == "optical calibration"
                else _Lease(self.events, operation, token)
            )
            self.leases.append(lease)
            return lease

    class _DeferredThread:
        def __init__(self, *, target, **_kwargs) -> None:
            self.target = target

        def start(self) -> None:
            return None

        def is_alive(self) -> bool:
            return False

        def join(self, _timeout=None) -> None:
            return None

    events: list[object] = []
    sessions = _BusySessions(events)
    lifecycle = OpticalCalibrationLifecycle(
        sessions=sessions,
        events=_Events(),
        thread_factory=threading.Thread,
    )
    child = lifecycle.open_child_session(
        "flat-field calibration", _flat_request(full_wizard=True)
    )
    lifecycle.close_child(child)

    assert lifecycle.shutdown(0.0) is False
    sessions.leases[0].busy = False
    assert lifecycle.shutdown(0.1) is True


def test_parent_session_shutdown_close_runs_off_calling_thread() -> None:
    caller_thread_id = threading.get_ident()
    close_thread_ids: list[int] = []

    class _RecordingLease(_Lease):
        def close(self) -> dict[str, object]:
            if self.operation == "optical calibration":
                close_thread_ids.append(threading.get_ident())
            return super().close()

    class _RecordingSessions(_Sessions):
        def open(self, operation: str, parent_token: str | None = None) -> _Lease:
            token = f"lease-{len(self.leases) + 1}"
            lease = _RecordingLease(self.events, operation, token)
            self.leases.append(lease)
            return lease

    events: list[object] = []
    lifecycle = OpticalCalibrationLifecycle(
        sessions=_RecordingSessions(events),
        events=_Events(),
        thread_factory=threading.Thread,
    )
    child = lifecycle.open_child_session(
        "flat-field calibration", _flat_request(full_wizard=True)
    )
    lifecycle.close_child(child)

    assert lifecycle.shutdown(1.0) is True
    assert close_thread_ids
    assert all(thread_id != caller_thread_id for thread_id in close_thread_ids)


def _production_adapter_runtime(
    monkeypatch,
    *,
    fit_error: Exception | None = None,
) -> tuple[OpticalCalibrationRuntime, list[object], list[OpticalCalibrationOutcome]]:
    events: list[object] = []
    completions: list[OpticalCalibrationOutcome] = []
    frames = [_frame() for _ in range(20)]
    counter = 0

    def wait_raw(**kwargs):
        nonlocal counter
        events.append(("frame", kwargs["after_counter"]))
        counter += 1
        return frames.pop(0), counter

    stage = OpticalCalibrationStageAdapter(
        reserve_task=lambda label: (
            events.append(("begin", label))
            or SimpleNamespace(release=lambda: events.append(("finish",)))
        ),
        read_position=lambda: events.append(("position",)) or (10.0, 20.0, 3.0),
        raise_action=lambda action, feed: events.append(("needles", action, feed)),
        move_xy_callback=lambda x, y, *, feedrate: events.append(
            ("move", x, y, feedrate)
        ),
    )
    camera = OpticalCalibrationCameraAdapter(
        apply_lock=lambda settings: events.append(("lock", settings.enabled)) or "key",
        restore_lock=lambda key: events.append(("restore", key)) or "",
        raw_counter=lambda: counter,
        wait_raw_callback=wait_raw,
    )
    sessions = _Sessions(events)
    store = OpticalCalibrationStoreAdapter(
        lambda objective, captured, *, metadata: (
            events.append(("store", objective, len(captured), metadata["capture_grid"]))
            or SimpleNamespace(
                current_manifest="current.json", reference_image="reference.png"
            )
        )
    )
    event_adapter = OpticalCalibrationEventAdapter(
        progress_callback=lambda event: events.append(("progress", event.message)),
        completion_callback=completions.append,
        warning_callback=lambda message: events.append(("warning", message)),
    )
    if fit_error is None:
        monkeypatch.setattr(
            "probe_station_gui.camera.optical_calibration_runtime.fit_lens_artifact",
            lambda *_args, **_kwargs: SimpleNamespace(
                payload={}, before_preview=_frame(), after_preview=_frame()
            ),
        )
        monkeypatch.setattr(
            "probe_station_gui.camera.optical_calibration_runtime.lens_success_message",
            lambda *_args, **_kwargs: "Lens distortion calibration saved.",
        )
    else:
        def fail_fit(*_args, **_kwargs):
            raise fit_error

        monkeypatch.setattr(
            "probe_station_gui.camera.optical_calibration_runtime.fit_lens_artifact",
            fail_fit,
        )
    runtime = OpticalCalibrationRuntime(
        stage=stage,
        camera=camera,
        sessions=OpticalCalibrationSessionAdapter(sessions),
        store=store,
        events=event_adapter,
        thread_factory=_InlineThread,
    )
    return runtime, events, completions


def test_runtime_drives_production_adapters_with_literal_capture_positions(
    monkeypatch,
) -> None:
    runtime, flat_events, completions = _production_adapter_runtime(monkeypatch)

    runtime.start_flat(_flat_request())

    flat_moves = [event[1:3] for event in flat_events if event[0] == "move"]
    assert flat_moves == pytest.approx(
        [
            (10.0, 20.0),
            (9.98, 19.98),
            (10.0, 19.98),
            (10.02, 19.98),
            (10.02, 20.0),
            (9.98, 20.0),
            (9.98, 20.02),
            (10.0, 20.02),
            (10.02, 20.02),
            (10.0, 20.0),
        ]
    )
    assert completions[-1].success is True
    assert ("store", "X20", 9, [3, 3]) in flat_events
    assert flat_events[-3:] == [
        ("restore", "key"),
        ("finish",),
        ("session_close", "flat-field calibration"),
    ]

    runtime, lens_events, completions = _production_adapter_runtime(monkeypatch)
    runtime.start_lens(_lens_request())

    lens_moves = [event[1:3] for event in lens_events if event[0] == "move"]
    assert lens_moves == pytest.approx(
        [
            (10.0, 20.0),
            (10.0385, 19.9545),
            (10.0035, 19.965),
            (9.9685, 19.9755),
            (9.965, 20.0105),
            (10.035, 19.9895),
            (10.0315, 20.0245),
            (9.9965, 20.035),
            (9.9615, 20.0455),
            (10.0, 20.0),
        ]
    )
    assert completions[-1].success is True
    assert lens_events[-3:] == [
        ("restore", "key"),
        ("finish",),
        ("session_close", "lens distortion calibration"),
    ]


def test_production_adapter_lens_fit_failure_still_restores_every_resource(
    monkeypatch,
) -> None:
    runtime, events, completions = _production_adapter_runtime(
        monkeypatch,
        fit_error=RuntimeError("fit failed"),
    )

    runtime.start_lens(_lens_request())

    assert completions[-1].success is False
    assert "fit failed" in completions[-1].message
    assert [event[1:3] for event in events if event[0] == "move"][-1] == (
        10.0,
        20.0,
    )
    assert events[-3:] == [
        ("restore", "key"),
        ("finish",),
        ("session_close", "lens distortion calibration"),
    ]


def test_store_adapter_returns_typed_install_result() -> None:
    adapter = OpticalCalibrationStoreAdapter(
        lambda *_args, **_kwargs: SimpleNamespace(
            current_manifest="current.json",
            reference_image="reference.png",
        )
    )

    result = adapter.install("X20", [_frame()], {})

    result_type = getattr(calibration_adapters, "FlatFieldInstallResult", None)
    assert result_type is not None
    assert isinstance(result, result_type)
    assert result.current_manifest == "current.json"
    assert result.reference_image == "reference.png"


@pytest.mark.parametrize(
    "payload, expected",
    (
        ({"model_type": "stage_geometry"}, "baseline_residual_mean_px"),
        (
            {
                "model_version": 1,
                "model_type": "unsupported",
                "frame_size": [100, 50],
                "pixels_to_mm": [[-0.001, 0.0], [0.0, -0.001]],
                "calibrated_pixels_to_mm": [[-0.001, 0.0], [0.0, -0.001]],
                "center_px": [50.0, 25.0],
                "k1": 0.0,
                "k2": 0.0,
                "p1": 0.0,
                "p2": 0.0,
                "baseline_residual_mean_px": 1.0,
                "baseline_residual_max_px": 2.0,
                "residual_mean_px": 0.5,
                "residual_max_px": 1.0,
                "feature_count": 20,
                "observation_count": 40,
                "optimizer_success": True,
            },
            "model_type",
        ),
    ),
)
def test_successful_malformed_completion_discards_artifact(payload, expected) -> None:
    artifact = LensCalibrationArtifact(payload, _frame(), _frame())
    outcome = OpticalCalibrationOutcome(
        "lens", None, "lens", True, "complete", "X20", False,
        lens_artifact=artifact,
    )
    saves = []

    presentation = prepare_lens_completion(
        outcome,
        True,
        "complete",
        artifact,
        limits=LensFitLimits(),
        save=lambda *_args: saves.append(True),
    )

    assert presentation.success is False
    assert presentation.artifact is None
    assert presentation.metrics is None
    assert presentation.wizard_kwargs() == {}
    assert saves == []
    assert expected in presentation.message.lower()
