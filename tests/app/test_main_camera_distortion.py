from __future__ import annotations

import threading

from PySide6.QtGui import QColor, QImage

from tests.app.import_reset import restore_real_imports_for_main


restore_real_imports_for_main()

from main import Main


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
    window._correct_camera_frame_for_active_objective = lambda frame: _tagged_copy(
        frame,
        "corrected",
    )

    Main._on_camera_frame(window, _source_image("raw"))

    assert window._latest_camera_frame is not None
    assert window._latest_camera_frame.text("tag") == "corrected"
    assert window._latest_camera_frame_for_notifications.text("tag") == "corrected"
    assert window.view.frames[-1].text("tag") == "corrected"
    assert window.stage_controller.frames[-1].text("tag") == "corrected"


def _source_image(tag: str) -> QImage:
    image = QImage(32, 24, QImage.Format_RGB32)
    image.fill(QColor("black"))
    image.setText("tag", tag)
    return image


def _tagged_copy(frame: QImage, tag: str) -> QImage:
    copy = frame.copy()
    copy.setText("tag", tag)
    return copy
