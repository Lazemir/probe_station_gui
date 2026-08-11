from __future__ import annotations

from probe_station_gui.design.plot_interaction import (
    ClickEffect,
    GuidePreview,
    MouseButton,
    PlotAction,
    PlotClick,
    PlotInteraction,
    PointerModifiers,
    ScheduleMoveSuppressionClear,
    SelectionPreview,
    SelectionRequest,
    SnapClickIntent,
)
from probe_station_gui.design.model import SnapResult
from probe_station_gui.design.selection_geometry import SegmentGeometry
from probe_station_gui.design.selection_model import (
    EntityOwner,
    SelectableDesignEntity,
)


def test_tool_switch_invalidates_delayed_transient_click() -> None:
    interaction = PlotInteraction()
    interaction.set_context(document_present=True, preview_active=False)
    interaction.set_route_edit_enabled(True)
    interaction.set_tool("point")
    click = interaction.click(
        PlotClick(MouseButton.LEFT, (4.0, 5.0), PointerModifiers())
    )
    intent = next(
        effect for effect in click.effects if isinstance(effect, SnapClickIntent)
    )

    switched = interaction.set_tool("select")

    assert switched.generation > intent.generation
    assert switched.invalidate_transient_snaps


def test_tool_switch_does_not_discard_nontransient_calibration_completion() -> None:
    interaction = PlotInteraction()
    interaction.set_context(document_present=True, preview_active=False)
    click = interaction.click(PlotClick(MouseButton.LEFT, (4.0, 5.0)))
    intent = next(
        effect for effect in click.effects if isinstance(effect, SnapClickIntent)
    )

    interaction.set_tool("select")
    completed = interaction.complete_click(
        intent,
        SnapResult((6.0, 7.0), "vertex", 0.1),
    )

    assert completed.effects == (
        ClickEffect(PlotAction.CALIBRATION, (6.0, 7.0), (0,), points=((6.0, 7.0),)),
    )


def test_click_tool_matrix_matches_existing_product_actions() -> None:
    interaction = PlotInteraction()
    interaction.set_context(document_present=True, preview_active=False)
    cases = (
        ("legacy", False, None, MouseButton.LEFT, PlotAction.CALIBRATION, (0,)),
        ("legacy", False, None, MouseButton.RIGHT, PlotAction.CALIBRATION, (1,)),
        ("legacy", True, None, MouseButton.LEFT, PlotAction.ROUTE_POINT, ()),
        ("point", True, None, MouseButton.LEFT, PlotAction.POINT, ()),
        ("guide", True, None, MouseButton.LEFT, PlotAction.GUIDE_POINT, ()),
        ("align", False, None, MouseButton.LEFT, PlotAction.ALIGNMENT_POINT, ()),
        (
            "array",
            False,
            "array_origin",
            MouseButton.LEFT,
            PlotAction.ROUTE_PICK,
            ("array_origin",),
        ),
    )
    for tool, edit, pick, button, expected_action, expected_payload in cases:
        interaction.set_tool(tool)
        interaction.set_route_edit_enabled(edit)
        interaction.set_route_pick_mode(pick)

        transition = interaction.click(PlotClick(button, (4.0, 5.0)))

        intent = next(
            effect
            for effect in transition.effects
            if isinstance(effect, SnapClickIntent)
        )
        assert (intent.action, intent.payload) == (expected_action, expected_payload)


def test_move_click_requires_navigation_plain_single_click_and_no_drag_suppression() -> (
    None
):
    interaction = PlotInteraction()
    interaction.set_context(document_present=True, preview_active=False)
    interaction.set_tool("move")
    interaction.set_navigation_enabled(True)

    assert (
        interaction.click(PlotClick(MouseButton.LEFT, (1.0, 2.0), double=True)).effects
        == ()
    )
    assert (
        interaction.click(
            PlotClick(MouseButton.LEFT, (1.0, 2.0), PointerModifiers(control=True))
        ).effects
        == ()
    )
    assert (
        interaction.click(
            PlotClick(MouseButton.LEFT, (1.0, 2.0), PointerModifiers(other=True))
        ).effects
        == ()
    )
    interaction.move_press((0.0, 0.0))
    assert interaction.move_active
    interaction.move_motion((20.0, 0.0), drag_threshold=10.0)
    released = interaction.move_release()
    assert isinstance(released.effects[0], ScheduleMoveSuppressionClear)
    assert interaction.click(PlotClick(MouseButton.LEFT, (1.0, 2.0))).effects == ()
    interaction.clear_move_suppression()
    accepted = interaction.click(PlotClick(MouseButton.LEFT, (1.0, 2.0)))
    assert isinstance(accepted.effects[0], SnapClickIntent)


def test_guide_completion_and_hover_constraint_are_owned_by_interaction() -> None:
    interaction = PlotInteraction()
    interaction.set_context(document_present=True, preview_active=False)
    interaction.set_route_edit_enabled(True)
    interaction.set_tool("guide")
    first = SnapClickIntent(
        PlotAction.GUIDE_POINT, (1.0, 2.0), (), False, False, interaction.generation
    )
    second = SnapClickIntent(
        PlotAction.GUIDE_POINT, (6.0, 4.0), (), True, False, interaction.generation
    )

    first_result = interaction.complete_click(
        first, SnapResult((1.0, 2.0), "free", 0.0)
    )
    hover = interaction.hover(SnapResult((6.0, 4.0), "vertex", 0.1), shift=True)
    second_result = interaction.complete_click(
        second, SnapResult((6.0, 4.0), "vertex", 0.1)
    )

    assert first_result.effects == (GuidePreview(((1.0, 2.0),)),)
    assert hover.effects[-1] == GuidePreview(((1.0, 2.0), (6.0, 2.0)))
    click_effect = next(
        effect for effect in second_result.effects if isinstance(effect, ClickEffect)
    )
    assert click_effect.action is PlotAction.GUIDE_POINT
    assert click_effect.points == ((1.0, 2.0), (6.0, 2.0))
    assert interaction.guide_anchor is None


def test_selection_gesture_owns_direction_drag_and_modifier_snapshot() -> None:
    interaction = PlotInteraction()
    interaction.set_context(document_present=True, preview_active=False)
    interaction.set_tool("select")
    crossing = SelectableDesignEntity(
        id="guide:a",
        owner=EntityOwner.MARKUP,
        source_id="a",
        geometry=SegmentGeometry((-2.0, 5.0), (5.0, 5.0)),
    )
    interaction.selection_press(
        scene_point=(10.0, 10.0),
        design_point=(10.0, 10.0),
        hit_entity_id=None,
        modifiers=PointerModifiers(control=True),
    )
    assert interaction.selection_active

    moved = interaction.selection_motion(
        scene_point=(0.0, 0.0),
        design_point=(0.0, 0.0),
        drag_threshold=5.0,
    )
    finished = interaction.selection_release((0.0, 0.0), (crossing,))

    assert moved.effects == (SelectionPreview((10.0, 10.0), (0.0, 0.0), True),)
    assert finished.effects == (
        SelectionPreview(None, None, False),
        SelectionRequest(frozenset({"guide:a"}), "invert"),
    )


def test_cancel_and_preview_context_clear_transient_state_and_reject_stale_generation() -> (
    None
):
    interaction = PlotInteraction()
    interaction.set_context(document_present=True, preview_active=False)
    interaction.set_route_edit_enabled(True)
    interaction.set_tool("guide")
    intent = interaction.click(PlotClick(MouseButton.LEFT, (1.0, 2.0))).effects[0]
    interaction.complete_click(intent, SnapResult((1.0, 2.0), "free", 0.0))

    cancelled = interaction.cancel()
    stale = interaction.complete_click(intent, SnapResult((3.0, 4.0), "vertex", 0.1))
    detached = interaction.set_context(document_present=True, preview_active=True)

    assert cancelled.invalidate_transient_snaps
    assert GuidePreview(()) in cancelled.effects
    assert stale.effects == ()
    assert detached.invalidate_transient_snaps
    assert interaction.click(PlotClick(MouseButton.LEFT, (9.0, 9.0))).effects == ()


def test_document_or_preview_context_change_clears_guide_session() -> None:
    interaction = PlotInteraction()
    interaction.set_context(
        document_present=True,
        preview_active=False,
        document_generation=1,
    )
    interaction.set_route_edit_enabled(True)
    interaction.set_tool("guide")
    intent = interaction.click(PlotClick(MouseButton.LEFT, (1.0, 2.0))).effects[0]
    interaction.complete_click(intent, SnapResult((1.0, 2.0), "free", 0.0))

    preview = interaction.set_context(
        document_present=True,
        preview_active=True,
        document_generation=2,
    )

    assert preview.invalidate_transient_snaps
    assert preview.effects == (GuidePreview(()),)
    assert interaction.guide_anchor is None


def test_cancel_clears_active_selection_preview() -> None:
    interaction = PlotInteraction()
    interaction.set_context(document_present=True, preview_active=False)
    interaction.set_tool("select")
    interaction.selection_press(
        scene_point=(0.0, 0.0),
        design_point=(0.0, 0.0),
        hit_entity_id=None,
        modifiers=PointerModifiers(),
    )
    interaction.selection_motion(
        scene_point=(20.0, 20.0),
        design_point=(20.0, 20.0),
        drag_threshold=5.0,
    )

    cancelled = interaction.cancel()

    assert SelectionPreview(None, None, False) in cancelled.effects
    assert not interaction.selection_active
