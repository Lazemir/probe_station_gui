"""Durable Design-frame calibration fingerprints and readiness reconciliation."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import replace

from probe_station_gui.coordinates.model import (
    AxisReadiness,
    CoordinateFrameRecord,
    FrameKind,
    ReadinessStatus,
    VISIBLE_STAGE_AXES,
)
from probe_station_gui.design.frame_registration import DesignFrameMetadata
from probe_station_gui.stage.axis_mapping import curve_from_settings


def design_calibration_fingerprints(
    calibrations: Mapping[str, object],
) -> tuple[tuple[str, str], ...]:
    """Return canonical visible-axis controller-to-physical curve fingerprints."""

    return tuple((axis, _fingerprint(calibrations.get(axis))) for axis in VISIBLE_STAGE_AXES)


def reconcile_design_calibrations(
    records: Iterable[CoordinateFrameRecord],
    calibrations: Mapping[str, object],
) -> tuple[tuple[CoordinateFrameRecord, ...], bool]:
    """Prepare stale Design records when durable curve fingerprints differ.

    Custom records deliberately remain unchanged: their settings are explicit
    physical-Machine geometry rather than derived Design registration.
    """

    current = design_calibration_fingerprints(calibrations)
    reconciled: list[CoordinateFrameRecord] = []
    changed_any = False
    for record in records:
        if record.kind is not FrameKind.DESIGN:
            reconciled.append(record)
            continue
        updated = _reconcile_design_record(record, current)
        reconciled.append(updated)
        changed_any = changed_any or updated != record
    return tuple(reconciled), changed_any


def _reconcile_design_record(
    record: CoordinateFrameRecord,
    current: tuple[tuple[str, str], ...],
) -> CoordinateFrameRecord:
    if not isinstance(record.metadata, Mapping):
        return _fail_closed_malformed_metadata(record, current)
    raw_metadata = dict(record.metadata)
    try:
        metadata = DesignFrameMetadata.from_mapping(raw_metadata)
    except (KeyError, TypeError, ValueError, IndexError, OverflowError):
        return _fail_closed_malformed_metadata(record, current)
    stored = dict(metadata.calibration_fingerprints)
    current_map = dict(current)
    changed_axes = {
        axis
        for axis in VISIBLE_STAGE_AXES
        if stored.get(axis) != current_map[axis]
    }
    if not changed_axes:
        return record
    readiness = dict(record.readiness)
    for axis in _dependent_axes(changed_axes):
        if readiness[axis].available:
            readiness[axis] = AxisReadiness(
                ReadinessStatus.STALE,
                "Axis calibration changed.",
            )
    updated_metadata = dict(raw_metadata)
    updated_metadata["calibration_fingerprints"] = [list(item) for item in current]
    return replace(
        record,
        version=record.version + 1,
        readiness=readiness,
        metadata=updated_metadata,
    )


def _fail_closed_malformed_metadata(
    record: CoordinateFrameRecord,
    current: tuple[tuple[str, str], ...],
) -> CoordinateFrameRecord:
    raw_metadata = dict(record.metadata) if isinstance(record.metadata, Mapping) else {}
    if raw_metadata.get("calibration_metadata_invalid") is True and (
        raw_metadata.get("calibration_fingerprints")
        == [list(item) for item in current]
    ):
        return record
    readiness = dict(record.readiness)
    for axis in VISIBLE_STAGE_AXES:
        if readiness[axis].available:
            readiness[axis] = AxisReadiness(
                ReadinessStatus.STALE,
                "Design calibration metadata is invalid.",
            )
    raw_metadata["calibration_fingerprints"] = [list(item) for item in current]
    raw_metadata["calibration_metadata_invalid"] = True
    return replace(
        record,
        version=record.version + 1,
        readiness=readiness,
        metadata=raw_metadata,
    )


def _dependent_axes(changed_axes: set[str]) -> set[str]:
    if changed_axes.intersection({"X", "Y", "B"}):
        return {"X", "Y", "B", "Z", "A"}
    if "Z" in changed_axes:
        return {"Z", "A"}
    if "A" in changed_axes:
        return {"A"}
    return set()


def _fingerprint(value: object) -> str:
    curve = curve_from_settings(value) if _is_curve_settings(value) else None
    if curve is None:
        return "identity"
    payload = (
        tuple(_normalized(value) for value in curve.controller),
        tuple(_normalized(value) for value in curve.physical),
    )
    return json.dumps(payload, separators=(",", ":"), allow_nan=False)


def _is_curve_settings(value: object) -> bool:
    return all(hasattr(value, name) for name in ("enabled", "controller_points", "physical_points"))


def _normalized(value: float) -> float:
    return 0.0 if float(value) == 0.0 else float(value)


__all__ = ["design_calibration_fingerprints", "reconcile_design_calibrations"]
