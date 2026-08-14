from __future__ import annotations

import logging
from PySide6.QtCore import QTimer
from probe_station_gui.design.model import DesignModelError
from probe_station_gui.design import route_editing
from probe_station_gui.design.selection_model import (
    SelectionModel,
    apply_markup_entity_changes,
)
from probe_station_gui.route.dialog_adapter import (
    open_or_update_route_measurement_dialog,
    route_dialog_handlers,
    route_dialog_restore_plan,
)
from probe_station_gui.route.measurement_settings import RouteMeasurementSettingsStore

logger = logging.getLogger("main")


class _MainDesignEditDialogMixin:
    def _create_measurement_route(self) -> None:
        if not self._design_mutation_ready():
            return
        try:
            plan = route_editing.create_measurement_route(self._design_session)
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._last_selected_design_point = None
        self._refresh_design_panel()
        self._show_navigation_status(plan)

    def _load_measurement_route(self, route_path: str) -> None:
        if not self._design_mutation_ready():
            return
        try:
            plan = route_editing.load_measurement_route(
                self._design_session, route_path
            )
        except DesignModelError as exc:
            self._show_status(str(exc), 7000)
            return
        if not self._apply_route_edit_plan(plan):
            return
        self._restore_route_measurement_state_after_design_load()

    def _save_measurement_route(self) -> None:
        if not self._design_mutation_ready():
            return
        try:
            plan = route_editing.save_measurement_route(self._design_session)
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._apply_route_edit_plan(plan, update_selection=False)

    def _save_measurement_route_as(self, route_path: str) -> None:
        if not self._design_mutation_ready():
            return
        try:
            plan = route_editing.save_measurement_route(
                self._design_session, route_path
            )
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._apply_route_edit_plan(plan, update_selection=False)

    def _add_design_route_point(self, x_value: float, y_value: float) -> None:
        if not self._design_edit_safe():
            self._show_status("Design editing is locked.", 4000)
            return
        try:
            plan = route_editing.add_design_route_point(
                self._design_session, x_value, y_value
            )
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._apply_route_edit_plan(plan)

    def _design_edit_safe(self) -> bool:
        if self._design_session.document is None:
            return False
        if bool(getattr(self, "_design_load_pending", False)):
            return False
        return not self._route_run_execution.snapshot().thread_alive

    def _design_mutation_ready(self) -> bool:
        if not bool(getattr(self, "_design_load_pending", False)):
            return True
        self._show_status("Design is loading.", 3000)
        return False

    def _markup_mutation_ready(self) -> bool:
        if not self._design_edit_safe():
            self._show_status("Design editing is locked.", 4000)
            return False
        if getattr(self, "_design_markup_load_request_id", None) is not None:
            self._show_status("Markup is loading.", 3000)
            return False
        return True

    def _add_design_guide(self, start: object, end: object) -> None:
        if not self._markup_mutation_ready():
            return
        markup = getattr(self, "_design_markup", None)
        if markup is None:
            self._show_status("Load a design before adding Markup.", 4000)
            return
        try:
            updated = markup.append_guide(start, end)
        except (TypeError, ValueError) as exc:
            self._show_status(str(exc), 4000)
            return
        guide_id = updated.guides[-1].id
        self._design_markup = updated
        self._design_markup_direct_guide_ids.append(guide_id)
        self._refresh_design_markup_ui()
        self._publish_design_markup()
        self._show_status("Guide added.", 2500)

    def _set_design_markup_visibility(self, visible: bool) -> None:
        markup = getattr(self, "_design_markup", None)
        if markup is None:
            return
        updated = markup.with_visibility(visible)
        if updated == markup:
            return
        self._design_markup = updated
        self._refresh_design_markup_ui()
        if getattr(self, "_design_markup_load_request_id", None) is not None:
            self._design_markup_pending_visibility = bool(visible)
            return
        self._publish_design_markup()

    def _undo_last_design_guide(self) -> None:
        if not self._markup_mutation_ready():
            return
        markup = getattr(self, "_design_markup", None)
        if markup is None:
            return
        current_ids = {guide.id for guide in markup.guides}
        direct_ids = self._design_markup_direct_guide_ids
        while direct_ids and direct_ids[-1] not in current_ids:
            direct_ids.pop()
        if not direct_ids:
            return
        removed_id = direct_ids.pop()
        self._design_markup = markup.remove_ids({removed_id})
        self._refresh_design_markup_ui()
        self._publish_design_markup()

    def _clear_design_guides(self) -> None:
        if not self._markup_mutation_ready():
            return
        markup = getattr(self, "_design_markup", None)
        if markup is None or not markup.guides:
            return
        self._design_markup = markup.remove_ids({guide.id for guide in markup.guides})
        self._design_markup_direct_guide_ids = []
        self._refresh_design_markup_ui()
        self._delete_persisted_design_markup(markup.source_path)
        self._show_status("Markup cleared.", 3000)

    def _delete_design_selection(self) -> None:
        if not self._markup_mutation_ready():
            return
        window = self.design_layout_window
        if window is None:
            return
        plan = window.plan_mixed_delete(edit_safe=True)
        self._commit_mixed_design_edit(plan, selection_after=SelectionModel())

    def _apply_mixed_design_array(self, request: object) -> None:
        if not all(
            hasattr(request, attribute)
            for attribute in (
                "direction_1",
                "count_1",
                "direction_2",
                "count_2",
                "serpentine",
                "source_ids",
            )
        ):
            self._show_status("Array settings are invalid.", 4000)
            return
        if not self._markup_mutation_ready():
            return
        plan = self.design_layout_window.plan_mixed_array(
            request,
            edit_safe=True,
        )
        self._commit_mixed_design_edit(
            plan,
            selection_after=self.design_layout_window.selection,
        )

    def _commit_mixed_design_edit(
        self,
        plan: object,
        *,
        selection_after: SelectionModel,
    ) -> None:
        if not getattr(plan, "accepted", False):
            self._show_status(str(getattr(plan, "status_message", "")), 4000)
            return
        markup = getattr(self, "_design_markup", None)
        new_markup = markup
        try:
            if markup is not None:
                new_markup = apply_markup_entity_changes(markup, plan)
            elif getattr(plan, "required_guide_ids", frozenset()):
                raise ValueError("Selected guide segments are stale.")
        except ValueError as exc:
            self._show_status(str(exc), 4000)
            return
        route_plan = route_editing.apply_route_entity_changes(
            self._design_session,
            plan,
        )
        if not route_plan.accepted:
            self._show_navigation_status(route_plan)
            return
        markup_changed = new_markup != markup
        self._design_markup = new_markup
        self._last_selected_design_point = route_plan.last_selected_design_point
        self._refresh_design_panel()
        if self.design_layout_window is not None:
            self.design_layout_window.set_selection(selection_after)
        if markup_changed:
            self._publish_design_markup()
        self._show_navigation_status(route_plan)

    def _add_current_design_route_point(self) -> None:
        if self._current_design_stage_xy is None:
            self._show_status("Current design position is unavailable.", 4000)
            return
        design_xy = self._design_xy_from_raw_stage_xy(self._current_design_stage_xy)
        if design_xy is None:
            self._show_status(
                "Design registration is required before adding the current position.",
                5000,
            )
            return
        self._add_design_route_point(design_xy[0], design_xy[1])

    def _add_route_array_points(
        self,
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
    ) -> None:
        if not self._design_edit_safe():
            self._show_status("Design editing is locked.", 4000)
            return
        try:
            plan = route_editing.add_route_array_points(
                self._design_session,
                origin_x,
                origin_y,
                step_x_dx,
                step_x_dy,
                count_x,
                step_y_dx,
                step_y_dy,
                count_y,
                serpentine,
                replace_existing,
                selected_indices,
            )
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._apply_route_edit_plan(plan)

    def _remove_selected_route_point(self) -> None:
        if not self._design_mutation_ready():
            return
        plan = route_editing.remove_selected_route_point(self._design_session)
        self._apply_route_edit_plan(plan)

    def _clear_measurement_route_points(self) -> None:
        if not self._design_mutation_ready():
            return
        plan = route_editing.clear_measurement_route_points(self._design_session)
        self._apply_route_edit_plan(plan, empty_selection=True)

    def _open_route_measurement_dialog(self, *, start_context: bool = True) -> None:
        route = self._design_session.route
        if route is None or not route.points:
            self._show_status("Create or load a probe route before measuring.", 5000)
            return
        execution = self._route_run_execution.snapshot()
        self._route_measurement_dialog = open_or_update_route_measurement_dialog(
            dialog=self._route_measurement_dialog,
            route=route,
            document=self._design_session.document,
            meter_type=self.lcr_controller.meter_type(),
            settings_path=(
                self.settings_manager.config_dir()
                / RouteMeasurementSettingsStore.FILENAME
            ),
            parent=self,
            session_active=self._route_measurement_session_active,
            current_point=self._route_measurement_current_point,
            thread_active=execution.thread_alive,
            waiting=execution.waiting,
            handlers=route_dialog_handlers(self),
        )
        dialog = self._route_measurement_dialog
        if start_context and not execution.thread_alive:
            configuration = dialog.current_configuration()
            self._start_route_measurement(
                configuration,
                wait_before_first_point=True,
            )

    def _restore_route_measurement_state_after_design_load(self) -> None:
        state = self._route_measurement_settings_store().load()
        route = self._design_session.route
        if route is None or not route.points:
            route_path = str(state.get("session_route_path") or "").strip()
            if RouteMeasurementSettingsStore.session_active(state) and route_path:
                try:
                    route_plan = route_editing.load_measurement_route(
                        self._design_session,
                        route_path,
                    )
                except DesignModelError as exc:
                    logger.warning("Unable to restore saved probe route: %s", exc)
                else:
                    if self._apply_route_edit_plan(route_plan):
                        route = self._design_session.route
        if route is None or not route.points:
            return
        plan = route_dialog_restore_plan(
            state,
            route,
        )
        self._route_measurement_session_active = plan.session_active
        if plan.session_active and plan.current_point is not None:
            self._set_route_measurement_resume_point(plan.current_point)
        if plan.open_dialog:
            QTimer.singleShot(0, self._open_route_measurement_dialog)

    def _route_measurement_settings_store(self) -> RouteMeasurementSettingsStore:
        return RouteMeasurementSettingsStore(self.settings_manager.config_dir())

    def _clear_route_measurement_dialog(self) -> None:
        self._route_measurement_dialog = None
        execution = self._route_run_execution.snapshot()
        runner = execution.runner
        if runner is not None and execution.thread_alive:
            self._route_measurement_context_close_requested = True
            runner.stop()
