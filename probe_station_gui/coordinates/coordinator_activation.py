"""Design-session activation behind the coordinate coordinator boundary."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Callable, Mapping

from probe_station_gui.design import objective_offsets
from probe_station_gui.design.frame_registration import (
    DesignFrameMetadata,
    find_equivalent_migrated_frame,
    migrate_legacy_design_state,
)
from probe_station_gui.design.model import DesignModelError
from probe_station_gui.design.navigation_adapter import (
    activate_design_frame_for_document,
)
from probe_station_gui.design.session import DesignSession

from .coordinator_model import (
    CoordinateNotice,
    DesignActivationRequest,
    DesignSessionCheckpoint,
    FrameRecordsPublication,
    LegacyDesignStateRewriteResult,
    RewriteLegacyDesignStateIntent,
    _RegistrationTransitionParts,
)
from .registry import CoordinateFrameRegistry


@dataclass(frozen=True)
class _LegacyMigrationLease:
    session_identity: int
    source_path: str
    source_load_id: str
    top_cell_name: str
    visible_layers: tuple[tuple[int, int], ...]
    rotation_quarter_turns: int
    frame_id: str
    minimum_frame_version: int
    success_notice_code: str


class CoordinateDesignActivation:
    """Prepare activation off-owner, then return one atomic adoption proposal."""

    def __init__(
        self,
        *,
        registry: CoordinateFrameRegistry,
        session: DesignSession,
        allocate_intent_id: Callable[[], int],
    ) -> None:
        self._registry = registry
        self._session = session
        self._allocate_intent_id = allocate_intent_id
        self._legacy_migration_state: dict[str, object] | None = None
        self._legacy_rewrite_intent_id: int | None = None
        self._legacy_rewrite_failures = 0
        self._legacy_migration_lease: _LegacyMigrationLease | None = None
        self._next_legacy_migration_generation = 0

    @property
    def legacy_migration_state(self) -> dict[str, object] | None:
        if not self._legacy_migration_is_current(self._session):
            return None
        return deepcopy(self._legacy_migration_state)

    def activate(
        self,
        request: DesignActivationRequest,
    ) -> _RegistrationTransitionParts:
        candidate = DesignSession()
        try:
            candidate.apply_state(request.session_state)
        except (TypeError, ValueError) as exc:
            return self._failure(exc)
        document = candidate.document
        if document is None:
            self._discard_legacy_migration_unless_current(candidate)
            return _RegistrationTransitionParts(
                proposed_session_state=candidate.snapshot_state()
            )
        proposed_registry = CoordinateFrameRegistry()
        proposed_registry.reset(self._registry.snapshot().records)
        mapper = self._machine_mapper(request)
        physical_b = self._physical_b(request.machine_snapshot)
        pivot = request.pivot_machine_xy or (0.0, 0.0)
        legacy = self._legacy_publication(
            candidate,
            proposed_registry,
            request,
            physical_b=physical_b,
            pivot=pivot,
            mapper=mapper,
        )
        if legacy is not None:
            return legacy
        try:
            metadata = request.frame_metadata
            if metadata is not None and not isinstance(
                metadata,
                DesignFrameMetadata,
            ):
                raise TypeError("frame_metadata must be DesignFrameMetadata")
            create_new = request.create_new or self._should_replace_registered(
                candidate,
                proposed_registry,
                request,
            )
            activation = activate_design_frame_for_document(
                candidate,
                proposed_registry,
                document,
                requested_frame_id=request.requested_frame_id,
                create_new=create_new,
                current_metadata=metadata,
                machine_point_for_navigation=mapper,
                machine_b_deg=physical_b,
                pivot_machine_xy=pivot,
            )
            runtime_record = self._runtime_record(
                activation.record,
                request,
                physical_b=physical_b,
            )
            projection = candidate.prepare_active_frame_link(
                runtime_record,
                machine_point_for_navigation=mapper,
                machine_b_deg=physical_b,
                pivot_machine_xy=pivot,
            )
            candidate.apply_active_frame_link(runtime_record, projection)
        except (DesignModelError, KeyError, OSError, TypeError, ValueError) as exc:
            return self._failure(exc)
        candidate_state = candidate.snapshot_state()
        if not activation.created and not activation.updated:
            self._discard_legacy_migration_unless_current(candidate)
            return _RegistrationTransitionParts(
                proposed_session_state=candidate_state
            )
        previous = self._registry.get(activation.record.frame_id)
        publication = FrameRecordsPublication.for_committed_record(
            self._registry.snapshot().records,
            activation.record,
            previous_record=previous,
            previous_session=DesignSessionCheckpoint.capture(self._session),
            projection=projection,
            runtime_record=runtime_record,
        )
        self._discard_legacy_migration_unless_current(candidate)
        return _RegistrationTransitionParts(
            publication=publication,
            proposed_session_state=candidate_state,
        )

    def close(self) -> _RegistrationTransitionParts:
        candidate = DesignSession()
        candidate.apply_state(self._session.snapshot_state())
        candidate.unload_document()
        self._discard_legacy_migration()
        return _RegistrationTransitionParts(
            proposed_session_state=candidate.snapshot_state()
        )

    def observe_notices(
        self,
        notices: tuple[CoordinateNotice, ...],
    ) -> _RegistrationTransitionParts:
        lease = self._legacy_migration_lease
        if lease is None or not any(
            notice.code == lease.success_notice_code for notice in notices
        ):
            return _RegistrationTransitionParts()
        if not self._legacy_migration_is_current(self._session):
            self._discard_legacy_migration()
            return _RegistrationTransitionParts()
        if (
            self._legacy_migration_state is None
            or self._legacy_rewrite_intent_id is not None
        ):
            return _RegistrationTransitionParts()
        return self._rewrite_intent()

    def complete(
        self,
        intent_id: int,
        result: object,
    ) -> _RegistrationTransitionParts | None:
        if not isinstance(result, LegacyDesignStateRewriteResult):
            return None
        if (
            result.intent_id != intent_id
            or self._legacy_rewrite_intent_id != intent_id
        ):
            return _RegistrationTransitionParts()
        if not self._legacy_migration_is_current(self._session):
            self._discard_legacy_migration()
            return _RegistrationTransitionParts()
        self._legacy_rewrite_intent_id = None
        if result.succeeded:
            self._discard_legacy_migration()
            return _RegistrationTransitionParts(
                notices=(
                    CoordinateNotice("", code="legacy_migration_completed"),
                )
            )
        self._legacy_rewrite_failures += 1
        if self._legacy_rewrite_failures == 1:
            return self._rewrite_intent()
        return _RegistrationTransitionParts(
            notices=(
                CoordinateNotice(
                    "Design registration migration could not be finalized.",
                    severity="warning",
                    duration_ms=6000,
                    code="legacy_migration_rewrite_failed",
                ),
            )
        )

    def _rewrite_intent(self) -> _RegistrationTransitionParts:
        if not self._legacy_migration_is_current(self._session):
            self._discard_legacy_migration()
            return _RegistrationTransitionParts()
        state = self._current_legacy_replacement_state()
        if state is None:
            return _RegistrationTransitionParts(
                notices=(
                    CoordinateNotice(
                        "Design registration migration could not be finalized.",
                        severity="warning",
                        duration_ms=6000,
                        code="legacy_migration_rewrite_unavailable",
                    ),
                )
            )
        intent_id = self._allocate_intent_id()
        self._legacy_rewrite_intent_id = intent_id
        return _RegistrationTransitionParts(
            intents=(
                RewriteLegacyDesignStateIntent(
                    intent_id,
                    state,
                ),
            )
        )

    def _current_legacy_replacement_state(self) -> dict[str, object] | None:
        lease = self._legacy_migration_lease
        if lease is None:
            return None
        try:
            state = self._session.export_persisted_state()
        except (OSError, TypeError, ValueError):
            return None
        if not isinstance(state, dict):
            return None
        if (
            state.get("version") != 3
            or str(state.get("active_frame_id") or "") != lease.frame_id
        ):
            return None
        return deepcopy(state)

    def _legacy_publication(
        self,
        candidate: DesignSession,
        proposed_registry: CoordinateFrameRegistry,
        request: DesignActivationRequest,
        *,
        physical_b: float | None,
        pivot: tuple[float, float],
        mapper,
    ) -> _RegistrationTransitionParts | None:
        legacy_state = candidate.export_persisted_state()
        if not (
            candidate.active_frame_id is None
            and isinstance(legacy_state, dict)
            and len(candidate.source_design_marks_compact()) >= 2
            and len(candidate.source_stage_marks_compact()) >= 2
        ):
            return None
        if physical_b is None or request.machine_snapshot is None:
            candidate.block_legacy_registration_until_b(
                "Design registration requires a current B position."
            )
            return _RegistrationTransitionParts(
                proposed_session_state=candidate.snapshot_state()
            )
        try:
            converted = self._convert_legacy_state(
                legacy_state,
                candidate,
                request,
            )
            metadata = request.frame_metadata
            if metadata is not None and not isinstance(
                metadata,
                DesignFrameMetadata,
            ):
                raise TypeError("frame_metadata must be DesignFrameMetadata")
            migrated = migrate_legacy_design_state(
                converted,
                design_document=candidate.document,
                physical_b_deg=physical_b,
                pivot_machine_xy=pivot,
                existing_names=(
                    record.name for record in proposed_registry.snapshot().records
                ),
                metadata=metadata,
            )
            if migrated is None:
                return None
            existing = find_equivalent_migrated_frame(
                proposed_registry.snapshot().records,
                migrated,
            )
            selected = existing or migrated
            runtime_record = self._runtime_record(
                selected,
                request,
                physical_b=physical_b,
            )
            projection = candidate.prepare_active_frame_link(
                runtime_record,
                machine_point_for_navigation=mapper,
                machine_b_deg=physical_b,
                pivot_machine_xy=pivot,
            )
            candidate.apply_active_frame_link(runtime_record, projection)
        except (DesignModelError, KeyError, OSError, TypeError, ValueError) as exc:
            return self._failure(exc)
        replacement_state = candidate.export_persisted_state()
        if replacement_state is None:
            return self._failure(
                DesignModelError("Migrated Design state is unavailable.")
            )
        self._next_legacy_migration_generation += 1
        success_notice_code = (
            "legacy_migration_saved:"
            f"{self._next_legacy_migration_generation}"
        )
        try:
            migration_lease = self._legacy_lease(
                candidate,
                selected,
                success_notice_code=success_notice_code,
            )
        except (DesignModelError, OSError, TypeError, ValueError) as exc:
            return self._failure(exc)
        self._legacy_migration_state = deepcopy(dict(legacy_state))
        self._legacy_rewrite_intent_id = None
        self._legacy_rewrite_failures = 0
        self._legacy_migration_lease = migration_lease
        return _RegistrationTransitionParts(
            proposed_session_state=candidate.snapshot_state(),
            publication=FrameRecordsPublication.for_committed_record(
                self._registry.snapshot().records,
                selected,
                previous_record=existing,
                previous_session=DesignSessionCheckpoint.capture(self._session),
                projection=projection,
                runtime_record=runtime_record,
                success_message="",
                success_code=success_notice_code,
            ),
        )

    def _discard_legacy_migration_unless_current(
        self,
        candidate: DesignSession,
    ) -> None:
        if not self._legacy_migration_is_current(candidate):
            self._discard_legacy_migration()

    def _discard_legacy_migration(self) -> None:
        self._legacy_migration_state = None
        self._legacy_rewrite_intent_id = None
        self._legacy_rewrite_failures = 0
        self._legacy_migration_lease = None

    def _legacy_migration_is_current(self, session: DesignSession) -> bool:
        lease = self._legacy_migration_lease
        document = session.document
        if lease is None or document is None:
            return False
        try:
            source_path = str(document.path.expanduser().resolve())
        except OSError:
            return False
        record = self._registry.get(lease.frame_id)
        return bool(
            id(self._session) == lease.session_identity
            and source_path == lease.source_path
            and str(document.source_load_id) == lease.source_load_id
            and str(document.top_cell_name) == lease.top_cell_name
            and tuple(sorted(document.visible_layers)) == lease.visible_layers
            and int(document.rotation_quarter_turns) % 4
            == lease.rotation_quarter_turns
            and session.active_frame_id == lease.frame_id
            and record is not None
            and record.version >= lease.minimum_frame_version
        )

    def _legacy_lease(
        self,
        candidate: DesignSession,
        selected,
        *,
        success_notice_code: str,
    ) -> _LegacyMigrationLease:
        document = candidate.document
        if document is None:
            raise DesignModelError("Migrated Design document is unavailable.")
        return _LegacyMigrationLease(
            session_identity=id(self._session),
            source_path=str(document.path.expanduser().resolve()),
            source_load_id=str(document.source_load_id),
            top_cell_name=str(document.top_cell_name),
            visible_layers=tuple(sorted(document.visible_layers)),
            rotation_quarter_turns=int(document.rotation_quarter_turns) % 4,
            frame_id=str(selected.frame_id),
            minimum_frame_version=int(selected.version),
            success_notice_code=success_notice_code,
        )

    @classmethod
    def _convert_legacy_state(
        cls,
        state: Mapping[str, object],
        session: DesignSession,
        request: DesignActivationRequest,
    ) -> dict[str, object]:
        document = session.document
        snapshot = request.machine_snapshot
        if document is None or snapshot is None:
            raise DesignModelError("Legacy Design registration is unavailable.")
        capture_mode, capture_offsets = cls._verified_legacy_provenance(
            state.get("stage_coordinate_provenance"),
            snapshot,
        )
        converted = dict(state)
        turns = int(document.rotation_quarter_turns) % 4
        for key in ("source_design_marks", "check_design_marks"):
            converted[key] = [
                list(
                    document.rotate_point(
                        (float(point[0]), float(point[1])),
                        -turns,
                    )
                )
                for point in state.get(key, ())
            ]
        for key in ("source_stage_marks", "check_stage_marks"):
            physical: list[list[float]] = []
            for point in state.get(key, ()):
                configured = objective_offsets.camera_stage_to_raw_stage(
                    (float(point[0]), float(point[1])),
                    request.objective_xy_offset,
                )
                raw_machine = tuple(
                    float(configured[index])
                    + (
                        capture_offsets[snapshot.axis_index[axis]]
                        if capture_mode == "work"
                        else 0.0
                    )
                    for index, axis in enumerate(("X", "Y"))
                )
                physical.append(
                    [
                        snapshot.mapper.controller_to_physical("X", raw_machine[0]),
                        snapshot.mapper.controller_to_physical("Y", raw_machine[1]),
                    ]
                )
            converted[key] = physical
        return converted

    @staticmethod
    def _verified_legacy_provenance(
        value: object,
        snapshot: object,
    ) -> tuple[str, tuple[float, ...]]:
        message = "Legacy registration requires verified capture-time WCO provenance."
        if not isinstance(value, Mapping) or set(value) != {
            "position_reporting_mode",
            "coordinate_system",
            "work_offset",
        }:
            raise DesignModelError(message)
        mode = value.get("position_reporting_mode")
        if mode not in {"machine", "work"}:
            raise DesignModelError(message)
        coordinate_system = value.get("coordinate_system")
        if mode == "work" and (
            not isinstance(coordinate_system, str) or not coordinate_system.strip()
        ):
            raise DesignModelError(message)
        if mode == "machine" and coordinate_system is not None:
            raise DesignModelError(message)
        raw_offsets = value.get("work_offset")
        if not isinstance(raw_offsets, (list, tuple)):
            raise DesignModelError(message)
        try:
            offsets = tuple(
                float(offset)
                for offset in raw_offsets
                if not isinstance(offset, bool)
            )
            required = tuple(snapshot.axis_index[axis] for axis in ("X", "Y"))
        except (KeyError, TypeError, ValueError) as exc:
            raise DesignModelError(message) from exc
        if (
            len(offsets) != len(raw_offsets)
            or not all(math.isfinite(offset) for offset in offsets)
            or any(index < 0 or index >= len(offsets) for index in required)
        ):
            raise DesignModelError(message)
        if mode == "machine" and any(
            not math.isclose(offsets[index], 0.0, abs_tol=1e-12)
            for index in required
        ):
            raise DesignModelError(message)
        return str(mode), offsets

    @staticmethod
    def _should_replace_registered(
        session: DesignSession,
        registry: CoordinateFrameRegistry,
        request: DesignActivationRequest,
    ) -> bool:
        if not request.create_new_if_registered or session.active_frame_id is None:
            return False
        current = registry.get(session.active_frame_id)
        if current is None:
            return False
        try:
            metadata = DesignFrameMetadata.from_mapping(current.metadata)
        except (KeyError, TypeError, ValueError):
            return False
        return bool(
            metadata.source_design_marks
            or metadata.source_machine_marks
            or metadata.check_design_marks
            or metadata.check_machine_marks
        )

    @staticmethod
    def _runtime_record(record, request, *, physical_b):
        unavailable = {
            axis
            for axis in ("X", "Y")
            if axis not in request.homed_axes
        }
        if physical_b is None:
            unavailable.add("B")
        if request.pivot_machine_xy is None:
            unavailable.update({"X", "Y", "B"})
        if not unavailable:
            return record
        return record.with_authority_block(
            unavailable,
            "Controller coordinate authority is unavailable.",
        )

    @staticmethod
    def _physical_b(snapshot: object | None) -> float | None:
        try:
            value = float(snapshot.physical_machine_pose.require("B"))
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    @staticmethod
    def _machine_mapper(request: DesignActivationRequest):
        snapshot = request.machine_snapshot
        if snapshot is None:
            return None

        def map_point(machine_xy):
            configured = (
                snapshot.physical_machine_to_configured_controller(
                    "X", machine_xy[0]
                ),
                snapshot.physical_machine_to_configured_controller(
                    "Y", machine_xy[1]
                ),
            )
            return objective_offsets.raw_stage_to_camera_stage(
                configured,
                request.objective_xy_offset,
            )

        return map_point

    @staticmethod
    def instances(
        session: DesignSession,
        registry: CoordinateFrameRegistry,
    ) -> tuple[tuple[str, str], ...]:
        document = session.document
        if document is None:
            return ()
        try:
            source_path = document.path.expanduser().resolve()
        except OSError:
            return ()
        matches: list[tuple[str, str]] = []
        for record in registry.snapshot().records:
            try:
                metadata = DesignFrameMetadata.from_mapping(record.metadata)
                same_design = (
                    Path(metadata.source_path).expanduser().resolve() == source_path
                    and metadata.top_cell_name == document.top_cell_name
                )
            except (KeyError, OSError, TypeError, ValueError):
                same_design = False
            if same_design:
                matches.append((record.frame_id, record.name))
        return tuple(matches)

    @staticmethod
    def _failure(error: Exception) -> _RegistrationTransitionParts:
        return _RegistrationTransitionParts(
            notices=(
                CoordinateNotice(
                    str(error),
                    severity="warning",
                    duration_ms=6000,
                    code="design_activation_rejected",
                ),
            ),
            accepted=False,
        )


__all__ = ["CoordinateDesignActivation"]
