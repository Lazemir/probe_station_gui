from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import threading
import time
from typing import Any, Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
import pytest

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    RenderFailure,
    RenderFrame,
    RenderRequest,
    SnapRequest,
    SnapResponse,
    SnapFailure,
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


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _process_until(
    app: QApplication,
    predicate: Callable[[], bool],
    timeout_s: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for queued Qt delivery")


def _drain_events(app: QApplication) -> None:
    for _ in range(5):
        app.processEvents()


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


def _lifecycle_state(
    worker: KLayoutRenderWorker | KLayoutSnapWorker,
) -> tuple[object, ...]:
    common = (
        worker._stopping,
        worker._stop_requested.is_set(),
        worker._thread,
        worker._latest_config,
    )
    if isinstance(worker, KLayoutRenderWorker):
        return (*common, worker._pending)
    return (*common, worker._hover, tuple(worker._clicks))


@pytest.mark.parametrize(
    ("worker_kind", "method_name"),
    [
        ("render", "submit"),
        ("render", "stop"),
        ("snap", "submit_hover"),
        ("snap", "submit_click"),
        ("snap", "stop"),
    ],
)
def test_public_lifecycle_methods_reject_foreign_threads_without_mutation(
    worker_kind: str,
    method_name: str,
) -> None:
    worker: KLayoutRenderWorker | KLayoutSnapWorker
    if worker_kind == "render":
        worker = KLayoutRenderWorker(backend_factory=_RenderHarness().factory)
    else:
        worker = KLayoutSnapWorker(backend_factory=_SnapHarness().factory)
    before = _lifecycle_state(worker)
    errors: list[BaseException] = []

    def invoke() -> None:
        try:
            if method_name == "submit":
                assert isinstance(worker, KLayoutRenderWorker)
                worker.submit(_render_request(1))
            elif method_name == "submit_hover":
                assert isinstance(worker, KLayoutSnapWorker)
                worker.submit_hover(_snap_request(1))
            elif method_name == "submit_click":
                assert isinstance(worker, KLayoutSnapWorker)
                worker.submit_click(_snap_request(1, purpose="click"))
            else:
                worker.stop(timeout_s=0.0)
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=invoke)
    caller.start()
    caller.join(1.0)
    after = _lifecycle_state(worker)
    worker.stop(timeout_s=1.0)

    assert caller.is_alive() is False
    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert str(errors[0]) == "KLayout worker methods must be called from the creator thread."
    assert after == before


def test_render_keeps_only_newest_pending_request(qt_app: QApplication) -> None:
    harness = _RenderHarness(block_ids={1})
    worker = KLayoutRenderWorker(backend_factory=harness.factory)
    frames: list[RenderFrame] = []
    worker.frame_ready.connect(frames.append, Qt.ConnectionType.DirectConnection)

    worker.submit(_render_request(1))
    assert harness.entered.wait(1.0)
    worker.submit(_render_request(2))
    worker.submit(_render_request(3))
    harness.release.set()
    _process_until(qt_app, lambda: len(frames) == 2)
    worker.stop()

    assert [request_id for request_id, _thread_id in harness.render_calls] == [1, 3]
    assert [frame.request_id for frame in frames] == [1, 3]


def test_snap_keeps_newest_hover_but_prioritizes_fifo_clicks(
    qt_app: QApplication,
) -> None:
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
    _process_until(qt_app, lambda: len(responses) == 4)
    worker.stop()

    assert [request_id for request_id, _thread_id in harness.snap_calls] == [1, 4, 5, 3]
    assert [response.request_id for response in responses] == [1, 4, 5, 3]


def test_stale_hover_after_prioritized_click_does_not_exit_snap_thread(
    qt_app: QApplication,
) -> None:
    harness = _SnapHarness()
    factory_entered = threading.Event()
    release_factory = threading.Event()

    def blocked_factory() -> Any:
        factory_entered.set()
        release_factory.wait(1.0)
        return harness.factory()

    worker = KLayoutSnapWorker(backend_factory=blocked_factory)
    responses: list[SnapResponse] = []
    worker.snap_ready.connect(responses.append, Qt.ConnectionType.DirectConnection)
    config_a = _config(generation=1)
    config_b = replace(config_a, visible_layers=frozenset({(2, 0)}), generation=2)

    worker.submit_hover(_snap_request(1, config=config_a))
    assert factory_entered.wait(1.0)
    worker.submit_click(_snap_request(2, purpose="click", config=config_b))
    release_factory.set()
    _process_until(
        qt_app,
        lambda: [response.request_id for response in responses] == [2],
    )
    worker.submit_hover(_snap_request(3, config=config_b))
    _process_until(
        qt_app,
        lambda: [response.request_id for response in responses] == [2, 3],
    )
    worker.stop()

    assert [request_id for request_id, _thread_id in harness.snap_calls] == [2, 3]


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


def test_new_configuration_replaces_stale_render_and_suppresses_its_late_frame(
    qt_app: QApplication,
) -> None:
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
    _process_until(qt_app, lambda: [frame.request_id for frame in frames] == [3])
    worker.stop()

    assert [generation for generation, _thread_id in harness.ensure_calls] == [1, 2]
    assert len(harness.factory_threads) == 1


def test_success_signals_are_never_emitted_while_condition_is_owned(
    qt_app: QApplication,
) -> None:
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

    def assert_render_publication_unlocked(_value: object) -> None:
        assert render_worker._condition.acquire(blocking=False)
        render_worker._condition.release()
        checked.append("render-private")

    def assert_snap_publication_unlocked(_value: object) -> None:
        assert snap_worker._condition.acquire(blocking=False)
        snap_worker._condition.release()
        checked.append("snap-private")

    render_worker.loaded.connect(assert_render_unlocked, Qt.ConnectionType.DirectConnection)
    render_worker.frame_ready.connect(assert_render_unlocked, Qt.ConnectionType.DirectConnection)
    snap_worker.loaded.connect(assert_snap_unlocked, Qt.ConnectionType.DirectConnection)
    snap_worker.snap_ready.connect(assert_snap_unlocked, Qt.ConnectionType.DirectConnection)
    render_worker._publication_posted.connect(
        assert_render_publication_unlocked,
        Qt.ConnectionType.DirectConnection,
    )
    snap_worker._publication_posted.connect(
        assert_snap_publication_unlocked,
        Qt.ConnectionType.DirectConnection,
    )

    render_worker.submit(_render_request(1))
    snap_worker.submit_hover(_snap_request(2))
    _process_until(qt_app, lambda: len(checked) == 8)
    render_worker.stop()
    snap_worker.stop()

    assert checked.count("render") == 2
    assert checked.count("snap") == 2
    assert checked.count("render-private") == 2
    assert checked.count("snap-private") == 2


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


def test_stop_returns_promptly_and_drops_queued_publications(
    qt_app: QApplication,
) -> None:
    harness = _RenderHarness()
    worker = KLayoutRenderWorker(backend_factory=harness.factory)
    loaded: list[KLayoutConfig] = []
    frames: list[RenderFrame] = []
    failures: list[str] = []
    frame_posted = threading.Event()

    def observe_publication(publication: object) -> None:
        if getattr(publication, "kind") == "frame_ready":
            frame_posted.set()

    worker._publication_posted.connect(
        observe_publication,
        Qt.ConnectionType.DirectConnection,
    )
    worker.loaded.connect(loaded.append, Qt.ConnectionType.DirectConnection)
    worker.frame_ready.connect(frames.append, Qt.ConnectionType.DirectConnection)
    worker.failed.connect(failures.append, Qt.ConnectionType.DirectConnection)

    worker.submit(_render_request(1))
    assert frame_posted.wait(1.0)
    assert loaded == [] and frames == [] and failures == []
    started = time.monotonic()
    worker.stop(timeout_s=0.0)
    elapsed = time.monotonic() - started
    _drain_events(qt_app)
    _wait_until(lambda: bool(harness.close_threads))

    assert elapsed < 0.2
    assert loaded == []
    assert frames == []
    assert failures == []


def test_new_config_drops_queued_old_generation_before_gui_delivery(
    qt_app: QApplication,
) -> None:
    harness = _RenderHarness(block_ids={2})
    worker = KLayoutRenderWorker(backend_factory=harness.factory)
    frames: list[RenderFrame] = []
    posted_frame_ids: list[int] = []
    old_frame_posted = threading.Event()
    new_frame_posted = threading.Event()

    def observe_publication(publication: object) -> None:
        if getattr(publication, "kind") != "frame_ready":
            return
        request_id = getattr(publication, "value").request_id
        posted_frame_ids.append(request_id)
        (old_frame_posted if request_id == 1 else new_frame_posted).set()

    worker._publication_posted.connect(
        observe_publication,
        Qt.ConnectionType.DirectConnection,
    )
    worker.frame_ready.connect(frames.append, Qt.ConnectionType.DirectConnection)
    config_a = _config(generation=1)
    config_b = replace(config_a, visible_layers=frozenset({(2, 0)}), generation=2)

    worker.submit(_render_request(1, config_a))
    assert old_frame_posted.wait(1.0)
    worker.submit(_render_request(2, config_b))
    assert harness.entered.wait(1.0)
    _drain_events(qt_app)
    assert frames == []
    harness.release.set()
    assert new_frame_posted.wait(1.0)
    _process_until(qt_app, lambda: len(frames) == 1)
    worker.stop()

    assert posted_frame_ids == [1, 2]
    assert [frame.request_id for frame in frames] == [2]


def test_all_public_signals_are_delivered_on_worker_creator_thread(
    qt_app: QApplication,
) -> None:
    class FailingRenderBackend:
        def ensure_config(self, _config: KLayoutConfig) -> None:
            raise RuntimeError("render config exploded")

        def render(self, _request: RenderRequest) -> RenderFrame:
            raise AssertionError("render must not run after config failure")

        def close(self) -> None:
            pass

    class FailingSnapBackend:
        def ensure_config(self, _config: KLayoutConfig) -> None:
            raise RuntimeError("snap config exploded")

        def snap(self, _request: SnapRequest) -> SnapResponse:
            raise AssertionError("snap must not run after config failure")

        def close(self) -> None:
            pass

    creator_thread_id = threading.get_ident()
    deliveries: list[tuple[str, int]] = []

    def record(label: str) -> Callable[[object], None]:
        return lambda _value: deliveries.append((label, threading.get_ident()))

    render_worker = KLayoutRenderWorker(backend_factory=_RenderHarness().factory)
    snap_worker = KLayoutSnapWorker(backend_factory=_SnapHarness().factory)
    failed_render_worker = KLayoutRenderWorker(backend_factory=FailingRenderBackend)
    failed_snap_worker = KLayoutSnapWorker(backend_factory=FailingSnapBackend)
    render_worker.loaded.connect(record("render-loaded"), Qt.ConnectionType.DirectConnection)
    render_worker.frame_ready.connect(
        record("frame-ready"),
        Qt.ConnectionType.DirectConnection,
    )
    snap_worker.loaded.connect(record("snap-loaded"), Qt.ConnectionType.DirectConnection)
    snap_worker.snap_ready.connect(record("snap-ready"), Qt.ConnectionType.DirectConnection)
    failed_render_worker.failed.connect(
        record("render-failed"),
        Qt.ConnectionType.DirectConnection,
    )
    failed_snap_worker.failed.connect(
        record("snap-failed"),
        Qt.ConnectionType.DirectConnection,
    )

    render_worker.submit(_render_request(1))
    snap_worker.submit_hover(_snap_request(2))
    failed_render_worker.submit(_render_request(3))
    failed_snap_worker.submit_hover(_snap_request(4))
    _process_until(qt_app, lambda: len(deliveries) == 6)
    render_worker.stop()
    snap_worker.stop()
    failed_render_worker.stop()
    failed_snap_worker.stop()

    assert {label for label, _thread_id in deliveries} == {
        "render-loaded",
        "frame-ready",
        "snap-loaded",
        "snap-ready",
        "render-failed",
        "snap-failed",
    }
    assert {thread_id for _label, thread_id in deliveries} == {creator_thread_id}


def test_failed_signal_is_emitted_unlocked_and_worker_stops_after_backend_error(
    qt_app: QApplication,
) -> None:
    class FailingBackend:
        def ensure_config(self, _config: KLayoutConfig) -> None:
            pass

        def render(self, _request: RenderRequest) -> RenderFrame:
            raise RuntimeError("render exploded")

        def close(self) -> None:
            pass

    worker = KLayoutRenderWorker(backend_factory=FailingBackend)
    failures: list[RenderFailure] = []

    def collect_failure(failure: RenderFailure) -> None:
        assert worker._condition.acquire(blocking=False)
        worker._condition.release()
        failures.append(failure)

    worker.failed.connect(collect_failure, Qt.ConnectionType.DirectConnection)
    worker.submit(_render_request(1))
    _process_until(qt_app, lambda: bool(failures))
    worker.stop()

    assert len(failures) == 1
    assert failures[0].request_id == 1
    assert failures[0].message == "RuntimeError: render exploded"


def test_stop_before_submit_does_not_create_a_thread_and_future_submit_is_ignored() -> None:
    harness = _RenderHarness()
    worker = KLayoutRenderWorker(backend_factory=harness.factory)

    worker.stop(timeout_s=0.01)
    worker.submit(_render_request(1))
    time.sleep(0.02)

    assert harness.factory_threads == []


@pytest.mark.parametrize("kind", ["render", "snap"])
def test_request_failure_keeps_request_correlation(
    qt_app: QApplication,
    kind: str,
) -> None:
    if kind == "render":
        class Backend:
            def ensure_config(self, _config: KLayoutConfig) -> None:
                pass

            def render(self, _request: RenderRequest) -> RenderFrame:
                raise RuntimeError("render exploded")

            def close(self) -> None:
                pass

        worker = KLayoutRenderWorker(backend_factory=Backend)
        failures: list[RenderFailure] = []
        worker.failed.connect(failures.append)
        request = _render_request(31)
        worker.submit(request)
    else:
        class Backend:
            def ensure_config(self, _config: KLayoutConfig) -> None:
                pass

            def snap(self, _request: SnapRequest) -> SnapResponse:
                raise RuntimeError("snap exploded")

            def close(self) -> None:
                pass

        worker = KLayoutSnapWorker(backend_factory=Backend)
        failures: list[SnapFailure] = []
        request = _snap_request(32, purpose="click")
        worker.failed.connect(failures.append)
        worker.submit_click(request)

    _process_until(qt_app, lambda: bool(failures))
    worker.stop()

    failure = failures[0]
    assert failure.request_id == request.request_id
    assert failure.config_generation == request.config.generation
    assert failure.purpose == request.purpose
    assert failure.message.endswith("exploded")
    if kind == "render":
        assert failure.viewport_generation == request.viewport_generation


@pytest.mark.parametrize("kind", ["render", "snap"])
def test_finished_is_delivered_on_creator_thread_after_bounded_stop(
    qt_app: QApplication,
    kind: str,
) -> None:
    creator_thread = threading.get_ident()
    harness = (
        _RenderHarness(block_ids={41})
        if kind == "render"
        else _SnapHarness(block_ids={41})
    )
    worker = (
        KLayoutRenderWorker(backend_factory=harness.factory)
        if kind == "render"
        else KLayoutSnapWorker(backend_factory=harness.factory)
    )
    finished_threads: list[int] = []
    worker.finished.connect(lambda: finished_threads.append(threading.get_ident()))
    if kind == "render":
        worker.submit(_render_request(41))
    else:
        worker.submit_hover(_snap_request(41))
    assert harness.entered.wait(1.0)

    started = time.monotonic()
    worker.stop(0.0)
    assert time.monotonic() - started < 0.1
    assert finished_threads == []
    harness.release.set()
    _process_until(qt_app, lambda: bool(finished_threads))

    assert finished_threads == [creator_thread]
