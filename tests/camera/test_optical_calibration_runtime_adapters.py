from __future__ import annotations

from types import SimpleNamespace

import pytest

from probe_station_gui.camera import (
    optical_calibration_adapters as calibration_adapters,
)
from probe_station_gui.camera.optical_calibration_adapters import (
    OpticalCalibrationCameraAdapter,
    OpticalCalibrationEventAdapter,
    OpticalCalibrationSessionAdapter,
    OpticalCalibrationStageAdapter,
    OpticalCalibrationStoreAdapter,
    prepare_lens_completion,
)
from probe_station_gui.camera.optical_calibration_geometry import LensFitLimits
from probe_station_gui.camera.optical_calibration_runtime import (
    LensCalibrationArtifact,
    OpticalCalibrationOutcome,
    OpticalCalibrationRuntime,
)
from tests.camera.optical_calibration_runtime_test_support import (
    _InlineThread,
    _Sessions,
    _flat_request,
    _frame,
    _lens_request,
)


@pytest.fixture(autouse=True)
def _surface_inline_worker_errors():
    _InlineThread.errors.clear()
    yield
    assert _InlineThread.errors == []


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
