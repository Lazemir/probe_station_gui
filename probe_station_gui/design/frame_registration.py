"""Persistent Design-frame registration and legacy migration helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import math
from pathlib import Path
from typing import Iterable, Mapping
import uuid

from probe_station_gui.coordinates.model import (
    AxisReadiness,
    CoordinateFrameRecord,
    ReadinessStatus,
    VISIBLE_STAGE_AXES,
)
from probe_station_gui.coordinates.registry import invalidate_axes
from probe_station_gui.coordinates.transforms import BFrameTransform
from probe_station_gui.design.model import DesignDocument, Point2D
from probe_station_gui.design.rigid_registration import fit_rigid_registration


DesignFrameDraft = CoordinateFrameRecord


@dataclass(frozen=True)
class DesignFrameMetadata:
    source_path: str
    source_size: int
    source_mtime_ns: int
    source_sha256: str
    top_cell_name: str
    design_unit_mm: float
    source_design_marks: tuple[Point2D, ...] = ()
    source_machine_marks: tuple[Point2D, ...] = ()
    check_design_marks: tuple[Point2D, ...] = ()
    check_machine_marks: tuple[Point2D, ...] = ()
    scale_ratio: float | None = None
    rms_residual_mm: float | None = None
    max_residual_mm: float | None = None
    machine_profile_id: str = "default"
    calibration_fingerprints: tuple[tuple[str, str], ...] = ()

    def to_dict(self) -> dict[str, object]:
        """Return JSON-safe values with the stable durable field set."""

        value = asdict(self)
        for field_name in (
            "source_design_marks",
            "source_machine_marks",
            "check_design_marks",
            "check_machine_marks",
            "calibration_fingerprints",
        ):
            value[field_name] = [list(item) for item in value[field_name]]
        return value

    @classmethod
    def from_mapping(cls, value: Mapping[str, object]) -> DesignFrameMetadata:
        return cls(
            source_path=str(value["source_path"]),
            source_size=int(value["source_size"]),
            source_mtime_ns=int(value["source_mtime_ns"]),
            source_sha256=str(value["source_sha256"]),
            top_cell_name=str(value["top_cell_name"]),
            design_unit_mm=float(value["design_unit_mm"]),
            source_design_marks=_points(value.get("source_design_marks", ())),
            source_machine_marks=_points(value.get("source_machine_marks", ())),
            check_design_marks=_points(value.get("check_design_marks", ())),
            check_machine_marks=_points(value.get("check_machine_marks", ())),
            scale_ratio=_optional_float(value.get("scale_ratio")),
            rms_residual_mm=_optional_float(value.get("rms_residual_mm")),
            max_residual_mm=_optional_float(value.get("max_residual_mm")),
            machine_profile_id=str(value.get("machine_profile_id", "default")),
            calibration_fingerprints=tuple(
                (str(item[0]), str(item[1]))
                for item in value.get("calibration_fingerprints", ())
            ),
        )

    @classmethod
    def from_document(cls, document: DesignDocument) -> DesignFrameMetadata:
        path = Path(document.path).expanduser().resolve()
        stat = path.stat()
        return cls(
            source_path=str(path),
            source_size=int(stat.st_size),
            source_mtime_ns=int(stat.st_mtime_ns),
            source_sha256=_sha256(path),
            top_cell_name=document.top_cell_name,
            design_unit_mm=float(document.dbu) * 1e3,
        )


def new_design_frame_draft(
    design_document: DesignDocument,
    *,
    existing_names: Iterable[str],
    metadata: DesignFrameMetadata | None = None,
) -> DesignFrameDraft:
    """Create an independent missing-reference frame for one loaded design."""

    frame_metadata = metadata or DesignFrameMetadata.from_document(design_document)
    frame_metadata = replace(
        frame_metadata,
        source_design_marks=(),
        source_machine_marks=(),
        check_design_marks=(),
        check_machine_marks=(),
        scale_ratio=None,
        rms_residual_mm=None,
        max_residual_mm=None,
    )
    name = _next_frame_name(design_document.path.stem, existing_names)
    return CoordinateFrameRecord.create_design(
        frame_id=str(uuid.uuid4()),
        name=name,
        transform=BFrameTransform.identity(),
        readiness={
            axis: AxisReadiness(
                ReadinessStatus.MISSING,
                f"Design {axis} reference is not registered.",
            )
            for axis in VISIBLE_STAGE_AXES
        },
        metadata=frame_metadata.to_dict(),
    )


def commit_xyb_registration(
    draft: DesignFrameDraft,
    *,
    design_points: Iterable[Point2D],
    physical_machine_points: Iterable[Point2D],
    physical_b_deg: float,
    pivot_machine_xy: Point2D,
    check_design_points: Iterable[Point2D] = (),
    check_machine_points: Iterable[Point2D] = (),
) -> CoordinateFrameRecord:
    """Fit and record X/Y/B references without issuing any hardware action."""

    metadata = DesignFrameMetadata.from_mapping(draft.metadata)
    source_design = _points(design_points)
    source_machine = _points(physical_machine_points)
    check_design = _points(check_design_points)
    check_machine = _points(check_machine_points)
    physical_b = _finite_float(physical_b_deg, "Physical B")
    _finite_point(pivot_machine_xy, "B pivot")
    fit = fit_rigid_registration(
        design_points=source_design,
        machine_points=source_machine,
        design_unit_mm=metadata.design_unit_mm,
        check_design_points=check_design,
        check_machine_points=check_machine,
    )
    transform = BFrameTransform(
        origin_xy_at_reference_b=(
            float(fit.offset_machine_mm[0]),
            float(fit.offset_machine_mm[1]),
        ),
        reference_b_deg=physical_b,
        xy_angle_at_reference_b_deg=float(fit.rotation_deg),
        b_zero_machine_deg=physical_b - float(fit.rotation_deg),
    )
    readiness = dict(draft.readiness)
    for axis in ("X", "Y", "B"):
        readiness[axis] = AxisReadiness(ReadinessStatus.READY)
    for axis in ("Z", "A"):
        readiness[axis] = AxisReadiness(
            ReadinessStatus.MISSING,
            f"Design {axis} reference is not registered.",
        )
    updated_metadata = replace(
        metadata,
        source_design_marks=source_design,
        source_machine_marks=source_machine,
        check_design_marks=check_design,
        check_machine_marks=check_machine,
        scale_ratio=float(fit.distance_scale_ratio),
        rms_residual_mm=float(fit.source_residual.rms_mm),
        max_residual_mm=float(fit.source_residual.max_mm),
    )
    return replace(
        draft,
        transform=transform,
        readiness=readiness,
        metadata=updated_metadata.to_dict(),
    )


def migrate_legacy_design_state(
    legacy_state: Mapping[str, object],
    *,
    design_document: DesignDocument,
    physical_b_deg: float,
    pivot_machine_xy: Point2D = (0.0, 0.0),
    existing_names: Iterable[str] = (),
    metadata: DesignFrameMetadata | None = None,
) -> CoordinateFrameRecord | None:
    """Convert a saved mark registration while leaving Z and A unavailable."""

    source_design = _points(legacy_state.get("source_design_marks", ()))
    source_machine = _points(legacy_state.get("source_stage_marks", ()))
    if len(source_design) < 2 or len(source_design) != len(source_machine):
        return None
    check_design = _points(legacy_state.get("check_design_marks", ()))
    check_machine = _points(legacy_state.get("check_stage_marks", ()))
    if len(check_design) != len(check_machine):
        check_design = ()
        check_machine = ()
    migrated = commit_xyb_registration(
        new_design_frame_draft(
            design_document,
            existing_names=existing_names,
            metadata=metadata,
        ),
        design_points=source_design,
        physical_machine_points=source_machine,
        physical_b_deg=physical_b_deg,
        pivot_machine_xy=pivot_machine_xy,
        check_design_points=check_design,
        check_machine_points=check_machine,
    )
    if bool(legacy_state.get("registration_valid", True)):
        return migrated
    reason = str(
        legacy_state.get("registration_stale_reason")
        or legacy_state.get("registration_status")
        or "Migrated Design registration is stale."
    )
    return invalidate_axes(migrated, {"X", "Y", "B"}, reason)


def find_equivalent_migrated_frame(
    frames: Iterable[CoordinateFrameRecord],
    candidate: CoordinateFrameRecord,
) -> CoordinateFrameRecord | None:
    """Find a previously published migration result, ignoring identity/version."""

    candidate_metadata = DesignFrameMetadata.from_mapping(candidate.metadata)
    for frame in frames:
        try:
            frame_metadata = DesignFrameMetadata.from_mapping(frame.metadata)
        except (KeyError, TypeError, ValueError):
            continue
        if (
            frame.kind is candidate.kind
            and frame.transform == candidate.transform
            and frame.readiness == candidate.readiness
            and frame_metadata == candidate_metadata
        ):
            return frame
    return None


def design_frame_for_loaded_document(
    frame: CoordinateFrameRecord,
    design_document: DesignDocument,
    *,
    current_metadata: DesignFrameMetadata | None = None,
) -> CoordinateFrameRecord:
    """Retain a matching frame or stale it when its source fingerprint changed."""

    stored = DesignFrameMetadata.from_mapping(frame.metadata)
    current = current_metadata or DesignFrameMetadata.from_document(design_document)
    if (
        Path(stored.source_path).resolve() == Path(current.source_path).resolve()
        and stored.source_size == current.source_size
        and stored.source_mtime_ns == current.source_mtime_ns
        and stored.source_sha256 == current.source_sha256
        and stored.top_cell_name == current.top_cell_name
    ):
        return frame
    return invalidate_axes(
        frame,
        {"X", "Y", "B"},
        "Design source file changed since this frame was registered.",
    )


def _next_frame_name(base_name: str, existing_names: Iterable[str]) -> str:
    used = {str(name).casefold() for name in existing_names}
    if base_name.casefold() not in used:
        return base_name
    suffix = 2
    while f"{base_name} ({suffix})".casefold() in used:
        suffix += 1
    return f"{base_name} ({suffix})"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite_float(value: object, label: str) -> float:
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{label} must be finite.")
    return converted


def _finite_point(value: object, label: str) -> Point2D:
    try:
        x_value, y_value = value
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must contain two coordinates.") from exc
    return (
        _finite_float(x_value, f"{label} X"),
        _finite_float(y_value, f"{label} Y"),
    )


def _points(value: object) -> tuple[Point2D, ...]:
    if value is None:
        return ()
    return tuple(_finite_point(point, "Registration mark") for point in value)


def _optional_float(value: object) -> float | None:
    return None if value is None else _finite_float(value, "Registration diagnostic")


__all__ = [
    "DesignFrameDraft",
    "DesignFrameMetadata",
    "commit_xyb_registration",
    "design_frame_for_loaded_document",
    "find_equivalent_migrated_frame",
    "migrate_legacy_design_state",
    "new_design_frame_draft",
]
