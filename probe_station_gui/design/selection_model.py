"""Shared selectable projections and pure mixed design edit plans."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Collection, Iterable
import uuid

from probe_station_gui.design.markup import GuideSegment, MarkupDocument
from probe_station_gui.design.selection_geometry import (
    Geometry,
    PointGeometry,
    SegmentGeometry,
    SelectionRect,
)
from probe_station_gui.route.model import MeasurementRoute, RoutePoint


class EntityOwner(str, Enum):
    ROUTE = "route"
    MARKUP = "markup"


@dataclass(frozen=True)
class SelectableDesignEntity:
    id: str
    owner: EntityOwner
    source_id: str
    geometry: Geometry
    route_index: int | None = None

    def hit_test(self, point: tuple[float, float], *, tolerance: float) -> bool:
        return self.geometry.hit_test(point, tolerance=tolerance)

    def contained_by(self, rect: SelectionRect) -> bool:
        return self.geometry.contained_by(rect)

    def crosses(self, rect: SelectionRect) -> bool:
        return self.geometry.crosses(rect)

    def translated(self, dx: float, dy: float) -> SelectableDesignEntity:
        return SelectableDesignEntity(
            id=self.id,
            owner=self.owner,
            source_id=self.source_id,
            geometry=self.geometry.translated(dx, dy),
            route_index=self.route_index,
        )


@dataclass(frozen=True)
class SelectionModel:
    ids: frozenset[str] = frozenset()

    def replace(self, entity_ids: Iterable[str]) -> SelectionModel:
        return SelectionModel(frozenset(entity_ids))

    def add(self, entity_ids: Iterable[str]) -> SelectionModel:
        return SelectionModel(self.ids | frozenset(entity_ids))

    def invert(self, entity_ids: Iterable[str]) -> SelectionModel:
        return SelectionModel(self.ids ^ frozenset(entity_ids))

    def clear(self) -> SelectionModel:
        return SelectionModel()

    def prune(self, valid_ids: Iterable[str]) -> SelectionModel:
        return SelectionModel(self.ids & frozenset(valid_ids))

    def apply(self, entity_ids: Iterable[str], mode: str) -> SelectionModel:
        if mode == "replace":
            return self.replace(entity_ids)
        if mode == "add":
            return self.add(entity_ids)
        if mode == "invert":
            return self.invert(entity_ids)
        raise ValueError(f"Unknown selection update mode {mode!r}.")


@dataclass(frozen=True)
class MixedArrayRequest:
    direction_1: tuple[float, float]
    count_1: int
    direction_2: tuple[float, float]
    count_2: int
    serpentine: bool = False
    source_ids: frozenset[str] = frozenset()


@dataclass(frozen=True)
class MixedDeletePlan:
    accepted: bool
    required_route_ids: frozenset[str] = frozenset()
    required_guide_ids: frozenset[str] = frozenset()
    route_remove_ids: frozenset[str] = frozenset()
    guide_remove_ids: frozenset[str] = frozenset()
    status_message: str = ""


@dataclass(frozen=True)
class MixedArrayPlan:
    accepted: bool
    required_route_ids: frozenset[str] = frozenset()
    required_guide_ids: frozenset[str] = frozenset()
    route_copies: tuple[RoutePoint, ...] = ()
    guide_copies: tuple[GuideSegment, ...] = ()
    status_message: str = ""


MixedEditPlan = MixedDeletePlan | MixedArrayPlan


def route_entity_id(route_point_id: str) -> str:
    return f"route:{route_point_id}"


def markup_entity_id(guide_id: str) -> str:
    return f"markup:{guide_id}"


def project_entities(
    route: MeasurementRoute | None,
    markup: MarkupDocument | None,
) -> tuple[SelectableDesignEntity, ...]:
    entities: list[SelectableDesignEntity] = []
    if route is not None:
        entities.extend(
            SelectableDesignEntity(
                id=route_entity_id(point.id),
                owner=EntityOwner.ROUTE,
                source_id=point.id,
                geometry=PointGeometry(point.camera_center),
                route_index=index,
            )
            for index, point in enumerate(route.points)
        )
    if markup is not None and markup.visible:
        entities.extend(
            SelectableDesignEntity(
                id=markup_entity_id(guide.id),
                owner=EntityOwner.MARKUP,
                source_id=guide.id,
                geometry=SegmentGeometry(guide.start, guide.end),
            )
            for guide in markup.guides
        )
    return tuple(entities)


def entities_in_rect(
    entities: Iterable[SelectableDesignEntity],
    rect: SelectionRect,
    *,
    crossing: bool,
) -> set[str]:
    predicate = "crosses" if crossing else "contained_by"
    return {
        entity.id
        for entity in entities
        if getattr(entity, predicate)(rect)
    }


def plan_mixed_delete(
    entities: Iterable[SelectableDesignEntity],
    selected_ids: Collection[str],
    *,
    edit_safe: bool,
) -> MixedDeletePlan:
    selected, error = _selected_entities(entities, selected_ids, edit_safe=edit_safe)
    if error:
        return MixedDeletePlan(False, status_message=error)
    route_ids = frozenset(
        entity.source_id for entity in selected if entity.owner is EntityOwner.ROUTE
    )
    guide_ids = frozenset(
        entity.source_id for entity in selected if entity.owner is EntityOwner.MARKUP
    )
    return MixedDeletePlan(
        True,
        required_route_ids=route_ids,
        required_guide_ids=guide_ids,
        route_remove_ids=route_ids,
        guide_remove_ids=guide_ids,
        status_message=f"Deleted {len(selected)} selected entities.",
    )


def plan_mixed_array(
    entities: Iterable[SelectableDesignEntity],
    selected_ids: Collection[str],
    request: MixedArrayRequest,
    *,
    route: MeasurementRoute | None,
    edit_safe: bool,
) -> MixedArrayPlan:
    materialized = tuple(entities)
    selected, error = _selected_entities(
        materialized,
        selected_ids,
        edit_safe=edit_safe,
    )
    if error:
        return MixedArrayPlan(False, status_message=error)
    parameter_error = _array_parameter_error(request)
    if parameter_error:
        return MixedArrayPlan(False, status_message=parameter_error)
    route_entities = sorted(
        (entity for entity in selected if entity.owner is EntityOwner.ROUTE),
        key=lambda entity: entity.route_index if entity.route_index is not None else -1,
    )
    guide_entities = [
        entity for entity in selected if entity.owner is EntityOwner.MARKUP
    ]
    route_by_id = {point.id: point for point in route.points} if route is not None else {}
    if route_entities and route is None:
        return MixedArrayPlan(False, status_message="Selected route points are stale.")
    offsets = _array_offsets(request)
    if not offsets:
        return MixedArrayPlan(False, status_message="Array has no copy cells.")
    planned_route_points = list(route.points) if route is not None else []
    route_copies: list[RoutePoint] = []
    guide_copies: list[GuideSegment] = []
    guide_by_id = {
        entity.source_id: entity.geometry
        for entity in guide_entities
        if isinstance(entity.geometry, SegmentGeometry)
    }
    for dx, dy in offsets:
        for entity in route_entities:
            source = route_by_id.get(entity.source_id)
            if source is None:
                return MixedArrayPlan(
                    False,
                    status_message="Selected route points are stale.",
                )
            copied = _copy_route_point(source, dx, dy, planned_route_points)
            planned_route_points.append(copied)
            route_copies.append(copied)
        for entity in guide_entities:
            geometry = guide_by_id[entity.source_id].translated(dx, dy)
            guide_copies.append(
                GuideSegment(
                    id=_new_guide_id(),
                    start=geometry.start,
                    end=geometry.end,
                )
            )
    return MixedArrayPlan(
        True,
        required_route_ids=frozenset(entity.source_id for entity in route_entities),
        required_guide_ids=frozenset(entity.source_id for entity in guide_entities),
        route_copies=tuple(route_copies),
        guide_copies=tuple(guide_copies),
        status_message=(
            f"Created {len(route_copies)} route points and "
            f"{len(guide_copies)} guide segments."
        ),
    )


def apply_markup_entity_changes(
    markup: MarkupDocument,
    plan: MixedEditPlan,
) -> MarkupDocument:
    current_ids = {guide.id for guide in markup.guides}
    if not plan.accepted or not plan.required_guide_ids <= current_ids:
        raise ValueError("Selected guide segments are stale.")
    if isinstance(plan, MixedDeletePlan):
        return markup.remove_ids(plan.guide_remove_ids)
    return markup.append_guides(plan.guide_copies)


def _selected_entities(
    entities: Iterable[SelectableDesignEntity],
    selected_ids: Collection[str],
    *,
    edit_safe: bool,
) -> tuple[list[SelectableDesignEntity], str]:
    if not edit_safe:
        return [], "Design editing is locked."
    wanted = frozenset(selected_ids)
    if not wanted:
        return [], "Select at least one entity."
    by_id = {entity.id: entity for entity in entities}
    if not wanted <= by_id.keys():
        return [], "Selection is stale."
    return [entity for entity in entities if entity.id in wanted], ""


def _array_parameter_error(request: MixedArrayRequest) -> str:
    if request.count_1 <= 0 or request.count_2 <= 0:
        return "Array counts must be positive."
    vectors = (*request.direction_1, *request.direction_2)
    if not all(math.isfinite(float(value)) for value in vectors):
        return "Array directions must be finite."
    if request.count_1 > 1 and _is_zero_vector(request.direction_1):
        return "Array direction 1 must be non-zero."
    if request.count_2 > 1 and _is_zero_vector(request.direction_2):
        return "Array direction 2 must be non-zero."
    return ""


def _array_offsets(request: MixedArrayRequest) -> tuple[tuple[float, float], ...]:
    offsets: list[tuple[float, float]] = []
    for second_index in range(request.count_2):
        if request.serpentine and second_index % 2:
            first_indices = range(request.count_1 - 1, -1, -1)
        else:
            first_indices = range(request.count_1)
        for first_index in first_indices:
            if first_index == 0 and second_index == 0:
                continue
            offsets.append(
                (
                    request.direction_1[0] * first_index
                    + request.direction_2[0] * second_index,
                    request.direction_1[1] * first_index
                    + request.direction_2[1] * second_index,
                )
            )
    return tuple(offsets)


def _copy_route_point(
    source: RoutePoint,
    dx: float,
    dy: float,
    existing: Collection[RoutePoint],
) -> RoutePoint:
    point_id = _next_route_point_id(existing)
    metadata = dict(source.metadata)
    metadata["array_source_point_id"] = source.id
    return RoutePoint(
        id=point_id,
        label=f"P{len(existing) + 1:03d}",
        camera_center=(source.camera_center[0] + dx, source.camera_center[1] + dy),
        metadata=metadata,
    )


def _next_route_point_id(existing: Collection[RoutePoint]) -> str:
    existing_ids = {point.id for point in existing}
    index = len(existing) + 1
    while f"p{index:03d}" in existing_ids:
        index += 1
    return f"p{index:03d}"


def _new_guide_id() -> str:
    return uuid.uuid4().hex


def _is_zero_vector(vector: tuple[float, float]) -> bool:
    return abs(vector[0]) <= 1e-12 and abs(vector[1]) <= 1e-12


__all__ = [
    "EntityOwner",
    "MixedArrayPlan",
    "MixedArrayRequest",
    "MixedDeletePlan",
    "MixedEditPlan",
    "SelectableDesignEntity",
    "SelectionModel",
    "apply_markup_entity_changes",
    "entities_in_rect",
    "markup_entity_id",
    "plan_mixed_array",
    "plan_mixed_delete",
    "project_entities",
    "route_entity_id",
]
