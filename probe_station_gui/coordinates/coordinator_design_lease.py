"""Current Design-coordinate capability and calibrated projection policy."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from probe_station_gui.design import objective_offsets
from probe_station_gui.design.frame_registration import DesignFrameMetadata
from probe_station_gui.design.model import DesignModelError

from .coordinator_model import (
    CoordinateAuthorityObservation,
    CoordinateSystemSnapshot,
    DesignCoordinateLease,
)
from .model import AxisReadiness, CoordinateFrameRecord
from .provenance import design_frame_provenance_error
from .registry import CoordinateFrameRegistry
from .source_identity import source_identity

if TYPE_CHECKING:
    from probe_station_gui.design.session import DesignSession


class DesignCoordinateLeaseReducer:
    """Build and validate one immutable capability for Design projection."""

    def __init__(
        self,
        *,
        registry: CoordinateFrameRegistry,
        session: DesignSession,
    ) -> None:
        self._registry = registry
        self._session = session
        self._last_refresh_key: tuple[object, ...] | None = None

    def lease(
        self,
        snapshot: CoordinateSystemSnapshot,
        authority: CoordinateAuthorityObservation,
    ) -> DesignCoordinateLease:
        session = self._session
        document = session.document
        frame_id = session.active_frame_id
        record = self._registry.get(frame_id) if frame_id is not None else None
        readiness = self._readiness(record)
        blocked_axes = self._authority_blocked_axes(authority)
        provenance_error = (
            None if record is None else design_frame_provenance_error(record)
        )
        metadata = None
        machine_snapshot = None
        pivot = None
        objective = (0.0, 0.0)
        reason = self._lease_reason(
            snapshot,
            record=record,
            frame_id=frame_id,
            provenance_error=provenance_error,
            blocked_axes=blocked_axes,
        )
        if reason is None:
            try:
                assert record is not None and document is not None
                metadata = DesignFrameMetadata.from_mapping(record.metadata)
                if (
                    source_identity(metadata.source_path)
                    != source_identity(document.path)
                    or metadata.top_cell_name != document.top_cell_name
                ):
                    raise DesignModelError(
                        "Design coordinate frame belongs to a different document context."
                    )
                machine_snapshot = authority.machine_snapshot
                if machine_snapshot is None:
                    raise DesignModelError(
                        "A synchronized Machine-coordinate snapshot is unavailable."
                    )
                machine_snapshot.physical_machine_pose.require("B")
                if authority.pivot_machine_xy is None:
                    raise DesignModelError("Rotation pivot is unavailable.")
                pivot = self._finite_point(
                    authority.pivot_machine_xy,
                    "Rotation pivot is invalid.",
                )
                objective = self._finite_point(
                    authority.objective_xy_offset,
                    "Active objective offset is invalid.",
                )
            except Exception as exc:
                reason = str(exc) or type(exc).__name__
        return DesignCoordinateLease(
            load_complete=bool(snapshot.frames_loaded),
            frame_id=frame_id,
            frame_version=None if record is None else int(record.version),
            transform=None if record is None else record.transform,
            readiness=readiness,
            provenance_error=provenance_error,
            authority_blocked_axes=blocked_axes,
            rejection_reason=reason,
            document=document,
            metadata=metadata,
            machine_coordinate_snapshot=machine_snapshot,
            pivot_machine_xy=pivot,
            objective_xy_offset=objective,
            fingerprint=self._fingerprint(snapshot, record, authority),
        )

    def is_current(
        self,
        snapshot: CoordinateSystemSnapshot,
        authority: CoordinateAuthorityObservation,
        lease: DesignCoordinateLease,
    ) -> bool:
        current = self.lease(snapshot, authority)
        return bool(current.usable and current.fingerprint == lease.fingerprint)

    def project_design_to_raw_stage(
        self,
        snapshot: CoordinateSystemSnapshot,
        authority: CoordinateAuthorityObservation,
        lease: DesignCoordinateLease,
        design_xy: tuple[float, float],
    ) -> tuple[float, float] | None:
        if not self.is_current(snapshot, authority, lease):
            return None
        if (
            lease.transform is None
            or lease.document is None
            or lease.metadata is None
            or lease.machine_coordinate_snapshot is None
            or lease.pivot_machine_xy is None
        ):
            return None
        turns = int(lease.document.rotation_quarter_turns) % 4
        canonical = lease.document.rotate_point(
            self._finite_point(design_xy, "Design point is invalid."),
            -turns,
        )
        machine_snapshot = lease.machine_coordinate_snapshot
        physical_xy = lease.transform.frame_xy_to_machine(
            (
                canonical[0] * lease.metadata.design_unit_mm,
                canonical[1] * lease.metadata.design_unit_mm,
            ),
            machine_b_deg=machine_snapshot.physical_machine_pose.require("B"),
            pivot_machine_xy=lease.pivot_machine_xy,
        )
        return (
            machine_snapshot.physical_machine_to_configured_controller(
                "X", physical_xy[0]
            ),
            machine_snapshot.physical_machine_to_configured_controller(
                "Y", physical_xy[1]
            ),
        )

    def project_design_to_camera_stage(
        self,
        snapshot: CoordinateSystemSnapshot,
        authority: CoordinateAuthorityObservation,
        lease: DesignCoordinateLease,
        design_xy: tuple[float, float],
    ) -> tuple[float, float] | None:
        raw_xy = self.project_design_to_raw_stage(
            snapshot,
            authority,
            lease,
            design_xy,
        )
        if raw_xy is None:
            return None
        return objective_offsets.raw_stage_to_camera_stage(
            raw_xy,
            lease.objective_xy_offset,
        )

    def project_raw_stage_to_design(
        self,
        snapshot: CoordinateSystemSnapshot,
        authority: CoordinateAuthorityObservation,
        lease: DesignCoordinateLease,
        raw_xy: tuple[float, float],
    ) -> tuple[float, float] | None:
        if not self.is_current(snapshot, authority, lease):
            return None
        if (
            lease.transform is None
            or lease.document is None
            or lease.metadata is None
            or lease.machine_coordinate_snapshot is None
            or lease.pivot_machine_xy is None
        ):
            return None
        machine_snapshot = lease.machine_coordinate_snapshot
        raw = self._finite_point(raw_xy, "Stage point is invalid.")
        physical_xy = (
            machine_snapshot.configured_controller_to_physical_machine("X", raw[0]),
            machine_snapshot.configured_controller_to_physical_machine("Y", raw[1]),
        )
        canonical_mm = lease.transform.machine_xy_to_frame(
            physical_xy,
            machine_b_deg=machine_snapshot.physical_machine_pose.require("B"),
            pivot_machine_xy=lease.pivot_machine_xy,
        )
        canonical = (
            canonical_mm[0] / lease.metadata.design_unit_mm,
            canonical_mm[1] / lease.metadata.design_unit_mm,
        )
        return lease.document.rotate_point(
            canonical,
            int(lease.document.rotation_quarter_turns) % 4,
        )

    def refresh_active_link(
        self,
        snapshot: CoordinateSystemSnapshot,
        authority: CoordinateAuthorityObservation,
    ) -> None:
        lease = self.lease(snapshot, authority)
        refresh_key = (
            lease.fingerprint,
            self._registration_state_key(),
        )
        if refresh_key == self._last_refresh_key:
            return
        if lease.frame_id is None:
            self._last_refresh_key = refresh_key
            return
        record = self._registry.get(lease.frame_id)
        if record is None or not lease.usable:
            self._session.invalidate_registration(
                lease.reason or "Design coordinate frame is unavailable."
            )
            self._remember_refresh(lease)
            return
        assert lease.machine_coordinate_snapshot is not None
        assert lease.pivot_machine_xy is not None
        machine_snapshot = lease.machine_coordinate_snapshot

        def project_machine(point: tuple[float, float]) -> tuple[float, float]:
            raw = (
                machine_snapshot.physical_machine_to_configured_controller(
                    "X", point[0]
                ),
                machine_snapshot.physical_machine_to_configured_controller(
                    "Y", point[1]
                ),
            )
            return objective_offsets.raw_stage_to_camera_stage(
                raw,
                lease.objective_xy_offset,
            )

        try:
            self._session.link_active_frame(
                record,
                machine_point_for_navigation=project_machine,
                machine_b_deg=machine_snapshot.physical_machine_pose.require("B"),
                pivot_machine_xy=lease.pivot_machine_xy,
            )
        except (DesignModelError, KeyError, TypeError, ValueError) as exc:
            self._session.invalidate_registration(str(exc) or type(exc).__name__)
        self._remember_refresh(lease)

    def _lease_reason(
        self,
        snapshot: CoordinateSystemSnapshot,
        *,
        record: CoordinateFrameRecord | None,
        frame_id: str | None,
        provenance_error: str | None,
        blocked_axes: frozenset[str],
    ) -> str | None:
        if not snapshot.frames_loaded:
            return "Design coordinate provenance is being checked."
        if frame_id is None or record is None:
            return "A durable Design coordinate frame is required."
        semantic = record.semantic_validation_error()
        if semantic is not None:
            return semantic
        if provenance_error is not None:
            return provenance_error
        if self._session.document is None:
            return "Load a design before using Design coordinates."
        if record.transform is None:
            return "Design coordinate transform is unavailable."
        for axis in ("X", "Y", "B"):
            state = record.readiness.get(axis)
            if state is None:
                return "Design X/Y/B readiness is unavailable."
            if not state.available:
                return state.reason or "Design X/Y/B registration is required."
        if {"X", "Y", "B"}.intersection(blocked_axes):
            return "Controller coordinate authority is unavailable."
        return None

    @classmethod
    def _authority_blocked_axes(
        cls,
        authority: CoordinateAuthorityObservation,
    ) -> frozenset[str]:
        blocked = {
            axis for axis in ("X", "Y") if axis not in authority.homed_axes
        }
        blocked.update(
            axis
            for axis in ("X", "Y", "B")
            if axis not in authority.physical_pose.values
        )
        if authority.machine_snapshot is None:
            blocked.update(("X", "Y", "B"))
        if cls.pivot_is_unavailable(authority):
            blocked.update(("X", "Y", "B"))
        return frozenset(blocked)

    def _fingerprint(
        self,
        snapshot: CoordinateSystemSnapshot,
        record: CoordinateFrameRecord | None,
        authority: CoordinateAuthorityObservation,
    ) -> tuple[object, ...]:
        document = self._session.document
        machine_snapshot = authority.machine_snapshot
        document_key = (
            None
            if document is None
            else (
                id(document),
                source_identity(document.path),
                document.source_load_id,
                document.top_cell_name,
                int(document.rotation_quarter_turns),
                tuple(sorted(document.visible_layers)),
                float(document.dbu),
                tuple(float(value) for value in document.bounds),
            )
        )
        record_key = (
            None
            if record is None
            else (
                record.frame_id,
                int(record.version),
                record.transform,
                tuple(
                    (axis, record.readiness[axis])
                    for axis in sorted(record.readiness)
                ),
                tuple(
                    sorted(
                        (str(key), repr(value))
                        for key, value in record.metadata.items()
                    )
                ),
            )
        )
        snapshot_key = (
            None
            if machine_snapshot is None
            else (
                id(machine_snapshot.mapper),
                machine_snapshot.raw_machine_position,
                machine_snapshot.configured_position,
                machine_snapshot.work_offset,
                machine_snapshot.coordinate_system,
                machine_snapshot.position_reporting_mode,
                tuple(sorted(machine_snapshot.axis_index.items())),
                tuple(
                    sorted(machine_snapshot.physical_machine_pose.values.items())
                ),
            )
        )
        return (
            bool(snapshot.frames_loaded),
            self._session.active_frame_id,
            document_key,
            record_key,
            tuple(sorted(authority.homed_axes)),
            tuple(sorted(authority.physical_pose.values.items())),
            snapshot_key,
            authority.pivot_machine_xy,
            authority.objective_xy_offset,
            authority.pivot_error,
            authority.pivot_error_permanent,
        )

    def _remember_refresh(self, lease: DesignCoordinateLease) -> None:
        self._last_refresh_key = (
            lease.fingerprint,
            self._registration_state_key(),
        )

    def _registration_state_key(self) -> tuple[object, ...]:
        registration = self._session.registration
        registration_key = (
            None
            if registration is None
            else (
                bool(registration.valid),
                tuple(tuple(float(value) for value in row) for row in registration.matrix),
                tuple(tuple(float(value) for value in point) for point in registration.source_stage_marks),
                float(registration.design_unit_mm),
            )
        )
        return (
            self._session.active_frame_id,
            str(self._session.registration_status),
            tuple(self._session.source_design_marks),
            tuple(self._session.source_stage_marks),
            tuple(self._session.check_design_marks),
            tuple(self._session.check_stage_marks),
            registration_key,
        )

    @staticmethod
    def pivot_is_unavailable(authority: CoordinateAuthorityObservation) -> bool:
        pivot = authority.pivot_machine_xy
        if pivot is None:
            return True
        try:
            point = tuple(float(value) for value in pivot)
        except (TypeError, ValueError):
            return True
        return len(point) != 2 or not all(math.isfinite(value) for value in point)

    @staticmethod
    def _readiness(
        record: CoordinateFrameRecord | None,
    ) -> tuple[tuple[str, AxisReadiness], ...]:
        if record is None:
            return ()
        return tuple(
            (axis, record.readiness[axis])
            for axis in ("X", "Y", "Z", "A", "B")
            if axis in record.readiness
        )

    @staticmethod
    def _finite_point(
        value: tuple[float, float],
        message: str,
    ) -> tuple[float, float]:
        try:
            point = (float(value[0]), float(value[1]))
        except (IndexError, TypeError, ValueError) as exc:
            raise DesignModelError(message) from exc
        if not all(math.isfinite(component) for component in point):
            raise DesignModelError(message)
        return point


__all__ = ["DesignCoordinateLeaseReducer"]
