"""Immutable interface values for coordinate-system orchestration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, TypeAlias

from probe_station_gui.design.session import DesignFrameLinkProjection, DesignSession

from .model import CoordinateFrameRecord

if TYPE_CHECKING:
    from .persistence import CoordinateFrameDocument


@dataclass(frozen=True)
class MachineProfileObservation:
    machine_profile_id: str

    def __post_init__(self) -> None:
        profile_id = str(self.machine_profile_id).strip()
        object.__setattr__(self, "machine_profile_id", profile_id or "default")


@dataclass(frozen=True)
class CoordinateNotice:
    message: str
    severity: str = "info"
    duration_ms: int = 0
    code: str | None = None


@dataclass(frozen=True)
class LoadCoordinateFramesIntent:
    intent_id: int
    machine_profile_id: str


@dataclass(frozen=True)
class SaveCoordinateFramesIntent:
    intent_id: int
    document: CoordinateFrameDocument


CoordinateAdapterIntent: TypeAlias = (
    LoadCoordinateFramesIntent | SaveCoordinateFramesIntent
)


@dataclass(frozen=True)
class CoordinateAdapterCompletion:
    intent_id: int
    result: object


@dataclass(frozen=True)
class DesignSessionCheckpoint:
    """Exact coordinate-relevant state restored after a failed publication."""

    active_frame_id: str | None
    source_design_marks: tuple[object, ...]
    source_stage_marks: tuple[object, ...]
    check_design_marks: tuple[object, ...]
    check_stage_marks: tuple[object, ...]
    registration: object | None
    registration_status: str
    runtime_blocked_persisted_state: object | None
    legacy_stage_coordinate_provenance: object | None
    legacy_stage_coordinate_provenance_present: bool

    @classmethod
    def capture(cls, session: DesignSession) -> DesignSessionCheckpoint:
        return cls(
            active_frame_id=session.active_frame_id,
            source_design_marks=tuple(session.source_design_marks),
            source_stage_marks=tuple(session.source_stage_marks),
            check_design_marks=tuple(session.check_design_marks),
            check_stage_marks=tuple(session.check_stage_marks),
            registration=session.registration,
            registration_status=str(session.registration_status),
            runtime_blocked_persisted_state=deepcopy(
                getattr(session, "_runtime_blocked_persisted_state", None)
            ),
            legacy_stage_coordinate_provenance=deepcopy(
                getattr(session, "_legacy_stage_coordinate_provenance", None)
            ),
            legacy_stage_coordinate_provenance_present=bool(
                getattr(
                    session,
                    "_legacy_stage_coordinate_provenance_present",
                    False,
                )
            ),
        )

    def restore(self, session: DesignSession) -> None:
        session.active_frame_id = self.active_frame_id
        session.source_design_marks = tuple(self.source_design_marks)
        session.source_stage_marks = tuple(self.source_stage_marks)
        session.check_design_marks = list(self.check_design_marks)
        session.check_stage_marks = list(self.check_stage_marks)
        session.registration = self.registration
        session.registration_status = self.registration_status
        session._runtime_blocked_persisted_state = deepcopy(
            self.runtime_blocked_persisted_state
        )
        session._legacy_stage_coordinate_provenance = deepcopy(
            self.legacy_stage_coordinate_provenance
        )
        session._legacy_stage_coordinate_provenance_present = (
            self.legacy_stage_coordinate_provenance_present
        )

    def with_registration_baseline(self, baseline: object) -> DesignSessionCheckpoint:
        """Overlay the immutable capture-batch baseline onto a pre-effect checkpoint."""

        baseline_status = getattr(baseline, "baseline_registration_status", None)
        return replace(
            self,
            source_stage_marks=tuple(baseline.baseline_source_stage_marks),
            check_stage_marks=tuple(baseline.baseline_check_stage_marks),
            registration=baseline.baseline_registration,
            registration_status=(
                self.registration_status
                if baseline_status is None
                else str(baseline_status)
            ),
        )


@dataclass(frozen=True)
class DesignSessionFrameLink:
    frame_id: str
    projection: DesignFrameLinkProjection
    runtime_record: CoordinateFrameRecord | None = None


@dataclass(frozen=True)
class FrameRecordsPublication:
    records: tuple[CoordinateFrameRecord, ...]
    previous_record: CoordinateFrameRecord | None = None
    committed_record: CoordinateFrameRecord | None = None
    previous_session: DesignSessionCheckpoint | None = None
    proposed_session_link: DesignSessionFrameLink | None = None
    success_notice: CoordinateNotice | None = None

    @classmethod
    def for_committed_record(
        cls,
        records: tuple[CoordinateFrameRecord, ...],
        committed_record: CoordinateFrameRecord,
        *,
        previous_record: CoordinateFrameRecord | None = None,
        previous_session: DesignSessionCheckpoint | None = None,
        projection: DesignFrameLinkProjection | None = None,
        runtime_record: CoordinateFrameRecord | None = None,
        success_notice: CoordinateNotice | None = None,
        success_message: str | None = None,
        success_duration_ms: int = 0,
        success_code: str | None = None,
    ) -> FrameRecordsPublication:
        """Build one immutable replace-or-append proposal without touching owners."""

        if success_notice is not None and success_message is not None:
            raise ValueError("Provide either success_notice or success_message.")

        replaced = tuple(
            committed_record if record.frame_id == committed_record.frame_id else record
            for record in records
        )
        proposed_records = (
            replaced
            if any(record.frame_id == committed_record.frame_id for record in records)
            else (*replaced, committed_record)
        )
        link = (
            None
            if projection is None
            else DesignSessionFrameLink(
                frame_id=committed_record.frame_id,
                projection=projection,
                runtime_record=runtime_record,
            )
        )
        notice = (
            success_notice
            if success_message is None
            else CoordinateNotice(
                success_message,
                duration_ms=success_duration_ms,
                code=success_code,
            )
        )
        return cls(
            records=proposed_records,
            previous_record=previous_record,
            committed_record=committed_record,
            previous_session=previous_session,
            proposed_session_link=link,
            success_notice=notice,
        )


@dataclass(frozen=True)
class CoordinateSystemSnapshot:
    frames_loaded: bool
    records: tuple[CoordinateFrameRecord, ...]
    document: CoordinateFrameDocument | None
    selected_frame_id: str = "machine"


@dataclass(frozen=True)
class CoordinateTransition:
    snapshot: CoordinateSystemSnapshot
    intents: tuple[CoordinateAdapterIntent, ...] = ()
    notices: tuple[CoordinateNotice, ...] = ()


__all__ = [
    "CoordinateAdapterCompletion",
    "CoordinateAdapterIntent",
    "CoordinateNotice",
    "CoordinateSystemSnapshot",
    "CoordinateTransition",
    "DesignSessionCheckpoint",
    "DesignSessionFrameLink",
    "FrameRecordsPublication",
    "LoadCoordinateFramesIntent",
    "MachineProfileObservation",
    "SaveCoordinateFramesIntent",
]
