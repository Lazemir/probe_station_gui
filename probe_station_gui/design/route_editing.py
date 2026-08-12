"""Measurement-route editing policy without Qt side effects."""

from __future__ import annotations

from dataclasses import dataclass

from probe_station_gui.design.model import Point2D
from probe_station_gui.design.selection_model import (
    MixedArrayPlan,
    MixedDeletePlan,
    MixedEditPlan,
)
from probe_station_gui.design.session import DesignSession
from probe_station_gui.design import session_navigation
from probe_station_gui.route.model import MeasurementRoute, RouteModelError, RoutePoint


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


def unload_design_document(session: DesignSession) -> RouteEditPlan:
    if session.document is None:
        return RouteEditPlan(False)
    document_name = session.document.path.name
    session_navigation.unload_document(session)
    return RouteEditPlan(
        True,
        status_message=f"Unloaded design '{document_name}'.",
        status_timeout_ms=5000,
    )


def set_design_top_cell(session: DesignSession, top_cell_name: str) -> RouteEditPlan:
    session_navigation.set_top_cell(session, top_cell_name)
    return RouteEditPlan(
        True,
        status_message=f"Switched design top cell to '{top_cell_name}'.",
        status_timeout_ms=5000,
    )


def create_measurement_route(session: DesignSession) -> RouteEditPlan:
    route = session_navigation.create_route(session)
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
    session_navigation.set_visible_layers(session, visible_layers)
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
    rotated_document = session_navigation.rotate_document(session, delta)
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
    session_navigation.set_route(session, route)
    current_point = session_navigation.current_route_point(session)
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
    point = session_navigation.add_route_point(
        session,
        (float(x_value), float(y_value)),
    )
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
        route = session_navigation.create_route(session)
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
            return RouteEditPlan(
                False, status_message="Selected route points are stale."
            )
        return RouteEditPlan(True, status_message=plan.status_message)
    if not route_change_requested:
        current = session_navigation.current_route_point(session)
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
    current = session_navigation.current_route_point(session)
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
    point = session_navigation.remove_selected_route_point(session)
    if point is None:
        return RouteEditPlan(
            False,
            status_message="No route point is selected.",
            status_timeout_ms=3000,
        )
    current_point = session_navigation.current_route_point(session)
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
    point = session_navigation.select_route_point(session, int(index))
    return RoutePointSelectionPlan(
        selected=point is not None,
        point=point,
        last_selected_design_point=(point.camera_center if point is not None else None),
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
        point = session_navigation.current_route_point(session)
        return RoutePointSelectionPlan(
            False,
            point=point,
            last_selected_design_point=(
                point.camera_center if point is not None else None
            ),
        )
    return select_route_point(session, index)


__all__ = [
    "RouteEditPlan",
    "RoutePointSelectionPlan",
    "add_design_route_point",
    "add_route_array_points",
    "apply_route_entity_changes",
    "clear_measurement_route_points",
    "create_measurement_route",
    "load_measurement_route",
    "remove_selected_route_point",
    "rotate_design_document",
    "save_measurement_route",
    "select_route_point",
    "select_route_point_for_measurement",
    "set_design_layer_visibility",
    "set_design_top_cell",
    "unload_design_document",
]
