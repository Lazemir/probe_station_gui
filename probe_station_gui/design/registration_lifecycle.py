"""Registration capture identity, batching, normalization, and cancellation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import uuid

from probe_station_gui.coordinates import rotate_xy


MachinePoint = tuple[float, float]
StagePoint = tuple[float, float]


class RegistrationCancellation(str, Enum):
    """Reasons that invalidate an in-flight registration capture batch."""

    DOCUMENT_UNLOADED = "document_unloaded"
    DESIGN_CHANGED = "design_changed"
    TOP_CELL_CHANGED = "top_cell_changed"
    FRAME_CHANGED = "frame_changed"
    MARK_SET_CHANGED = "mark_set_changed"


@dataclass(frozen=True)
class RegistrationCaptureContext:
    """Immutable design/frame context and rollback baseline for a capture batch."""

    session_identity: int
    frame_id: str | None
    frame_version: int | None
    source_identity: tuple[str, str]
    top_cell_name: str
    rotation_quarter_turns: int
    pivot_machine_xy: MachinePoint
    source_design_marks: tuple[MachinePoint, ...]
    check_design_marks: tuple[MachinePoint, ...]
    objective_xy_offset: MachinePoint = (0.0, 0.0)
    mark_kind: str = "source"
    baseline_source_stage_marks: tuple[StagePoint | None, ...] = ()
    baseline_check_stage_marks: tuple[StagePoint, ...] = ()
    baseline_registration: object | None = None
    baseline_registration_status: str = "No design registration."
    existing_source_machine_marks: tuple[MachinePoint, ...] = ()
    existing_check_machine_marks: tuple[MachinePoint, ...] = ()


@dataclass(frozen=True)
class RegistrationCaptureToken:
    request_id: str
    operation_id: str
    session_identity: int
    frame_id: str | None
    frame_version: int | None
    source_identity: tuple[str, str]
    top_cell_name: str
    rotation_quarter_turns: int
    pivot_machine_xy: MachinePoint
    source_design_marks: tuple[MachinePoint, ...]
    check_design_marks: tuple[MachinePoint, ...]
    objective_xy_offset: MachinePoint = (0.0, 0.0)
    mark_kind: str = "source"
    superseded_effects: RegistrationEffects = field(
        default_factory=lambda: RegistrationEffects(),
        compare=False,
        repr=False,
    )


@dataclass(frozen=True)
class RegistrationSample:
    """One synchronized physical Machine sample and its display-stage point."""

    mark_kind: str
    physical_machine_xy: MachinePoint
    physical_b_deg: float
    stage_xy: StagePoint | None = None


@dataclass(frozen=True)
class RegistrationCaptureOutcome:
    """Immutable hardware callback result accepted before adapter conversion."""

    succeeded: bool
    message: str | None = None


@dataclass(frozen=True)
class NormalizedRegistrationSample:
    """A captured sample expressed at the batch reference B position."""

    mark_kind: str
    machine_xy: MachinePoint
    reference_b_deg: float
    captured_machine_xy: MachinePoint
    captured_b_deg: float
    captured_pivot_machine_xy: MachinePoint
    stage_xy: StagePoint | None


@dataclass(frozen=True)
class RegistrationEffects:
    """Immutable actions for the Qt/session/registry adapter to execute."""

    accepted: bool = False
    reason: str | None = None
    cancellation: RegistrationCancellation | None = None
    restore_baseline: bool = False
    baseline_session_identity: int | None = None
    baseline_frame_id: str | None = None
    baseline_source_stage_marks: tuple[StagePoint | None, ...] = ()
    baseline_check_stage_marks: tuple[StagePoint, ...] = ()
    baseline_registration: object | None = None
    baseline_registration_status: str | None = None
    captured_sample: RegistrationSample | None = None
    normalized_samples: tuple[NormalizedRegistrationSample, ...] = ()
    commit_requested: bool = False
    rollback_effects: RegistrationEffects | None = None


@dataclass
class _PendingRegistrationEvidence:
    context: RegistrationCaptureContext
    operation_id: str
    active_request_id: str
    samples: list[tuple[RegistrationCaptureToken, RegistrationSample]] = field(
        default_factory=list
    )


class DesignRegistrationLifecycle:
    """Own one transactional registration evidence batch at a time."""

    def __init__(self) -> None:
        self._pending: _PendingRegistrationEvidence | None = None

    def begin_capture(
        self,
        context: RegistrationCaptureContext,
    ) -> RegistrationCaptureToken:
        pending = self._pending
        context_matches = pending is not None and self._contexts_match(
            pending.context,
            context,
        )
        operation_id = (
            pending.operation_id
            if context_matches
            else str(uuid.uuid4())
        )
        superseded_effects = (
            RegistrationEffects()
            if pending is None or context_matches
            else self._baseline_effects(pending)
        )
        request_id = str(uuid.uuid4())
        token = RegistrationCaptureToken(
            request_id=request_id,
            operation_id=operation_id,
            session_identity=context.session_identity,
            frame_id=context.frame_id,
            frame_version=context.frame_version,
            source_identity=context.source_identity,
            top_cell_name=context.top_cell_name,
            rotation_quarter_turns=int(context.rotation_quarter_turns) % 4,
            pivot_machine_xy=context.pivot_machine_xy,
            objective_xy_offset=context.objective_xy_offset,
            mark_kind=context.mark_kind,
            source_design_marks=context.source_design_marks,
            check_design_marks=context.check_design_marks,
            superseded_effects=superseded_effects,
        )
        if pending is None or not context_matches:
            pending_context = context
            if (
                pending is not None
                and pending.context.session_identity == context.session_identity
                and pending.context.frame_id == context.frame_id
            ):
                previous = pending.context
                pending_context = replace(
                    context,
                    baseline_source_stage_marks=(
                        previous.baseline_source_stage_marks
                    ),
                    baseline_check_stage_marks=(
                        previous.baseline_check_stage_marks
                    ),
                    baseline_registration=previous.baseline_registration,
                    baseline_registration_status=(
                        previous.baseline_registration_status
                    ),
                )
            self._pending = _PendingRegistrationEvidence(
                context=pending_context,
                operation_id=operation_id,
                active_request_id=request_id,
            )
        else:
            pending.active_request_id = request_id
        return token

    def accept_sample(
        self,
        token: RegistrationCaptureToken,
        sample: RegistrationSample | RegistrationCaptureOutcome,
    ) -> RegistrationEffects:
        pending = self._pending
        if pending is None or not self._token_is_current(token, pending):
            return RegistrationEffects(reason="Registration capture is stale.")
        if isinstance(sample, RegistrationCaptureOutcome):
            if sample.succeeded:
                return RegistrationEffects(accepted=True)
            pending.active_request_id = ""
            return RegistrationEffects(
                accepted=True,
                reason=str(sample.message or "Machine coordinates are unavailable."),
            )
        if (
            sample.mark_kind not in {"source", "check"}
            or sample.mark_kind != token.mark_kind
        ):
            return RegistrationEffects(reason="Registration mark kind is invalid.")

        pending.samples.append((token, sample))
        pending.active_request_id = ""
        normalized = self._normalized_samples(pending.samples)
        complete = self._batch_is_complete(pending)
        rollback_effects = self._baseline_effects(pending) if complete else None
        effects = RegistrationEffects(
            accepted=True,
            captured_sample=sample,
            normalized_samples=normalized,
            commit_requested=complete,
            baseline_session_identity=pending.context.session_identity,
            baseline_frame_id=pending.context.frame_id,
            baseline_source_stage_marks=pending.context.baseline_source_stage_marks,
            baseline_check_stage_marks=pending.context.baseline_check_stage_marks,
            baseline_registration=pending.context.baseline_registration,
            baseline_registration_status=(
                pending.context.baseline_registration_status
            ),
            rollback_effects=rollback_effects,
        )
        if complete:
            self._pending = None
        return effects

    def cancel(self, reason: RegistrationCancellation) -> RegistrationEffects:
        pending = self._pending
        self._pending = None
        if pending is None:
            return RegistrationEffects(cancellation=reason)
        return self._baseline_effects(pending, cancellation=reason)

    @staticmethod
    def _baseline_effects(
        pending: _PendingRegistrationEvidence,
        *,
        cancellation: RegistrationCancellation | None = None,
    ) -> RegistrationEffects:
        context = pending.context
        return RegistrationEffects(
            cancellation=cancellation,
            restore_baseline=True,
            baseline_session_identity=context.session_identity,
            baseline_frame_id=context.frame_id,
            baseline_source_stage_marks=context.baseline_source_stage_marks,
            baseline_check_stage_marks=context.baseline_check_stage_marks,
            baseline_registration=context.baseline_registration,
            baseline_registration_status=context.baseline_registration_status,
        )

    @staticmethod
    def _contexts_match(
        left: RegistrationCaptureContext,
        right: RegistrationCaptureContext,
    ) -> bool:
        return bool(
            left.session_identity == right.session_identity
            and left.frame_id == right.frame_id
            and left.frame_version == right.frame_version
            and left.source_identity == right.source_identity
            and left.top_cell_name == right.top_cell_name
            and int(left.rotation_quarter_turns) % 4
            == int(right.rotation_quarter_turns) % 4
            and left.pivot_machine_xy == right.pivot_machine_xy
            and left.objective_xy_offset == right.objective_xy_offset
            and left.source_design_marks == right.source_design_marks
            and left.check_design_marks == right.check_design_marks
            and left.existing_source_machine_marks
            == right.existing_source_machine_marks
            and left.existing_check_machine_marks
            == right.existing_check_machine_marks
        )

    @staticmethod
    def _token_is_current(
        token: RegistrationCaptureToken,
        pending: _PendingRegistrationEvidence,
    ) -> bool:
        context = pending.context
        return bool(
            token.request_id == pending.active_request_id
            and token.operation_id == pending.operation_id
            and token.session_identity == context.session_identity
            and token.frame_id == context.frame_id
            and token.frame_version == context.frame_version
            and token.source_identity == context.source_identity
            and token.top_cell_name == context.top_cell_name
            and token.rotation_quarter_turns
            == int(context.rotation_quarter_turns) % 4
            and token.pivot_machine_xy == context.pivot_machine_xy
            and token.objective_xy_offset == context.objective_xy_offset
            and token.source_design_marks == context.source_design_marks
            and token.check_design_marks == context.check_design_marks
        )

    @staticmethod
    def _batch_is_complete(pending: _PendingRegistrationEvidence) -> bool:
        context = pending.context
        source_count = len(context.existing_source_machine_marks) + sum(
            sample.mark_kind == "source" for _token, sample in pending.samples
        )
        check_count = len(context.existing_check_machine_marks) + sum(
            sample.mark_kind == "check" for _token, sample in pending.samples
        )
        return bool(
            len(context.source_design_marks) >= 2
            and source_count == len(context.source_design_marks)
            and check_count == len(context.check_design_marks)
        )

    @staticmethod
    def _normalized_samples(
        samples: list[tuple[RegistrationCaptureToken, RegistrationSample]],
    ) -> tuple[NormalizedRegistrationSample, ...]:
        reference_b = samples[0][1].physical_b_deg
        normalized: list[NormalizedRegistrationSample] = []
        for token, sample in samples:
            pivot = token.pivot_machine_xy
            offset = (
                sample.physical_machine_xy[0] - pivot[0],
                sample.physical_machine_xy[1] - pivot[1],
            )
            rotated = rotate_xy(offset, reference_b - sample.physical_b_deg)
            normalized.append(
                NormalizedRegistrationSample(
                    mark_kind=sample.mark_kind,
                    machine_xy=(pivot[0] + rotated[0], pivot[1] + rotated[1]),
                    reference_b_deg=reference_b,
                    captured_machine_xy=sample.physical_machine_xy,
                    captured_b_deg=sample.physical_b_deg,
                    captured_pivot_machine_xy=pivot,
                    stage_xy=sample.stage_xy,
                )
            )
        return tuple(normalized)


__all__ = [
    "DesignRegistrationLifecycle",
    "NormalizedRegistrationSample",
    "RegistrationCancellation",
    "RegistrationCaptureContext",
    "RegistrationCaptureOutcome",
    "RegistrationCaptureToken",
    "RegistrationEffects",
    "RegistrationSample",
]
