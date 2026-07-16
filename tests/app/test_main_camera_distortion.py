from __future__ import annotations

import threading
import logging
import time
from types import SimpleNamespace

from PySide6.QtGui import QColor, QImage

from tests.app.import_reset import restore_real_imports_for_main


restore_real_imports_for_main()

from main import Main
from probe_station_gui.camera.live_correction import LiveCameraCorrectionResult


class FakeView:
    def __init__(self) -> None:
        self.frames: list[QImage] = []

    def set_frame(self, frame: QImage) -> None:
        self.frames.append(frame.copy())


class FakeStageController:
    def __init__(self) -> None:
        self.frames: list[QImage] = []

    def on_frame_ready(self, frame: QImage) -> None:
        self.frames.append(frame.copy())


class FakeFrameProcessor:
    def __init__(self) -> None:
        self.requests: list[object] = []

    def submit(self, request: object) -> bool:
        self.requests.append(request)
        return True


def test_camera_frame_pipeline_sends_corrected_frame_to_view_and_stage() -> None:
    window = Main.__new__(Main)
    window._last_camera_frame_ui_timestamp = None
    window.CAMERA_UI_FRAME_GAP_WARNING_S = 999.0
    window._latest_camera_frame_condition = threading.Condition()
    window._latest_camera_frame = None
    window._latest_camera_frame_counter = 0
    window._latest_camera_frame_for_notifications = None
    window.view = FakeView()
    window.stage_controller = FakeStageController()
    window.settings_manager = SimpleNamespace(
        active_objective_configuration=lambda: SimpleNamespace(
            name="X20",
            distortion_correction_configured=True,
            distortion_correction={"model_version": 1},
        )
    )
    window._live_camera_frame_processor = FakeFrameProcessor()

    Main._on_camera_frame(window, _source_image("raw"))

    assert window._latest_raw_camera_frame is not None
    assert window._latest_raw_camera_frame.text("tag") == "raw"
    assert window._latest_raw_camera_frame_counter == 1
    assert window._latest_camera_frame is None
    assert window.view.frames == []

    request = window._live_camera_frame_processor.requests[-1]
    Main._on_live_camera_frame_processed(
        window,
        LiveCameraCorrectionResult(
            sequence=request.sequence,
            frame=_tagged_copy(request.frame, "corrected"),
        ),
    )

    assert window._latest_camera_frame is not None
    assert window._latest_camera_frame.text("tag") == "corrected"
    assert window._latest_camera_frame_for_notifications.text("tag") == "corrected"
    assert window.view.frames[-1].text("tag") == "corrected"
    assert window.stage_controller.frames[-1].text("tag") == "corrected"


def test_recovered_transient_timeout_suppresses_one_ui_gap_warning(caplog) -> None:
    window = Main.__new__(Main)
    window._last_camera_frame_ui_timestamp = time.monotonic() - 1.0
    window.CAMERA_UI_FRAME_GAP_WARNING_S = 0.25
    window._latest_camera_frame_condition = threading.Condition()
    window._latest_camera_frame = None
    window._latest_camera_frame_counter = 0
    window._latest_raw_camera_frame = None
    window._latest_raw_camera_frame_counter = 0
    window._latest_camera_frame_for_notifications = None
    window._suppress_next_camera_ui_gap = True
    window.view = FakeView()
    window.stage_controller = FakeStageController()

    with caplog.at_level(logging.WARNING):
        Main._on_live_camera_frame_processed(
            window,
            LiveCameraCorrectionResult(
                sequence=1,
                frame=_source_image("corrected"),
                suppress_gap_warning=True,
            ),
        )

    assert "Camera UI frame gap" not in caplog.text
    assert window._suppress_next_camera_ui_gap is False


def _source_image(tag: str) -> QImage:
    image = QImage(32, 24, QImage.Format_RGB32)
    image.fill(QColor("black"))
    image.setText("tag", tag)
    return image


def _tagged_copy(frame: QImage, tag: str) -> QImage:
    copy = frame.copy()
    copy.setText("tag", tag)
    return copy
