"""Qt-free retained presentation state for the Design plot."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from enum import IntFlag, auto
from typing import Iterable

from probe_station_gui.design.focus_candidate import FocusCandidate
from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.model import DesignDocument, MeasurementTarget, Point2D, SnapResult
from probe_station_gui.design.selection_geometry import SegmentGeometry, guide_snap_candidates
from probe_station_gui.design.selection_model import (
    EntityOwner,
    SelectableDesignEntity,
    SelectionModel,
)
from probe_station_gui.route.model import MeasurementRoute


DesignContentKey = tuple[str, str, int, tuple[float, float, float, float], str | None]
ViewRange = tuple[tuple[float, float], tuple[float, float]]


class DirtyRegion(IntFlag):
    """Render regions invalidated by one presentation transition."""

    NONE = 0
    DOCUMENT = auto()
    NAVIGATION = auto()
    TARGETS = auto()
    ROUTE = auto()
    ROUTE_PREVIEW = auto()
    TOOL_SKETCH = auto()
    TOOL_MEASURE = auto()
    MIXED_PREVIEW = auto()
    ALIGNMENT = auto()
    FOCUS = auto()
    CURRENT = auto()
    REGISTRATION = auto()
    SELECTION = auto()
    HOVER = auto()
    AXIS = auto()

    ALL_OVERLAYS = (
        TARGETS
        | ROUTE
        | ROUTE_PREVIEW
        | TOOL_SKETCH
        | TOOL_MEASURE
        | MIXED_PREVIEW
        | ALIGNMENT
        | FOCUS
        | CURRENT
        | REGISTRATION
        | SELECTION
        | HOVER
        | AXIS
    )
    ALL = DOCUMENT | NAVIGATION | ALL_OVERLAYS


@dataclass(frozen=True)
class NavigationContent:
    document_bounds: tuple[float, float, float, float] | None
    route: MeasurementRoute | None = None
    markup: MarkupDocument | None = None


@dataclass(frozen=True)
class PlotRenderPlan:
    """Immutable snapshot consumed by Qt render adapters."""

    document: DesignDocument | None = None
    preview_active: bool = False
    targets: tuple[MeasurementTarget, ...] = ()
    selected_target_id: str | None = None
    probe_route: MeasurementRoute | None = None
    selected_route_point_index: int = -1
    probe_route_preview_points: tuple[Point2D, ...] = ()
    probe_route_preview_offsets: tuple[Point2D, ...] = ()
    tool_measure_segments: tuple[tuple[Point2D, Point2D], ...] = ()
    tool_measure_points: tuple[Point2D, ...] = ()
    alignment_draft_points: tuple[Point2D, ...] = ()
    tool_sketch_segments: tuple[tuple[Point2D, Point2D], ...] = ()
    tool_sketch_points: tuple[Point2D, ...] = ()
    selectable_entities: tuple[SelectableDesignEntity, ...] = ()
    selection: SelectionModel = SelectionModel()
    selection_managed: bool = False
    mixed_preview_points: tuple[Point2D, ...] = ()
    mixed_preview_segments: tuple[SegmentGeometry, ...] = ()
    source_design_marks: tuple[Point2D | None, Point2D | None] = (None, None)
    current_design_position: Point2D | None = None
    fov_design_size: Point2D | None = None
    check_design_marks: tuple[Point2D, ...] = ()
    focus_candidate: FocusCandidate | None = None
    selected_focus_point: Point2D | None = None
    hover_snap: SnapResult | None = None


@dataclass(frozen=True)
class PresentationChange:
    plan: PlotRenderPlan
    dirty: DirtyRegion
    same_document: bool = False
    same_content: bool = False
    restore_view_range: ViewRange | None = None
    overlay_refresh: bool = True
    markup_changed: bool = False
    markup_snap_candidates: tuple[object, ...] = ()
    route_geometry_changed: bool = False


@dataclass(frozen=True)
class SnapGeometryInstall:
    installed: bool = False
    failure: str | None = None


class PlotPresentation:
    """Own normalized retained state behind an immutable render-plan seam."""

    def __init__(self) -> None:
        self._document: DesignDocument | None = None
        self._targets: tuple[MeasurementTarget, ...] = ()
        self._selected_target_id: str | None = None
        self._probe_route: MeasurementRoute | None = None
        self._probe_route_document_key: DesignContentKey | None = None
        self._selected_route_point_index = -1
        self._probe_route_preview_points: tuple[Point2D, ...] = ()
        self._probe_route_preview_offsets: tuple[Point2D, ...] = ()
        self._tool_measure_segments: tuple[tuple[Point2D, Point2D], ...] = ()
        self._tool_measure_points: tuple[Point2D, ...] = ()
        self._alignment_draft_points: tuple[Point2D, ...] = ()
        self._tool_sketch_segments: tuple[tuple[Point2D, Point2D], ...] = ()
        self._tool_sketch_points: tuple[Point2D, ...] = ()
        self._markup: MarkupDocument | None = None
        self._markup_document_key: DesignContentKey | None = None
        self._selectable_entities: tuple[SelectableDesignEntity, ...] = ()
        self._selection = SelectionModel()
        self._selection_managed = False
        self._mixed_preview_points: tuple[Point2D, ...] = ()
        self._mixed_preview_segments: tuple[SegmentGeometry, ...] = ()
        self._source_design_marks: tuple[Point2D | None, Point2D | None] = (
            None,
            None,
        )
        self._current_design_position: Point2D | None = None
        self._fov_design_size: Point2D | None = None
        self._check_design_marks: tuple[Point2D, ...] = ()
        self._focus_candidate: FocusCandidate | None = None
        self._selected_focus_point: Point2D | None = None
        self._hover_snap: SnapResult | None = None
        self._preview_active = False
        self._preview_previous_document: DesignDocument | None = None
        self._preview_previous_view_range: ViewRange | None = None

    @property
    def document(self) -> DesignDocument | None:
        return self._document

    @property
    def preview_active(self) -> bool:
        return self._preview_active

    @property
    def preview_previous_document(self) -> DesignDocument | None:
        return self._preview_previous_document

    @property
    def selectable_entities(self) -> tuple[SelectableDesignEntity, ...]:
        return self._selectable_entities

    @property
    def selected_focus_point(self) -> Point2D | None:
        return self._selected_focus_point

    @property
    def hover_snap(self) -> SnapResult | None:
        return self._hover_snap

    @property
    def plan(self) -> PlotRenderPlan:
        return PlotRenderPlan(
            document=self._document,
            preview_active=self._preview_active,
            targets=self._targets,
            selected_target_id=self._selected_target_id,
            probe_route=self._probe_route,
            selected_route_point_index=self._selected_route_point_index,
            probe_route_preview_points=self._probe_route_preview_points,
            probe_route_preview_offsets=self._probe_route_preview_offsets,
            tool_measure_segments=self._tool_measure_segments,
            tool_measure_points=self._tool_measure_points,
            alignment_draft_points=self._alignment_draft_points,
            tool_sketch_segments=self._tool_sketch_segments,
            tool_sketch_points=self._tool_sketch_points,
            selectable_entities=self._selectable_entities,
            selection=self._selection,
            selection_managed=self._selection_managed,
            mixed_preview_points=self._mixed_preview_points,
            mixed_preview_segments=self._mixed_preview_segments,
            source_design_marks=self._source_design_marks,
            current_design_position=self._current_design_position,
            fov_design_size=self._fov_design_size,
            check_design_marks=self._check_design_marks,
            focus_candidate=self._focus_candidate,
            selected_focus_point=self._selected_focus_point,
            hover_snap=self._hover_snap,
        )

    def refresh(self) -> PresentationChange:
        return self._change(DirtyRegion.ALL)

    def refresh_overlays(self) -> PresentationChange:
        return self._change(DirtyRegion.ALL_OVERLAYS)

    def set_document(self, document: DesignDocument | None) -> PresentationChange:
        previous_key = _content_key(self._document)
        new_key = _content_key(document)
        same_document = document is self._document
        same_content = (
            self._document is not None
            and document is not None
            and previous_key == new_key
        )
        if not same_document:
            self._focus_candidate = None
            self._selected_focus_point = None
        self._document = document
        dirty = DirtyRegion.ALL_OVERLAYS
        if not same_document:
            dirty |= DirtyRegion.DOCUMENT | DirtyRegion.NAVIGATION
        return self._change(
            dirty,
            same_document=same_document,
            same_content=same_content,
            route_geometry_changed=not same_document,
        )

    def begin_preview(
        self,
        document: DesignDocument,
        view_range: ViewRange | None,
    ) -> PresentationChange:
        self.start_preview(view_range)
        change = self.set_document(document)
        return self._copy_change(change, dirty=change.dirty | DirtyRegion.NAVIGATION)

    def finish_preview(
        self,
        document: DesignDocument | None,
    ) -> PresentationChange:
        restore = self.end_preview(document)
        change = self.set_document(document)
        self.clear_preview_history()
        return self._copy_change(
            change,
            dirty=change.dirty | DirtyRegion.NAVIGATION,
            restore_view_range=restore,
        )

    def start_preview(self, view_range: ViewRange | None) -> None:
        if not self._preview_active:
            self._preview_previous_document = self._document
            self._preview_previous_view_range = view_range
        self._preview_active = True

    def end_preview(self, document: DesignDocument | None) -> ViewRange | None:
        restore = (
            self._preview_previous_view_range
            if document is self._preview_previous_document
            else None
        )
        self._preview_active = False
        return restore

    def clear_preview_history(self) -> None:
        self._preview_previous_document = None
        self._preview_previous_view_range = None

    def navigation_content(self) -> NavigationContent:
        if self._document is None:
            return NavigationContent(None)
        identity = _content_key(self._document)
        return NavigationContent(
            self._document.bounds,
            route=(
                self._probe_route
                if not self._preview_active
                and self._probe_route_document_key == identity
                else None
            ),
            markup=(
                self._markup
                if not self._preview_active
                and self._markup_document_key == identity
                else None
            ),
        )

    def set_targets(
        self,
        targets: Iterable[MeasurementTarget],
        *,
        selected_target_id: str | None,
    ) -> PresentationChange:
        self._targets = tuple(targets)
        self._selected_target_id = selected_target_id
        return self._change(DirtyRegion.TARGETS)

    def set_probe_route(
        self,
        route: MeasurementRoute | None,
        *,
        selected_route_point_index: int,
    ) -> PresentationChange:
        self._probe_route = route
        self._selected_route_point_index = int(selected_route_point_index)
        self._probe_route_document_key = (
            _content_key(self._document)
            if self._document is not None
            and route is not None
            and _route_matches_document(route, self._document)
            else None
        )
        return self._change(
            DirtyRegion.ROUTE | DirtyRegion.NAVIGATION,
            route_geometry_changed=True,
        )

    def set_probe_route_preview(self, preview: object) -> PresentationChange:
        if (
            isinstance(preview, tuple)
            and len(preview) == 2
            and isinstance(preview[0], list)
            and isinstance(preview[1], list)
        ):
            self._probe_route_preview_points = _points(preview[0])
            self._probe_route_preview_offsets = _points(preview[1], limit=2)
        else:
            self._probe_route_preview_points = ()
            self._probe_route_preview_offsets = ()
        return self._change(
            DirtyRegion.ROUTE_PREVIEW,
            route_geometry_changed=True,
        )

    def set_markup(self, markup: MarkupDocument | None) -> PresentationChange:
        changed = markup != self._markup
        self._markup = markup
        self._markup_document_key = (
            _content_key(self._document)
            if self._document is not None
            and markup is not None
            and _markup_matches_document(markup, self._document)
            else None
        )
        snap_candidates: tuple[object, ...] = ()
        if markup is None or not markup.visible:
            self._tool_sketch_segments = ()
            self._selectable_entities = tuple(
                entity
                for entity in self._selectable_entities
                if entity.owner is not EntityOwner.MARKUP
            )
            self._selection = self._selection.prune(
                entity.id for entity in self._selectable_entities
            )
        else:
            geometries = tuple(guide.geometry() for guide in markup.guides)
            self._tool_sketch_segments = tuple(
                (geometry.start, geometry.end) for geometry in geometries
            )
            snap_candidates = tuple(guide_snap_candidates(geometries))
        return self._change(
            DirtyRegion.TOOL_SKETCH | DirtyRegion.SELECTION | DirtyRegion.NAVIGATION,
            markup_changed=changed,
            markup_snap_candidates=snap_candidates,
        )

    def set_selectable_entities(self, entities: object) -> PresentationChange:
        if isinstance(entities, (list, tuple)) and all(
            isinstance(entity, SelectableDesignEntity) for entity in entities
        ):
            self._selectable_entities = tuple(entities)
        else:
            self._selectable_entities = ()
        self._selection = self._selection.prune(
            entity.id for entity in self._selectable_entities
        )
        return self._change(DirtyRegion.SELECTION | DirtyRegion.TOOL_SKETCH)

    def set_selection(self, selection: SelectionModel) -> PresentationChange:
        self._selection = selection.prune(
            entity.id for entity in self._selectable_entities
        )
        self._selection_managed = True
        return self._change(DirtyRegion.SELECTION | DirtyRegion.ROUTE)

    def set_tool_measure_points(self, points: object) -> PresentationChange:
        self._tool_measure_points = _points(points, limit=2, require_list=True)
        return self._change(DirtyRegion.TOOL_MEASURE)

    def set_tool_measure_segments(self, segments: object) -> PresentationChange:
        self._tool_measure_segments = _segments(segments, require_list=True)
        return self._change(DirtyRegion.TOOL_MEASURE)

    def set_tool_sketch_points(self, points: object) -> PresentationChange:
        self._tool_sketch_points = _points(points, limit=2, require_list=True)
        return self._change(DirtyRegion.TOOL_SKETCH)

    def set_guide_preview(self, points: Iterable[Point2D]) -> PresentationChange:
        self._tool_sketch_points = tuple(points)
        return self._change(DirtyRegion.TOOL_SKETCH, overlay_refresh=False)

    def set_tool_sketch_segments(self, segments: object) -> PresentationChange:
        self._tool_sketch_segments = _segments(segments, require_list=True)
        return self._change(DirtyRegion.TOOL_SKETCH)

    def set_mixed_array_preview(
        self,
        points: object,
        segments: object,
    ) -> PresentationChange:
        self._mixed_preview_points = _points(points)
        self._mixed_preview_segments = (
            tuple(
                segment
                for segment in segments
                if isinstance(segment, SegmentGeometry)
            )
            if isinstance(segments, (list, tuple))
            else ()
        )
        return self._change(DirtyRegion.MIXED_PREVIEW, overlay_refresh=False)

    def set_source_design_marks(
        self,
        points: list[Point2D | None],
    ) -> PresentationChange:
        normalized = list(points[:2])
        while len(normalized) < 2:
            normalized.append(None)
        self._source_design_marks = (normalized[0], normalized[1])
        return self._change(DirtyRegion.REGISTRATION)

    def set_alignment_draft_points(self, points: object) -> PresentationChange:
        self._alignment_draft_points = _points(points)
        return self._change(DirtyRegion.ALIGNMENT, overlay_refresh=False)

    def set_current_design_position(
        self,
        point: Point2D | None,
        *,
        fov_design_size: Point2D | None = None,
    ) -> PresentationChange:
        self._current_design_position = point
        self._fov_design_size = fov_design_size
        return self._change(DirtyRegion.CURRENT, overlay_refresh=False)

    def set_registration_marks(
        self,
        source_design_marks: list[Point2D | None],
        check_design_marks: list[Point2D],
    ) -> PresentationChange:
        normalized = list(source_design_marks[:2])
        while len(normalized) < 2:
            normalized.append(None)
        self._source_design_marks = (normalized[0], normalized[1])
        self._check_design_marks = tuple(check_design_marks)
        return self._change(DirtyRegion.REGISTRATION)

    def set_focus_candidate(
        self,
        candidate: FocusCandidate | None,
    ) -> PresentationChange:
        self._focus_candidate = candidate
        return self._change(DirtyRegion.FOCUS, overlay_refresh=False)

    def set_selected_focus_point(
        self,
        point: Point2D | None,
    ) -> PresentationChange:
        self._selected_focus_point = (
            None if point is None else (float(point[0]), float(point[1]))
        )
        return self._change(DirtyRegion.FOCUS, overlay_refresh=False)

    def set_hover(self, snap_result: SnapResult | None) -> PresentationChange:
        self._hover_snap = snap_result
        return self._change(DirtyRegion.HOVER, overlay_refresh=False)

    def geometry_snap_result(
        self,
        raw_point: Point2D,
        *,
        max_distance: float | None,
    ) -> SnapResult | None:
        document = self._document
        if (
            document is None
            or document.file_backed
            or not document.has_snap_geometry()
        ):
            return None
        return document.snap_point_info(raw_point, max_distance=max_distance)

    def install_snap_geometry(
        self,
        document: object,
        geometry: object,
        result: object,
    ) -> SnapGeometryInstall:
        if document is not self._document:
            return SnapGeometryInstall()
        if result is not None and not isinstance(result, (int, float)):
            return SnapGeometryInstall(failure=str(result))
        if (
            not isinstance(document, DesignDocument)
            or not isinstance(geometry, tuple)
            or len(geometry) < 3
        ):
            return SnapGeometryInstall()
        document.set_snap_geometry(*geometry)
        return SnapGeometryInstall(installed=True)

    def hit_entity(
        self,
        point: Point2D,
        *,
        tolerance: float | None,
    ) -> SelectableDesignEntity | None:
        if tolerance is None:
            return None
        hits = tuple(
            entity
            for entity in self._selectable_entities
            if entity.hit_test(point, tolerance=tolerance)
        )
        if not hits:
            return None
        return min(hits, key=lambda entity: entity.owner is not EntityOwner.ROUTE)

    def _change(
        self,
        dirty: DirtyRegion,
        *,
        same_document: bool = False,
        same_content: bool = False,
        restore_view_range: ViewRange | None = None,
        overlay_refresh: bool = True,
        markup_changed: bool = False,
        markup_snap_candidates: tuple[object, ...] = (),
        route_geometry_changed: bool = False,
    ) -> PresentationChange:
        return PresentationChange(
            self.plan,
            dirty,
            same_document=same_document,
            same_content=same_content,
            restore_view_range=restore_view_range,
            overlay_refresh=overlay_refresh,
            markup_changed=markup_changed,
            markup_snap_candidates=markup_snap_candidates,
            route_geometry_changed=route_geometry_changed,
        )

    @staticmethod
    def _copy_change(
        change: PresentationChange,
        *,
        dirty: DirtyRegion,
        restore_view_range: ViewRange | None = None,
    ) -> PresentationChange:
        return PresentationChange(
            change.plan,
            dirty,
            same_document=change.same_document,
            same_content=change.same_content,
            restore_view_range=restore_view_range,
            overlay_refresh=change.overlay_refresh,
            markup_changed=change.markup_changed,
            markup_snap_candidates=change.markup_snap_candidates,
            route_geometry_changed=change.route_geometry_changed,
        )


def _content_key(document: DesignDocument | None) -> DesignContentKey | None:
    if document is None:
        return None
    return (
        os.path.normcase(os.path.abspath(os.fspath(document.path))),
        str(document.top_cell_name),
        int(document.rotation_quarter_turns) % 4,
        tuple(float(value) for value in document.bounds),
        document.source_load_id,
    )


def _route_matches_document(
    route: MeasurementRoute,
    document: DesignDocument,
) -> bool:
    same_path = os.path.normcase(os.path.abspath(route.design.path)) == os.path.normcase(
        os.path.abspath(os.fspath(document.path))
    )
    return (
        same_path
        and route.design.top_cell_name == str(document.top_cell_name)
        and all(
            math.isclose(route_value, document_value, rel_tol=0.0, abs_tol=1e-9)
            for route_value, document_value in zip(
                route.design.bounds,
                document.bounds,
                strict=True,
            )
        )
        and math.isclose(route.design.dbu, document.dbu, rel_tol=0.0, abs_tol=1e-12)
    )


def _markup_matches_document(
    markup: MarkupDocument,
    document: DesignDocument,
) -> bool:
    return markup.source_path == os.path.normcase(
        os.path.abspath(os.fspath(document.path))
    )


def _points(
    values: object,
    *,
    limit: int | None = None,
    require_list: bool = False,
) -> tuple[Point2D, ...]:
    if require_list and not isinstance(values, list):
        return ()
    if not isinstance(values, (list, tuple)):
        return ()
    source = values if limit is None else values[:limit]
    return tuple(
        (float(point[0]), float(point[1]))
        for point in source
        if isinstance(point, (list, tuple)) and len(point) == 2
    )


def _segments(
    values: object,
    *,
    require_list: bool = False,
) -> tuple[tuple[Point2D, Point2D], ...]:
    if require_list and not isinstance(values, list):
        return ()
    if not isinstance(values, (list, tuple)):
        return ()
    segments: list[tuple[Point2D, Point2D]] = []
    for value in values:
        if (
            isinstance(value, (list, tuple))
            and len(value) == 2
            and isinstance(value[0], (list, tuple))
            and isinstance(value[1], (list, tuple))
            and len(value[0]) == 2
            and len(value[1]) == 2
        ):
            segments.append(
                (
                    (float(value[0][0]), float(value[0][1])),
                    (float(value[1][0]), float(value[1][1])),
                )
            )
    return tuple(segments)


__all__ = [
    "DesignContentKey",
    "DirtyRegion",
    "NavigationContent",
    "PlotPresentation",
    "PlotRenderPlan",
    "PresentationChange",
    "ViewRange",
]
