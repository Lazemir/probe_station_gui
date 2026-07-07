import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtGui import QPainter, QPen

from probe_station_gui.camera import distortion as distortion_module
from probe_station_gui.camera.distortion import (
    GridCalibrationFrame,
    RadialDistortionModel,
    StageFeatureObservation,
    apply_distortion_correction,
    apply_radial_distortion_correction,
    correction_from_payload,
    detect_bright_feature_bounds,
    detect_bright_grid,
    distortion_payload_from_points,
    fit_seam_radial_distortion,
    fit_distortion_from_grid_frames,
    fit_stage_geometry_from_grid_frames,
    fit_stage_geometry_from_observations,
    _projection_line_centers,
)


def test_apply_distortion_correction_keeps_image_size() -> None:
    image = QImage(80, 60, QImage.Format_RGB32)
    image.fill(QColor("black"))
    payload = distortion_payload_from_points(
        frame_size=(80, 60),
        source_points=[
            (10.0, 10.0),
            (70.0, 10.0),
            (10.0, 50.0),
            (70.0, 50.0),
        ],
        target_points=[
            (12.0, 11.0),
            (68.0, 10.0),
            (11.0, 48.0),
            (69.0, 49.0),
        ],
        grid_spacing_um=50.0,
    )

    corrected = apply_distortion_correction(image, correction_from_payload(payload))

    assert corrected.width() == 80
    assert corrected.height() == 60
    assert corrected.format() == QImage.Format_RGB32


def test_apply_distortion_correction_preserves_rgb888_frame_format() -> None:
    image = QImage(80, 60, QImage.Format_RGB888)
    image.fill(QColor("black"))
    payload = distortion_payload_from_points(
        frame_size=(80, 60),
        source_points=[
            (10.0, 10.0),
            (70.0, 10.0),
            (10.0, 50.0),
            (70.0, 50.0),
        ],
        target_points=[
            (12.0, 11.0),
            (68.0, 10.0),
            (11.0, 48.0),
            (69.0, 49.0),
        ],
        grid_spacing_um=50.0,
    )

    corrected = apply_distortion_correction(image, correction_from_payload(payload))

    assert corrected.width() == 80
    assert corrected.height() == 60
    assert corrected.format() == QImage.Format_RGB888


def test_identity_radial_distortion_model_leaves_pixels_unchanged() -> None:
    image = np.zeros((24, 32, 3), dtype=np.uint8)
    image[5:18, 7:11] = (240, 20, 10)
    image[10:13, 4:28] = (20, 230, 40)
    model = RadialDistortionModel(
        frame_size=(32, 24),
        center_px=(17.0, 11.0),
        k1=0.0,
        k2=0.0,
    )

    corrected = apply_radial_distortion_correction(image, model)

    assert np.array_equal(corrected, image)


def test_seam_radial_distortion_fit_uses_dual_annealing(monkeypatch) -> None:
    reference = np.zeros((40, 60, 3), dtype=np.uint8)
    reference[:, 20:24] = 255
    shifted = reference.copy()
    scale = distortion_module.MicroscopeScaleCalibration(
        pixel_size_x_um=1000.0,
        pixel_size_y_um=1000.0,
    )
    tiles = (
        (
            distortion_module.MicroscopeScanTile(
                index=1,
                row=0,
                column=0,
                stage_xy=(0.0, 0.0),
                label="control",
            ),
            reference,
        ),
        (
            distortion_module.MicroscopeScanTile(
                index=2,
                row=0,
                column=1,
                stage_xy=(-0.03, 0.0),
                label="right",
            ),
            shifted,
        ),
    )
    calls: list[tuple[tuple[tuple[float, float], ...], float]] = []

    def fake_dual_annealing(objective, bounds, **kwargs):
        vector = np.array(
            [
                (bounds[0][0] + bounds[0][1]) * 0.5,
                (bounds[1][0] + bounds[1][1]) * 0.5,
                0.0,
                0.0,
            ],
            dtype=float,
        )
        score = float(objective(vector))
        calls.append((tuple(bounds), score))
        return type(
            "Result",
            (),
            {
                "x": vector,
                "fun": score,
                "success": True,
                "nfev": 1,
                "message": "ok",
            },
        )()

    monkeypatch.setattr(distortion_module, "_dual_annealing", fake_dual_annealing)

    fit = fit_seam_radial_distortion(
        tiles,
        scale,
        maxiter=3,
        optimization_scale=1.0,
        seed=7,
    )

    assert calls
    bounds, score = calls[0]
    assert len(bounds) == 4
    assert bounds[0][0] < 30.0 < bounds[0][1]
    assert bounds[1][0] < 20.0 < bounds[1][1]
    assert np.isfinite(score)
    assert fit.model.center_px == pytest.approx((30.0, 20.0))
    assert fit.optimizer == "dual_annealing"


def test_seam_radial_distortion_fit_keeps_identity_when_optimizer_is_worse(
    monkeypatch,
) -> None:
    reference = np.zeros((40, 60, 3), dtype=np.uint8)
    reference[:, 20:24] = 255
    shifted = reference.copy()
    scale = distortion_module.MicroscopeScaleCalibration(
        pixel_size_x_um=1000.0,
        pixel_size_y_um=1000.0,
    )
    tiles = (
        (
            distortion_module.MicroscopeScanTile(
                index=1,
                row=0,
                column=0,
                stage_xy=(0.0, 0.0),
                label="control",
            ),
            reference,
        ),
        (
            distortion_module.MicroscopeScanTile(
                index=2,
                row=0,
                column=1,
                stage_xy=(-0.03, 0.0),
                label="right",
            ),
            shifted,
        ),
    )

    def fake_dual_annealing(objective, bounds, **kwargs):
        baseline = objective((30.0, 20.0, 0.0, 0.0))
        return type(
            "Result",
            (),
            {
                "x": np.array([24.0, 16.0, 0.05, -0.02], dtype=float),
                "fun": float(baseline) + 1.0,
                "success": True,
                "nfev": 1,
                "message": "worse",
            },
        )()

    monkeypatch.setattr(distortion_module, "_dual_annealing", fake_dual_annealing)

    fit = fit_seam_radial_distortion(
        tiles,
        scale,
        maxiter=3,
        optimization_scale=1.0,
        seed=7,
    )

    assert not fit.success
    assert fit.optimized_score == pytest.approx(fit.baseline_score)
    assert fit.model.center_px == pytest.approx((30.0, 20.0))
    assert fit.model.k1 == pytest.approx(0.0)
    assert fit.model.k2 == pytest.approx(0.0)


def test_stage_geometry_fit_recovers_matrix_from_unknown_feature_points() -> None:
    frame_size = (220, 180)
    true_matrix = np.array(
        [
            [-0.00122, 0.000035],
            [0.000018, -0.00117],
        ],
        dtype=float,
    )
    initial_matrix = (
        (-0.00115, 0.000010),
        (0.000005, -0.00122),
    )
    true_center = (116.0, 84.0)
    true_params = {
        "center_px": true_center,
        "k1": 0.030,
        "k2": -0.014,
        "p1": 0.0035,
        "p2": -0.0025,
    }
    stage_positions = [
        (0.0, 0.0),
        (-0.070, -0.055),
        (0.0, -0.055),
        (0.070, -0.055),
        (-0.070, 0.0),
        (0.070, 0.0),
        (-0.070, 0.055),
        (0.0, 0.055),
        (0.070, 0.055),
    ]
    feature_world = {
        f"f{index}": (x_mm, y_mm)
        for index, (x_mm, y_mm) in enumerate(
            (
                (-0.070, -0.045),
                (0.000, -0.045),
                (0.070, -0.045),
                (-0.070, 0.000),
                (0.000, 0.000),
                (0.070, 0.000),
                (-0.070, 0.045),
                (0.000, 0.045),
                (0.070, 0.045),
            )
        )
    }
    observations: list[StageFeatureObservation] = []
    frame_center = np.array([frame_size[0] * 0.5, frame_size[1] * 0.5], dtype=float)
    inverse_matrix = np.linalg.inv(true_matrix)
    for frame_index, stage_xy in enumerate(stage_positions):
        stage = np.asarray(stage_xy, dtype=float)
        for feature_id, world_xy in feature_world.items():
            corrected_delta = inverse_matrix @ (stage - np.asarray(world_xy, dtype=float))
            corrected_px = frame_center + corrected_delta
            if (
                corrected_px[0] < 18.0
                or corrected_px[0] > frame_size[0] - 18.0
                or corrected_px[1] < 18.0
                or corrected_px[1] > frame_size[1] - 18.0
            ):
                continue
            observed_px = _inverse_synthetic_geometry_point(
                corrected_px,
                frame_size=frame_size,
                **true_params,
            )
            observations.append(
                StageFeatureObservation(
                    frame_index=frame_index,
                    feature_id=feature_id,
                    stage_xy=stage_xy,
                    pixel_xy=(float(observed_px[0]), float(observed_px[1])),
                )
            )

    fit = fit_stage_geometry_from_observations(
        observations,
        frame_size=frame_size,
        initial_pixels_to_mm=initial_matrix,
        max_nfev=500,
    )

    assert fit.observation_count == len(observations)
    assert fit.feature_count >= 5
    assert fit.baseline_residual_mean_px > fit.residual_mean_px * 4.0
    assert fit.residual_mean_px < 0.35
    assert fit.pixels_to_mm[0][0] == pytest.approx(true_matrix[0, 0], rel=0.025)
    assert fit.pixels_to_mm[1][1] == pytest.approx(true_matrix[1, 1], rel=0.025)
    assert 0.0 <= fit.center_px[0] <= frame_size[0]
    assert 0.0 <= fit.center_px[1] <= frame_size[1]


def test_stage_geometry_payload_applies_to_qimage() -> None:
    observations = [
        StageFeatureObservation(0, "a", (0.0, 0.0), (35.0, 25.0)),
        StageFeatureObservation(1, "a", (0.010, 0.0), (25.0, 25.0)),
        StageFeatureObservation(0, "b", (0.0, 0.0), (35.0, 45.0)),
        StageFeatureObservation(1, "b", (0.010, 0.0), (25.0, 45.0)),
        StageFeatureObservation(0, "c", (0.0, 0.0), (55.0, 45.0)),
        StageFeatureObservation(1, "c", (0.010, 0.0), (45.0, 45.0)),
    ]
    fit = fit_stage_geometry_from_observations(
        observations,
        frame_size=(80, 60),
        initial_pixels_to_mm=((-0.001, 0.0), (0.0, -0.001)),
        optimize_distortion=False,
    )
    image = QImage(80, 60, QImage.Format_RGB32)
    image.fill(QColor("black"))

    corrected = apply_distortion_correction(image, correction_from_payload(fit.to_payload()))

    assert corrected.width() == 80
    assert corrected.height() == 60
    assert corrected.format() == QImage.Format_RGB32
    assert fit.to_payload()["model_type"] == "stage_geometry"


def test_stage_geometry_payload_rejects_invalid_pixels_matrix() -> None:
    payload = {
        "model_version": 1,
        "model_type": "stage_geometry",
        "frame_size": [80, 60],
        "pixels_to_mm": None,
        "calibrated_pixels_to_mm": [[-0.001, 0.0], [0.0, -0.001]],
    }

    with pytest.raises(ValueError, match="valid pixels_to_mm"):
        correction_from_payload(payload)


def test_stage_geometry_fit_from_grid_frames_does_not_need_grid_spacing() -> None:
    frames = [
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(360, 260),
                vertical_lines=(90.0, 180.0, 270.0),
                horizontal_lines=(70.0, 150.0, 230.0),
            ),
            stage_offset_mm=(0.0, 0.0),
        ),
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(360, 260),
                vertical_lines=(130.0, 220.0, 310.0),
                horizontal_lines=(70.0, 150.0, 230.0),
            ),
            stage_offset_mm=(-0.040, 0.0),
        ),
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(360, 260),
                vertical_lines=(90.0, 180.0, 270.0),
                horizontal_lines=(115.0, 195.0),
            ),
            stage_offset_mm=(0.0, -0.045),
        ),
    ]

    fit = fit_stage_geometry_from_grid_frames(
        frames,
        frame_size=(360, 260),
        pixels_to_mm=((-0.0010, 0.0), (0.0, -0.0010)),
        max_nfev=200,
    )

    assert fit.feature_count >= 6
    assert fit.residual_mean_px < 1.0
    assert fit.pixels_to_mm[0][0] == pytest.approx(-0.0010, rel=0.15)
    assert fit.pixels_to_mm[1][1] == pytest.approx(-0.0010, rel=0.15)


def test_correction_from_payload_rejects_frame_size_mismatch() -> None:
    payload = distortion_payload_from_points(
        frame_size=(80, 60),
        source_points=[
            (10.0, 10.0),
            (70.0, 10.0),
            (10.0, 50.0),
            (70.0, 50.0),
        ],
        target_points=[
            (10.0, 10.0),
            (70.0, 10.0),
            (10.0, 50.0),
            (70.0, 50.0),
        ],
        grid_spacing_um=50.0,
    )
    image = QImage(81, 60, QImage.Format_RGB32)

    with pytest.raises(ValueError, match="frame size"):
        apply_distortion_correction(image, correction_from_payload(payload))


def test_detect_bright_grid_finds_visible_lines() -> None:
    image = _synthetic_grid_image(
        frame_size=(240, 180),
        vertical_lines=(40.0, 105.0, 170.0),
        horizontal_lines=(35.0, 95.0, 155.0),
    )

    detection = detect_bright_grid(image)

    assert len(detection.vertical_lines_px) == 3
    assert len(detection.horizontal_lines_px) == 3
    assert len(detection.intersections_px) == 9
    assert detection.vertical_lines_px[1] == pytest.approx(105.0, abs=2.0)
    assert detection.horizontal_lines_px[1] == pytest.approx(95.0, abs=2.0)


def test_projection_line_centers_prefers_strong_grid_bands() -> None:
    projection = np.zeros(1200, dtype=float)
    for center in (120, 540, 960):
        projection[center - 8 : center + 9] = 1.0
    for center in range(180, 900, 80):
        projection[center : center + 2] = 0.45

    centers = _projection_line_centers(projection)

    assert centers == pytest.approx((120, 540, 960), abs=1.0)


def test_projection_line_centers_rejects_non_grid_edge_band() -> None:
    projection = np.zeros(1500, dtype=float)
    for center in (27, 102, 530, 958, 1386):
        projection[center - 7 : center + 8] = 1.0

    centers = _projection_line_centers(projection)

    assert centers == pytest.approx((102, 530, 958, 1386), abs=1.0)


def test_detect_bright_grid_prefers_expected_grid_pitch_over_fine_comb() -> None:
    image = _synthetic_grid_image(
        frame_size=(360, 220),
        vertical_lines=(70.0, 180.0, 290.0),
        horizontal_lines=(55.0, 155.0),
    )
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(QPen(QColor("#fff2a0"), 7, Qt.SolidLine, Qt.RoundCap))
        for x_pos in (20.0, 45.0, 70.0, 95.0, 120.0, 145.0):
            painter.drawLine(QPointF(x_pos, 0.0), QPointF(x_pos, 220.0))
    finally:
        painter.end()

    detection = detect_bright_grid(image, expected_spacing_px=(110.0, 100.0))

    assert detection.vertical_lines_px == pytest.approx((70.0, 180.0, 290.0), abs=3.0)


def test_partial_grid_frames_fit_distortion_payload() -> None:
    frames = [
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(320, 240),
                vertical_lines=(42.0, 112.0, 182.0),
                horizontal_lines=(45.0, 115.0, 185.0),
            ),
            stage_offset_mm=(0.0, 0.0),
        ),
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(320, 240),
                vertical_lines=(75.0, 145.0, 215.0),
                horizontal_lines=(45.0, 115.0, 185.0),
            ),
            stage_offset_mm=(0.05, 0.0),
        ),
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(320, 240),
                vertical_lines=(42.0, 112.0, 182.0),
                horizontal_lines=(72.0, 142.0, 212.0),
            ),
            stage_offset_mm=(0.0, 0.05),
        ),
    ]

    payload = fit_distortion_from_grid_frames(frames, frame_size=(320, 240))

    assert payload["model_version"] == 1
    assert payload["frame_size"] == [320, 240]
    assert len(payload["source_points"]) >= 12
    assert payload["residual_max_px"] < 3.0


def test_grid_fit_uses_expected_pitch_for_grid_selection_only() -> None:
    frames = [
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(600, 420),
                vertical_lines=(80.0, 200.0, 320.0, 440.0),
                horizontal_lines=(70.0, 190.0, 310.0),
            ),
            stage_offset_mm=(0.0, 0.0),
        )
    ]

    payload = fit_distortion_from_grid_frames(
        frames,
        frame_size=(600, 420),
        pixels_to_mm=((-0.0005, 0.0), (0.0, -0.0005)),
    )

    assert payload["axis_source_x"] == pytest.approx((80.0, 200.0, 320.0, 440.0))
    assert payload["axis_target_x"] == pytest.approx((80.0, 200.0, 320.0, 440.0))
    assert payload["axis_source_y"] == pytest.approx((70.0, 190.0, 310.0))
    assert payload["axis_target_y"] == pytest.approx((70.0, 190.0, 310.0))


def test_detect_bright_feature_bounds_prefers_center_structure() -> None:
    image = QImage(640, 420, QImage.Format_RGB32)
    image.fill(QColor("#303020"))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(QPen(QColor("#fff2a0"), 5, Qt.SolidLine, Qt.RoundCap))
        for x_pos in range(250, 391, 28):
            painter.drawLine(QPointF(float(x_pos), 145.0), QPointF(float(x_pos), 275.0))
        for y_pos in range(160, 261, 25):
            painter.drawLine(QPointF(230.0, float(y_pos)), QPointF(410.0, float(y_pos)))
        painter.setPen(QPen(QColor("#fffbd0"), 18, Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(QPointF(612.0, 20.0), QPointF(612.0, 400.0))
    finally:
        painter.end()

    bounds = detect_bright_feature_bounds(image)

    assert bounds is not None
    assert bounds.left == pytest.approx(228.0, abs=18.0)
    assert bounds.right == pytest.approx(413.0, abs=18.0)
    assert bounds.top == pytest.approx(143.0, abs=18.0)
    assert bounds.bottom == pytest.approx(278.0, abs=18.0)


def test_grid_fit_reports_grid_pixel_matrix_estimate_from_known_grid_pitch() -> None:
    frames = [
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(620, 420),
                vertical_lines=(80.0, 200.0, 320.0, 440.0),
                horizontal_lines=(70.0, 170.0, 270.0, 370.0),
            ),
            stage_offset_mm=(0.0, 0.0),
        )
    ]
    original = ((-0.00048, 0.00010), (-0.00014, -0.00049))

    payload = fit_distortion_from_grid_frames(
        frames,
        frame_size=(620, 420),
        pixels_to_mm=original,
    )

    estimate = payload["grid_pixels_to_mm_estimate"]
    x_norm = float(np.hypot(estimate[0][0], estimate[1][0]))
    y_norm = float(np.hypot(estimate[0][1], estimate[1][1]))
    assert payload["grid_line_spacing_px"] == pytest.approx([120.0, 100.0])
    assert payload["grid_pixel_size_um"] == pytest.approx([50.0 / 120.0, 0.5])
    assert x_norm == pytest.approx(0.05 / 120.0)
    assert y_norm == pytest.approx(0.05 / 100.0)
    assert estimate[0][0] < 0.0
    assert estimate[1][1] < 0.0


def test_grid_fit_reports_calibrated_pixel_matrix_from_stage_offsets() -> None:
    frames = [
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(620, 420),
                vertical_lines=(80.0, 200.0, 320.0, 440.0),
                horizontal_lines=(70.0, 170.0, 270.0, 370.0),
            ),
            stage_offset_mm=(0.0, 0.0),
        ),
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(620, 420),
                vertical_lines=(80.0, 200.0, 320.0, 440.0),
                horizontal_lines=(70.0, 170.0, 270.0, 370.0),
            ),
            stage_offset_mm=(0.05, 0.0),
        ),
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(620, 420),
                vertical_lines=(80.0, 200.0, 320.0, 440.0),
                horizontal_lines=(70.0, 170.0, 270.0, 370.0),
            ),
            stage_offset_mm=(0.0, 0.05),
        ),
    ]
    original = ((-0.00050, 0.0), (0.0, -0.00050))

    payload = fit_distortion_from_grid_frames(
        frames,
        frame_size=(620, 420),
        grid_spacing_um=60.0,
        pixels_to_mm=original,
    )

    estimate = payload["grid_pixels_to_mm_estimate"]
    calibrated = payload["calibrated_pixels_to_mm"]
    estimate_x_norm = float(np.hypot(estimate[0][0], estimate[1][0]))
    calibrated_x_norm = float(np.hypot(calibrated[0][0], calibrated[1][0]))
    calibrated_y_norm = float(np.hypot(calibrated[0][1], calibrated[1][1]))
    assert estimate_x_norm == pytest.approx(0.06 / 120.0)
    assert calibrated_x_norm == pytest.approx(0.05 / 120.0)
    assert calibrated_y_norm == pytest.approx(0.05 / 100.0)
    assert payload["calibrated_stage_spacing_mm"] == pytest.approx([0.05, 0.05])


def test_grid_fit_ignores_large_capture_offsets_for_pixel_matrix() -> None:
    frames = [
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(620, 420),
                vertical_lines=(80.0, 200.0, 320.0, 440.0),
                horizontal_lines=(70.0, 170.0, 270.0, 370.0),
            ),
            stage_offset_mm=(0.0, 0.0),
        ),
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(620, 420),
                vertical_lines=(80.0, 200.0, 320.0, 440.0),
                horizontal_lines=(70.0, 170.0, 270.0, 370.0),
            ),
            stage_offset_mm=(0.65, 0.0),
        ),
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(620, 420),
                vertical_lines=(80.0, 200.0, 320.0, 440.0),
                horizontal_lines=(70.0, 170.0, 270.0, 370.0),
            ),
            stage_offset_mm=(0.0, 0.50),
        ),
    ]

    payload = fit_distortion_from_grid_frames(
        frames,
        frame_size=(620, 420),
        pixels_to_mm=((-0.00050, 0.0), (0.0, -0.00050)),
    )

    assert "grid_pixels_to_mm_estimate" in payload
    assert "calibrated_pixels_to_mm" not in payload
    assert "calibrated_stage_spacing_mm" not in payload


def test_distorted_single_grid_frame_uses_axis_mapping() -> None:
    frames = [
        GridCalibrationFrame(
            frame=_synthetic_grid_image(
                frame_size=(340, 240),
                vertical_lines=(35.0, 70.0, 205.0, 260.0),
                horizontal_lines=(40.0, 120.0, 205.0),
            ),
            stage_offset_mm=(0.0, 0.0),
        )
    ]

    payload = fit_distortion_from_grid_frames(frames, frame_size=(340, 240))

    assert payload["axis_source_x"] == pytest.approx((35.0, 70.0, 205.0, 260.0))
    assert payload["axis_target_x"] == pytest.approx((35.0, 110.0, 185.0, 260.0))
    assert payload["axis_source_y"] == pytest.approx((40.0, 120.0, 205.0))
    assert payload["residual_max_px"] == pytest.approx(0.0)
    correction = correction_from_payload(payload)
    assert correction.axis_source_x == pytest.approx((35.0, 70.0, 205.0, 260.0))


def test_axis_interpolation_maps_are_reused_during_apply(monkeypatch) -> None:
    payload = fit_distortion_from_grid_frames(
        [
            GridCalibrationFrame(
                frame=_synthetic_grid_image(
                    frame_size=(120, 90),
                    vertical_lines=(20.0, 40.0, 80.0, 100.0),
                    horizontal_lines=(20.0, 45.0, 75.0),
                ),
                stage_offset_mm=(0.0, 0.0),
            )
        ],
        frame_size=(120, 90),
    )
    correction = correction_from_payload(payload)
    image = QImage(120, 90, QImage.Format_RGB32)
    image.fill(QColor("black"))

    def fail_if_recomputed(*_args, **_kwargs):
        raise AssertionError("axis maps should be precomputed")

    monkeypatch.setattr(
        distortion_module,
        "_axis_interpolation_maps",
        fail_if_recomputed,
    )

    apply_distortion_correction(image, correction)
    apply_distortion_correction(image, correction)


def _synthetic_grid_image(
    *,
    frame_size: tuple[int, int],
    vertical_lines: tuple[float, ...],
    horizontal_lines: tuple[float, ...],
) -> QImage:
    image = QImage(frame_size[0], frame_size[1], QImage.Format_RGB32)
    image.fill(QColor("#101010"))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setPen(QPen(QColor("#fff2a0"), 7, Qt.SolidLine, Qt.RoundCap))
        for x_pos in vertical_lines:
            painter.drawLine(
                QPointF(float(x_pos), 0.0),
                QPointF(float(x_pos), float(frame_size[1])),
            )
        for y_pos in horizontal_lines:
            painter.drawLine(
                QPointF(0.0, float(y_pos)),
                QPointF(float(frame_size[0]), float(y_pos)),
            )
    finally:
        painter.end()
    return image


def _inverse_synthetic_geometry_point(
    corrected_px: np.ndarray,
    *,
    frame_size: tuple[int, int],
    center_px: tuple[float, float],
    k1: float,
    k2: float,
    p1: float,
    p2: float,
) -> np.ndarray:
    observed = np.asarray(corrected_px, dtype=float).copy()
    target = np.asarray(corrected_px, dtype=float)
    for _ in range(10):
        mapped = _synthetic_geometry_correct_point(
            observed,
            frame_size=frame_size,
            center_px=center_px,
            k1=k1,
            k2=k2,
            p1=p1,
            p2=p2,
        )
        observed += target - mapped
    return observed


def _synthetic_geometry_correct_point(
    pixel_xy: np.ndarray,
    *,
    frame_size: tuple[int, int],
    center_px: tuple[float, float],
    k1: float,
    k2: float,
    p1: float,
    p2: float,
) -> np.ndarray:
    radius = max(float(frame_size[0]), float(frame_size[1])) * 0.5
    x = (float(pixel_xy[0]) - float(center_px[0])) / radius
    y = (float(pixel_xy[1]) - float(center_px[1])) / radius
    r2 = x * x + y * y
    radial = 1.0 + float(k1) * r2 + float(k2) * r2 * r2
    x_corr = x * radial + 2.0 * float(p1) * x * y + float(p2) * (r2 + 2.0 * x * x)
    y_corr = y * radial + float(p1) * (r2 + 2.0 * y * y) + 2.0 * float(p2) * x * y
    return np.array(
        [
            float(center_px[0]) + x_corr * radius,
            float(center_px[1]) + y_corr * radius,
        ],
        dtype=float,
    )
