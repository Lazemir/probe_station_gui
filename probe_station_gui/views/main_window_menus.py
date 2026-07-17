"""Menu construction for the main probe station window."""

from __future__ import annotations

import threading
from typing import Any, Protocol

from PySide6.QtCore import Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import QMessageBox

from probe_station_gui.views import main_window_needle_calibration as needle_calibration_ui
from probe_station_gui.views.main_window_auxiliary import (
    open_settings_dialog,
    show_connection_dialog,
    show_microscope_scan_dialog,
    show_surface_map_window,
    toggle_contact_calibration_window,
    toggle_design_layout_window,
)


class MainWindowMenuOwner(Protocol):
    ALIGNMENT_CAPTURE_SHORTCUT: str
    resistance_dock: Any
    oscillation_dock: Any
    joystick_dock: Any
    alignment_dock: Any
    _sample_load_action: Any
    _sample_unload_action: Any
    _design_layout_window_action: Any
    _contact_calibration_window_action: Any
    _surface_map_window_action: Any
    _microscope_scan_action: Any
    _optical_calibration_action: Any
    _click_calibration_action: Any
    _lens_distortion_calibration_action: Any
    _ruler_action: Any
    _rect_action: Any
    _alignment_capture_action: Any
    _alignment_exit_action: Any

    def menuBar(self) -> Any: ...  # noqa: N802 - Qt naming
    def addAction(self, action: Any) -> None: ...  # noqa: N802 - Qt naming
    def _open_status_log(self) -> None: ...
    def _show_click_calibration_dialog(self) -> None: ...
    def _show_optical_calibration_wizard(self) -> None: ...
    def _show_lens_distortion_dialog(self) -> None: ...
    def _on_measure_action_toggled(self, checked: bool) -> None: ...
    def _capture_manual_alignment_center_shortcut(self) -> None: ...
    def _cancel_manual_alignment_pick(self) -> None: ...
    def _on_measure_mode_exited(self) -> None: ...


def setup_main_window_menus(owner: MainWindowMenuOwner) -> None:
    """Create main-window menus and bind them to existing owner handlers."""

    app_menu = owner.menuBar().addMenu("Application")
    navigation_menu = owner.menuBar().addMenu("Navigation")
    panels_menu = owner.menuBar().addMenu("Tools")
    calibration_menu = owner.menuBar().addMenu("Calibration")
    _add_application_actions(owner, app_menu)
    _add_navigation_actions(owner, navigation_menu)
    _add_calibration_actions(owner, calibration_menu)
    _add_dock_actions(owner, panels_menu, calibration_menu)
    _add_measurement_actions(owner, panels_menu)
    _add_alignment_shortcuts(owner)


def _add_application_actions(owner: MainWindowMenuOwner, app_menu: Any) -> None:
    settings_action = QAction("Settings", owner)
    settings_action.triggered.connect(lambda _checked=False: open_settings_dialog(owner))
    app_menu.addAction(settings_action)

    open_log_action = QAction("Open Status Log", owner)
    open_log_action.triggered.connect(owner._open_status_log)
    app_menu.addAction(open_log_action)

    serial_connection_action = QAction("Connection", owner)
    serial_connection_action.triggered.connect(
        lambda _checked=False: show_connection_dialog(owner)
    )
    app_menu.addAction(serial_connection_action)


def _add_navigation_actions(owner: MainWindowMenuOwner, navigation_menu: Any) -> None:
    owner._sample_load_action = QAction("Load Sample", owner)
    owner._sample_load_action.triggered.connect(
        lambda _checked=False: needle_calibration_ui.request_sample_load(
            owner,
            thread_factory=threading.Thread,
        )
    )
    navigation_menu.addAction(owner._sample_load_action)

    owner._sample_unload_action = QAction("Unload Sample", owner)
    owner._sample_unload_action.triggered.connect(
        lambda _checked=False: needle_calibration_ui.request_sample_unload(
            owner,
            message_box=QMessageBox,
            thread_factory=threading.Thread,
        )
    )
    navigation_menu.addAction(owner._sample_unload_action)

    owner._design_layout_window_action = QAction("Design Window", owner)
    owner._design_layout_window_action.setCheckable(True)
    owner._design_layout_window_action.toggled.connect(
        lambda visible: toggle_design_layout_window(owner, visible)
    )
    navigation_menu.addAction(owner._design_layout_window_action)


def _add_calibration_actions(owner: MainWindowMenuOwner, calibration_menu: Any) -> None:
    owner._contact_calibration_window_action = QAction(
        "Contact / Stone Calibration", owner
    )
    owner._contact_calibration_window_action.setCheckable(True)
    owner._contact_calibration_window_action.toggled.connect(
        lambda visible: toggle_contact_calibration_window(owner, visible)
    )
    calibration_menu.addAction(owner._contact_calibration_window_action)

    owner._surface_map_window_action = QAction("Surface Map", owner)
    owner._surface_map_window_action.triggered.connect(
        lambda _checked=False: show_surface_map_window(owner)
    )
    calibration_menu.addAction(owner._surface_map_window_action)

    owner._microscope_scan_action = QAction("Microscope Scan", owner)
    owner._microscope_scan_action.triggered.connect(
        lambda _checked=False: show_microscope_scan_dialog(owner)
    )
    calibration_menu.addAction(owner._microscope_scan_action)

    owner._optical_calibration_action = QAction("Optical Calibration", owner)
    owner._optical_calibration_action.triggered.connect(
        owner._show_optical_calibration_wizard
    )
    calibration_menu.addAction(owner._optical_calibration_action)

    owner._click_calibration_action = QAction("Click-to-Move Calibration", owner)
    owner._click_calibration_action.triggered.connect(
        owner._show_click_calibration_dialog
    )
    calibration_menu.addAction(owner._click_calibration_action)

    owner._lens_distortion_calibration_action = QAction(
        "Lens Distortion Calibration", owner
    )
    owner._lens_distortion_calibration_action.triggered.connect(
        owner._show_lens_distortion_dialog
    )
    calibration_menu.addAction(owner._lens_distortion_calibration_action)


def _add_dock_actions(
    owner: MainWindowMenuOwner,
    panels_menu: Any,
    calibration_menu: Any,
) -> None:
    for dock, title in (
        (owner.resistance_dock, "Resistance"),
        (owner.oscillation_dock, "Oscillation"),
        (owner.joystick_dock, "Joystick"),
    ):
        if dock is None:
            continue
        action = dock.toggleViewAction()
        action.setText(title)
        panels_menu.addAction(action)

    for dock, title in (
        (owner.alignment_dock, "Alignment"),
    ):
        if dock is None:
            continue
        action = dock.toggleViewAction()
        action.setText(title)
        calibration_menu.addAction(action)


def _add_measurement_actions(owner: MainWindowMenuOwner, panels_menu: Any) -> None:
    panels_menu.addSeparator()
    owner._ruler_action = QAction("Ruler", owner)
    owner._ruler_action.setCheckable(True)
    owner._ruler_action.setShortcut(QKeySequence("R"))
    owner._ruler_action.setShortcutContext(Qt.ApplicationShortcut)
    owner._ruler_action.toggled.connect(owner._on_measure_action_toggled)
    panels_menu.addAction(owner._ruler_action)
    owner.addAction(owner._ruler_action)

    owner._rect_action = QAction("Rectangle", owner)
    owner._rect_action.setCheckable(True)
    owner._rect_action.setShortcut(QKeySequence("T"))
    owner._rect_action.setShortcutContext(Qt.ApplicationShortcut)
    owner._rect_action.toggled.connect(owner._on_measure_action_toggled)
    panels_menu.addAction(owner._rect_action)
    owner.addAction(owner._rect_action)


def _add_alignment_shortcuts(owner: MainWindowMenuOwner) -> None:
    owner._alignment_capture_action = QAction("Capture Alignment Point", owner)
    owner._alignment_capture_action.setShortcut(
        QKeySequence(owner.ALIGNMENT_CAPTURE_SHORTCUT)
    )
    owner._alignment_capture_action.setShortcutContext(Qt.ApplicationShortcut)
    owner._alignment_capture_action.triggered.connect(
        owner._capture_manual_alignment_center_shortcut
    )
    owner.addAction(owner._alignment_capture_action)

    owner._alignment_exit_action = QAction("Cancel Alignment Pick", owner)
    owner._alignment_exit_action.setShortcut(QKeySequence(Qt.Key_Escape))
    owner._alignment_exit_action.setShortcutContext(Qt.WindowShortcut)
    owner._alignment_exit_action.triggered.connect(owner._cancel_manual_alignment_pick)
    owner._alignment_exit_action.triggered.connect(owner._on_measure_mode_exited)
    owner.addAction(owner._alignment_exit_action)


__all__ = ["setup_main_window_menus"]
