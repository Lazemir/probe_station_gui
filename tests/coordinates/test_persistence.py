from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from probe_station_gui.coordinates.model import (
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    ReadinessStatus,
)
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameDocument,
    CoordinateFrameLoadResult,
    CoordinateFrameStoreSuccess,
    CoordinateFrameStoreWorker,
    FilesystemCoordinateFrameBackend,
)
from probe_station_gui.coordinates.transforms import BFrameTransform
from probe_station_gui.settings.manager import SettingsManager


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


def _record(
    *,
    name: str = "valid",
    version: int = 0,
    frame_id: str = "ba64e533-7143-49ef-b41a-290f16d7ca1b",
) -> CoordinateFrameRecord:
    return CoordinateFrameRecord(
        frame_id=frame_id,
        kind=FrameKind.DESIGN,
        name=name,
        version=version,
        transform=BFrameTransform(
            origin_xy_at_reference_b=(1.25, -2.5),
            reference_b_deg=3.5,
            xy_angle_at_reference_b_deg=-4.5,
            b_zero_machine_deg=5.5,
            z_zero_machine_mm=6.5,
            a_zero_machine_mm=7.5,
        ),
        readiness={
            axis: AxisReadiness(ReadinessStatus.READY, f"{axis} ready")
            for axis in ("X", "Y", "Z", "A", "B")
        },
        metadata={"source": "calibration"},
    )


def test_document_round_trip_uses_explicit_json_values() -> None:
    document = CoordinateFrameDocument(records=(_record(version=4),))

    payload = document.to_dict()
    restored = CoordinateFrameDocument.from_dict(payload)

    assert restored == document
    assert payload["version"] == 1
    assert payload["records"][0]["kind"] == "design"
    assert payload["records"][0]["readiness"]["X"] == {
        "status": "ready",
        "reason": "X ready",
    }
    assert payload["records"][0]["transform"] == {
        "origin_xy_at_reference_b": [1.25, -2.5],
        "reference_b_deg": 3.5,
        "xy_angle_at_reference_b_deg": -4.5,
        "b_zero_machine_deg": 5.5,
        "z_zero_machine_mm": 6.5,
        "a_zero_machine_mm": 7.5,
    }


def test_document_load_isolates_one_invalid_record(tmp_path: Path) -> None:
    path = tmp_path / "coordinate-frames.json"
    valid = _record()
    payload = CoordinateFrameDocument(records=(valid,)).to_dict()
    payload["records"].append({"frame_id": "broken"})
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = FilesystemCoordinateFrameBackend(path).load()

    assert [record.name for record in loaded.records] == ["valid"]
    assert len(loaded.diagnostics) == 1
    assert loaded.diagnostics[0].index == 1


def test_rejected_raw_record_survives_valid_record_mutation_and_save() -> None:
    payload = CoordinateFrameDocument(records=(_record(),)).to_dict()
    rejected = {"frame_id": "broken", "future_payload": {"keep": [1, 2, 3]}}
    payload["records"].append(deepcopy(rejected))

    loaded = CoordinateFrameDocument.from_dict(payload)
    renamed = loaded.records[0].with_name("renamed")
    saved = loaded.with_records((renamed,)).to_dict()

    assert saved["records"][0]["name"] == "renamed"
    assert saved["records"][1] == rejected


@pytest.mark.parametrize("future_field_location", ["record", "transform"])
def test_future_record_survives_unrelated_sibling_mutation_and_save(
    future_field_location: str,
) -> None:
    future = CoordinateFrameDocument(records=(_record(name="future"),)).to_dict()[
        "records"
    ][0]
    if future_field_location == "record":
        future["future_record_field"] = {"keep": [1, 2, 3]}
    else:
        future["transform"]["future_transform_field"] = {
            "keep": [4, 5, 6]
        }
    sibling = _record(
        name="sibling",
        frame_id="7ba7cc11-0940-4d4d-ad24-e6c8e6f1d55c",
    )
    serialized_sibling = CoordinateFrameDocument(records=(sibling,)).to_dict()[
        "records"
    ][0]
    payload = {
        "version": 1,
        "records": [deepcopy(future), serialized_sibling],
    }

    loaded = CoordinateFrameDocument.from_dict(payload)
    renamed = loaded.records[0].with_name("renamed sibling")
    saved = loaded.with_records((renamed,)).to_dict()

    assert [diagnostic.index for diagnostic in loaded.diagnostics] == [0]
    assert [record.name for record in loaded.records] == ["sibling"]
    assert saved["records"][0] == future
    assert saved["records"][1]["name"] == "renamed sibling"


def test_duplicate_valid_uuid_is_diagnosed_deterministically_and_preserved() -> None:
    payload = CoordinateFrameDocument(records=(_record(name="first"),)).to_dict()
    duplicate = deepcopy(payload["records"][0])
    duplicate["name"] = "duplicate"
    payload["records"].append(deepcopy(duplicate))

    loaded = CoordinateFrameDocument.from_dict(payload)

    assert [record.name for record in loaded.records] == ["first"]
    assert [diagnostic.index for diagnostic in loaded.diagnostics] == [1]
    assert "duplicate" in loaded.diagnostics[0].message.lower()
    assert loaded.to_dict()["records"][1] == duplicate


@pytest.mark.parametrize(
    ("axis", "transform_field", "message"),
    [
        ("Z", "z_zero_machine_mm", "READY Z requires"),
        ("A", "a_zero_machine_mm", "READY A requires"),
    ],
)
def test_semantically_corrupt_ready_origin_is_rejected_and_preserved(
    axis: str,
    transform_field: str,
    message: str,
) -> None:
    payload = CoordinateFrameDocument(records=(_record(),)).to_dict()
    corrupt = deepcopy(payload["records"][0])
    corrupt["transform"][transform_field] = None
    payload["records"] = [corrupt]

    loaded = CoordinateFrameDocument.from_dict(payload)

    assert loaded.records == ()
    assert message in loaded.diagnostics[0].message
    assert loaded.to_dict()["records"] == [corrupt]


def test_ready_a_without_ready_z_is_rejected_and_preserved() -> None:
    payload = CoordinateFrameDocument(records=(_record(),)).to_dict()
    corrupt = deepcopy(payload["records"][0])
    corrupt["readiness"]["Z"] = {
        "status": "missing",
        "reason": "Set Z first.",
    }
    payload["records"] = [corrupt]

    loaded = CoordinateFrameDocument.from_dict(payload)

    assert loaded.records == ()
    assert "READY A requires READY Z" in loaded.diagnostics[0].message
    assert loaded.to_dict()["records"] == [corrupt]


def test_document_load_diagnoses_invalid_json_scalar_types_per_record(
    tmp_path: Path,
) -> None:
    path = tmp_path / "coordinate-frames.json"
    payload = CoordinateFrameDocument(records=(_record(),)).to_dict()
    valid_record = payload["records"][0]

    invalid_records = []
    for field, value in (
        ("frame_id", 123),
        ("kind", 123),
        ("name", 123),
        ("version", True),
    ):
        invalid = deepcopy(valid_record)
        invalid[field] = value
        invalid_records.append(invalid)

    invalid_status = deepcopy(valid_record)
    invalid_status["readiness"]["X"]["status"] = 1
    invalid_records.append(invalid_status)

    invalid_reason = deepcopy(valid_record)
    invalid_reason["readiness"]["X"]["reason"] = 1
    invalid_records.append(invalid_reason)

    boolean_coordinate = deepcopy(valid_record)
    boolean_coordinate["transform"]["origin_xy_at_reference_b"][0] = True
    invalid_records.append(boolean_coordinate)

    numeric_string_coordinate = deepcopy(valid_record)
    numeric_string_coordinate["transform"]["reference_b_deg"] = "3.5"
    invalid_records.append(numeric_string_coordinate)

    payload["records"].extend(invalid_records)
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = FilesystemCoordinateFrameBackend(path).load()

    assert [record.name for record in loaded.records] == ["valid"]
    assert [diagnostic.index for diagnostic in loaded.diagnostics] == list(
        range(1, 9)
    )


@pytest.mark.parametrize("payload", [[], {"version": 0, "records": []}, {"version": 99, "records": []}])
def test_document_rejects_invalid_root_or_schema_version(payload: object) -> None:
    with pytest.raises(ValueError, match="version|object"):
        CoordinateFrameDocument.from_dict(payload)


def test_backend_loads_missing_file_as_empty_document(tmp_path: Path) -> None:
    loaded = FilesystemCoordinateFrameBackend(
        tmp_path / "coordinate-frames.json"
    ).load()

    assert loaded == CoordinateFrameDocument()


def test_failed_atomic_replace_preserves_previous_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "coordinate-frames.json"
    backend = FilesystemCoordinateFrameBackend(path)
    first = CoordinateFrameDocument(records=(_record(),))
    backend.save(first)
    previous = path.read_bytes()

    def fail_replace(_source, _target) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(
        "probe_station_gui.coordinates.persistence.os.replace",
        fail_replace,
    )

    with pytest.raises(OSError, match="replace failed"):
        backend.save(CoordinateFrameDocument())

    assert path.read_bytes() == previous
    assert list(tmp_path.glob("*.tmp")) == []


def test_invalid_document_version_is_not_saved_over_existing_document(
    tmp_path: Path,
) -> None:
    path = tmp_path / "coordinate-frames.json"
    backend = FilesystemCoordinateFrameBackend(path)
    backend.save(CoordinateFrameDocument(records=(_record(),)))
    previous = path.read_bytes()

    with pytest.raises(ValueError, match="version"):
        backend.save(CoordinateFrameDocument(version=99))

    assert path.read_bytes() == previous


def test_backend_persists_design_records_only(tmp_path: Path) -> None:
    design = _record()
    custom = CoordinateFrameRecord(
        frame_id="bd5dbba5-4a51-4314-bb18-c6cdca3e1e0f",
        kind=FrameKind.CUSTOM,
        name="settings-owned",
        version=0,
        transform=BFrameTransform.identity(),
        readiness=design.readiness,
        metadata={},
    )
    path = tmp_path / "coordinate-frames.json"

    FilesystemCoordinateFrameBackend(path).save(
        CoordinateFrameDocument(records=(design, custom))
    )

    assert [record.kind for record in FilesystemCoordinateFrameBackend(path).load().records] == [
        FrameKind.DESIGN
    ]


def test_settings_manager_exposes_coordinate_frames_path(tmp_path: Path) -> None:
    manager = SettingsManager.__new__(SettingsManager)
    manager._config_dir = tmp_path

    assert manager.coordinate_frames_path() == tmp_path / "coordinate-frames.json"
    assert SettingsManager.COORDINATE_FRAMES_FILENAME == "coordinate-frames.json"


def test_worker_loads_and_saves_off_creator_thread(
    qt_app: QApplication,
) -> None:
    creator_thread = threading.get_ident()
    backend = _RecordingBackend(CoordinateFrameDocument(records=(_record(),)))
    worker = CoordinateFrameStoreWorker(backend_factory=lambda: backend)
    loaded: list[CoordinateFrameLoadResult] = []
    saved: list[CoordinateFrameStoreSuccess] = []
    worker.loaded.connect(loaded.append)
    worker.saved.connect(saved.append)

    worker.load(1)
    _wait_until(qt_app, lambda: len(loaded) == 1)
    worker.publish(2, CoordinateFrameDocument())
    _wait_until(qt_app, lambda: len(saved) == 1)

    assert loaded == [CoordinateFrameLoadResult(1, backend.loaded_document)]
    assert saved == [CoordinateFrameStoreSuccess(2, "save")]
    assert backend.thread_ids
    assert set(backend.thread_ids) == {backend.thread_ids[0]}
    assert backend.thread_ids[0] != creator_thread
    worker.stop()


def test_worker_coalesces_pending_publications_to_newest(
    qt_app: QApplication,
) -> None:
    first = CoordinateFrameDocument(records=(_record(name="first"),))
    second = CoordinateFrameDocument(records=(_record(name="second"),))
    newest = CoordinateFrameDocument(records=(_record(name="newest"),))
    backend = _BlockingBackend()
    worker = CoordinateFrameStoreWorker(backend_factory=lambda: backend)
    saved: list[CoordinateFrameStoreSuccess] = []
    worker.saved.connect(saved.append)

    worker.publish(1, first)
    assert backend.started.wait(timeout=1.0)
    worker.publish(2, second)
    worker.publish(3, newest)
    backend.release.set()
    _wait_until(qt_app, lambda: worker.is_idle)
    _wait_until(qt_app, lambda: len(saved) == 2)

    assert backend.saved_documents == [first, newest]
    assert saved == [
        CoordinateFrameStoreSuccess(1, "save"),
        CoordinateFrameStoreSuccess(3, "save"),
    ]
    worker.stop()


def test_timed_out_stop_drains_latest_publication_on_nondaemon_thread() -> None:
    first = CoordinateFrameDocument(records=(_record(name="first"),))
    newest = CoordinateFrameDocument(records=(_record(name="newest"),))
    backend = _BlockingBackend()
    worker = CoordinateFrameStoreWorker(backend_factory=lambda: backend)
    worker.publish(1, first)
    assert backend.started.wait(timeout=1.0)
    worker.publish(2, newest)

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
    assert backend.saved_documents == [first, newest]


class _RecordingBackend:
    def __init__(self, loaded_document: CoordinateFrameDocument) -> None:
        self.loaded_document = loaded_document
        self.thread_ids: list[int] = []
        self.saved_documents: list[CoordinateFrameDocument] = []

    def load(self) -> CoordinateFrameDocument:
        self.thread_ids.append(threading.get_ident())
        return self.loaded_document

    def save(self, document: CoordinateFrameDocument) -> None:
        self.thread_ids.append(threading.get_ident())
        self.saved_documents.append(document)


class _BlockingBackend(_RecordingBackend):
    def __init__(self) -> None:
        super().__init__(CoordinateFrameDocument())
        self.started = threading.Event()
        self.release = threading.Event()

    def save(self, document: CoordinateFrameDocument) -> None:
        self.thread_ids.append(threading.get_ident())
        self.saved_documents.append(document)
        if len(self.saved_documents) == 1:
            self.started.set()
            assert self.release.wait(timeout=2.0)
