"""Camera acquisition worker running in a QThread."""

from __future__ import annotations

import logging
import importlib
import queue
import time
from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True)
class _CameraCommand:
    action: str
    payload: dict[str, Any]


class Grabber(QObject):
    """Continuously grab frames from the first detected camera."""

    FRAME_GAP_WARNING_S = 0.25
    FRAME_LOG_INTERVAL_S = 5.0
    FRAME_TIMEOUT_S = 0.5

    frame_ready: Signal = Signal(QImage)
    error: Signal = Signal(str)
    camera_settings_snapshot_ready: Signal = Signal(object)
    camera_setting_changed: Signal = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._running = False
        self._camera: Any | None = None
        self._acquiring = False
        self._frame_index = 0
        self._last_frame_timestamp: float | None = None
        self._last_frame_log_timestamp = 0.0
        self._camera_commands: queue.Queue[_CameraCommand] = queue.Queue()

    def request_camera_settings_snapshot(self) -> None:
        """Request a full GenICam node snapshot from the live camera."""

        if self._camera is None:
            self.camera_settings_snapshot_ready.emit(
                {
                    "ok": False,
                    "message": "Camera is not ready.",
                    "maps": [],
                }
            )
            return
        self._camera_commands.put(_CameraCommand("snapshot", {}))

    def request_camera_setting_update(
        self,
        map_key: str,
        node_name: str,
        value: object,
    ) -> None:
        """Request a writable GenICam node update on the camera thread."""

        if self._camera is None:
            self.camera_setting_changed.emit(
                {
                    "ok": False,
                    "message": "Camera is not ready.",
                    "map_key": map_key,
                    "node_name": node_name,
                }
            )
            return
        self._camera_commands.put(
            _CameraCommand(
                "set",
                {
                    "map_key": str(map_key),
                    "node_name": str(node_name),
                    "value": value,
                },
            )
        )

    def request_camera_command_execute(self, map_key: str, node_name: str) -> None:
        """Request execution of a writable GenICam command node."""

        if self._camera is None:
            self.camera_setting_changed.emit(
                {
                    "ok": False,
                    "message": "Camera is not ready.",
                    "map_key": map_key,
                    "node_name": node_name,
                }
            )
            return
        self._camera_commands.put(
            _CameraCommand(
                "execute",
                {
                    "map_key": str(map_key),
                    "node_name": str(node_name),
                },
            )
        )

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
            self._running = False
            return
        cam = None
        try:
            system = SpinSystem()
            cams = CameraList.create_from_system(system, True, True)
            if cams.get_size() < 1:
                self.error.emit("No cameras detected")
                self._running = False
                return

            cam = cams.create_camera_by_index(0)
            self._camera = cam
            cam.init_cam()
            self._set_rgb8_pixel_format(cam)
            cam.begin_acquisition()
            self._acquiring = True

            while self._running:
                self._process_camera_commands()
                if not self._acquiring:
                    time.sleep(0.05)
                    continue

                try:
                    icam = cam.get_next_image(timeout=self.FRAME_TIMEOUT_S)
                except Exception as exc:  # pragma: no cover - hardware dependent
                    self.error.emit(str(exc))
                    time.sleep(0.1)
                    continue
                if icam is None:  # pragma: no cover - hardware dependent
                    continue

                img = icam.deep_copy_image(icam)
                icam.release()
                self._emit_frame(img)

        except Exception as exc:  # pragma: no cover - hardware dependent
            self.error.emit(f"init: {exc!r}")
        finally:
            self._running = False
            self._shutdown_camera(cam)

    @Slot()
    def stop(self) -> None:
        self._running = False

    def _emit_frame(self, img: object) -> None:
        try:
            pix_fmt = str(img.get_pix_fmt())
            if pix_fmt != "RGB8":
                img = img.convert_fmt("RGB8")
            height = img.get_height()
            width = img.get_width()
            stride = img.get_stride()
            buffer = img.get_image_data()
            array = np.frombuffer(buffer, dtype=np.uint8, count=stride * height).reshape(
                height, stride
            )
            array = array[:, : width * 3].reshape(height, width, 3)
            qimg = QImage(array.data, width, height, width * 3, QImage.Format_RGB888)
        except Exception as exc:  # pragma: no cover - hardware dependent
            self.error.emit(f"frame conversion: {exc!r}")
            return

        self._frame_index += 1
        now = time.monotonic()
        frame_interval = (
            now - self._last_frame_timestamp
            if self._last_frame_timestamp is not None
            else None
        )
        self._last_frame_timestamp = now
        if frame_interval is not None and frame_interval > self.FRAME_GAP_WARNING_S:
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
        self.frame_ready.emit(qimg.copy())

    def _set_rgb8_pixel_format(self, cam: object) -> None:
        try:
            pixel_format = cam.camera_nodes.PixelFormat
            if pixel_format.is_available() and pixel_format.is_writable():
                pixel_format.set_node_value_from_str("RGB8")
        except Exception as exc:  # pragma: no cover - hardware dependent
            logger.warning("Unable to set camera PixelFormat to RGB8: %s", exc)

    def _shutdown_camera(self, cam: object | None) -> None:
        if cam is None:
            self._camera = None
            self._acquiring = False
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

    def _process_camera_commands(self) -> None:
        while True:
            try:
                command = self._camera_commands.get_nowait()
            except queue.Empty:
                return
            if command.action == "snapshot":
                self.camera_settings_snapshot_ready.emit(
                    self._camera_settings_snapshot()
                )
            elif command.action == "set":
                self.camera_setting_changed.emit(
                    self._apply_camera_setting(command.payload)
                )
            elif command.action == "execute":
                self.camera_setting_changed.emit(
                    self._execute_camera_command(command.payload)
                )

    def _camera_settings_snapshot(self) -> dict[str, Any]:
        cam = self._camera
        if cam is None:
            return {"ok": False, "message": "Camera is not ready.", "maps": []}

        maps: list[dict[str, Any]] = []
        total_nodes = 0
        paused = self._pause_acquisition()
        try:
            for map_key, title in (
                ("camera", "Camera"),
                ("transport_device", "Transport Device"),
                ("transport_stream", "Transport Stream"),
            ):
                try:
                    node_map = self._node_map(map_key)
                    nodes = self._read_node_map(map_key, node_map)
                    maps.append(
                        {
                            "key": map_key,
                            "title": title,
                            "nodes": nodes,
                            "error": "",
                        }
                    )
                    total_nodes += len(nodes)
                except Exception as exc:  # pragma: no cover - hardware dependent
                    maps.append(
                        {
                            "key": map_key,
                            "title": title,
                            "nodes": [],
                            "error": str(exc),
                        }
                    )
        finally:
            self._resume_acquisition(paused)
        return {
            "ok": True,
            "message": f"Loaded {total_nodes} camera settings.",
            "maps": maps,
            "streaming": paused,
        }

    def _node_map(self, map_key: str) -> object:
        cam = self._camera
        if cam is None:
            raise RuntimeError("Camera is not ready.")
        if map_key == "camera":
            return cam.get_node_map()
        if map_key == "transport_device":
            return cam.get_tl_dev_node_map()
        if map_key == "transport_stream":
            return cam.get_tl_stream_node_map()
        raise KeyError(f"Unknown camera node map: {map_key}")

    def _node_by_name(self, map_key: str, node_name: str) -> object:
        node_map = self._node_map(map_key)
        getter = getattr(node_map, "get_node_by_name", None)
        if getter is not None:
            node = getter(node_name)
            if node is not None:
                return node
        try:
            return getattr(node_map, node_name)
        except AttributeError as exc:
            raise KeyError(node_name) from exc

    def _read_node_map(self, map_key: str, node_map: object) -> list[dict[str, Any]]:
        nodes: list[dict[str, Any]] = []
        for node in node_map.get_nodes():
            info = self._read_node_info(map_key, node)
            if info is None:
                continue
            nodes.append(info)
        return nodes

    def _read_node_info(
        self,
        map_key: str,
        node: object,
    ) -> dict[str, Any] | None:
        name = self._safe_node_string(node, "get_name")
        if not name:
            return None
        implemented = self._safe_node_bool(node, "is_implemented", True)
        if not implemented:
            return None
        available = self._safe_node_bool(node, "is_available", False)
        readable = self._safe_node_bool(node, "is_readable", False)
        writable = self._safe_node_bool(node, "is_writable", False)
        node_type = self._node_type(node)
        info: dict[str, Any] = {
            "map_key": map_key,
            "name": name,
            "display_name": self._safe_node_string(node, "get_display_name") or name,
            "type": node_type,
            "class_name": type(node).__name__,
            "value": "",
            "available": available,
            "readable": readable,
            "writable": writable,
            "visibility": self._safe_node_string(node, "get_visibility"),
            "description": self._safe_node_string(node, "get_description"),
            "short_description": self._safe_node_string(
                node, "get_short_description"
            ),
            "tooltip": self._safe_node_string(node, "get_tooltip"),
            "unit": self._safe_node_string(node, "get_unit"),
            "representation": self._safe_node_string(node, "get_representation"),
            "minimum": self._safe_node_value(node, "get_min_value"),
            "maximum": self._safe_node_value(node, "get_max_value"),
            "increment": self._safe_node_value(node, "get_inc"),
            "entries": [],
            "children": [],
        }
        if readable:
            info["value"] = self._safe_node_string(node, "get_node_value_as_str")
        if node_type == "enum":
            info["entries"] = self._enum_entries(node)
        if node_type == "category":
            info["children"] = self._category_children(node)
        return info

    def _node_type(self, node: object) -> str:
        class_name = type(node).__name__
        class_types = {
            "SpinBoolNode": "boolean",
            "SpinCategoryNode": "category",
            "SpinCommandNode": "command",
            "SpinEnumNode": "enum",
            "SpinFloatNode": "float",
            "SpinIntNode": "integer",
            "SpinRegisterNode": "register",
            "SpinStrNode": "string",
            "SpinValueNode": "value",
        }
        if class_name in class_types:
            return class_types[class_name]
        value = self._safe_node_string(node, "get_node_type")
        lowered = value.lower()
        if "boolean" in lowered:
            return "boolean"
        if "category" in lowered:
            return "category"
        if "command" in lowered:
            return "command"
        if "enumeration" in lowered or lowered == "enum":
            return "enum"
        if "float" in lowered:
            return "float"
        if "integer" in lowered or lowered == "int":
            return "integer"
        if "string" in lowered or lowered == "str":
            return "string"
        if "register" in lowered:
            return "register"
        return value or class_name

    def _enum_entries(self, node: object) -> list[str]:
        entries: list[str] = []
        try:
            node_entries = node.get_entries()
        except Exception:
            return entries
        for entry in node_entries:
            if not self._safe_node_bool(entry, "is_implemented", True):
                continue
            if not self._safe_node_bool(entry, "is_available", True):
                continue
            name = self._safe_node_string(entry, "get_enum_name")
            if name:
                entries.append(name)
        return entries

    def _category_children(self, node: object) -> list[str]:
        children: list[str] = []
        for method_name in ("get_features", "get_children", "get_nodes"):
            method = getattr(node, method_name, None)
            if method is None:
                continue
            try:
                child_nodes = method()
            except Exception:
                continue
            for child in child_nodes:
                if isinstance(child, str):
                    name = child
                else:
                    name = self._safe_node_string(child, "get_name")
                if name and name not in children:
                    children.append(name)
            if children:
                break
        return children

    def _apply_camera_setting(self, payload: dict[str, Any]) -> dict[str, Any]:
        map_key = str(payload.get("map_key", ""))
        node_name = str(payload.get("node_name", ""))
        value = payload.get("value")
        try:
            paused = self._pause_acquisition()
            try:
                node = self._node_by_name(map_key, node_name)
                info = self._read_node_info(map_key, node)
                if info is None:
                    raise RuntimeError("Camera setting is unavailable.")
                if not info["writable"]:
                    raise RuntimeError("Camera setting is read-only.")
                self._set_node_value(node, str(info["type"]), value)
                updated = self._read_node_info(map_key, node) or info
            finally:
                self._resume_acquisition(paused)
        except Exception as exc:  # pragma: no cover - hardware dependent
            return {
                "ok": False,
                "message": f"{node_name}: {exc}",
                "map_key": map_key,
                "node_name": node_name,
            }
        return {
            "ok": True,
            "message": f"{updated['display_name']} updated.",
            "map_key": map_key,
            "node_name": node_name,
            "node": updated,
        }

    def _execute_camera_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        map_key = str(payload.get("map_key", ""))
        node_name = str(payload.get("node_name", ""))
        try:
            paused = self._pause_acquisition()
            try:
                node = self._node_by_name(map_key, node_name)
                info = self._read_node_info(map_key, node)
                if info is None:
                    raise RuntimeError("Camera command is unavailable.")
                if not info["writable"]:
                    raise RuntimeError("Camera command is not writable.")
                self._execute_command_node(node)
                updated = self._read_node_info(map_key, node) or info
            finally:
                self._resume_acquisition(paused)
        except Exception as exc:  # pragma: no cover - hardware dependent
            return {
                "ok": False,
                "message": f"{node_name}: {exc}",
                "map_key": map_key,
                "node_name": node_name,
            }
        return {
            "ok": True,
            "message": f"{info['display_name']} executed.",
            "map_key": map_key,
            "node_name": node_name,
            "node": updated,
        }

    def _pause_acquisition(self) -> bool:
        cam = self._camera
        was_acquiring = self._acquiring and cam is not None
        if was_acquiring:
            cam.end_acquisition()
            self._acquiring = False
        return was_acquiring

    def _resume_acquisition(self, was_acquiring: bool) -> None:
        cam = self._camera
        if was_acquiring and self._running and cam is not None:
            cam.begin_acquisition()
            self._acquiring = True

    def _set_node_value(self, node: object, node_type: str, value: object) -> None:
        if node_type == "boolean":
            node.set_node_value(self._coerce_bool(value))
            return
        if node_type == "integer":
            node.set_node_value(self._coerce_int(value))
            return
        if node_type == "float":
            node.set_node_value(float(value))
            return
        if node_type == "enum":
            self._set_node_value_from_str(node, str(value))
            return
        if node_type == "string":
            node.set_node_value(str(value))
            return
        if hasattr(node, "set_node_value_from_str"):
            self._set_node_value_from_str(node, str(value))
            return
        node.set_node_value(value)

    @staticmethod
    def _set_node_value_from_str(node: object, value: str) -> None:
        method = getattr(node, "set_node_value_from_str")
        try:
            method(value, verify=True)
        except TypeError:
            method(value)

    @staticmethod
    def _execute_command_node(node: object) -> None:
        method = getattr(node, "execute_node")
        try:
            method(verify=True)
        except TypeError:
            method()

    @staticmethod
    def _coerce_bool(value: object) -> bool:
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on", "enabled"}:
            return True
        if text in {"0", "false", "no", "off", "disabled"}:
            return False
        raise ValueError(f"Invalid boolean value: {value!r}")

    @staticmethod
    def _coerce_int(value: object) -> int:
        if isinstance(value, int):
            return value
        text = str(value).strip()
        if text.lower().startswith("0x"):
            return int(text, 16)
        return int(float(text))

    @staticmethod
    def _safe_node_string(node: object, method_name: str) -> str:
        value = Grabber._safe_node_value(node, method_name)
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _safe_node_bool(node: object, method_name: str, default: bool) -> bool:
        value = Grabber._safe_node_value(node, method_name)
        if value is None:
            return default
        return bool(value)

    @staticmethod
    def _safe_node_value(node: object, method_name: str) -> object | None:
        method = getattr(node, method_name, None)
        if method is None:
            return None
        try:
            return method()
        except Exception:
            return None


__all__ = ["Grabber"]
