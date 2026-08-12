from __future__ import annotations

import queue
import threading
import time
from collections import Counter
from collections.abc import Callable
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication, QObject, Qt, Slot
from PySide6.QtWidgets import QApplication

from probe_station_gui.camera import worker as worker_module
from probe_station_gui.camera.worker import Grabber


class _PixelFormatNode:
    def is_available(self) -> bool:
        return False


class _StreamModeNode:
    def __init__(self) -> None:
        self.value = "OldestFirst"

    def is_available(self) -> bool:
        return True

    def is_writable(self) -> bool:
        return True

    def set_node_value_from_str(self, value: str) -> None:
        self.value = value

    def get_node_value_as_str(self) -> str:
        return self.value


class _StreamNodeMap:
    def __init__(self) -> None:
        self._mode = _StreamModeNode()

    def get_node_by_name(self, name: str) -> object | None:
        if name == "StreamBufferHandlingMode":
            return self._mode
        return None


class _CameraImage:
    def __init__(self, events: list[object], *, copy_error: Exception | None = None):
        self._events = events
        self._copy_error = copy_error
        self.detached = object()

    def deep_copy_image(self, source: object) -> object:
        self._events.append(("deep_copy", source is self))
        if self._copy_error is not None:
            raise self._copy_error
        return self.detached

    def release(self) -> None:
        self._events.append(("image_release",))


class _FakeCamera:
    def __init__(
        self,
        events: list[object],
        *,
        image: _CameraImage | None = None,
        block_next_image: bool = False,
    ) -> None:
        self.events = events
        self.image = image
        self.camera_nodes = type(
            "CameraNodes", (), {"PixelFormat": _PixelFormatNode()}
        )()
        self._stream_node_map = _StreamNodeMap()
        self.next_image_entered = threading.Event()
        self.allow_next_image = threading.Event()
        if not block_next_image:
            self.allow_next_image.set()

    def init_cam(self) -> None:
        self.events.append(("init",))

    def get_tl_stream_node_map(self) -> _StreamNodeMap:
        return self._stream_node_map

    def begin_acquisition(self) -> None:
        self.events.append(("begin",))

    def get_next_image(self, *, timeout: float) -> _CameraImage | None:
        self.events.append(("get", timeout))
        self.next_image_entered.set()
        if not self.allow_next_image.wait(1.0):
            raise TimeoutError("test did not release the fake camera")
        return self.image

    def end_acquisition(self) -> None:
        self.events.append(("end",))

    def deinit_cam(self) -> None:
        self.events.append(("deinit",))

    def release(self) -> None:
        self.events.append(("camera_release",))


def _install_backend(monkeypatch: pytest.MonkeyPatch, camera: _FakeCamera) -> None:
    class CameraList:
        @classmethod
        def create_from_system(cls, _system, _update_interfaces, _update_cameras):
            return cls()

        def get_size(self) -> int:
            return 1

        def create_camera_by_index(self, index: int) -> _FakeCamera:
            assert index == 0
            return camera

    class SpinSystem:
        pass

    monkeypatch.setattr(
        worker_module,
        "_load_camera_backend",
        lambda: (CameraList, SpinSystem, None),
    )


def _shutdown_executor(grabber: Grabber) -> None:
    grabber._camera_settings_executor.shutdown(wait=True, cancel_futures=True)


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 1.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.002)
    assert predicate()


def test_failed_deep_copy_releases_original_before_camera_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    image = _CameraImage(events, copy_error=RuntimeError("copy failed"))
    camera = _FakeCamera(events, image=image)
    _install_backend(monkeypatch, camera)
    grabber = Grabber()
    errors: list[str] = []
    grabber.error.connect(errors.append, Qt.ConnectionType.DirectConnection)

    grabber.start()

    assert events == [
        ("init",),
        ("begin",),
        ("get", Grabber.FRAME_TIMEOUT_S),
        ("deep_copy", True),
        ("image_release",),
        ("end",),
        ("deinit",),
        ("camera_release",),
    ]
    assert errors == ["init: RuntimeError('copy failed')"]


def test_camera_is_released_when_initialization_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    camera = _FakeCamera(events)

    def failed_init() -> None:
        events.append(("init",))
        raise RuntimeError("initialization failed")

    camera.init_cam = failed_init  # type: ignore[method-assign]
    _install_backend(monkeypatch, camera)
    grabber = Grabber()
    errors: list[str] = []
    grabber.error.connect(errors.append, Qt.ConnectionType.DirectConnection)

    grabber.start()

    assert events == [
        ("init",),
        ("deinit",),
        ("camera_release",),
    ]
    assert errors == ["init: RuntimeError('initialization failed')"]


def test_started_acquisition_is_ended_when_owner_construction_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    camera = _FakeCamera(events)
    _install_backend(monkeypatch, camera)

    def failed_nodes(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("owner construction failed")

    monkeypatch.setattr(worker_module.genicam_nodes, "GenICamNodes", failed_nodes)
    grabber = Grabber()

    grabber.start()

    assert events == [
        ("init",),
        ("begin",),
        ("end",),
        ("deinit",),
        ("camera_release",),
    ]


@pytest.mark.parametrize(
    ("failure_phase", "acquisition_started"),
    [
        ("stream configuration", False),
        ("pixel configuration", False),
        ("begin acquisition", False),
        ("transaction construction", True),
    ],
)
def test_partial_camera_setup_releases_every_owned_resource(
    monkeypatch: pytest.MonkeyPatch,
    failure_phase: str,
    acquisition_started: bool,
) -> None:
    events: list[object] = []
    camera = _FakeCamera(events)
    _install_backend(monkeypatch, camera)
    grabber = Grabber()

    def fail(*_args: object, **_kwargs: object) -> object:
        if failure_phase == "begin acquisition":
            events.append(("begin",))
        raise RuntimeError(failure_phase)

    if failure_phase == "stream configuration":
        monkeypatch.setattr(grabber, "_set_stream_buffer_handling_mode", fail)
    elif failure_phase == "pixel configuration":
        monkeypatch.setattr(grabber, "_set_rgb8_pixel_format", fail)
    elif failure_phase == "begin acquisition":
        camera.begin_acquisition = fail  # type: ignore[method-assign]
    else:
        monkeypatch.setattr(
            worker_module.settings_transactions,
            "CameraSettingsTransactions",
            fail,
        )

    grabber.start()

    cleanup = [
        event[0] for event in events if event[0] in {"end", "deinit", "camera_release"}
    ]
    if acquisition_started:
        assert cleanup == ["end", "deinit", "camera_release"]
    else:
        assert cleanup == ["deinit", "camera_release"]


def test_worker_is_one_shot_and_rejects_second_start_before_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend_calls: list[bool] = []

    class EmptyCameraList:
        @classmethod
        def create_from_system(cls, _system, _update_interfaces, _update_cameras):
            return cls()

        def get_size(self) -> int:
            return 0

    class SpinSystem:
        pass

    def load_backend():
        backend_calls.append(True)
        return EmptyCameraList, SpinSystem, None

    monkeypatch.setattr(worker_module, "_load_camera_backend", load_backend)
    grabber = Grabber()
    errors: list[str] = []
    grabber.error.connect(errors.append, Qt.ConnectionType.DirectConnection)

    grabber.start()
    grabber.start()

    assert backend_calls == [True]
    assert errors == ["No cameras detected", "Camera worker cannot be restarted."]


def test_backend_unavailable_shuts_executor_before_start_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        worker_module,
        "_load_camera_backend",
        lambda: (None, None, RuntimeError("rotpy unavailable")),
    )
    grabber = Grabber()
    try:
        grabber.start()

        with pytest.raises(RuntimeError, match="cannot schedule new futures"):
            grabber._camera_settings_executor.submit(lambda: None)
    finally:
        _shutdown_executor(grabber)


@pytest.mark.parametrize(
    ("signal_name", "submit", "expected"),
    [
        (
            "camera_settings_snapshot_ready",
            lambda worker: worker.request_camera_settings_snapshot(
                "camera", ["Gain"], request_id="snapshot-id"
            ),
            {"request_id": "snapshot-id", "maps": []},
        ),
        (
            "camera_setting_changed",
            lambda worker: worker.request_camera_setting_update(
                "camera", "Gain", 1.5, request_id="setting-id"
            ),
            {
                "request_id": "setting-id",
                "map_key": "camera",
                "node_name": "Gain",
            },
        ),
        (
            "camera_settings_batch_changed",
            lambda worker: worker.request_camera_settings_batch(
                [("Gain", 1.5)], request_id="batch-id"
            ),
            {"request_id": "batch-id"},
        ),
        (
            "camera_setting_changed",
            lambda worker: worker.request_camera_command_execute(
                "camera", "AcquisitionStart"
            ),
            {"map_key": "camera", "node_name": "AcquisitionStart"},
        ),
        (
            "camera_settings_override_changed",
            lambda worker: worker.request_temporary_camera_settings(
                [("TriggerMode", "On")], restore_key="laser"
            ),
            {"restore_key": "laser"},
        ),
        (
            "camera_settings_override_changed",
            lambda worker: worker.request_restore_camera_settings(restore_key="laser"),
            {"restore_key": "laser"},
        ),
    ],
)
def test_post_stop_requests_reject_synchronously_and_never_queue(
    monkeypatch: pytest.MonkeyPatch,
    signal_name: str,
    submit: Callable[[Grabber], None],
    expected: dict[str, Any],
) -> None:
    events: list[object] = []
    camera = _FakeCamera(events, block_next_image=True)
    _install_backend(monkeypatch, camera)
    grabber = Grabber()
    results: list[dict[str, Any]] = []
    getattr(grabber, signal_name).connect(
        results.append,
        Qt.ConnectionType.DirectConnection,
    )
    thread = threading.Thread(target=grabber.start, daemon=True)
    thread.start()
    assert camera.next_image_entered.wait(1.0)
    try:
        grabber.stop()
        submit(grabber)

        assert len(results) == 1
        assert results[0]["ok"] is False
        assert results[0]["message"] == "Camera is not ready."
        assert {key: results[0][key] for key in expected} == expected
        with pytest.raises(queue.Empty):
            grabber._camera_commands.get_nowait()
    finally:
        camera.allow_next_image.set()
        thread.join(2.0)
        assert not thread.is_alive()


def test_command_accepted_before_stop_is_rejected_once_during_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    camera = _FakeCamera(events, block_next_image=True)
    _install_backend(monkeypatch, camera)
    grabber = Grabber()
    results: list[dict[str, Any]] = []
    grabber.camera_settings_batch_changed.connect(
        results.append,
        Qt.ConnectionType.DirectConnection,
    )
    thread = threading.Thread(target=grabber.start, daemon=True)
    thread.start()
    assert camera.next_image_entered.wait(1.0)
    try:
        grabber.request_camera_settings_batch(
            [("Gain", 1.5)],
            request_id="accepted-before-stop",
        )
        assert grabber._camera_commands.qsize() == 1

        grabber.stop()
        camera.allow_next_image.set()
        thread.join(2.0)

        assert not thread.is_alive()
        assert len(results) == 1
        assert results[0]["ok"] is False
        assert results[0]["request_id"] == "accepted-before-stop"
        assert grabber._camera_commands.empty()
    finally:
        camera.allow_next_image.set()
        grabber.stop()
        thread.join(2.0)


class _QueuedReceiver(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.results: list[tuple[int, dict[str, Any]]] = []

    @Slot(object)
    def receive(self, result: object) -> None:
        assert isinstance(result, dict)
        self.results.append((threading.get_ident(), result))


def test_settings_executor_is_fifo_single_thread_and_preserves_result_ids() -> None:
    app = QApplication.instance() or QApplication([])
    del app
    grabber = Grabber()
    submitter_thread = threading.get_ident()
    first_started = threading.Event()
    release_first = threading.Event()
    task_events: list[tuple[str, int]] = []
    direct_results: list[tuple[int, dict[str, Any]]] = []
    queued_receiver = _QueuedReceiver()
    grabber.camera_setting_changed.connect(
        lambda result: direct_results.append((threading.get_ident(), result)),
        Qt.ConnectionType.DirectConnection,
    )
    grabber.camera_setting_changed.connect(queued_receiver.receive)

    def task(payload: dict[str, Any]) -> dict[str, Any]:
        task_events.append((str(payload["request_id"]), threading.get_ident()))
        if payload["request_id"] == "one":
            first_started.set()
            assert release_first.wait(1.0)
        return {"ok": True}

    try:
        for request_id in ("one", "two", "three"):
            grabber._submit_camera_task(
                "ordered task",
                task,
                {"request_id": request_id},
                grabber.camera_setting_changed,
            )
        assert first_started.wait(1.0)
        release_first.set()
        _wait_until(lambda: len(direct_results) == 3)
        _wait_until(lambda: len(queued_receiver.results) == 3)

        assert [item[0] for item in task_events] == ["one", "two", "three"]
        task_threads = {item[1] for item in task_events}
        assert len(task_threads) == 1
        assert submitter_thread not in task_threads
        assert [item[1]["request_id"] for item in direct_results] == [
            "one",
            "two",
            "three",
        ]
        assert {item[0] for item in direct_results} == task_threads
        assert [item[1]["request_id"] for item in queued_receiver.results] == [
            "one",
            "two",
            "three",
        ]
        assert {item[0] for item in queued_receiver.results} == {submitter_thread}
    finally:
        release_first.set()
        _shutdown_executor(grabber)


def test_cancelled_and_submit_rejected_tasks_publish_once_with_request_id() -> None:
    grabber = Grabber()
    first_started = threading.Event()
    release_first = threading.Event()
    ran: list[str] = []
    results: list[dict[str, Any]] = []
    grabber.camera_settings_batch_changed.connect(
        results.append,
        Qt.ConnectionType.DirectConnection,
    )

    def blocking(payload: dict[str, Any]) -> dict[str, Any]:
        ran.append(str(payload["request_id"]))
        first_started.set()
        assert release_first.wait(1.0)
        return {"ok": True}

    grabber._submit_camera_task(
        "first",
        blocking,
        {"request_id": "first-id"},
        grabber.camera_settings_batch_changed,
    )
    assert first_started.wait(1.0)
    grabber._submit_camera_task(
        "cancelled",
        lambda payload: ran.append(str(payload["request_id"])) or {"ok": True},
        {"request_id": "cancelled-id"},
        grabber.camera_settings_batch_changed,
    )
    shutdown = threading.Thread(
        target=lambda: grabber._camera_settings_executor.shutdown(
            wait=True,
            cancel_futures=True,
        ),
        daemon=True,
    )
    shutdown.start()
    _wait_until(
        lambda: any(result.get("request_id") == "cancelled-id" for result in results)
    )
    release_first.set()
    shutdown.join(2.0)
    assert not shutdown.is_alive()

    grabber._submit_camera_task(
        "after shutdown",
        lambda _payload: {"ok": True},
        {"request_id": "rejected-id"},
        grabber.camera_settings_batch_changed,
    )

    assert ran == ["first-id"]
    assert Counter(result["request_id"] for result in results) == Counter(
        {"first-id": 1, "cancelled-id": 1, "rejected-id": 1}
    )
    assert (
        next(result for result in results if result["request_id"] == "first-id")["ok"]
        is True
    )
    assert (
        next(result for result in results if result["request_id"] == "cancelled-id")[
            "ok"
        ]
        is False
    )
    assert (
        next(result for result in results if result["request_id"] == "rejected-id")[
            "ok"
        ]
        is False
    )


def test_executor_finishes_running_task_before_camera_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    camera = _FakeCamera(events, block_next_image=True)
    _install_backend(monkeypatch, camera)
    grabber = Grabber()
    task_started = threading.Event()
    allow_task_finish = threading.Event()
    results: list[dict[str, Any]] = []
    grabber.camera_settings_snapshot_ready.connect(
        results.append,
        Qt.ConnectionType.DirectConnection,
    )
    original_runner = grabber._run_camera_task

    def blocking_runner(task_name, func, payload):
        task_started.set()
        assert allow_task_finish.wait(1.0)
        events.append(("settings_finished",))
        return original_runner(task_name, func, payload)

    monkeypatch.setattr(grabber, "_run_camera_task", blocking_runner)
    grabber._camera_commands.put(
        worker_module._CameraCommand("snapshot", {"request_id": "accepted-id"})
    )
    thread = threading.Thread(target=grabber.start, daemon=True)
    thread.start()
    assert task_started.wait(1.0)
    assert camera.next_image_entered.wait(1.0)
    grabber.stop()
    camera.allow_next_image.set()
    time.sleep(0.02)
    assert ("camera_release",) not in events
    allow_task_finish.set()
    thread.join(2.0)

    assert not thread.is_alive()
    assert len(results) == 1
    assert results[0]["request_id"] == "accepted-id"
    assert events.index(("settings_finished",)) < events.index(("camera_release",))


def test_settings_execute_while_acquisition_remains_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[object] = []
    camera = _FakeCamera(events, block_next_image=True)
    _install_backend(monkeypatch, camera)
    setting_started = threading.Event()
    allow_setting = threading.Event()

    class BlockingNodes:
        def __init__(self, _camera: object, *, is_streaming: Callable[[], bool]):
            self._is_streaming = is_streaming

        def apply_setting(self, payload: dict[str, Any]) -> dict[str, Any]:
            events.append(("setting_started", self._is_streaming()))
            setting_started.set()
            assert allow_setting.wait(1.0)
            return {"ok": True, "node_name": payload["node_name"]}

    monkeypatch.setattr(worker_module.genicam_nodes, "GenICamNodes", BlockingNodes)
    grabber = Grabber()
    results: list[dict[str, Any]] = []
    grabber.camera_setting_changed.connect(
        results.append,
        Qt.ConnectionType.DirectConnection,
    )
    grabber._camera_commands.put(
        worker_module._CameraCommand(
            "set",
            {"map_key": "camera", "node_name": "Gain", "value": 1.5},
        )
    )
    thread = threading.Thread(target=grabber.start, daemon=True)
    thread.start()
    try:
        assert setting_started.wait(1.0)
        assert camera.next_image_entered.wait(1.0)
        assert events.count(("begin",)) == 1
        assert ("end",) not in events
        assert ("setting_started", True) in events
    finally:
        grabber.stop()
        camera.allow_next_image.set()
        allow_setting.set()
        thread.join(2.0)

    assert not thread.is_alive()
    assert events.count(("begin",)) == 1
    assert events.count(("end",)) == 1
    assert results == [{"ok": True, "node_name": "Gain"}]
