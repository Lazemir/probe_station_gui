from __future__ import annotations

import json
import os
import threading
import time

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.settings.manager import SettingsManager
from probe_station_gui.settings.selection_persistence import (
    SoftwareCoordinateSelectionSnapshot,
)
from probe_station_gui.settings.software_coordinate_selection_store import (
    SoftwareCoordinateSelectionSaveFailure,
    SoftwareCoordinateSelectionStoreWorker,
)


FRAME_ONE = "11111111-1111-4111-8111-111111111111"
FRAME_TWO = "22222222-2222-4222-8222-222222222222"
FRAME_THREE = "33333333-3333-4333-8333-333333333333"


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _wait_until(
    app: QApplication,
    predicate,
    *,
    timeout_s: float = 1.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()


def _manager(tmp_path, monkeypatch) -> SettingsManager:
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setattr(
        "probe_station_gui.settings.manager.platform.system",
        lambda: "Windows",
    )
    return SettingsManager()


def test_worker_persists_off_creator_thread_and_coalesces_to_latest_value(
    qt_app: QApplication,
) -> None:
    creator_thread = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    first = SoftwareCoordinateSelectionSnapshot(FRAME_ONE, 1)
    second = SoftwareCoordinateSelectionSnapshot(FRAME_TWO, 2)
    newest = SoftwareCoordinateSelectionSnapshot(FRAME_THREE, 3)
    persisted: list[SoftwareCoordinateSelectionSnapshot] = []
    thread_ids: list[int] = []

    def persist(snapshot: SoftwareCoordinateSelectionSnapshot) -> bool:
        thread_ids.append(threading.get_ident())
        persisted.append(snapshot)
        if len(persisted) == 1:
            entered.set()
            assert release.wait(timeout=2.0)
        return True

    worker = SoftwareCoordinateSelectionStoreWorker(persist)
    worker.publish(first)
    assert entered.wait(timeout=1.0)
    worker.publish(second)
    worker.publish(newest)
    release.set()
    _wait_until(qt_app, lambda: worker.is_idle)

    assert persisted == [first, newest]
    assert set(thread_ids) == {thread_ids[0]}
    assert thread_ids[0] != creator_thread
    worker.stop()


def test_worker_reports_persistence_failure_on_creator_thread(
    qt_app: QApplication,
) -> None:
    creator_thread = threading.get_ident()
    delivered: list[tuple[int, SoftwareCoordinateSelectionSaveFailure]] = []
    snapshot = SoftwareCoordinateSelectionSnapshot(FRAME_ONE, 9)

    def fail(_snapshot: SoftwareCoordinateSelectionSnapshot) -> bool:
        raise OSError("disk unavailable")

    worker = SoftwareCoordinateSelectionStoreWorker(fail)
    worker.failed.connect(
        lambda failure: delivered.append((threading.get_ident(), failure))
    )
    worker.publish(snapshot)
    _wait_until(qt_app, lambda: bool(delivered))

    thread_id, failure = delivered[0]
    assert thread_id == creator_thread
    assert failure == SoftwareCoordinateSelectionSaveFailure(
        FRAME_ONE,
        9,
        "OSError: disk unavailable",
    )
    worker.stop()


def test_zero_timeout_stop_keeps_nondaemon_drain_and_pending_latest_value() -> None:
    entered = threading.Event()
    release = threading.Event()
    first = SoftwareCoordinateSelectionSnapshot(FRAME_ONE, 1)
    newest = SoftwareCoordinateSelectionSnapshot(FRAME_TWO, 2)
    persisted: list[SoftwareCoordinateSelectionSnapshot] = []

    def persist(snapshot: SoftwareCoordinateSelectionSnapshot) -> bool:
        persisted.append(snapshot)
        if len(persisted) == 1:
            entered.set()
            assert release.wait(timeout=2.0)
        return True

    worker = SoftwareCoordinateSelectionStoreWorker(persist)
    worker.publish(first)
    assert entered.wait(timeout=1.0)
    worker.publish(newest)

    started_at = time.perf_counter()
    worker.stop(timeout_s=0.0)
    assert time.perf_counter() - started_at < 0.1
    drain = worker.drain_thread
    assert drain is not None and drain.is_alive() and not drain.daemon

    release.set()
    drain.join(timeout=1.0)
    assert not drain.is_alive()
    assert persisted == [first, newest]


def test_worker_requires_canonical_immutable_snapshot() -> None:
    worker = SoftwareCoordinateSelectionStoreWorker(lambda _snapshot: True)

    with pytest.raises(TypeError, match="immutable snapshot"):
        worker.publish((FRAME_ONE, 1))  # type: ignore[arg-type]

    worker.stop()


def test_manager_keeps_new_selection_across_stale_dialog_save_and_restart(
    tmp_path,
    monkeypatch,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    stale_dialog_settings = manager.settings.clone()
    stale_dialog_settings.software_coordinates.last_selected_frame_id = FRAME_ONE
    snapshot = manager.set_software_coordinate_selection(FRAME_TWO)

    assert isinstance(snapshot, SoftwareCoordinateSelectionSnapshot)
    assert manager.persist_software_coordinate_selection(snapshot)
    manager.replace_and_save(stale_dialog_settings, apply_runtime=False)

    main = json.loads(
        (manager.config_dir() / SettingsManager.CONFIG_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert main["software_coordinates"]["last_selected_frame_id"] == FRAME_TWO
    restarted = SettingsManager()
    assert restarted.settings.software_coordinates.last_selected_frame_id == FRAME_TWO
    assert restarted.settings.software_coordinates.selection_generation == 1


def test_latest_value_worker_persists_manager_selection_and_unrelated_setting(
    qt_app: QApplication,
    tmp_path,
    monkeypatch,
) -> None:
    manager = _manager(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()

    def persist(snapshot: SoftwareCoordinateSelectionSnapshot) -> bool:
        if snapshot.frame_id == FRAME_ONE:
            entered.set()
            assert release.wait(timeout=2.0)
        return manager.persist_software_coordinate_selection(snapshot)

    worker = SoftwareCoordinateSelectionStoreWorker(persist)
    worker.publish(manager.set_software_coordinate_selection(FRAME_ONE))
    assert entered.wait(timeout=1.0)
    newest = manager.set_software_coordinate_selection(FRAME_THREE)
    manager.update_and_save(
        lambda settings: setattr(settings, "design_last_directory", "C:/latest")
    )
    worker.publish(newest)
    release.set()
    _wait_until(qt_app, lambda: worker.is_idle)
    worker.stop()

    main = json.loads(
        (manager.config_dir() / SettingsManager.CONFIG_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    sidecar = json.loads(
        (
            manager.config_dir()
            / SettingsManager.SOFTWARE_COORDINATE_SELECTION_FILENAME
        ).read_text(encoding="utf-8")
    )
    assert main["design_last_directory"] == "C:/latest"
    assert main["software_coordinates"]["last_selected_frame_id"] == FRAME_THREE
    assert sidecar == {
        "version": 1,
        "last_selected_frame_id": FRAME_THREE,
        "generation": 2,
    }
