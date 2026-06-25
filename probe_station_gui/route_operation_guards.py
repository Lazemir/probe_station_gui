"""Pure guard rules for route operation UI actions."""

from __future__ import annotations

from probe_station_gui.route_control_state import ApiRouteControlState


def route_shift_save_block_message(
    *,
    runner_active: bool,
    runner_waiting: bool,
    api_route_control: ApiRouteControlState,
) -> str | None:
    if api_route_control.active:
        runner_active = False
    if not runner_active and not api_route_control.active:
        return "Route measurement is not ready."
    if runner_active and not runner_waiting:
        return "Pause route measurement before saving shift."
    if not runner_active and not api_route_control.paused:
        return "Pause API route control before saving shift."
    return None


def route_contact_move_block_message(
    *,
    route_active: bool,
    route_waiting: bool,
    api_route_control: ApiRouteControlState,
) -> str | None:
    if route_active and not route_waiting:
        return "Pause or wait for route measurement before moving to a contact."
    if api_route_control.blocks_route_adjustment:
        return "Pause API route control before moving to a contact."
    return None


__all__ = [
    "route_contact_move_block_message",
    "route_shift_save_block_message",
]
