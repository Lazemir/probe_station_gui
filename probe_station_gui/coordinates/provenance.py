"""Off-thread validation for durable Design-frame provenance."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
from typing import Iterable

from probe_station_gui.design.frame_registration import DesignFrameMetadata

from .model import CoordinateFrameRecord, FrameKind


RUNTIME_PROVENANCE_STATUS = "_runtime_provenance_status"
RUNTIME_PROVENANCE_REASON = "_runtime_provenance_reason"


@dataclass(frozen=True)
class FrameProvenanceDiagnostic:
    frame_id: str
    message: str


@dataclass(frozen=True)
class DesignFrameProvenanceResult:
    records: tuple[CoordinateFrameRecord, ...]
    diagnostics: tuple[FrameProvenanceDiagnostic, ...] = ()


def design_frame_provenance_error(record: CoordinateFrameRecord) -> str | None:
    """Return a runtime provenance block, if validation has failed or is pending."""

    if record.kind is not FrameKind.DESIGN:
        return None
    status = record.metadata.get(RUNTIME_PROVENANCE_STATUS)
    if status not in {"pending", "blocked", "unknown"}:
        return None
    reason = str(record.metadata.get(RUNTIME_PROVENANCE_REASON, "")).strip()
    return reason or "Design coordinate provenance is unavailable."


def validate_design_frame_provenance(
    records: Iterable[CoordinateFrameRecord],
    current_machine_profile_id: str,
) -> DesignFrameProvenanceResult:
    """Validate source fingerprints and machine identity without GUI interaction."""

    profile_id = str(current_machine_profile_id).strip()
    if not profile_id:
        raise ValueError("Current machine profile ID must not be empty.")
    validated: list[CoordinateFrameRecord] = []
    diagnostics: list[FrameProvenanceDiagnostic] = []
    for record in records:
        if record.kind is not FrameKind.DESIGN:
            validated.append(record)
            continue
        try:
            _validate_one(record, profile_id)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            reason = _user_reason(exc)
            validated.append(_with_status(record, "blocked", reason))
            diagnostics.append(FrameProvenanceDiagnostic(record.frame_id, reason))
        else:
            validated.append(_with_status(record, "verified", ""))
    return DesignFrameProvenanceResult(tuple(validated), tuple(diagnostics))


def mark_design_frame_provenance_pending(
    records: Iterable[CoordinateFrameRecord],
) -> tuple[CoordinateFrameRecord, ...]:
    """Fail closed while the asynchronous source checks are outstanding."""

    return tuple(
        _with_status(
            record,
            "pending",
            "Design coordinate provenance is being checked.",
        )
        if record.kind is FrameKind.DESIGN
        else record
        for record in records
    )


def _validate_one(record: CoordinateFrameRecord, profile_id: str) -> None:
    raw = record.metadata
    for key in ("source_size", "source_mtime_ns"):
        value = raw[key]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("Design source fingerprint is invalid.")
    for key in ("source_path", "source_sha256", "machine_profile_id"):
        value = raw[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Design source fingerprint is invalid.")
    metadata = DesignFrameMetadata.from_mapping(raw)
    if metadata.machine_profile_id != profile_id:
        raise ValueError("Design coordinate frame belongs to a different machine profile.")
    source = Path(metadata.source_path).expanduser().resolve()
    stat = source.stat()
    if (
        stat.st_size != metadata.source_size
        or stat.st_mtime_ns != metadata.source_mtime_ns
        or _sha256(source) != metadata.source_sha256
    ):
        raise ValueError("Design source changed since this frame was registered.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _with_status(
    record: CoordinateFrameRecord,
    status: str,
    reason: str,
) -> CoordinateFrameRecord:
    metadata = dict(record.metadata)
    metadata[RUNTIME_PROVENANCE_STATUS] = status
    metadata[RUNTIME_PROVENANCE_REASON] = reason
    return replace(record, metadata=metadata)


def _user_reason(exc: Exception) -> str:
    if isinstance(exc, OSError):
        return "Design source is unavailable."
    message = str(exc).strip()
    return message or "Design coordinate provenance is unavailable."


__all__ = [
    "RUNTIME_PROVENANCE_REASON",
    "RUNTIME_PROVENANCE_STATUS",
    "DesignFrameProvenanceResult",
    "FrameProvenanceDiagnostic",
    "design_frame_provenance_error",
    "mark_design_frame_provenance_pending",
    "validate_design_frame_provenance",
]
