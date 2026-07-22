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

from probe_station_gui.settings.manager import (
    Settings,
    SettingsManager,
    SoftwareCoordinateSelectionSnapshot,
)
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
    first = SoftwareCoordinateSelectionSnapshot(FRAME_ONE, 1)
    second = SoftwareCoordinateSelectionSnapshot(FRAME_NEWEST, 2)
    newest = SoftwareCoordinateSelectionSnapshot(FRAME_RESTORED, 3)
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
    assert failure.frame_id == FRAME_ONE
    assert failure.generation == 9
    assert failure.message == "OSError: disk unavailable"
    worker.stop()


def test_zero_timeout_stop_keeps_explicit_nondaemon_drain_and_last_value() -> None:
    entered = threading.Event()
    release = threading.Event()
    first = SoftwareCoordinateSelectionSnapshot(FRAME_ONE, 1)
    newest = SoftwareCoordinateSelectionSnapshot(FRAME_NEWEST, 2)
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
    elapsed = time.perf_counter() - started_at

    assert elapsed < 0.1
    drain = worker.drain_thread
    assert drain is not None and drain.is_alive()
    assert not drain.daemon

    release.set()
    drain.join(timeout=1.0)
    assert not drain.is_alive()
    assert persisted == [first, newest]


def _settings_manager(tmp_path) -> SettingsManager:
    manager = SettingsManager.__new__(SettingsManager)
    manager._settings = Settings()
    manager._config_dir = tmp_path
    manager._config_path = tmp_path / "settings.json"
    manager._logger = logging.getLogger(__name__)
    manager._settings_lock = threading.RLock()
    manager._persistence_lock = threading.RLock()
    manager.apply = lambda: None
    return manager


def _read_json(path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _restart_settings(manager: SettingsManager) -> Settings:
    restarted = _settings_manager(manager._config_path.parent)
    restarted._settings = restarted._load()
    return restarted.settings


def test_in_memory_selection_update_performs_no_disk_io(tmp_path) -> None:
    manager = _settings_manager(tmp_path)
    writes: list[dict[str, object]] = []
    manager._write_settings_file_atomic = writes.append

    snapshot = manager.set_software_coordinate_selection(FRAME_ONE)

    assert manager.settings.software_coordinates.last_selected_frame_id == FRAME_ONE
    assert manager.settings.software_coordinates.selection_generation == 1
    assert snapshot.frame_id == FRAME_ONE
    assert snapshot.generation == 1
    assert writes == []


def test_stale_replace_cannot_supersede_pending_gui_selection(tmp_path) -> None:
    manager = _settings_manager(tmp_path)
    first = manager.set_software_coordinate_selection(FRAME_ONE)
    assert manager.persist_software_coordinate_selection(first)
    stale = manager.settings.clone()

    newest = manager.set_software_coordinate_selection(FRAME_NEWEST)
    manager.replace_and_save(stale, apply_runtime=False)

    assert manager.persist_software_coordinate_selection(newest)
    persisted = _read_json(manager._config_path)
    assert persisted["software_coordinates"]["last_selected_frame_id"] == FRAME_NEWEST
    assert persisted["software_coordinates"]["selection_generation"] == 2
    restored = _restart_settings(manager)
    assert restored.software_coordinates.last_selected_frame_id == FRAME_NEWEST
    assert restored.software_coordinates.selection_generation == 2


def test_main_settings_recover_new_selection_when_sidecar_write_fails(
    tmp_path,
    monkeypatch,
) -> None:
    manager = _settings_manager(tmp_path)
    first = manager.set_software_coordinate_selection(FRAME_ONE)
    assert manager.persist_software_coordinate_selection(first)
    newest = manager.set_software_coordinate_selection(FRAME_NEWEST)

    def fail_sidecar(_snapshot) -> None:
        raise OSError("sidecar unavailable")

    monkeypatch.setattr(
        manager,
        "_write_software_coordinate_selection_atomic",
        fail_sidecar,
    )
    with pytest.raises(OSError, match="sidecar unavailable"):
        manager.persist_software_coordinate_selection(newest)
    manager.update_and_save(
        lambda settings: setattr(settings, "design_last_directory", "C:/new-design")
    )

    persisted = _read_json(manager._config_path)
    sidecar = _read_json(manager._software_coordinate_selection_path())
    assert persisted["software_coordinates"]["last_selected_frame_id"] == FRAME_NEWEST
    assert persisted["software_coordinates"]["selection_generation"] == 2
    assert sidecar["last_selected_frame_id"] == FRAME_ONE
    assert sidecar["generation"] == 1
    restored = _restart_settings(manager)
    assert restored.software_coordinates.last_selected_frame_id == FRAME_NEWEST
    assert restored.software_coordinates.selection_generation == 2


def test_settings_dialog_replacement_cannot_roll_selection_back(tmp_path) -> None:
    manager = _settings_manager(tmp_path)
    stale_dialog_snapshot = manager.settings.clone()
    stale_dialog_snapshot.software_coordinates.last_selected_frame_id = FRAME_ONE
    newest = manager.set_software_coordinate_selection(FRAME_NEWEST)
    assert manager.persist_software_coordinate_selection(newest)

    manager.replace_and_save(stale_dialog_snapshot, apply_runtime=False)

    assert manager.settings.software_coordinates.last_selected_frame_id == FRAME_NEWEST
    assert _restart_settings(manager).software_coordinates.last_selected_frame_id == (
        FRAME_NEWEST
    )


def test_newer_gui_generation_genuinely_supersedes_older_publication(tmp_path) -> None:
    manager = _settings_manager(tmp_path)
    older = manager.set_software_coordinate_selection(FRAME_NEWEST)
    newest = manager.set_software_coordinate_selection(FRAME_RESTORED)

    assert manager.persist_software_coordinate_selection(older) is False
    assert manager.persist_software_coordinate_selection(newest) is True

    restored = _restart_settings(manager)
    assert restored.software_coordinates.last_selected_frame_id == FRAME_RESTORED
    assert restored.software_coordinates.selection_generation == 2


@pytest.mark.parametrize("generation", [0, 7])
def test_equal_generations_prefer_main_settings_deterministically(
    tmp_path,
    generation: int,
) -> None:
    manager = _settings_manager(tmp_path)
    main = Settings()
    main.software_coordinates.last_selected_frame_id = FRAME_ONE
    main.software_coordinates.selection_generation = generation
    manager._write_settings_file_atomic(main.to_dict())
    sidecar: dict[str, object] = {"last_selected_frame_id": FRAME_NEWEST}
    if generation:
        sidecar.update({"version": 1, "generation": generation})
    manager._software_coordinate_selection_path().write_text(
        json.dumps(sidecar),
        encoding="utf-8",
    )

    restored = manager._load()

    assert restored.software_coordinates.last_selected_frame_id == FRAME_ONE
    assert restored.software_coordinates.selection_generation == generation


def test_higher_generation_wins_and_corrupt_generation_is_legacy_zero(
    tmp_path,
) -> None:
    manager = _settings_manager(tmp_path)
    main = Settings()
    main.software_coordinates.last_selected_frame_id = FRAME_ONE
    main.software_coordinates.selection_generation = 4
    manager._write_settings_file_atomic(main.to_dict())
    manager._software_coordinate_selection_path().write_text(
        json.dumps(
            {
                "version": 1,
                "last_selected_frame_id": FRAME_NEWEST,
                "generation": "broken",
            }
        ),
        encoding="utf-8",
    )

    restored = manager._load()

    assert restored.software_coordinates.last_selected_frame_id == FRAME_ONE
    assert restored.software_coordinates.selection_generation == 4

    manager._software_coordinate_selection_path().write_text(
        json.dumps(
            {
                "version": 1,
                "last_selected_frame_id": FRAME_RESTORED,
                "generation": 5,
            }
        ),
        encoding="utf-8",
    )
    restored = manager._load()
    assert restored.software_coordinates.last_selected_frame_id == FRAME_RESTORED
    assert restored.software_coordinates.selection_generation == 5

    manager._settings = restored
    next_selection = manager.set_software_coordinate_selection(FRAME_NEWEST)
    assert next_selection.generation == 6


def test_selection_persistence_uses_config_path_parent_without_config_dir(
    tmp_path,
) -> None:
    manager = _settings_manager(tmp_path)
    del manager._config_dir
    snapshot = manager.set_software_coordinate_selection(FRAME_ONE)

    assert manager.persist_software_coordinate_selection(snapshot)

    assert manager._software_coordinate_selection_path().parent == tmp_path
    assert manager._software_coordinate_selection_path().exists()


def test_gui_selection_does_not_wait_for_worker_filesystem_write(
    tmp_path,
    monkeypatch,
) -> None:
    manager = _settings_manager(tmp_path)
    older = manager.set_software_coordinate_selection(FRAME_ONE)
    write_entered = threading.Event()
    release_write = threading.Event()
    real_write = manager._write_settings_file_atomic

    def blocked_write(data: dict) -> None:
        write_entered.set()
        assert release_write.wait(timeout=2.0)
        real_write(data)

    monkeypatch.setattr(manager, "_write_settings_file_atomic", blocked_write)
    persisted: list[bool] = []
    persist_thread = threading.Thread(
        target=lambda: persisted.append(
            manager.persist_software_coordinate_selection(older)
        )
    )
    persist_thread.start()
    assert write_entered.wait(timeout=1.0)

    safety_release = threading.Timer(0.5, release_write.set)
    safety_release.start()
    started_at = time.perf_counter()
    newest = manager.set_software_coordinate_selection(FRAME_NEWEST)
    elapsed = time.perf_counter() - started_at
    returned_before_release = not release_write.is_set()
    release_write.set()
    safety_release.cancel()
    persist_thread.join(timeout=1.0)

    assert returned_before_release
    assert elapsed < 0.1
    assert newest.generation == 2
    assert persisted == [True]


def test_worker_and_ordinary_save_serialize_without_losing_latest_settings(
    tmp_path,
    monkeypatch,
) -> None:
    manager = _settings_manager(tmp_path)
    older = manager.set_software_coordinate_selection(FRAME_ONE)
    write_entered = threading.Event()
    release_write = threading.Event()
    real_write = manager._write_settings_file_atomic
    write_count = 0

    def block_first_write(data: dict) -> None:
        nonlocal write_count
        write_count += 1
        if write_count == 1:
            write_entered.set()
            assert release_write.wait(timeout=2.0)
        real_write(data)

    monkeypatch.setattr(manager, "_write_settings_file_atomic", block_first_write)
    errors: list[BaseException] = []

    def persist_selection() -> None:
        try:
            manager.persist_software_coordinate_selection(older)
        except BaseException as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    def save_unrelated_setting() -> None:
        try:
            manager.update_and_save(
                lambda settings: setattr(
                    settings,
                    "design_last_directory",
                    "C:/latest-design",
                )
            )
        except BaseException as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    worker_thread = threading.Thread(target=persist_selection)
    ordinary_thread = threading.Thread(target=save_unrelated_setting)
    worker_thread.start()
    assert write_entered.wait(timeout=1.0)
    ordinary_thread.start()

    safety_release = threading.Timer(0.5, release_write.set)
    safety_release.start()
    newest = manager.set_software_coordinate_selection(FRAME_NEWEST)
    setter_returned_before_release = not release_write.is_set()
    release_write.set()
    safety_release.cancel()
    worker_thread.join(timeout=1.0)
    ordinary_thread.join(timeout=1.0)

    assert setter_returned_before_release
    assert not worker_thread.is_alive()
    assert not ordinary_thread.is_alive()
    assert errors == []
    assert newest.generation == 2
    persisted = _read_json(manager._config_path)
    assert persisted["design_last_directory"] == "C:/latest-design"
    assert persisted["software_coordinates"]["last_selected_frame_id"] == (
        FRAME_NEWEST
    )
    assert _restart_settings(manager).software_coordinates.last_selected_frame_id == (
        FRAME_NEWEST
    )


def test_gui_selection_does_not_wait_for_ordinary_settings_save(
    tmp_path,
    monkeypatch,
) -> None:
    manager = _settings_manager(tmp_path)
    manager.set_software_coordinate_selection(FRAME_ONE)
    write_entered = threading.Event()
    release_write = threading.Event()
    real_write = manager._write_settings_file_atomic

    def blocked_write(data: dict) -> None:
        write_entered.set()
        assert release_write.wait(timeout=2.0)
        real_write(data)

    monkeypatch.setattr(manager, "_write_settings_file_atomic", blocked_write)
    save_thread = threading.Thread(target=manager.save)
    save_thread.start()
    assert write_entered.wait(timeout=1.0)

    safety_release = threading.Timer(0.5, release_write.set)
    safety_release.start()
    newest = manager.set_software_coordinate_selection(FRAME_RESTORED)
    returned_before_release = not release_write.is_set()
    release_write.set()
    safety_release.cancel()
    save_thread.join(timeout=1.0)
    assert returned_before_release
    assert not save_thread.is_alive()

    assert manager.persist_software_coordinate_selection(newest)
    restored = _restart_settings(manager)
    assert restored.software_coordinates.last_selected_frame_id == FRAME_RESTORED
    assert restored.software_coordinates.selection_generation == 2


def test_rapid_selection_persistence_preserves_newer_unrelated_settings(
    qt_app: QApplication,
    tmp_path,
) -> None:
    manager = _settings_manager(tmp_path)
    first_entered = threading.Event()
    release_first = threading.Event()

    def persist(snapshot: SoftwareCoordinateSelectionSnapshot) -> bool:
        if snapshot.frame_id == FRAME_ONE:
            first_entered.set()
            assert release_first.wait(timeout=2.0)
        return manager.persist_software_coordinate_selection(snapshot)

    worker = SoftwareCoordinateSelectionStoreWorker(persist)
    first = manager.set_software_coordinate_selection(FRAME_ONE)
    worker.publish(first)
    assert first_entered.wait(timeout=1.0)

    newest = manager.set_software_coordinate_selection(FRAME_NEWEST)
    manager.update_and_save(
        lambda settings: setattr(settings, "design_last_directory", "C:/new-design")
    )
    worker.publish(newest)
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
    assert selection_document == {
        "version": 1,
        "last_selected_frame_id": FRAME_NEWEST,
        "generation": 2,
    }


def test_persisted_selection_document_overrides_settings_snapshot_on_load(
    tmp_path,
) -> None:
    manager = _settings_manager(tmp_path)
    selection_path = (
        manager._config_dir / SettingsManager.SOFTWARE_COORDINATE_SELECTION_FILENAME
    )
    selection_path.write_text(
        json.dumps(
            {
                "version": 1,
                "last_selected_frame_id": FRAME_RESTORED,
                "generation": 1,
            }
        ),
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
    snapshot = manager.set_software_coordinate_selection(FRAME_NEWEST)
    assert manager.persist_software_coordinate_selection(snapshot)

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
    snapshot = manager.set_software_coordinate_selection(FRAME_ONE)
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
        manager.persist_software_coordinate_selection(snapshot)
    assert list(tmp_path.glob(".*.tmp")) == []

    assert manager.persist_software_coordinate_selection(snapshot)
    restored = json.loads(
        manager._software_coordinate_selection_path().read_text(encoding="utf-8")
    )
    assert restored == {
        "version": 1,
        "last_selected_frame_id": FRAME_ONE,
        "generation": 1,
    }
