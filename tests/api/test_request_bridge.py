from __future__ import annotations

import threading
import time

from PySide6.QtCore import Qt

from probe_station_gui.api.request_bridge import ApiRequestBridge


def _wait_for(predicate, *, timeout_s: float = 1.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition was not reached")


def test_bridge_timeout_cancels_queued_request_before_late_gui_delivery() -> None:
    handled: list[dict[str, object]] = []
    captured: list[object] = []
    bridge = ApiRequestBridge(lambda request: handled.append(request) or {"accepted": True})
    bridge.request_received.connect(
        captured.append,
        Qt.ConnectionType.DirectConnection,
    )

    response = bridge.submit({"action": "command"}, timeout_s=0.01)
    assert response == {
        "accepted": False,
        "status_code": 503,
        "message": "GUI did not process the API request in time.",
    }

    assert len(captured) == 1
    bridge._handle_request(captured[0])
    assert handled == []


def test_bridge_close_wakes_waiting_api_request_with_shutdown_error() -> None:
    bridge = ApiRequestBridge(lambda _request: {"accepted": True})
    result: list[dict[str, object]] = []
    worker = threading.Thread(
        target=lambda: result.append(
            bridge.submit({"action": "command"}, timeout_s=30.0)
        )
    )

    worker.start()
    _wait_for(lambda: bool(getattr(bridge, "_pending", {})))
    bridge.close()
    worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert result == [
        {
            "accepted": False,
            "status_code": 503,
            "message": "GUI API bridge is shutting down.",
        }
    ]


def test_bridge_close_preserves_shutdown_result_for_in_flight_handler() -> None:
    handler_started = threading.Event()
    release_handler = threading.Event()
    captured: list[dict[str, object]] = []

    def handler(_request):
        handler_started.set()
        assert release_handler.wait(timeout=1.0)
        return {"accepted": True}

    bridge = ApiRequestBridge(handler)
    bridge.request_received.connect(
        captured.append,
        Qt.ConnectionType.DirectConnection,
    )
    result: list[dict[str, object]] = []
    submitter = threading.Thread(
        target=lambda: result.append(
            bridge.submit({"action": "command"}, timeout_s=30.0)
        )
    )
    submitter.start()
    _wait_for(lambda: len(captured) == 1)
    delivery = threading.Thread(target=lambda: bridge._handle_request(captured[0]))
    delivery.start()
    assert handler_started.wait(timeout=1.0)

    bridge.close()
    submitter.join(timeout=1.0)
    release_handler.set()
    delivery.join(timeout=1.0)

    expected = {
        "accepted": False,
        "status_code": 503,
        "message": "GUI API bridge is shutting down.",
    }
    assert result == [expected]
    assert captured[0]["result"] == expected
