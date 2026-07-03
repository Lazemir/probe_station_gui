import pytest
from PySide6.QtGui import QColor, QImage

from probe_station_gui.camera.distortion import (
    apply_distortion_correction,
    correction_from_payload,
    distortion_payload_from_points,
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
