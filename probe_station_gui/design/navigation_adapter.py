"""Design activation and persisted-restore policy without Qt side effects."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from probe_station_gui.coordinates.coordinator_model import (
    DesignSessionCheckpoint,
    FrameRecordsPublication,
)
from probe_station_gui.coordinates.model import CoordinateFrameRecord
from probe_station_gui.coordinates.registry import CoordinateFrameRegistry
from probe_station_gui.coordinates.provenance import design_frame_provenance_error
from probe_station_gui.coordinates.source_identity import source_identity
from probe_station_gui.design.frame_registration import (
    DesignFrameMetadata,
    design_frame_for_loaded_document,
    new_design_frame_draft,
)
from probe_station_gui.design.model import (
    DesignDocument,
    DesignModelError,
    Point2D,
)
from probe_station_gui.design import session_navigation, session_registration
from probe_station_gui.design.session import DesignSession
from probe_station_gui.design.session_registration import (
    DEFAULT_B_AXIS_ROTATION_PIVOT_STAGE,
    DesignFrameLinkProjection,
)
from probe_station_gui.design.session_state import (
    PreparedDesignSessionRestore,
    apply_prepared_session_restore,
)
from probe_station_gui.route.model import MeasurementRoute


DEFAULT_STAGE_AXIS_NAMES = ("X", "Y", "Z", "A", "B", "C")
DEFAULT_POSITION_TOLERANCE = 1e-3


@dataclass(frozen=True)
class PersistedDesignRestoreDecision:
    """Decision for one pending persisted-design restore attempt."""

    should_start_load: bool = False
    design_path: str | None = None
    restore_state: dict[str, object] | None = None
    clear_cached_design: bool = False
    axes_to_mark_unhomed: set[str] | None = None
    status_message: str | None = None
    status_timeout_ms: int = 5000


@dataclass(frozen=True)
class DesignLoadResultPlan:
    """Post-load session state and UI refresh plan."""

    accepted: bool
    document: DesignDocument | None = None
    document_directory: Path | None = None
    status_message: str | None = None
    status_timeout_ms: int = 5000
    clear_cached_design: bool = False
    last_selected_design_point: Point2D | None = None
    route_to_restore: MeasurementRoute | None = None


@dataclass(frozen=True)
class DesignFrameActivation:
    record: CoordinateFrameRecord
    created: bool = False
    updated: bool = False


@dataclass(frozen=True)
class PreparedDesignFrameActivation:
    activation: DesignFrameActivation
    projection: DesignFrameLinkProjection
    publication: FrameRecordsPublication | None = None


def coerce_position_tuple(value: object) -> tuple[float, ...] | None:
    if not isinstance(value, (list, tuple)) or not value:
        return None
    values: list[float] = []
    for item in value:
        try:
            coordinate = float(item)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(coordinate):
            return None
        values.append(coordinate)
    return tuple(values)


def position_axis_mismatches(
    expected: tuple[float, ...] | None,
    actual: tuple[float, ...],
    axis_names: tuple[str, ...] = DEFAULT_STAGE_AXIS_NAMES,
    tolerance: float = DEFAULT_POSITION_TOLERANCE,
) -> set[str]:
    if not expected:
        return {"X", "Y"}
    mismatches: set[str] = set()
    for index, axis_name in enumerate(axis_names):
        if index >= len(expected):
            break
        if index >= len(actual):
            mismatches.add(axis_name)
            continue
        try:
            expected_value = float(expected[index])
            actual_value = float(actual[index])
        except (TypeError, ValueError):
            mismatches.add(axis_name)
            continue
        if (
            not math.isfinite(expected_value)
            or not math.isfinite(actual_value)
            or abs(expected_value - actual_value) > tolerance
        ):
            mismatches.add(axis_name)
    return mismatches


def prepare_design_frame_publication(
    session: DesignSession,
    records: tuple[CoordinateFrameRecord, ...],
    committed_record: CoordinateFrameRecord,
    *,
    previous_record: CoordinateFrameRecord | None = None,
    previous_session: DesignSessionCheckpoint | None = None,
    runtime_record: CoordinateFrameRecord | None = None,
    machine_point_for_navigation: Callable[[Point2D], Point2D] | None = None,
    machine_b_deg: float | None = None,
    pivot_machine_xy: Point2D = DEFAULT_B_AXIS_ROTATION_PIVOT_STAGE,
    success_message: str | None = None,
    success_duration_ms: int = 0,
    success_code: str | None = None,
) -> FrameRecordsPublication:
    """Prepare a linked frame proposal without mutating the adopted session."""

    checkpoint = previous_session or DesignSessionCheckpoint.capture(session)
    projection_record = runtime_record or committed_record
    projection = session_registration.prepare_active_frame_link(
        session,
        projection_record,
        machine_point_for_navigation=machine_point_for_navigation,
        machine_b_deg=machine_b_deg,
        pivot_machine_xy=pivot_machine_xy,
    )
    return FrameRecordsPublication.for_committed_record(
        records,
        committed_record,
        previous_record=previous_record,
        previous_session=checkpoint,
        projection=projection,
        runtime_record=runtime_record,
        success_message=success_message,
        success_duration_ms=success_duration_ms,
        success_code=success_code,
    )


def activate_design_frame_for_document(
    session: DesignSession,
    registry: CoordinateFrameRegistry,
    document: DesignDocument,
    *,
    requested_frame_id: str | None = None,
    create_new: bool = False,
    current_metadata: DesignFrameMetadata | None = None,
    machine_point_for_navigation: Callable[[Point2D], Point2D] | None = None,
    machine_b_deg: float | None = None,
    pivot_machine_xy: Point2D = DEFAULT_B_AXIS_ROTATION_PIVOT_STAGE,
) -> DesignFrameActivation:
    """Select a durable frame for a loaded design or create another draft."""

    snapshot = registry.snapshot()
    records = snapshot.records
    existing_names = tuple(record.name for record in records)
    if create_new:
        candidate = new_design_frame_draft(
            document,
            existing_names=existing_names,
            metadata=current_metadata,
        )
        projection = session_registration.prepare_active_frame_link(
            session,
            candidate,
            machine_point_for_navigation=machine_point_for_navigation,
            machine_b_deg=machine_b_deg,
            pivot_machine_xy=pivot_machine_xy,
        )
        record = registry.add(candidate)
        session_registration.apply_active_frame_link(session, record, projection)
        return DesignFrameActivation(record=record, created=True)

    selected = registry.get(requested_frame_id) if requested_frame_id else None
    if requested_frame_id and selected is None:
        raise DesignModelError("Selected Design coordinate frame was not found.")
    if requested_frame_id and selected is not None:
        provenance_error = design_frame_provenance_error(selected)
        if provenance_error is not None:
            if session.active_frame_id == requested_frame_id:
                session_registration.clear_registration(session)
            raise DesignModelError(provenance_error)
        document_source = source_identity(document.path)
        mismatch_message = None
        if _frame_source_path(selected) != document_source:
            mismatch_message = "Coordinate frame belongs to a different design file."
        elif _frame_top_cell(selected) != document.top_cell_name:
            mismatch_message = (
                "Coordinate frame belongs to a different design top cell."
            )
        if mismatch_message is not None:
            if session.active_frame_id == requested_frame_id:
                session_registration.clear_registration(session)
            raise DesignModelError(mismatch_message)
    if selected is None:
        document_source = source_identity(document.path)
        selected = next(
            (
                record
                for record in records
                if _frame_source_path(record) == document_source
                and _frame_top_cell(record) == document.top_cell_name
            ),
            None,
        )
    if selected is not None:
        provenance_error = design_frame_provenance_error(selected)
        if provenance_error is not None:
            if session.active_frame_id == selected.frame_id:
                session_registration.clear_registration(session)
            raise DesignModelError(provenance_error)
    if selected is None:
        candidate = new_design_frame_draft(
            document,
            existing_names=existing_names,
            metadata=current_metadata,
        )
        projection = session_registration.prepare_active_frame_link(
            session,
            candidate,
            machine_point_for_navigation=machine_point_for_navigation,
            machine_b_deg=machine_b_deg,
            pivot_machine_xy=pivot_machine_xy,
        )
        selected = registry.add(candidate)
        session_registration.apply_active_frame_link(session, selected, projection)
        return DesignFrameActivation(record=selected, created=True)

    reconciled = design_frame_for_loaded_document(
        selected,
        document,
        current_metadata=current_metadata,
    )
    updated = reconciled != selected
    projection = session_registration.prepare_active_frame_link(
        session,
        reconciled,
        machine_point_for_navigation=machine_point_for_navigation,
        machine_b_deg=machine_b_deg,
        pivot_machine_xy=pivot_machine_xy,
    )
    if updated:
        reconciled = registry.replace(reconciled, expected_version=selected.version)
    session_registration.apply_active_frame_link(session, reconciled, projection)
    return DesignFrameActivation(record=reconciled, updated=updated)


def prepare_design_frame_activation(
    session: DesignSession,
    registry: CoordinateFrameRegistry,
    document: DesignDocument,
    *,
    requested_frame_id: str | None = None,
    current_metadata: DesignFrameMetadata | None = None,
    machine_point_for_navigation: Callable[[Point2D], Point2D] | None = None,
    machine_b_deg: float | None = None,
    pivot_machine_xy: Point2D = DEFAULT_B_AXIS_ROTATION_PIVOT_STAGE,
) -> PreparedDesignFrameActivation:
    """Prepare activation and any publication without mutating live owners."""

    proposed_registry = CoordinateFrameRegistry()
    proposed_registry.reset(registry.snapshot().records)
    proposed_session = DesignSession(document=document)
    proposed_session.active_frame_id = session.active_frame_id
    activation = activate_design_frame_for_document(
        proposed_session,
        proposed_registry,
        document,
        requested_frame_id=requested_frame_id,
        current_metadata=current_metadata,
        machine_point_for_navigation=machine_point_for_navigation,
        machine_b_deg=machine_b_deg,
        pivot_machine_xy=pivot_machine_xy,
    )
    if activation.created or activation.updated:
        publication = prepare_design_frame_publication(
            session,
            registry.snapshot().records,
            activation.record,
            previous_record=(
                registry.get(activation.record.frame_id) if activation.updated else None
            ),
            machine_point_for_navigation=machine_point_for_navigation,
            machine_b_deg=machine_b_deg,
            pivot_machine_xy=pivot_machine_xy,
        )
        link = publication.proposed_session_link
        assert link is not None
        return PreparedDesignFrameActivation(
            activation=activation,
            projection=link.projection,
            publication=publication,
        )
    projection = session_registration.prepare_active_frame_link(
        session,
        activation.record,
        machine_point_for_navigation=machine_point_for_navigation,
        machine_b_deg=machine_b_deg,
        pivot_machine_xy=pivot_machine_xy,
    )
    return PreparedDesignFrameActivation(activation, projection)


def _frame_source_path(record: CoordinateFrameRecord) -> str | None:
    try:
        metadata = DesignFrameMetadata.from_mapping(record.metadata)
        return source_identity(metadata.source_path)
    except (KeyError, TypeError, ValueError):
        return None


def _frame_top_cell(record: CoordinateFrameRecord) -> str:
    try:
        return DesignFrameMetadata.from_mapping(record.metadata).top_cell_name
    except (KeyError, TypeError, ValueError):
        return ""


def prepare_persisted_design_restore(
    cached_state: dict[str, object] | None,
    *,
    expected_position: tuple[float, ...] | None = None,
    actual_position: tuple[float, ...],
    document_loaded: bool,
    axis_names: tuple[str, ...] = DEFAULT_STAGE_AXIS_NAMES,
    tolerance: float = DEFAULT_POSITION_TOLERANCE,
) -> PersistedDesignRestoreDecision:
    if cached_state is None or document_loaded:
        return PersistedDesignRestoreDecision()
    design_state = dict(cached_state)
    axis_mismatches = position_axis_mismatches(
        expected_position,
        actual_position,
        axis_names,
        tolerance,
    )
    xy_mismatches = axis_mismatches.intersection({"X", "Y"})
    if expected_position is None or xy_mismatches:
        return PersistedDesignRestoreDecision(
            clear_cached_design=True,
            axes_to_mark_unhomed=set(xy_mismatches),
            status_message=(
                "Controller X/Y coordinates changed. Cleared cached design selection."
            ),
        )
    design_path = str(design_state.get("document_path") or "").strip()
    if not design_path:
        return PersistedDesignRestoreDecision(clear_cached_design=True)
    z_axes = {"Z"} if "Z" in axis_mismatches else set()
    return PersistedDesignRestoreDecision(
        should_start_load=True,
        design_path=design_path,
        restore_state=design_state,
        axes_to_mark_unhomed=z_axes,
    )


def design_load_error_plan(
    error: object,
    restore_state: dict[str, object] | None,
) -> DesignLoadResultPlan:
    return DesignLoadResultPlan(
        accepted=False,
        status_message=str(error),
        status_timeout_ms=6000,
        clear_cached_design=restore_state is not None,
    )


def unexpected_design_document_plan(
    restore_state: dict[str, object] | None,
) -> DesignLoadResultPlan:
    return DesignLoadResultPlan(
        accepted=False,
        status_message="Loaded design has an unexpected type.",
        status_timeout_ms=6000,
        clear_cached_design=restore_state is not None,
    )


def design_document_loaded_plan(
    session: DesignSession,
    document: object,
    error: object,
    restore_state: dict[str, object] | None,
    prepared_restore: PreparedDesignSessionRestore | None = None,
) -> DesignLoadResultPlan:
    if error is not None:
        return design_load_error_plan(error, restore_state)
    if not isinstance(document, DesignDocument):
        return unexpected_design_document_plan(restore_state)
    return apply_loaded_design_document(
        session,
        document,
        restore_state,
        prepared_restore=prepared_restore,
    )


def apply_loaded_design_document(
    session: DesignSession,
    document: DesignDocument,
    restore_state: dict[str, object] | None,
    *,
    prepared_restore: PreparedDesignSessionRestore | None = None,
) -> DesignLoadResultPlan:
    if prepared_restore is not None:
        apply_prepared_session_restore(session, prepared_restore)
        if session.document is not None:
            document = session.document
    elif restore_state is None:
        session_navigation.load_document(session, document)
    else:
        raise DesignModelError(
            "Persisted design restore was not prepared on the document-load thread."
        )
    current_route_point = session_navigation.current_route_point(session)
    last_selected_design_point = (
        current_route_point.camera_center if current_route_point is not None else None
    )
    return DesignLoadResultPlan(
        accepted=True,
        document=document,
        document_directory=document.path.parent,
        status_message=f"Loaded design '{document.path.name}' ({document.top_cell_name}).",
        status_timeout_ms=5000,
        last_selected_design_point=last_selected_design_point,
        route_to_restore=session.route,
    )


__all__ = [
    "DesignFrameActivation",
    "DesignLoadResultPlan",
    "PersistedDesignRestoreDecision",
    "PreparedDesignFrameActivation",
    "activate_design_frame_for_document",
    "apply_loaded_design_document",
    "coerce_position_tuple",
    "design_load_error_plan",
    "design_document_loaded_plan",
    "position_axis_mismatches",
    "prepare_design_frame_activation",
    "prepare_design_frame_publication",
    "prepare_persisted_design_restore",
    "unexpected_design_document_plan",
]
