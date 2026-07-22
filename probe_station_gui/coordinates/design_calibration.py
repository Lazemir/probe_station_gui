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


def design_calibration_fingerprints(
    calibrations: Mapping[str, object],
) -> tuple[tuple[str, str], ...]:
    """Return canonical visible-axis controller-to-physical curve fingerprints."""

    return tuple(
        (axis, _fingerprint(calibrations.get(axis)))
        for axis in VISIBLE_STAGE_AXES
    )


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
    metadata = DesignFrameMetadata.from_mapping(record.metadata)
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
    updated_metadata = replace(metadata, calibration_fingerprints=current)
    return replace(
        record,
        version=record.version + 1,
        readiness=readiness,
        metadata=updated_metadata.to_dict(),
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
    serializer = getattr(value, "to_dict", None)
    raw = serializer() if callable(serializer) else value
    try:
        return json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return repr(raw)


__all__ = ["design_calibration_fingerprints", "reconcile_design_calibrations"]
