from __future__ import annotations

import threading
import time
from types import SimpleNamespace

from PySide6.QtCore import QCoreApplication

from probe_station_gui.stage.axis_coordinates import NeedleHeightSaveResult
from probe_station_gui.stage.controller import StageController
from probe_station_gui.stage.errors import StageControllerError


def test_needle_height_save_request_returns_before_serial_work() -> None:
    controller = StageController()
    controller._serial = SimpleNamespace(is_open=True)
    started = threading.Event()
    release = threading.Event()

    def block_status(*_args: object, **_kwargs: object) -> None:
        started.set()
        assert release.wait(1.0)
        return None

    controller._query_status_with_required_coordinates = block_status
    try:
        accepted = controller.request_needle_height_save("save-1")

        assert accepted is True
        assert started.wait(1.0)
        assert controller._operation_lifecycle.snapshot().owner_thread is not (
            threading.current_thread()
        )
    finally:
        release.set()
        controller.shutdown()


def test_needle_height_save_transaction_publishes_lowering_after_g10() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    controller = StageController()
    controller._serial = SimpleNamespace(is_open=True)
    events: list[tuple[object, ...]] = []
    finished = threading.Event()

    def read_a() -> float:
        events.append(("read", threading.current_thread().name))
        return -0.75

    def set_a_zero(axis: str, value: float) -> None:
        events.append(("g10", axis, value, threading.current_thread().name))

    controller._read_current_a_position = read_a
    controller._axis_a_lowering_for_configured_coordinate = lambda value: abs(value)
    controller._set_current_axis_work_coordinate_locked = set_a_zero
    controller.needle_height_save_finished.connect(
        lambda request_id, result: (
            events.append(
                ("result", request_id, result, threading.current_thread().name)
            ),
            finished.set(),
        )
    )
    try:
        assert controller.request_needle_height_save("save-2") is True
        deadline = time.monotonic() + 1.0
        while not finished.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        assert finished.is_set()
    finally:
        controller.shutdown()

    worker_names = {event[-1] for event in events[:2]}
    assert len(worker_names) == 1
    assert threading.current_thread().name not in worker_names
    assert events[:2] == [
        ("read", next(iter(worker_names))),
        ("g10", "A", 0.0, next(iter(worker_names))),
    ]
    assert events[2][0:2] == ("result", "save-2")
    assert events[2][2] == NeedleHeightSaveResult(True, lowering_mm=0.75)
    assert events[2][3] == threading.current_thread().name


def test_needle_height_save_reports_missing_a_without_g10() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    controller = StageController()
    controller._serial = SimpleNamespace(is_open=True)
    controller._read_current_a_position = lambda: (
        controller._record_a_position_read_failure(
            "status query returned no complete status frame"
        )
        or None
    )
    controller._set_current_axis_work_coordinate_locked = lambda *_args: (
        _ for _ in ()
    ).throw(AssertionError("G10 must not run"))
    received: list[tuple[object, object]] = []
    finished = threading.Event()
    controller.needle_height_save_finished.connect(
        lambda request_id, result: (
            received.append((request_id, result)),
            finished.set(),
        )
    )
    try:
        assert controller.request_needle_height_save("save-3") is True
        deadline = time.monotonic() + 1.0
        while not finished.is_set() and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        assert finished.is_set()
    finally:
        controller.shutdown()

    assert len(received) == 1
    request_id, result = received[0]
    assert request_id == "save-3"
    assert result == NeedleHeightSaveResult(
        False,
        error="Unable to read A position: controller status was unavailable.",
    )


def test_needle_height_save_reports_g10_failure_once() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    controller = StageController()
    controller._serial = SimpleNamespace(is_open=True)
    controller._read_current_a_position = lambda: -0.5
    controller._axis_a_lowering_for_configured_coordinate = lambda _value: 0.5
    controller._set_current_axis_work_coordinate_locked = lambda *_args: (
        _ for _ in ()
    ).throw(StageControllerError("controller rejected G10"))
    received: list[tuple[object, object]] = []
    controller.needle_height_save_finished.connect(
        lambda request_id, result: received.append((request_id, result))
    )
    try:
        assert controller.request_needle_height_save("save-4") is True
        deadline = time.monotonic() + 1.0
        while not received and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        assert received
    finally:
        controller.shutdown()

    assert received == [
        (
            "save-4",
            NeedleHeightSaveResult(
                False,
                error="Unable to set A0 at needle contact: controller rejected G10",
            ),
        )
    ]


def test_needle_height_save_keeps_committed_lowering_when_refresh_fails() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    controller = StageController()
    controller._serial = SimpleNamespace(is_open=True)
    controller._read_current_a_position = lambda: -0.75
    controller._axis_a_lowering_for_configured_coordinate = lambda _value: 0.75
    statuses = iter(
        (
            SimpleNamespace(state="Idle", coordinate_system="G54"),
            StageControllerError("serial dropped after acknowledged G10"),
        )
    )

    def query_status(*_args: object, **_kwargs: object) -> object:
        status = next(statuses)
        if isinstance(status, Exception):
            raise status
        return status

    commands: list[str] = []
    controller._query_status_with_required_coordinates = query_status
    controller._require_homed_axes = lambda *_args: None
    controller._write_current_command_and_wait = commands.append
    controller._query_work_coordinate_offsets = lambda: {}
    received: list[tuple[object, object]] = []
    controller.needle_height_save_finished.connect(
        lambda request_id, result: received.append((request_id, result))
    )
    try:
        assert controller.request_needle_height_save("save-committed") is True
        deadline = time.monotonic() + 1.0
        while not received and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        assert received
    finally:
        controller.shutdown()

    assert commands == ["G10 L20 P1 A0"]
    assert received == [
        (
            "save-committed",
            NeedleHeightSaveResult(True, lowering_mm=0.75),
        )
    ]


def test_needle_height_save_normalizes_unexpected_backend_failure() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    controller = StageController()
    controller._serial = SimpleNamespace(is_open=True)
    controller._read_current_a_position = lambda: (_ for _ in ()).throw(
        RuntimeError("status decoder failed")
    )
    received: list[tuple[object, object]] = []
    controller.needle_height_save_finished.connect(
        lambda request_id, result: received.append((request_id, result))
    )
    try:
        assert controller.request_needle_height_save("save-5") is True
        deadline = time.monotonic() + 1.0
        while not received and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(0.001)
        assert received
    finally:
        controller.shutdown()

    assert received == [
        (
            "save-5",
            NeedleHeightSaveResult(
                False,
                error="Unable to save needle contact: status decoder failed",
            ),
        )
    ]


def test_shutdown_suppresses_late_needle_height_result_and_new_request() -> None:
    app = QCoreApplication.instance() or QCoreApplication([])
    controller = StageController()
    controller._serial = SimpleNamespace(is_open=True)
    read_started = threading.Event()
    release_read = threading.Event()

    def read_a() -> float:
        read_started.set()
        assert release_read.wait(1.0)
        return -0.4

    controller._read_current_a_position = read_a
    controller._axis_a_lowering_for_configured_coordinate = lambda _value: 0.4
    controller._set_current_axis_work_coordinate_locked = lambda *_args: None
    received: list[tuple[object, object]] = []
    controller.needle_height_save_finished.connect(
        lambda request_id, result: received.append((request_id, result))
    )

    assert controller.request_needle_height_save("save-6") is True
    assert read_started.wait(1.0)
    shutdown_thread = threading.Thread(target=controller.shutdown)
    shutdown_thread.start()
    deadline = time.monotonic() + 1.0
    while not shutdown_thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.001)
    release_read.set()
    shutdown_thread.join(timeout=1.0)
    assert not shutdown_thread.is_alive()
    app.processEvents()

    assert received == []
    assert controller.request_needle_height_save("save-7") is False


def test_busy_needle_height_save_request_is_rejected() -> None:
    controller = StageController()
    controller._serial = SimpleNamespace(is_open=True)
    started = threading.Event()
    release = threading.Event()

    def read_a() -> None:
        started.set()
        assert release.wait(1.0)
        return None

    controller._read_current_a_position = read_a
    try:
        assert controller.request_needle_height_save("save-8") is True
        assert started.wait(1.0)
        assert controller.request_needle_height_save("save-9") is False
    finally:
        release.set()
        controller.shutdown()
