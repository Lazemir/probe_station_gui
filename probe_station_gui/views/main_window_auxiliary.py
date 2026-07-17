"""Lazy auxiliary window construction for the main probe station window."""

from __future__ import annotations

import logging
from typing import Any, Protocol

from PySide6.QtWidgets import QDialog

from probe_station_gui.camera import microscope_scan
from probe_station_gui.route.dialog_adapter import (
    route_measurement_point_request_handler_for_owner,
)


logger = logging.getLogger(__name__)


class MainWindowAuxiliaryOwner(Protocol):
    surface_map_window: Any
    microscope_scan_dialog: Any
    design_layout_window: Any
    design_navigator_panel: Any
    _design_layout_window_class: object | None
    _design_layout_window_requested: bool
    _design_layout_window_action: Any
    _contact_calibration_window_action: Any
    contact_calibration_window: Any
    stage_controller: Any
    serial_connection: Any
    serial_connection_dialog: Any
    serial_connection_panel: Any
    serial_connection_tabs: Any
    settings_manager: Any
    lcr_controller: Any
    grabber: Any
    _api_key_store: Any
    _design_session: Any

    def _surface_map_stage_status(self) -> Any: ...
    def _surface_map_move_to_xy(self, *args: Any) -> Any: ...
    def _update_stage_coordinate_apply_state(self) -> None: ...
    def _start_microscope_scan(self, *args: Any) -> None: ...
    def _request_stop_microscope_scan(self) -> None: ...
    def _clear_microscope_scan_dialog(self) -> None: ...
    def _show_status(self, message: str) -> None: ...
    def _preload_design_layout_window(self) -> None: ...
    def _collapse_alignment_panel_if_ready(self) -> None: ...
    def _stop_telegram_bot_service(self) -> None: ...
    def _apply_settings_from_dialog(self, *args: Any) -> None: ...
    def _configure_telegram_bot_from_settings(self) -> None: ...
    def _load_design_document(self, *args: Any) -> None: ...
    def _unload_design_document(self, *args: Any) -> None: ...
    def _set_design_top_cell(self, *args: Any) -> None: ...
    def _set_design_layer_visibility(self, *args: Any) -> None: ...
    def _rotate_design_document(self, *args: Any) -> None: ...
    def _create_measurement_route(self, *args: Any) -> None: ...
    def _load_measurement_route(self, *args: Any) -> None: ...
    def _save_measurement_route(self, *args: Any) -> None: ...
    def _save_measurement_route_as(self, *args: Any) -> None: ...
    def _add_current_design_route_point(self, *args: Any) -> None: ...
    def _remove_selected_route_point(self, *args: Any) -> None: ...
    def _clear_measurement_route_points(self, *args: Any) -> None: ...
    def _select_route_point(self, *args: Any) -> None: ...
    def _set_route_needle_offsets(self, *args: Any) -> None: ...
    def _set_route_edit_enabled(self, *args: Any) -> None: ...
    def _add_route_array_points(self, *args: Any) -> None: ...
    def _add_design_guide(self, *args: Any) -> None: ...
    def _delete_design_selection(self, *args: Any) -> None: ...
    def _undo_last_design_guide(self, *args: Any) -> None: ...
    def _clear_design_guides(self, *args: Any) -> None: ...
    def _set_design_markup_visibility(self, *args: Any) -> None: ...
    def _apply_mixed_design_array(self, *args: Any) -> None: ...
    def _open_route_measurement_dialog(self, *args: Any) -> None: ...
    def _start_route_measurement(self, *args: Any) -> None: ...
    def _request_stop_route_measurement(self, *args: Any) -> None: ...
    def _request_pause_route_measurement(self, *args: Any) -> None: ...
    def _request_route_measurement_point_correction(self, *args: Any) -> None: ...
    def _save_route_measurement_shift(self, *args: Any) -> None: ...
    def _submit_route_measurement_confirmation(self, *args: Any) -> None: ...
    def _submit_route_measurement_jump(self, *args: Any) -> None: ...
    def _request_route_contact_move(self, *args: Any) -> None: ...
    def _move_to_design_target(self, *args: Any) -> None: ...
    def _select_next_design_target(self, *args: Any) -> None: ...
    def _select_previous_design_target(self, *args: Any) -> None: ...
    def _on_design_target_selected(self, *args: Any) -> None: ...
    def _on_design_snap_enabled_changed(self, *args: Any) -> None: ...
    def _on_design_layout_point_selected(self, *args: Any) -> None: ...
    def _on_alignment_draft_accepted(self, *args: Any) -> None: ...
    def _on_alignment_draft_discarded(self, *args: Any) -> None: ...
    def _move_to_design_window_point(self, *args: Any) -> None: ...
    def _add_design_route_point(self, *args: Any) -> None: ...


def show_surface_map_window(
    owner: MainWindowAuxiliaryOwner,
    window_class: object | None = None,
) -> None:
    """Show the lazily-created surface map window."""

    if owner.surface_map_window is None:
        if window_class is None:
            from probe_station_gui.views.surface_map_panel import SurfaceMapWindow

            window_class = SurfaceMapWindow
        owner.surface_map_window = window_class(
            stage_status_provider=owner._surface_map_stage_status,
            stage_move_requester=owner._surface_map_move_to_xy,
            settings_path=(
                owner.settings_manager.config_dir() / "surface-map-settings.json"
            ),
            parent=None,
        )
        owner.surface_map_window.capture_running_changed.connect(
            lambda _running: owner._update_stage_coordinate_apply_state()
        )
    owner.surface_map_window.showNormal()
    owner.surface_map_window.raise_()


def show_microscope_scan_dialog(
    owner: MainWindowAuxiliaryOwner,
    dialog_class: object | None = None,
) -> None:
    """Show the lazily-created microscope scan dialog."""

    if dialog_class is None:
        from probe_station_gui.dialogs.microscope_scan_dialog import (
            MicroscopeScanDialog,
        )

        dialog_class = MicroscopeScanDialog

    default_dir = microscope_scan.default_output_dir(owner._design_session.document)
    if owner.microscope_scan_dialog is None:
        dialog = dialog_class(
            default_output_dir=default_dir,
            parent=None,
        )
        dialog.scan_requested.connect(owner._start_microscope_scan)
        dialog.stop_requested.connect(owner._request_stop_microscope_scan)
        dialog.finished.connect(lambda _result: owner._clear_microscope_scan_dialog())
        owner.microscope_scan_dialog = dialog
    owner.microscope_scan_dialog.show()
    owner.microscope_scan_dialog.raise_()
    owner.microscope_scan_dialog.activateWindow()


def toggle_design_layout_window(
    owner: MainWindowAuxiliaryOwner,
    visible: bool,
) -> None:
    """Apply the Design Window action state to the lazy design window."""

    if owner.design_layout_window is None:
        owner._design_layout_window_requested = bool(visible)
        if visible:
            if owner._design_layout_window_class is not None:
                create_design_layout_window(owner, owner._design_layout_window_class)
                return
            owner._show_status("Preparing design window...")
            owner._preload_design_layout_window()
        return
    if visible:
        owner._design_layout_window_requested = True
        owner.design_layout_window.show_and_raise()
        owner._collapse_alignment_panel_if_ready()
    else:
        owner._design_layout_window_requested = False
        owner.design_layout_window.hide()


def sync_design_layout_window_action(
    owner: MainWindowAuxiliaryOwner,
    visible: bool,
) -> None:
    """Mirror design window visibility into its checkable menu action."""

    if owner._design_layout_window_action is None:
        return
    owner._design_layout_window_action.blockSignals(True)
    owner._design_layout_window_action.setChecked(visible)
    owner._design_layout_window_action.blockSignals(False)
    if visible:
        owner._collapse_alignment_panel_if_ready()


def toggle_contact_calibration_window(
    owner: MainWindowAuxiliaryOwner,
    visible: bool,
) -> None:
    """Apply the Contact / Stone Calibration action state."""

    if owner.contact_calibration_window is None:
        if owner._contact_calibration_window_action is not None:
            owner._contact_calibration_window_action.blockSignals(True)
            owner._contact_calibration_window_action.setChecked(False)
            owner._contact_calibration_window_action.blockSignals(False)
        return
    if visible:
        owner.contact_calibration_window.show_and_raise()
        latest_lowering = owner.stage_controller.latest_axis_a_lowering()
        if latest_lowering is not None:
            owner.contact_calibration_window.set_current_needle_lowering(
                latest_lowering
            )
        elif owner.serial_connection is not None and owner.serial_connection.is_open:
            owner.stage_controller.request_status_refresh()
        return
    owner.contact_calibration_window.hide()


def sync_contact_calibration_window_action(
    owner: MainWindowAuxiliaryOwner,
    visible: bool,
) -> None:
    """Mirror contact calibration visibility into its checkable menu action."""

    if owner._contact_calibration_window_action is None:
        return
    owner._contact_calibration_window_action.blockSignals(True)
    owner._contact_calibration_window_action.setChecked(visible)
    owner._contact_calibration_window_action.blockSignals(False)


def open_settings_dialog(
    owner: MainWindowAuxiliaryOwner,
    initial_tab: object = None,
) -> None:
    """Open the settings dialog and apply the existing owner lifecycle hooks."""

    from probe_station_gui.dialogs.settings_dialog import SettingsDialog

    tab_name = initial_tab if isinstance(initial_tab, str) else None
    owner._stop_telegram_bot_service()
    dialog = SettingsDialog(
        owner.settings_manager.settings,
        owner,
        initial_tab=tab_name,
        camera_settings_source=owner.grabber,
        api_key_store=owner._api_key_store,
    )
    dialog.settings_applied.connect(owner._apply_settings_from_dialog)
    try:
        if dialog.exec() != QDialog.Accepted and not dialog.was_applied():
            logger.debug("Settings dialog cancelled")
    finally:
        owner._configure_telegram_bot_from_settings()


def show_connection_dialog(
    owner: MainWindowAuxiliaryOwner,
    tab_name: object = None,
) -> None:
    """Show the existing serial/LCR connection dialog."""

    if owner.serial_connection_dialog is None:
        return
    if owner.serial_connection_panel is not None:
        owner.serial_connection_panel.set_lcr_resource(
            owner.lcr_controller.connection_label()
        )
    if owner.serial_connection_tabs is not None:
        owner.serial_connection_tabs.setCurrentIndex(
            1 if tab_name == "terminal" else 0
        )
    owner.serial_connection_dialog.show()
    owner.serial_connection_dialog.raise_()
    owner.serial_connection_dialog.activateWindow()


def create_design_layout_window(
    owner: MainWindowAuxiliaryOwner,
    design_layout_window_class: object | None = None,
) -> None:
    """Create and wire the lazily-loaded design layout window."""

    if owner.design_layout_window is not None:
        return
    design_layout_window_class = _resolve_design_layout_window_class(
        owner,
        design_layout_window_class,
    )
    owner._design_layout_window_class = design_layout_window_class
    owner.design_layout_window = design_layout_window_class()
    owner.design_navigator_panel = owner.design_layout_window.navigator_panel
    _connect_design_navigator_document_signals(owner)
    _connect_design_navigator_route_signals(owner)
    _connect_design_layout_window_signals(owner)
    owner.design_navigator_panel.set_design_dialog_directory(
        owner.settings_manager.design_last_directory()
    )
    owner._refresh_design_panel()
    if owner._design_layout_window_requested:
        owner.design_layout_window.show_and_raise()
        owner._collapse_alignment_panel_if_ready()


def _resolve_design_layout_window_class(
    owner: MainWindowAuxiliaryOwner,
    design_layout_window_class: object | None,
) -> object:
    if design_layout_window_class is not None:
        return design_layout_window_class
    if owner._design_layout_window_class is not None:
        return owner._design_layout_window_class
    from probe_station_gui.views.design_navigator_panel import (
        DesignLayoutWindow as imported_design_layout_window_class,
    )

    return imported_design_layout_window_class


def _connect_design_navigator_document_signals(
    owner: MainWindowAuxiliaryOwner,
) -> None:
    owner.design_navigator_panel.load_design_requested.connect(
        owner._load_design_document
    )
    owner.design_navigator_panel.unload_design_requested.connect(
        owner._unload_design_document
    )
    owner.design_navigator_panel.top_cell_changed.connect(owner._set_design_top_cell)
    owner.design_navigator_panel.layer_visibility_changed.connect(
        owner._set_design_layer_visibility
    )
    owner.design_navigator_panel.design_rotate_requested.connect(
        owner._rotate_design_document
    )


def _connect_design_navigator_route_signals(
    owner: MainWindowAuxiliaryOwner,
) -> None:
    _connect_route_file_and_edit_signals(owner)
    _connect_route_measurement_signals(owner)
    _connect_design_target_signals(owner)


def _connect_route_file_and_edit_signals(owner: MainWindowAuxiliaryOwner) -> None:
    owner.design_navigator_panel.route_new_requested.connect(
        owner._create_measurement_route
    )
    owner.design_navigator_panel.route_open_requested.connect(
        owner._load_measurement_route
    )
    owner.design_navigator_panel.route_save_requested.connect(
        owner._save_measurement_route
    )
    owner.design_navigator_panel.route_save_as_requested.connect(
        owner._save_measurement_route_as
    )
    owner.design_navigator_panel.route_add_current_requested.connect(
        owner._add_current_design_route_point
    )
    owner.design_navigator_panel.route_remove_selected_requested.connect(
        owner._remove_selected_route_point
    )
    owner.design_navigator_panel.route_clear_requested.connect(
        owner._clear_measurement_route_points
    )
    owner.design_navigator_panel.route_selected.connect(owner._select_route_point)
    owner.design_navigator_panel.route_offsets_changed.connect(
        owner._set_route_needle_offsets
    )
    owner.design_navigator_panel.route_edit_enabled_changed.connect(
        owner._set_route_edit_enabled
    )


def _connect_route_measurement_signals(owner: MainWindowAuxiliaryOwner) -> None:
    owner.design_navigator_panel.route_measurement_run_requested.connect(
        owner._open_route_measurement_dialog
    )
    owner.design_navigator_panel.route_measurement_measure_requested.connect(
        route_measurement_point_request_handler_for_owner(owner)
    )
    owner.design_navigator_panel.route_measurement_stop_requested.connect(
        owner._request_stop_route_measurement
    )
    owner.design_navigator_panel.route_measurement_pause_requested.connect(
        owner._request_pause_route_measurement
    )
    owner.design_navigator_panel.route_measurement_interrupt_requested.connect(
        owner._request_route_measurement_point_correction
    )
    owner.design_navigator_panel.route_measurement_save_shift_requested.connect(
        owner._save_route_measurement_shift
    )
    owner.design_navigator_panel.route_measurement_confirmation_requested.connect(
        owner._submit_route_measurement_confirmation
    )
    owner.design_navigator_panel.route_measurement_jump_requested.connect(
        owner._submit_route_measurement_jump
    )
    owner.design_navigator_panel.route_measurement_move_requested.connect(
        owner._request_route_contact_move
    )


def _connect_design_target_signals(owner: MainWindowAuxiliaryOwner) -> None:
    owner.design_navigator_panel.move_to_target_requested.connect(
        owner._move_to_design_target
    )
    owner.design_navigator_panel.next_target_requested.connect(
        owner._select_next_design_target
    )
    owner.design_navigator_panel.previous_target_requested.connect(
        owner._select_previous_design_target
    )
    owner.design_navigator_panel.target_selected.connect(
        owner._on_design_target_selected
    )
    owner.design_navigator_panel.snap_enabled_changed.connect(
        owner._on_design_snap_enabled_changed
    )


def _connect_design_layout_window_signals(
    owner: MainWindowAuxiliaryOwner,
) -> None:
    owner.design_layout_window.calibration_point_selected.connect(
        owner._on_design_layout_point_selected
    )
    owner.design_layout_window.alignment_draft_accepted.connect(
        owner._on_alignment_draft_accepted
    )
    owner.design_layout_window.alignment_draft_discarded.connect(
        owner._on_alignment_draft_discarded
    )
    owner.design_layout_window.move_requested.connect(
        lambda x_value, y_value: owner._move_to_design_window_point(
            x_value, y_value
        )
    )
    owner.design_layout_window.route_point_requested.connect(
        owner._add_design_route_point
    )
    owner.design_layout_window.point_requested.connect(
        owner._add_design_route_point
    )
    owner.design_layout_window.guide_requested.connect(
        owner._add_design_guide
    )
    owner.design_layout_window.delete_selection_requested.connect(
        owner._delete_design_selection
    )
    owner.design_layout_window.guide_undo_requested.connect(
        owner._undo_last_design_guide
    )
    owner.design_layout_window.guide_clear_requested.connect(
        owner._clear_design_guides
    )
    owner.design_layout_window.markup_visibility_changed.connect(
        owner._set_design_markup_visibility
    )
    owner.design_layout_window.mixed_array_requested.connect(
        owner._apply_mixed_design_array
    )
    owner.design_layout_window.hover_snap_changed.connect(
        owner.design_navigator_panel.set_hover_snap
    )
    owner.design_layout_window.visibility_changed.connect(
        lambda visible: sync_design_layout_window_action(owner, visible)
    )


__all__ = [
    "create_design_layout_window",
    "open_settings_dialog",
    "show_microscope_scan_dialog",
    "show_connection_dialog",
    "show_surface_map_window",
    "sync_contact_calibration_window_action",
    "sync_design_layout_window_action",
    "toggle_contact_calibration_window",
    "toggle_design_layout_window",
]
