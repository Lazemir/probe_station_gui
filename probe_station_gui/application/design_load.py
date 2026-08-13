from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from PySide6.QtWidgets import QApplication
from probe_station_gui.design.frame_registration import DesignFrameMetadata
from probe_station_gui.design.registration_lifecycle import RegistrationCancellation
from probe_station_gui.design.model import DesignDocument, DesignModelError
from probe_station_gui.design import navigation_adapter as design_navigation
from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.markup_store import MarkupStoreWorker
from probe_station_gui.design.session import DesignSession
from probe_station_gui.design.session_state import (
    PreparedDesignSessionRestore,
    document_with_persisted_session_view,
    persisted_design_source_is_current,
    prepare_persisted_session_restore,
)
from probe_station_gui.views.main_window_auxiliary import toggle_design_layout_window
from probe_station_gui.views import (
    main_window_connection_flow as connection_flow,
    main_window_coordinate_flow as coordinate_flow,
    main_window_design_workspace as design_workspace,
)

logger = logging.getLogger("main")


@dataclass(frozen=True)
class _PendingDesignMarkupLoad:
    generation: int
    session: DesignSession
    plan: design_navigation.DesignLoadResultPlan
    show_window: bool
    previous_markup: MarkupDocument | None
    frame_metadata: DesignFrameMetadata | None = None


@dataclass(frozen=True)
class _LoadedDesignDocument:
    document: DesignDocument
    frame_metadata: DesignFrameMetadata
    prepared_restore: PreparedDesignSessionRestore | None = None


class _MainDesignLoadMixin:
    def _load_design_document(self, design_path: str) -> None:
        self._start_design_document_load(
            design_path, restore_state=None, show_window=True
        )

    def _start_design_document_load(
        self,
        design_path: str,
        *,
        restore_state: dict[str, object] | None,
        show_window: bool,
    ) -> None:
        transition = self._coordinate_system_coordinator.cancel_registration(
            RegistrationCancellation.DESIGN_CHANGED
        )
        coordinate_flow.apply_coordinate_transition(self, transition)
        self._invalidate_pending_design_markup_load()
        self._design_load_generation += 1
        generation = self._design_load_generation
        self._set_design_load_pending_ui(True)
        path_text = str(design_path)
        if restore_state is not None:
            self._design_load_restore_states[generation] = dict(restore_state)
        self._design_load_show_window[generation] = bool(show_window)
        if show_window:
            toggle_design_layout_window(self, True)
            self._set_design_load_pending_ui(True)
        if self.design_layout_window is not None and show_window:
            self.design_layout_window.set_status_message("Loading design...")
        elif self.design_navigator_panel:
            self.design_navigator_panel.set_status_message("Loading design...")
        self._show_status(f"Loading design '{Path(path_text).name}'...")

        def load_design() -> None:
            try:
                if restore_state is not None and not persisted_design_source_is_current(
                    restore_state
                ):
                    raise DesignModelError(
                        "Cached design file changed or is unavailable. "
                        "Cleared cached design selection."
                    )
                document = DesignDocument.load(path_text)
                prepared_restore = None
                if restore_state is not None:
                    document = document_with_persisted_session_view(
                        document,
                        restore_state,
                    )
                    prepared_restore = prepare_persisted_session_restore(
                        document,
                        restore_state,
                    )
                frame_metadata = DesignFrameMetadata.from_document(document)
            except Exception as exc:
                self.design_document_loaded.emit(generation, None, exc)
                return
            self.design_document_loaded.emit(
                generation,
                _LoadedDesignDocument(
                    document,
                    frame_metadata,
                    prepared_restore,
                ),
                None,
            )

        threading.Thread(
            target=load_design, name="DesignDocumentLoad", daemon=True
        ).start()

    def _on_design_document_loaded(
        self, generation: int, document: object, error: object
    ) -> None:
        if generation != self._design_load_generation:
            return
        restore_state = self._design_load_restore_states.pop(generation, None)
        show_window = self._design_load_show_window.pop(generation, True)
        frame_metadata = None
        prepared_restore = None
        if isinstance(document, _LoadedDesignDocument):
            frame_metadata = document.frame_metadata
            prepared_restore = document.prepared_restore
            document = document.document
        candidate_session = self._snapshot_design_session()
        previous_markup = getattr(self, "_design_markup", None)
        try:
            plan = design_navigation.design_document_loaded_plan(
                candidate_session,
                document,
                error,
                restore_state,
                prepared_restore,
            )
        except DesignModelError as exc:
            self._set_design_load_pending_ui(False)
            self._show_status(str(exc), 6000)
            if restore_state is not None:
                design_workspace.save_controller_state_without_design(self)
            return
        if not plan.accepted:
            self._set_design_load_pending_ui(False)
            self._apply_design_load_failure_plan(plan, show_window)
            return
        if plan.document is not None:
            if frame_metadata is not None:
                frame_metadata = replace(
                    frame_metadata,
                    top_cell_name=plan.document.top_cell_name,
                    design_unit_mm=float(plan.document.dbu) * 1e3,
                )
            load_arguments = {
                "generation": generation,
                "candidate_session": candidate_session,
                "plan": plan,
                "show_window": show_window,
                "previous_markup": previous_markup,
            }
            if frame_metadata is not None:
                load_arguments["frame_metadata"] = frame_metadata
            self._begin_design_markup_load(plan.document, **load_arguments)

    def _snapshot_design_session(self) -> DesignSession:
        candidate = DesignSession()
        candidate.apply_state(self._design_session.snapshot_state())
        return candidate

    def _apply_design_load_success_plan(
        self, plan: design_navigation.DesignLoadResultPlan, show_window: bool
    ) -> None:
        self._reset_manual_alignment(cancel_pick=True)
        self._pending_alignment_preparation = None
        self._last_selected_design_point = plan.last_selected_design_point
        self._set_design_snap_enabled(True)
        if plan.document_directory is not None:
            self.settings_manager.set_design_last_directory(plan.document_directory)
            if self.design_navigator_panel is not None:
                self.design_navigator_panel.set_design_dialog_directory(
                    plan.document_directory
                )
        if self.design_layout_window is not None and show_window:
            self.design_layout_window.set_status_message("Rendering design...")
            QApplication.processEvents()
        self._refresh_design_panel()
        self._refresh_design_position()
        if show_window:
            toggle_design_layout_window(self, True)
        connection_flow.persist_controller_state_if_available(self)
        self._show_navigation_status(plan)
        self._restore_route_measurement_state_after_design_load()

    def _apply_design_load_failure_plan(
        self, plan: design_navigation.DesignLoadResultPlan, show_window: bool
    ) -> None:
        message = plan.status_message or ""
        self._show_status(message, plan.status_timeout_ms)
        if self.design_layout_window is not None and show_window:
            self.design_layout_window.set_status_message(message)
        elif self.design_navigator_panel:
            self.design_navigator_panel.set_status_message(message)
        if plan.clear_cached_design:
            design_workspace.save_controller_state_without_design(self)

    def _ensure_design_markup_store(self) -> MarkupStoreWorker:
        store = getattr(self, "_design_markup_store", None)
        if store is not None:
            return store
        store = MarkupStoreWorker(self)
        store.loaded.connect(self._on_design_markup_loaded)
        store.failed.connect(self._on_design_markup_store_failed)
        self._design_markup_store = store
        return store

    def _next_design_markup_request_id(self) -> int:
        self._design_markup_request_id = (
            int(getattr(self, "_design_markup_request_id", 0)) + 1
        )
        return self._design_markup_request_id

    def _begin_design_markup_load(
        self,
        document: DesignDocument,
        *,
        generation: int,
        candidate_session: DesignSession,
        plan: design_navigation.DesignLoadResultPlan,
        show_window: bool,
        previous_markup: MarkupDocument | None,
        frame_metadata: DesignFrameMetadata | None = None,
    ) -> None:
        self._design_markup_pending_visibility = None
        request_id = self._next_design_markup_request_id()
        self._design_markup_load_request_id = request_id
        contexts = getattr(self, "_design_markup_load_contexts", None)
        if contexts is None:
            contexts = {}
            self._design_markup_load_contexts = contexts
        contexts.clear()
        contexts[request_id] = _PendingDesignMarkupLoad(
            generation=generation,
            session=candidate_session,
            plan=plan,
            show_window=show_window,
            previous_markup=previous_markup,
            frame_metadata=frame_metadata,
        )
        self._set_design_load_pending_ui(True)
        if show_window:
            window = self.design_layout_window
            if window is not None and hasattr(window, "set_document_preview"):
                window.set_document_preview(document)
        self._ensure_design_markup_store().load(request_id, document.path)

    def _commit_pending_design_load(
        self,
        context: _PendingDesignMarkupLoad,
        markup: MarkupDocument,
    ) -> None:
        frame_metadata = self._design_metadata_with_calibration_fingerprints(
            context.frame_metadata
        )
        workspace_after = design_workspace.capture_design_workspace(
            self,
            session_state=context.session.snapshot_state(),
            frame_metadata=frame_metadata,
            markup=markup,
            direct_guide_ids=(),
            pending_visibility=None,
            last_selected_design_point=context.plan.last_selected_design_point,
            pending_alignment_preparation=None,
        )
        transition = coordinate_flow.activate_current_design(
            self,
            session_state=context.session.snapshot_state(),
            frame_metadata=frame_metadata,
            workspace_after=workspace_after,
        )
        if transition is None or not transition.accepted:
            self._design_markup_pending_visibility = None
            self._finish_design_markup_load_ui(
                self._design_session.document,
                show_window=context.show_window,
            )
            return
        self._apply_design_load_success_plan(context.plan, context.show_window)
        self._finish_design_markup_load_ui(
            context.plan.document,
            show_window=context.show_window,
        )

    def _set_design_load_pending_ui(self, pending: bool) -> None:
        self._design_load_pending = bool(pending)
        panel = self.design_navigator_panel
        if panel is not None and hasattr(panel, "set_design_load_pending"):
            panel.set_design_load_pending(pending)
        window = self.design_layout_window
        if window is not None and hasattr(window, "set_design_load_pending"):
            window.set_design_load_pending(pending)
        if window is not None and hasattr(window, "set_route_edit_enabled"):
            window.set_route_edit_enabled(
                False if pending else self._design_edit_safe()
            )
