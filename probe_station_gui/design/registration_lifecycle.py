"""Registration capture identity, batching, normalization, and cancellation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import math
import uuid

from probe_station_gui.coordinates import rotate_xy
from probe_station_gui.design.focus_candidate import FocusCandidate


MachinePoint = tuple[float, float]
StagePoint = tuple[float, float]


class RegistrationCancellation(str, Enum):
    """Reasons that invalidate an in-flight registration capture batch."""

    DOCUMENT_UNLOADED = "document_unloaded"
    DESIGN_CHANGED = "design_changed"
    TOP_CELL_CHANGED = "top_cell_changed"
    FRAME_CHANGED = "frame_changed"
    MARK_SET_CHANGED = "mark_set_changed"
    Z_CHANGED = "z_changed"
    ROUTE_CONTEXT_CHANGED = "route_context_changed"


class FocusCompletionKind(str, Enum):
    TARGET_BOUND = "target_bound"
    MOVE_FINISHED = "move_finished"
    AUTOFOCUS_FINISHED = "autofocus_finished"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class FocusOperationToken:
    request_id: str
    frame_id: str | None
    frame_version: int | None


@dataclass(frozen=True)
class ContactOperationToken:
    request_id: str
    session_identity: int
    frame_id: str | None
    frame_version: int | None


@dataclass(frozen=True)
class FocusCompletion:
    kind: FocusCompletionKind
    token: FocusOperationToken
    target_xy: StagePoint | None = None
    succeeded: bool | None = None
    physical_z_mm: float | None = None
    message: str | None = None


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
    operator_alignment: bool = False
    mark_index: int | None = None
    configured_target_xy: MachinePoint | None = None
    capture_source: str | None = None


@dataclass(frozen=True)
class RegistrationContext:
    """Immutable design, optical, frame, and reference identity."""

    session_identity: int
    source_identity: tuple[str, str]
    top_cell_name: str
    visible_layers: tuple[tuple[int, int], ...]
    rotation_quarter_turns: int
    frame_id: str | None
    frame_version: int | None
    fov_size: tuple[float, float] | None
    objective_name: str
    optical_calibration_identity: str
    xyb_ready: bool
    z_ready: bool
    a_ready: bool


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
    operator_alignment: bool = False
    mark_index: int | None = None
    configured_target_xy: MachinePoint | None = None
    capture_source: str | None = None
    capture_allowed: bool = True
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
    mark_index: int | None = None


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
    captured_objective_xy_offset: MachinePoint
    stage_xy: StagePoint | None
    mark_index: int | None = None


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
    focus_candidate: FocusCandidate | None = None
    clear_focus_candidate: bool = False
    commit_z_mm: float | None = None
    commit_a_mm: float | None = None
    focus_token: FocusOperationToken | None = None
    start_autofocus: bool = False
    contact_token: ContactOperationToken | None = None
    capture_contact: bool = False
    operator_alignment: bool = False


@dataclass
class _PendingRegistrationEvidence:
    context: RegistrationCaptureContext
    operation_id: str
    active_token: RegistrationCaptureToken | None
    samples: list[tuple[RegistrationCaptureToken, RegistrationSample]] = field(
        default_factory=list
    )


@dataclass
class _PendingFocus:
    context: RegistrationContext
    token: FocusOperationToken
    target_xy: StagePoint | None = None
    move_completed: bool = False


@dataclass(frozen=True)
class _PendingContact:
    context: RegistrationContext
    token: ContactOperationToken


class DesignRegistrationLifecycle:
    """Own one transactional registration evidence batch at a time."""

    def __init__(self) -> None:
        self._pending: _PendingRegistrationEvidence | None = None
        self._focus_candidate: FocusCandidate | None = None
        self._focus_context: RegistrationContext | None = None
        self._pending_focus: _PendingFocus | None = None
        self._pending_contact: _PendingContact | None = None
        self._accepted_contact_identity: tuple[int, str | None, int | None] | None = None

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
        capture_allowed = not bool(
            context_matches
            and context.operator_alignment
            and pending is not None
            and pending.active_token is not None
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
            operator_alignment=context.operator_alignment,
            mark_index=context.mark_index,
            configured_target_xy=context.configured_target_xy,
            capture_source=context.capture_source,
            capture_allowed=capture_allowed,
            superseded_effects=superseded_effects,
        )
        if not capture_allowed:
            return token
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
                active_token=token,
            )
        else:
            pending.active_token = token
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
            pending.active_token = None
            return RegistrationEffects(
                accepted=True,
                reason=str(sample.message or "Machine coordinates are unavailable."),
            )
        if (
            sample.mark_kind not in {"source", "check"}
            or sample.mark_kind != token.mark_kind
            or sample.mark_index != token.mark_index
        ):
            return RegistrationEffects(reason="Registration mark kind is invalid.")

        if token.mark_index is None:
            pending.samples.append((token, sample))
        else:
            pending.samples = [
                item
                for item in pending.samples
                if item[0].mark_index != token.mark_index
            ]
            pending.samples.append((token, sample))
        pending.active_token = None
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
            operator_alignment=pending.context.operator_alignment,
        )
        if complete:
            self._pending = None
        return effects

    def set_focus_candidate(
        self,
        candidate: FocusCandidate,
        context: RegistrationContext,
    ) -> RegistrationEffects:
        self._focus_candidate = candidate
        self._focus_context = context
        self._pending_focus = None
        return RegistrationEffects(
            accepted=True,
            focus_candidate=candidate,
        )

    def accept_focus(
        self,
        context: RegistrationContext,
        completion: FocusCompletion | None = None,
    ) -> RegistrationEffects:
        if completion is not None:
            return self._accept_focus_completion(context, completion)
        candidate = self._focus_candidate
        if candidate is None:
            return RegistrationEffects(reason="No focus candidate is available.")
        if context != self._focus_context:
            self._focus_candidate = None
            self._focus_context = None
            self._pending_focus = None
            return RegistrationEffects(
                reason="The focus candidate is stale.",
                clear_focus_candidate=True,
            )
        if not context.xyb_ready or context.z_ready:
            return RegistrationEffects(
                reason="Ready X/Y/B and a missing Z reference are required."
            )
        token = FocusOperationToken(
            request_id=str(uuid.uuid4()),
            frame_id=context.frame_id,
            frame_version=context.frame_version,
        )
        self._pending_focus = _PendingFocus(context=context, token=token)
        return RegistrationEffects(
            accepted=True,
            focus_candidate=candidate,
            focus_token=token,
        )

    def _accept_focus_completion(
        self,
        context: RegistrationContext,
        completion: FocusCompletion,
    ) -> RegistrationEffects:
        pending = self._pending_focus
        if pending is None or completion.token != pending.token:
            return RegistrationEffects(reason="The focus operation is stale.")
        if context != pending.context:
            self._pending_focus = None
            self._focus_candidate = None
            self._focus_context = None
            return RegistrationEffects(
                reason="The focus operation context changed.",
                clear_focus_candidate=True,
            )
        if completion.kind is FocusCompletionKind.CANCELLED:
            self._pending_focus = None
            return RegistrationEffects(accepted=True, reason=completion.message)
        if completion.kind is FocusCompletionKind.TARGET_BOUND:
            target = self._finite_point(completion.target_xy)
            if target is None:
                return RegistrationEffects(reason="The focus move target is invalid.")
            pending.target_xy = target
            pending.move_completed = False
            return RegistrationEffects(accepted=True, focus_token=pending.token)
        if completion.kind is FocusCompletionKind.MOVE_FINISHED:
            target = self._finite_point(completion.target_xy)
            if target is None or target != pending.target_xy:
                return RegistrationEffects(reason="The focus move completion is stale.")
            if not completion.succeeded:
                self._pending_focus = None
                return RegistrationEffects(
                    accepted=True,
                    reason=completion.message,
                )
            pending.move_completed = True
            return RegistrationEffects(
                accepted=True,
                focus_token=pending.token,
                start_autofocus=True,
            )
        if not pending.move_completed:
            return RegistrationEffects(reason="The focus move is incomplete.")
        self._pending_focus = None
        if not completion.succeeded or completion.physical_z_mm is None:
            return RegistrationEffects(accepted=True, reason=completion.message)
        physical_z = float(completion.physical_z_mm)
        if not math.isfinite(physical_z):
            return RegistrationEffects(reason="The focus Z coordinate is invalid.")
        self._focus_candidate = None
        self._focus_context = None
        return RegistrationEffects(
            accepted=True,
            clear_focus_candidate=True,
            commit_z_mm=physical_z,
            focus_token=pending.token,
        )

    def accept_first_contact(
        self,
        context: RegistrationContext,
        a_mm: float | None,
        *,
        token: ContactOperationToken | None = None,
    ) -> RegistrationEffects:
        identity = (
            context.session_identity,
            context.frame_id,
            context.frame_version,
        )
        pending = self._pending_contact
        if token is not None and (
            pending is None
            or token != pending.token
            or context != pending.context
        ):
            return RegistrationEffects(reason="The contact operation is stale.")
        if (
            not context.z_ready
            or context.a_ready
            or self._accepted_contact_identity == identity
        ):
            return RegistrationEffects(
                reason="A ready focus reference without contact is required."
            )
        if a_mm is None:
            if token is not None:
                return RegistrationEffects(
                    accepted=True,
                    capture_contact=True,
                    contact_token=token,
                )
            contact_token = ContactOperationToken(
                request_id=str(uuid.uuid4()),
                session_identity=context.session_identity,
                frame_id=context.frame_id,
                frame_version=context.frame_version,
            )
            self._pending_contact = _PendingContact(
                context=context,
                token=contact_token,
            )
            return RegistrationEffects(
                accepted=True,
                capture_contact=True,
                contact_token=contact_token,
            )
        physical_a = float(a_mm)
        if not math.isfinite(physical_a):
            return RegistrationEffects(reason="The contact A coordinate is invalid.")
        self._pending_contact = None
        self._accepted_contact_identity = identity
        return RegistrationEffects(
            accepted=True,
            commit_a_mm=physical_a,
        )

    def cancel(self, reason: RegistrationCancellation) -> RegistrationEffects:
        pending = self._pending
        self._pending = None
        clear_focus_candidate = self._focus_candidate is not None
        self._focus_candidate = None
        self._focus_context = None
        self._pending_focus = None
        self._pending_contact = None
        self._accepted_contact_identity = None
        if pending is None:
            return RegistrationEffects(
                cancellation=reason,
                clear_focus_candidate=clear_focus_candidate,
            )
        return replace(
            self._baseline_effects(pending, cancellation=reason),
            clear_focus_candidate=clear_focus_candidate,
        )

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
            operator_alignment=context.operator_alignment,
        )

    @staticmethod
    def _contexts_match(
        left: RegistrationCaptureContext,
        right: RegistrationCaptureContext,
    ) -> bool:
        shared_matches = bool(
            left.session_identity == right.session_identity
            and left.frame_id == right.frame_id
            and left.frame_version == right.frame_version
            and left.source_identity == right.source_identity
            and left.top_cell_name == right.top_cell_name
            and int(left.rotation_quarter_turns) % 4
            == int(right.rotation_quarter_turns) % 4
            and left.source_design_marks == right.source_design_marks
            and left.check_design_marks == right.check_design_marks
            and left.existing_source_machine_marks
            == right.existing_source_machine_marks
            and left.existing_check_machine_marks
            == right.existing_check_machine_marks
            and left.operator_alignment == right.operator_alignment
        )
        if not shared_matches:
            return False
        if left.operator_alignment:
            return True
        return bool(
            left.pivot_machine_xy == right.pivot_machine_xy
            and left.objective_xy_offset == right.objective_xy_offset
            and left.mark_index == right.mark_index
            and left.configured_target_xy == right.configured_target_xy
            and left.capture_source == right.capture_source
        )

    @staticmethod
    def _token_is_current(
        token: RegistrationCaptureToken,
        pending: _PendingRegistrationEvidence,
    ) -> bool:
        return token == pending.active_token

    @staticmethod
    def _batch_is_complete(pending: _PendingRegistrationEvidence) -> bool:
        context = pending.context
        if context.operator_alignment:
            indices = {
                token.mark_index
                for token, sample in pending.samples
                if sample.mark_kind == "source" and token.mark_index is not None
            }
            return indices == set(range(len(context.source_design_marks)))
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
        ordered_samples = (
            sorted(samples, key=lambda item: int(item[0].mark_index))
            if all(token.mark_index is not None for token, _sample in samples)
            else samples
        )
        reference_b = ordered_samples[0][1].physical_b_deg
        normalized: list[NormalizedRegistrationSample] = []
        for token, sample in ordered_samples:
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
                    captured_objective_xy_offset=token.objective_xy_offset,
                    stage_xy=sample.stage_xy,
                    mark_index=sample.mark_index,
                )
            )
        if any(sample.mark_index is not None for sample in normalized):
            normalized.sort(
                key=lambda sample: (
                    sample.mark_index is None,
                    -1 if sample.mark_index is None else sample.mark_index,
                )
            )
        return tuple(normalized)

    @staticmethod
    def _finite_point(value: object) -> StagePoint | None:
        try:
            point = tuple(float(item) for item in value)  # type: ignore[union-attr]
        except (TypeError, ValueError):
            return None
        if len(point) != 2 or not all(math.isfinite(item) for item in point):
            return None
        return point[0], point[1]


__all__ = [
    "DesignRegistrationLifecycle",
    "ContactOperationToken",
    "FocusCompletion",
    "FocusCompletionKind",
    "FocusOperationToken",
    "NormalizedRegistrationSample",
    "RegistrationCancellation",
    "RegistrationCaptureContext",
    "RegistrationCaptureOutcome",
    "RegistrationCaptureToken",
    "RegistrationContext",
    "RegistrationEffects",
    "RegistrationSample",
]
