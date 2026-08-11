from __future__ import annotations

import pytest

from probe_station_gui.camera.optical_calibration_geometry import (
    flat_field_capture_offsets_mm,
    lens_capture_offsets_mm,
)
from probe_station_gui.camera.optical_calibration_runtime import (
    FlatFieldCalibrationRequest,
    LensCalibrationArtifact,
    OpticalCalibrationRuntime,
)
from tests.camera.optical_calibration_runtime_test_support import (
    _Camera,
    _Events,
    _FailingSession,
    _ForbiddenPort,
    _InlineThread,
    _Sessions,
    _Stage,
    _Store,
    _flat_request,
    _frame,
    _lens_request,
    _runtime,
    _valid_lens_payload,
)


@pytest.fixture(autouse=True)
def _surface_inline_worker_errors():
    _InlineThread.errors.clear()
    yield
    assert _InlineThread.errors == []


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
