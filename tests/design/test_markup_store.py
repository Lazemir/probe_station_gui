from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.markup_store import (
    FilesystemMarkupStoreBackend,
    MarkupStoreFailure,
    MarkupStoreWorker,
    StoreLoadResult,
    StoreSuccess,
    markup_path_for_source,
)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _wait_until(
    qt_app: QApplication,
    predicate,
    *,
    timeout: float = 2.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        qt_app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    qt_app.processEvents()
    assert predicate()


def _document(tmp_path: Path, guide_count: int = 1) -> MarkupDocument:
    source = tmp_path / "chip.gds"
    if not source.exists():
        source.write_bytes(b"gds")
    document = MarkupDocument.empty(source)
    for index in range(guide_count):
        document = document.append_guide(
            (float(index), 0.0),
            (float(index), 1.0),
            guide_id=f"guide-{index}",
        )
    return document


def test_markup_path_is_stable_hash_under_requested_root(tmp_path: Path) -> None:
    source = tmp_path / "Designs" / "chip.gds"
    source.parent.mkdir()
    source.write_bytes(b"gds")

    first = markup_path_for_source(source, root=tmp_path / "markup")
    second = markup_path_for_source(
        source.parent / "." / "CHIP.gds",
        root=tmp_path / "markup",
    )

    assert first == second
    assert first.parent == tmp_path / "markup"
    assert first.suffix == ".json"
    assert len(first.stem) == 64


def test_filesystem_backend_round_trip_is_atomic(tmp_path: Path) -> None:
    document = _document(tmp_path)
    root = tmp_path / "markup"
    backend = FilesystemMarkupStoreBackend(root=root)

    backend.save(document)

    assert backend.load(document.source_path) == document
    assert list(root.glob("*.tmp")) == []
    payload = json.loads(
        markup_path_for_source(document.source_path, root=root).read_text(
            encoding="utf-8"
        )
    )
    assert payload["guides"][0]["id"] == "guide-0"


def test_worker_saves_loads_and_deletes_off_creator_thread(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    document = _document(tmp_path)
    root = tmp_path / "markup"
    creator_thread = threading.get_ident()
    backend = _RecordingBackend(root)
    worker = MarkupStoreWorker(backend_factory=lambda: backend)
    saved: list[StoreSuccess] = []
    loaded: list[StoreLoadResult] = []
    deleted: list[StoreSuccess] = []
    worker.saved.connect(saved.append)
    worker.loaded.connect(loaded.append)
    worker.deleted.connect(deleted.append)

    worker.publish(1, document)
    _wait_until(qt_app, lambda: len(saved) == 1)
    worker.load(2, document.source_path)
    _wait_until(qt_app, lambda: len(loaded) == 1)
    worker.delete(3, document.source_path)
    _wait_until(qt_app, lambda: len(deleted) == 1)

    assert saved == [StoreSuccess(1, "save", document.source_path)]
    assert loaded == [StoreLoadResult(2, document.source_path, document)]
    assert deleted == [StoreSuccess(3, "delete", document.source_path)]
    assert backend.thread_ids
    assert set(backend.thread_ids) == {backend.thread_ids[0]}
    assert backend.thread_ids[0] != creator_thread
    worker.stop()


def test_pending_saves_for_one_source_coalesce_to_newest_snapshot(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    first = _document(tmp_path, guide_count=1)
    second = _document(tmp_path, guide_count=2)
    newest = _document(tmp_path, guide_count=3)
    backend = _BlockingBackend(tmp_path / "markup")
    worker = MarkupStoreWorker(backend_factory=lambda: backend)

    worker.publish(1, first)
    assert backend.started.wait(timeout=1.0)
    worker.publish(2, second)
    worker.publish(3, newest)
    backend.release.set()
    _wait_until(qt_app, lambda: worker.is_idle)

    assert backend.saved_documents == [first, newest]
    worker.stop()


def test_zero_timeout_stop_returns_immediately_but_keeps_a_nondaemon_drain(
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    backend = _BlockingBackend(tmp_path / "markup")
    worker = MarkupStoreWorker(backend_factory=lambda: backend)
    worker.publish(1, document)
    assert backend.started.wait(timeout=1.0)

    started_at = time.perf_counter()
    worker.stop(timeout_s=0.0)
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.1
    drain = worker.drain_thread
    assert drain is not None and drain.is_alive()
    assert not drain.daemon

    backend.release.set()
    drain.join(timeout=1.0)
    assert not drain.is_alive()
    assert backend.saved_documents == [document]


def test_delete_and_reload_remain_ordered_behind_an_active_save(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    document = _document(tmp_path)
    backend = _OrderingBackend()
    worker = MarkupStoreWorker(backend_factory=lambda: backend)
    loads: list[StoreLoadResult] = []
    worker.loaded.connect(loads.append)

    worker.publish(1, document)
    assert backend.started.wait(timeout=1.0)
    worker.delete(2, document.source_path)
    worker.load(3, document.source_path)
    backend.release.set()
    _wait_until(qt_app, lambda: bool(loads))

    assert backend.operations == ["save", "delete", "load"]
    assert loads == [StoreLoadResult(3, document.source_path, None)]
    worker.stop()


def test_corrupt_json_reports_load_failure_without_document(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    document = _document(tmp_path)
    root = tmp_path / "markup"
    path = markup_path_for_source(document.source_path, root=root)
    path.parent.mkdir(parents=True)
    path.write_text("not json", encoding="utf-8")
    worker = MarkupStoreWorker(
        backend_factory=lambda: FilesystemMarkupStoreBackend(root=root)
    )
    failures: list[MarkupStoreFailure] = []
    loads: list[StoreLoadResult] = []
    worker.failed.connect(failures.append)
    worker.loaded.connect(loads.append)

    worker.load(7, document.source_path)
    _wait_until(qt_app, lambda: bool(failures))

    assert loads == []
    assert failures[0].request_id == 7
    assert failures[0].operation == "load"
    assert failures[0].source_path == document.source_path
    worker.stop()


def test_delete_missing_sidecar_is_successful(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    document = _document(tmp_path)
    worker = MarkupStoreWorker(
        backend_factory=lambda: FilesystemMarkupStoreBackend(root=tmp_path / "markup")
    )
    deleted: list[StoreSuccess] = []
    worker.deleted.connect(deleted.append)

    worker.delete(8, document.source_path)
    _wait_until(qt_app, lambda: bool(deleted))

    assert deleted[0].request_id == 8
    worker.stop()


class _RecordingBackend(FilesystemMarkupStoreBackend):
    def __init__(self, root: Path) -> None:
        super().__init__(root=root)
        self.thread_ids: list[int] = []

    def load(self, source_path: str) -> MarkupDocument | None:
        self.thread_ids.append(threading.get_ident())
        return super().load(source_path)

    def save(self, document: MarkupDocument) -> None:
        self.thread_ids.append(threading.get_ident())
        super().save(document)

    def delete(self, source_path: str) -> None:
        self.thread_ids.append(threading.get_ident())
        super().delete(source_path)


class _BlockingBackend(_RecordingBackend):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.started = threading.Event()
        self.release = threading.Event()
        self.saved_documents: list[MarkupDocument] = []

    def save(self, document: MarkupDocument) -> None:
        self.thread_ids.append(threading.get_ident())
        self.saved_documents.append(document)
        if len(self.saved_documents) == 1:
            self.started.set()
            assert self.release.wait(timeout=2.0)


class _OrderingBackend:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()
        self.operations: list[str] = []

    def save(self, _document: MarkupDocument) -> None:
        self.operations.append("save")
        self.started.set()
        assert self.release.wait(timeout=2.0)

    def delete(self, _source_path: str) -> None:
        self.operations.append("delete")

    def load(self, _source_path: str) -> MarkupDocument | None:
        self.operations.append("load")
        return None
