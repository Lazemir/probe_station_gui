"""Architecture contract for the split Design navigation modules."""

from probe_station_gui.design import (
    navigation_adapter,
    navigation_targeting,
    route_editing,
)


def test_navigation_workflows_are_owned_by_their_responsibility_modules() -> None:
    assert navigation_adapter.activate_design_frame_for_document.__module__ == (
        "probe_station_gui.design.navigation_adapter"
    )
    assert route_editing.apply_route_entity_changes.__module__ == (
        "probe_station_gui.design.route_editing"
    )
    assert navigation_targeting.plan_design_target_move.__module__ == (
        "probe_station_gui.design.navigation_targeting"
    )


def test_navigation_adapter_is_not_a_compatibility_facade() -> None:
    extracted_names = {
        "RouteEditPlan",
        "apply_route_entity_changes",
        "design_panel_presentation",
        "plan_design_target_move",
    }
    assert extracted_names.isdisjoint(vars(navigation_adapter))
