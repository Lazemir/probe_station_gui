"""Atomic and temporary camera-settings transactions."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from . import genicam_nodes


class CameraSettingsTransactions:
    """Apply ordered node changes with rollback and temporary restoration."""

    def __init__(
        self,
        nodes: genicam_nodes.GenICamNodes,
        *,
        is_streaming: Callable[[], bool],
    ) -> None:
        self._nodes = nodes
        self._is_streaming = is_streaming
        self._temporary_settings: dict[str, list[dict[str, Any]]] = {}

    def apply_batch(self, payload: dict[str, Any]) -> dict[str, Any]:
        map_key = str(payload.get("map_key") or "camera")
        request_id = str(payload.get("request_id") or "")
        settings = [
            item
            for item in payload.get("settings") or []
            if isinstance(item, dict) and str(item.get("node_name") or "")
        ]
        node_names = [str(item.get("node_name") or "") for item in settings]
        if not settings:
            return {
                "ok": False,
                "message": "No camera settings provided.",
                "request_id": request_id,
                "nodes": [],
                "rollback_errors": [],
            }
        duplicate_names = sorted(
            {name for name in node_names if node_names.count(name) > 1}
        )
        if duplicate_names:
            return {
                "ok": False,
                "message": f"Duplicate camera settings: {', '.join(duplicate_names)}.",
                "request_id": request_id,
                "nodes": [],
                "rollback_errors": [],
            }

        prepared: list[tuple[dict[str, Any], object, dict[str, Any]]] = []
        try:
            for item in settings:
                node_name = str(item.get("node_name") or "")
                node, info = self._nodes.inspect_setting(map_key, node_name)
                if info is None or not info["available"]:
                    raise RuntimeError(f"{node_name}: camera setting is unavailable.")
                if not info["readable"]:
                    raise RuntimeError(
                        f"{node_name}: camera setting cannot be restored."
                    )
                if not info["writable"]:
                    raise RuntimeError(f"{node_name}: camera setting is read-only.")
                prepared.append((item, node, info))
        except Exception as exc:  # pragma: no cover - hardware dependent
            return {
                "ok": False,
                "message": f"Camera settings batch failed: {exc}",
                "request_id": request_id,
                "nodes": [],
                "rollback_errors": [],
            }

        changed_settings: list[dict[str, Any]] = []
        updated_nodes: list[dict[str, Any]] = []
        try:
            for item, node, info in prepared:
                changed_settings.append(
                    {
                        "map_key": map_key,
                        "node_name": str(item.get("node_name") or ""),
                        "value": info.get("value"),
                    }
                )
                self._nodes.write_setting(node, str(info["type"]), item.get("value"))
                updated_nodes.append(self._nodes.describe(map_key, node) or info)
        except Exception as exc:  # pragma: no cover - hardware dependent
            _, rollback_errors = self._restore_values(list(reversed(changed_settings)))
            message = f"Camera settings batch failed: {exc}"
            if rollback_errors:
                message = f"{message}; rollback errors: {'; '.join(rollback_errors)}"
            return {
                "ok": False,
                "message": message,
                "request_id": request_id,
                "nodes": updated_nodes,
                "rollback_errors": rollback_errors,
                "streaming": bool(self._is_streaming()),
            }

        return {
            "ok": True,
            "message": f"Applied {len(updated_nodes)} camera settings.",
            "request_id": request_id,
            "nodes": updated_nodes,
            "rollback_errors": [],
            "streaming": bool(self._is_streaming()),
        }

    def apply_temporary(self, payload: dict[str, Any]) -> dict[str, Any]:
        map_key = str(payload.get("map_key") or "camera")
        restore_key = str(payload.get("restore_key") or "default")
        if restore_key in self._temporary_settings:
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
                node, info = self._nodes.inspect_setting(map_key, node_name)
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
                self._nodes.write_setting(node, str(info["type"]), value)
                updated_nodes.append(self._nodes.describe(map_key, node) or info)
        except Exception as exc:  # pragma: no cover - hardware dependent
            _, rollback_errors = self._restore_values(list(reversed(saved_settings)))
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

        self._temporary_settings[restore_key] = saved_settings
        return {
            "ok": True,
            "message": f"Applied {len(updated_nodes)} temporary camera settings.",
            "restore_key": restore_key,
            "nodes": updated_nodes,
        }

    def restore_temporary(self, payload: dict[str, Any]) -> dict[str, Any]:
        restore_key = str(payload.get("restore_key") or "default")
        saved_settings = self._temporary_settings.get(restore_key)
        if not saved_settings:
            return {
                "ok": True,
                "message": f"No temporary camera settings active: {restore_key}.",
                "restore_key": restore_key,
                "nodes": [],
            }
        restored_nodes, errors = self._restore_values(list(reversed(saved_settings)))
        if errors:
            return {
                "ok": False,
                "message": f"Camera settings restore failed: {'; '.join(errors)}",
                "restore_key": restore_key,
                "nodes": restored_nodes,
                "errors": errors,
            }
        self._temporary_settings.pop(restore_key, None)
        return {
            "ok": True,
            "message": f"Restored {len(restored_nodes)} camera settings.",
            "restore_key": restore_key,
            "nodes": restored_nodes,
        }

    def _restore_values(
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
                    self._nodes.set_value(map_key, node_name, item.get("value"))
                )
            except Exception as exc:  # pragma: no cover - hardware dependent
                errors.append(f"{node_name}: {exc}")
        return restored_nodes, errors


__all__ = ["CameraSettingsTransactions"]
