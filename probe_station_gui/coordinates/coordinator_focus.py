"""Focus-reference workflow for the coordinate-system coordinator."""

from __future__ import annotations

from dataclasses import dataclass, replace
import math
from typing import Callable

from probe_station_gui.design import objective_offsets, session_registration
from probe_station_gui.design.focus_candidate import FocusCandidate
from probe_station_gui.design.frame_registration import (
    DesignFrameMetadata,
    reset_focus_reference,
    set_focus_reference,
)
from probe_station_gui.design.model import DesignModelError
from probe_station_gui.design.registration_lifecycle import (
    DesignRegistrationLifecycle,
    FocusCompletion,
    FocusCompletionKind,
    FocusOperationToken,
    RegistrationCancellation,
    RegistrationContext,
    RegistrationEffects,
)
from probe_station_gui.design.session import DesignSession

from .coordinator_model import (
    AutofocusResult,
    CoordinateNotice,
    DesignSessionCheckpoint,
    FocusCandidateRequest,
    FocusMoveResult,
    FocusReferenceRequest,
    FocusReferenceResetRequest,
    FocusSearchLease,
    FrameRecordsPublication,
    MoveToFocusTargetIntent,
    RegistrationOpticalObservation,
    RunAutofocusIntent,
    _RegistrationTransitionParts,
)
from .registry import CoordinateFrameRegistry
from .source_identity import source_identity


@dataclass(frozen=True)
class _PendingFocus:
    token: FocusOperationToken
    context: RegistrationContext
    request: FocusReferenceRequest
    target_xy: tuple[float, float]


class CoordinateFocusWorkflow:
    """Own focus candidate identity and the move/autofocus completion chain."""

    def __init__(
        self,
        *,
        registry: CoordinateFrameRegistry,
        session: DesignSession,
        lifecycle: DesignRegistrationLifecycle,
        allocate_intent_id: Callable[[], int],
        apply_effects: Callable[[RegistrationEffects], None],
    ) -> None:
        self._registry = registry
        self._session = session
        self._lifecycle = lifecycle
        self._allocate_intent_id = allocate_intent_id
        self._apply_effects = apply_effects
        self._candidate: FocusCandidate | None = None
        self._observed_context: RegistrationContext | None = None
        self._moves: dict[int, _PendingFocus] = {}
        self._autofocus: dict[int, _PendingFocus] = {}

    @property
    def candidate(self) -> FocusCandidate | None:
        return self._candidate

    def offer(
        self,
        request: FocusCandidateRequest,
    ) -> _RegistrationTransitionParts:
        if not all(
            hasattr(request.candidate, attribute)
            for attribute in ("center", "bounds", "distance_from_design_center")
        ):
            raise TypeError("candidate must be a FocusCandidate")
        context = self._context(request.optical)
        if context is None:
            return _RegistrationTransitionParts(
                notices=(self._warning("Design coordinate frame is unavailable."),)
            )
        if request.lease is not None and request.lease.context != context:
            return _RegistrationTransitionParts(
                notices=(self._warning("The focus search result is stale."),)
            )
        self.observe_effects(
            self._lifecycle.set_focus_candidate(request.candidate, context)
        )
        self._observed_context = context
        return _RegistrationTransitionParts()

    def observe(
        self,
        optical: RegistrationOpticalObservation,
    ) -> _RegistrationTransitionParts:
        context = self._context(optical)
        if context == self._observed_context:
            return _RegistrationTransitionParts()
        self._observed_context = context
        effects = self._lifecycle.observe_focus_context(context)
        self.observe_effects(effects)
        if effects.clear_focus_candidate:
            self._moves.clear()
            self._autofocus.clear()
        return _RegistrationTransitionParts()

    def search_lease(
        self,
        optical: RegistrationOpticalObservation,
    ) -> FocusSearchLease | None:
        context = self._context(optical)
        return None if context is None else FocusSearchLease(context)

    def use(
        self,
        request: FocusReferenceRequest,
    ) -> _RegistrationTransitionParts:
        context = self._context(request.optical)
        if context is None:
            self.observe_effects(
                self._lifecycle.cancel(RegistrationCancellation.DESIGN_CHANGED)
            )
            return _RegistrationTransitionParts(
                notices=(
                    self._warning("Find a new focus reference for the current view."),
                )
            )
        effects = self._lifecycle.accept_focus(context)
        self.observe_effects(effects)
        if not effects.accepted or effects.focus_token is None:
            return _RegistrationTransitionParts(
                notices=(
                    self._warning(
                        effects.reason
                        or "Find a new focus reference for the current view."
                    ),
                )
            )
        try:
            target = self._move_target(request, effects.focus_token, context)
        except (
            AttributeError,
            DesignModelError,
            KeyError,
            RuntimeError,
            TypeError,
            ValueError,
        ) as exc:
            self._cancel(context, effects.focus_token)
            return _RegistrationTransitionParts(
                notices=(self._warning(str(exc) or "Design registration is required."),)
            )
        bound = self._lifecycle.accept_focus(
            context,
            FocusCompletion(
                kind=FocusCompletionKind.TARGET_BOUND,
                token=effects.focus_token,
                target_xy=target,
            ),
        )
        self.observe_effects(bound)
        if not bound.accepted:
            return _RegistrationTransitionParts(
                notices=(self._warning(bound.reason or "Focus move is unavailable."),)
            )
        intent_id = self._allocate_intent_id()
        self._moves[intent_id] = _PendingFocus(
            effects.focus_token,
            context,
            request,
            target,
        )
        return _RegistrationTransitionParts(
            intents=(MoveToFocusTargetIntent(intent_id, target),)
        )

    def complete(
        self,
        intent_id: int,
        result: object,
    ) -> _RegistrationTransitionParts | None:
        if isinstance(result, FocusMoveResult):
            return self._complete_move(intent_id, result)
        if isinstance(result, AutofocusResult):
            return self._complete_autofocus(intent_id, result)
        return None

    def reset(
        self,
        request: FocusReferenceResetRequest,
    ) -> _RegistrationTransitionParts:
        frame_id = self._session.active_frame_id
        current = self._registry.get(frame_id) if frame_id is not None else None
        if current is None:
            return _RegistrationTransitionParts(
                notices=(self._warning("Design coordinate frame is unavailable."),)
            )
        checkpoint = DesignSessionCheckpoint.capture(self._session)
        try:
            reset = replace(
                reset_focus_reference(current, reason=request.reason),
                version=current.version + 1,
            )
            physical_b = request.machine_snapshot.physical_machine_pose.require("B")
            projection = session_registration.prepare_active_frame_link(
                self._session,
                reset,
                machine_point_for_navigation=self._machine_mapper(request),
                machine_b_deg=physical_b,
                pivot_machine_xy=request.pivot_machine_xy,
            )
        except (DesignModelError, KeyError, RuntimeError, TypeError, ValueError) as exc:
            return _RegistrationTransitionParts(notices=(self._warning(str(exc)),))
        return _RegistrationTransitionParts(
            publication=FrameRecordsPublication.for_committed_record(
                self._registry.snapshot().records,
                reset,
                previous_record=current,
                previous_session=checkpoint,
                projection=projection,
                success_message="Focus reference reset.",
                success_duration_ms=4000,
            )
        )

    def observe_effects(self, effects: RegistrationEffects) -> None:
        self._apply_effects(effects)
        if effects.clear_focus_candidate:
            self._candidate = None
        elif effects.focus_candidate is not None:
            self._candidate = effects.focus_candidate

    def clear(self) -> None:
        self._moves.clear()
        self._autofocus.clear()
        self._candidate = None
        self._observed_context = None

    def _complete_move(
        self,
        intent_id: int,
        result: FocusMoveResult,
    ) -> _RegistrationTransitionParts:
        pending = self._moves.pop(intent_id, None)
        if pending is None or result.intent_id != intent_id:
            return _RegistrationTransitionParts()
        context = self._context(pending.request.optical)
        if context is None:
            self.observe_effects(
                self._lifecycle.cancel(RegistrationCancellation.FRAME_CHANGED)
            )
            return _RegistrationTransitionParts()
        effects = self._lifecycle.accept_focus(
            context,
            FocusCompletion(
                kind=FocusCompletionKind.MOVE_FINISHED,
                token=pending.token,
                target_xy=(
                    pending.target_xy
                    if result.completed_target_xy is None and not result.succeeded
                    else result.completed_target_xy
                ),
                succeeded=bool(result.succeeded),
                message=str(result.message or ""),
            ),
        )
        self.observe_effects(effects)
        if not effects.accepted:
            return _RegistrationTransitionParts()
        if not effects.start_autofocus or effects.focus_token is None:
            notices = () if not effects.reason else (self._warning(effects.reason),)
            return _RegistrationTransitionParts(notices=notices)
        autofocus_id = self._allocate_intent_id()
        self._autofocus[autofocus_id] = pending
        return _RegistrationTransitionParts(intents=(RunAutofocusIntent(autofocus_id),))

    def _complete_autofocus(
        self,
        intent_id: int,
        result: AutofocusResult,
    ) -> _RegistrationTransitionParts:
        pending = self._autofocus.pop(intent_id, None)
        if pending is None or result.intent_id != intent_id:
            return _RegistrationTransitionParts()
        context = self._context(pending.request.optical)
        if context is None:
            self.observe_effects(
                self._lifecycle.cancel(RegistrationCancellation.FRAME_CHANGED)
            )
            return _RegistrationTransitionParts()
        checkpoint = DesignSessionCheckpoint.capture(self._session)
        effects = self._lifecycle.accept_focus(
            context,
            FocusCompletion(
                kind=FocusCompletionKind.AUTOFOCUS_FINISHED,
                token=pending.token,
                succeeded=bool(result.succeeded),
                physical_z_mm=result.physical_z_mm,
                message=str(result.message or ""),
            ),
        )
        self.observe_effects(effects)
        if effects.commit_z_mm is None or effects.focus_token is None:
            notices = () if not effects.reason else (self._warning(effects.reason),)
            return _RegistrationTransitionParts(notices=notices)
        current = self._registry.get(effects.focus_token.frame_id)
        if current is None or current.version != effects.focus_token.frame_version:
            return _RegistrationTransitionParts()
        try:
            focused = replace(
                set_focus_reference(current, physical_machine_z_mm=effects.commit_z_mm),
                version=current.version + 1,
            )
            physical_b = pending.request.machine_snapshot.physical_machine_pose.require(
                "B"
            )
            projection = session_registration.prepare_active_frame_link(
                self._session,
                focused,
                machine_point_for_navigation=self._machine_mapper(pending.request),
                machine_b_deg=physical_b,
                pivot_machine_xy=pending.request.pivot_machine_xy,
            )
        except (DesignModelError, KeyError, RuntimeError, TypeError, ValueError) as exc:
            return _RegistrationTransitionParts(notices=(self._warning(str(exc)),))
        return _RegistrationTransitionParts(
            publication=FrameRecordsPublication.for_committed_record(
                self._registry.snapshot().records,
                focused,
                previous_record=current,
                previous_session=checkpoint,
                projection=projection,
                success_message="Focus reference ready.",
                success_duration_ms=5000,
            )
        )

    def _context(
        self,
        optical: RegistrationOpticalObservation,
    ) -> RegistrationContext | None:
        document = self._session.document
        frame_id = self._session.active_frame_id
        record = self._registry.get(frame_id) if frame_id is not None else None
        if document is None:
            return None
        try:
            source_path = source_identity(document.path)
        except OSError:
            return None
        return RegistrationContext(
            session_identity=id(self._session),
            source_identity=(source_path, str(document.source_load_id)),
            top_cell_name=str(document.top_cell_name),
            visible_layers=tuple(sorted(document.visible_layers)),
            rotation_quarter_turns=int(document.rotation_quarter_turns) % 4,
            frame_id=frame_id,
            frame_version=None if record is None else record.version,
            fov_size=tuple(float(value) for value in optical.fov_size),
            objective_name=str(optical.objective_name),
            optical_calibration_identity=str(optical.optical_calibration_identity),
            xyb_ready=bool(
                record is not None
                and all(record.readiness[axis].available for axis in ("X", "Y", "B"))
            ),
            z_ready=bool(record is not None and record.readiness["Z"].available),
            a_ready=bool(record is not None and record.readiness["A"].available),
        )

    def _cancel(self, context: RegistrationContext, token: FocusOperationToken) -> None:
        self.observe_effects(
            self._lifecycle.accept_focus(
                context,
                FocusCompletion(kind=FocusCompletionKind.CANCELLED, token=token),
            )
        )

    def _move_target(
        self,
        request: FocusReferenceRequest,
        token: FocusOperationToken,
        context: RegistrationContext,
    ) -> tuple[float, float]:
        document = self._session.document
        record = self._registry.get(token.frame_id)
        if (
            document is None
            or record is None
            or record.version != token.frame_version
            or self._session.active_frame_id != token.frame_id
            or context.frame_id != token.frame_id
            or context.frame_version != token.frame_version
            or record.transform is None
        ):
            raise DesignModelError("Design registration is required.")
        metadata = DesignFrameMetadata.from_mapping(record.metadata)
        turns = int(document.rotation_quarter_turns) % 4
        canonical = document.rotate_point(request.design_point, -turns)
        physical_b = request.machine_snapshot.physical_machine_pose.require("B")
        physical_xy = record.transform.frame_xy_to_machine(
            (
                float(canonical[0]) * metadata.design_unit_mm,
                float(canonical[1]) * metadata.design_unit_mm,
            ),
            machine_b_deg=physical_b,
            pivot_machine_xy=request.pivot_machine_xy,
        )
        target = (
            float(
                request.machine_snapshot.physical_machine_to_configured_controller(
                    "X", physical_xy[0]
                )
            ),
            float(
                request.machine_snapshot.physical_machine_to_configured_controller(
                    "Y", physical_xy[1]
                )
            ),
        )
        if not all(math.isfinite(value) for value in target):
            raise DesignModelError("The focus move target is invalid.")
        return target

    @staticmethod
    def _machine_mapper(
        request: FocusReferenceRequest | FocusReferenceResetRequest,
    ):
        def map_point(machine_xy):
            configured = (
                request.machine_snapshot.physical_machine_to_configured_controller(
                    "X", machine_xy[0]
                ),
                request.machine_snapshot.physical_machine_to_configured_controller(
                    "Y", machine_xy[1]
                ),
            )
            return objective_offsets.raw_stage_to_camera_stage(
                configured,
                request.objective_xy_offset,
            )

        return map_point

    @staticmethod
    def _warning(message: str) -> CoordinateNotice:
        return CoordinateNotice(str(message), severity="warning", duration_ms=6000)


__all__ = ["CoordinateFocusWorkflow"]
