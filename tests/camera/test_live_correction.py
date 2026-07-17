from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QColor, QImage

from probe_station_gui.camera.live_correction import (
    LatestFrameProcessor,
    LiveCameraCorrectionPipeline,
    LiveCameraCorrectionRequest,
)


def test_pipeline_loads_active_objective_flat_field_and_reuses_cache(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reference_path = _write_reference(tmp_path, "X20")
    _write_current_profile(tmp_path, "X20", reference_path)
    compile_calls: list[str] = []

    from probe_station_gui.camera import live_correction

    real_compile = live_correction.compile_flat_field_correction

    def counted_compile(profile):
        compile_calls.append(profile.source)
        return real_compile(profile)

    monkeypatch.setattr(
        live_correction,
        "compile_flat_field_correction",
        counted_compile,
    )
    pipeline = LiveCameraCorrectionPipeline(tmp_path, profile_refresh_s=60.0)
    request = LiveCameraCorrectionRequest(
        sequence=1,
        frame=_frame(80),
        objective_name="X20",
        distortion_configured=False,
        distortion_payload={},
    )

    first = pipeline.process(request)
    second = pipeline.process(request)

    assert first.frame.pixelColor(1, 3) != request.frame.pixelColor(1, 3)
    assert second.frame.pixelColor(1, 3) == first.frame.pixelColor(1, 3)
    assert len(compile_calls) == 1


def test_pipeline_does_not_rebuild_unchanged_flat_field_profile(
    tmp_path: Path,
    monkeypatch,
) -> None:
    reference_path = _write_reference(tmp_path, "X20")
    _write_current_profile(tmp_path, "X20", reference_path)
    build_calls: list[str] = []

    from probe_station_gui.camera import flat_field_calibration

    real_build = flat_field_calibration.build_flat_field_profile

    def counted_build(*args, **kwargs):
        build_calls.append(str(kwargs.get("source")))
        return real_build(*args, **kwargs)

    monkeypatch.setattr(
        flat_field_calibration,
        "build_flat_field_profile",
        counted_build,
    )
    pipeline = LiveCameraCorrectionPipeline(tmp_path, profile_refresh_s=0.0)
    request = LiveCameraCorrectionRequest(
        sequence=1,
        frame=_frame(80),
        objective_name="X20",
        distortion_configured=False,
        distortion_payload={},
    )

    pipeline.process(request)
    pipeline.process(request)

    assert len(build_calls) == 1


def test_pipeline_applies_flat_field_before_distortion(tmp_path: Path) -> None:
    events: list[str] = []
    flat_model = object()
    distortion_model = object()
    pipeline = LiveCameraCorrectionPipeline(
        tmp_path,
        flat_field_apply=lambda frame, model: events.append("flat") or frame,
        distortion_compile=lambda payload: events.append("compile") or distortion_model,
        distortion_apply=lambda frame, model: events.append("distortion") or frame,
    )
    pipeline._compiled_flat_field_for_objective = lambda _name: flat_model

    pipeline.process(
        LiveCameraCorrectionRequest(
            sequence=1,
            frame=_frame(100),
            objective_name="X20",
            distortion_configured=True,
            distortion_payload={"model_version": 1},
        )
    )

    assert events.index("flat") < events.index("distortion")
    assert events.count("compile") == 1


def test_pipeline_without_objective_profile_passes_frame_through(tmp_path: Path) -> None:
    source = _frame(91)
    pipeline = LiveCameraCorrectionPipeline(tmp_path)

    result = pipeline.process(
        LiveCameraCorrectionRequest(
            sequence=7,
            frame=source,
            objective_name="X5",
            distortion_configured=False,
            distortion_payload={},
        )
    )

    assert result.sequence == 7
    assert result.frame is source


def test_latest_frame_processor_replaces_stale_pending_request() -> None:
    processing_started = threading.Event()
    release_first = threading.Event()
    all_results = threading.Event()
    results: list[int] = []

    def process(value: int) -> int:
        if value == 1:
            processing_started.set()
            assert release_first.wait(2.0)
        return value

    processor = LatestFrameProcessor(process, thread_name="test-live-camera")
    processor.frame_ready.connect(
        lambda value: (results.append(value), all_results.set() if len(results) == 2 else None)
    )
    app = QCoreApplication.instance() or QCoreApplication([])
    try:
        assert processor.submit(1) is True
        assert processing_started.wait(2.0)
        assert processor.submit(2) is True
        assert processor.submit(3) is True
        release_first.set()
        deadline = time.monotonic() + 2.0
        while not all_results.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.01)
        assert all_results.is_set()
    finally:
        processor.shutdown(timeout_s=2.0)

    assert results == [1, 3]
    assert processor.is_alive() is False


def _write_reference(tmp_path: Path, objective: str) -> Path:
    profile_dir = tmp_path / "calibrations" / "flat-field" / objective / "profile"
    profile_dir.mkdir(parents=True)
    path = profile_dir / "reference.png"
    image = QImage(31, 17, QImage.Format_RGB32)
    for y in range(image.height()):
        for x in range(image.width()):
            shade = 45 + 4 * x
            image.setPixelColor(x, y, QColor(shade, shade, shade))
    assert image.save(str(path), "PNG")
    return path


def _write_current_profile(
    tmp_path: Path,
    objective: str,
    reference_path: Path,
) -> None:
    current_path = (
        tmp_path / "calibrations" / "flat-field" / objective / "current.json"
    )
    current_path.write_text(
        json.dumps(
            {
                "version": 1,
                "objective": objective,
                "reference_image": str(reference_path),
                "frame_size_px": [31, 17],
                "blur_radius_px": 9,
                "max_gain": 5.0,
                "coordinate_space": "raw camera frame, before lens distortion correction",
                "application_order": ["flat_field", "lens_distortion", "mosaic"],
            }
        ),
        encoding="utf-8",
    )


def _frame(shade: int) -> QImage:
    image = QImage(31, 17, QImage.Format_RGB32)
    image.fill(QColor(shade, shade, shade))
    return image
