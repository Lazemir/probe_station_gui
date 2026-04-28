"""Camera acquisition worker running in a QThread."""

from __future__ import annotations

import logging
import importlib
import time

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage

logger = logging.getLogger(__name__)


class _LazyModule:
    def __init__(self, module_name: str) -> None:
        self._module_name = module_name
        self._module: object | None = None

    def __getattr__(self, name: str) -> object:
        if self._module is None:
            self._module = importlib.import_module(self._module_name)
        return getattr(self._module, name)


np = _LazyModule("numpy")


def _load_camera_backend():
    try:  # pragma: no cover - optional runtime dependency
        from rotpy.camera import CameraList
        from rotpy.system import SpinSystem
    except Exception as exc:  # pragma: no cover - optional runtime dependency
        return None, None, exc
    return CameraList, SpinSystem, None


class Grabber(QObject):
    """Continuously grab frames from the first detected camera."""

    FRAME_GAP_WARNING_S = 0.25
    FRAME_LOG_INTERVAL_S = 5.0

    frame_ready: Signal = Signal(QImage)
    error: Signal = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self._running = False
        self._frame_index = 0
        self._last_frame_timestamp: float | None = None
        self._last_frame_log_timestamp = 0.0

    @Slot()
    def start(self) -> None:
        self._running = True
        self._last_frame_timestamp = None
        self._last_frame_log_timestamp = 0.0
        CameraList, SpinSystem, import_error = _load_camera_backend()
        if CameraList is None or SpinSystem is None:
            message = "Camera backend unavailable"
            if import_error is not None:
                message = f"{message}: {import_error}"
            self.error.emit(message)
            return
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
                self._frame_index += 1
                now = time.monotonic()
                frame_interval = (
                    now - self._last_frame_timestamp
                    if self._last_frame_timestamp is not None
                    else None
                )
                self._last_frame_timestamp = now
                if (
                    frame_interval is not None
                    and frame_interval > self.FRAME_GAP_WARNING_S
                ):
                    logger.warning(
                        "Camera frame gap %.3fs before index=%s size=%sx%s",
                        frame_interval,
                        self._frame_index,
                        width,
                        height,
                    )
                    self._last_frame_log_timestamp = now
                elif now - self._last_frame_log_timestamp >= self.FRAME_LOG_INTERVAL_S:
                    logger.debug(
                        "TIMING camera_frame_ready index=%s size=%sx%s interval=%s",
                        self._frame_index,
                        width,
                        height,
                        (
                            f"{frame_interval:.3f}s"
                            if frame_interval is not None
                            else "first"
                        ),
                    )
                    self._last_frame_log_timestamp = now
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
