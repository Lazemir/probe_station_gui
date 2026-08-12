"""Camera acquisition worker running in a QThread."""

from __future__ import annotations

import logging
import concurrent.futures
import importlib
import queue
import threading
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal, Slot
from PySide6.QtGui import QImage

from . import genicam_nodes, settings_transactions

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


@dataclass(frozen=True)
class _CameraCommand:
    action: str
    payload: dict[str, Any]


class _LifecycleState(Enum):
    NEW = auto()
    RUNNING = auto()
    ACCEPTING = auto()
    STOPPING = auto()
    STOPPED = auto()


class Grabber(QObject):
    """Continuously grab frames from the first detected camera."""

    FRAME_GAP_WARNING_S = 0.25
    FRAME_LOG_INTERVAL_S = 5.0
    FRAME_TIMEOUT_S = 0.5
    SETTINGS_TASK_WARNING_S = 0.25
    NEW_BUFFER_TIMEOUT_CODE = -1011
    NEW_BUFFER_TIMEOUT_ALERT_COUNT = 5

    frame_ready: Signal = Signal(QImage)
    frame_gap_suppressed: Signal = Signal()
    error: Signal = Signal(str)
    camera_settings_snapshot_ready: Signal = Signal(object)
    camera_setting_changed: Signal = Signal(object)
    camera_settings_batch_changed: Signal = Signal(object)
    camera_settings_override_changed: Signal = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._lifecycle_lock = threading.Lock()
        self._lifecycle_state = _LifecycleState.NEW
        self._running = False
        self._camera: Any | None = None
        self._acquiring = False
        self._frame_index = 0
        self._latest_frame_condition = threading.Condition()
        self._latest_frame: QImage | None = None
        self._latest_frame_counter = 0
        self._last_frame_timestamp: float | None = None
        self._last_frame_log_timestamp = 0.0
        self._new_buffer_timeout_count = 0
        self._new_buffer_timeout_alerted = False
        self._suppress_next_frame_gap = False
        self._camera_commands: queue.Queue[_CameraCommand] = queue.Queue()
        self._genicam_nodes: genicam_nodes.GenICamNodes | None = None
        self._settings_transactions: (
            settings_transactions.CameraSettingsTransactions | None
        ) = None
        self._camera_settings_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="camera-settings",
        )
        self._camera_settings_executor_shutdown = False

    def request_camera_settings_snapshot(
        self,
        map_key: str | None = None,
        node_names: list[str] | None = None,
        *,
        request_id: str | None = None,
    ) -> None:
        """Request a GenICam node snapshot from the live camera."""

        payload: dict[str, Any] = {}
        if request_id:
            payload["request_id"] = str(request_id)
        if map_key and node_names:
            payload["map_key"] = str(map_key)
            payload["node_names"] = [str(name) for name in node_names if str(name)]
        if not self._try_queue_camera_command(_CameraCommand("snapshot", payload)):
            self.camera_settings_snapshot_ready.emit(
                {
                    "ok": False,
                    "message": "Camera is not ready.",
                    "maps": [],
                    "request_id": request_id,
                }
            )
            return

    def request_camera_setting_update(
        self,
        map_key: str,
        node_name: str,
        value: object,
        *,
        request_id: str | None = None,
    ) -> None:
        """Request a writable GenICam node update on the camera thread."""

        command = _CameraCommand(
            "set",
            {
                "map_key": str(map_key),
                "node_name": str(node_name),
                "value": value,
                "request_id": request_id,
            },
        )
        if not self._try_queue_camera_command(command):
            self.camera_setting_changed.emit(
                {
                    "ok": False,
                    "message": "Camera is not ready.",
                    "map_key": map_key,
                    "node_name": node_name,
                    "request_id": request_id,
                }
            )
            return

    def request_camera_settings_batch(
        self,
        settings: Mapping[str, object] | Iterable[tuple[str, object]],
        *,
        request_id: str,
        map_key: str = "camera",
    ) -> None:
        """Request an ordered, atomic GenICam settings update."""

        try:
            setting_items = self._camera_setting_items(settings)
        except Exception as exc:
            self.camera_settings_batch_changed.emit(
                {
                    "ok": False,
                    "message": f"Camera settings batch invalid: {exc}",
                    "request_id": str(request_id),
                }
            )
            return
        if not setting_items:
            self.camera_settings_batch_changed.emit(
                {
                    "ok": False,
                    "message": "No camera settings provided.",
                    "request_id": str(request_id),
                }
            )
            return
        command = _CameraCommand(
            "batch_set",
            {
                "map_key": str(map_key),
                "request_id": str(request_id),
                "settings": setting_items,
            },
        )
        if not self._try_queue_camera_command(command):
            self.camera_settings_batch_changed.emit(
                {
                    "ok": False,
                    "message": "Camera is not ready.",
                    "request_id": str(request_id),
                }
            )
            return

    def request_camera_command_execute(self, map_key: str, node_name: str) -> None:
        """Request execution of a writable GenICam command node."""

        command = _CameraCommand(
            "execute",
            {
                "map_key": str(map_key),
                "node_name": str(node_name),
            },
        )
        if not self._try_queue_camera_command(command):
            self.camera_setting_changed.emit(
                {
                    "ok": False,
                    "message": "Camera is not ready.",
                    "map_key": map_key,
                    "node_name": node_name,
                }
            )
            return

    def request_temporary_camera_settings(
        self,
        settings: Mapping[str, object] | Iterable[tuple[str, object]],
        *,
        map_key: str = "camera",
        restore_key: str = "default",
    ) -> None:
        """Temporarily apply GenICam settings, saving current values for restore."""

        if not self._camera_commands_are_accepted():
            self.camera_settings_override_changed.emit(
                {
                    "ok": False,
                    "message": "Camera is not ready.",
                    "restore_key": restore_key,
                }
            )
            return
        try:
            setting_items = self._camera_setting_items(settings)
        except Exception as exc:
            self.camera_settings_override_changed.emit(
                {
                    "ok": False,
                    "message": f"Camera temporary settings invalid: {exc}",
                    "restore_key": restore_key,
                }
            )
            return
        if not setting_items:
            self.camera_settings_override_changed.emit(
                {
                    "ok": False,
                    "message": "No camera settings provided.",
                    "restore_key": restore_key,
                }
            )
            return
        command = _CameraCommand(
            "temporary_set",
            {
                "map_key": str(map_key),
                "restore_key": str(restore_key),
                "settings": setting_items,
            },
        )
        if not self._try_queue_camera_command(command):
            self.camera_settings_override_changed.emit(
                {
                    "ok": False,
                    "message": "Camera is not ready.",
                    "restore_key": restore_key,
                }
            )

    def request_restore_camera_settings(self, *, restore_key: str = "default") -> None:
        """Restore a previously applied temporary GenICam settings set."""

        command = _CameraCommand(
            "temporary_restore",
            {
                "restore_key": str(restore_key),
            },
        )
        if not self._try_queue_camera_command(command):
            self.camera_settings_override_changed.emit(
                {
                    "ok": False,
                    "message": "Camera is not ready.",
                    "restore_key": restore_key,
                }
            )
            return

    @Slot()
    def start(self) -> None:
        with self._lifecycle_lock:
            if self._lifecycle_state is not _LifecycleState.NEW:
                rejected = True
            else:
                rejected = False
                self._lifecycle_state = _LifecycleState.RUNNING
                self._running = True
        if rejected:
            self.error.emit("Camera worker cannot be restarted.")
            return
        self._last_frame_timestamp = None
        self._last_frame_log_timestamp = 0.0
        self._reset_acquisition_timeout_state()
        cam = None
        try:
            cam = self._initialize_camera()
            if cam is None:
                return

            while self._running:
                self._process_camera_commands()
                if not self._acquiring:
                    time.sleep(0.05)
                    continue

                try:
                    icam = cam.get_next_image(timeout=self.FRAME_TIMEOUT_S)
                except Exception as exc:  # pragma: no cover - hardware dependent
                    self._handle_acquisition_exception(exc)
                    time.sleep(0.1)
                    continue
                if icam is None:  # pragma: no cover - hardware dependent
                    continue

                try:
                    img = icam.deep_copy_image(icam)
                finally:
                    icam.release()
                self._emit_frame(img)

        except Exception as exc:  # pragma: no cover - hardware dependent
            self.error.emit(f"init: {exc!r}")
        finally:
            with self._lifecycle_lock:
                self._running = False
                if self._lifecycle_state is not _LifecycleState.STOPPED:
                    self._lifecycle_state = _LifecycleState.STOPPING
            self._reject_pending_camera_commands()
            self._shutdown_camera_settings_executor()
            self._shutdown_camera(self._camera)
            with self._lifecycle_lock:
                self._camera = None
                self._acquiring = False
                self._lifecycle_state = _LifecycleState.STOPPED

    def _initialize_camera(self) -> object | None:
        CameraList, SpinSystem, import_error = _load_camera_backend()
        if CameraList is None or SpinSystem is None:
            message = "Camera backend unavailable"
            if import_error is not None:
                message = f"{message}: {import_error}"
            self.error.emit(message)
            return None
        system = SpinSystem()
        cameras = CameraList.create_from_system(system, True, True)
        if cameras.get_size() < 1:
            self.error.emit("No cameras detected")
            return None

        camera = cameras.create_camera_by_index(0)
        with self._lifecycle_lock:
            self._camera = camera
        camera.init_cam()
        self._set_stream_buffer_handling_mode(camera)
        self._set_rgb8_pixel_format(camera)
        camera.begin_acquisition()
        with self._lifecycle_lock:
            self._acquiring = True
        nodes = genicam_nodes.GenICamNodes(
            camera,
            is_streaming=lambda: self._acquiring,
        )
        transactions = settings_transactions.CameraSettingsTransactions(
            nodes,
            is_streaming=lambda: self._acquiring,
        )
        with self._lifecycle_lock:
            self._genicam_nodes = nodes
            self._settings_transactions = transactions
            if self._lifecycle_state is _LifecycleState.RUNNING:
                self._lifecycle_state = _LifecycleState.ACCEPTING
        return camera

    @Slot()
    def stop(self) -> None:
        shutdown_executor = False
        with self._lifecycle_lock:
            if self._lifecycle_state is _LifecycleState.NEW:
                self._lifecycle_state = _LifecycleState.STOPPED
                shutdown_executor = True
            elif self._lifecycle_state in {
                _LifecycleState.RUNNING,
                _LifecycleState.ACCEPTING,
            }:
                self._lifecycle_state = _LifecycleState.STOPPING
            self._running = False
        if shutdown_executor:
            self._shutdown_camera_settings_executor()

    def _camera_commands_are_accepted(self) -> bool:
        with self._lifecycle_lock:
            return (
                self._lifecycle_state is _LifecycleState.ACCEPTING
                and self._camera is not None
            )

    def _try_queue_camera_command(self, command: _CameraCommand) -> bool:
        with self._lifecycle_lock:
            if (
                self._lifecycle_state is not _LifecycleState.ACCEPTING
                or self._camera is None
            ):
                return False
            self._camera_commands.put(command)
            return True

    def _shutdown_camera_settings_executor(self) -> None:
        with self._lifecycle_lock:
            if self._camera_settings_executor_shutdown:
                return
            self._camera_settings_executor_shutdown = True
        self._camera_settings_executor.shutdown(wait=True, cancel_futures=True)

    def _reject_pending_camera_commands(self) -> None:
        while True:
            try:
                command = self._camera_commands.get_nowait()
            except queue.Empty:
                return
            rejection = self._camera_command_rejection(command)
            if rejection is None:
                logger.warning("Unknown camera command discarded: %s", command.action)
                continue
            signal, result = rejection
            signal.emit(result)

    def _camera_command_rejection(
        self,
        command: _CameraCommand,
    ) -> tuple[Signal, dict[str, Any]] | None:
        payload = command.payload
        result: dict[str, Any] = {
            "ok": False,
            "message": "Camera is not ready.",
        }
        if command.action == "snapshot":
            result["maps"] = []
            result["request_id"] = payload.get("request_id")
            return self.camera_settings_snapshot_ready, result
        if command.action in {"set", "execute"}:
            result["map_key"] = payload.get("map_key")
            result["node_name"] = payload.get("node_name")
            if "request_id" in payload:
                result["request_id"] = payload.get("request_id")
            return self.camera_setting_changed, result
        if command.action == "batch_set":
            result["request_id"] = payload.get("request_id")
            return self.camera_settings_batch_changed, result
        if command.action in {"temporary_set", "temporary_restore"}:
            result["restore_key"] = payload.get("restore_key")
            return self.camera_settings_override_changed, result
        return None

    def latest_frame_counter(self) -> int:
        """Return the acquisition counter without crossing the GUI event queue."""

        with self._latest_frame_condition:
            return int(self._latest_frame_counter)

    def wait_for_frame(
        self,
        *,
        after_counter: int | None = None,
        timeout_s: float = 2.0,
    ) -> tuple[QImage | None, int]:
        """Read the latest acquired frame directly from the camera worker cache."""

        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._latest_frame_condition:
            while True:
                frame = self._latest_frame
                counter = int(self._latest_frame_counter)
                fresh_enough = after_counter is None or counter > int(after_counter)
                if frame is not None and fresh_enough:
                    return frame.copy(), counter
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    if frame is not None and after_counter is None:
                        return frame.copy(), counter
                    return None, counter
                self._latest_frame_condition.wait(min(remaining, 0.1))

    def _cache_latest_frame(self, frame: QImage) -> None:
        with self._latest_frame_condition:
            self._latest_frame = frame.copy()
            self._latest_frame_counter = int(self._frame_index)
            self._latest_frame_condition.notify_all()

    def _emit_frame(self, img: object) -> None:
        try:
            pix_fmt = str(img.get_pix_fmt())
            if pix_fmt != "RGB8":
                img = img.convert_fmt("RGB8")
            height = img.get_height()
            width = img.get_width()
            stride = img.get_stride()
            buffer = img.get_image_data()
            array = np.frombuffer(
                buffer, dtype=np.uint8, count=stride * height
            ).reshape(height, stride)
            array = array[:, : width * 3].reshape(height, width, 3)
            qimg = QImage(array.data, width, height, width * 3, QImage.Format_RGB888)
        except Exception as exc:  # pragma: no cover - hardware dependent
            self.error.emit(f"frame conversion: {exc!r}")
            return

        suppress_frame_gap = bool(self._suppress_next_frame_gap)
        self._reset_acquisition_timeout_state()
        self._frame_index += 1
        self._cache_latest_frame(qimg)
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
            and not suppress_frame_gap
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
                f"{frame_interval:.3f}s" if frame_interval is not None else "first",
            )
            self._last_frame_log_timestamp = now
        if suppress_frame_gap:
            self.frame_gap_suppressed.emit()
        self.frame_ready.emit(qimg.copy())

    def _handle_acquisition_exception(self, exc: Exception) -> bool:
        """Classify one acquisition failure and emit only actionable errors."""

        message = str(exc) or type(exc).__name__
        if not self._is_new_buffer_timeout(exc, message):
            self._reset_acquisition_timeout_state()
            self.error.emit(message)
            return True

        self._new_buffer_timeout_count += 1
        if self._new_buffer_timeout_count < self.NEW_BUFFER_TIMEOUT_ALERT_COUNT:
            self._suppress_next_frame_gap = True
            return False

        self._suppress_next_frame_gap = False
        if not self._new_buffer_timeout_alerted:
            self._new_buffer_timeout_alerted = True
            self.error.emit(message)
            return True
        return False

    def _is_new_buffer_timeout(self, exc: Exception, message: str) -> bool:
        try:
            code = int(getattr(exc, "spin_error_code"))
        except (AttributeError, TypeError, ValueError):
            return False
        return (
            code == self.NEW_BUFFER_TIMEOUT_CODE
            and "NEW_BUFFER_DATA" in str(message).upper()
        )

    def _reset_acquisition_timeout_state(self) -> None:
        self._new_buffer_timeout_count = 0
        self._new_buffer_timeout_alerted = False
        self._suppress_next_frame_gap = False

    def _set_rgb8_pixel_format(self, cam: object) -> None:
        try:
            pixel_format = cam.camera_nodes.PixelFormat
            if pixel_format.is_available() and pixel_format.is_writable():
                pixel_format.set_node_value_from_str("RGB8")
        except Exception as exc:  # pragma: no cover - hardware dependent
            logger.warning("Unable to set camera PixelFormat to RGB8: %s", exc)

    def _set_stream_buffer_handling_mode(self, cam: object) -> None:
        try:
            node_map = cam.get_tl_stream_node_map()
            node = node_map.get_node_by_name("StreamBufferHandlingMode")
            if node is None:
                logger.warning("StreamBufferHandlingMode is unavailable.")
                return
            if not node.is_available() or not node.is_writable():
                logger.warning("StreamBufferHandlingMode is not writable.")
                return
            node.set_node_value_from_str("NewestOnly")
            logger.info(
                "Camera stream buffer handling mode: %s",
                node.get_node_value_as_str(),
            )
        except Exception as exc:  # pragma: no cover - hardware dependent
            logger.warning(
                "Unable to set StreamBufferHandlingMode to NewestOnly: %s",
                exc,
            )

    def _shutdown_camera(self, cam: object | None) -> None:
        if cam is None:
            self._camera = None
            self._acquiring = False
            self._genicam_nodes = None
            self._settings_transactions = None
            return
        if self._acquiring:
            try:
                cam.end_acquisition()
            except Exception as exc:  # pragma: no cover - hardware dependent
                logger.warning("Unable to end camera acquisition: %s", exc)
        self._acquiring = False
        try:
            cam.deinit_cam()
        except Exception as exc:  # pragma: no cover - hardware dependent
            logger.warning("Unable to deinitialize camera: %s", exc)
        try:
            cam.release()
        except Exception as exc:  # pragma: no cover - hardware dependent
            logger.warning("Unable to release camera: %s", exc)
        self._camera = None
        self._genicam_nodes = None
        self._settings_transactions = None

    def _process_camera_commands(self) -> None:
        nodes = self._genicam_nodes
        transactions = self._settings_transactions
        if nodes is None or transactions is None:
            self._reject_pending_camera_commands()
            return
        while True:
            try:
                command = self._camera_commands.get_nowait()
            except queue.Empty:
                return
            if command.action == "snapshot":
                self._submit_camera_task(
                    "settings snapshot",
                    nodes.snapshot,
                    command.payload,
                    self.camera_settings_snapshot_ready,
                )
            elif command.action == "set":
                self._submit_camera_task(
                    "setting update",
                    nodes.apply_setting,
                    command.payload,
                    self.camera_setting_changed,
                )
            elif command.action == "batch_set":
                self._submit_camera_task(
                    "settings batch",
                    transactions.apply_batch,
                    command.payload,
                    self.camera_settings_batch_changed,
                )
            elif command.action == "execute":
                self._submit_camera_task(
                    "command execute",
                    nodes.execute_command,
                    command.payload,
                    self.camera_setting_changed,
                )
            elif command.action == "temporary_set":
                self._submit_camera_task(
                    "temporary settings",
                    transactions.apply_temporary,
                    command.payload,
                    self.camera_settings_override_changed,
                )
            elif command.action == "temporary_restore":
                self._submit_camera_task(
                    "settings restore",
                    transactions.restore_temporary,
                    command.payload,
                    self.camera_settings_override_changed,
                )

    def _submit_camera_task(
        self,
        task_name: str,
        func: Callable[[dict[str, Any]], dict[str, Any]],
        payload: dict[str, Any],
        signal: Signal,
    ) -> None:
        try:
            future = self._camera_settings_executor.submit(
                self._run_camera_task,
                task_name,
                func,
                dict(payload),
            )
        except RuntimeError as exc:
            result = {"ok": False, "message": f"Camera {task_name} failed: {exc}"}
            request_id = payload.get("request_id")
            if request_id is not None:
                result["request_id"] = request_id
            signal.emit(result)
            return
        future.add_done_callback(
            lambda done: self._emit_camera_task_result(
                task_name,
                done,
                signal,
                payload,
            )
        )

    def _run_camera_task(
        self,
        task_name: str,
        func: Callable[[dict[str, Any]], dict[str, Any]],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        started = time.monotonic()
        result = func(payload)
        elapsed = time.monotonic() - started
        if elapsed > self.SETTINGS_TASK_WARNING_S:
            logger.warning("Camera %s took %.3fs", task_name, elapsed)
        else:
            logger.debug("Camera %s took %.3fs", task_name, elapsed)
        return result

    def _emit_camera_task_result(
        self,
        task_name: str,
        future: concurrent.futures.Future[dict[str, Any]],
        signal: Signal,
        payload: dict[str, Any],
    ) -> None:
        try:
            result = future.result()
        except Exception as exc:  # pragma: no cover - hardware dependent
            result = {
                "ok": False,
                "message": f"Camera {task_name} failed: {exc}",
            }
        request_id = payload.get("request_id")
        if request_id is not None:
            result.setdefault("request_id", request_id)
        signal.emit(result)

    @staticmethod
    def _camera_setting_items(
        settings: Mapping[str, object] | Iterable[tuple[str, object]],
    ) -> list[dict[str, object]]:
        if isinstance(settings, Mapping):
            iterator = settings.items()
        else:
            iterator = settings
        items: list[dict[str, object]] = []
        for node_name, value in iterator:
            name = str(node_name)
            if name:
                items.append({"node_name": name, "value": value})
        return items


__all__ = ["Grabber"]
