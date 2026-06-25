from probe_station_gui.route.control_state import ApiRouteControlState
from probe_station_gui.route.operation_guards import (
    route_contact_move_block_message,
    route_shift_save_block_message,
)


def test_route_shift_save_block_message_preserves_readiness_guards() -> None:
    assert (
        route_shift_save_block_message(
            runner_active=False,
            runner_waiting=False,
            api_route_control=ApiRouteControlState(),
        )
        == "Route measurement is not ready."
    )
    assert (
        route_shift_save_block_message(
            runner_active=True,
            runner_waiting=False,
            api_route_control=ApiRouteControlState(),
        )
        == "Pause route measurement before saving shift."
    )
    assert (
        route_shift_save_block_message(
            runner_active=True,
            runner_waiting=True,
            api_route_control=ApiRouteControlState(),
        )
        is None
    )


def test_route_shift_save_block_message_preserves_api_control_guards() -> None:
    assert (
        route_shift_save_block_message(
            runner_active=True,
            runner_waiting=True,
            api_route_control=ApiRouteControlState(active=True),
        )
        == "Pause API route control before saving shift."
    )
    assert (
        route_shift_save_block_message(
            runner_active=False,
            runner_waiting=False,
            api_route_control=ApiRouteControlState(active=True),
        )
        == "Pause API route control before saving shift."
    )
    assert (
        route_shift_save_block_message(
            runner_active=False,
            runner_waiting=False,
            api_route_control=ApiRouteControlState(active=True, paused=True),
        )
        is None
    )


def test_route_contact_move_block_message_preserves_route_active_guard() -> None:
    assert (
        route_contact_move_block_message(
            route_active=True,
            route_waiting=False,
            api_route_control=ApiRouteControlState(),
        )
        == "Pause or wait for route measurement before moving to a contact."
    )
    assert (
        route_contact_move_block_message(
            route_active=True,
            route_waiting=False,
            api_route_control=ApiRouteControlState(active=True),
        )
        == "Pause or wait for route measurement before moving to a contact."
    )
    assert (
        route_contact_move_block_message(
            route_active=True,
            route_waiting=True,
            api_route_control=ApiRouteControlState(),
        )
        is None
    )


def test_route_contact_move_block_message_preserves_api_control_guard() -> None:
    assert (
        route_contact_move_block_message(
            route_active=False,
            route_waiting=False,
            api_route_control=ApiRouteControlState(active=True),
        )
        == "Pause API route control before moving to a contact."
    )
    assert (
        route_contact_move_block_message(
            route_active=False,
            route_waiting=False,
            api_route_control=ApiRouteControlState(active=True, paused=True),
        )
        is None
    )
