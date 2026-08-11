from __future__ import annotations

import json
import logging
import os
import threading
import time

import pytest

from probe_station_gui.settings.document import Settings
from probe_station_gui.settings.selection_persistence import (
    SOFTWARE_COORDINATE_SELECTION_FILENAME,
    SoftwareCoordinateSelectionPersistence,
    SoftwareCoordinateSelectionSnapshot,
)


FRAME_ONE = "11111111-1111-4111-8111-111111111111"
FRAME_TWO = "22222222-2222-4222-8222-222222222222"
FRAME_THREE = "33333333-3333-4333-8333-333333333333"


def _owner(tmp_path) -> SoftwareCoordinateSelectionPersistence:
    return SoftwareCoordinateSelectionPersistence(
        config_dir=tmp_path,
        settings_path=tmp_path / "settings.json",
        state_lock=threading.RLock(),
        logger=logging.getLogger(__name__),
    )


def _read_json(path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_selection_snapshot_is_immutable_and_invalid_ids_are_rejected(tmp_path) -> None:
    owner = _owner(tmp_path)
    settings = Settings()

    snapshot = owner.select(settings, FRAME_ONE)

    assert snapshot == SoftwareCoordinateSelectionSnapshot(FRAME_ONE, 1)
    assert settings.software_coordinates.last_selected_frame_id == FRAME_ONE
    assert settings.software_coordinates.selection_generation == 1
    assert not (tmp_path / "settings.json").exists()
    with pytest.raises((AttributeError, TypeError)):
        snapshot.generation = 2  # type: ignore[misc]
    with pytest.raises(ValueError, match="Machine or a coordinate-frame UUID"):
        owner.select(settings, "display name")


def test_stale_selection_write_is_rejected_before_any_file_write(tmp_path) -> None:
    owner = _owner(tmp_path)
    settings = Settings()
    stale = owner.select(settings, FRAME_ONE)
    current = owner.select(settings, FRAME_TWO)

    assert owner.persist(stale, settings.to_dict) is False
    assert not (tmp_path / "settings.json").exists()
    assert owner.persist(current, settings.to_dict) is True
    assert _read_json(tmp_path / "settings.json")["software_coordinates"] == {
        **settings.software_coordinates.to_dict(),
    }


def test_main_and_sidecar_restart_merge_uses_higher_generation_and_main_tie(
    tmp_path,
) -> None:
    owner = _owner(tmp_path)
    main = Settings()
    main.software_coordinates.last_selected_frame_id = FRAME_ONE
    main.software_coordinates.selection_generation = 4
    owner.save_main(main.to_dict)
    sidecar_path = tmp_path / SOFTWARE_COORDINATE_SELECTION_FILENAME
    sidecar_path.write_text(
        json.dumps(
            {
                "version": 1,
                "last_selected_frame_id": FRAME_TWO,
                "generation": 5,
            }
        ),
        encoding="utf-8",
    )

    restored = Settings()
    restored.software_coordinates.last_selected_frame_id = FRAME_ONE
    restored.software_coordinates.selection_generation = 4
    owner.restore(restored)
    assert restored.software_coordinates.last_selected_frame_id == FRAME_TWO
    assert restored.software_coordinates.selection_generation == 5

    tied = Settings()
    tied.software_coordinates.last_selected_frame_id = FRAME_THREE
    tied.software_coordinates.selection_generation = 5
    owner.restore(tied)
    assert tied.software_coordinates.last_selected_frame_id == FRAME_THREE
    assert tied.software_coordinates.selection_generation == 5


@pytest.mark.parametrize(
    "payload",
    [
        "{broken",
        json.dumps(["not", "an", "object"]),
        json.dumps({"last_selected_frame_id": FRAME_TWO}),
        json.dumps(
            {
                "version": 1,
                "last_selected_frame_id": FRAME_TWO,
                "generation": "legacy",
            }
        ),
    ],
)
def test_malformed_corrupt_and_legacy_sidecars_fall_back_with_warning(
    tmp_path,
    caplog,
    payload: str,
) -> None:
    owner = _owner(tmp_path)
    sidecar_path = tmp_path / SOFTWARE_COORDINATE_SELECTION_FILENAME
    sidecar_path.write_text(payload, encoding="utf-8")
    settings = Settings()
    settings.software_coordinates.last_selected_frame_id = FRAME_ONE
    settings.software_coordinates.selection_generation = 7

    with caplog.at_level(logging.WARNING):
        owner.restore(settings)

    assert settings.software_coordinates.last_selected_frame_id == FRAME_ONE
    assert settings.software_coordinates.selection_generation == 7
    assert "Failed to load software coordinate selection" in caplog.text


def test_utf8_bom_sidecar_is_loaded_and_writes_are_plain_utf8(tmp_path) -> None:
    owner = _owner(tmp_path)
    sidecar_path = tmp_path / SOFTWARE_COORDINATE_SELECTION_FILENAME
    sidecar_path.write_bytes(
        b"\xef\xbb\xbf"
        + json.dumps(
            {
                "version": 1,
                "last_selected_frame_id": FRAME_TWO,
                "generation": 3,
            }
        ).encode("utf-8")
    )
    settings = Settings()

    owner.restore(settings)
    snapshot = owner.select(settings, FRAME_THREE)
    assert owner.persist(snapshot, settings.to_dict)

    assert not sidecar_path.read_bytes().startswith(b"\xef\xbb\xbf")
    assert _read_json(sidecar_path)["last_selected_frame_id"] == FRAME_THREE


def test_future_sidecar_is_preserved_and_blocks_atomic_rewrite(tmp_path) -> None:
    owner = _owner(tmp_path)
    future = {
        "version": 2,
        "last_selected_frame_id": FRAME_TWO,
        "generation": 99,
        "future_state": {"scope": "operator"},
    }
    sidecar_path = tmp_path / SOFTWARE_COORDINATE_SELECTION_FILENAME
    sidecar_path.write_text(json.dumps(future), encoding="utf-8")
    settings = Settings()
    snapshot = owner.select(settings, FRAME_ONE)

    with pytest.raises(ValueError, match="unsupported.*version"):
        owner.persist(snapshot, settings.to_dict)

    assert _read_json(tmp_path / "settings.json")["software_coordinates"] == (
        settings.software_coordinates.to_dict()
    )
    assert _read_json(sidecar_path) == future


def test_atomic_replace_failure_cleans_temp_file_and_retry_succeeds(
    tmp_path,
    monkeypatch,
) -> None:
    owner = _owner(tmp_path)
    settings = Settings()
    snapshot = owner.select(settings, FRAME_ONE)
    real_replace = os.replace
    attempts = 0

    def flaky_replace(source, destination) -> None:
        nonlocal attempts
        attempts += 1
        if PathLike(destination).name == SOFTWARE_COORDINATE_SELECTION_FILENAME and attempts == 2:
            raise OSError("replace interrupted")
        real_replace(source, destination)

    class PathLike:
        def __init__(self, value) -> None:
            self.name = os.path.basename(os.fspath(value))

    monkeypatch.setattr(
        "probe_station_gui.settings.selection_persistence.os.replace",
        flaky_replace,
    )

    with pytest.raises(OSError, match="replace interrupted"):
        owner.persist(snapshot, settings.to_dict)
    assert _read_json(tmp_path / "settings.json")["software_coordinates"] == (
        settings.software_coordinates.to_dict()
    )
    assert list(tmp_path.glob(".*.tmp")) == []
    assert owner.persist(snapshot, settings.to_dict)


def test_shared_lock_serializes_main_and_selection_writes_without_blocking_select(
    tmp_path,
    monkeypatch,
) -> None:
    owner = _owner(tmp_path)
    settings = Settings()
    first = owner.select(settings, FRAME_ONE)
    entered = threading.Event()
    release = threading.Event()
    real_replace = os.replace
    main_path = tmp_path / "settings.json"

    def blocked_replace(source, destination) -> None:
        if destination == main_path and not entered.is_set():
            entered.set()
            assert release.wait(2.0)
        real_replace(source, destination)

    monkeypatch.setattr(
        "probe_station_gui.settings.selection_persistence.os.replace",
        blocked_replace,
    )
    errors: list[BaseException] = []
    persist_thread = threading.Thread(
        target=lambda: owner.persist(first, settings.to_dict),
    )
    persist_thread.start()
    assert entered.wait(1.0)

    started = time.perf_counter()
    newest = owner.select(settings, FRAME_TWO)
    elapsed = time.perf_counter() - started
    ordinary_done = threading.Event()

    def save_unrelated() -> None:
        try:
            settings.design_last_directory = "C:/latest"
            owner.merge(settings)
            owner.save_main(settings.to_dict)
        except BaseException as exc:  # pragma: no cover - reports thread details
            errors.append(exc)
        finally:
            ordinary_done.set()

    ordinary_thread = threading.Thread(target=save_unrelated)
    ordinary_thread.start()
    assert not ordinary_done.wait(0.05)
    release.set()
    persist_thread.join(1.0)
    ordinary_thread.join(1.0)

    assert elapsed < 0.1
    assert newest.generation == 2
    assert errors == []
    assert _read_json(main_path)["software_coordinates"][
        "last_selected_frame_id"
    ] == FRAME_TWO
