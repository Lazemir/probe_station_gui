from __future__ import annotations

from dataclasses import replace
import time
from uuid import uuid4

import pytest
from PySide6.QtWidgets import QApplication

from probe_station_gui.coordinates import (
    AxisReadiness,
    BFrameTransform,
    CoordinateFrameRecord,
    FrameKind,
    ReadinessStatus,
)
from probe_station_gui.coordinates.design_calibration import (
    design_calibration_fingerprints,
    reconcile_design_calibrations,
)
from probe_station_gui.coordinates.persistence import (
    CoordinateFrameDocument,
    FilesystemCoordinateFrameBackend,
    CoordinateFrameStoreWorker,
)
from probe_station_gui.design.frame_registration import DesignFrameMetadata
from probe_station_gui.settings.axis_calibration_config import (
    AxisCalibrationSettings,
    default_axis_calibrations,
)


def _design(*, fingerprints: tuple[tuple[str, str], ...] = ()) -> CoordinateFrameRecord:
    metadata = DesignFrameMetadata(
        source_path="design.gds",
        source_size=1,
        source_mtime_ns=2,
        source_sha256="abc",
        top_cell_name="TOP",
        design_unit_mm=0.001,
        source_design_marks=((1.0, 2.0),),
        source_machine_marks=((3.0, 4.0),),
        calibration_fingerprints=fingerprints,
    )
    return CoordinateFrameRecord(
        frame_id=str(uuid4()),
        kind=FrameKind.DESIGN,
        name="design",
        version=4,
        transform=BFrameTransform.identity(),
        readiness={axis: AxisReadiness(ReadinessStatus.READY) for axis in "XYZAB"},
        metadata=metadata.to_dict(),
    )


def _custom() -> CoordinateFrameRecord:
    return CoordinateFrameRecord(
        frame_id=str(uuid4()),
        kind=FrameKind.CUSTOM,
        name="custom",
        version=2,
        transform=BFrameTransform.identity(),
        readiness={axis: AxisReadiness(ReadinessStatus.READY) for axis in "XYZAB"},
        metadata={},
    )


@pytest.mark.parametrize(
    ("axis", "expected"),
    [
        ("X", {"X", "Y", "B", "Z", "A"}),
        ("Y", {"X", "Y", "B", "Z", "A"}),
        ("B", {"X", "Y", "B", "Z", "A"}),
        ("Z", {"Z", "A"}),
        ("A", {"A"}),
    ],
)
def test_reconcile_invalidates_design_dependencies_and_preserves_marks(
    axis: str,
    expected: set[str],
) -> None:
    settings = default_axis_calibrations()
    previous = design_calibration_fingerprints(settings)
    settings[axis] = AxisCalibrationSettings(
        enabled=True,
        controller_points=[0.0, 1.0, 2.0],
        physical_points=[0.0, 1.2, 2.8],
    )
    record = _design(fingerprints=previous)
    if axis == "X":
        record = replace(
            record,
            readiness={
                **record.readiness,
                "A": AxisReadiness(ReadinessStatus.STALE, "Existing stale reason."),
            },
        )

    reconciled, changed = reconcile_design_calibrations((record,), settings)

    updated = reconciled[0]
    assert changed
    assert updated.version == record.version + 1
    assert {
        name for name, state in updated.readiness.items() if state.status is ReadinessStatus.STALE
    } == expected
    assert updated.metadata["source_design_marks"] == [[1.0, 2.0]]
    assert updated.metadata["source_machine_marks"] == [[3.0, 4.0]]
    assert updated.metadata["calibration_fingerprints"] == [list(item) for item in design_calibration_fingerprints(settings)]
    if axis == "X":
        assert updated.readiness["A"].reason == "Existing stale reason."


def test_legacy_missing_fingerprints_fail_closed_and_same_fingerprints_are_noop() -> None:
    settings = default_axis_calibrations()
    legacy = _design()

    stale, changed = reconcile_design_calibrations((legacy,), settings)
    no_op, changed_again = reconcile_design_calibrations(stale, settings)

    assert changed
    assert all(state.status is ReadinessStatus.STALE for state in stale[0].readiness.values())
    assert not changed_again
    assert no_op == stale


def test_c_only_curve_change_and_custom_records_are_noops() -> None:
    settings = default_axis_calibrations()
    fingerprints = design_calibration_fingerprints(settings)
    design = _design(fingerprints=fingerprints)
    custom = _custom()
    settings["C"] = AxisCalibrationSettings(
        enabled=True,
        controller_points=[0.0, 1.0],
        physical_points=[0.0, 2.0],
    )

    reconciled, changed = reconcile_design_calibrations((design, custom), settings)

    assert not changed
    assert reconciled == (design, custom)


def test_fingerprint_uses_runtime_effective_curve_not_file_or_disabled_values() -> None:
    settings = default_axis_calibrations()
    settings["X"] = AxisCalibrationSettings(
        enabled=True,
        calibration_file="first.csv",
        controller_points=[-0.0, 1.0],
        physical_points=[-0.0, 2.0],
    )
    first = design_calibration_fingerprints(settings)
    settings["X"].calibration_file = "other.csv"
    assert design_calibration_fingerprints(settings) == first
    settings["X"] = AxisCalibrationSettings(
        enabled=False,
        controller_points=[0.0, 1.0],
        physical_points=[0.0, 9.0],
    )
    assert dict(design_calibration_fingerprints(settings))["X"] == "identity"
    settings["X"] = AxisCalibrationSettings(
        enabled=True,
        controller_points=[0.0, 0.0],
        physical_points=[0.0, 1.0],
    )
    assert dict(design_calibration_fingerprints(settings))["X"] == "identity"


def test_malformed_metadata_isolated_preserved_and_stable() -> None:
    settings = default_axis_calibrations()
    valid = _design(fingerprints=design_calibration_fingerprints(settings))
    changed_settings = default_axis_calibrations()
    changed_settings["Z"] = AxisCalibrationSettings(
        enabled=True, controller_points=[0.0, 1.0], physical_points=[0.0, 2.0]
    )
    malformed = replace(
        _design(),
        metadata={"future_key": {"kept": True}},
    )

    reconciled, changed = reconcile_design_calibrations(
        (malformed, valid), changed_settings
    )
    repeated, changed_again = reconcile_design_calibrations(reconciled, changed_settings)

    assert changed
    assert all(not state.available for state in reconciled[0].readiness.values())
    assert reconciled[0].metadata["future_key"] == {"kept": True}
    assert reconciled[0].metadata["calibration_metadata_invalid"] is True
    assert reconciled[1].readiness["Z"].status is ReadinessStatus.STALE
    assert reconciled[1].readiness["A"].status is ReadinessStatus.STALE
    assert not changed_again
    assert repeated == reconciled


def test_overflowing_metadata_isolated_without_blocking_later_design() -> None:
    settings = default_axis_calibrations()
    changed = default_axis_calibrations()
    changed["Z"] = AxisCalibrationSettings(enabled=True, controller_points=[0.0, 1.0], physical_points=[0.0, 2.0])
    malformed = replace(_design(), metadata={"source_size": float("inf"), "future": "kept"})
    valid = _design(fingerprints=design_calibration_fingerprints(settings))
    records, changed_any = reconcile_design_calibrations((malformed, valid), changed)
    assert changed_any
    assert records[0].metadata["future"] == "kept"
    assert all(not state.available for state in records[0].readiness.values())
    assert records[1].readiness["Z"].status is ReadinessStatus.STALE


def test_reconciled_design_round_trips_and_fresh_reconcile_is_noop(tmp_path) -> None:
    previous = default_axis_calibrations()
    current = default_axis_calibrations()
    current["Z"] = AxisCalibrationSettings(
        enabled=True, controller_points=[0.0, 1.0], physical_points=[0.0, 2.0]
    )
    stale, changed = reconcile_design_calibrations(
        (_design(fingerprints=design_calibration_fingerprints(previous)),), current
    )
    path = tmp_path / "coordinate-frames.json"
    backend = FilesystemCoordinateFrameBackend(path)
    backend.save(CoordinateFrameDocument(records=stale))
    loaded = backend.load().records

    fresh, changed_again = reconcile_design_calibrations(loaded, current)

    assert changed
    assert loaded[0].readiness["Z"].status is ReadinessStatus.STALE
    assert not changed_again
    assert fresh == loaded


def test_real_worker_failure_keeps_runtime_stale_and_retry_persists_it(tmp_path) -> None:
    app = QApplication.instance() or QApplication([])
    old_settings = default_axis_calibrations()
    new_settings = default_axis_calibrations()
    new_settings["Z"] = AxisCalibrationSettings(
        enabled=True, controller_points=[0.0, 1.0], physical_points=[0.0, 2.0]
    )
    ready = _design(fingerprints=design_calibration_fingerprints(old_settings))
    stale, changed = reconcile_design_calibrations((ready,), new_settings)

    class FailOnceBackend:
        def __init__(self):
            self.document = CoordinateFrameDocument(records=(ready,))
            self.failed_once = False

        def load(self):
            return self.document

        def save(self, document):
            if not self.failed_once:
                self.failed_once = True
                raise OSError("intentional failure")
            self.document = document

    backend = FailOnceBackend()
    worker = CoordinateFrameStoreWorker(backend_factory=lambda: backend)
    failures, saved = [], []
    worker.failed.connect(failures.append)
    worker.saved.connect(saved.append)
    worker.publish(1, CoordinateFrameDocument(records=stale))
    _wait_for(app, lambda: len(failures) == 1)
    assert changed and stale[0].readiness["Z"].status is ReadinessStatus.STALE
    assert backend.document.records[0].readiness["Z"].status is ReadinessStatus.READY
    fresh, changed_again = reconcile_design_calibrations(backend.load().records, new_settings)
    assert changed_again and fresh[0].readiness["Z"].status is ReadinessStatus.STALE
    worker.publish(2, CoordinateFrameDocument(records=fresh))
    _wait_for(app, lambda: len(saved) == 1)
    assert backend.document.records[0].readiness["Z"].status is ReadinessStatus.STALE
    worker.stop()


def _wait_for(app: QApplication, predicate) -> None:
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    assert predicate()
