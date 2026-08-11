"""Immutable interface values for coordinate-system orchestration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, replace
import math
from typing import TYPE_CHECKING, TypeAlias

from .model import (
    STAGE_AXES,
    AxisReadiness,
    CoordinateFrameRecord,
    PhysicalMachinePose,
)
from .presentation import CoordinateDisplayPlan
from .transforms import BFrameTransform

if TYPE_CHECKING:
    from probe_station_gui.design.session import (
        DesignFrameLinkProjection,
        DesignSession,
        DesignSessionState,
    )
    from probe_station_gui.design.frame_registration import DesignFrameMetadata
    from probe_station_gui.design.model import DesignDocument
    from probe_station_gui.stage.machine_coordinates import MachineCoordinateSnapshot

    from .persistence import CoordinateFrameDocument
    from probe_station_gui.settings.software_coordinates import (
        SoftwareCoordinateSettings,
    )


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


@dataclass(frozen=True)
class CaptureMachinePoseIntent:
    intent_id: int
    axes: tuple[str, ...] = ("X", "Y", "B")


@dataclass(frozen=True)
class MoveToFocusTargetIntent:
    intent_id: int
    target_xy: tuple[float, float]


@dataclass(frozen=True)
class RunAutofocusIntent:
    intent_id: int


@dataclass(frozen=True)
class ReadPhysicalAIntent:
    intent_id: int


@dataclass(frozen=True)
class PersistCoordinateSelectionIntent:
    frame_id: str


@dataclass(frozen=True)
class RewriteLegacyDesignStateIntent:
    """Replace controller-owned legacy Design state after frame durability."""

    intent_id: int
    persisted_design_state: object


CoordinateAdapterIntent: TypeAlias = (
    CaptureMachinePoseIntent
    | LoadCoordinateFramesIntent
    | MoveToFocusTargetIntent
    | PersistCoordinateSelectionIntent
    | ReadPhysicalAIntent
    | RewriteLegacyDesignStateIntent
    | RunAutofocusIntent
    | SaveCoordinateFramesIntent
)


@dataclass(frozen=True)
class CoordinateSystemSelection:
    frame_id: str

    def __post_init__(self) -> None:
        frame_id = str(self.frame_id).strip()
        if not frame_id:
            raise ValueError("Coordinate System ID must not be empty.")
        object.__setattr__(self, "frame_id", frame_id)


@dataclass(frozen=True)
class CoordinateAuthorityObservation:
    physical_pose: PhysicalMachinePose
    homed_axes: frozenset[str]
    machine_snapshot: MachineCoordinateSnapshot | None
    pivot_machine_xy: tuple[float, float] | None
    objective_xy_offset: tuple[float, float] = (0.0, 0.0)
    pivot_error: str | None = None
    pivot_error_permanent: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.physical_pose, PhysicalMachinePose):
            raise TypeError("physical_pose must be a PhysicalMachinePose")
        object.__setattr__(
            self,
            "homed_axes",
            frozenset(str(axis).strip().upper() for axis in self.homed_axes),
        )
        if self.pivot_error_permanent and not str(self.pivot_error or "").strip():
            raise ValueError("A permanent pivot error requires a reason.")


@dataclass(frozen=True)
class CoordinateMotionLease:
    """Immutable selected-system basis shared by display and one GUI move."""

    selected_frame_id: str
    available: bool
    reason: str | None
    record: CoordinateFrameRecord | None
    authority: CoordinateAuthorityObservation
    display_values: tuple[tuple[str, float], ...]
    basis_fingerprint: tuple[object, ...]
    fingerprint: tuple[object, ...]


@dataclass(frozen=True)
class CoordinateMotionRequest:
    """Absolute or relative GUI-axis values tied to one displayed snapshot."""

    lease: CoordinateMotionLease
    mode: str
    axis_values: tuple[tuple[str, float], ...]
    allow_pose_rebase: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.lease, CoordinateMotionLease):
            raise TypeError("lease must be a CoordinateMotionLease")
        mode = str(self.mode).strip().upper()
        if mode not in {"G90", "G91"}:
            raise ValueError("Coordinate motion mode must be G90 or G91.")
        normalized: list[tuple[str, float]] = []
        seen: set[str] = set()
        for raw_axis, raw_value in self.axis_values:
            axis = str(raw_axis).strip().upper()
            if axis not in STAGE_AXES:
                raise ValueError(f"Unsupported stage axis {raw_axis!r}.")
            if axis in seen:
                raise ValueError(f"Coordinate motion axis {axis} appears more than once.")
            value = float(raw_value)
            if not math.isfinite(value):
                raise ValueError(f"{axis} coordinate must be finite.")
            seen.add(axis)
            normalized.append((axis, value))
        if not normalized:
            raise ValueError("Coordinate motion requires at least one axis value.")
        object.__setattr__(self, "mode", mode)
        object.__setattr__(self, "axis_values", tuple(normalized))
        object.__setattr__(self, "allow_pose_rebase", bool(self.allow_pose_rebase))


@dataclass(frozen=True)
class CoordinateMotionProjection:
    """Configured Machine targets/vector derived from one GUI motion lease."""

    lease: CoordinateMotionLease
    accepted: bool
    raw_targets: tuple[tuple[str, float], ...] = ()
    raw_distances: tuple[tuple[str, float], ...] = ()
    display_targets: tuple[tuple[str, float], ...] = ()
    machine_targets: tuple[tuple[str, float], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class CustomSystemsRequest:
    settings: SoftwareCoordinateSettings


@dataclass(frozen=True)
class DesignCalibrationObservation:
    fingerprints: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class DesignCoordinateLease:
    """One immutable, current-or-stale capability for Design projection."""

    load_complete: bool
    frame_id: str | None
    frame_version: int | None
    transform: BFrameTransform | None
    readiness: tuple[tuple[str, AxisReadiness], ...]
    provenance_error: str | None
    authority_blocked_axes: frozenset[str]
    rejection_reason: str | None
    document: DesignDocument | None
    metadata: DesignFrameMetadata | None
    machine_coordinate_snapshot: MachineCoordinateSnapshot | None
    pivot_machine_xy: tuple[float, float] | None
    objective_xy_offset: tuple[float, float]
    fingerprint: tuple[object, ...]

    @property
    def usable(self) -> bool:
        return self.rejection_reason is None

    @property
    def reason(self) -> str | None:
        return self.rejection_reason

    def readiness_for(self, axis: str) -> AxisReadiness | None:
        return dict(self.readiness).get(str(axis).strip().upper())


@dataclass(frozen=True)
class CoordinateAdapterCompletion:
    intent_id: int
    result: object


@dataclass(frozen=True)
class RegistrationCaptureRequest:
    pivot_machine_xy: tuple[float, float]
    objective_xy_offset: tuple[float, float]
    check_mark: bool = False
    operator_alignment: bool = False
    mark_index: int | None = None
    configured_target_xy: tuple[float, float] | None = None
    capture_source: str | None = None
    operator_pick_generation: int | None = None


@dataclass(frozen=True)
class RegistrationSourceMarkRequest:
    point: tuple[float, float]
    slot: int | None = None


@dataclass(frozen=True)
class RegistrationSourceMarksRequest:
    points: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class RegistrationCheckMarkRequest:
    point: tuple[float, float]


@dataclass(frozen=True)
class RegistrationInvalidationRequest:
    reason: str


@dataclass(frozen=True)
class RegistrationAlignmentRequest:
    preparation: object


@dataclass(frozen=True)
class DesignActivationRequest:
    session_state: DesignSessionState | None
    frame_metadata: object | None
    machine_snapshot: object | None
    pivot_machine_xy: tuple[float, float] | None
    objective_xy_offset: tuple[float, float] = (0.0, 0.0)
    requested_frame_id: str | None = None
    create_new: bool = False
    create_new_if_registered: bool = False
    homed_axes: frozenset[str] = frozenset({"X", "Y"})
    workspace_before: DesignWorkspaceCheckpoint | None = None
    workspace_after: DesignWorkspaceCheckpoint | None = None


@dataclass(frozen=True)
class MachinePoseCaptureResult:
    intent_id: int
    succeeded: bool
    snapshot: object | None = None
    message: str = ""
    active_operator_pick_slot: int | None = None
    active_operator_pick_generation: int | None = None


@dataclass(frozen=True)
class RegistrationOpticalObservation:
    fov_size: tuple[float, float]
    objective_name: str
    optical_calibration_identity: str


@dataclass(frozen=True)
class FocusSearchLease:
    context: object = field(repr=False)


@dataclass(frozen=True)
class FocusCandidateRequest:
    candidate: object
    optical: RegistrationOpticalObservation
    lease: FocusSearchLease | None = None


@dataclass(frozen=True)
class FocusReferenceRequest:
    design_point: tuple[float, float]
    optical: RegistrationOpticalObservation
    machine_snapshot: object
    pivot_machine_xy: tuple[float, float]
    objective_xy_offset: tuple[float, float]


@dataclass(frozen=True)
class FocusReferenceResetRequest:
    machine_snapshot: object
    pivot_machine_xy: tuple[float, float]
    objective_xy_offset: tuple[float, float]
    reason: str = "Focus reference reset."


@dataclass(frozen=True)
class FocusMoveResult:
    intent_id: int
    succeeded: bool
    message: str = ""
    completed_target_xy: tuple[float, float] | None = None


@dataclass(frozen=True)
class AutofocusResult:
    intent_id: int
    succeeded: bool
    physical_z_mm: float | None = None
    message: str = ""


@dataclass(frozen=True)
class FirstContactRequest:
    frame_id: str
    frame_version: int


@dataclass(frozen=True)
class PhysicalAReadResult:
    intent_id: int
    succeeded: bool
    physical_a_mm: float | None = None
    interrupted: bool = False
    message: str = ""


@dataclass(frozen=True)
class LegacyDesignStateRewriteResult:
    intent_id: int
    succeeded: bool
    message: str = ""


@dataclass(frozen=True)
class OperatorPickRelease:
    slot: int
    generation: int | None


@dataclass(frozen=True)
class RegistrationProjection:
    valid: bool
    status: str
    source_stage_marks: tuple[tuple[float, float], ...]
    matrix: tuple[tuple[float, float], tuple[float, float]]
    design_unit_mm: float


@dataclass(frozen=True)
class FinishOperatorAlignmentUiEffect:
    """Finish the exact operator-alignment draft just published."""


@dataclass(frozen=True)
class RestoreOperatorAlignmentUiEffect:
    """Restore the exact draft checkpoint after publication rollback."""

    design_marks: tuple[tuple[float, float], ...]
    stage_marks: tuple[tuple[float, float] | None, ...]


@dataclass(frozen=True)
class DesignWorkspaceCheckpoint:
    """Detached adapter-owned Design presentation and navigation state."""

    session_state: DesignSessionState
    frame_metadata: object | None = None
    markup: object | None = None
    direct_guide_ids: tuple[str, ...] = ()
    pending_visibility: object | None = None
    last_selected_design_point: tuple[float, float] | None = None
    pending_alignment_preparation: object | None = None


@dataclass(frozen=True)
class RestoreDesignWorkspaceUiEffect:
    """Three-way rollback one optimistic adapter workspace checkpoint."""

    previous: DesignWorkspaceCheckpoint
    applied: DesignWorkspaceCheckpoint


CoordinateUiEffect: TypeAlias = (
    FinishOperatorAlignmentUiEffect
    | RestoreOperatorAlignmentUiEffect
    | RestoreDesignWorkspaceUiEffect
)


@dataclass(frozen=True)
class RegistrationWorkflowSnapshot:
    active_frame_id: str | None = None
    active_frame_version: int | None = None
    source_design_marks: tuple[tuple[float, float], ...] = ()
    source_stage_marks: tuple[tuple[float, float], ...] = ()
    check_design_marks: tuple[tuple[float, float], ...] = ()
    check_stage_marks: tuple[tuple[float, float], ...] = ()
    registration_valid: bool = False
    registration_status: str = "No design registration."
    registration_projection: RegistrationProjection | None = None
    operator_stage_marks: tuple[tuple[float, float] | None, ...] = ()
    alignment_fit_residuals: tuple[float, float] | None = None
    focus_candidate: object | None = None
    operator_pick_release: OperatorPickRelease | None = None
    registration_instances: tuple[tuple[str, str], ...] = ()
    legacy_migration_state: object | None = None


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
    state: DesignSessionState | None = None

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
            state=session.snapshot_state(),
        )

    def restore(self, session: DesignSession) -> None:
        if self.state is not None:
            session.apply_state(self.state)
            return
        self.restore_coordinate_state(session)

    def restore_coordinate_state(self, session: DesignSession) -> None:
        """Restore only frame-link state, preserving newer design annotations."""

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
            state=(
                None
                if self.state is None
                else replace(
                    self.state,
                    source_stage_marks=tuple(
                        baseline.baseline_source_stage_marks
                    ),
                    check_stage_marks=tuple(
                        baseline.baseline_check_stage_marks
                    ),
                    registration=baseline.baseline_registration,
                    registration_status=(
                        self.state.registration_status
                        if baseline_status is None
                        else str(baseline_status)
                    ),
                )
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
    ui_effects: tuple[CoordinateUiEffect, ...] = ()
    rollback_ui_effects: tuple[CoordinateUiEffect, ...] = ()

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
        ui_effects: tuple[CoordinateUiEffect, ...] = (),
        rollback_ui_effects: tuple[CoordinateUiEffect, ...] = (),
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
            ui_effects=tuple(ui_effects),
            rollback_ui_effects=tuple(rollback_ui_effects),
        )


@dataclass(frozen=True)
class CoordinateSystemSnapshot:
    frames_loaded: bool
    records: tuple[CoordinateFrameRecord, ...]
    document: CoordinateFrameDocument | None
    selected_frame_id: str = "machine"
    display_plan: CoordinateDisplayPlan | None = None
    design_lease: DesignCoordinateLease | None = None
    motion_lease: CoordinateMotionLease | None = None
    registration: RegistrationWorkflowSnapshot = field(
        default_factory=RegistrationWorkflowSnapshot
    )


@dataclass(frozen=True)
class CoordinateTransition:
    snapshot: CoordinateSystemSnapshot
    intents: tuple[CoordinateAdapterIntent, ...] = ()
    notices: tuple[CoordinateNotice, ...] = ()
    accepted: bool = True
    view_changed: bool = False
    ui_effects: tuple[CoordinateUiEffect, ...] = ()


@dataclass(frozen=True)
class _RegistrationTransitionParts:
    intents: tuple[CoordinateAdapterIntent, ...] = ()
    notices: tuple[CoordinateNotice, ...] = ()
    publication: FrameRecordsPublication | None = None
    operator_pick_release: OperatorPickRelease | None = None
    proposed_session_state: DesignSessionState | None = None
    accepted: bool = True
    view_changed: bool = False
    ui_effects: tuple[CoordinateUiEffect, ...] = ()


__all__ = [
    "CoordinateAdapterCompletion",
    "CoordinateAdapterIntent",
    "CaptureMachinePoseIntent",
    "CoordinateNotice",
    "CoordinateAuthorityObservation",
    "CoordinateMotionLease",
    "CoordinateMotionProjection",
    "CoordinateMotionRequest",
    "CoordinateSystemSelection",
    "CustomSystemsRequest",
    "DesignCalibrationObservation",
    "DesignCoordinateLease",
    "CoordinateSystemSnapshot",
    "CoordinateTransition",
    "FinishOperatorAlignmentUiEffect",
    "DesignSessionCheckpoint",
    "DesignWorkspaceCheckpoint",
    "DesignActivationRequest",
    "DesignSessionFrameLink",
    "FrameRecordsPublication",
    "FocusCandidateRequest",
    "FocusMoveResult",
    "FocusReferenceRequest",
    "FocusReferenceResetRequest",
    "FocusSearchLease",
    "FirstContactRequest",
    "LoadCoordinateFramesIntent",
    "LegacyDesignStateRewriteResult",
    "MachinePoseCaptureResult",
    "MachineProfileObservation",
    "OperatorPickRelease",
    "RegistrationCaptureRequest",
    "RegistrationAlignmentRequest",
    "RegistrationCheckMarkRequest",
    "RegistrationInvalidationRequest",
    "RegistrationSourceMarkRequest",
    "RegistrationSourceMarksRequest",
    "RegistrationOpticalObservation",
    "RegistrationProjection",
    "RegistrationWorkflowSnapshot",
    "RestoreOperatorAlignmentUiEffect",
    "RestoreDesignWorkspaceUiEffect",
    "PhysicalAReadResult",
    "PersistCoordinateSelectionIntent",
    "ReadPhysicalAIntent",
    "RewriteLegacyDesignStateIntent",
    "MoveToFocusTargetIntent",
    "RunAutofocusIntent",
    "AutofocusResult",
    "SaveCoordinateFramesIntent",
]
