from __future__ import annotations

import threading

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QColor, QImage

from tests.app.import_reset import restore_real_imports_for_main


restore_real_imports_for_main()

from main import Main


def test_camera_api_frame_selects_raw_and_corrected_storage() -> None:
    window = Main.__new__(Main)
    window._latest_camera_frame_condition = threading.Condition()
    window._latest_raw_camera_frame = _solid_image("red")
    window._latest_raw_camera_frame_counter = 11
    window._latest_camera_frame = _solid_image("blue")
    window._latest_camera_frame_counter = 13

    raw = Main._api_camera_frame(window, "raw", None, 0.0)
    corrected = Main._api_camera_frame(window, "corrected", None, 0.0)

    raw_image = QImage.fromData(QByteArray(raw["data"]), "PNG")
    corrected_image = QImage.fromData(QByteArray(corrected["data"]), "PNG")
    assert raw["counter"] == 11
    assert corrected["counter"] == 13
    assert raw_image.pixelColor(0, 0) == QColor("red")
    assert corrected_image.pixelColor(0, 0) == QColor("blue")


def test_camera_api_submitters_forward_request_ids_and_order() -> None:
    class FakeGrabber:
        def __init__(self) -> None:
            self.calls = []

        def request_camera_settings_snapshot(self, *args, **kwargs) -> None:
            self.calls.append(("snapshot", args, kwargs))

        def request_camera_settings_batch(self, *args, **kwargs) -> None:
            self.calls.append(("batch", args, kwargs))

    window = Main.__new__(Main)
    window.grabber = FakeGrabber()

    Main._submit_camera_settings_snapshot(window, "read-1", ["Gain"])
    Main._submit_camera_settings_batch(
        window,
        "write-1",
        [("ExposureAuto", "Off"), ("ExposureTime", 1800.0)],
    )

    assert window.grabber.calls == [
        (
            "snapshot",
            ("camera", ["Gain"]),
            {"request_id": "read-1"},
        ),
        (
            "batch",
            ([("ExposureAuto", "Off"), ("ExposureTime", 1800.0)],),
            {"request_id": "write-1", "map_key": "camera"},
        ),
    ]


def _solid_image(color: str) -> QImage:
    image = QImage(8, 6, QImage.Format_RGB32)
    image.fill(QColor(color))
    return image
