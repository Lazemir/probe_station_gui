from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading
import time
from typing import Callable

from PySide6.QtCore import Qt

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    RenderFrame,
    RenderRequest,
    SnapRequest,
    SnapResponse,
)
from probe_station_gui.design.klayout_workers import (
    KLayoutRenderWorker,
    KLayoutSnapWorker,
)
from probe_station_gui.design.model import SnapResult


def _config(*, generation: int = 1, path: Path = Path("layout.gds")) -> KLayoutConfig:
    return KLayoutConfig(
        path=path,
        top_cell_name="TOP",
        visible_layers=frozenset({(1, 0)}),
        source_bounds=(0.0, 0.0, 10.0, 20.0),
        display_bounds=(0.0, 0.0, 10.0, 20.0),
        rotation_quarter_turns=0,
        generation=generation,
    )


def _render_request(request_id: int, config: KLayoutConfig | None = None) -> RenderRequest:
    return RenderRequest(
        request_id=request_id,
        config=config or _config(),
        world_box=(1.0, 2.0, 6.0, 8.0),
        pixel_width=320,
        pixel_height=240,
        viewport_generation=request_id,
        density=2.0,
    )


def _snap_request(
    request_id: int,
    *,
    purpose: str = "hover",
    config: KLayoutConfig | None = None,
) -> SnapRequest:
    return SnapRequest(
        request_id=request_id,
        config=config or _config(),
        point=(float(request_id), 0.25),
        radius=0.5,
        purpose=purpose,
    )


def _frame(request: RenderRequest) -> RenderFrame:
    return RenderFrame(
        request_id=request.request_id,
        config_generation=request.config.generation,
        viewport_generation=request.viewport_generation,
        world_box=request.world_box,
        pixel_width=request.pixel_width,
        pixel_height=request.pixel_height,
        density=request.density,
        image=object(),
        elapsed_ms=1.0,
        purpose=request.purpose,
    )


def _response(request: SnapRequest) -> SnapResponse:
    return SnapResponse(
        request_id=request.request_id,
        config_generation=request.config.generation,
        raw_point=request.point,
        result=SnapResult(request.point, "free", 0.0),
        elapsed_ms=1.0,
        shapes_inspected=0,
        purpose=request.purpose,
    )


def _wait_until(predicate: Callable[[], bool], timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for worker state")


class _RenderHarness:
    def __init__(self, *, block_ids: set[int] | None = None) -> None:
        self.block_ids = block_ids or set()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.factory_threads: list[threading.Thread] = []
        self.ensure_calls: list[tuple[int, int]] = []
        self.render_calls: list[tuple[int, int]] = []
        self.close_threads: list[int] = []

    def factory(self) -> object:
        self.factory_threads.append(threading.current_thread())
        harness = self

        class Backend:
            def ensure_config(self, config: KLayoutConfig) -> None:
                harness.ensure_calls.append((config.generation, threading.get_ident()))

            def render(self, request: RenderRequest) -> RenderFrame:
                harness.render_calls.append((request.request_id, threading.get_ident()))
                if request.request_id in harness.block_ids:
                    harness.entered.set()
                    harness.release.wait(2.0)
                return _frame(request)

            def close(self) -> None:
                harness.close_threads.append(threading.get_ident())

        return Backend()


class _SnapHarness:
    def __init__(self, *, block_ids: set[int] | None = None) -> None:
        self.block_ids = block_ids or set()
        self.entered = threading.Event()
        self.release = threading.Event()
        self.factory_threads: list[threading.Thread] = []
        self.ensure_calls: list[tuple[int, int]] = []
        self.snap_calls: list[tuple[int, int]] = []
        self.close_threads: list[int] = []

    def factory(self) -> object:
        self.factory_threads.append(threading.current_thread())
        harness = self

        class Backend:
            def ensure_config(self, config: KLayoutConfig) -> None:
                harness.ensure_calls.append((config.generation, threading.get_ident()))

            def snap(self, request: SnapRequest) -> SnapResponse:
                harness.snap_calls.append((request.request_id, threading.get_ident()))
                if request.request_id in harness.block_ids:
                    harness.entered.set()
                    harness.release.wait(2.0)
                return _response(request)

            def close(self) -> None:
                harness.close_threads.append(threading.get_ident())

        return Backend()


def test_render_keeps_only_newest_pending_request() -> None:
    harness = _RenderHarness(block_ids={1})
    worker = KLayoutRenderWorker(backend_factory=harness.factory)
    frames: list[RenderFrame] = []
    worker.frame_ready.connect(frames.append, Qt.ConnectionType.DirectConnection)

    worker.submit(_render_request(1))
    assert harness.entered.wait(1.0)
    worker.submit(_render_request(2))
    worker.submit(_render_request(3))
    harness.release.set()
    _wait_until(lambda: len(frames) == 2)
    worker.stop()

    assert [request_id for request_id, _thread_id in harness.render_calls] == [1, 3]
    assert [frame.request_id for frame in frames] == [1, 3]


def test_snap_keeps_newest_hover_but_prioritizes_fifo_clicks() -> None:
    harness = _SnapHarness(block_ids={1})
    worker = KLayoutSnapWorker(backend_factory=harness.factory)
    responses: list[SnapResponse] = []
    worker.snap_ready.connect(responses.append, Qt.ConnectionType.DirectConnection)

    worker.submit_hover(_snap_request(1))
    assert harness.entered.wait(1.0)
    worker.submit_hover(_snap_request(2))
    worker.submit_hover(_snap_request(3))
    worker.submit_click(_snap_request(4, purpose="click"))
    worker.submit_click(_snap_request(5, purpose="click"))
    harness.release.set()
    _wait_until(lambda: len(responses) == 4)
    worker.stop()

    assert [request_id for request_id, _thread_id in harness.snap_calls] == [1, 4, 5, 3]
    assert [response.request_id for response in responses] == [1, 4, 5, 3]


def test_workers_lazily_create_independent_single_owner_daemon_threads() -> None:
    render_harness = _RenderHarness(block_ids={1})
    snap_harness = _SnapHarness(block_ids={2})
    render_worker = KLayoutRenderWorker(backend_factory=render_harness.factory)
    snap_worker = KLayoutSnapWorker(backend_factory=snap_harness.factory)

    assert render_harness.factory_threads == []
    assert snap_harness.factory_threads == []
    render_worker.submit(_render_request(1))
    snap_worker.submit_hover(_snap_request(2))
    assert render_harness.entered.wait(1.0)
    assert snap_harness.entered.wait(1.0)

    render_thread = render_harness.factory_threads[0]
    snap_thread = snap_harness.factory_threads[0]
    assert render_thread.daemon is True
    assert snap_thread.daemon is True
    assert render_thread.ident != snap_thread.ident
    render_harness.release.set()
    snap_harness.release.set()
    render_worker.stop()
    snap_worker.stop()

    assert len(render_harness.factory_threads) == 1
    assert len(snap_harness.factory_threads) == 1
    render_owner = render_thread.ident
    snap_owner = snap_thread.ident
    assert render_owner is not None and snap_owner is not None
    assert {thread_id for _generation, thread_id in render_harness.ensure_calls} == {render_owner}
    assert {thread_id for _request_id, thread_id in render_harness.render_calls} == {render_owner}
    assert set(render_harness.close_threads) == {render_owner}
    assert {thread_id for _generation, thread_id in snap_harness.ensure_calls} == {snap_owner}
    assert {thread_id for _request_id, thread_id in snap_harness.snap_calls} == {snap_owner}
    assert set(snap_harness.close_threads) == {snap_owner}


def test_new_configuration_replaces_stale_render_and_suppresses_its_late_frame() -> None:
    harness = _RenderHarness(block_ids={1})
    worker = KLayoutRenderWorker(backend_factory=harness.factory)
    frames: list[RenderFrame] = []
    worker.frame_ready.connect(frames.append, Qt.ConnectionType.DirectConnection)
    config_1 = _config(generation=1)
    config_2 = replace(config_1, visible_layers=frozenset({(2, 0)}), generation=2)

    worker.submit(_render_request(1, config_1))
    assert harness.entered.wait(1.0)
    worker.submit(_render_request(2, config_1))
    worker.submit(_render_request(3, config_2))
    harness.release.set()
    _wait_until(lambda: [item[0] for item in harness.render_calls] == [1, 3])
    _wait_until(lambda: [frame.request_id for frame in frames] == [3])
    worker.stop()

    assert [generation for generation, _thread_id in harness.ensure_calls] == [1, 2]
    assert len(harness.factory_threads) == 1


def test_success_signals_are_never_emitted_while_condition_is_owned() -> None:
    render_harness = _RenderHarness()
    snap_harness = _SnapHarness()
    render_worker = KLayoutRenderWorker(backend_factory=render_harness.factory)
    snap_worker = KLayoutSnapWorker(backend_factory=snap_harness.factory)
    checked: list[str] = []

    def assert_render_unlocked(_value: object) -> None:
        assert render_worker._condition.acquire(blocking=False)
        render_worker._condition.release()
        checked.append("render")

    def assert_snap_unlocked(_value: object) -> None:
        assert snap_worker._condition.acquire(blocking=False)
        snap_worker._condition.release()
        checked.append("snap")

    render_worker.loaded.connect(assert_render_unlocked, Qt.ConnectionType.DirectConnection)
    render_worker.frame_ready.connect(assert_render_unlocked, Qt.ConnectionType.DirectConnection)
    snap_worker.loaded.connect(assert_snap_unlocked, Qt.ConnectionType.DirectConnection)
    snap_worker.snap_ready.connect(assert_snap_unlocked, Qt.ConnectionType.DirectConnection)

    render_worker.submit(_render_request(1))
    snap_worker.submit_hover(_snap_request(2))
    _wait_until(lambda: len(checked) == 4)
    render_worker.stop()
    snap_worker.stop()

    assert checked.count("render") == 2
    assert checked.count("snap") == 2


def test_stop_is_bounded_and_suppresses_late_success_or_failure() -> None:
    harness = _RenderHarness(block_ids={1})
    worker = KLayoutRenderWorker(backend_factory=harness.factory)
    frames: list[RenderFrame] = []
    failures: list[str] = []
    worker.frame_ready.connect(frames.append, Qt.ConnectionType.DirectConnection)
    worker.failed.connect(failures.append, Qt.ConnectionType.DirectConnection)

    worker.submit(_render_request(1))
    assert harness.entered.wait(1.0)
    started = time.monotonic()
    worker.stop(timeout_s=0.02)
    elapsed = time.monotonic() - started
    harness.release.set()
    _wait_until(lambda: bool(harness.close_threads))

    assert elapsed < 0.2
    assert frames == []
    assert failures == []


def test_failed_signal_is_emitted_unlocked_and_worker_stops_after_backend_error() -> None:
    class FailingBackend:
        def ensure_config(self, _config: KLayoutConfig) -> None:
            pass

        def render(self, _request: RenderRequest) -> RenderFrame:
            raise RuntimeError("render exploded")

        def close(self) -> None:
            pass

    worker = KLayoutRenderWorker(backend_factory=FailingBackend)
    failures: list[str] = []

    def collect_failure(message: str) -> None:
        assert worker._condition.acquire(blocking=False)
        worker._condition.release()
        failures.append(message)

    worker.failed.connect(collect_failure, Qt.ConnectionType.DirectConnection)
    worker.submit(_render_request(1))
    _wait_until(lambda: bool(failures))
    worker.stop()

    assert failures == ["RuntimeError: render exploded"]


def test_stop_before_submit_does_not_create_a_thread_and_future_submit_is_ignored() -> None:
    harness = _RenderHarness()
    worker = KLayoutRenderWorker(backend_factory=harness.factory)

    worker.stop(timeout_s=0.01)
    worker.submit(_render_request(1))
    time.sleep(0.02)

    assert harness.factory_threads == []
