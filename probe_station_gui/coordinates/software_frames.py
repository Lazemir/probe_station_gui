"""Materialize settings-backed custom frames into registry records."""

from __future__ import annotations

from collections.abc import Iterable

from probe_station_gui.settings.software_coordinates import SoftwareCoordinateSettings

from .model import AxisReadiness, CoordinateFrameRecord, FrameKind, ReadinessStatus
from .transforms import BFrameTransform


def materialize_custom_frames(
    existing_records: Iterable[CoordinateFrameRecord],
    settings: SoftwareCoordinateSettings,
) -> tuple[CoordinateFrameRecord, ...]:
    """Prepare one complete registry document from authoritative frame settings.

    Existing Design records remain untouched.  Custom records retain their
    stable IDs and advance their record version only when their materialized
    content changes.  Validation happens before returning any replacement set.
    """

    if settings.materialization_blocked:
        raise ValueError(
            "Software coordinate settings are degraded and cannot update custom frames."
        )

    existing = tuple(existing_records)
    custom_by_id = {frame.frame_id: frame for frame in settings.custom_frames}
    design_ids = {
        record.frame_id for record in existing if record.kind is not FrameKind.CUSTOM
    }
    collisions = design_ids.intersection(custom_by_id)
    if collisions:
        raise ValueError(f"Custom frame ID collides with a durable frame: {sorted(collisions)!r}.")
    existing_custom = {
        record.frame_id: record
        for record in existing
        if record.kind is FrameKind.CUSTOM
    }
    prepared_custom = {
        frame_id: _materialize_one(frame, existing_custom.get(frame_id))
        for frame_id, frame in custom_by_id.items()
    }
    retained = [record for record in existing if record.kind is not FrameKind.CUSTOM]
    for frame in settings.custom_frames:
        retained.append(prepared_custom[frame.frame_id])
    return tuple(retained)


def _materialize_one(settings_frame, existing: CoordinateFrameRecord | None) -> CoordinateFrameRecord:
    if settings_frame.a_zero_mm is not None and settings_frame.z_zero_mm is None:
        raise ValueError("Frame A origin requires a Z origin.")
    transform = BFrameTransform(
        origin_xy_at_reference_b=(settings_frame.origin_x_mm, settings_frame.origin_y_mm),
        reference_b_deg=settings_frame.reference_b_deg,
        xy_angle_at_reference_b_deg=settings_frame.xy_angle_deg,
        b_zero_machine_deg=settings_frame.b_zero_deg,
        z_zero_machine_mm=settings_frame.z_zero_mm,
        a_zero_machine_mm=settings_frame.a_zero_mm,
    )
    readiness = {
        "X": AxisReadiness(ReadinessStatus.READY),
        "Y": AxisReadiness(ReadinessStatus.READY),
        "B": AxisReadiness(ReadinessStatus.READY),
        "Z": AxisReadiness(
            ReadinessStatus.READY if settings_frame.z_zero_mm is not None else ReadinessStatus.MISSING,
            "" if settings_frame.z_zero_mm is not None else "Set Z zero.",
        ),
        "A": AxisReadiness(
            ReadinessStatus.READY if settings_frame.a_zero_mm is not None else ReadinessStatus.MISSING,
            "" if settings_frame.a_zero_mm is not None else "Set A zero.",
        ),
    }
    candidate = CoordinateFrameRecord(
        frame_id=settings_frame.frame_id,
        kind=FrameKind.CUSTOM,
        name=settings_frame.name,
        version=0 if existing is None else existing.version,
        transform=transform,
        readiness=readiness,
        metadata={"settings_backed": True},
    )
    if existing is None:
        return candidate
    return candidate if candidate == existing else CoordinateFrameRecord(
        frame_id=candidate.frame_id,
        kind=candidate.kind,
        name=candidate.name,
        version=existing.version + 1,
        transform=candidate.transform,
        readiness=candidate.readiness,
        metadata=candidate.metadata,
    )


__all__ = ["materialize_custom_frames"]
