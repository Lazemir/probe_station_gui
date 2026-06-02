"""Camera acquisition worker running in a QThread."""

from __future__ import annotations

import logging
import concurrent.futures
import importlib
import queue
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Callable

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
    SETTINGS_TASK_WARNING_S = 0.25

    frame_ready: Signal = Signal(QImage)
    error: Signal = Signal(str)
    camera_settings_snapshot_ready: Signal = Signal(object)
    camera_setting_changed: Signal = Signal(object)
    camera_settings_override_changed: Signal = Signal(object)

    def __init__(self) -> None:
        super().__init__()
        self._running = False
        self._camera: Any | None = None
        self._acquiring = False
        self._frame_index = 0
        self._last_frame_timestamp: float | None = None
        self._last_frame_log_timestamp = 0.0
        self._camera_commands: queue.Queue[_CameraCommand] = queue.Queue()
        self._temporary_camera_settings: dict[str, list[dict[str, Any]]] = {}
        self._camera_settings_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="camera-settings",
        )

    def request_camera_settings_snapshot(
        self,
        map_key: str | None = None,
        node_names: list[str] | None = None,
    ) -> None:
        """Request a GenICam node snapshot from the live camera."""

        if self._camera is None:
            self.camera_settings_snapshot_ready.emit(
                {
                    "ok": False,
                    "message": "Camera is not ready.",
                    "maps": [],
                }
            )
            return
        payload: dict[str, Any] = {}
        if map_key and node_names:
            payload["map_key"] = str(map_key)
            payload["node_names"] = [str(name) for name in node_names if str(name)]
        self._camera_commands.put(_CameraCommand("snapshot", payload))

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

    def request_temporary_camera_settings(
        self,
        settings: Mapping[str, object] | Iterable[tuple[str, object]],
        *,
        map_key: str = "camera",
        restore_key: str = "default",
    ) -> None:
        """Temporarily apply GenICam settings, saving current values for restore."""

        if self._camera is None:
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
        self._camera_commands.put(
            _CameraCommand(
                "temporary_set",
                {
                    "map_key": str(map_key),
                    "restore_key": str(restore_key),
                    "settings": setting_items,
                },
            )
        )

    def request_restore_camera_settings(self, *, restore_key: str = "default") -> None:
        """Restore a previously applied temporary GenICam settings set."""

        if self._camera is None:
            self.camera_settings_override_changed.emit(
                {
                    "ok": False,
                    "message": "Camera is not ready.",
                    "restore_key": restore_key,
                }
            )
            return
        self._camera_commands.put(
            _CameraCommand(
                "temporary_restore",
                {
                    "restore_key": str(restore_key),
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
            self._camera_settings_executor.shutdown(wait=True, cancel_futures=True)
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
        self._temporary_camera_settings.clear()

    def _process_camera_commands(self) -> None:
        while True:
            try:
                command = self._camera_commands.get_nowait()
            except queue.Empty:
                return
            if command.action == "snapshot":
                self._submit_camera_task(
                    "settings snapshot",
                    self._camera_settings_snapshot,
                    command.payload,
                    self.camera_settings_snapshot_ready,
                )
            elif command.action == "set":
                self._submit_camera_task(
                    "setting update",
                    self._apply_camera_setting,
                    command.payload,
                    self.camera_setting_changed,
                )
            elif command.action == "execute":
                self._submit_camera_task(
                    "command execute",
                    self._execute_camera_command,
                    command.payload,
                    self.camera_setting_changed,
                )
            elif command.action == "temporary_set":
                self._submit_camera_task(
                    "temporary settings",
                    self._apply_temporary_camera_settings,
                    command.payload,
                    self.camera_settings_override_changed,
                )
            elif command.action == "temporary_restore":
                self._submit_camera_task(
                    "settings restore",
                    self._restore_temporary_camera_settings,
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
            signal.emit({"ok": False, "message": f"Camera {task_name} failed: {exc}"})
            return
        future.add_done_callback(
            lambda done: self._emit_camera_task_result(task_name, done, signal)
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
    ) -> None:
        try:
            result = future.result()
        except Exception as exc:  # pragma: no cover - hardware dependent
            result = {
                "ok": False,
                "message": f"Camera {task_name} failed: {exc}",
            }
        signal.emit(result)

    def _camera_settings_snapshot(
        self,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        cam = self._camera
        if cam is None:
            return {"ok": False, "message": "Camera is not ready.", "maps": []}
        payload = payload or {}
        target_map_key = str(payload.get("map_key") or "")
        node_names = [
            str(name) for name in payload.get("node_names") or [] if str(name)
        ]
        if target_map_key and node_names:
            return self._camera_settings_partial_snapshot(target_map_key, node_names)

        maps: list[dict[str, Any]] = []
        total_nodes = 0
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
        return {
            "ok": True,
            "message": f"Loaded {total_nodes} camera settings.",
            "maps": maps,
            "streaming": self._acquiring,
        }

    def _camera_settings_partial_snapshot(
        self,
        map_key: str,
        node_names: list[str],
    ) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        errors: list[str] = []
        for node_name in node_names:
            try:
                node = self._node_by_name(map_key, node_name)
                info = self._read_node_info(map_key, node)
                if info is not None:
                    nodes.append(info)
            except Exception as exc:  # pragma: no cover - hardware dependent
                errors.append(f"{node_name}: {exc}")

        return {
            "ok": True,
            "partial": True,
            "message": f"Updated {len(nodes)} camera settings.",
            "maps": [
                {
                    "key": map_key,
                    "title": self._node_map_title(map_key),
                    "nodes": nodes,
                    "error": "; ".join(errors),
                }
            ],
            "streaming": self._acquiring,
        }

    @staticmethod
    def _node_map_title(map_key: str) -> str:
        return {
            "camera": "Camera",
            "transport_device": "Transport Device",
            "transport_stream": "Transport Stream",
        }.get(map_key, map_key or "Node Map")

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
            updated = self._set_camera_setting_value(map_key, node_name, value)
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

    def _apply_temporary_camera_settings(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        map_key = str(payload.get("map_key") or "camera")
        restore_key = str(payload.get("restore_key") or "default")
        if restore_key in self._temporary_camera_settings:
            return {
                "ok": False,
                "message": f"Temporary camera settings already active: {restore_key}.",
                "restore_key": restore_key,
            }
        settings = [
            item
            for item in payload.get("settings") or []
            if isinstance(item, dict) and str(item.get("node_name") or "")
        ]
        saved_settings: list[dict[str, Any]] = []
        updated_nodes: list[dict[str, Any]] = []
        try:
            for item in settings:
                node_name = str(item.get("node_name") or "")
                value = item.get("value")
                node = self._node_by_name(map_key, node_name)
                info = self._read_node_info(map_key, node)
                if info is None:
                    raise RuntimeError(f"{node_name}: camera setting is unavailable.")
                if not info["readable"]:
                    raise RuntimeError(
                        f"{node_name}: camera setting cannot be restored."
                    )
                if not info["writable"]:
                    raise RuntimeError(f"{node_name}: camera setting is read-only.")
                saved_settings.append(
                    {
                        "map_key": map_key,
                        "node_name": node_name,
                        "value": info.get("value"),
                    }
                )
                self._set_node_value(node, str(info["type"]), value)
                updated_nodes.append(self._read_node_info(map_key, node) or info)
        except Exception as exc:  # pragma: no cover - hardware dependent
            _, rollback_errors = self._restore_camera_setting_values(
                list(reversed(saved_settings))
            )
            message = f"Camera temporary settings failed: {exc}"
            if rollback_errors:
                message = f"{message}; rollback errors: {'; '.join(rollback_errors)}"
            return {
                "ok": False,
                "message": message,
                "restore_key": restore_key,
                "nodes": updated_nodes,
                "rollback_errors": rollback_errors,
            }

        self._temporary_camera_settings[restore_key] = saved_settings
        return {
            "ok": True,
            "message": f"Applied {len(updated_nodes)} temporary camera settings.",
            "restore_key": restore_key,
            "nodes": updated_nodes,
        }

    def _restore_temporary_camera_settings(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        restore_key = str(payload.get("restore_key") or "default")
        saved_settings = self._temporary_camera_settings.get(restore_key)
        if not saved_settings:
            return {
                "ok": True,
                "message": f"No temporary camera settings active: {restore_key}.",
                "restore_key": restore_key,
                "nodes": [],
            }
        restored_nodes, errors = self._restore_camera_setting_values(
            list(reversed(saved_settings))
        )
        if errors:
            return {
                "ok": False,
                "message": f"Camera settings restore failed: {'; '.join(errors)}",
                "restore_key": restore_key,
                "nodes": restored_nodes,
                "errors": errors,
            }
        self._temporary_camera_settings.pop(restore_key, None)
        return {
            "ok": True,
            "message": f"Restored {len(restored_nodes)} camera settings.",
            "restore_key": restore_key,
            "nodes": restored_nodes,
        }

    def _restore_camera_setting_values(
        self,
        saved_settings: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        restored_nodes: list[dict[str, Any]] = []
        errors: list[str] = []
        for item in saved_settings:
            map_key = str(item.get("map_key") or "camera")
            node_name = str(item.get("node_name") or "")
            try:
                restored_nodes.append(
                    self._set_camera_setting_value(
                        map_key,
                        node_name,
                        item.get("value"),
                    )
                )
            except Exception as exc:  # pragma: no cover - hardware dependent
                errors.append(f"{node_name}: {exc}")
        return restored_nodes, errors

    def _set_camera_setting_value(
        self,
        map_key: str,
        node_name: str,
        value: object,
    ) -> dict[str, Any]:
        node = self._node_by_name(map_key, node_name)
        info = self._read_node_info(map_key, node)
        if info is None:
            raise RuntimeError("Camera setting is unavailable.")
        if not info["writable"]:
            raise RuntimeError("Camera setting is read-only.")
        self._set_node_value(node, str(info["type"]), value)
        return self._read_node_info(map_key, node) or info

    def _execute_camera_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        map_key = str(payload.get("map_key", ""))
        node_name = str(payload.get("node_name", ""))
        try:
            node = self._node_by_name(map_key, node_name)
            info = self._read_node_info(map_key, node)
            if info is None:
                raise RuntimeError("Camera command is unavailable.")
            if not info["writable"]:
                raise RuntimeError("Camera command is not writable.")
            self._execute_command_node(node)
            updated = self._read_node_info(map_key, node) or info
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
