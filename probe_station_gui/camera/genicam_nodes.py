"""Qt-free GenICam node discovery, schema, and value coercion."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class GenICamNodes:
    """Expose camera node operations without leaking GenICam type handling."""

    def __init__(
        self,
        camera: object,
        *,
        is_streaming: Callable[[], bool],
    ) -> None:
        self._camera = camera
        self._is_streaming = is_streaming

    def snapshot(
        self,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = payload or {}
        request_id = payload.get("request_id")
        target_map_key = str(payload.get("map_key") or "")
        node_names = [
            str(name) for name in payload.get("node_names") or [] if str(name)
        ]
        if target_map_key and node_names:
            result = self._partial_snapshot(target_map_key, node_names)
            if request_id is not None:
                result["request_id"] = request_id
            return result

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
        result = {
            "ok": True,
            "message": f"Loaded {total_nodes} camera settings.",
            "maps": maps,
            "streaming": bool(self._is_streaming()),
        }
        if request_id is not None:
            result["request_id"] = request_id
        return result

    def apply_setting(self, payload: dict[str, Any]) -> dict[str, Any]:
        map_key = str(payload.get("map_key", ""))
        node_name = str(payload.get("node_name", ""))
        value = payload.get("value")
        try:
            updated = self.set_value(map_key, node_name, value)
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

    def execute_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        map_key = str(payload.get("map_key", ""))
        node_name = str(payload.get("node_name", ""))
        try:
            node, info = self.inspect_setting(map_key, node_name)
            if info is None:
                raise RuntimeError("Camera command is unavailable.")
            if not info["writable"]:
                raise RuntimeError("Camera command is not writable.")
            self._execute_command_node(node)
            updated = self.describe(map_key, node) or info
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

    def inspect_setting(
        self,
        map_key: str,
        node_name: str,
    ) -> tuple[object, dict[str, Any] | None]:
        node = self._node_by_name(map_key, node_name)
        return node, self.describe(map_key, node)

    def describe(self, map_key: str, node: object) -> dict[str, Any] | None:
        return self._read_node_info(map_key, node)

    def write_setting(self, node: object, node_type: str, value: object) -> None:
        self._set_node_value(node, node_type, value)

    def set_value(
        self,
        map_key: str,
        node_name: str,
        value: object,
    ) -> dict[str, Any]:
        node, info = self.inspect_setting(map_key, node_name)
        if info is None:
            raise RuntimeError("Camera setting is unavailable.")
        if not info["writable"]:
            raise RuntimeError("Camera setting is read-only.")
        self.write_setting(node, str(info["type"]), value)
        return self.describe(map_key, node) or info

    def _partial_snapshot(
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
                error = f"{node_name}: {exc}"
                errors.append(error)
                nodes.append(
                    {
                        "map_key": map_key,
                        "name": node_name,
                        "display_name": node_name,
                        "type": "",
                        "value": "",
                        "available": False,
                        "readable": False,
                        "writable": False,
                        "entries": [],
                        "error": error,
                    }
                )

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
            "streaming": bool(self._is_streaming()),
        }

    @staticmethod
    def _node_map_title(map_key: str) -> str:
        return {
            "camera": "Camera",
            "transport_device": "Transport Device",
            "transport_stream": "Transport Stream",
        }.get(map_key, map_key or "Node Map")

    def _node_map(self, map_key: str) -> object:
        if map_key == "camera":
            return self._camera.get_node_map()  # type: ignore[attr-defined]
        if map_key == "transport_device":
            return self._camera.get_tl_dev_node_map()  # type: ignore[attr-defined]
        if map_key == "transport_stream":
            return self._camera.get_tl_stream_node_map()  # type: ignore[attr-defined]
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
        for node in node_map.get_nodes():  # type: ignore[attr-defined]
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
            "short_description": self._safe_node_string(node, "get_short_description"),
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
            node_entries = node.get_entries()  # type: ignore[attr-defined]
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

    def _set_node_value(self, node: object, node_type: str, value: object) -> None:
        if node_type == "boolean":
            node.set_node_value(self._coerce_bool(value))  # type: ignore[attr-defined]
            return
        if node_type == "integer":
            node.set_node_value(self._coerce_int(value))  # type: ignore[attr-defined]
            return
        if node_type == "float":
            node.set_node_value(float(value))  # type: ignore[attr-defined]
            return
        if node_type == "enum":
            self._set_node_value_from_str(node, str(value))
            return
        if node_type == "string":
            node.set_node_value(str(value))  # type: ignore[attr-defined]
            return
        if hasattr(node, "set_node_value_from_str"):
            self._set_node_value_from_str(node, str(value))
            return
        node.set_node_value(value)  # type: ignore[attr-defined]

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
        value = GenICamNodes._safe_node_value(node, method_name)
        if value is None:
            return ""
        return str(value)

    @staticmethod
    def _safe_node_bool(node: object, method_name: str, default: bool) -> bool:
        value = GenICamNodes._safe_node_value(node, method_name)
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


__all__ = ["GenICamNodes"]
