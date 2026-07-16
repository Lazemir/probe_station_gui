from __future__ import annotations

import threading
import time

from PySide6.QtCore import QByteArray
from PySide6.QtGui import QColor, QImage

from probe_station_gui.camera.api_control import (
    CameraApiBroker,
    encode_camera_frame_png,
)


def _wait_for_count(items: list[object], count: int) -> None:
    deadline = time.monotonic() + 1.0
    while len(items) < count and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(items) == count


def test_read_rejects_unknown_and_duplicate_nodes_before_submit() -> None:
    submitted: list[object] = []
    broker = CameraApiBroker(
        snapshot_submit=lambda request_id, names: submitted.append((request_id, names)),
        batch_submit=lambda request_id, settings: submitted.append(
            (request_id, settings)
        ),
        frame_counter=lambda: 0,
    )

    unknown = broker.read_settings(["TriggerMode"])
    duplicate = broker.read_settings(["Gain", "Gain"])

    assert unknown["accepted"] is False
    assert unknown["status_code"] == 400
    assert duplicate["accepted"] is False
    assert duplicate["status_code"] == 400
    assert submitted == []


def test_concurrent_results_are_correlated_by_request_id() -> None:
    submitted: list[tuple[str, list[str]]] = []
    submitted_event = threading.Event()

    def submit(request_id: str, names: list[str]) -> None:
        submitted.append((request_id, names))
        if len(submitted) == 2:
            submitted_event.set()

    broker = CameraApiBroker(
        snapshot_submit=submit,
        batch_submit=lambda _request_id, _settings: None,
        frame_counter=lambda: 17,
    )
    results: dict[str, dict[str, object]] = {}
    first = threading.Thread(
        target=lambda: results.update(first=broker.read_settings(["Gain"]))
    )
    second = threading.Thread(
        target=lambda: results.update(second=broker.read_settings(["ExposureTime"]))
    )
    first.start()
    second.start()
    assert submitted_event.wait(1.0)

    for request_id, names in reversed(submitted):
        broker.complete(
            {
                "ok": True,
                "request_id": request_id,
                "maps": [{"key": "camera", "nodes": [{"name": names[0]}]}],
                "streaming": True,
            }
        )
    first.join(1.0)
    second.join(1.0)

    assert results["first"]["nodes"][0]["name"] == "Gain"
    assert results["second"]["nodes"][0]["name"] == "ExposureTime"
    assert results["first"]["frame_counter_at_completion"] == 17
    assert results["second"]["accepted"] is True


def test_timeout_removes_pending_request_and_late_completion_is_ignored() -> None:
    submitted: list[tuple[str, list[str]]] = []
    broker = CameraApiBroker(
        snapshot_submit=lambda request_id, names: submitted.append((request_id, names)),
        batch_submit=lambda _request_id, _settings: None,
        frame_counter=lambda: 0,
        timeout_s=0.01,
    )

    result = broker.read_settings(["Gain"])

    assert result["accepted"] is False
    assert result["status_code"] == 504
    assert broker._pending == {}
    broker.complete({"ok": True, "request_id": submitted[0][0], "maps": []})
    assert broker._pending == {}


def test_write_preserves_order_and_returns_completion_watermark() -> None:
    submitted: list[tuple[str, list[tuple[str, object]]]] = []
    submitted_event = threading.Event()

    def submit(request_id: str, settings: list[tuple[str, object]]) -> None:
        submitted.append((request_id, settings))
        submitted_event.set()

    broker = CameraApiBroker(
        snapshot_submit=lambda _request_id, _names: None,
        batch_submit=submit,
        frame_counter=lambda: 23,
    )
    result_holder: dict[str, object] = {}
    thread = threading.Thread(
        target=lambda: result_holder.update(
            broker.write_settings(
                [
                    {"name": "ExposureAuto", "value": "Off"},
                    {"name": "ExposureTime", "value": 1800.0},
                ]
            )
        )
    )
    thread.start()
    assert submitted_event.wait(1.0)
    request_id, settings = submitted[0]
    broker.complete(
        {
            "ok": True,
            "request_id": request_id,
            "nodes": [{"name": name, "value": value} for name, value in settings],
            "streaming": True,
        }
    )
    thread.join(1.0)

    assert settings == [("ExposureAuto", "Off"), ("ExposureTime", 1800.0)]
    assert result_holder["accepted"] is True
    assert result_holder["frame_counter_at_completion"] == 23


def test_png_encoding_returns_image_metadata_and_decodable_bytes() -> None:
    image = QImage(12, 7, QImage.Format_RGB32)
    image.fill(QColor("#336699"))

    result = encode_camera_frame_png(image, counter=42, space="raw")

    assert result["accepted"] is True
    assert result["counter"] == 42
    assert result["space"] == "raw"
    assert result["width"] == 12
    assert result["height"] == 7
    decoded = QImage.fromData(QByteArray(result["data"]), "PNG")
    assert not decoded.isNull()
    assert decoded.size() == image.size()
