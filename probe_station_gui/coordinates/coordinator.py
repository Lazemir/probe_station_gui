"""Qt-free application boundary for Coordinate Systems."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from .coordinator_model import (
    CoordinateAdapterCompletion,
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
    RegistrationOpticalObservation,
    RewriteLegacyDesignStateIntent,
)
from .coordinator_persistence import (
    CoordinatePersistenceReducer,
    _ContactCommitRollback,
)
from .lifecycle import CoordinateFrameLifecycle
from .registry import CoordinateFrameRegistry

if TYPE_CHECKING:
    from probe_station_gui.design.registration_lifecycle import (
        DesignRegistrationLifecycle,
        RegistrationCancellation,
    )
    from probe_station_gui.design.session import DesignSession


class CoordinateSystemCoordinator:
    """Coordinate-system workflows behind one immutable transition interface."""

    def __init__(
        self,
        *,
        registry: CoordinateFrameRegistry,
        session: DesignSession,
        lifecycle: CoordinateFrameLifecycle | None = None,
        registration_lifecycle: DesignRegistrationLifecycle | None = None,
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

    def start(self, profile: MachineProfileObservation) -> CoordinateTransition:
        if not isinstance(profile, MachineProfileObservation):
            raise TypeError("profile must be a MachineProfileObservation")
        return self._with_registration(self._persistence.start(profile))

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
        return self._merge_registration_parts(
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
        )

    def publish_frame_records(
        self,
        publication: FrameRecordsPublication,
    ) -> CoordinateTransition:
        if not isinstance(publication, FrameRecordsPublication):
            raise TypeError("publication must be a FrameRecordsPublication")
        return self._with_registration(self._persistence.publish(publication))

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
        if not self._persistence.snapshot().frames_loaded:
            self._registration.cancel(RegistrationCancellation.DESIGN_CHANGED)
            self._session.apply_state(request.session_state)
            return self._transition(view_changed=True)
        return self._registration_transition(self._registration.activate(request))

    def close_design(self) -> CoordinateTransition:
        return self._registration_transition(self._registration.close_design())

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

    def snapshot(self) -> CoordinateSystemSnapshot:
        return replace(
            self._persistence.snapshot(),
            registration=self._registration.snapshot(),
        )

    def _transition(
        self,
        *,
        intents=(),
        notices=(),
        view_changed=False,
    ) -> CoordinateTransition:
        return CoordinateTransition(
            snapshot=self.snapshot(),
            intents=tuple(intents),
            notices=tuple(notices),
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

    def _registration_transition(self, parts) -> CoordinateTransition:
        if parts.proposed_session_state is not None:
            self._session.apply_state(parts.proposed_session_state)
        if parts.publication is None:
            registration = self._registration.snapshot(
                operator_pick_release=parts.operator_pick_release
            )
            return CoordinateTransition(
                snapshot=replace(self._persistence.snapshot(), registration=registration),
                intents=parts.intents,
                notices=parts.notices,
                accepted=parts.accepted,
                view_changed=parts.view_changed,
                ui_effects=parts.ui_effects,
            )
        published = self._persistence.publish(parts.publication)
        registration = self._registration.snapshot(
            operator_pick_release=parts.operator_pick_release
        )
        return CoordinateTransition(
            snapshot=replace(
                published.snapshot,
                registration=registration,
            ),
            intents=(*parts.intents, *published.intents),
            notices=(*parts.notices, *published.notices),
            accepted=parts.accepted and published.accepted,
            view_changed=parts.view_changed or published.view_changed,
            ui_effects=(*parts.ui_effects, *published.ui_effects),
        )


__all__ = ["CoordinateSystemCoordinator"]
