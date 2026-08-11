from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import FrozenInstanceError
import importlib
from pathlib import Path

import pytest

from probe_station_gui.design.selection_geometry import PointGeometry
from probe_station_gui.design.selection_model import (
    EntityOwner,
    SelectableDesignEntity,
    route_entity_id,
)
from probe_station_gui.design.tool_session import (
    ArrayToolConfiguration,
    DesignToolContext,
    DesignToolEffectKind,
    DesignToolSession,
)
from probe_station_gui.route.model import (
    MeasurementRoute,
    RouteDesignBinding,
    RoutePoint,
)


ROOT = Path(__file__).resolve().parents[2]


def _route() -> MeasurementRoute:
    return MeasurementRoute(
        name="tool route",
        design=RouteDesignBinding(
            path="design.gds",
            sha256="0" * 64,
            top_cell_name="TOP",
            bounds=(0.0, 0.0, 100.0, 100.0),
            dbu=0.001,
        ),
        points=[
            RoutePoint(
                id="p001",
                label="P001",
                camera_center=(1.0, 2.0),
            )
        ],
    )


def _context(
    *,
    document_token: str = "design-a",
    selected: bool = True,
    route: MeasurementRoute | None = None,
    edit_safe: bool = True,
) -> DesignToolContext:
    route = route if route is not None else _route()
    entity_id = route_entity_id("p001")
    return DesignToolContext.capture(
        document_token=document_token,
        selection_ids={entity_id} if selected else set(),
        selectable_entities=(
            SelectableDesignEntity(
                id=entity_id,
                owner=EntityOwner.ROUTE,
                source_id="p001",
                geometry=PointGeometry((1.0, 2.0)),
                route_index=0,
            ),
        ),
        route=route,
        edit_safe=edit_safe,
    )


def _session(context: DesignToolContext | None = None) -> DesignToolSession:
    transition = DesignToolSession().replace_context(context or _context())
    return transition.session


def _kinds(transition: object) -> list[DesignToolEffectKind]:
    return [effect.kind for effect in transition.effects]


def test_ruler_begin_hover_finish_clear_and_cancel_are_one_immutable_flow() -> None:
    original = _session()
    ruler = original.activate("ruler").session

    started = ruler.pick((1.0, 2.0))
    assert original.active_tool == "select"
    assert started.session.ruler_anchor == (1.0, 2.0)
    assert started.session.measure_preview == ((1.0, 2.0),)
    assert _kinds(started) == [
        DesignToolEffectKind.MEASURE_PREVIEW_CHANGED,
        DesignToolEffectKind.PICK_MODE_CHANGED,
    ]

    hovered = started.session.hover(
        (6.0, 4.0),
        shift=True,
        control=False,
        generation=started.session.context_generation,
    )
    assert hovered.session.measure_preview == ((1.0, 2.0), (6.0, 2.0))
    assert hovered.session.ruler_readout.end_text == "End: X=6.000, Y=2.000"

    finished = hovered.session.pick((6.0, 4.0), shift=True)
    assert finished.session.ruler_anchor is None
    assert finished.session.ruler_segments == (((1.0, 2.0), (6.0, 2.0)),)
    assert finished.session.measure_preview is None
    assert _kinds(finished) == [
        DesignToolEffectKind.MEASUREMENTS_CHANGED,
        DesignToolEffectKind.MEASURE_PREVIEW_CHANGED,
        DesignToolEffectKind.PICK_MODE_CHANGED,
    ]

    next_start = finished.session.pick((9.0, 9.0)).session
    cancelled = next_start.cancel()
    assert cancelled.session.active_tool == "select"
    assert cancelled.session.ruler_anchor is None
    assert cancelled.session.ruler_segments == finished.session.ruler_segments

    cleared = cancelled.session.clear_ruler()
    assert cleared.session.ruler_segments == ()
    assert _kinds(cleared) == [
        DesignToolEffectKind.MEASURE_PREVIEW_CHANGED,
        DesignToolEffectKind.MEASUREMENTS_CHANGED,
    ]


def test_alignment_draft_accept_and_cancel_keep_effect_order_and_copy() -> None:
    alignment = _session().activate("align").session
    first = alignment.append_alignment_point((1.0, 2.0))
    second = first.session.append_alignment_point((5.0, 6.0))
    accepted_points = second.session.alignment_draft

    accepted = second.session.accept_alignment()
    assert accepted.session.active_tool == "select"
    assert accepted.session.alignment_draft == ()
    assert accepted.effects[-1].kind is DesignToolEffectKind.ALIGNMENT_ACCEPTED
    assert accepted.effects[-1].value == accepted_points
    assert _kinds(accepted) == [
        DesignToolEffectKind.ALIGNMENT_CHANGED,
        DesignToolEffectKind.MEASURE_PREVIEW_CHANGED,
        DesignToolEffectKind.ACTIVE_TOOL_CHANGED,
        DesignToolEffectKind.ROUTE_PREVIEW_CHANGED,
        DesignToolEffectKind.MIXED_ARRAY_PREVIEW_CHANGED,
        DesignToolEffectKind.MEASURE_PREVIEW_CHANGED,
        DesignToolEffectKind.ALIGNMENT_ACCEPTED,
    ]

    alignment_again = accepted.session.activate("align").session
    with_point = alignment_again.append_alignment_point((9.0, 3.0)).session
    cancelled = with_point.cancel()
    assert cancelled.session.alignment_draft == ()
    assert cancelled.session.active_tool == "select"
    assert _kinds(cancelled)[:2] == [
        DesignToolEffectKind.ALIGNMENT_CHANGED,
        DesignToolEffectKind.ALIGNMENT_DISCARDED,
    ]


def test_alignment_accept_requires_two_distinct_points() -> None:
    alignment = _session().activate("align").session
    duplicate = alignment.append_alignment_point((1.0, 2.0)).session
    duplicate = duplicate.append_alignment_point((1.0, 2.0)).session

    rejected = duplicate.accept_alignment()

    assert rejected.session is duplicate
    assert rejected.effects == ()
    assert not duplicate.alignment_valid


def test_array_direction_pick_owns_configuration_preview_and_create_intent() -> None:
    activated = _session().activate("array")
    assert _kinds(activated)[:3] == [
        DesignToolEffectKind.MEASURE_PREVIEW_CHANGED,
        DesignToolEffectKind.ACTIVE_TOOL_CHANGED,
        DesignToolEffectKind.MEASURE_PREVIEW_CHANGED,
    ]
    array = activated.session
    assert array.mixed_array_preview.route_points

    armed = array.begin_array_direction("array_dir1")
    anchored = armed.session.pick((0.0, 0.0))
    hovered = anchored.session.hover(
        (4.0, 3.0),
        control=True,
        generation=anchored.session.context_generation,
    )
    assert hovered.session.measure_preview == ((0.0, 0.0), (3.5, 3.5))
    assert hovered.session.array_configuration == array.array_configuration

    committed = hovered.session.pick((4.0, 3.0), control=True)
    config = committed.session.array_configuration
    assert config.direction_1_length == pytest.approx(3.5 * 2**0.5)
    assert config.direction_1_angle_degrees == pytest.approx(45.0)
    assert committed.session.pick_mode is None
    assert committed.session.measure_preview is None

    created = committed.session.create_array()
    assert created.effects[0].kind is DesignToolEffectKind.MIXED_ARRAY_REQUESTED
    assert created.effects[0].value.source_ids == frozenset({route_entity_id("p001")})
    assert created.session.active_tool == "select"


def test_tool_switch_discards_alignment_and_clears_stale_previews() -> None:
    alignment = _session().activate("align").session
    alignment = alignment.append_alignment_point((1.0, 2.0)).session

    switched = alignment.activate("guide")

    assert switched.session.active_tool == "guide"
    assert switched.session.alignment_draft == ()
    assert switched.session.measure_preview is None
    assert switched.session.mixed_array_preview.is_empty
    assert _kinds(switched)[:2] == [
        DesignToolEffectKind.ALIGNMENT_CHANGED,
        DesignToolEffectKind.ALIGNMENT_DISCARDED,
    ]
    assert switched.session.status_message == "Click two points."


def test_context_replacement_replans_preview_and_rejects_stale_pointer_events() -> None:
    array = _session().begin_array_direction("array_dir2").session
    anchored = array.pick((0.0, 0.0)).session
    stale_generation = anchored.context_generation

    replaced = anchored.replace_context(
        _context(document_token="design-b", selected=False)
    )

    assert replaced.session.context_generation == stale_generation + 1
    assert replaced.session.pick_mode == "array_dir2"
    assert replaced.session.pick_anchor == (0.0, 0.0)
    assert replaced.session.measure_preview == ((0.0, 0.0),)
    assert replaced.session.mixed_array_preview.is_empty

    stale = replaced.session.hover(
        (4.0, 3.0),
        generation=stale_generation,
    )
    assert stale.session is replaced.session
    assert stale.effects == ()

    stale_pick = replaced.session.pick(
        (4.0, 3.0),
        generation=stale_generation,
    )
    assert stale_pick.session is replaced.session
    assert stale_pick.effects == ()


def test_equivalent_context_refresh_keeps_in_progress_pick() -> None:
    context = _context()
    array = _session(context).begin_array_direction("array_dir1").session
    anchored = array.pick((0.0, 0.0)).session

    refreshed = anchored.replace_context(context)

    assert refreshed.session is anchored
    assert refreshed.effects == ()
    assert refreshed.session.pick_anchor == (0.0, 0.0)


def test_context_replan_and_tool_switch_retain_durable_tool_values() -> None:
    ruler = _session().activate("ruler").session
    ruler = ruler.pick((0.0, 0.0)).session
    ruler = ruler.pick((2.0, 0.0)).session
    array = ruler.activate("array").session
    configuration = ArrayToolConfiguration(
        direction_1_length=25.0,
        direction_1_angle_degrees=30.0,
        count_1=3,
    )
    array = array.configure_array(configuration).session
    anchored = array.begin_array_direction("array_dir1").session
    anchored = anchored.pick((1.0, 1.0)).session

    locked = anchored.replace_context(_context(edit_safe=False)).session
    assert locked.mixed_array_preview.is_empty
    assert locked.pick_anchor == (1.0, 1.0)
    assert locked.array_configuration == configuration
    assert locked.ruler_segments == (((0.0, 0.0), (2.0, 0.0)),)

    reopened = locked.activate("select").session.activate("array").session
    assert reopened.array_configuration == configuration
    assert reopened.ruler_segments == locked.ruler_segments


def test_document_replacement_silently_clears_alignment_but_keeps_tool() -> None:
    alignment = _session().activate("align").session
    alignment = alignment.append_alignment_point((1.0, 2.0)).session

    replaced = alignment.replace_context(_context(document_token="design-b"))

    assert replaced.session.active_tool == "align"
    assert replaced.session.alignment_draft == ()
    assert _kinds(replaced) == [DesignToolEffectKind.ALIGNMENT_CHANGED]


def test_empty_alignment_cancel_discards_once_and_repeated_cancel_is_stable() -> None:
    alignment = _session().activate("align").session

    cancelled = alignment.cancel()
    repeated = cancelled.session.cancel()

    assert _kinds(cancelled)[0] is DesignToolEffectKind.ALIGNMENT_DISCARDED
    assert DesignToolEffectKind.ALIGNMENT_DISCARDED not in _kinds(repeated)
    assert repeated.session == cancelled.session


def test_captured_context_is_detached_from_later_route_mutation() -> None:
    route = _route()
    session = _session(_context(route=route)).activate("array").session
    original_points = session.mixed_array_preview.route_points

    route.points[0] = RoutePoint("p001", "changed", (99.0, 99.0))
    configured = session.configure_array(ArrayToolConfiguration(count_1=2))

    assert configured.session.mixed_array_preview.route_points
    assert configured.session.mixed_array_preview.route_points != ((99.0, 99.0),)
    assert original_points[0] != (99.0, 99.0)


def test_captured_context_deeply_freezes_nested_route_metadata() -> None:
    route = _route()
    route.points[0].metadata.update(
        {"nested": {"value": 1}, "labels": ["first", "second"]}
    )

    context = _context(route=route)

    assert isinstance(hash(context), int)
    with pytest.raises((AttributeError, TypeError)):
        context._route.points[0].metadata["nested"]["value"] = 7  # type: ignore[index,union-attr]


def test_array_hover_does_not_deepcopy_the_full_route() -> None:
    class CopyCountingMetadata(dict[str, object]):
        copy_count = 0

        def __deepcopy__(self, memo: dict[int, object]) -> CopyCountingMetadata:
            type(self).copy_count += 1
            return type(self)(deepcopy(dict(self), memo))

    route = _route()
    route.points[0] = RoutePoint(
        id="p001",
        label="P001",
        camera_center=(1.0, 2.0),
        metadata=CopyCountingMetadata({"selected": True}),
    )
    route.points.extend(
        RoutePoint(
            id=f"p{index:03d}",
            label=f"P{index:03d}",
            camera_center=(float(index), float(index)),
            metadata=CopyCountingMetadata({"selected": False}),
        )
        for index in range(2, 6)
    )
    array = _session(_context(route=route)).activate("array").session
    anchored = array.begin_array_direction("array_dir1").session
    anchored = anchored.pick((0.0, 0.0)).session
    CopyCountingMetadata.copy_count = 0

    first = anchored.hover(
        (4.0, 3.0),
        generation=anchored.context_generation,
    ).session
    first.hover((5.0, 3.0), generation=first.context_generation)

    assert CopyCountingMetadata.copy_count == 0


def test_tool_session_is_frozen_qt_free_owner_and_panel_has_no_legacy_policy() -> None:
    session = DesignToolSession()
    with pytest.raises(FrozenInstanceError):
        session.active_tool = "array"  # type: ignore[misc]

    owner_sources = [
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "probe_station_gui/design/tool_context.py",
            "probe_station_gui/design/tool_session.py",
        )
    ]
    assert all("PySide6" not in source for source in owner_sources)
    assert all("Qt" not in source for source in owner_sources)

    panel_path = ROOT / "probe_station_gui/views/design_navigator_panel.py"
    panel_source = panel_path.read_text(encoding="utf-8")
    tree = ast.parse(panel_source)
    forbidden_fields = {
        "_active_design_tool",
        "_route_pick_mode",
        "_route_pick_anchor_mode",
        "_route_pick_anchor_point",
        "_ruler_anchor",
        "_ruler_end",
        "_ruler_segments",
        "_alignment_draft_points",
    }
    assigned_attributes = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
    }
    assert forbidden_fields.isdisjoint(assigned_attributes)
    assert "def _set_design_tool(" not in panel_source
    assert "def _route_vector_anchor_or_none(" not in panel_source
    assert "def _alignment_draft_is_valid(" not in panel_source
    assert "constrain_vector_endpoint" not in panel_source
    assert "plan_mixed_array" not in panel_source
    assert "vector_from_length_angle" not in panel_source
    assert "vector_length_angle" not in panel_source
    panel_module = importlib.import_module(
        "probe_station_gui.views.design_navigator_panel"
    )
    assert not hasattr(panel_module, "DesignToolSession")
