from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from PySide6.QtWidgets import QMessageBox
from probe_station_gui.design.model import DesignDocument, DesignModelError
from probe_station_gui.design import route_editing
from probe_station_gui.design.markup import (
    MarkupDocument,
    MarkupLoadChoice,
    resolve_loaded_markup,
)
from probe_station_gui.views import (
    main_window_coordinate_flow as coordinate_flow,
    main_window_design_workspace as design_workspace,
)

logger = logging.getLogger("main")


class _MainDesignMarkupMixin:
    def _finish_design_markup_load_ui(
        self,
        document: DesignDocument | None,
        *,
        show_window: bool,
    ) -> None:
        window = self.design_layout_window
        if (
            show_window
            and window is not None
            and hasattr(window, "finish_document_preview")
        ):
            window.finish_document_preview(document)
        self._set_design_load_pending_ui(False)

    def _invalidate_pending_design_markup_load(self) -> None:
        active_request = getattr(self, "_design_markup_load_request_id", None)
        contexts = getattr(self, "_design_markup_load_contexts", {})
        context = contexts.get(active_request) if active_request is not None else None
        self._design_markup_load_request_id = None
        contexts.clear()
        self._design_markup_pending_visibility = None
        self._finish_design_markup_load_ui(
            self._design_session.document,
            show_window=bool(context is not None and context.show_window),
        )

    def _on_design_markup_loaded(self, result: object) -> None:
        if not all(
            hasattr(result, attribute)
            for attribute in ("request_id", "source_path", "document")
        ):
            return
        active_request = getattr(self, "_design_markup_load_request_id", None)
        contexts = getattr(self, "_design_markup_load_contexts", {})
        context = contexts.pop(result.request_id, None)
        if result.request_id != active_request or context is None:
            return
        if context.generation != self._design_load_generation:
            self._design_markup_load_request_id = None
            return
        document = context.plan.document
        if document is None:
            self._design_markup_load_request_id = None
            return
        try:
            decision = resolve_loaded_markup(result.document, document.path)
        except (OSError, ValueError) as exc:
            self._design_markup_load_request_id = None
            empty = MarkupDocument.empty(document.path)
            pending_visibility = getattr(
                self,
                "_design_markup_pending_visibility",
                None,
            )
            if pending_visibility is not None:
                empty = empty.with_visibility(pending_visibility)
            self._commit_pending_design_load(context, empty)
            self._show_status(f"Markup could not be loaded: {exc}", 6000)
            return
        if decision.needs_choice:
            choice = self._prompt_changed_markup_choice(document.path)
            decision = resolve_loaded_markup(result.document, document.path, choice)
        self._design_markup_load_request_id = None
        if decision.cancel_load:
            pending_visibility = getattr(
                self,
                "_design_markup_pending_visibility",
                None,
            )
            self._design_markup_pending_visibility = None
            if pending_visibility is not None:
                self._design_markup = context.previous_markup
                self._refresh_design_markup_ui()
            self._finish_design_markup_load_ui(
                self._design_session.document,
                show_window=context.show_window,
            )
            self._show_status("Design load canceled.", 4000)
            return
        if not decision.accepted or decision.document is None:
            return
        loaded_markup = decision.document
        pending_visibility = getattr(
            self,
            "_design_markup_pending_visibility",
            None,
        )
        if pending_visibility is not None:
            loaded_markup = loaded_markup.with_visibility(pending_visibility)
        self._commit_pending_design_load(context, loaded_markup)
        if decision.delete_stored:
            self._delete_persisted_design_markup(decision.document.source_path)
        elif decision.publish or pending_visibility is not None:
            self._publish_design_markup()

    def _prompt_changed_markup_choice(self, source_path: Path) -> MarkupLoadChoice:
        message_box = QMessageBox(self)
        message_box.setIcon(QMessageBox.Icon.Warning)
        message_box.setWindowTitle("Design Markup")
        message_box.setText(f"'{source_path.name}' changed since its Markup was saved.")
        message_box.setInformativeText(
            "Keep the existing guides, start with an empty Markup, or cancel loading."
        )
        keep_button = message_box.addButton(
            "Keep Markup",
            QMessageBox.ButtonRole.AcceptRole,
        )
        empty_button = message_box.addButton(
            "Start Empty",
            QMessageBox.ButtonRole.DestructiveRole,
        )
        message_box.addButton(
            "Cancel",
            QMessageBox.ButtonRole.RejectRole,
        )
        message_box.exec()
        clicked = message_box.clickedButton()
        if clicked is keep_button:
            return MarkupLoadChoice.KEEP
        if clicked is empty_button:
            return MarkupLoadChoice.START_EMPTY
        return MarkupLoadChoice.CANCEL

    def _on_design_markup_store_failed(self, failure: object) -> None:
        if not all(
            hasattr(failure, attribute)
            for attribute in ("request_id", "operation", "source_path", "message")
        ):
            return
        if failure.operation == "load" and failure.request_id == getattr(
            self, "_design_markup_load_request_id", None
        ):
            self._design_markup_load_request_id = None
            context = getattr(self, "_design_markup_load_contexts", {}).pop(
                failure.request_id,
                None,
            )
            if (
                context is not None
                and context.generation == self._design_load_generation
                and context.plan.document is not None
            ):
                empty = MarkupDocument.empty(context.plan.document.path)
                pending_visibility = getattr(
                    self,
                    "_design_markup_pending_visibility",
                    None,
                )
                if pending_visibility is not None:
                    empty = empty.with_visibility(pending_visibility)
                self._commit_pending_design_load(context, empty)
            else:
                self._set_design_load_pending_ui(False)
            self._show_status("Markup could not be loaded.", 6000)
            logger.warning("Markup load failed: %s", failure.message)
            return
        if failure.operation == "load":
            return
        self._show_status("Markup could not be saved.", 6000)
        logger.warning(
            "Markup %s failed for %s: %s",
            failure.operation,
            failure.source_path,
            failure.message,
        )

    def _refresh_design_markup_ui(self) -> None:
        markup = getattr(self, "_design_markup", None)
        direct_ids = self._prune_design_guide_undo_stack()
        window = self.design_layout_window
        if window is not None:
            window.set_markup(markup)
            window.set_guide_undo_available(bool(direct_ids))

    def _prune_design_guide_undo_stack(self) -> list[str]:
        markup = getattr(self, "_design_markup", None)
        direct_ids = getattr(self, "_design_markup_direct_guide_ids", [])
        current_ids = (
            {guide.id for guide in markup.guides} if markup is not None else set()
        )
        direct_ids[:] = [guide_id for guide_id in direct_ids if guide_id in current_ids]
        return direct_ids

    def _publish_design_markup(self) -> None:
        markup = getattr(self, "_design_markup", None)
        if markup is None:
            return
        request_id = self._next_design_markup_request_id()
        store = self._ensure_design_markup_store()
        store.publish(request_id, markup)

    def _delete_persisted_design_markup(self, source_path: str | Path) -> None:
        request_id = self._next_design_markup_request_id()
        self._ensure_design_markup_store().delete(request_id, source_path)

    def _stop_design_markup_store(self) -> None:
        store = getattr(self, "_design_markup_store", None)
        if store is not None:
            store.stop(timeout_s=0.0)

    def _stop_coordinate_frame_store(self) -> None:
        store = getattr(self, "_coordinate_frame_store", None)
        if store is not None:
            store.stop(timeout_s=0.0)

    def _stop_software_coordinate_selection_store(self) -> None:
        store = getattr(self, "_software_coordinate_selection_store", None)
        if store is not None:
            store.stop(timeout_s=2.0)

    def _on_software_coordinate_selection_store_failed(self, failure: object) -> None:
        message = str(getattr(failure, "message", "Unknown persistence error."))
        logger.warning("Software coordinate selection save failed: %s", message)
        self._show_status("Coordinate selection could not be saved.", 6000)

    def _on_coordinate_frame_document_loaded(self, result: object) -> None:
        coordinate_flow.handle_coordinate_frame_loaded(self, result)

    def _on_coordinate_frame_document_saved(self, result: object) -> None:
        coordinate_flow.handle_coordinate_frame_saved(self, result)

    def _on_coordinate_frame_store_failed(self, failure: object) -> None:
        operation = str(getattr(failure, "operation", "operation"))
        message = str(getattr(failure, "message", "Unknown persistence error."))
        logger.warning("Coordinate frame %s failed: %s", operation, message)
        coordinate_flow.handle_coordinate_frame_failed(self, failure)

    def _show_navigation_status(self, plan: object) -> None:
        message = getattr(plan, "status_message", None)
        if message is not None:
            self._show_status(message, getattr(plan, "status_timeout_ms", 5000))

    def _apply_route_edit_plan(
        self,
        plan: route_editing.RouteEditPlan,
        *,
        empty_selection: bool = False,
        update_selection: bool = True,
    ) -> bool:
        if not plan.accepted:
            self._show_navigation_status(plan)
            return False
        if update_selection:
            self._last_selected_design_point = (
                None if empty_selection else plan.last_selected_design_point
            )
        self._refresh_design_panel()
        self._show_navigation_status(plan)
        return True

    def _unload_design_document(self) -> None:
        if not self._design_mutation_ready():
            return
        self._design_load_generation += 1
        loaded_document = self._design_session.document
        candidate_session = self._snapshot_design_session()
        plan = route_editing.unload_design_document(candidate_session)
        if not plan.accepted:
            return
        coordinate_flow.apply_coordinate_transition(
            self,
            self._coordinate_system_coordinator.close_design(),
        )
        self._design_session.apply_state(candidate_session.snapshot_state())
        if loaded_document is not None:
            self._delete_persisted_design_markup(loaded_document.path)
        self._design_markup_load_request_id = None
        getattr(self, "_design_markup_load_contexts", {}).clear()
        self._design_markup = None
        self._design_markup_direct_guide_ids = []
        self._design_markup_pending_visibility = None
        self._reset_manual_alignment(cancel_pick=True)
        self._stage_motion.discard_alignment_rotation()
        self._last_selected_design_point = None
        self._refresh_design_panel()
        self._update_design_position(None)
        self._show_navigation_status(plan)

    def _set_design_top_cell(self, top_cell_name: str) -> None:
        if not self._design_mutation_ready():
            return
        candidate_session = self._snapshot_design_session()
        try:
            plan = route_editing.set_design_top_cell(candidate_session, top_cell_name)
        except DesignModelError as exc:
            self._show_status(str(exc), 6000)
            return
        metadata = getattr(self, "_active_design_frame_metadata", None)
        document = candidate_session.document
        if metadata is not None and document is not None:
            metadata = replace(
                metadata,
                top_cell_name=document.top_cell_name,
                design_unit_mm=float(document.dbu) * 1e3,
            )
        workspace_after = design_workspace.capture_design_workspace(
            self,
            session_state=candidate_session.snapshot_state(),
            frame_metadata=metadata,
            last_selected_design_point=None,
        )
        transition = coordinate_flow.activate_current_design(
            self,
            session_state=candidate_session.snapshot_state(),
            frame_metadata=metadata,
            workspace_after=workspace_after,
        )
        if transition is None or not transition.accepted:
            return
        self._refresh_design_panel()
        self._refresh_design_position()
        self._show_navigation_status(plan)

    def _set_design_layer_visibility(
        self, layer: int, datatype: int, visible: bool
    ) -> None:
        if not self._design_mutation_ready():
            return
        candidate_session = self._snapshot_design_session()
        try:
            plan = route_editing.set_design_layer_visibility(
                candidate_session, layer, datatype, visible
            )
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        if not plan.accepted:
            return
        workspace_after = design_workspace.capture_design_workspace(
            self,
            session_state=candidate_session.snapshot_state(),
        )
        transition = coordinate_flow.activate_current_design(
            self,
            session_state=candidate_session.snapshot_state(),
            workspace_after=workspace_after,
        )
        if transition is None or not transition.accepted:
            return
        self._refresh_design_panel()

    def _rotate_design_document(self, quarter_turn_delta: int) -> None:
        if not self._design_mutation_ready():
            return
        document = self._design_session.document
        if document is None:
            self._show_status("Load a design before rotating it.", 4000)
            return
        if self._coordinate_system_coordinator.snapshot().registration.registration_valid:
            self._show_status(
                "Clear design registration before rotating the design.",
                5000,
            )
            return
        if self._stage_motion.alignment_rotation_pending():
            self._show_status(
                "Wait for chip rotation to finish before rotating the design.",
                5000,
            )
            return
        if self._route_run_execution.snapshot().thread_alive:
            self._show_status(
                "Stop route measurement before rotating the design.", 5000
            )
            return
        delta = int(quarter_turn_delta) % 4
        if delta == 0:
            delta = 1
        markup = getattr(self, "_design_markup", None)
        rotated_markup = (
            markup.transform_design_coordinates(
                lambda point: document.rotate_point(point, delta)
            )
            if markup is not None
            else None
        )
        candidate_session = self._snapshot_design_session()
        try:
            plan = route_editing.rotate_design_document(
                candidate_session,
                delta,
                self._last_selected_design_point,
                can_rotate=True,
            )
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        workspace_after = design_workspace.capture_design_workspace(
            self,
            session_state=candidate_session.snapshot_state(),
            markup=rotated_markup,
            last_selected_design_point=plan.last_selected_design_point,
        )
        transition = coordinate_flow.activate_current_design(
            self,
            session_state=candidate_session.snapshot_state(),
            workspace_after=workspace_after,
        )
        if transition is None or not transition.accepted:
            return
        self._refresh_design_panel()
        self._refresh_design_position()
        if rotated_markup != markup:
            self._publish_design_markup()
        self._show_navigation_status(plan)
