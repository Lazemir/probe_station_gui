"""First-contact A-reference workflow for the coordinate-system coordinator."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from probe_station_gui.design.frame_registration import set_contact_reference
from probe_station_gui.design.registration_lifecycle import (
    ContactOperationToken,
    DesignRegistrationLifecycle,
    RegistrationCancellation,
    RegistrationContext,
    RegistrationEffects,
)
from probe_station_gui.design.session import DesignSession

from .coordinator_model import (
    FirstContactRequest,
    FrameRecordsPublication,
    PhysicalAReadResult,
    ReadPhysicalAIntent,
    _RegistrationTransitionParts,
)
from .registry import CoordinateFrameRegistry


@dataclass(frozen=True)
class _PendingContact:
    token: ContactOperationToken
    context: RegistrationContext


class CoordinateContactWorkflow:
    """Own the Z-gated, first-write-wins contact read and publication."""

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
        self._reads: dict[int, _PendingContact] = {}

    def arm(
        self,
        request: FirstContactRequest,
    ) -> _RegistrationTransitionParts:
        context = self._context(request)
        if context is None:
            return _RegistrationTransitionParts()
        effects = self._lifecycle.accept_first_contact(context, None)
        self._apply_effects(effects)
        if not effects.capture_contact or effects.contact_token is None:
            return _RegistrationTransitionParts()
        intent_id = self._allocate_intent_id()
        self._reads[intent_id] = _PendingContact(effects.contact_token, context)
        return _RegistrationTransitionParts(
            intents=(ReadPhysicalAIntent(intent_id),)
        )

    def complete(
        self,
        intent_id: int,
        result: object,
    ) -> _RegistrationTransitionParts | None:
        if not isinstance(result, PhysicalAReadResult):
            return None
        pending = self._reads.pop(intent_id, None)
        if pending is None or result.intent_id != intent_id:
            return _RegistrationTransitionParts()
        if result.interrupted:
            self._cancel()
            return _RegistrationTransitionParts()
        request = FirstContactRequest(
            frame_id=pending.token.frame_id or "",
            frame_version=(
                -1 if pending.token.frame_version is None else pending.token.frame_version
            ),
        )
        context = self._context(request)
        if context is None or not result.succeeded:
            self._cancel()
            return _RegistrationTransitionParts()
        effects = self._lifecycle.accept_first_contact(
            context,
            result.physical_a_mm,
            token=pending.token,
        )
        self._apply_effects(effects)
        if effects.commit_a_mm is None:
            return _RegistrationTransitionParts()
        current = self._registry.get(pending.token.frame_id)
        if current is None or current.version != pending.token.frame_version:
            return _RegistrationTransitionParts()
        try:
            contacted = replace(
                set_contact_reference(
                    current,
                    physical_machine_a_mm=effects.commit_a_mm,
                ),
                version=current.version + 1,
            )
        except (KeyError, RuntimeError, TypeError, ValueError):
            return _RegistrationTransitionParts()
        return _RegistrationTransitionParts(
            publication=FrameRecordsPublication.for_committed_record(
                self._registry.snapshot().records,
                contacted,
                previous_record=current,
                success_message="Contact reference ready.",
                success_duration_ms=5000,
            )
        )

    def clear(self) -> None:
        self._reads.clear()

    def _cancel(self) -> None:
        self._apply_effects(
            self._lifecycle.cancel(RegistrationCancellation.ROUTE_CONTEXT_CHANGED)
        )

    def _context(self, request: FirstContactRequest) -> RegistrationContext | None:
        document = self._session.document
        frame_id = self._session.active_frame_id
        record = self._registry.get(frame_id) if frame_id is not None else None
        if (
            document is None
            or record is None
            or frame_id != request.frame_id
            or record.version != request.frame_version
        ):
            return None
        try:
            source_path = str(Path(document.path).expanduser().resolve())
        except OSError:
            return None
        return RegistrationContext(
            session_identity=id(self._session),
            source_identity=(source_path, str(document.source_load_id)),
            top_cell_name=str(document.top_cell_name),
            visible_layers=tuple(sorted(document.visible_layers)),
            rotation_quarter_turns=int(document.rotation_quarter_turns) % 4,
            frame_id=frame_id,
            frame_version=record.version,
            fov_size=None,
            objective_name="",
            optical_calibration_identity="",
            xyb_ready=all(
                record.readiness[axis].available for axis in ("X", "Y", "B")
            ),
            z_ready=record.readiness["Z"].available,
            a_ready=record.readiness["A"].available,
        )


__all__ = ["CoordinateContactWorkflow"]
