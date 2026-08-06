from __future__ import annotations

from dataclasses import replace
import hashlib
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
    CoordinateFrameStoreWorker,
)
from probe_station_gui.coordinates.provenance import (
    RUNTIME_PROVENANCE_REASON,
    RUNTIME_PROVENANCE_STATUS,
    validate_design_frame_provenance,
)
from probe_station_gui.coordinates.transforms import BFrameTransform
from probe_station_gui.design.frame_registration import DesignFrameMetadata


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record(
    path: Path,
    *,
    profile: str = "rig-a",
    frame_id: str = "a915904c-945f-405b-b96f-28fd3e2b0718",
) -> CoordinateFrameRecord:
    stat = path.stat()
    metadata = DesignFrameMetadata(
        source_path=str(path.resolve()),
        source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns,
        source_sha256=_sha256(path),
        top_cell_name="TOP",
        design_unit_mm=0.001,
        machine_profile_id=profile,
    )
    return CoordinateFrameRecord(
        frame_id=frame_id,
        kind=FrameKind.DESIGN,
        name="chip",
        version=0,
        transform=BFrameTransform(
            origin_xy_at_reference_b=(0.0, 0.0),
            reference_b_deg=0.0,
            xy_angle_at_reference_b_deg=0.0,
            b_zero_machine_deg=0.0,
            z_zero_machine_mm=0.0,
            a_zero_machine_mm=0.0,
        ),
        readiness={
            axis: AxisReadiness(ReadinessStatus.READY)
            for axis in ("X", "Y", "Z", "A", "B")
        },
        metadata=metadata.to_dict(),
    )


def test_matching_source_and_machine_profile_are_verified(tmp_path: Path) -> None:
    source = tmp_path / "design.gds"
    source.write_bytes(b"gds-v1")

    result = validate_design_frame_provenance((_record(source),), "rig-a")

    assert result.diagnostics == ()
    assert result.records[0].metadata[RUNTIME_PROVENANCE_STATUS] == "verified"
    assert result.records[0].metadata[RUNTIME_PROVENANCE_REASON] == ""
    persisted = CoordinateFrameDocument(records=result.records).to_dict()
    assert RUNTIME_PROVENANCE_STATUS not in persisted["records"][0]["metadata"]
    assert RUNTIME_PROVENANCE_REASON not in persisted["records"][0]["metadata"]


@pytest.mark.parametrize("failure", ["missing", "changed", "profile"])
def test_source_or_machine_profile_mismatch_remains_blocked(
    tmp_path: Path,
    failure: str,
) -> None:
    source = tmp_path / f"{failure}.gds"
    source.write_bytes(b"gds-v1")
    record = _record(source)
    profile = "rig-a"
    if failure == "missing":
        source.unlink()
    elif failure == "changed":
        source.write_bytes(b"gds-v2-longer")
    else:
        profile = "rig-b"

    result = validate_design_frame_provenance((record,), profile)

    assert len(result.diagnostics) == 1
    assert result.records[0].metadata[RUNTIME_PROVENANCE_STATUS] == "blocked"
    assert result.records[0].metadata[RUNTIME_PROVENANCE_REASON]


def test_store_worker_keeps_delayed_load_unpublished_until_validation_finishes(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    source = tmp_path / "delayed.gds"
    source.write_bytes(b"gds-v1")
    release = threading.Event()
    entered = threading.Event()
    worker_thread_ids: list[int] = []

    class _DelayedBackend:
        def load(self) -> CoordinateFrameDocument:
            worker_thread_ids.append(threading.get_ident())
            entered.set()
            assert release.wait(2.0)
            return CoordinateFrameDocument(records=(_record(source),))

        def save(self, _document: CoordinateFrameDocument) -> None:
            raise AssertionError("save not expected")

    worker = CoordinateFrameStoreWorker(backend_factory=_DelayedBackend)
    results: list[object] = []
    worker.loaded.connect(results.append)

    worker.load(7, machine_profile_id="rig-a")
    assert entered.wait(1.0)
    qt_app.processEvents()
    assert results == []

    release.set()
    deadline = time.monotonic() + 2.0
    while not results and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    worker.stop(timeout_s=1.0)

    assert len(results) == 1
    assert results[0].runtime_records[0].metadata[RUNTIME_PROVENANCE_STATUS] == (
        "verified"
    )
    assert worker_thread_ids != [threading.get_ident()]


def test_store_worker_isolates_malformed_fingerprint_from_valid_sibling(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    bad_source = tmp_path / "bad.gds"
    good_source = tmp_path / "good.gds"
    bad_source.write_bytes(b"bad-gds")
    good_source.write_bytes(b"good-gds")
    bad = _record(bad_source)
    bad_metadata = dict(bad.metadata)
    bad_metadata["calibration_fingerprints"] = [[]]
    bad = replace(bad, metadata=bad_metadata)
    good = _record(
        good_source,
        frame_id="7d8a14a7-1e8d-4b89-8897-57413c09c4b8",
    )

    class _Backend:
        def load(self) -> CoordinateFrameDocument:
            return CoordinateFrameDocument(records=(bad, good))

        def save(self, _document: CoordinateFrameDocument) -> None:
            raise AssertionError("save not expected")

    worker = CoordinateFrameStoreWorker(backend_factory=_Backend)
    loaded: list[object] = []
    failed: list[object] = []
    worker.loaded.connect(loaded.append)
    worker.failed.connect(failed.append)

    worker.load(8, machine_profile_id="rig-a")
    deadline = time.monotonic() + 2.0
    while not loaded and not failed and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    worker.stop(timeout_s=1.0)

    assert failed == []
    assert len(loaded) == 1
    result = loaded[0]
    assert [
        record.metadata[RUNTIME_PROVENANCE_STATUS]
        for record in result.runtime_records
    ] == ["blocked", "verified"]
    assert [diagnostic.frame_id for diagnostic in result.provenance_diagnostics] == [
        bad.frame_id
    ]
