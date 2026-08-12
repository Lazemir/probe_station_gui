"""Composition root for private Design registration workflows."""

from __future__ import annotations

from dataclasses import replace

from probe_station_gui.design.registration_lifecycle import (
    DesignRegistrationLifecycle,
    RegistrationCancellation,
    RegistrationEffects,
)
from probe_station_gui.design import session_registration
from probe_station_gui.design.session import DesignSession

from .coordinator_contact import CoordinateContactWorkflow
from .coordinator_activation import CoordinateDesignActivation
from .coordinator_focus import CoordinateFocusWorkflow
from .coordinator_model import (
    FirstContactRequest,
    DesignActivationRequest,
    FocusCandidateRequest,
    FocusReferenceRequest,
    FocusReferenceResetRequest,
    FocusSearchLease,
    RegistrationOpticalObservation,
    OperatorPickRelease,
    RegistrationProjection,
    RegistrationCaptureRequest,
    RegistrationAlignmentRequest,
    RegistrationCheckMarkRequest,
    RegistrationInvalidationRequest,
    RegistrationSourceMarkRequest,
    RegistrationSourceMarksRequest,
    RegistrationWorkflowSnapshot,
    _RegistrationTransitionParts,
)
from .coordinator_registration_capture import RegistrationCaptureWorkflow
from .registry import CoordinateFrameRegistry


class CoordinateRegistrationReducer:
    """Present one deep registration boundary over three private workflows."""

    def __init__(
        self,
        *,
        registry: CoordinateFrameRegistry,
        session: DesignSession,
        lifecycle: DesignRegistrationLifecycle,
    ) -> None:
        self._registry = registry
        self._session = session
        self._lifecycle = lifecycle
        self._next_intent_id = 0
        dependencies = {
            "registry": registry,
            "session": session,
            "lifecycle": lifecycle,
            "allocate_intent_id": self._allocate_intent_id,
            "apply_effects": self._apply_effects,
        }
        self._capture = RegistrationCaptureWorkflow(**dependencies)
        self._focus = CoordinateFocusWorkflow(**dependencies)
        self._contact = CoordinateContactWorkflow(**dependencies)
        self._activation = CoordinateDesignActivation(
            registry=registry,
            session=session,
            allocate_intent_id=self._allocate_intent_id,
        )

    def snapshot(
        self,
        *,
        operator_pick_release: OperatorPickRelease | None = None,
    ) -> RegistrationWorkflowSnapshot:
        frame_id = self._session.active_frame_id
        record = self._registry.get(frame_id) if frame_id is not None else None
        registration = self._session.registration
        projection = (
            None
            if registration is None
            else RegistrationProjection(
                valid=bool(registration.valid),
                status=str(self._session.registration_status),
                source_stage_marks=tuple(registration.source_stage_marks),
                matrix=(
                    (
                        float(registration.matrix[0][0]),
                        float(registration.matrix[0][1]),
                    ),
                    (
                        float(registration.matrix[1][0]),
                        float(registration.matrix[1][1]),
                    ),
                ),
                design_unit_mm=float(registration.design_unit_mm),
            )
        )
        return RegistrationWorkflowSnapshot(
            active_frame_id=None if record is None else frame_id,
            active_frame_version=None if record is None else record.version,
            source_design_marks=tuple(
                session_registration.source_design_marks_compact(self._session)
            ),
            source_stage_marks=tuple(
                session_registration.source_stage_marks_compact(self._session)
            ),
            check_design_marks=tuple(self._session.check_design_marks),
            check_stage_marks=tuple(self._session.check_stage_marks),
            registration_valid=bool(registration is not None and registration.valid),
            registration_status=str(self._session.registration_status),
            registration_projection=projection,
            operator_stage_marks=self._capture.operator_stage_marks,
            alignment_fit_residuals=self._capture.fit_residuals,
            focus_candidate=self._focus.candidate,
            operator_pick_release=operator_pick_release,
            registration_instances=self._activation.instances(
                self._session,
                self._registry,
            ),
            legacy_migration_state=self._activation.legacy_migration_state,
        )

    def observe_notices(self, notices) -> _RegistrationTransitionParts:
        return self._activation.observe_notices(notices)

    def activate(
        self,
        request: DesignActivationRequest,
    ) -> _RegistrationTransitionParts:
        parts = self._activation.activate(request)
        if parts.proposed_session_state is not None or parts.publication is not None:
            cancelled = self._cancel_state(RegistrationCancellation.DESIGN_CHANGED)
            return self._merge_cancellation(
                replace(parts, view_changed=True),
                cancelled,
            )
        return parts

    def close_design(self) -> _RegistrationTransitionParts:
        cancelled = self._cancel_state(RegistrationCancellation.DOCUMENT_UNLOADED)
        return self._merge_cancellation(
            replace(self._activation.close(), view_changed=True),
            cancelled,
        )

    def capture(
        self,
        request: RegistrationCaptureRequest,
    ) -> _RegistrationTransitionParts:
        return self._capture.begin(request)

    def offer_focus_candidate(
        self,
        request: FocusCandidateRequest,
    ) -> _RegistrationTransitionParts:
        return self._focus.offer(request)

    def focus_search_lease(
        self,
        optical: RegistrationOpticalObservation,
    ) -> FocusSearchLease | None:
        return self._focus.search_lease(optical)

    def observe_focus_context(
        self,
        optical: RegistrationOpticalObservation,
    ) -> _RegistrationTransitionParts:
        return self._focus.observe(optical)

    def use_focus_reference(
        self,
        request: FocusReferenceRequest,
    ) -> _RegistrationTransitionParts:
        return self._focus.use(request)

    def reset_focus_reference(
        self,
        request: FocusReferenceResetRequest,
    ) -> _RegistrationTransitionParts:
        parts = self._focus.reset(request)
        if parts.publication is not None:
            return self._merge_cancellation(
                parts,
                self._cancel_state(RegistrationCancellation.Z_CHANGED),
            )
        return parts

    def arm_first_contact(
        self,
        request: FirstContactRequest,
    ) -> _RegistrationTransitionParts:
        cancelled = self._cancel_state(RegistrationCancellation.ROUTE_CONTEXT_CHANGED)
        return self._merge_cancellation(self._contact.arm(request), cancelled)

    def complete(
        self,
        intent_id: int,
        result: object,
    ) -> _RegistrationTransitionParts | None:
        for workflow in (
            self._capture,
            self._focus,
            self._contact,
            self._activation,
        ):
            completed = workflow.complete(intent_id, result)
            if completed is not None:
                return completed
        return None

    def cancel(
        self,
        reason: RegistrationCancellation,
    ) -> _RegistrationTransitionParts:
        return self._cancel_state(reason)

    def set_source_design_mark(
        self,
        request: RegistrationSourceMarkRequest,
    ) -> _RegistrationTransitionParts:
        cancelled = self._cancel_state(RegistrationCancellation.MARK_SET_CHANGED)
        session_registration.clear_source_stage_marks(self._session)
        if request.slot is None:
            session_registration.add_source_design_mark(self._session, request.point)
        else:
            session_registration.set_source_design_mark(
                self._session,
                request.slot,
                request.point,
            )
        return replace(cancelled, view_changed=True)

    def replace_source_design_marks(
        self,
        request: RegistrationSourceMarksRequest,
    ) -> _RegistrationTransitionParts:
        cancelled = self._cancel_state(RegistrationCancellation.MARK_SET_CHANGED)
        self._session.source_design_marks = tuple(request.points)
        session_registration.clear_source_stage_marks(self._session)
        return replace(cancelled, view_changed=True)

    def add_check_design_mark(
        self,
        request: RegistrationCheckMarkRequest,
    ) -> _RegistrationTransitionParts:
        cancelled = self._cancel_state(RegistrationCancellation.MARK_SET_CHANGED)
        session_registration.add_check_design_mark(self._session, request.point)
        return replace(cancelled, view_changed=True)

    def clear_source_stage_marks(self) -> _RegistrationTransitionParts:
        cancelled = self._cancel_state(RegistrationCancellation.MARK_SET_CHANGED)
        session_registration.clear_source_stage_marks(self._session)
        return replace(cancelled, view_changed=True)

    def apply_prepared_alignment(
        self,
        request: RegistrationAlignmentRequest,
    ) -> _RegistrationTransitionParts:
        session_registration.apply_prepared_alignment(
            self._session,
            request.preparation,
        )
        return _RegistrationTransitionParts(view_changed=True)

    def invalidate_registration(
        self,
        request: RegistrationInvalidationRequest,
    ) -> _RegistrationTransitionParts:
        session_registration.invalidate_registration(self._session, request.reason)
        return _RegistrationTransitionParts(view_changed=True)

    def _cancel_state(
        self,
        reason: RegistrationCancellation,
    ) -> _RegistrationTransitionParts:
        effects = self._lifecycle.cancel(reason)
        self._apply_effects(effects)
        cancelled = self._capture.cancel(effects)
        self._focus.clear()
        self._contact.clear()
        return cancelled

    @staticmethod
    def _merge_cancellation(
        parts: _RegistrationTransitionParts,
        cancelled: _RegistrationTransitionParts,
    ) -> _RegistrationTransitionParts:
        return replace(
            parts,
            view_changed=parts.view_changed or cancelled.view_changed,
            ui_effects=(*cancelled.ui_effects, *parts.ui_effects),
        )

    def _apply_effects(self, effects: RegistrationEffects) -> None:
        session = self._session
        if (
            effects.restore_baseline
            and effects.baseline_session_identity == id(session)
            and effects.baseline_frame_id == session.active_frame_id
        ):
            session.source_stage_marks = tuple(effects.baseline_source_stage_marks)
            session.check_stage_marks = list(effects.baseline_check_stage_marks)
            session.registration = effects.baseline_registration
            session.registration_status = str(
                effects.baseline_registration_status or "No design registration."
            )
        sample = effects.captured_sample
        if sample is None or sample.stage_xy is None or effects.operator_alignment:
            return
        if sample.mark_kind == "check":
            session_registration.add_check_stage_mark(session, sample.stage_xy)
        elif sample.mark_index is not None:
            session_registration.set_source_stage_mark(
                session,
                sample.mark_index,
                sample.stage_xy,
            )
        else:
            session_registration.add_source_stage_mark(session, sample.stage_xy)

    def _allocate_intent_id(self) -> int:
        self._next_intent_id -= 1
        return self._next_intent_id


__all__ = ["CoordinateRegistrationReducer"]
