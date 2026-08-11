from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtGui import QColor, QImage

import probe_station_gui.camera.geometry_alignment_preview as geometry_alignment_preview
import probe_station_gui.camera.geometry_feature_tracking as geometry_feature_tracking
import probe_station_gui.camera.geometry_segmentation as geometry_segmentation
from probe_station_gui.camera import optical_calibration_geometry as geometry
from probe_station_gui.camera.distortion import (
    GridCalibrationFrame,
    StageGeometryCorrection,
)


def _frame(color: int = 100) -> QImage:
    image = QImage(100, 50, QImage.Format_RGB32)
    image.fill(QColor(color, color, color))
    return image


def _valid_payload(**changes: object) -> dict[str, object]:
    payload: dict[str, object] = {
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
    payload.update(changes)
    return payload


def test_fit_orchestrates_masks_image_y_convention_and_preview_order(monkeypatch) -> None:
    frames = (
        GridCalibrationFrame(_frame(90), (0.0, 0.0)),
        GridCalibrationFrame(_frame(110), (0.1, -0.2)),
    )
    persisted_matrix = ((-0.001, 0.0002), (-0.0003, -0.002))
    image_matrix = ((-0.001, -0.0002), (-0.0003, 0.002))
    masks = (object(), object())
    observations = (object(),)
    before = _frame(70)
    after = _frame(130)
    calls: list[str] = []

    def segment(frame):
        index = 0 if frame is frames[0].frame else 1
        calls.append(f"segment-{index}")
        return masks[index]

    def build_observations(items, item_masks, **kwargs):
        calls.append("observations")
        assert tuple(items) == frames
        assert tuple(item_masks) == masks
        assert kwargs == {
            "frame_size": (100, 50),
            "image_pixels_to_mm": image_matrix,
            "match_gate_px": 12.0,
        }
        return observations

    image_payload = _valid_payload(
        pixels_to_mm=[[-0.001, -0.0002], [-0.0003, 0.002]],
        calibrated_pixels_to_mm=[[-0.001, -0.0002], [-0.0003, 0.002]],
    )

    def fit(items, **kwargs):
        calls.append("fit")
        assert items is observations
        assert kwargs == {
            "frame_size": (100, 50),
            "initial_pixels_to_mm": image_matrix,
        }
        return SimpleNamespace(to_payload=lambda: dict(image_payload))

    correction = StageGeometryCorrection(
        frame_size=(100, 50),
        pixels_to_mm=persisted_matrix,
        center_px=(50.0, 25.0),
        feature_count=20,
        observation_count=40,
    )

    def previews(items, item_masks, matrix, payload):
        calls.append("previews")
        assert tuple(items) == frames
        assert tuple(item_masks) == masks
        assert matrix == persisted_matrix
        assert payload["pixels_to_mm"] == [list(row) for row in persisted_matrix]
        return before, after

    monkeypatch.setattr(geometry_segmentation, "segment_metal_geometry", segment)
    monkeypatch.setattr(
        geometry_feature_tracking,
        "build_geometry_feature_observations",
        build_observations,
    )
    monkeypatch.setattr(geometry, "fit_stage_geometry_from_observations", fit)
    monkeypatch.setattr(geometry, "correction_from_payload", lambda _payload: correction)
    monkeypatch.setattr(
        geometry_alignment_preview,
        "build_geometry_alignment_previews",
        previews,
    )

    artifact = geometry.fit_lens_artifact(
        frames,
        size_px=(100, 50),
        pixels_to_mm=persisted_matrix,
        limits=geometry.LensFitLimits(),
    )

    assert calls == ["segment-0", "segment-1", "observations", "fit", "previews"]
    assert artifact.before_preview is before
    assert artifact.after_preview is after
    assert artifact.payload["pixels_to_mm"] == [list(row) for row in persisted_matrix]
    assert artifact.payload["calibrated_pixels_to_mm"] == [
        list(row) for row in persisted_matrix
    ]


def test_fit_rejects_missing_click_calibration_before_segmentation(monkeypatch) -> None:
    monkeypatch.setattr(
        geometry_segmentation,
        "segment_metal_geometry",
        lambda _frame: pytest.fail("segmentation must not run"),
    )

    with pytest.raises(RuntimeError, match="requires click-to-move calibration"):
        geometry.fit_lens_artifact(
            (GridCalibrationFrame(_frame(), (0.0, 0.0)),),
            size_px=(100, 50),
            pixels_to_mm=((0.0, 0.0), (0.0, 0.0)),
            limits=geometry.LensFitLimits(),
        )


def test_validation_previews_and_success_message_use_persisted_residuals() -> None:
    payload = _valid_payload()
    limits = geometry.LensFitLimits()

    geometry.validate_lens_payload(payload, limits)
    metrics = geometry.validate_lens_previews(payload, _frame(), _frame(), limits)
    message = geometry.lens_success_message(payload, limits)

    assert metrics.without_calibration == (1.5, 2.5)
    assert metrics.with_calibration == (0.5, 1.0)
    assert "0.50 px mean" in message
    assert "1.00 px max" in message


def test_validation_allows_high_finite_baseline_metrics() -> None:
    payload = _valid_payload(
        baseline_residual_mean_px=50.0,
        baseline_residual_max_px=100.0,
    )

    geometry.validate_lens_payload(payload, geometry.LensFitLimits())


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (_valid_payload(residual_mean_px=-0.1), "negative"),
        (_valid_payload(residual_max_px=float("nan")), "not finite"),
    ),
)
def test_validation_rejects_negative_or_nonfinite_residuals(payload, message) -> None:
    with pytest.raises(RuntimeError, match=message):
        geometry.validate_lens_payload(payload, geometry.LensFitLimits())


def test_fit_propagates_preview_generation_failure(monkeypatch) -> None:
    frames = (GridCalibrationFrame(_frame(), (0.0, 0.0)),)
    monkeypatch.setattr(
        geometry_segmentation,
        "segment_metal_geometry",
        lambda _frame: object(),
    )
    monkeypatch.setattr(
        geometry_feature_tracking,
        "build_geometry_feature_observations",
        lambda *_args, **_kwargs: (),
    )
    monkeypatch.setattr(
        geometry,
        "fit_stage_geometry_from_observations",
        lambda *_args, **_kwargs: SimpleNamespace(to_payload=_valid_payload),
    )
    monkeypatch.setattr(geometry, "validate_lens_payload", lambda *_args: None)

    def fail_previews(*_args, **_kwargs):
        raise RuntimeError("preview generation failed")

    monkeypatch.setattr(
        geometry_alignment_preview,
        "build_geometry_alignment_previews",
        fail_previews,
    )

    with pytest.raises(RuntimeError, match="preview generation failed"):
        geometry.fit_lens_artifact(
            frames,
            size_px=(100, 50),
            pixels_to_mm=((-0.001, 0.0002), (-0.0003, -0.002)),
            limits=geometry.LensFitLimits(),
        )


def test_fit_propagates_stage_geometry_failure(monkeypatch) -> None:
    frames = (GridCalibrationFrame(_frame(), (0.0, 0.0)),)
    monkeypatch.setattr(
        geometry_segmentation,
        "segment_metal_geometry",
        lambda _frame: object(),
    )
    monkeypatch.setattr(
        geometry_feature_tracking,
        "build_geometry_feature_observations",
        lambda *_args, **_kwargs: (),
    )

    def fail_fit(*_args, **_kwargs):
        raise RuntimeError("stage geometry fit failed")

    monkeypatch.setattr(geometry, "fit_stage_geometry_from_observations", fail_fit)
    monkeypatch.setattr(
        geometry_alignment_preview,
        "build_geometry_alignment_previews",
        lambda *_args, **_kwargs: pytest.fail("preview must not run after fit failure"),
    )

    with pytest.raises(RuntimeError, match="stage geometry fit failed"):
        geometry.fit_lens_artifact(
            frames,
            size_px=(100, 50),
            pixels_to_mm=((-0.001, 0.0002), (-0.0003, -0.002)),
            limits=geometry.LensFitLimits(),
        )


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        (_valid_payload(model_type="unsupported"), "model_type"),
        (_valid_payload(residual_mean_px=3.1), "residual is too high"),
    ),
)
def test_validation_rejects_unsupported_or_excessive_results(payload, message) -> None:
    with pytest.raises(RuntimeError, match=message):
        geometry.validate_lens_payload(payload, geometry.LensFitLimits())
