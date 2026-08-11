"""Qt-free application boundary for Coordinate Systems."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING

from .coordinator_model import (
    CoordinateAdapterCompletion,
    CoordinateAuthorityObservation,
    CoordinateMotionProjection,
    CoordinateMotionRequest,
    CoordinateSystemSelection,
    CustomSystemsRequest,
    DesignCalibrationObservation,
    DesignCoordinateLease,
    CoordinateSystemSnapshot,
    CoordinateTransition,
    FrameRecordsPublication,
    DesignActivationRequest,
    FocusCandidateRequest,
    FocusReferenceRequest,
    FocusReferenceResetRequest,
    FocusSearchLease,
    FirstContactRequest,
    MachineProfileObservation,
    RegistrationCaptureRequest,
    RegistrationAlignmentRequest,
    RegistrationCheckMarkRequest,
    RegistrationInvalidationRequest,
    RegistrationSourceMarkRequest,
    RegistrationSourceMarksRequest,
    RegistrationOpticalObservation,
    RewriteLegacyDesignStateIntent,
)
from .coordinator_persistence import (
    CoordinatePersistenceReducer,
    _ContactCommitRollback,
)
from .lifecycle import CoordinateFrameLifecycle
from .registry import CoordinateFrameRegistry


def _optional_int(value: object) -> int | None:
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None

if TYPE_CHECKING:
    from probe_station_gui.design.registration_lifecycle import (
        DesignRegistrationLifecycle,
        RegistrationCancellation,
    )
    from probe_station_gui.design.session import DesignSession
    from probe_station_gui.design.session_state import DesignSessionState


class CoordinateSystemCoordinator:
    """Coordinate-system workflows behind one immutable transition interface."""

    def __init__(
        self,
        *,
        restore_frame_id: str | None = None,
    ) -> None:
        from probe_station_gui.design.session import DesignSession

        self._initialize(
            registry=CoordinateFrameRegistry(),
            session=DesignSession(),
            lifecycle=CoordinateFrameLifecycle(),
            registration_lifecycle=None,
            restore_frame_id=restore_frame_id,
        )

    @classmethod
    def _adopt_session(
        cls,
        *,
        session: DesignSession,
        restore_frame_id: str | None = None,
    ) -> CoordinateSystemCoordinator:
        """Adopt the application's one non-coordinate Design state owner."""

        coordinator = cls.__new__(cls)
        coordinator._initialize(
            registry=CoordinateFrameRegistry(),
            session=session,
            lifecycle=CoordinateFrameLifecycle(),
            registration_lifecycle=None,
            restore_frame_id=restore_frame_id,
        )
        return coordinator

    @classmethod
    def _for_testing(
        cls,
        *,
        registry: CoordinateFrameRegistry,
        session: DesignSession,
        lifecycle: CoordinateFrameLifecycle | None = None,
        registration_lifecycle: DesignRegistrationLifecycle | None = None,
        restore_frame_id: str | None = None,
    ) -> CoordinateSystemCoordinator:
        coordinator = cls.__new__(cls)
        coordinator._initialize(
            registry=registry,
            session=session,
            lifecycle=lifecycle or CoordinateFrameLifecycle(),
            registration_lifecycle=registration_lifecycle,
            restore_frame_id=restore_frame_id,
        )
        return coordinator

    def _initialize(
        self,
        *,
        registry: CoordinateFrameRegistry,
        session: DesignSession,
        lifecycle: CoordinateFrameLifecycle,
        registration_lifecycle: DesignRegistrationLifecycle | None,
        restore_frame_id: str | None,
    ) -> None:
        from probe_station_gui.design.registration_lifecycle import (
            DesignRegistrationLifecycle,
        )

        from .coordinator_registration import CoordinateRegistrationReducer

        self._registration_lifecycle = registration_lifecycle or DesignRegistrationLifecycle()
        self._session = session
        self._persistence = CoordinatePersistenceReducer(
            registry=registry,
            session=session,
            lifecycle=lifecycle,
        )
        self._registration = CoordinateRegistrationReducer(
            registry=registry,
            session=session,
            lifecycle=self._registration_lifecycle,
        )
        from .coordinator_selection import CoordinateSelectionReducer

        self._selection = CoordinateSelectionReducer(
            registry=registry,
            session=session,
            restore_frame_id=restore_frame_id,
        )

    def start(self, profile: MachineProfileObservation) -> CoordinateTransition:
        if not isinstance(profile, MachineProfileObservation):
            raise TypeError("profile must be a MachineProfileObservation")
        transition = self._with_registration(self._persistence.start(profile))
        return self._merge_selection_parts(
            transition,
            self._selection.reconcile(self._persistence.snapshot()),
        )

    def complete(
        self,
        completion: CoordinateAdapterCompletion,
    ) -> CoordinateTransition:
        if not isinstance(completion, CoordinateAdapterCompletion):
            raise TypeError("completion must be a CoordinateAdapterCompletion")
        registration = self._registration.complete(
            int(completion.intent_id),
            completion.result,
        )
        if registration is not None:
            return self._registration_transition(registration)
        transition, publication_outcomes = self._persistence.complete(completion)
        selection = self._selection.reconcile(transition.snapshot)
        registration = self._registration.observe_notices(transition.notices)
        legacy_notice_consumed = any(
            isinstance(intent, RewriteLegacyDesignStateIntent)
            for intent in registration.intents
        )
        lifecycle = self._registration_lifecycle
        if lifecycle is not None:
            for outcome in publication_outcomes:
                if isinstance(outcome, _ContactCommitRollback):
                    lifecycle._release_contact_commit(
                        session_identity=outcome.session_identity,
                        frame_id=outcome.frame_id,
                        frame_version=outcome.frame_version,
                    )
        return self._merge_selection_parts(self._merge_registration_parts(
            replace(
                transition,
                notices=tuple(
                    notice
                    for notice in transition.notices
                    if not (
                        legacy_notice_consumed
                        and str(notice.code or "").startswith(
                            "legacy_migration_saved:"
                        )
                    )
                ),
            ),
            registration,
        ), selection)

    def _publish_frame_records(
        self,
        publication: FrameRecordsPublication,
    ) -> CoordinateTransition:
        if not isinstance(publication, FrameRecordsPublication):
            raise TypeError("publication must be a FrameRecordsPublication")
        transition = self._with_registration(self._persistence.publish(publication))
        return self._merge_selection_parts(
            transition,
            self._selection.reconcile(self._persistence.snapshot()),
        )

    def capture_registration_mark(
        self,
        request: RegistrationCaptureRequest,
    ) -> CoordinateTransition:
        if not isinstance(request, RegistrationCaptureRequest):
            raise TypeError("request must be a RegistrationCaptureRequest")
        parts = self._registration.capture(request)
        return self._registration_transition(parts)

    def activate_design(
        self,
        request: DesignActivationRequest,
    ) -> CoordinateTransition:
        from probe_station_gui.design.registration_lifecycle import (
            RegistrationCancellation,
        )

        if not isinstance(request, DesignActivationRequest):
            raise TypeError("request must be a DesignActivationRequest")
        if request.session_state is None:
            request = replace(
                request,
                session_state=self._session.snapshot_state(),
            )
        if not self._persistence.snapshot().frames_loaded:
            self._registration.cancel(RegistrationCancellation.DESIGN_CHANGED)
            self._session.apply_state(request.session_state)
            return self._transition(view_changed=True)
        return self._registration_transition(self._registration.activate(request))

    def close_design(self) -> CoordinateTransition:
        return self._registration_transition(self._registration.close_design())

    def controller_persistence_state(
        self,
        workspace_state: DesignSessionState,
    ) -> dict[str, object] | None:
        """Return a detached workspace/coordinate persistence projection."""

        from probe_station_gui.design.session_state import (
            DesignSessionState,
            export_persisted_session_state,
        )

        if not isinstance(workspace_state, DesignSessionState):
            raise TypeError("workspace_state must be a DesignSessionState")
        coordinate_state = self._session.snapshot_state()
        merged_state = replace(
            coordinate_state,
            document=workspace_state.document,
            targets=workspace_state.targets,
            selected_target_index=workspace_state.selected_target_index,
            route=workspace_state.route,
            selected_route_point_index=(
                workspace_state.selected_route_point_index
            ),
        )
        record = next(
            (
                candidate
                for candidate in self._persistence.snapshot().records
                if candidate.frame_id == merged_state.active_frame_id
            ),
            None,
        )
        metadata = {} if record is None else dict(record.metadata)
        route = merged_state.route
        route_path = None if route is None or route.path is None else str(route.path)
        return deepcopy(
            export_persisted_session_state(
                merged_state,
                document_size=_optional_int(metadata.get("source_size")),
                document_mtime_ns=_optional_int(
                    metadata.get("source_mtime_ns")
                ),
                route_path=route_path,
            )
        )

    def close(self) -> CoordinateTransition:
        """Discard open coordinate workflows during application shutdown."""

        return self.close_design()

    def cancel_registration(
        self,
        reason: RegistrationCancellation,
    ) -> CoordinateTransition:
        from probe_station_gui.design.registration_lifecycle import (
            RegistrationCancellation,
        )

        if not isinstance(reason, RegistrationCancellation):
            raise TypeError("reason must be a RegistrationCancellation")
        parts = self._registration.cancel(reason)
        return self._registration_transition(parts)

    def set_registration_source_mark(
        self,
        request: RegistrationSourceMarkRequest,
    ) -> CoordinateTransition:
        if not isinstance(request, RegistrationSourceMarkRequest):
            raise TypeError("request must be a RegistrationSourceMarkRequest")
        return self._registration_transition(
            self._registration.set_source_design_mark(request)
        )

    def replace_registration_source_marks(
        self,
        request: RegistrationSourceMarksRequest,
    ) -> CoordinateTransition:
        if not isinstance(request, RegistrationSourceMarksRequest):
            raise TypeError("request must be a RegistrationSourceMarksRequest")
        return self._registration_transition(
            self._registration.replace_source_design_marks(request)
        )

    def add_registration_check_mark(
        self,
        request: RegistrationCheckMarkRequest,
    ) -> CoordinateTransition:
        if not isinstance(request, RegistrationCheckMarkRequest):
            raise TypeError("request must be a RegistrationCheckMarkRequest")
        return self._registration_transition(
            self._registration.add_check_design_mark(request)
        )

    def clear_registration_source_stage_marks(self) -> CoordinateTransition:
        return self._registration_transition(
            self._registration.clear_source_stage_marks()
        )

    def apply_registration_alignment(
        self,
        request: RegistrationAlignmentRequest,
    ) -> CoordinateTransition:
        if not isinstance(request, RegistrationAlignmentRequest):
            raise TypeError("request must be a RegistrationAlignmentRequest")
        return self._registration_transition(
            self._registration.apply_prepared_alignment(request)
        )

    def invalidate_registration(
        self,
        request: RegistrationInvalidationRequest,
    ) -> CoordinateTransition:
        if not isinstance(request, RegistrationInvalidationRequest):
            raise TypeError("request must be a RegistrationInvalidationRequest")
        return self._registration_transition(
            self._registration.invalidate_registration(request)
        )

    def offer_focus_candidate(
        self,
        request: FocusCandidateRequest,
    ) -> CoordinateTransition:
        if not isinstance(request, FocusCandidateRequest):
            raise TypeError("request must be a FocusCandidateRequest")
        return self._registration_transition(
            self._registration.offer_focus_candidate(request)
        )

    def focus_search_lease(
        self,
        optical: RegistrationOpticalObservation,
    ) -> FocusSearchLease | None:
        if not isinstance(optical, RegistrationOpticalObservation):
            raise TypeError("optical must be a RegistrationOpticalObservation")
        return self._registration.focus_search_lease(optical)

    def observe_focus_context(
        self,
        optical: RegistrationOpticalObservation,
    ) -> CoordinateTransition:
        if not isinstance(optical, RegistrationOpticalObservation):
            raise TypeError("optical must be a RegistrationOpticalObservation")
        return self._registration_transition(
            self._registration.observe_focus_context(optical)
        )

    def use_focus_reference(
        self,
        request: FocusReferenceRequest,
    ) -> CoordinateTransition:
        if not isinstance(request, FocusReferenceRequest):
            raise TypeError("request must be a FocusReferenceRequest")
        return self._registration_transition(
            self._registration.use_focus_reference(request)
        )

    def reset_focus_reference(
        self,
        request: FocusReferenceResetRequest,
    ) -> CoordinateTransition:
        if not isinstance(request, FocusReferenceResetRequest):
            raise TypeError("request must be a FocusReferenceResetRequest")
        return self._registration_transition(
            self._registration.reset_focus_reference(request)
        )

    def arm_first_contact(
        self,
        request: FirstContactRequest,
    ) -> CoordinateTransition:
        if not isinstance(request, FirstContactRequest):
            raise TypeError("request must be a FirstContactRequest")
        return self._registration_transition(
            self._registration.arm_first_contact(request)
        )

    def select_system(
        self,
        request: CoordinateSystemSelection,
    ) -> CoordinateTransition:
        if not isinstance(request, CoordinateSystemSelection):
            raise TypeError("request must be a CoordinateSystemSelection")
        parts = self._selection.select(request, self._persistence.snapshot())
        return self._transition(
            intents=parts.intents,
            notices=parts.notices,
            accepted=parts.accepted,
        )

    def observe_authority(
        self,
        observation: CoordinateAuthorityObservation,
    ) -> CoordinateTransition:
        if not isinstance(observation, CoordinateAuthorityObservation):
            raise TypeError("observation must be a CoordinateAuthorityObservation")
        registration_before = self._registration.snapshot()
        parts = self._selection.observe(observation, self._persistence.snapshot())
        return self._transition(
            intents=parts.intents,
            notices=parts.notices,
            view_changed=(registration_before != self._registration.snapshot()),
        )

    def project_motion(
        self,
        request: CoordinateMotionRequest,
    ) -> CoordinateMotionProjection:
        if not isinstance(request, CoordinateMotionRequest):
            raise TypeError("request must be a CoordinateMotionRequest")
        return self._selection.project_motion(self._persistence.snapshot(), request)

    def synchronize_custom_systems(
        self,
        request: CustomSystemsRequest,
    ) -> CoordinateTransition:
        if not isinstance(request, CustomSystemsRequest):
            raise TypeError("request must be a CustomSystemsRequest")
        parts = self._selection.synchronize_custom_systems(
            request,
            self._persistence.snapshot(),
        )
        return self._transition(intents=parts.intents)

    def observe_design_calibrations(
        self,
        observation: DesignCalibrationObservation,
    ) -> CoordinateTransition:
        if not isinstance(observation, DesignCalibrationObservation):
            raise TypeError("observation must be a DesignCalibrationObservation")
        parts, changed = self._selection.observe_calibrations(
            observation,
            self._persistence.snapshot(),
        )
        if not changed or not self._persistence.snapshot().frames_loaded:
            return self._transition(
                intents=parts.intents,
                notices=parts.notices,
                view_changed=changed,
            )
        published = self._publish_frame_records(
            FrameRecordsPublication(records=self.snapshot().records)
        )
        return replace(
            published,
            intents=(*parts.intents, *published.intents),
            notices=(*parts.notices, *published.notices),
            view_changed=True,
        )

    def snapshot(self) -> CoordinateSystemSnapshot:
        snapshot = replace(
            self._persistence.snapshot(),
            registration=self._registration.snapshot(),
        )
        return self._selection.snapshot(snapshot)

    def current_design_lease(self) -> DesignCoordinateLease:
        return self._selection.design_lease(self._persistence.snapshot())

    def design_lease_is_current(self, lease: DesignCoordinateLease) -> bool:
        if not isinstance(lease, DesignCoordinateLease):
            raise TypeError("lease must be a DesignCoordinateLease")
        return self._selection.design_lease_is_current(
            self._persistence.snapshot(),
            lease,
        )

    def project_design_to_raw_stage(
        self,
        lease: DesignCoordinateLease,
        design_xy: tuple[float, float],
    ) -> tuple[float, float] | None:
        if not isinstance(lease, DesignCoordinateLease):
            raise TypeError("lease must be a DesignCoordinateLease")
        return self._selection.project_design_to_raw_stage(
            self._persistence.snapshot(),
            lease,
            design_xy,
        )

    def project_design_to_camera_stage(
        self,
        lease: DesignCoordinateLease,
        design_xy: tuple[float, float],
    ) -> tuple[float, float] | None:
        if not isinstance(lease, DesignCoordinateLease):
            raise TypeError("lease must be a DesignCoordinateLease")
        return self._selection.project_design_to_camera_stage(
            self._persistence.snapshot(),
            lease,
            design_xy,
        )

    def project_raw_stage_to_design(
        self,
        lease: DesignCoordinateLease,
        raw_xy: tuple[float, float],
    ) -> tuple[float, float] | None:
        if not isinstance(lease, DesignCoordinateLease):
            raise TypeError("lease must be a DesignCoordinateLease")
        return self._selection.project_raw_stage_to_design(
            self._persistence.snapshot(),
            lease,
            raw_xy,
        )

    def _transition(
        self,
        *,
        intents=(),
        notices=(),
        accepted=True,
        view_changed=False,
    ) -> CoordinateTransition:
        return CoordinateTransition(
            snapshot=self.snapshot(),
            intents=tuple(intents),
            notices=tuple(notices),
            accepted=bool(accepted),
            view_changed=bool(view_changed),
        )

    def _with_registration(
        self,
        transition: CoordinateTransition,
    ) -> CoordinateTransition:
        return replace(transition, snapshot=self.snapshot())

    def _merge_registration_parts(
        self,
        transition: CoordinateTransition,
        parts,
    ) -> CoordinateTransition:
        return replace(
            transition,
            snapshot=self.snapshot(),
            intents=(*transition.intents, *parts.intents),
            notices=(*transition.notices, *parts.notices),
            accepted=transition.accepted and parts.accepted,
            view_changed=transition.view_changed or parts.view_changed,
            ui_effects=(*transition.ui_effects, *parts.ui_effects),
        )

    def _merge_selection_parts(self, transition, parts) -> CoordinateTransition:
        return replace(
            transition,
            snapshot=self.snapshot(),
            intents=(*transition.intents, *parts.intents),
            notices=(*transition.notices, *parts.notices),
            accepted=transition.accepted and parts.accepted,
        )

    def _registration_transition(self, parts) -> CoordinateTransition:
        if parts.proposed_session_state is not None:
            self._session.apply_state(parts.proposed_session_state)
        if parts.publication is None:
            registration = self._registration.snapshot(
                operator_pick_release=parts.operator_pick_release
            )
            return CoordinateTransition(
                snapshot=self._selection.snapshot(
                    replace(self._persistence.snapshot(), registration=registration)
                ),
                intents=parts.intents,
                notices=parts.notices,
                accepted=parts.accepted,
                view_changed=parts.view_changed,
                ui_effects=parts.ui_effects,
            )
        published = self._persistence.publish(parts.publication)
        selection = self._selection.reconcile(published.snapshot)
        registration = self._registration.snapshot(
            operator_pick_release=parts.operator_pick_release
        )
        return CoordinateTransition(
            snapshot=self._selection.snapshot(
                replace(
                    published.snapshot,
                    registration=registration,
                )
            ),
            intents=(*parts.intents, *published.intents, *selection.intents),
            notices=(*parts.notices, *published.notices, *selection.notices),
            accepted=parts.accepted and published.accepted and selection.accepted,
            view_changed=parts.view_changed or published.view_changed,
            ui_effects=(*parts.ui_effects, *published.ui_effects),
        )


__all__ = ["CoordinateSystemCoordinator"]
