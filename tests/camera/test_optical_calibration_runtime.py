from __future__ import annotations

import threading
from dataclasses import replace
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QColor, QImage

from probe_station_gui.camera.exposure_policy import ExposurePolicyBusyError
from probe_station_gui.camera.optical_calibration_lifecycle import (
    OpticalCalibrationLifecycle,
)
from probe_station_gui.camera.optical_calibration_runtime import (
    FlatFieldCalibrationRequest,
    LensCalibrationArtifact,
    LensDistortionCalibrationRequest,
    OpticalCalibrationRuntime,
)
from probe_station_gui.camera.optical_calibration_geometry import (
    flat_field_capture_offsets_mm,
    lens_capture_offsets_mm,
)
from probe_station_gui.camera.optical_calibration_adapters import (
    OpticalCalibrationCameraAdapter,
    OpticalCalibrationStageAdapter,
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
    def __init__(self, *, target, **_kwargs) -> None:
        self._target = target
        self._alive = False

    def start(self) -> None:
        self._alive = True
        try:
            self._target()
        finally:
            self._alive = False

    def is_alive(self) -> bool:
        return self._alive

    def join(self, _timeout=None) -> None:
        return None


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


class _Camera:
    def __init__(self, events: list[object], frames: list[QImage]) -> None:
        self.events = events
        self.frames = list(frames)
        self.counter = 0
        self.restore_warning = ""

    def lock(self) -> str:
        self.events.append(("camera_lock",))
        return "camera-key"

    def restore(self, key: str) -> str:
        self.events.append(("camera_restore", key))
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


def test_full_wizard_reuses_parent_session_across_flat_then_lens(monkeypatch) -> None:
    events: list[object] = []
    sessions = _Sessions(events)
    runtime, emitted = _runtime(
        events,
        frames=[_frame() for _ in range(20)],
        sessions=sessions,
    )
    artifact = LensCalibrationArtifact({}, _frame(), _frame())
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
    artifact = LensCalibrationArtifact(
        {"model_type": "stage_geometry"}, _frame(), _frame()
    )
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
        thread_factory=_DeferredThread,
        sleep=lambda _seconds: (_ for _ in ()).throw(
            AssertionError("shutdown exceeded its timeout")
        ),
    )
    child = lifecycle.open_child_session(
        "flat-field calibration", _flat_request(full_wizard=True)
    )
    lifecycle.close_child(child)

    assert lifecycle.shutdown(0.0) is False
    sessions.leases[0].busy = False
    assert lifecycle.shutdown(0.1) is True


def test_production_stage_and_camera_adapters_preserve_binding_order() -> None:
    events: list[object] = []
    frame = _frame()
    stage = OpticalCalibrationStageAdapter(
        begin_task=lambda label: events.append(("begin", label)),
        read_position=lambda: events.append(("position",)) or (1.0, 2.0, 3.0),
        raise_action=lambda action, feed: events.append(("needles", action, feed)),
        move_xy_callback=lambda x, y, *, feedrate: events.append(
            ("move", x, y, feedrate)
        ),
        finish_task=lambda: events.append(("finish",)),
    )
    camera = OpticalCalibrationCameraAdapter(
        apply_lock=lambda settings: events.append(("lock", settings.enabled)) or "key",
        restore_lock=lambda key: events.append(("restore", key)) or "",
        raw_counter=lambda: 7,
        wait_raw_callback=lambda **kwargs: (
            events.append(("frame", kwargs)) or (frame, 8)
        ),
    )

    stage.reserve("flat-field calibration")
    assert stage.start_position() == (1.0, 2.0)
    key = camera.lock()
    stage.raise_needles(70.0)
    captured = camera.wait_raw(after_counter=7, timeout_s=2.0)
    stage.move_xy(3.0, 4.0, 120.0)
    assert camera.restore(key) == ""
    stage.release()

    assert captured is frame
    assert events == [
        ("begin", "flat-field calibration"),
        ("position",),
        ("lock", True),
        ("needles", "raise", 70.0),
        ("frame", {"after_counter": 7, "timeout_s": 2.0}),
        ("move", 3.0, 4.0, 120.0),
        ("restore", "key"),
        ("finish",),
    ]
