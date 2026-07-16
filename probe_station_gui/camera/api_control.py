"""Thread-safe bridge between the local API and camera acquisition state."""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QBuffer, QIODevice, QObject, Slot
from PySide6.QtGui import QImage


OPERATOR_CAMERA_NODE_NAMES = (
    "ExposureAuto",
    "ExposureTime",
    "GainAuto",
    "Gain",
    "BlackLevel",
    "BalanceWhiteAuto",
    "BalanceRatioSelector",
    "BalanceRatio",
)
_OPERATOR_CAMERA_NODE_SET = frozenset(OPERATOR_CAMERA_NODE_NAMES)

SnapshotSubmitter = Callable[[str, list[str]], None]
BatchSubmitter = Callable[[str, list[tuple[str, object]]], None]
FrameCounter = Callable[[], int]


@dataclass
class _PendingCameraOperation:
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None


class CameraApiBroker(QObject):
    """Correlate synchronous API requests with asynchronous camera results."""

    def __init__(
        self,
        *,
        snapshot_submit: SnapshotSubmitter,
        batch_submit: BatchSubmitter,
        frame_counter: FrameCounter,
        timeout_s: float = 5.0,
    ) -> None:
        super().__init__()
        self._snapshot_submit = snapshot_submit
        self._batch_submit = batch_submit
        self._frame_counter = frame_counter
        self._timeout_s = max(0.001, float(timeout_s))
        self._lock = threading.RLock()
        self._pending: dict[str, _PendingCameraOperation] = {}

    def read_settings(self, names: Sequence[str] | None = None) -> dict[str, Any]:
        requested = list(OPERATOR_CAMERA_NODE_NAMES if names is None else names)
        error = _camera_node_names_error(requested)
        if error:
            return _rejected(error, 400)
        result = self._submit_and_wait(
            lambda request_id: self._snapshot_submit(request_id, requested)
        )
        if not result.get("accepted", False):
            return result
        nodes: list[dict[str, Any]] = []
        for node_map in result.get("maps") or []:
            if not isinstance(node_map, Mapping):
                continue
            for node in node_map.get("nodes") or []:
                if isinstance(node, Mapping):
                    nodes.append(_api_node_payload(node))
        return {
            **result,
            "camera_ready": True,
            "nodes": nodes,
        }

    def write_settings(
        self,
        settings: Sequence[Mapping[str, object]],
    ) -> dict[str, Any]:
        ordered: list[tuple[str, object]] = []
        for item in settings:
            if not isinstance(item, Mapping):
                return _rejected("Each camera setting must be an object.", 400)
            name = str(item.get("name") or "").strip()
            if not name:
                return _rejected("Camera setting name is required.", 400)
            if "value" not in item:
                return _rejected(f"Camera setting value is required: {name}.", 400)
            ordered.append((name, item.get("value")))
        if not ordered:
            return _rejected("Provide at least one camera setting.", 400)
        error = _camera_node_names_error([name for name, _value in ordered])
        if error:
            return _rejected(error, 400)
        result = self._submit_and_wait(
            lambda request_id: self._batch_submit(request_id, ordered)
        )
        if result.get("accepted", False):
            result["nodes"] = [
                _api_node_payload(node)
                for node in result.get("nodes") or []
                if isinstance(node, Mapping)
            ]
        return result

    @Slot(object)
    def complete(self, result: object) -> None:
        if not isinstance(result, Mapping):
            return
        request_id = str(result.get("request_id") or "")
        if not request_id:
            return
        try:
            frame_counter = int(self._frame_counter())
        except Exception:
            frame_counter = 0
        event: threading.Event | None = None
        with self._lock:
            pending = self._pending.get(request_id)
            if pending is None or pending.result is not None:
                return
            pending.result = {
                **dict(result),
                "frame_counter_at_completion": frame_counter,
            }
            event = pending.event
        if event is not None:
            event.set()

    def _submit_and_wait(
        self,
        submit: Callable[[str], None],
    ) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        pending = _PendingCameraOperation()
        with self._lock:
            self._pending[request_id] = pending
        try:
            submit(request_id)
        except Exception as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            return _rejected(f"Unable to submit camera operation: {exc}", 503)

        completed = pending.event.wait(self._timeout_s)
        with self._lock:
            resolved = self._pending.pop(request_id, None)
        result = resolved.result if resolved is not None else None
        if result is None and not completed:
            return _rejected("Camera operation timed out.", 504)
        if result is None:
            return _rejected("Camera operation completed without a result.", 500)
        return _normalize_worker_result(result)


def encode_camera_frame_png(
    frame: QImage | None,
    *,
    counter: int,
    space: str,
) -> dict[str, Any]:
    """Encode a copied camera frame for a binary API response."""

    normalized_space = str(space or "").strip().lower()
    if normalized_space not in {"raw", "corrected"}:
        return _rejected(f"Unsupported camera frame space: {space!r}.", 400)
    if frame is None or frame.isNull():
        return _rejected("Camera frame is not available.", 503)
    image = frame.copy()
    buffer = QBuffer()
    if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
        return _rejected("Unable to allocate camera frame buffer.", 500)
    if not image.save(buffer, "PNG"):
        buffer.close()
        return _rejected("Unable to encode camera frame as PNG.", 500)
    data = bytes(buffer.data())
    buffer.close()
    return {
        "accepted": True,
        "status_code": 200,
        "data": data,
        "content_type": "image/png",
        "counter": int(counter),
        "space": normalized_space,
        "width": int(image.width()),
        "height": int(image.height()),
    }


def _camera_node_names_error(names: Sequence[str]) -> str:
    normalized = [str(name or "").strip() for name in names]
    if any(not name for name in normalized):
        return "Camera setting name is required."
    duplicates = sorted({name for name in normalized if normalized.count(name) > 1})
    if duplicates:
        return f"Duplicate camera settings: {', '.join(duplicates)}."
    unknown = sorted(set(normalized) - _OPERATOR_CAMERA_NODE_SET)
    if unknown:
        return f"Unsupported camera settings: {', '.join(unknown)}."
    return ""


def _normalize_worker_result(result: Mapping[str, object]) -> dict[str, Any]:
    normalized = dict(result)
    accepted = bool(normalized.get("ok", False))
    normalized["accepted"] = accepted
    if accepted:
        normalized["status_code"] = 200
        return normalized
    message = str(normalized.get("message") or "Camera operation failed.")
    normalized["status_code"] = 503 if "not ready" in message.lower() else 409
    return normalized


def _api_node_payload(node: Mapping[str, object]) -> dict[str, Any]:
    payload = dict(node)
    entries = payload.pop("entries", payload.get("enum_entries", []))
    payload["enum_entries"] = list(entries) if isinstance(entries, (list, tuple)) else []
    return payload


def _rejected(message: str, status_code: int) -> dict[str, Any]:
    return {
        "accepted": False,
        "status_code": int(status_code),
        "message": str(message),
    }


__all__ = [
    "CameraApiBroker",
    "OPERATOR_CAMERA_NODE_NAMES",
    "encode_camera_frame_png",
]
