from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.design.plot_presentation import DirtyRegion, PlotPresentation
from probe_station_gui.design.selection_geometry import PointGeometry, SegmentGeometry
from probe_station_gui.design.selection_model import (
    EntityOwner,
    SelectableDesignEntity,
    SelectionModel,
)
from probe_station_gui.route.model import MeasurementRoute, RoutePoint


def _document(
    path: Path,
    *,
    rotation: int = 0,
    source_load_id: str | None = None,
) -> DesignDocument:
    path.write_bytes(b"gds")
    return DesignDocument(
        path=path,
        library=None,
        top_cell=None,
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-6,
        user_unit=1e-9,
        bounds=(0.0, 0.0, 100.0, 50.0),
        polygons_by_layer={},
        visible_layers=frozenset({(1, 0)}),
        rotation_quarter_turns=rotation,
        source_load_id=source_load_id,
    )


def test_document_change_distinguishes_object_identity_from_render_content(
    tmp_path: Path,
) -> None:
    presentation = PlotPresentation()
    document = _document(tmp_path / "chip.gds", source_load_id="load-a")

    first = presentation.set_document(document)
    same_object = presentation.set_document(document)
    same_content = presentation.set_document(
        replace(document, visible_layers=frozenset({(2, 0)}))
    )
    new_content = presentation.set_document(
        replace(document, source_load_id="load-b")
    )

    assert first.same_document is False
    assert first.same_content is False
    assert same_object.same_document is True
    assert same_object.same_content is True
    assert same_content.same_document is False
    assert same_content.same_content is True
    assert new_content.same_content is False
    assert new_content.dirty & DirtyRegion.DOCUMENT


def test_nested_preview_captures_once_and_restores_only_original_object(
    tmp_path: Path,
) -> None:
    presentation = PlotPresentation()
    original = _document(tmp_path / "original.gds")
    first_preview = _document(tmp_path / "first.gds")
    second_preview = _document(tmp_path / "second.gds")
    saved_range = ((1.0, 11.0), (2.0, 12.0))
    ignored_nested_range = ((20.0, 30.0), (40.0, 50.0))
    presentation.set_document(original)

    presentation.begin_preview(first_preview, saved_range)
    presentation.begin_preview(second_preview, ignored_nested_range)
    same_content_clone = replace(original)
    clone_finish = presentation.finish_preview(same_content_clone)

    assert clone_finish.restore_view_range is None
    assert presentation.plan.preview_active is False

    presentation.set_document(original)
    presentation.begin_preview(first_preview, saved_range)
    identity_finish = presentation.finish_preview(original)

    assert identity_finish.restore_view_range == saved_range
    assert identity_finish.plan.document is original


def test_navigation_content_uses_only_matching_route_and_markup_outside_preview(
    tmp_path: Path,
) -> None:
    presentation = PlotPresentation()
    document = _document(tmp_path / "matching.gds")
    route = MeasurementRoute.default_for_document(document)
    route.points.append(RoutePoint("far", "Far", (1_000.0, 0.0)))
    markup = MarkupDocument.empty(document.path).append_guide(
        (0.0, 0.0),
        (2_000.0, 0.0),
        guide_id="far-guide",
    )
    presentation.set_document(document)
    presentation.set_probe_route(route, selected_route_point_index=-1)
    presentation.set_markup(markup)

    normal = presentation.navigation_content()
    presentation.begin_preview(_document(tmp_path / "preview.gds"), None)
    preview = presentation.navigation_content()

    assert normal.route is route
    assert normal.markup is markup
    assert preview.route is None
    assert preview.markup is None


def test_selection_is_pruned_and_hit_testing_prefers_route_entities() -> None:
    presentation = PlotPresentation()
    markup_entity = SelectableDesignEntity(
        id="markup:g1",
        owner=EntityOwner.MARKUP,
        source_id="g1",
        geometry=SegmentGeometry((-5.0, 0.0), (5.0, 0.0)),
    )
    route_entity = SelectableDesignEntity(
        id="route:p1",
        owner=EntityOwner.ROUTE,
        source_id="p1",
        geometry=PointGeometry((0.0, 0.0)),
        route_index=0,
    )

    presentation.set_selectable_entities((markup_entity, route_entity))
    change = presentation.set_selection(
        SelectionModel(frozenset({markup_entity.id, route_entity.id, "stale"}))
    )

    assert change.plan.selection.ids == frozenset(
        {markup_entity.id, route_entity.id}
    )
    assert presentation.hit_entity((0.0, 0.0), tolerance=1.0) is route_entity


def test_snap_geometry_completion_is_validated_by_presentation_owner(
    tmp_path: Path,
) -> None:
    presentation = PlotPresentation()
    document = _document(tmp_path / "snap.gds")
    presentation.set_document(document)

    failure = presentation.install_snap_geometry(
        document, (), RuntimeError("build failed")
    )
    stale = presentation.install_snap_geometry(
        _document(tmp_path / "stale.gds"), (), None
    )

    assert failure.failure == "build failed"
    assert failure.installed is False
    assert stale.failure is None
    assert stale.installed is False


def test_point_and_segment_inputs_are_normalized_into_immutable_render_plan() -> None:
    presentation = PlotPresentation()

    points_change = presentation.set_tool_measure_points([(1, 2), [3.0, 4.0], (5, 6)])
    segments_change = presentation.set_tool_measure_segments(
        [((0, 1), [2, 3]), "bad", ((4.0, 5.0), (6.0, 7.0))]
    )

    assert points_change.plan.tool_measure_points == ((1.0, 2.0), (3.0, 4.0))
    assert segments_change.plan.tool_measure_segments == (
        ((0.0, 1.0), (2.0, 3.0)),
        ((4.0, 5.0), (6.0, 7.0)),
    )
    assert segments_change.dirty == DirtyRegion.TOOL_MEASURE
