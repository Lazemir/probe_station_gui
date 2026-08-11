"""Selection, authority, Design leases, and projection for Coordinate Systems."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from .coordinator_design_lease import DesignCoordinateLeaseReducer
from .coordinator_model import (
    CoordinateAuthorityObservation,
    CoordinateNotice,
    CoordinateMotionProjection,
    CoordinateMotionRequest,
    CoordinateSystemSelection,
    CoordinateSystemSnapshot,
    CustomSystemsRequest,
    DesignCalibrationObservation,
    DesignCoordinateLease,
    PersistCoordinateSelectionIntent,
)
from .coordinator_motion import (
    build_coordinate_motion_lease,
    project_coordinate_motion,
)
from .lifecycle import MACHINE_FRAME_ID
from .model import CoordinateFrameRecord, PhysicalMachinePose
from .presentation import (
    CoordinateSelectorEntry,
    build_coordinate_display_plan,
)
from .provenance import design_frame_provenance_error
from .registry import CoordinateFrameRegistry
from .software_frames import materialize_custom_frames
from .design_calibration import reconcile_design_calibration_fingerprints

if TYPE_CHECKING:
    from probe_station_gui.design.session import DesignSession


@dataclass(frozen=True)
class _SelectionDecision:
    selected_frame_id: str
    available: bool
    reason: str | None
    persist_selection: bool = False


@dataclass(frozen=True)
class _SelectionParts:
    intents: tuple[PersistCoordinateSelectionIntent, ...] = ()
    notices: tuple[CoordinateNotice, ...] = ()
    accepted: bool = True


class CoordinateSelectionReducer:
    """Own one GUI selection intent and current Design projection authority."""

    def __init__(
        self,
        *,
        registry: CoordinateFrameRegistry,
        session: DesignSession,
        restore_frame_id: str | None = None,
    ) -> None:
        self._registry = registry
        self._custom_settings = None
        self._calibration_fingerprints = None
        self._design = DesignCoordinateLeaseReducer(
            registry=registry,
            session=session,
        )
        restore = str(restore_frame_id or MACHINE_FRAME_ID).strip()
        self._restore_frame_id = None if restore == MACHINE_FRAME_ID else restore
        self._selected_frame_id = MACHINE_FRAME_ID
        self._authority = CoordinateAuthorityObservation(
            physical_pose=PhysicalMachinePose({}),
            homed_axes=frozenset(),
            machine_snapshot=None,
            pivot_machine_xy=(0.0, 0.0),
        )
        self._decision = _SelectionDecision(MACHINE_FRAME_ID, True, None)

    def select(
        self,
        request: CoordinateSystemSelection,
        snapshot: CoordinateSystemSnapshot,
    ) -> _SelectionParts:
        previous = self._selected_frame_id
        previous_decision = self._decision
        decision = self._decide(
            snapshot,
            requested_frame_id=request.frame_id,
            explicit=True,
        )
        accepted = decision.available and (
            request.frame_id == MACHINE_FRAME_ID
            or decision.selected_frame_id == request.frame_id
        )
        if accepted:
            self._selected_frame_id = decision.selected_frame_id
            self._restore_frame_id = None
            self._decision = decision
        else:
            self._selected_frame_id = previous
            self._decision = previous_decision
        return _SelectionParts(
            intents=self._persistence_intents(decision if accepted else None),
            notices=(
                ()
                if accepted
                else (
                    CoordinateNotice(
                        decision.reason or "Coordinate System is unavailable.",
                        severity="warning",
                        duration_ms=5000,
                        code="coordinate_selection_rejected",
                    ),
                )
            ),
            accepted=accepted,
        )

    def observe(
        self,
        observation: CoordinateAuthorityObservation,
        snapshot: CoordinateSystemSnapshot,
    ) -> _SelectionParts:
        self._authority = observation
        parts = self.reconcile(snapshot)
        self._design.refresh_active_link(snapshot, observation)
        return parts

    def reconcile(self, snapshot: CoordinateSystemSnapshot) -> _SelectionParts:
        records = tuple(snapshot.records)
        notices: tuple[CoordinateNotice, ...] = ()
        if self._calibration_fingerprints is not None:
            records, _changed = reconcile_design_calibration_fingerprints(
                records,
                self._calibration_fingerprints,
            )
        if self._custom_settings is not None:
            try:
                records = materialize_custom_frames(
                    records,
                    self._custom_settings,
                )
            except ValueError as exc:
                self._custom_settings = None
                notices = (
                    CoordinateNotice(
                        str(exc),
                        severity="warning",
                        duration_ms=6000,
                        code="custom_coordinate_overlay_rejected",
                    ),
                )
        if records != self._registry.snapshot().records:
            self._registry.reset(records)
        snapshot = replace(snapshot, records=records)
        decision = self._decide(snapshot, requested_frame_id=None, explicit=False)
        self._selected_frame_id = decision.selected_frame_id
        self._decision = decision
        return _SelectionParts(
            intents=self._persistence_intents(decision),
            notices=notices,
        )

    def synchronize_custom_systems(
        self,
        request: CustomSystemsRequest,
        snapshot: CoordinateSystemSnapshot,
    ) -> _SelectionParts:
        records = materialize_custom_frames(snapshot.records, request.settings)
        self._custom_settings = request.settings
        return self.reconcile(replace(snapshot, records=records))

    def observe_calibrations(
        self,
        observation: DesignCalibrationObservation,
        snapshot: CoordinateSystemSnapshot,
    ) -> tuple[_SelectionParts, bool]:
        records, changed = reconcile_design_calibration_fingerprints(
            snapshot.records,
            observation.fingerprints,
        )
        self._calibration_fingerprints = observation.fingerprints
        return self.reconcile(replace(snapshot, records=records)), changed

    def snapshot(self, snapshot: CoordinateSystemSnapshot) -> CoordinateSystemSnapshot:
        registry = self._registry.snapshot()
        snapshot = replace(snapshot, records=registry.records)
        authority = self._authority
        entries = (
            CoordinateSelectorEntry(
                frame_id=MACHINE_FRAME_ID,
                name="Machine",
                group="Machine",
                enabled=True,
            ),
            *(
                self._selector_entry(record, snapshot)
                for record in self._registry.snapshot().records
            ),
        )
        plan = build_coordinate_display_plan(
            registry,
            selected_frame_id=self._decision.selected_frame_id,
            physical_pose=authority.physical_pose,
            pivot_machine_xy=authority.pivot_machine_xy or (float("nan"),) * 2,
            homed_axes=authority.homed_axes,
            authority_axes=authority.physical_pose.values,
            selection_available=self._decision.available,
            selection_reason=self._decision.reason,
            selector_entries=entries,
        )
        motion_lease = build_coordinate_motion_lease(
            registry,
            selected_frame_id=self._decision.selected_frame_id,
            selection_available=self._decision.available,
            selection_reason=self._decision.reason,
            authority=authority,
            display_plan=plan,
        )
        return replace(
            snapshot,
            selected_frame_id=self._decision.selected_frame_id,
            display_plan=plan,
            design_lease=self._design.lease(snapshot, authority),
            motion_lease=motion_lease,
        )

    def project_motion(
        self,
        snapshot: CoordinateSystemSnapshot,
        request: CoordinateMotionRequest,
    ) -> CoordinateMotionProjection:
        current = self.snapshot(snapshot).motion_lease
        assert current is not None
        return project_coordinate_motion(current, request)

    def design_lease(self, snapshot: CoordinateSystemSnapshot) -> DesignCoordinateLease:
        return self._design.lease(snapshot, self._authority)

    def design_lease_is_current(
        self,
        snapshot: CoordinateSystemSnapshot,
        lease: DesignCoordinateLease,
    ) -> bool:
        return self._design.is_current(snapshot, self._authority, lease)

    def project_design_to_raw_stage(
        self,
        snapshot: CoordinateSystemSnapshot,
        lease: DesignCoordinateLease,
        design_xy: tuple[float, float],
    ) -> tuple[float, float] | None:
        return self._design.project_design_to_raw_stage(
            snapshot,
            self._authority,
            lease,
            design_xy,
        )

    def project_design_to_camera_stage(
        self,
        snapshot: CoordinateSystemSnapshot,
        lease: DesignCoordinateLease,
        design_xy: tuple[float, float],
    ) -> tuple[float, float] | None:
        return self._design.project_design_to_camera_stage(
            snapshot,
            self._authority,
            lease,
            design_xy,
        )

    def project_raw_stage_to_design(
        self,
        snapshot: CoordinateSystemSnapshot,
        lease: DesignCoordinateLease,
        raw_xy: tuple[float, float],
    ) -> tuple[float, float] | None:
        return self._design.project_raw_stage_to_design(
            snapshot,
            self._authority,
            lease,
            raw_xy,
        )

    def _decide(
        self,
        snapshot: CoordinateSystemSnapshot,
        *,
        requested_frame_id: str | None,
        explicit: bool,
    ) -> _SelectionDecision:
        restoring = not explicit and self._restore_frame_id is not None
        requested = str(
            requested_frame_id
            if explicit
            else self._restore_frame_id or self._selected_frame_id
        )
        if requested == MACHINE_FRAME_ID:
            return _SelectionDecision(
                MACHINE_FRAME_ID,
                True,
                None,
                persist_selection=explicit,
            )
        if not snapshot.frames_loaded:
            if explicit:
                return _SelectionDecision(
                    self._selected_frame_id,
                    False,
                    "Coordinate frames are loading.",
                )
            return _SelectionDecision(
                self._selected_frame_id,
                self._selected_frame_id == MACHINE_FRAME_ID,
                None if restoring else "Coordinate frames are loading.",
            )
        record = self._registry.get(requested)
        permanent_reason = (
            "Coordinate frame is unavailable."
            if record is None
            else record.semantic_validation_error()
        )
        pivot_invalid = self._authority.pivot_error_permanent
        if permanent_reason is None and pivot_invalid:
            permanent_reason = (
                self._authority.pivot_error or "Rotation pivot is invalid."
            )
        if permanent_reason is not None:
            if explicit and requested != self._selected_frame_id:
                return _SelectionDecision(
                    self._selected_frame_id,
                    False,
                    permanent_reason,
                )
            persist = restoring or self._selected_frame_id != MACHINE_FRAME_ID
            self._restore_frame_id = None
            return _SelectionDecision(
                MACHINE_FRAME_ID,
                True,
                None,
                persist_selection=persist,
            )
        assert record is not None
        reason = self._temporary_reason(record)
        if restoring and reason is not None:
            self._restore_frame_id = None
            return _SelectionDecision(requested, False, reason)
        if explicit and reason is not None:
            return _SelectionDecision(self._selected_frame_id, False, reason)
        if restoring:
            self._restore_frame_id = None
        return _SelectionDecision(
            requested,
            reason is None,
            reason,
            persist_selection=explicit,
        )

    def _temporary_reason(self, record: CoordinateFrameRecord) -> str | None:
        provenance = design_frame_provenance_error(record)
        if provenance is not None:
            return provenance
        authority = self._authority
        if not {"X", "Y"}.issubset(authority.homed_axes):
            return "Home X and Y to use this coordinate system."
        missing_xy = {"X", "Y"} - set(authority.physical_pose.values)
        if missing_xy:
            return "Physical Machine X/Y coordinate authority is unavailable."
        if "B" not in authority.physical_pose.values:
            return "B coordinate is unavailable."
        if self._design.pivot_is_unavailable(authority):
            return authority.pivot_error or "Rotation pivot is unavailable."
        return None

    def _selector_entry(
        self,
        record: CoordinateFrameRecord,
        snapshot: CoordinateSystemSnapshot,
    ) -> CoordinateSelectorEntry:
        permanent = record.semantic_validation_error()
        reason = permanent or self._temporary_reason(record)
        group = "Designs" if record.kind.value == "design" else "Custom"
        return CoordinateSelectorEntry(
            frame_id=record.frame_id,
            name=record.name,
            group=group,
            enabled=bool(snapshot.frames_loaded and reason is None),
            reason=reason or ("Coordinate frames are loading." if not snapshot.frames_loaded else ""),
        )

    @staticmethod
    def _persistence_intents(
        decision: _SelectionDecision | None,
    ) -> tuple[PersistCoordinateSelectionIntent, ...]:
        if decision is None or not decision.persist_selection:
            return ()
        return (PersistCoordinateSelectionIntent(decision.selected_frame_id),)


__all__ = ["CoordinateSelectionReducer"]
