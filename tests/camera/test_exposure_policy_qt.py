from __future__ import annotations

import threading
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from probe_station_gui.camera.exposure_policy_qt import (
    ExposurePolicyQtAdapter,
    ExposurePolicyQtAdapterError,
)


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


def test_adapter_shutdown_joins_active_command_and_suppresses_late_signals() -> None:
    app = _app()
    controller = _BlockingController()
    adapter = ExposurePolicyQtAdapter(controller)
    finished: list[dict[str, object]] = []
    adapter.command_finished.connect(finished.append)

    adapter.request_update(False, "camera")
    assert controller.started.wait(1.0)
    threading.Timer(0.02, controller.release.set).start()

    adapter.shutdown(timeout_s=1.0)
    app.processEvents()
    adapter.request_once()

    assert controller.completed.is_set()
    assert controller.once_calls == 0
    assert finished == []
    assert controller.unsubscribe_calls == 1


def test_closed_adapter_reports_timeout_and_survives_destruction() -> None:
    app = _app()
    controller = _BlockingController()
    adapter = ExposurePolicyQtAdapter(controller)
    finished: list[dict[str, object]] = []
    adapter.command_finished.connect(finished.append)

    adapter.request_update(False, "camera")
    assert controller.started.wait(1.0)

    with pytest.raises(ExposurePolicyQtAdapterError, match="did not stop"):
        adapter.shutdown(timeout_s=0.01)
    adapter.deleteLater()
    app.processEvents()
    controller.release.set()
    _wait_until(app, controller.completed.is_set)

    assert finished == []
    assert controller.unsubscribe_calls == 1


def test_shutdown_waits_for_registered_state_emission_and_blocks_copied_callback() -> None:
    _app()
    controller = _BlockingController()
    adapter = ExposurePolicyQtAdapter(controller)
    emission_entered = threading.Event()
    release_emission = threading.Event()
    emission_calls: list[dict[str, object]] = []
    shutdown_complete = threading.Event()
    shutdown_errors: list[BaseException] = []

    def block_emission(state: dict[str, object]) -> None:
        emission_calls.append(state)
        emission_entered.set()
        assert release_emission.wait(1.0)

    adapter.state_changed.connect(block_emission, Qt.ConnectionType.DirectConnection)
    copied_callback = tuple(controller._callbacks)[0]
    emitter = threading.Thread(
        target=lambda: copied_callback(
            {"auto_enabled": True, "engine": "software", "busy": False}
        )
    )
    emitter.start()
    assert emission_entered.wait(1.0)

    def shutdown_adapter() -> None:
        try:
            adapter.shutdown(timeout_s=1.0)
        except BaseException as exc:
            shutdown_errors.append(exc)
        finally:
            shutdown_complete.set()

    shutdown = threading.Thread(target=shutdown_adapter)
    shutdown.start()
    try:
        assert not shutdown_complete.wait(0.05)
    finally:
        release_emission.set()
        emitter.join(1.0)
        shutdown.join(1.0)

    copied_callback({"auto_enabled": False, "engine": "camera", "busy": False})

    assert not emitter.is_alive()
    assert not shutdown.is_alive()
    assert shutdown_errors == []
    assert len(emission_calls) == 1


def test_shutdown_waits_for_registered_command_finished_emission() -> None:
    _app()
    controller = _BlockingController()
    adapter = ExposurePolicyQtAdapter(controller)
    emission_entered = threading.Event()
    release_emission = threading.Event()
    shutdown_complete = threading.Event()
    shutdown_errors: list[BaseException] = []

    def block_emission(_result: dict[str, object]) -> None:
        emission_entered.set()
        assert release_emission.wait(1.0)

    adapter.command_finished.connect(
        block_emission,
        Qt.ConnectionType.DirectConnection,
    )
    emitter = threading.Thread(
        target=lambda: adapter._run_command(lambda: {"accepted": True})
    )
    emitter.start()
    assert emission_entered.wait(1.0)

    def shutdown_adapter() -> None:
        try:
            adapter.shutdown(timeout_s=1.0)
        except BaseException as exc:
            shutdown_errors.append(exc)
        finally:
            shutdown_complete.set()

    shutdown = threading.Thread(target=shutdown_adapter)
    shutdown.start()
    try:
        assert not shutdown_complete.wait(0.05)
    finally:
        release_emission.set()
        emitter.join(1.0)
        shutdown.join(1.0)

    assert not emitter.is_alive()
    assert not shutdown.is_alive()
    assert shutdown_errors == []


class _BlockingController:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()
        self.update_calls: list[tuple[bool, str]] = []
        self.update_threads: list[int] = []
        self.worker_thread = 0
        self.once_calls = 0
        self._callbacks = []
        self.unsubscribe_calls = 0

    def subscribe(self, callback) -> None:
        self._callbacks.append(callback)

    def unsubscribe(self, callback) -> None:
        self.unsubscribe_calls += 1
        self._callbacks.remove(callback)

    def snapshot(self) -> dict[str, object]:
        return {"auto_enabled": True, "engine": "software", "busy": False}

    def set_policy(self, *, auto_enabled: bool, engine: str) -> dict[str, object]:
        self.worker_thread = threading.get_ident()
        self.update_threads.append(self.worker_thread)
        self.update_calls.append((auto_enabled, engine))
        self.started.set()
        try:
            assert self.release.wait(1.0)
            return {"accepted": True, "auto_enabled": auto_enabled, "engine": engine}
        finally:
            self.completed.set()

    def run_once(self) -> dict[str, object]:
        self.once_calls += 1
        return {"accepted": True}
