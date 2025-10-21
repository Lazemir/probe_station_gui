"""Camera acquisition worker running in a QThread."""

from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage
from rotpy.camera import CameraList
from rotpy.system import SpinSystem


class Grabber(QObject):
    """Continuously grab frames from the first detected camera."""

    frame_ready: Signal = Signal(QImage)
    error: Signal = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._running = False

    @Slot()
    def start(self) -> None:
        self._running = True
        try:
            system = SpinSystem()
            cams = CameraList.create_from_system(system, True, True)
            if cams.get_size() < 1:
                self.error.emit("No cameras detected")
                return

            cam = cams.create_camera_by_index(0)
            cam.init_cam()
            cam.camera_nodes.PixelFormat.set_node_value_from_str("RGB8")
            cam.begin_acquisition()

            while self._running:
                try:
                    icam = cam.get_next_image(timeout=5)
                except Exception as exc:  # pragma: no cover - hardware dependent
                    self.error.emit(str(exc))
                    time.sleep(0.1)
                    continue

                img = icam.deep_copy_image(icam)
                icam.release()

                height = img.get_height()
                width = img.get_width()
                stride = img.get_stride()
                buffer = img.get_image_data()
                array = np.frombuffer(buffer, dtype=np.uint8, count=stride * height).reshape(
                    height, stride
                )
                array = array[:, : width * 3].reshape(height, width, 3)

                qimg = QImage(array.data, width, height, width * 3, QImage.Format_RGB888)
                self.frame_ready.emit(qimg.copy())

            cam.end_acquisition()
            cam.deinit_cam()
            cam.release()
        except Exception as exc:  # pragma: no cover - hardware dependent
            self.error.emit(f"init: {exc!r}")

    @Slot()
    def stop(self) -> None:
        self._running = False


__all__ = ["Grabber"]
