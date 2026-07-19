"""Enablement policy for the design navigator panel."""

from __future__ import annotations

from dataclasses import dataclass

from probe_station_gui.route.run_ui import RouteRunControlPresentation


@dataclass(frozen=True)
class DesignNavigatorEnablement:
    has_document: bool
    has_route: bool
    route_saved: bool
    route_has_points: bool
    has_route_selection: bool
    has_current_design_position: bool
    route_running: bool
    design_registration_active: bool
    route_control: RouteRunControlPresentation
    design_load_pending: bool = False

    @property
    def can_edit_design(self) -> bool:
        return (
            self.has_document
            and not self.route_running
            and not self.design_load_pending
        )

    @property
    def can_use_document_controls(self) -> bool:
        return self.has_document and not self.design_load_pending

    @property
    def can_save_route(self) -> bool:
        return (
            self.has_route
            and self.route_saved
            and not self.route_running
            and not self.design_load_pending
        )

    @property
    def can_save_route_as(self) -> bool:
        return (
            self.has_route
            and not self.route_running
            and not self.design_load_pending
        )

    @property
    def can_use_rotate_tool(self) -> bool:
        return self.can_edit_design and not self.design_registration_active

    @property
    def can_move_design(self) -> bool:
        return self.can_edit_design and self.design_registration_active

    @property
    def can_edit_route_offsets(self) -> bool:
        return (
            self.has_route
            and not self.route_running
            and not self.design_load_pending
        )

    @property
    def can_add_current_route_point(self) -> bool:
        return (
            self.has_route
            and self.has_current_design_position
            and not self.route_running
            and not self.design_load_pending
        )

    @property
    def can_remove_route_point(self) -> bool:
        return (
            self.has_route_selection
            and not self.route_running
            and not self.design_load_pending
        )

    @property
    def can_clear_route(self) -> bool:
        return (
            self.has_route
            and self.route_has_points
            and not self.route_running
            and not self.design_load_pending
        )

    @property
    def can_run_selected(self) -> bool:
        return self.has_route_selection and (
            self.route_running or not self.design_load_pending
        )

    @property
    def can_confirm_waiting(self) -> bool:
        return self.route_control.can_confirm_waiting

    @property
    def can_save_shift(self) -> bool:
        return self.has_route_selection and self.can_confirm_waiting

    @property
    def can_move_selected(self) -> bool:
        return self.has_route_selection and (
            (not self.route_running and not self.design_load_pending)
            or self.can_confirm_waiting
        )

    @property
    def can_jump_selected(self) -> bool:
        return self.can_confirm_waiting and self.has_route_selection

    @property
    def can_use_tool_options(self) -> bool:
        return (
            self.has_document
            and not self.route_running
            and not self.design_load_pending
        )


__all__ = ["DesignNavigatorEnablement"]
