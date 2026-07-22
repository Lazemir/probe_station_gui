from __future__ import annotations

import os
import json
import logging
import threading
import time

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.settings.manager import Settings, SettingsManager
from probe_station_gui.settings import manager as settings_manager_module
from probe_station_gui.settings.software_coordinate_selection_store import (
    SoftwareCoordinateSelectionSaveFailure,
    SoftwareCoordinateSelectionStoreWorker,
)


FRAME_ONE = "11111111-1111-4111-8111-111111111111"
FRAME_NEWEST = "22222222-2222-4222-8222-222222222222"
FRAME_RESTORED = "33333333-3333-4333-8333-333333333333"


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


def test_worker_persists_off_creator_thread_and_coalesces_to_latest_value(
    qt_app: QApplication,
) -> None:
    creator_thread = threading.get_ident()
    entered = threading.Event()
    release = threading.Event()
    persisted: list[str] = []
    thread_ids: list[int] = []

    def persist(frame_id: str) -> bool:
        thread_ids.append(threading.get_ident())
        persisted.append(frame_id)
        if len(persisted) == 1:
            entered.set()
            assert release.wait(timeout=2.0)
        return True

    worker = SoftwareCoordinateSelectionStoreWorker(persist)
    worker.publish("frame-one")
    assert entered.wait(timeout=1.0)
    worker.publish("frame-two")
    worker.publish("frame-newest")
    release.set()
    _wait_until(qt_app, lambda: worker.is_idle)

    assert persisted == ["frame-one", "frame-newest"]
    assert set(thread_ids) == {thread_ids[0]}
    assert thread_ids[0] != creator_thread
    worker.stop()


def test_worker_reports_persistence_failure_on_creator_thread(
    qt_app: QApplication,
) -> None:
    creator_thread = threading.get_ident()
    delivered: list[tuple[int, SoftwareCoordinateSelectionSaveFailure]] = []

    def fail(_frame_id: str) -> bool:
        raise OSError("disk unavailable")

    worker = SoftwareCoordinateSelectionStoreWorker(fail)
    worker.failed.connect(
        lambda failure: delivered.append((threading.get_ident(), failure))
    )
    worker.publish("frame-one")
    _wait_until(qt_app, lambda: bool(delivered))

    thread_id, failure = delivered[0]
    assert thread_id == creator_thread
    assert failure.frame_id == "frame-one"
    assert failure.message == "OSError: disk unavailable"
    worker.stop()


def test_zero_timeout_stop_keeps_explicit_nondaemon_drain_and_last_value() -> None:
    entered = threading.Event()
    release = threading.Event()
    persisted: list[str] = []

    def persist(frame_id: str) -> bool:
        persisted.append(frame_id)
        if len(persisted) == 1:
            entered.set()
            assert release.wait(timeout=2.0)
        return True

    worker = SoftwareCoordinateSelectionStoreWorker(persist)
    worker.publish("frame-one")
    assert entered.wait(timeout=1.0)
    worker.publish("frame-newest")

    started_at = time.perf_counter()
    worker.stop(timeout_s=0.0)
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.1
    drain = worker.drain_thread
    assert drain is not None and drain.is_alive()
    assert not drain.daemon

    release.set()
    drain.join(timeout=1.0)
    assert not drain.is_alive()
    assert persisted == ["frame-one", "frame-newest"]


def _settings_manager(tmp_path) -> SettingsManager:
    manager = SettingsManager.__new__(SettingsManager)
    manager._settings = Settings()
    manager._config_dir = tmp_path
    manager._config_path = tmp_path / "settings.json"
    manager._logger = logging.getLogger(__name__)
    manager._settings_lock = threading.RLock()
    manager.apply = lambda: None
    return manager


def test_in_memory_selection_update_performs_no_disk_io(tmp_path) -> None:
    manager = _settings_manager(tmp_path)
    writes: list[dict[str, object]] = []
    manager._write_settings_file_atomic = writes.append

    manager.set_software_coordinate_selection(FRAME_ONE)

    assert manager.settings.software_coordinates.last_selected_frame_id == FRAME_ONE
    assert writes == []


def test_rapid_selection_persistence_preserves_newer_unrelated_settings(
    qt_app: QApplication,
    tmp_path,
) -> None:
    manager = _settings_manager(tmp_path)
    first_entered = threading.Event()
    release_first = threading.Event()

    def persist(frame_id: str) -> bool:
        if frame_id == FRAME_ONE:
            first_entered.set()
            assert release_first.wait(timeout=2.0)
        return manager.persist_software_coordinate_selection(frame_id)

    worker = SoftwareCoordinateSelectionStoreWorker(persist)
    manager.set_software_coordinate_selection(FRAME_ONE)
    worker.publish(FRAME_ONE)
    assert first_entered.wait(timeout=1.0)

    manager.set_software_coordinate_selection(FRAME_NEWEST)
    manager.update_and_save(
        lambda settings: setattr(settings, "design_last_directory", "C:/new-design")
    )
    worker.publish(FRAME_NEWEST)
    release_first.set()
    _wait_until(qt_app, lambda: worker.is_idle)
    worker.stop()

    persisted = json.loads(manager._config_path.read_text(encoding="utf-8"))
    assert persisted["software_coordinates"]["last_selected_frame_id"] == (
        FRAME_NEWEST
    )
    assert persisted["design_last_directory"] == "C:/new-design"
    selection_document = json.loads(
        (
            manager._config_dir
            / SettingsManager.SOFTWARE_COORDINATE_SELECTION_FILENAME
        ).read_text(encoding="utf-8")
    )
    assert selection_document == {"last_selected_frame_id": FRAME_NEWEST}


def test_persisted_selection_document_overrides_settings_snapshot_on_load(
    tmp_path,
) -> None:
    manager = _settings_manager(tmp_path)
    selection_path = (
        manager._config_dir / SettingsManager.SOFTWARE_COORDINATE_SELECTION_FILENAME
    )
    selection_path.write_text(
        json.dumps({"last_selected_frame_id": FRAME_RESTORED}),
        encoding="utf-8",
    )
    settings = Settings()

    manager._restore_software_coordinate_selection(settings)

    assert settings.software_coordinates.last_selected_frame_id == FRAME_RESTORED


@pytest.mark.parametrize(
    "invalid",
    ["", "   ", "chip display name", "not-a-uuid", None, 42],
)
def test_manager_rejects_non_stable_selection_ids(tmp_path, invalid: object) -> None:
    manager = _settings_manager(tmp_path)

    with pytest.raises(ValueError, match="Machine or a coordinate-frame UUID"):
        manager.set_software_coordinate_selection(invalid)  # type: ignore[arg-type]


def test_missing_or_corrupt_sidecar_keeps_settings_selection(
    tmp_path,
) -> None:
    manager = _settings_manager(tmp_path)
    settings = Settings()
    settings.software_coordinates.last_selected_frame_id = FRAME_ONE

    manager._restore_software_coordinate_selection(settings)
    assert settings.software_coordinates.last_selected_frame_id == FRAME_ONE

    manager._software_coordinate_selection_path().write_text("{broken", encoding="utf-8")
    manager._restore_software_coordinate_selection(settings)
    assert settings.software_coordinates.last_selected_frame_id == FRAME_ONE


def test_sidecar_remains_authoritative_after_ordinary_settings_save(tmp_path) -> None:
    manager = _settings_manager(tmp_path)
    manager.set_software_coordinate_selection(FRAME_NEWEST)
    assert manager.persist_software_coordinate_selection(FRAME_NEWEST)

    stale = Settings()
    stale.software_coordinates.last_selected_frame_id = FRAME_ONE
    manager.replace_and_save(stale, apply_runtime=False)
    raw = json.loads(manager._config_path.read_text(encoding="utf-8"))
    restored = manager._settings_from_raw(raw)
    manager._restore_software_coordinate_selection(restored)

    assert restored.software_coordinates.last_selected_frame_id == FRAME_NEWEST


def test_selection_sidecar_path_uses_manager_config_directory(tmp_path) -> None:
    manager = _settings_manager(tmp_path)

    assert manager._software_coordinate_selection_path() == (
        tmp_path / SettingsManager.SOFTWARE_COORDINATE_SELECTION_FILENAME
    )


def test_atomic_selection_write_cleans_temp_and_allows_retry(
    tmp_path,
    monkeypatch,
) -> None:
    manager = _settings_manager(tmp_path)
    manager.set_software_coordinate_selection(FRAME_ONE)
    real_replace = settings_manager_module.os.replace
    attempts = 0

    def flaky_replace(source, destination) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("replace interrupted")
        real_replace(source, destination)

    monkeypatch.setattr(settings_manager_module.os, "replace", flaky_replace)

    with pytest.raises(OSError, match="replace interrupted"):
        manager.persist_software_coordinate_selection(FRAME_ONE)
    assert list(tmp_path.glob(".*.tmp")) == []

    assert manager.persist_software_coordinate_selection(FRAME_ONE)
    restored = json.loads(
        manager._software_coordinate_selection_path().read_text(encoding="utf-8")
    )
    assert restored == {"last_selected_frame_id": FRAME_ONE}
