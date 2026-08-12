"""Design document lifecycle, route editing, and target navigation policy."""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol

from probe_station_gui.design.model import (
    DesignDocument,
    DesignModelError,
    LayerKey,
    MeasurementTarget,
    Point2D,
)
from probe_station_gui.design.rigid_registration import DesignRegistration
from probe_station_gui.design import session_registration
from probe_station_gui.route.model import MeasurementRoute, RoutePoint


class _NavigationSession(Protocol):
    document: DesignDocument | None
    registration: DesignRegistration | None
    source_design_marks: tuple[Point2D, ...]
    source_stage_marks: tuple[Point2D, ...]
    check_design_marks: list[Point2D]
    check_stage_marks: list[Point2D]
    targets: list[MeasurementTarget]
    selected_target_index: int
    route: MeasurementRoute | None
    selected_route_point_index: int
    registration_status: str
    active_frame_id: str | None
    _runtime_blocked_persisted_state: dict[str, object] | None
    _legacy_stage_coordinate_provenance: object
    _legacy_stage_coordinate_provenance_present: bool


def load_document(session: _NavigationSession, document: DesignDocument) -> None:
    """Attach a new design document and clear derived state."""
    session._runtime_blocked_persisted_state = None
    session.document = document
    session.active_frame_id = None
    clear_targets(session)
    clear_route(session)
    session_registration.clear_registration(session)


def unload_document(session: _NavigationSession) -> None:
    """Remove the active design and all related state."""
    session.document = None
    session.active_frame_id = None
    clear_targets(session)
    clear_route(session)
    session_registration.clear_registration(session)


def set_top_cell(session: _NavigationSession, top_cell_name: str) -> None:
    """Switch the active top cell and clear derived state."""
    if session.document is None:
        raise DesignModelError("No design document is loaded.")
    load_document(session, session.document.with_top_cell(top_cell_name))


def set_visible_layers(session: _NavigationSession, layers: set[LayerKey]) -> None:
    """Update the active visible layer subset."""
    if session.document is None:
        raise DesignModelError("No design document is loaded.")
    session.document = session.document.with_visible_layers(layers)


def rotate_document(
    session: _NavigationSession, quarter_turn_delta: int
) -> DesignDocument:
    """Rotate the active design and all design-space annotations by 90-degree steps."""
    if session.document is None:
        raise DesignModelError("No design document is loaded.")
    delta = int(quarter_turn_delta) % 4
    if delta == 0:
        return session.document
    old_document = session.document
    new_document = old_document.with_rotation_delta(delta)

    def rotate_point(point: Point2D) -> Point2D:
        return old_document.rotate_point(point, delta)

    registration_was_stale = session.registration is not None and (
        not session.registration.valid
    )
    stale_reason = (
        session.registration.stale_reason
        if session.registration is not None and session.registration.stale_reason
        else session.registration_status
    )
    session.document = new_document
    session.source_design_marks = tuple(
        (
            rotate_point(point)
            for point in session_registration.source_design_marks_compact(session)
        )
    )
    session.check_design_marks = [
        rotate_point(point) for point in session.check_design_marks
    ]
    session.targets = [
        replace(target, design_center=rotate_point(target.design_center))
        for target in session.targets
    ]
    if session.route is not None:
        session.route.transform_design_coordinates(
            new_document,
            rotate_point,
            lambda vector: old_document.rotate_vector(vector, delta),
        )
    session.registration = None
    session_registration.rebuild_registration(session)
    if registration_was_stale and session.registration is not None:
        session.registration = session.registration.mark_stale(stale_reason)
        session.registration_status = stale_reason
    return new_document


def clear_targets(session: _NavigationSession) -> None:
    """Remove optional navigation targets."""
    session.targets.clear()
    session.selected_target_index = -1


def set_targets(session: _NavigationSession, targets: list[MeasurementTarget]) -> None:
    """Replace optional navigation targets with a new ordered list."""
    session.targets = list(targets)
    session.selected_target_index = 0 if session.targets else -1


def create_route(
    session: _NavigationSession, *, name: str | None = None
) -> MeasurementRoute:
    """Create an empty route for the active design."""
    if session.document is None:
        raise DesignModelError("Load a design before creating a route.")
    route = MeasurementRoute.default_for_document(session.document, name=name)
    set_route(session, route)
    return route


def set_route(session: _NavigationSession, route: MeasurementRoute) -> None:
    """Attach a route after checking that it belongs to the active design."""
    if session.document is None:
        raise DesignModelError("Load a design before loading a route.")
    route.validate_for_document(session.document)
    session.route = route
    session.selected_route_point_index = 0 if route.points else -1


def clear_route(session: _NavigationSession) -> None:
    """Remove the current design-bound probe route."""
    session.route = None
    session.selected_route_point_index = -1


def add_route_point(session: _NavigationSession, point: Point2D) -> RoutePoint:
    """Append a route point, creating a route for the active design if needed."""
    if session.document is None:
        raise DesignModelError("Load a design before adding route points.")
    if session.route is None:
        create_route(session)
    assert session.route is not None
    route_point = session.route.add_point(point)
    session.selected_route_point_index = len(session.route.points) - 1
    return route_point


def remove_selected_route_point(session: _NavigationSession) -> RoutePoint | None:
    """Remove the selected route point and update selection."""
    if session.route is None:
        return None
    removed = session.route.remove_point_at(session.selected_route_point_index)
    if not session.route.points:
        session.selected_route_point_index = -1
    else:
        session.selected_route_point_index = min(
            max(session.selected_route_point_index, 0), len(session.route.points) - 1
        )
    return removed


def clear_route_points(session: _NavigationSession) -> None:
    """Remove all points from the current route without dropping its binding."""
    if session.route is None:
        return
    session.route.clear_points()
    session.selected_route_point_index = -1


def select_route_point(session: _NavigationSession, index: int) -> RoutePoint | None:
    """Select one route point by row index."""
    if session.route is None or not 0 <= index < len(session.route.points):
        session.selected_route_point_index = -1
        return None
    session.selected_route_point_index = index
    return session.route.points[index]


def current_route_point(session: _NavigationSession) -> RoutePoint | None:
    """Return the currently selected route point."""
    if session.route is None:
        return None
    if 0 <= session.selected_route_point_index < len(session.route.points):
        return session.route.points[session.selected_route_point_index]
    return None


def select_target_by_id(
    session: _NavigationSession, target_id: str
) -> MeasurementTarget | None:
    """Select a target by identifier and return it."""
    for index, target in enumerate(session.targets):
        if target.id == target_id:
            session.selected_target_index = index
            return target
    return None


def current_target(session: _NavigationSession) -> MeasurementTarget | None:
    """Return the currently selected target, if any."""
    if 0 <= session.selected_target_index < len(session.targets):
        return session.targets[session.selected_target_index]
    return None


def select_next_target(session: _NavigationSession) -> MeasurementTarget | None:
    """Advance selection to the next target."""
    if not session.targets:
        session.selected_target_index = -1
        return None
    if session.selected_target_index < 0:
        session.selected_target_index = 0
    else:
        session.selected_target_index = min(
            len(session.targets) - 1, session.selected_target_index + 1
        )
    return current_target(session)


def select_previous_target(session: _NavigationSession) -> MeasurementTarget | None:
    """Move selection to the previous target."""
    if not session.targets:
        session.selected_target_index = -1
        return None
    if session.selected_target_index < 0:
        session.selected_target_index = 0
    else:
        session.selected_target_index = max(0, session.selected_target_index - 1)
    return current_target(session)


def design_from_stage(session: _NavigationSession, stage_xy: Point2D) -> Point2D | None:
    """Project a stage-space point into design-space when registered."""
    if session.registration is None or not session.registration.valid:
        return None
    return session.registration.stage_to_design(stage_xy)


def stage_from_design(
    session: _NavigationSession, design_xy: Point2D
) -> Point2D | None:
    """Project a design-space point into stage-space when registered."""
    if session.registration is None or not session.registration.valid:
        return None
    return session.registration.design_to_stage(design_xy)


def selected_target_stage_xy(session: _NavigationSession) -> Point2D | None:
    """Resolve the selected target into stage-space coordinates."""
    target = current_target(session)
    if target is None:
        return None
    return stage_from_design(session, target.design_center)


__all__: list[str] = []
