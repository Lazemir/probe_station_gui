import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtGui import QPainter, QPen

from probe_station_gui.camera.distortion import (
    GridCalibrationFrame,
    apply_distortion_correction,
    correction_from_payload,
    detect_bright_grid,
    distortion_payload_from_points,
    fit_distortion_from_grid_frames,
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
