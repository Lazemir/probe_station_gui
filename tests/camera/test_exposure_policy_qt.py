from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.camera.exposure_policy_qt import ExposurePolicyQtAdapter


def _app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(app: QApplication, predicate) -> None:
    deadline = time.monotonic() + 1.0
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert predicate()


def test_adapter_dispatches_policy_commands_from_a_worker_and_rejects_busy() -> None:
    app = _app()
    controller = _BlockingController()
    adapter = ExposurePolicyQtAdapter(controller)
    finished: list[dict[str, object]] = []
    adapter.command_finished.connect(finished.append)

    gui_thread = threading.get_ident()
    adapter.request_update(False, "camera")
    assert controller.started.wait(1.0)

    adapter.request_once()
    _wait_until(app, lambda: len(finished) == 1)
    controller.release.set()
    _wait_until(app, lambda: len(finished) == 2)

    assert controller.update_calls == [(False, "camera")]
    assert controller.update_threads == [controller.worker_thread]
    assert controller.worker_thread != gui_thread
    assert controller.once_calls == 0
    assert any(result.get("busy") is True for result in finished)
    assert any(result.get("accepted") is True for result in finished)


class _BlockingController:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.update_calls: list[tuple[bool, str]] = []
        self.update_threads: list[int] = []
        self.worker_thread = 0
        self.once_calls = 0
        self._callbacks = []

    def subscribe(self, callback) -> None:
        self._callbacks.append(callback)

    def snapshot(self) -> dict[str, object]:
        return {"auto_enabled": True, "engine": "software", "busy": False}

    def set_policy(self, *, auto_enabled: bool, engine: str) -> dict[str, object]:
        self.worker_thread = threading.get_ident()
        self.update_threads.append(self.worker_thread)
        self.update_calls.append((auto_enabled, engine))
        self.started.set()
        assert self.release.wait(1.0)
        return {"accepted": True, "auto_enabled": auto_enabled, "engine": engine}

    def run_once(self) -> dict[str, object]:
        self.once_calls += 1
        return {"accepted": True}
