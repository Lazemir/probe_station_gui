"""Design navigation policy separated from Qt widget side effects."""

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
from probe_station_gui.design.frame_registration import (
    DesignFrameMetadata,
    design_frame_for_loaded_document,
    new_design_frame_draft,
)
from probe_station_gui.design.model import (
    DesignDocument,
    DesignModelError,
    MeasurementTarget,
    Point2D,
)
from probe_station_gui.design.session import (
    DEFAULT_B_AXIS_ROTATION_PIVOT_STAGE,
    DesignFrameLinkProjection,
    DesignSession,
)
from probe_station_gui.design.selection_model import (
    MixedArrayPlan,
    MixedDeletePlan,
    MixedEditPlan,
)
from probe_station_gui.route.model import MeasurementRoute, RouteModelError, RoutePoint


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


@dataclass(frozen=True)
class RoutePointSelectionPlan:
    """Route point selection outcome."""

    selected: bool
    point: RoutePoint | None = None
    last_selected_design_point: Point2D | None = None
    status_message: str | None = None
    status_timeout_ms: int = 3000


@dataclass(frozen=True)
class RouteEditPlan:
    """Generic route edit outcome."""

    accepted: bool
    status_message: str | None = None
    status_timeout_ms: int = 5000
    last_selected_design_point: Point2D | None = None
    route: MeasurementRoute | None = None
    selected_route_point_index: int = -1
    added_points: tuple[RoutePoint, ...] = ()
    removed_point: RoutePoint | None = None


@dataclass(frozen=True)
class DesignMovePlan:
    """Design move outcome for `Main` to execute against the stage."""

    accepted: bool
    stage_xy: Point2D | None = None
    design_xy: Point2D | None = None
    source_label: str = ""
    status_message: str | None = None
    status_timeout_ms: int = 5000
    last_selected_design_point: Point2D | None = None
    pending_planned_move_target_xy: Point2D | None = None


@dataclass(frozen=True)
class DesignTargetSelectionPlan:
    """Design target selection outcome."""

    selected: bool
    target: MeasurementTarget | None = None
    status_message: str | None = None
    status_timeout_ms: int = 3000


@dataclass(frozen=True)
class DesignPanelPresentation:
    """Widget-agnostic payload for design navigator and layout panels."""

    document: DesignDocument | None
    design_snap_enabled: bool
    registration_valid: bool
    targets: list[MeasurementTarget]
    selected_target_id: str | None
    route: MeasurementRoute | None
    selected_route_point_index: int
    route_measurement_running: bool
    calibration_prompt: str
    registration_status: str
    source_design_marks: tuple[Point2D, ...]
    check_design_marks: tuple[Point2D, ...]
    source_stage_marks: tuple[Point2D, ...]


@dataclass(frozen=True)
class DesignPositionPresentation:
    """Widget-agnostic payload for current design position and minimap."""

    document: DesignDocument | None
    targets: list[MeasurementTarget]
    selected_target_id: str | None
    probe_route: MeasurementRoute | None
    selected_route_point_index: int
    selected_design_point: Point2D | None
    stage_xy: Point2D | None
    current_design_position: Point2D | None
    fov_design_size: Point2D | None
    source_design_marks: tuple[Point2D, ...]
    check_design_marks: tuple[Point2D, ...]


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


def persisted_design_file_is_current(state: dict[str, object]) -> bool:
    path_text = str(state.get("document_path") or "").strip()
    if not path_text:
        return False
    path = Path(path_text).expanduser()
    try:
        stat = path.stat()
    except OSError:
        return False
    saved_size = state.get("document_size")
    if saved_size is not None:
        try:
            if int(saved_size) != int(stat.st_size):
                return False
        except (TypeError, ValueError):
            return False
    saved_mtime = state.get("document_mtime_ns")
    if saved_mtime is not None:
        try:
            if int(saved_mtime) != int(stat.st_mtime_ns):
                return False
        except (TypeError, ValueError):
            return False
    return True


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
    projection = session.prepare_active_frame_link(
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
        projection = session.prepare_active_frame_link(
            candidate,
            machine_point_for_navigation=machine_point_for_navigation,
            machine_b_deg=machine_b_deg,
            pivot_machine_xy=pivot_machine_xy,
        )
        record = registry.add(candidate)
        session.apply_active_frame_link(record, projection)
        return DesignFrameActivation(record=record, created=True)

    selected = registry.get(requested_frame_id) if requested_frame_id else None
    if requested_frame_id and selected is None:
        raise DesignModelError("Selected Design coordinate frame was not found.")
    if requested_frame_id and selected is not None:
        provenance_error = design_frame_provenance_error(selected)
        if provenance_error is not None:
            if session.active_frame_id == requested_frame_id:
                session.clear_registration()
            raise DesignModelError(provenance_error)
        resolved_path = document.path.expanduser().resolve()
        mismatch_message = None
        if _frame_source_path(selected) != resolved_path:
            mismatch_message = "Coordinate frame belongs to a different design file."
        elif _frame_top_cell(selected) != document.top_cell_name:
            mismatch_message = (
                "Coordinate frame belongs to a different design top cell."
            )
        if mismatch_message is not None:
            if session.active_frame_id == requested_frame_id:
                session.clear_registration()
            raise DesignModelError(mismatch_message)
    if selected is None:
        resolved_path = document.path.expanduser().resolve()
        selected = next(
            (
                record
                for record in records
                if _frame_source_path(record) == resolved_path
                and _frame_top_cell(record) == document.top_cell_name
            ),
            None,
        )
    if selected is not None:
        provenance_error = design_frame_provenance_error(selected)
        if provenance_error is not None:
            if session.active_frame_id == selected.frame_id:
                session.clear_registration()
            raise DesignModelError(provenance_error)
    if selected is None:
        candidate = new_design_frame_draft(
            document,
            existing_names=existing_names,
            metadata=current_metadata,
        )
        projection = session.prepare_active_frame_link(
            candidate,
            machine_point_for_navigation=machine_point_for_navigation,
            machine_b_deg=machine_b_deg,
            pivot_machine_xy=pivot_machine_xy,
        )
        selected = registry.add(candidate)
        session.apply_active_frame_link(selected, projection)
        return DesignFrameActivation(record=selected, created=True)

    reconciled = design_frame_for_loaded_document(
        selected,
        document,
        current_metadata=current_metadata,
    )
    updated = reconciled != selected
    projection = session.prepare_active_frame_link(
        reconciled,
        machine_point_for_navigation=machine_point_for_navigation,
        machine_b_deg=machine_b_deg,
        pivot_machine_xy=pivot_machine_xy,
    )
    if updated:
        reconciled = registry.replace(reconciled, expected_version=selected.version)
    session.apply_active_frame_link(reconciled, projection)
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
    projection = session.prepare_active_frame_link(
        activation.record,
        machine_point_for_navigation=machine_point_for_navigation,
        machine_b_deg=machine_b_deg,
        pivot_machine_xy=pivot_machine_xy,
    )
    return PreparedDesignFrameActivation(activation, projection)


def _frame_source_path(record: CoordinateFrameRecord) -> Path | None:
    try:
        return Path(DesignFrameMetadata.from_mapping(record.metadata).source_path).resolve()
    except (KeyError, TypeError, ValueError, OSError):
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
    file_is_current: Callable[[dict[str, object]], bool] = persisted_design_file_is_current,
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
    if not file_is_current(design_state):
        return PersistedDesignRestoreDecision(
            clear_cached_design=True,
            status_message=(
                "Cached design file changed or is unavailable. "
                "Cleared cached design selection."
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


def parse_persisted_visible_layers(value: object) -> set[tuple[int, int]]:
    layers: set[tuple[int, int]] = set()
    if not isinstance(value, list):
        return layers
    for item in value:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            continue
        try:
            layers.add((int(item[0]), int(item[1])))
        except (TypeError, ValueError):
            continue
    return layers


def document_with_persisted_design_view(
    document: DesignDocument,
    state: dict[str, object],
) -> DesignDocument:
    top_cell_name = str(state.get("top_cell_name") or "").strip()
    if top_cell_name and top_cell_name != document.top_cell_name:
        document = document.with_top_cell(top_cell_name)
    try:
        rotation_quarter_turns = int(state.get("rotation_quarter_turns", 0))
    except (TypeError, ValueError):
        rotation_quarter_turns = 0
    if rotation_quarter_turns:
        document = document.with_rotation_delta(rotation_quarter_turns)
    visible_layers = parse_persisted_visible_layers(state.get("visible_layers"))
    if visible_layers:
        document = document.with_visible_layers(visible_layers)
    return document


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
) -> DesignLoadResultPlan:
    if error is not None:
        return design_load_error_plan(error, restore_state)
    if not isinstance(document, DesignDocument):
        return unexpected_design_document_plan(restore_state)
    return apply_loaded_design_document(session, document, restore_state)


def apply_loaded_design_document(
    session: DesignSession,
    document: DesignDocument,
    restore_state: dict[str, object] | None,
) -> DesignLoadResultPlan:
    if restore_state is None:
        session.load_document(document)
    else:
        document = document_with_persisted_design_view(document, restore_state)
        session.restore_persisted_state(document, restore_state)
    current_route_point = session.current_route_point()
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


def unload_design_document(session: DesignSession) -> RouteEditPlan:
    if session.document is None:
        return RouteEditPlan(False)
    document_name = session.document.path.name
    session.unload_document()
    return RouteEditPlan(
        True,
        status_message=f"Unloaded design '{document_name}'.",
        status_timeout_ms=5000,
    )


def set_design_top_cell(session: DesignSession, top_cell_name: str) -> RouteEditPlan:
    session.set_top_cell(top_cell_name)
    return RouteEditPlan(
        True,
        status_message=f"Switched design top cell to '{top_cell_name}'.",
        status_timeout_ms=5000,
    )


def create_measurement_route(session: DesignSession) -> RouteEditPlan:
    route = session.create_route()
    return RouteEditPlan(
        True,
        status_message=f"Created route '{route.name}'.",
        status_timeout_ms=4000,
        route=route,
    )


def save_measurement_route(
    session: DesignSession,
    route_path: str | None = None,
) -> RouteEditPlan:
    route = session.route
    if route is None:
        return RouteEditPlan(
            False,
            status_message="No route is loaded.",
            status_timeout_ms=4000,
        )
    path = route.save(route_path)
    return RouteEditPlan(
        True,
        status_message=f"Saved route '{path.name}'.",
        status_timeout_ms=4000,
        route=route,
    )


def set_design_layer_visibility(
    session: DesignSession,
    layer: int,
    datatype: int,
    visible: bool,
) -> RouteEditPlan:
    document = session.document
    if document is None:
        return RouteEditPlan(False)
    visible_layers = set(document.visible_layers)
    layer_key = (int(layer), int(datatype))
    if visible:
        visible_layers.add(layer_key)
    else:
        visible_layers.discard(layer_key)
    session.set_visible_layers(visible_layers)
    return RouteEditPlan(True)


def rotate_design_document(
    session: DesignSession,
    quarter_turn_delta: int,
    last_selected_design_point: Point2D | None,
    *,
    can_rotate: bool,
) -> RouteEditPlan:
    if not can_rotate:
        return RouteEditPlan(False)
    document = session.document
    if document is None:
        return RouteEditPlan(
            False,
            status_message="Load a design before rotating it.",
            status_timeout_ms=4000,
        )
    delta = int(quarter_turn_delta) % 4
    if delta == 0:
        delta = 1
    rotated_document = session.rotate_document(delta)
    selected_point = (
        document.rotate_point(last_selected_design_point, delta)
        if last_selected_design_point is not None
        else None
    )
    return RouteEditPlan(
        True,
        status_message=(
            "Rotated design counterclockwise: "
            f"{rotated_document.rotation_quarter_turns * 90} deg."
        ),
        status_timeout_ms=4000,
        last_selected_design_point=selected_point,
    )


def load_measurement_route(session: DesignSession, route_path: str) -> RouteEditPlan:
    if session.document is None:
        return RouteEditPlan(
            False,
            status_message="Load a design before loading a route.",
        )
    route = MeasurementRoute.load(route_path)
    session.set_route(route)
    current_point = session.current_route_point()
    return RouteEditPlan(
        True,
        status_message=f"Loaded route '{route.name}' with {len(route.points)} points.",
        route=route,
        last_selected_design_point=(
            current_point.camera_center if current_point is not None else None
        ),
    )


def add_design_route_point(
    session: DesignSession,
    x_value: float,
    y_value: float,
) -> RouteEditPlan:
    point = session.add_route_point((float(x_value), float(y_value)))
    return RouteEditPlan(
        True,
        status_message=(
            f"Added route point {point.label} at X={point.camera_center[0]:.3f}, "
            f"Y={point.camera_center[1]:.3f}."
        ),
        status_timeout_ms=3000,
        last_selected_design_point=point.camera_center,
    )


def add_route_array_points(
    session: DesignSession,
    origin_x: float,
    origin_y: float,
    step_x_dx: float,
    step_x_dy: float,
    count_x: int,
    step_y_dx: float,
    step_y_dy: float,
    count_y: int,
    serpentine: bool,
    replace_existing: bool,
    selected_indices: object = None,
) -> RouteEditPlan:
    if session.document is None:
        return RouteEditPlan(
            False,
            status_message="Load a design before adding route points.",
        )
    route = session.route
    if route is None:
        route = session.create_route()
    selection: list[int] = []
    if isinstance(selected_indices, (list, tuple)):
        for item in selected_indices:
            try:
                selection.append(int(item))
            except (TypeError, ValueError):
                continue
    if len(selection) >= 2:
        added = route.add_array_copies_from_points(
            selection,
            (float(step_x_dx), float(step_x_dy)),
            int(count_x),
            (float(step_y_dx), float(step_y_dy)),
            int(count_y),
            serpentine=bool(serpentine),
        )
        replace_existing = False
    else:
        added = route.add_grid_points(
            (float(origin_x), float(origin_y)),
            (float(step_x_dx), float(step_x_dy)),
            int(count_x),
            (float(step_y_dx), float(step_y_dy)),
            int(count_y),
            serpentine=bool(serpentine),
            clear_existing=bool(replace_existing),
        )
    if added:
        session.selected_route_point_index = len(route.points) - 1
        last_selected = added[-1].camera_center
    else:
        session.selected_route_point_index = -1
        last_selected = None
    if len(selection) >= 2:
        return RouteEditPlan(
            True,
            status_message=(
                f"Added {len(added)} copied route points "
                f"from {len(selection)} selected points."
            ),
            status_timeout_ms=4000,
            route=route,
            selected_route_point_index=session.selected_route_point_index,
            added_points=tuple(added),
            last_selected_design_point=last_selected,
        )
    mode = "Replaced route with" if replace_existing else "Added"
    return RouteEditPlan(
        True,
        status_message=(
            f"{mode} {len(added)} array route points "
            f"from X={float(origin_x):.3f}, Y={float(origin_y):.3f}."
        ),
        status_timeout_ms=4000,
        route=route,
        selected_route_point_index=session.selected_route_point_index,
        added_points=tuple(added),
        last_selected_design_point=last_selected,
    )


def apply_route_entity_changes(
    session: DesignSession,
    plan: MixedEditPlan,
) -> RouteEditPlan:
    """Apply the route half of one prevalidated mixed design edit."""

    if not plan.accepted:
        return RouteEditPlan(False, status_message=plan.status_message)
    if isinstance(plan, MixedDeletePlan):
        remove_ids = plan.route_remove_ids
        append_points: tuple[RoutePoint, ...] = ()
    elif isinstance(plan, MixedArrayPlan):
        remove_ids = frozenset()
        append_points = plan.route_copies
    else:  # pragma: no cover - closed union defensive guard
        return RouteEditPlan(False, status_message="Unknown mixed design edit.")
    route = session.route
    route_change_requested = bool(
        plan.required_route_ids or remove_ids or append_points
    )
    if route is None:
        if route_change_requested:
            return RouteEditPlan(False, status_message="Selected route points are stale.")
        return RouteEditPlan(True, status_message=plan.status_message)
    if not route_change_requested:
        current = session.current_route_point()
        return RouteEditPlan(
            True,
            status_message=plan.status_message,
            route=route,
            selected_route_point_index=session.selected_route_point_index,
            last_selected_design_point=(
                current.camera_center if current is not None else None
            ),
        )
    selected_id = None
    if 0 <= session.selected_route_point_index < len(route.points):
        selected_id = route.points[session.selected_route_point_index].id
    removed_points = tuple(point for point in route.points if point.id in remove_ids)
    try:
        route.apply_point_changes(
            required_ids=plan.required_route_ids,
            remove_ids=remove_ids,
            append_points=append_points,
        )
    except RouteModelError as exc:
        return RouteEditPlan(False, status_message=str(exc))
    if not route.points:
        session.selected_route_point_index = -1
    elif selected_id is not None and any(
        point.id == selected_id for point in route.points
    ):
        session.selected_route_point_index = next(
            index for index, point in enumerate(route.points) if point.id == selected_id
        )
    else:
        session.selected_route_point_index = min(
            max(session.selected_route_point_index, 0),
            len(route.points) - 1,
        )
    current = session.current_route_point()
    return RouteEditPlan(
        True,
        status_message=plan.status_message,
        route=route,
        selected_route_point_index=session.selected_route_point_index,
        added_points=append_points,
        removed_point=removed_points[0] if removed_points else None,
        last_selected_design_point=(
            current.camera_center if current is not None else None
        ),
    )


def remove_selected_route_point(session: DesignSession) -> RouteEditPlan:
    point = session.remove_selected_route_point()
    if point is None:
        return RouteEditPlan(
            False,
            status_message="No route point is selected.",
            status_timeout_ms=3000,
        )
    current_point = session.current_route_point()
    return RouteEditPlan(
        True,
        status_message=f"Removed route point {point.label}.",
        status_timeout_ms=3000,
        removed_point=point,
        last_selected_design_point=(
            current_point.camera_center if current_point is not None else None
        ),
    )


def clear_measurement_route_points(session: DesignSession) -> RouteEditPlan:
    route = session.route
    if route is None:
        return RouteEditPlan(
            False,
            status_message="No route is loaded.",
            status_timeout_ms=3000,
        )
    route.clear_points()
    session.selected_route_point_index = -1
    return RouteEditPlan(
        True,
        status_message="Cleared route points.",
        status_timeout_ms=3000,
    )


def select_route_point(session: DesignSession, index: int) -> RoutePointSelectionPlan:
    point = session.select_route_point(int(index))
    return RoutePointSelectionPlan(
        selected=point is not None,
        point=point,
        last_selected_design_point=(
            point.camera_center if point is not None else None
        ),
    )


def select_route_point_for_measurement(
    session: DesignSession,
    point_number: int,
) -> RoutePointSelectionPlan:
    route = session.route
    if route is None or not route.points:
        return RoutePointSelectionPlan(False)
    index = int(point_number) - 1
    if not 0 <= index < len(route.points):
        return RoutePointSelectionPlan(False)
    if session.selected_route_point_index == index:
        point = session.current_route_point()
        return RoutePointSelectionPlan(
            False,
            point=point,
            last_selected_design_point=(
                point.camera_center if point is not None else None
            ),
        )
    return select_route_point(session, index)


def select_design_target(
    session: DesignSession,
    target_id: str,
) -> DesignTargetSelectionPlan:
    target = session.select_target_by_id(target_id)
    return DesignTargetSelectionPlan(target is not None, target)


def select_next_design_target(session: DesignSession) -> DesignTargetSelectionPlan:
    target = session.select_next_target()
    return DesignTargetSelectionPlan(
        target is not None,
        target,
        status_message=(
            f"Selected target '{target.label}'." if target is not None else None
        ),
    )


def select_previous_design_target(session: DesignSession) -> DesignTargetSelectionPlan:
    target = session.select_previous_target()
    return DesignTargetSelectionPlan(
        target is not None,
        target,
        status_message=(
            f"Selected target '{target.label}'." if target is not None else None
        ),
    )


def plan_design_target_move(
    session: DesignSession,
    target_id: str,
    stage_xy: Point2D | None,
) -> DesignMovePlan:
    target = session.select_target_by_id(target_id)
    if target is None:
        return DesignMovePlan(
            False,
            status_message=f"Unknown target '{target_id}'.",
        )
    if stage_xy is None:
        return DesignMovePlan(
            False,
            status_message="Design registration is required before moving to a target.",
            status_timeout_ms=6000,
        )
    return DesignMovePlan(
        True,
        stage_xy=(float(stage_xy[0]), float(stage_xy[1])),
        design_xy=target.design_center,
    )


def plan_design_coordinate_move(
    document_loaded: bool,
    stage_busy: bool,
    design_xy: Point2D,
    stage_xy: Point2D | None,
    source_label: str,
) -> DesignMovePlan:
    if not document_loaded:
        return DesignMovePlan(False)
    if stage_busy:
        return DesignMovePlan(
            False,
            status_message="Stage is busy. Ignoring design move request.",
            status_timeout_ms=3000,
        )
    if stage_xy is None:
        return DesignMovePlan(
            False,
            status_message="Design click-to-move requires completed registration.",
        )
    normalized_design = (float(design_xy[0]), float(design_xy[1]))
    normalized_stage = (float(stage_xy[0]), float(stage_xy[1]))
    return DesignMovePlan(
        True,
        stage_xy=normalized_stage,
        design_xy=normalized_design,
        source_label=source_label,
        last_selected_design_point=normalized_design,
        pending_planned_move_target_xy=normalized_stage,
    )


def design_panel_presentation(
    session: DesignSession,
    route_running: bool,
    pending_alignment_preparation: bool,
    design_snap_enabled: bool,
) -> DesignPanelPresentation:
    current_target = session.current_target()
    registration_valid = (
        session.registration is not None and session.registration.valid
    )
    return DesignPanelPresentation(
        document=session.document,
        design_snap_enabled=bool(design_snap_enabled),
        registration_valid=registration_valid,
        targets=list(session.targets),
        selected_target_id=current_target.id if current_target else None,
        route=session.route,
        selected_route_point_index=session.selected_route_point_index,
        route_measurement_running=bool(route_running),
        calibration_prompt=(
            "Calibration step 4/4: chip rotation is in progress."
            if pending_alignment_preparation
            else session.calibration_prompt()
        ),
        registration_status=session.registration_status,
        source_design_marks=tuple(session.source_design_marks_compact()),
        check_design_marks=tuple(session.check_design_marks),
        source_stage_marks=tuple(session.source_stage_marks_compact()),
    )


def design_position_presentation(
    session: DesignSession,
    stage_xy: Point2D | None,
    design_xy: Point2D | None,
    fov_design_size: Point2D | None,
    last_selected_design_point: Point2D | None,
) -> DesignPositionPresentation:
    current_target = session.current_target()
    return DesignPositionPresentation(
        document=session.document,
        targets=list(session.targets),
        selected_target_id=current_target.id if current_target else None,
        probe_route=session.route,
        selected_route_point_index=session.selected_route_point_index,
        selected_design_point=last_selected_design_point,
        stage_xy=stage_xy,
        current_design_position=design_xy,
        fov_design_size=fov_design_size,
        source_design_marks=tuple(session.source_design_marks_compact()),
        check_design_marks=tuple(session.check_design_marks),
    )


__all__ = [
    "DesignFrameActivation",
    "DesignLoadResultPlan",
    "DesignMovePlan",
    "DesignPanelPresentation",
    "DesignPositionPresentation",
    "DesignTargetSelectionPlan",
    "PersistedDesignRestoreDecision",
    "PreparedDesignFrameActivation",
    "RouteEditPlan",
    "RoutePointSelectionPlan",
    "add_design_route_point",
    "add_route_array_points",
    "activate_design_frame_for_document",
    "apply_loaded_design_document",
    "clear_measurement_route_points",
    "coerce_position_tuple",
    "design_load_error_plan",
    "design_document_loaded_plan",
    "design_panel_presentation",
    "design_position_presentation",
    "document_with_persisted_design_view",
    "create_measurement_route",
    "load_measurement_route",
    "parse_persisted_visible_layers",
    "persisted_design_file_is_current",
    "plan_design_coordinate_move",
    "plan_design_target_move",
    "position_axis_mismatches",
    "prepare_design_frame_publication",
    "prepare_design_frame_activation",
    "prepare_persisted_design_restore",
    "remove_selected_route_point",
    "rotate_design_document",
    "select_design_target",
    "select_next_design_target",
    "select_previous_design_target",
    "select_route_point",
    "select_route_point_for_measurement",
    "save_measurement_route",
    "set_design_layer_visibility",
    "set_design_top_cell",
    "unexpected_design_document_plan",
    "unload_design_document",
]
