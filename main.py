"""Application entry point for the probe station GUI."""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from datetime import datetime
from pathlib import Path
import sys
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QImage, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui import (
    Grabber,
    JoystickWindow,
    MicroscopeView,
    StageController,
    SerialTerminalWindow,
)
from probe_station_gui.design_model import DesignDocument, DesignModelError
from probe_station_gui.design_script import ScriptContext, load_measurement_plan
from probe_station_gui.design_session import AlignmentPreparation, DesignSession
from probe_station_gui.dialogs.settings_dialog import SettingsDialog
from probe_station_gui.lcr_meter import LCRMeterController
from probe_station_gui.settings_manager import Settings, SettingsManager
from probe_station_gui.views.alignment_panel import AlignmentPanel
from probe_station_gui.views.contact_oscillation_window import (
    ContactOscillationWindow,
)
from probe_station_gui.views.dock_widgets import CollapsibleDockWidget
from probe_station_gui.views.needle_calibration_panel import NeedleCalibrationPanel
from probe_station_gui.views.oscillation_panel import OscillationPanel
from probe_station_gui.views.serial_connection_panel import SerialConnectionPanel


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from probe_station_gui.views.design_navigator_panel import (
        DesignLayoutWindow,
        DesignNavigatorPanel,
    )


class Main(QMainWindow):
    """Main application window wiring the camera view and serial dialog."""

    design_layout_module_ready: Signal = Signal(object, object)
    design_document_loaded: Signal = Signal(int, object, object)

    ALIGNMENT_CAPTURE_SHORTCUT = "Space"
    ALIGNMENT_TARGET_ANGLES = (-180.0, -90.0, 0.0, 90.0, 180.0)
    DESIGN_POSITION_REFRESH_MS = 800
    CONTROLLER_STATUS_REFRESH_MS = 800
    DESIGN_OVERLAY_UPDATE_MS = 120
    DESIGN_SPACING_RATIO_TOLERANCE = 0.35
    MANUAL_JOG_UPDATE_MS = 50
    MANUAL_JOG_SETTLE_POLL_DELAYS_MS = (180, 420)
    MANUAL_JOG_IGNORE_IDLE_AFTER_COMMAND_S = 0.25
    MANUAL_JOG_RECONCILE_SMOOTH_THRESHOLD_MM = 0.35
    MANUAL_JOG_RECONCILE_SMOOTH_ALPHA = 0.35
    MANUAL_JOG_STATUS_SETTLE_HOLD_S = 0.8
    MANUAL_JOG_DEFAULT_STOP_TAIL_S = 0.11
    MANUAL_JOG_STOP_TAIL_MIN_S = 0.02
    MANUAL_JOG_STOP_TAIL_MAX_S = 0.25
    MANUAL_JOG_STOP_TAIL_LEARN_ALPHA = 0.25
    PLANNED_MOVE_DURATION_PADDING_S = 0.12
    TERMINAL_REFRESH_DELAYS_MS = (180, 500)
    TERMINAL_RESET_REFRESH_DELAYS_MS = (500, 1100, 1800)
    TERMINAL_RESUME_AFTER_JOG_MS = 180
    STAGE_AXIS_NAMES = ("X", "Y", "Z", "A", "B", "C")
    B_POSITION_CHANGE_TOLERANCE_DEG = 1e-3
    CAMERA_UI_FRAME_GAP_WARNING_S = 0.25

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Microscope control")
        self.menuBar().setNativeMenuBar(False)

        self.view = MicroscopeView()
        central_container = QWidget(self)
        central_layout = QVBoxLayout(central_container)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.view, 1)
        self.setCentralWidget(central_container)
        self.serial_connection = None
        self.serial_port_name: str | None = None
        self.serial_baud_rate: int | None = None
        self.settings_manager: SettingsManager = SettingsManager()
        self.joystick_panel: JoystickWindow | None = None
        self.serial_terminal_panel: SerialTerminalWindow | None = None
        self.serial_connection_panel: SerialConnectionPanel | None = None
        self.needle_calibration_panel: NeedleCalibrationPanel | None = None
        self.oscillation_panel: OscillationPanel | None = None
        self.contact_calibration_window: ContactOscillationWindow | None = None
        self.alignment_panel: AlignmentPanel | None = None
        self.design_navigator_panel: DesignNavigatorPanel | None = None
        self.design_layout_window: DesignLayoutWindow | None = None
        self._design_layout_preload_started = False
        self._design_layout_window_requested = False
        self._design_load_generation = 0
        self.joystick_dock: CollapsibleDockWidget | None = None
        self.serial_terminal_dock: CollapsibleDockWidget | None = None
        self.serial_connection_dock: CollapsibleDockWidget | None = None
        self.needle_calibration_dock: CollapsibleDockWidget | None = None
        self.oscillation_dock: CollapsibleDockWidget | None = None
        self.alignment_dock: CollapsibleDockWidget | None = None
        self._needle_calibration_active = False
        self._alignment_capture_action: QAction | None = None
        self._alignment_exit_action: QAction | None = None
        self._contact_calibration_window_action: QAction | None = None
        self._design_layout_window_action: QAction | None = None
        self._ruler_action: QAction | None = None
        self._rect_action: QAction | None = None
        self._last_selected_design_point: tuple[float, float] | None = None
        self._current_design_stage_xy: tuple[float, float] | None = None
        self._pending_design_stage_xy: tuple[float, float] | None = None
        self._pending_alignment_preparation: AlignmentPreparation | None = None
        self._pending_quick_alignment_rotation = False
        self._manual_alignment_pick_slot: int | None = None
        self._manual_alignment_points: list[tuple[float, float] | None] = [None, None]
        self._manual_jog_stage_position: tuple[float, ...] | None = None
        self._manual_jog_stage_xy: tuple[float, float] | None = None
        self._manual_jog_axis_velocities: dict[str, float] = {}
        self._manual_jog_stop_axis_velocities: dict[str, float] = {}
        self._manual_jog_velocity_xy: tuple[float, float] | None = None
        self._manual_jog_stop_prediction_until: float | None = None
        self._manual_jog_stop_tail_position: tuple[float, ...] | None = None
        self._manual_jog_stop_tail_s = self.MANUAL_JOG_DEFAULT_STOP_TAIL_S
        self._manual_jog_command_started_at: float | None = None
        self._manual_jog_last_timestamp: float | None = None
        self._manual_jog_last_prediction_log_at = 0.0
        self._manual_jog_waiting_for_fresh_status = False
        self._manual_jog_settle_until = 0.0
        self._manual_jog_stop_status_timestamp: float | None = None
        self._planned_move_origin_xy: tuple[float, float] | None = None
        self._planned_move_stage_xy: tuple[float, float] | None = None
        self._planned_move_target_xy: tuple[float, float] | None = None
        self._planned_move_started_at: float | None = None
        self._planned_move_ends_at: float | None = None
        self._planned_move_waiting_for_fresh_status = False
        self._planned_move_stop_status_timestamp: float | None = None
        self._design_snap_enabled = True
        self._last_reported_b_position: float | None = None
        self._last_camera_frame_ui_timestamp: float | None = None
        self._stage_unhomed_display_origins: dict[str, float] = {}
        self._controller_state_persistence_suspended = False
        self._controller_reboot_recovery_scheduled = False
        self._design_session = DesignSession()
        self.statusBar()
        self._stage_position_label = QLabel("Position: unavailable", self)
        self._stage_position_label.setMinimumWidth(390)
        self._stage_position_label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._stage_position_label.setTextFormat(Qt.RichText)
        self._stage_position_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._stage_position_label.setToolTip("Current controller position")
        self.statusBar().addPermanentWidget(self._stage_position_label, 0)
        self._status_log = QPlainTextEdit(self)
        self._status_log.setReadOnly(True)
        self._status_log.setMaximumHeight(80)
        self._status_log.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._status_log.document().setMaximumBlockCount(200)
        self.statusBar().addPermanentWidget(self._status_log, 1)
        self._status_log_path = (
            self.settings_manager.log_file_path().with_name("status-history.log")
        )
        self.grabber = Grabber()
        self.thread = QThread()
        self.grabber.moveToThread(self.thread)
        self.thread.started.connect(self.grabber.start)
        self.view.clicked.connect(self.on_click)
        self.view.hovered.connect(self._on_view_hover)
        self.view.hover_left.connect(self._on_view_hover_left)
        self.view.design_minimap_clicked.connect(self._move_to_minimap_design_point)
        self.view.design_minimap_double_clicked.connect(
            lambda: self._toggle_design_layout_window(True)
        )
        self.grabber.frame_ready.connect(self._on_camera_frame)
        self.grabber.error.connect(self.on_error)
        self.design_layout_module_ready.connect(self._on_design_layout_module_ready)
        self.design_document_loaded.connect(self._on_design_document_loaded)

        self.stage_controller = StageController()
        self.stage_controller.status_message.connect(self._show_status)
        self.stage_controller.movement_finished.connect(self.on_move_finished)
        self.stage_controller.calibration_changed.connect(self.on_calibration_changed)
        self.stage_controller.autofocus_finished.connect(self.on_autofocus_finished)
        self.stage_controller.stage_position_changed.connect(self._on_stage_position_changed)
        self.stage_controller.needle_height_changed.connect(self._on_needle_height_changed)
        self.stage_controller.controller_reboot_detected.connect(
            self._on_controller_reboot_detected
        )
        self.stage_controller.controller_reboot_ready.connect(
            self._on_controller_reboot_ready
        )
        self.stage_controller.oscillation_state_changed.connect(
            self._on_oscillation_state_changed
        )
        self.stage_controller.movement_started.connect(
            lambda: self._show_status("Moving stage...")
        )
        self.grabber.frame_ready.connect(self.stage_controller.on_frame_ready)
        self.lcr_controller = LCRMeterController()
        self.lcr_controller.status_message.connect(self._show_status)
        self._design_position_timer = QTimer(self)
        self._design_position_timer.setInterval(self.DESIGN_POSITION_REFRESH_MS)
        self._design_position_timer.timeout.connect(self._refresh_design_position)
        self._design_position_timer.start()
        self._controller_status_timer = QTimer(self)
        self._controller_status_timer.setInterval(self.CONTROLLER_STATUS_REFRESH_MS)
        self._controller_status_timer.timeout.connect(self._refresh_controller_status)
        self._controller_status_timer.start()
        self._design_overlay_timer = QTimer(self)
        self._design_overlay_timer.setSingleShot(True)
        self._design_overlay_timer.setInterval(self.DESIGN_OVERLAY_UPDATE_MS)
        self._design_overlay_timer.timeout.connect(self._flush_pending_design_position)
        self._manual_jog_timer = QTimer(self)
        self._manual_jog_timer.setInterval(self.MANUAL_JOG_UPDATE_MS)
        self._manual_jog_timer.timeout.connect(self._advance_motion_prediction)
        self._needle_height_timer = QTimer(self)
        self._needle_height_timer.setInterval(400)
        self._needle_height_timer.timeout.connect(self._refresh_needle_height)

        self._create_dock_widgets()

        self._setup_menus()
        self._apply_settings()

        QTimer.singleShot(0, self._auto_connect_if_possible)
        QTimer.singleShot(0, self._prime_keyboard_focus)
        QTimer.singleShot(0, self._start_camera_thread)
        QTimer.singleShot(0, self._preload_design_layout_window)

        self.setStyleSheet(
            """
            QMainWindow::separator { width: 8px; height: 8px; background: palette(window); }
            """
        )

    def _start_camera_thread(self) -> None:
        if not self.thread.isRunning():
            self.thread.start()

    def _preload_design_layout_window(self) -> None:
        if self.design_layout_window is not None or self._design_layout_preload_started:
            return
        self._design_layout_preload_started = True

        def load_design_window_module() -> None:
            try:
                from probe_station_gui.views.design_navigator_panel import (
                    DesignLayoutWindow as design_layout_window_class,
                )
            except Exception as exc:
                self.design_layout_module_ready.emit(None, exc)
                return
            self.design_layout_module_ready.emit(design_layout_window_class, None)

        threading.Thread(
            target=load_design_window_module,
            name="DesignLayoutImport",
            daemon=True,
        ).start()

    def _on_design_layout_module_ready(
        self,
        design_layout_window_class: object,
        error: object,
    ) -> None:
        if error is not None:
            self._design_layout_preload_started = False
            logger.error("Design window preload failed: %s", error)
            self._show_status(f"Unable to prepare design window: {error}")
            return
        self._create_design_layout_window(design_layout_window_class)

    def on_click(self, dx: float, dy: float, _rel_x: float, _rel_y: float) -> None:
        if self._manual_alignment_pick_slot is not None:
            self._capture_manual_alignment_clicked(dx, dy)
            return
        self.stage_controller.request_move(dx, dy)

    def on_error(self, message: str) -> None:
        logger.error("Camera error: %s", message)

    def _on_camera_frame(self, qimg: QImage) -> None:
        now = time.monotonic()
        if self._last_camera_frame_ui_timestamp is not None:
            frame_gap = now - self._last_camera_frame_ui_timestamp
            if frame_gap > self.CAMERA_UI_FRAME_GAP_WARNING_S:
                logger.warning(
                    "Camera UI frame gap %.3fs before display update",
                    frame_gap,
                )
        self._last_camera_frame_ui_timestamp = now
        self.view.set_frame(qimg)

    def _on_view_hover(
        self, dx: float, dy: float, _rel_x: float, _rel_y: float
    ) -> None:
        preview = self.stage_controller.preview_clicked_point_xy(dx, dy)
        if preview is None:
            self._update_coordinate_display()
            return
        center_xy, cursor_xy = preview
        self._update_coordinate_display(center_xy=center_xy, cursor_xy=cursor_xy)

    def _on_view_hover_left(self) -> None:
        self._update_coordinate_display(cursor_xy=None)

    def _show_status(self, message: str, timeout_ms: int = 0) -> None:
        if message:
            self.statusBar().showMessage(message, timeout_ms)
            self._status_log.appendPlainText(message)
            self._append_status_log(message)

    def _append_status_log(self, message: str) -> None:
        if not message:
            return
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"{timestamp} {message}\n"
        path: Path = self._status_log_path
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
        except OSError as exc:
            logger.warning("Failed to write status log: %s", exc)

    def _open_status_log(self) -> None:
        path: Path = self._status_log_path
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def on_serial_connected(self, serial_port) -> None:
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        self.serial_connection = serial_port
        self.serial_port_name = serial_port.port
        try:
            baud_rate = int(serial_port.baudrate)
        except TypeError:
            baud_rate = int(float(serial_port.baudrate))
        self.serial_baud_rate = baud_rate
        logger.info(
            "Serial connected: %s @ %s baud",
            self.serial_connection.port,
            self.serial_connection.baudrate,
        )
        self._stage_unhomed_display_origins.clear()
        self._last_reported_b_position = None
        cached_state = self.settings_manager.load_controller_state()
        self._controller_state_persistence_suspended = True
        try:
            self.stage_controller.set_serial(self.serial_connection)
        finally:
            self._controller_state_persistence_suspended = False
        self._restore_persisted_controller_state(
            cached_state,
            cache_already_loaded=True,
        )
        if self.joystick_panel and self.joystick_dock:
            self.joystick_panel.set_serial(self.serial_connection)
            self.joystick_dock.setVisible(True)
            self.joystick_dock.raise_()
            if self.joystick_dock.isFloating():
                self.joystick_dock.activateWindow()
        if self.serial_terminal_panel and self.serial_terminal_dock:
            self.serial_terminal_panel.set_serial(self.serial_connection)
            self.serial_terminal_dock.setVisible(True)
            self.serial_terminal_dock.raise_()
            if self.serial_terminal_dock.isFloating():
                self.serial_terminal_dock.activateWindow()
        QTimer.singleShot(0, self._run_serial_startup_sync)
        self._refresh_design_position()

    def on_serial_disconnected(self) -> None:
        self._stop_jog_before_serial_close("serial disconnect")
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        self.serial_connection = None
        self._stage_unhomed_display_origins.clear()
        self._last_reported_b_position = None
        self._manual_jog_timer.stop()
        self._manual_jog_stage_position = None
        self._manual_jog_stage_xy = None
        self._manual_jog_axis_velocities.clear()
        self._manual_jog_stop_axis_velocities.clear()
        self._manual_jog_velocity_xy = None
        self._manual_jog_stop_prediction_until = None
        self._manual_jog_stop_tail_position = None
        self._manual_jog_command_started_at = None
        self._manual_jog_last_timestamp = None
        self._manual_jog_waiting_for_fresh_status = False
        self._manual_jog_settle_until = 0.0
        self._manual_jog_stop_status_timestamp = None
        self._controller_reboot_recovery_scheduled = False
        self._clear_planned_move_prediction(clear_wait_state=True)
        logger.info("Serial disconnected")
        self.stage_controller.request_stop_oscillation()
        self._stop_needle_calibration()
        self._controller_state_persistence_suspended = True
        try:
            self.stage_controller.set_serial(None)
        finally:
            self._controller_state_persistence_suspended = False
        self._update_stage_position_display(None)
        auto_retry = self.sender() is not self.serial_connection_panel
        if self.serial_connection_panel:
            self.serial_connection_panel.handle_external_disconnect(auto_retry=auto_retry)
        if self.joystick_panel:
            self.joystick_panel.set_serial(None)
        if self.serial_terminal_panel:
            self.serial_terminal_panel.set_serial(None)
        if self.needle_calibration_panel:
            self.needle_calibration_panel.set_current_a(None)
        if self.contact_calibration_window is not None:
            self.contact_calibration_window.set_current_stage_position(None)
        if self.oscillation_panel:
            self.oscillation_panel.set_running(False, "")
        self._reset_manual_alignment(cancel_pick=True)
        self._invalidate_design_registration(
            "Design registration cleared after serial disconnect."
        )
        self._update_design_position(None)

    def _auto_connect_if_possible(self) -> None:
        if self.serial_connection_panel and not self.serial_connection:
            logger.debug("Attempting auto-connect through connection panel")
            self.serial_connection_panel.auto_connect()
        needle_settings = self.settings_manager.needle_calibration_configuration()
        if (
            needle_settings.visa_resource.strip()
            and not self.lcr_controller.is_connected()
        ):
            logger.debug(
                "Attempting LCR auto-connect to %s",
                needle_settings.visa_resource,
            )
            self.lcr_controller.request_connect()

    def _run_serial_startup_sync(self) -> None:
        if self.serial_connection is None or not self.serial_connection.is_open:
            return
        self.stage_controller.request_startup_sync(
            auto_home_a=True,
            clear_unverified_state=False,
        )

    def _on_controller_reboot_detected(self) -> None:
        self._stage_unhomed_display_origins.clear()

    def _on_controller_reboot_ready(self) -> None:
        if self._controller_reboot_recovery_scheduled:
            return
        self._controller_reboot_recovery_scheduled = True
        QTimer.singleShot(0, self._run_controller_reboot_recovery)

    def _run_controller_reboot_recovery(self) -> None:
        self._controller_reboot_recovery_scheduled = False
        self._run_serial_startup_sync()

    def _prime_keyboard_focus(self) -> None:
        if not self.isVisible():
            return
        self.raise_()
        self.activateWindow()
        self.view.setFocus(Qt.ActiveWindowFocusReason)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        QTimer.singleShot(0, self._prime_keyboard_focus)

    def _restore_persisted_controller_state(
        self,
        cached_state: dict | None = None,
        *,
        cache_already_loaded: bool = False,
    ) -> None:
        if self.serial_connection is None or not self.serial_connection.is_open:
            return
        if not cache_already_loaded:
            cached_state = self.settings_manager.load_controller_state()
        if not cached_state:
            logger.info("No cached controller homing state found for this connection.")
            return
        if not self.stage_controller.cached_controller_session_is_current(cached_state):
            self.settings_manager.clear_controller_state()
            self.stage_controller.clear_cached_controller_state()
            self._show_status(
                "Controller session changed. Cleared cached homing state."
            )
            return
        logger.info(
            "Controller session marker matches; restoring cached homing state pending live status."
        )
        self.stage_controller.import_cached_controller_state(cached_state)
        self._show_status("Restored cached homing state; reading live coordinates.")

    def _persist_controller_state(self, *_args) -> None:
        if self._controller_state_persistence_suspended:
            logger.debug("Skipping controller state persistence while serial state resets.")
            return
        self.settings_manager.save_controller_state(
            self.stage_controller.export_cached_controller_state()
        )

    def _setup_menus(self) -> None:
        app_menu = self.menuBar().addMenu("Application")
        panels_menu = self.menuBar().addMenu("Tools")
        calibration_menu = self.menuBar().addMenu("Calibration")

        settings_action = QAction("Settings…", self)
        settings_action.setText("Settings...")
        settings_action.triggered.connect(self._open_settings_dialog)
        app_menu.addAction(settings_action)

        open_log_action = QAction("Open Status Log…", self)
        open_log_action.setText("Open Status Log...")
        open_log_action.triggered.connect(self._open_status_log)
        app_menu.addAction(open_log_action)

        self._design_layout_window_action = QAction("Design Window", self)
        self._design_layout_window_action.setCheckable(True)
        self._design_layout_window_action.toggled.connect(
            self._toggle_design_layout_window
        )
        calibration_menu.addAction(self._design_layout_window_action)

        self._contact_calibration_window_action = QAction(
            "Contact / Stone Calibration", self
        )
        self._contact_calibration_window_action.setCheckable(True)
        self._contact_calibration_window_action.toggled.connect(
            self._toggle_contact_calibration_window
        )
        calibration_menu.addAction(self._contact_calibration_window_action)

        for dock, title in (
            (self.oscillation_dock, "Oscillation"),
            (self.serial_connection_dock, "Connection"),
            (self.joystick_dock, "Joystick"),
            (self.serial_terminal_dock, "Serial Terminal"),
        ):
            if dock is None:
                continue
            action = dock.toggleViewAction()
            action.setText(title)
            panels_menu.addAction(action)

        for dock, title in (
            (self.alignment_dock, "Alignment"),
            (self.needle_calibration_dock, "Needle Calibration"),
        ):
            if dock is None:
                continue
            action = dock.toggleViewAction()
            action.setText(title)
            calibration_menu.addAction(action)

        panels_menu.addSeparator()
        self._ruler_action = QAction("Ruler", self)
        self._ruler_action.setCheckable(True)
        self._ruler_action.setShortcut(QKeySequence("R"))
        self._ruler_action.setShortcutContext(Qt.ApplicationShortcut)
        self._ruler_action.toggled.connect(self._on_measure_action_toggled)
        panels_menu.addAction(self._ruler_action)
        self.addAction(self._ruler_action)

        self._rect_action = QAction("Rectangle", self)
        self._rect_action.setCheckable(True)
        self._rect_action.setShortcut(QKeySequence("T"))
        self._rect_action.setShortcutContext(Qt.ApplicationShortcut)
        self._rect_action.toggled.connect(self._on_measure_action_toggled)
        panels_menu.addAction(self._rect_action)
        self.addAction(self._rect_action)

        self._alignment_capture_action = QAction("Capture Alignment Point", self)
        self._alignment_capture_action.setShortcut(
            QKeySequence(self.ALIGNMENT_CAPTURE_SHORTCUT)
        )
        self._alignment_capture_action.setShortcutContext(Qt.ApplicationShortcut)
        self._alignment_capture_action.triggered.connect(
            self._capture_manual_alignment_center_shortcut
        )
        self.addAction(self._alignment_capture_action)

        self._alignment_exit_action = QAction("Cancel Alignment Pick", self)
        self._alignment_exit_action.setShortcut(QKeySequence(Qt.Key_Escape))
        self._alignment_exit_action.setShortcutContext(Qt.ApplicationShortcut)
        self._alignment_exit_action.triggered.connect(self._cancel_manual_alignment_pick)
        self._alignment_exit_action.triggered.connect(self._on_measure_mode_exited)
        self.addAction(self._alignment_exit_action)

    def _toggle_design_layout_window(self, visible: bool) -> None:
        if self.design_layout_window is None:
            self._design_layout_window_requested = bool(visible)
            if visible:
                self._show_status("Preparing design window...")
                self._preload_design_layout_window()
            return
        if visible:
            self._design_layout_window_requested = True
            self.design_layout_window.show_and_raise()
            self._collapse_alignment_panel_if_ready()
        else:
            self._design_layout_window_requested = False
            self.design_layout_window.hide()

    def _on_design_layout_window_visibility_changed(self, visible: bool) -> None:
        if self._design_layout_window_action is None:
            return
        self._design_layout_window_action.blockSignals(True)
        self._design_layout_window_action.setChecked(visible)
        self._design_layout_window_action.blockSignals(False)
        if visible:
            self._collapse_alignment_panel_if_ready()

    def _toggle_contact_calibration_window(self, visible: bool) -> None:
        if self.contact_calibration_window is None:
            if self._contact_calibration_window_action is not None:
                self._contact_calibration_window_action.blockSignals(True)
                self._contact_calibration_window_action.setChecked(False)
                self._contact_calibration_window_action.blockSignals(False)
            return
        if visible:
            self.contact_calibration_window.show_and_raise()
            return
        self.contact_calibration_window.hide()

    def _on_contact_calibration_window_visibility_changed(self, visible: bool) -> None:
        if self._contact_calibration_window_action is None:
            return
        self._contact_calibration_window_action.blockSignals(True)
        self._contact_calibration_window_action.setChecked(visible)
        self._contact_calibration_window_action.blockSignals(False)

    def _on_design_layout_point_selected(
        self, slot: int, x_value: float, y_value: float
    ) -> None:
        document = self._design_session.document
        if document is None:
            return
        if slot not in (0, 1):
            return
        snapped_point = (float(x_value), float(y_value))
        self._manual_alignment_pick_slot = None
        self._manual_alignment_points = [None, None]
        self._pending_alignment_preparation = None
        self._design_session.clear_source_stage_marks()
        self._last_selected_design_point = snapped_point
        self._set_design_snap_enabled(True)
        self._design_session.set_source_design_mark(slot, snapped_point)
        self._refresh_design_panel()
        slot_label = "1" if slot == 0 else "2"
        self._show_status(
            f"Design mark {slot_label} snapped to X={snapped_point[0]:.3f}, Y={snapped_point[1]:.3f}.",
            4000,
        )

    def _apply_settings(self) -> None:
        if self.joystick_panel:
            bindings = self.settings_manager.control_bindings()
            self.joystick_panel.apply_control_bindings(bindings)
            logger.debug("Joystick bindings reapplied from settings")
            jog = self.settings_manager.jog_configuration()
            self.joystick_panel.apply_jog_settings(
                jog.linear_distance_mm,
                jog.rotary_distance_deg,
                jog.motion_safety_disabled,
                jog.show_axis_a_controls,
                jog.show_axis_b_controls,
                jog.manual_axis_controls_enabled,
                jog.manual_axis,
                jog.manual_axis_distance_mm,
                jog.manual_axis_mode,
                jog.manual_axis_feedrate_mm_min,
            )
            logger.debug(
                "Joystick jog settings reapplied: linear_distance_mm=%s rotary_distance_deg=%s safety_disabled=%s axis_a=%s axis_b=%s manual=%s manual_axis=%s manual_axis_distance_mm=%s manual_mode=%s manual_feedrate_mm_min=%s",
                jog.linear_distance_mm,
                jog.rotary_distance_deg,
                jog.motion_safety_disabled,
                jog.show_axis_a_controls,
                jog.show_axis_b_controls,
                jog.manual_axis_controls_enabled,
                jog.manual_axis,
                jog.manual_axis_distance_mm,
                jog.manual_axis_mode,
                jog.manual_axis_feedrate_mm_min,
            )
        jog = self.settings_manager.jog_configuration()
        self.stage_controller.set_motion_safety_disabled(jog.motion_safety_disabled)
        needle_settings = self.settings_manager.needle_calibration_configuration()
        oscillation_settings = self.settings_manager.oscillation_configuration()
        self.stage_controller.apply_needle_calibration(
            down_position_mm=(
                needle_settings.down_position_mm
                if needle_settings.down_position_configured
                else None
            ),
        )
        coordinate_settings = self.settings_manager.coordinate_system_configuration()
        self.stage_controller.apply_coordinate_system_configuration(
            position_mode=coordinate_settings.position_mode,
            startup_mode=coordinate_settings.startup_mode,
            preferred_system=coordinate_settings.preferred_system,
        )
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_design_dialog_directory(
                self.settings_manager.design_last_directory()
            )
        if self.design_layout_window is not None:
            self.design_layout_window.set_snap_enabled(self._design_snap_enabled)
        self.lcr_controller.apply_configuration(
            resource_name=needle_settings.visa_resource,
            measurement_function=needle_settings.measurement_function,
            range_mode=needle_settings.range_mode,
            auto_range_enabled=needle_settings.auto_range_enabled,
            impedance_range=needle_settings.impedance_range,
            dcr_range=needle_settings.dcr_range,
            frequency_hz=needle_settings.frequency_hz,
            level_mode=needle_settings.level_mode,
            voltage_level_v=needle_settings.voltage_level_v,
            current_level_a=needle_settings.current_level_a,
            source_resistance_ohm=needle_settings.source_resistance_ohm,
            aperture_rate=needle_settings.aperture_rate,
            aperture_averages=needle_settings.aperture_averages,
            trigger_source=needle_settings.trigger_source,
            trigger_delay_s=needle_settings.trigger_delay_s,
            bias_enabled=needle_settings.bias_enabled,
            bias_level_v=needle_settings.bias_level_v,
            monitor1=needle_settings.monitor1,
            monitor2=needle_settings.monitor2,
            alc_enabled=needle_settings.alc_enabled,
            short_threshold_ohm=needle_settings.short_threshold_ohm,
            poll_interval_ms=needle_settings.poll_interval_ms,
        )
        self.lcr_controller.request_reconfigure()
        if self.needle_calibration_panel:
            self.needle_calibration_panel.apply_configuration(
                resource_name=needle_settings.visa_resource,
                saved_height=(
                    needle_settings.down_position_mm
                    if needle_settings.down_position_configured
                    else None
                ),
                short_threshold_ohm=needle_settings.short_threshold_ohm,
                measurement_function=needle_settings.measurement_function,
            )
        if self.contact_calibration_window is not None:
            self.contact_calibration_window.set_saved_surface_position(
                "chip",
                needle_settings.chip_position,
            )
            self.contact_calibration_window.set_saved_surface_position(
                "stone",
                needle_settings.stone_position,
            )
        if self.oscillation_panel:
            self.oscillation_panel.apply_configuration(
                mode=oscillation_settings.mode,
                amplitude_mm=oscillation_settings.amplitude_mm,
                feedrate_mm_min=oscillation_settings.feedrate_mm_min,
                turns_per_sweep=oscillation_settings.turns_per_sweep,
            )
        if self.serial_connection and self.serial_connection.is_open:
            self.stage_controller.request_startup_sync(auto_home_a=False)
        self._update_coordinate_display(cursor_xy=None)

    def _open_settings_dialog(self) -> None:
        dialog = SettingsDialog(self.settings_manager.settings, self)
        dialog.settings_applied.connect(self._apply_settings_from_dialog)
        if dialog.exec() != QDialog.Accepted:
            if not dialog.was_applied():
                logger.debug("Settings dialog cancelled")

    def _apply_settings_from_dialog(self, new_settings: object) -> None:
        if not isinstance(new_settings, Settings):
            return
        self.settings_manager.replace(new_settings)
        self.settings_manager.save()
        self._apply_settings()
        logger.info("Settings updated from dialog")

    def _design_backed_alignment_active(self) -> bool:
        return self._design_session.has_complete_source_design_marks()

    def _design_window_is_open(self) -> bool:
        return self.design_layout_window is not None and self.design_layout_window.isVisible()

    def _current_alignment_points(self) -> list[tuple[float, float] | None]:
        if self._design_backed_alignment_active():
            return list(self._design_session.source_stage_marks[:2])
        return list(self._manual_alignment_points[:2])

    def _collapse_alignment_panel_if_ready(self) -> None:
        if self.alignment_dock is None or not self._design_window_is_open():
            return
        registration = self._design_session.registration
        if registration is not None and registration.valid:
            self.alignment_dock.set_collapsed(True)

    def _collapse_alignment_panel_if_design_open(self) -> None:
        if self.alignment_dock is None or not self._design_window_is_open():
            return
        self.alignment_dock.set_collapsed(True)

    def _set_alignment_panel_expanded(self) -> None:
        if self.alignment_dock is None:
            return
        self.alignment_dock.setVisible(True)
        self.alignment_dock.set_collapsed(False)
        self.alignment_dock.raise_()

    def _arm_manual_alignment_pick(self, slot: int) -> None:
        if slot not in (0, 1):
            return
        self._manual_alignment_pick_slot = slot
        self._set_alignment_panel_expanded()
        self._refresh_manual_alignment_ui()
        self._update_coordinate_display(cursor_xy=None)
        self._show_status(
            f"Chip alignment: pick point {slot + 1} in the image, or press Space to capture the crosshair center.",
            6000,
        )

    def _cancel_manual_alignment_pick(self) -> None:
        if self._manual_alignment_pick_slot is None:
            return
        self._manual_alignment_pick_slot = None
        self._refresh_manual_alignment_ui()
        self._update_coordinate_display(cursor_xy=None)
        self._show_status("Chip alignment image pick cancelled.", 3000)

    def _reset_manual_alignment(self, *, cancel_pick: bool = True) -> None:
        self._manual_alignment_points = [None, None]
        if cancel_pick:
            self._manual_alignment_pick_slot = None
        self._refresh_manual_alignment_ui()
        self._update_coordinate_display(cursor_xy=None)

    def _reset_alignment_capture_points(self) -> None:
        if self._design_backed_alignment_active():
            self._pending_alignment_preparation = None
            self._pending_quick_alignment_rotation = False
            self._design_session.clear_source_stage_marks()
            self._set_design_snap_enabled(True)
            self._refresh_design_panel()
            self._refresh_design_position()
        else:
            self._reset_manual_alignment(cancel_pick=False)
            self._pending_quick_alignment_rotation = False
        self._manual_alignment_pick_slot = None
        self._set_alignment_panel_expanded()
        self._refresh_manual_alignment_ui()
        self._update_coordinate_display(cursor_xy=None)

    def _capture_manual_alignment_center_shortcut(self) -> None:
        if self._manual_alignment_pick_slot is None:
            return
        self._capture_manual_alignment_center(self._manual_alignment_pick_slot)

    def _resolve_alignment_capture_stage_position(
        self,
    ) -> tuple[float, float] | None:
        try:
            stage_position = self.stage_controller.current_stage_position()
        except Exception as exc:
            if str(exc) != "Unable to read stage position.":
                self._show_status(str(exc), 5000)
                return None
            latest = self.stage_controller.latest_stage_position()
            if latest is None or len(latest) < 2:
                self.stage_controller.request_status_refresh()
                self._show_status(str(exc), 5000)
                return None
            stage_position = latest
        if len(stage_position) < 2:
            self.stage_controller.request_status_refresh()
            self._show_status("X/Y coordinates are unavailable.", 5000)
            return None
        return (float(stage_position[0]), float(stage_position[1]))

    def _capture_manual_alignment_center(self, slot: int) -> None:
        if slot not in (0, 1):
            return
        center_xy = self._resolve_alignment_capture_stage_position()
        if center_xy is None:
            return
        self._capture_manual_alignment_point(slot, center_xy, source="center")

    def _request_alignment_capture(self, slot: int, mode: str) -> None:
        if mode == "image":
            self._arm_manual_alignment_pick(slot)
            return
        self._capture_manual_alignment_center(slot)

    def _zero_b_axis(self) -> None:
        try:
            self._invalidate_design_registration(
                "Design registration cleared after B-axis zeroing."
            )
            self.stage_controller.zero_b_axis()
        except Exception as exc:
            self._show_status(str(exc), 5000)

    def _reset_click_calibration(self) -> None:
        try:
            self.stage_controller.reset_calibration()
        except Exception as exc:
            self._show_status(str(exc), 5000)

    def _capture_manual_alignment_clicked(
        self, dx_pixels: float = 0.0, dy_pixels: float = 0.0
    ) -> None:
        slot = self._manual_alignment_pick_slot
        if slot is None:
            return
        try:
            center_xy, captured = self.stage_controller.resolve_clicked_point_xy(
                dx_pixels,
                dy_pixels,
            )
        except Exception as exc:
            self._show_status(str(exc), 5000)
            return
        self._update_coordinate_display(center_xy=center_xy, cursor_xy=captured)
        self._capture_manual_alignment_point(slot, captured, source="image")

    def _capture_manual_alignment_point(
        self, slot: int, captured: tuple[float, float], *, source: str
    ) -> None:
        if slot not in (0, 1):
            return
        self._manual_alignment_pick_slot = None
        self._refresh_manual_alignment_ui()

        label = "image" if source == "image" else "center"
        if self._design_backed_alignment_active():
            self._pending_alignment_preparation = None
            self._design_session.set_source_stage_mark(slot, captured)
            self._refresh_design_panel()
            self._refresh_design_position()
            pair_count = self._design_session.source_pair_count()
            if pair_count < 2:
                self._set_alignment_panel_expanded()
                self._show_status(
                    f"Design alignment: point {slot + 1} captured from {label} at X={captured[0]:.3f}, Y={captured[1]:.3f}. Capture the other point next.",
                    6000,
                )
                return
            try:
                preparation = self._design_session.prepare_source_alignment()
            except DesignModelError as exc:
                self._show_status(str(exc), 7000)
                return
            if not self._design_spacing_ratio_is_reasonable(preparation.distance_ratio):
                self._show_status(
                    "Design calibration aborted: mark spacing mismatch. "
                    f"Design {preparation.design_distance_mm:.4f} mm vs chip {preparation.stage_distance_mm:.4f} mm.",
                    8000,
                )
                return
            if abs(preparation.rotation_deg) < 1e-3:
                self._design_session.apply_prepared_alignment(preparation)
                self._set_design_snap_enabled(False)
                self._refresh_design_panel()
                self._refresh_design_position()
                self._collapse_alignment_panel_if_ready()
                self._show_status(
                    "Design calibration complete. "
                    f"Spacing ratio {preparation.distance_ratio:.3f}.",
                    7000,
                )
                return
            self._pending_alignment_preparation = preparation
            self._set_alignment_panel_expanded()
            self._show_status(
                "Two mark pairs captured. "
                f"Rotating chip by {preparation.rotation_deg:+.3f} deg to match the design.",
                7000,
            )
            self.stage_controller.request_rotate_b(preparation.rotation_deg)
            return

        self._manual_alignment_points[slot] = captured
        self._set_alignment_panel_expanded()
        self._refresh_manual_alignment_ui()
        other_slot = 1 - slot
        if self._manual_alignment_points[other_slot] is None:
            self._show_status(
                f"Chip alignment: point {slot + 1} captured from {label} at X={captured[0]:.3f}, Y={captured[1]:.3f}. Capture point {other_slot + 1} next.",
                6000,
            )
            return

        first_point = self._manual_alignment_points[0]
        second_point = self._manual_alignment_points[1]
        if first_point is None or second_point is None:
            return
        rotation_deg = self._calculate_alignment_rotation(first_point, second_point)
        if rotation_deg is None:
            self._show_status(
                "Chip alignment points are too close together. Capture two distinct points.",
                5000,
            )
            return
        if abs(rotation_deg) < 1e-3:
            self._collapse_alignment_panel_if_design_open()
            self._show_status("Chip alignment points are already aligned.", 5000)
            return

        self._show_status(
            f"Chip alignment: rotating B by {rotation_deg:+.3f} deg.",
            5000,
        )
        self._invalidate_design_registration(
            "Design registration cleared after B-axis rotation."
        )
        self._pending_quick_alignment_rotation = True
        self.stage_controller.request_rotate_b(rotation_deg)

    def _refresh_manual_alignment_ui(self) -> None:
        if self.alignment_panel is not None:
            self.alignment_panel.set_design_marks(self._design_session.source_design_marks)
            self.alignment_panel.set_captured_points(self._current_alignment_points())
            self.alignment_panel.set_pick_slot(self._manual_alignment_pick_slot)
            self.alignment_panel.set_registration_status(
                self._design_session.registration_status
            )
        if self._manual_alignment_pick_slot is None:
            instruction = ""
        else:
            instruction = (
                f"Chip alignment: click point {self._manual_alignment_pick_slot + 1} "
                "or press Space for the center."
            )
        self.view.set_alignment_mode(self._manual_alignment_pick_slot is not None)
        self.view.set_alignment_instruction(instruction)

    def _update_coordinate_display(
        self,
        *,
        center_xy: tuple[float, float] | None = None,
        cursor_xy: tuple[float, float] | None = None,
    ) -> None:
        latest = self.stage_controller.latest_stage_position()
        if (
            center_xy is None
            and latest is not None
            and len(latest) >= 2
            and self.stage_controller.axes_are_homed({"X", "Y"})
        ):
            center_xy = (float(latest[0]), float(latest[1]))
        if self.alignment_panel is not None:
            self.alignment_panel.set_coordinate_labels(
                self._format_active_coordinate_label("Center", center_xy),
                self._format_active_coordinate_label("Cursor", cursor_xy),
            )

    def _manual_jog_prediction_active(self) -> bool:
        if self._manual_jog_axis_velocities:
            return True
        if (
            self._manual_jog_stop_axis_velocities
            and self._manual_jog_stop_prediction_until is not None
        ):
            now = time.monotonic()
            return (
                now < self._manual_jog_stop_prediction_until
                or (
                    self._manual_jog_last_timestamp is not None
                    and self._manual_jog_last_timestamp
                    < self._manual_jog_stop_prediction_until
                )
            )
        return False

    def _manual_jog_prediction_available(self) -> bool:
        return (
            self._manual_jog_prediction_active()
            or bool(self._manual_jog_stop_axis_velocities)
            or (
                self._manual_jog_waiting_for_fresh_status
                and self._manual_jog_stage_position is not None
            )
        )

    def _manual_jog_prediction_velocities(self) -> dict[str, float]:
        if self._manual_jog_axis_velocities:
            return self._manual_jog_axis_velocities
        if self._manual_jog_prediction_active():
            return self._manual_jog_stop_axis_velocities
        return {}

    def _clear_manual_jog_stop_prediction(self) -> None:
        self._manual_jog_stop_axis_velocities.clear()
        self._manual_jog_stop_prediction_until = None
        self._manual_jog_stop_tail_position = None

    @staticmethod
    def _stage_xy_from_position(
        position: object | None,
    ) -> tuple[float, float] | None:
        if not isinstance(position, (tuple, list)) or len(position) < 2:
            return None
        try:
            return (float(position[0]), float(position[1]))
        except (TypeError, ValueError):
            return None

    def _position_with_stage_xy(
        self,
        stage_xy: tuple[float, float],
        *,
        base_position: object | None = None,
    ) -> tuple[float, ...]:
        position = base_position
        if not isinstance(position, (tuple, list)) or len(position) < 2:
            position = self.stage_controller.latest_stage_position()
        if isinstance(position, (tuple, list)) and len(position) >= 2:
            try:
                values = [float(value) for value in position]
            except (TypeError, ValueError):
                values = []
        else:
            values = []
        if len(values) < 2:
            values = [float(stage_xy[0]), float(stage_xy[1])]
        else:
            values[0] = float(stage_xy[0])
            values[1] = float(stage_xy[1])
        return tuple(values)

    def _seed_motion_prediction_position(self) -> tuple[float, ...] | None:
        if self._manual_jog_stage_position is not None:
            return tuple(float(value) for value in self._manual_jog_stage_position)
        if self._manual_jog_stage_xy is not None:
            return self._position_with_stage_xy(self._manual_jog_stage_xy)
        latest = self.stage_controller.latest_stage_position()
        if isinstance(latest, tuple) and len(latest) >= 2:
            try:
                return tuple(float(value) for value in latest)
            except (TypeError, ValueError):
                pass
        if self._current_design_stage_xy is not None:
            return self._position_with_stage_xy(
                self._current_design_stage_xy,
                base_position=None,
            )
        return None

    def _publish_stage_position_estimate(
        self,
        position: tuple[float, ...] | None,
    ) -> None:
        stage_xy = self._stage_xy_from_position(position)
        design_stage_xy = (
            stage_xy if self.stage_controller.axes_are_homed({"X", "Y"}) else None
        )
        self._update_stage_position_display(position)
        self._update_coordinate_display(center_xy=design_stage_xy)
        self._update_design_position(design_stage_xy)

    def _learn_manual_jog_stop_tail(
        self,
        predicted_position: object | None,
        actual_position: object | None,
    ) -> None:
        if not self._manual_jog_stop_axis_velocities:
            return
        if not isinstance(predicted_position, (tuple, list)) or not isinstance(
            actual_position, (tuple, list)
        ):
            return
        speed_sq = 0.0
        projected_error = 0.0
        for axis, velocity in self._manual_jog_stop_axis_velocities.items():
            try:
                axis_index = self.STAGE_AXIS_NAMES.index(axis)
            except ValueError:
                continue
            if axis_index >= len(predicted_position) or axis_index >= len(actual_position):
                continue
            try:
                predicted_value = float(predicted_position[axis_index])
                actual_value = float(actual_position[axis_index])
            except (TypeError, ValueError):
                continue
            speed_sq += velocity * velocity
            projected_error += (actual_value - predicted_value) * velocity
        if speed_sq <= 1e-9:
            return
        residual_s = projected_error / speed_sq
        old_tail_s = self._manual_jog_stop_tail_s
        learned_tail_s = min(
            self.MANUAL_JOG_STOP_TAIL_MAX_S,
            max(
                self.MANUAL_JOG_STOP_TAIL_MIN_S,
                old_tail_s + residual_s,
            ),
        )
        self._manual_jog_stop_tail_s = (
            old_tail_s
            + (learned_tail_s - old_tail_s) * self.MANUAL_JOG_STOP_TAIL_LEARN_ALPHA
        )
        logger.debug(
            "MOTION PREDICTION stop_tail_learn old=%.4f residual=%.4f learned=%.4f new=%.4f",
            old_tail_s,
            residual_s,
            learned_tail_s,
            self._manual_jog_stop_tail_s,
        )

    def _preferred_design_stage_xy(self) -> tuple[float, float] | None:
        if self._manual_jog_prediction_available():
            stage_xy = self._stage_xy_from_position(self._manual_jog_stage_position)
            if stage_xy is not None:
                return stage_xy
            if self._manual_jog_stage_xy is not None:
                return self._manual_jog_stage_xy
        if (
            self._planned_move_started_at is not None
            and self._planned_move_stage_xy is not None
        ):
            return self._planned_move_stage_xy
        if self._manual_jog_waiting_for_fresh_status:
            last_status_timestamp = self.stage_controller.last_status_timestamp()
            if (
                last_status_timestamp is not None
                and self._manual_jog_stop_status_timestamp is not None
                and last_status_timestamp > self._manual_jog_stop_status_timestamp
                and (self.stage_controller.latest_stage_state() or "").lower()
                == "idle"
            ):
                self._manual_jog_waiting_for_fresh_status = False
                self._manual_jog_settle_until = 0.0
                self._manual_jog_stop_status_timestamp = None
                self._clear_manual_jog_stop_prediction()
            elif (
                self._manual_jog_stage_xy is not None
                and time.monotonic() < self._manual_jog_settle_until
            ):
                return self._manual_jog_stage_xy
            else:
                self._manual_jog_waiting_for_fresh_status = False
                self._manual_jog_settle_until = 0.0
                self._manual_jog_stop_status_timestamp = None
                self._clear_manual_jog_stop_prediction()
        if self._planned_move_waiting_for_fresh_status:
            last_status_timestamp = self.stage_controller.last_status_timestamp()
            if (
                last_status_timestamp is not None
                and self._planned_move_stop_status_timestamp is not None
                and last_status_timestamp > self._planned_move_stop_status_timestamp
            ):
                self._planned_move_waiting_for_fresh_status = False
                self._planned_move_stop_status_timestamp = None
            elif self._planned_move_stage_xy is not None:
                return self._planned_move_stage_xy
            else:
                self._planned_move_waiting_for_fresh_status = False
                self._planned_move_stop_status_timestamp = None
        latest = self.stage_controller.latest_stage_position()
        if latest is None or len(latest) < 2:
            return None
        if not self.stage_controller.axes_are_homed({"X", "Y"}):
            return None
        return (float(latest[0]), float(latest[1]))

    def _format_coordinate_label(
        self, prefix: str, fluidnc_xy: tuple[float, float] | None
    ) -> str:
        if fluidnc_xy is None:
            return f"{prefix}: unavailable"
        systems = self._resolve_coordinate_systems(fluidnc_xy)
        parts = [
            f"{name} X={coords[0]:.3f}, Y={coords[1]:.3f}"
            for name, coords in systems.items()
        ]
        return f"{prefix}: " + " | ".join(parts)

    def _format_active_coordinate_label(
        self, prefix: str, fluidnc_xy: tuple[float, float] | None
    ) -> str:
        if fluidnc_xy is None:
            return f"{prefix}: unavailable"
        return (
            f"{prefix}: {self.stage_controller.coordinate_display_name()} "
            f"X={fluidnc_xy[0]:.3f}, Y={fluidnc_xy[1]:.3f}"
        )

    def _resolve_coordinate_systems(
        self, fluidnc_xy: tuple[float, float]
    ) -> dict[str, tuple[float, float]]:
        coordinates = {f"FluidNC {self.stage_controller.coordinate_display_name()}": fluidnc_xy}
        chip_xy = self._resolve_chip_coordinates(fluidnc_xy)
        if chip_xy is not None:
            coordinates["Chip/Stage registered"] = chip_xy
        design_xy = self._resolve_design_coordinates(fluidnc_xy)
        if design_xy is not None:
            coordinates["Design"] = design_xy
        return coordinates

    def _resolve_chip_coordinates(
        self, fluidnc_xy: tuple[float, float]
    ) -> tuple[float, float] | None:
        registration = self._design_session.registration
        if registration is None or not registration.valid:
            return None
        if not registration.source_stage_marks:
            return None
        origin = registration.source_stage_marks[0]
        return (
            float(fluidnc_xy[0]) - float(origin[0]),
            float(fluidnc_xy[1]) - float(origin[1]),
        )

    def _resolve_design_coordinates(
        self, fluidnc_xy: tuple[float, float]
    ) -> tuple[float, float] | None:
        try:
            return self._design_session.design_from_stage(fluidnc_xy)
        except Exception:
            return None

    @classmethod
    def _calculate_alignment_rotation(
        cls,
        first_position: tuple[float, float],
        second_position: tuple[float, float],
    ) -> float | None:
        dx = second_position[0] - first_position[0]
        dy = second_position[1] - first_position[1]
        if math.hypot(dx, dy) <= 1e-6:
            return None
        angle_deg = math.degrees(math.atan2(dy, dx))
        best_delta = min(
            (
                cls._normalise_angle(target - angle_deg)
                for target in cls.ALIGNMENT_TARGET_ANGLES
            ),
            key=lambda value: abs(value),
        )
        if abs(best_delta) > 45.0:
            return None
        return best_delta

    @staticmethod
    def _normalise_angle(angle_deg: float) -> float:
        return ((angle_deg + 180.0) % 360.0) - 180.0

    def show_joystick_window(self) -> None:
        if not self.joystick_panel or not self.joystick_dock:
            return
        self.joystick_dock.setVisible(True)
        self.joystick_dock.raise_()
        if self.joystick_dock.isFloating():
            self.joystick_dock.activateWindow()
        else:
            self.joystick_panel.setFocus(Qt.ActiveWindowFocusReason)

    def show_serial_terminal_window(self) -> None:
        if not self.serial_terminal_panel or not self.serial_terminal_dock:
            return
        self.serial_terminal_dock.setVisible(True)
        self.serial_terminal_dock.raise_()
        if self.serial_terminal_dock.isFloating():
            self.serial_terminal_dock.activateWindow()
        else:
            self.serial_terminal_panel.setFocus(Qt.ActiveWindowFocusReason)

    def _on_manual_motion_axis(self, axis: str) -> None:
        if axis.upper() == "B":
            self._invalidate_design_registration(
                "Design registration cleared after manual B-axis motion."
            )

    def _on_manual_jog_command_changed(
        self, commanded_distances: object, feedrate: float
    ) -> None:
        if not isinstance(commanded_distances, tuple):
            return
        self._clear_planned_move_prediction(clear_wait_state=True)
        self._manual_jog_waiting_for_fresh_status = False
        self._manual_jog_settle_until = 0.0
        self._manual_jog_stop_status_timestamp = None
        if self.serial_terminal_panel is not None:
            self.serial_terminal_panel.set_live_poll_paused(True)
        axis_components: dict[str, float] = {
            axis: 0.0 for axis in self.STAGE_AXIS_NAMES
        }
        for item in commanded_distances:
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            axis = str(item[0]).upper()
            try:
                distance = float(item[1])
            except (TypeError, ValueError):
                continue
            if axis in axis_components:
                axis_components[axis] = distance
        path_length = math.sqrt(
            sum(component * component for component in axis_components.values())
        )
        if path_length <= 1e-9:
            logger.debug("MOTION PREDICTION stop_requested command=%s", commanded_distances)
            self._manual_jog_axis_velocities.clear()
            self._clear_manual_jog_stop_prediction()
            self._manual_jog_velocity_xy = None
            self._manual_jog_timer.stop()
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
            return
        speed_mm_per_s = max(0.0, float(feedrate)) / 60.0
        self._manual_jog_axis_velocities = {
            axis: speed_mm_per_s * distance / path_length
            for axis, distance in axis_components.items()
            if abs(distance) > 1e-9
        }
        self._manual_jog_velocity_xy = (
            self._manual_jog_axis_velocities.get("X", 0.0),
            self._manual_jog_axis_velocities.get("Y", 0.0),
        )
        self._clear_manual_jog_stop_prediction()
        stage_source = "tracked"
        if self._manual_jog_stage_position is None and self._manual_jog_stage_xy is None:
            latest = self.stage_controller.latest_stage_position()
            if latest is not None and len(latest) >= 2:
                stage_source = "latest_status"
            elif self._current_design_stage_xy is not None:
                stage_source = "current_design"
            else:
                stage_source = "unknown"
        self._manual_jog_stage_position = self._seed_motion_prediction_position()
        self._manual_jog_stage_xy = self._stage_xy_from_position(
            self._manual_jog_stage_position
        )
        if (
            self._manual_jog_stage_position is None
            and self._current_design_stage_xy is not None
        ):
            self._manual_jog_stage_position = self._position_with_stage_xy(
                self._current_design_stage_xy,
                base_position=None,
            )
            self._manual_jog_stage_xy = self._current_design_stage_xy
            stage_source = "current_design"
        now = time.monotonic()
        self._manual_jog_command_started_at = now
        self._manual_jog_last_timestamp = now
        self._manual_jog_last_prediction_log_at = 0.0
        logger.debug(
            "MOTION PREDICTION start stage=%s design=%s velocity=(%.4f, %.4f) feedrate=%.3f command=%s source=%s",
            self._format_optional_point(self._manual_jog_stage_xy),
            self._format_optional_point(
                self._design_session.design_from_stage(self._manual_jog_stage_xy)
                if self._manual_jog_stage_xy is not None
                else None
            ),
            self._manual_jog_velocity_xy[0],
            self._manual_jog_velocity_xy[1],
            float(feedrate),
            commanded_distances,
            stage_source,
        )
        if self._manual_jog_stage_position is not None:
            self._publish_stage_position_estimate(self._manual_jog_stage_position)
        if not self._manual_jog_timer.isActive():
            self._manual_jog_timer.start()

    def _on_manual_jog_stopped(self) -> None:
        now = time.monotonic()
        stop_velocities = dict(self._manual_jog_axis_velocities)
        logger.debug(
            "MOTION PREDICTION stop_requested stage=%s design=%s stop_tail_s=%.4f",
            self._format_optional_point(self._manual_jog_stage_xy),
            self._format_optional_point(
                self._design_session.design_from_stage(self._manual_jog_stage_xy)
                if self._manual_jog_stage_xy is not None
                else None
            ),
            self._manual_jog_stop_tail_s if stop_velocities else 0.0,
        )
        if stop_velocities and self._manual_jog_stage_position is not None:
            self._manual_jog_stop_axis_velocities = stop_velocities
            self._manual_jog_stop_prediction_until = now + self._manual_jog_stop_tail_s
            self._manual_jog_stop_tail_position = None
            if self._manual_jog_last_timestamp is None:
                self._manual_jog_last_timestamp = now
            if not self._manual_jog_timer.isActive():
                self._manual_jog_timer.start()
        else:
            self._clear_manual_jog_stop_prediction()
            self._manual_jog_timer.stop()
        self._manual_jog_axis_velocities.clear()
        self._manual_jog_velocity_xy = None
        self._manual_jog_last_prediction_log_at = 0.0
        self._manual_jog_waiting_for_fresh_status = self._manual_jog_stage_xy is not None
        self._manual_jog_settle_until = (
            now + self.MANUAL_JOG_STATUS_SETTLE_HOLD_S
        )
        self._manual_jog_stop_status_timestamp = self.stage_controller.last_status_timestamp()
        if self.serial_terminal_panel is not None:
            QTimer.singleShot(
                self.TERMINAL_RESUME_AFTER_JOG_MS,
                lambda: self.serial_terminal_panel
                and self.serial_terminal_panel.set_live_poll_paused(False),
            )
        self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)

    def _save_manual_axis_jog_settings(
        self, axis: str, distance_mm: float, mode: str, feedrate_mm_min: float
    ) -> None:
        axis = axis.strip().upper()
        if axis not in {"X", "Y", "Z", "A", "B", "C"}:
            return
        mode = mode.strip().upper()
        if mode not in {"G90", "G91"}:
            return
        distance = max(0.001, float(distance_mm))
        feedrate = max(0.1, float(feedrate_mm_min))
        settings = self.settings_manager.settings.clone()
        if (
            settings.jog.manual_axis == axis
            and abs(settings.jog.manual_axis_distance_mm - distance) <= 1e-9
            and settings.jog.manual_axis_mode == mode
            and abs(settings.jog.manual_axis_feedrate_mm_min - feedrate) <= 1e-9
        ):
            return
        settings.jog.manual_axis = axis
        settings.jog.manual_axis_distance_mm = distance
        settings.jog.manual_axis_mode = mode
        settings.jog.manual_axis_feedrate_mm_min = feedrate
        self.settings_manager.replace(settings)
        self.settings_manager.save()

    def _advance_motion_prediction(self) -> None:
        if self._manual_jog_prediction_active():
            self._advance_manual_jog_prediction()
            return
        if self._planned_move_started_at is not None:
            self._advance_planned_move_prediction()
            return
        self._manual_jog_timer.stop()

    def _advance_manual_jog_prediction(self) -> None:
        velocities = self._manual_jog_prediction_velocities()
        if not velocities:
            return
        now = time.monotonic()
        if self._manual_jog_last_timestamp is None:
            self._manual_jog_last_timestamp = now
            return
        prediction_now = now
        if (
            not self._manual_jog_axis_velocities
            and self._manual_jog_stop_prediction_until is not None
        ):
            prediction_now = min(now, self._manual_jog_stop_prediction_until)
        dt = max(0.0, prediction_now - self._manual_jog_last_timestamp)
        self._manual_jog_last_timestamp = prediction_now
        if dt <= 0.0:
            if (
                self._manual_jog_stop_prediction_until is not None
                and now >= self._manual_jog_stop_prediction_until
            ):
                self._manual_jog_stop_tail_position = self._manual_jog_stage_position
                self._manual_jog_timer.stop()
            return
        if self._manual_jog_stage_position is None:
            self._manual_jog_stage_position = self._seed_motion_prediction_position()
            if self._manual_jog_stage_position is None:
                return
        values = [float(value) for value in self._manual_jog_stage_position]
        for axis, velocity in velocities.items():
            try:
                axis_index = self.STAGE_AXIS_NAMES.index(axis)
            except ValueError:
                continue
            if axis_index >= len(values):
                continue
            values[axis_index] = float(values[axis_index] + velocity * dt)
        self._manual_jog_stage_position = tuple(values)
        self._manual_jog_stage_xy = self._stage_xy_from_position(
            self._manual_jog_stage_position
        )
        if now - self._manual_jog_last_prediction_log_at >= 0.15:
            velocity_x = velocities.get("X", 0.0)
            velocity_y = velocities.get("Y", 0.0)
            logger.debug(
                "MOTION PREDICTION tick stage=%s design=%s dt=%.4f velocity=(%.4f, %.4f)",
                self._format_optional_point(self._manual_jog_stage_xy),
                self._format_optional_point(
                    self._design_session.design_from_stage(self._manual_jog_stage_xy)
                ),
                dt,
                velocity_x,
                velocity_y,
            )
            self._manual_jog_last_prediction_log_at = now
        self._publish_stage_position_estimate(self._manual_jog_stage_position)
        if (
            self._manual_jog_stop_prediction_until is not None
            and now >= self._manual_jog_stop_prediction_until
            and not self._manual_jog_axis_velocities
        ):
            self._manual_jog_stop_tail_position = self._manual_jog_stage_position
            self._manual_jog_timer.stop()

    def _advance_planned_move_prediction(self) -> None:
        if (
            self._planned_move_origin_xy is None
            or self._planned_move_target_xy is None
            or self._planned_move_started_at is None
            or self._planned_move_ends_at is None
        ):
            self._clear_planned_move_prediction(clear_wait_state=False)
            return
        now = time.monotonic()
        duration = max(
            self._planned_move_ends_at - self._planned_move_started_at,
            1e-6,
        )
        progress = min(
            1.0,
            max(0.0, (now - self._planned_move_started_at) / duration),
        )
        origin_x, origin_y = self._planned_move_origin_xy
        target_x, target_y = self._planned_move_target_xy
        self._planned_move_stage_xy = (
            float(origin_x + (target_x - origin_x) * progress),
            float(origin_y + (target_y - origin_y) * progress),
        )
        self._publish_stage_position_estimate(
            self._position_with_stage_xy(self._planned_move_stage_xy)
        )
        if progress >= 1.0:
            self._planned_move_waiting_for_fresh_status = (
                self._planned_move_stage_xy is not None
            )
            self._planned_move_stop_status_timestamp = (
                self.stage_controller.last_status_timestamp()
            )
            self._planned_move_origin_xy = None
            self._planned_move_target_xy = None
            self._planned_move_started_at = None
            self._planned_move_ends_at = None

    def _clear_planned_move_prediction(self, *, clear_wait_state: bool) -> None:
        self._planned_move_origin_xy = None
        self._planned_move_target_xy = None
        self._planned_move_started_at = None
        self._planned_move_ends_at = None
        if clear_wait_state:
            self._planned_move_stage_xy = None
            self._planned_move_waiting_for_fresh_status = False
            self._planned_move_stop_status_timestamp = None

    def _start_planned_move_prediction(
        self,
        target_stage_xy: tuple[float, float],
        *,
        source_label: str,
    ) -> None:
        origin_stage_xy = self._preferred_design_stage_xy()
        if origin_stage_xy is None:
            origin_stage_xy = self._current_design_stage_xy
        if origin_stage_xy is None:
            latest = self.stage_controller.latest_stage_position()
            if latest is not None and len(latest) >= 2:
                origin_stage_xy = (float(latest[0]), float(latest[1]))
        if origin_stage_xy is None:
            return
        distance_mm = math.hypot(
            float(target_stage_xy[0] - origin_stage_xy[0]),
            float(target_stage_xy[1] - origin_stage_xy[1]),
        )
        if distance_mm <= 1e-6:
            self._clear_planned_move_prediction(clear_wait_state=True)
            return
        speed_mm_per_s = float(self.stage_controller.DEFAULT_FEEDRATE) / 60.0
        if speed_mm_per_s <= 1e-6:
            return
        duration_s = (
            distance_mm / speed_mm_per_s
        ) + self.PLANNED_MOVE_DURATION_PADDING_S
        started_at = time.monotonic()
        self._manual_jog_waiting_for_fresh_status = False
        self._manual_jog_settle_until = 0.0
        self._manual_jog_stop_status_timestamp = None
        self._planned_move_origin_xy = origin_stage_xy
        self._planned_move_stage_xy = origin_stage_xy
        self._planned_move_target_xy = target_stage_xy
        self._planned_move_started_at = started_at
        self._planned_move_ends_at = started_at + max(duration_s, 0.05)
        self._planned_move_waiting_for_fresh_status = False
        self._planned_move_stop_status_timestamp = None
        logger.debug(
            "MOTION PREDICTION planned_move_start source=%s origin=%s target=%s distance=%.4f duration=%.4f feedrate=%.3f",
            source_label,
            self._format_optional_point(origin_stage_xy),
            self._format_optional_point(target_stage_xy),
            distance_mm,
            duration_s,
            float(self.stage_controller.DEFAULT_FEEDRATE),
        )
        self._publish_stage_position_estimate(
            self._position_with_stage_xy(origin_stage_xy)
        )
        if not self._manual_jog_timer.isActive():
            self._manual_jog_timer.start()

    def _schedule_status_refreshes(self, delays_ms: tuple[int, ...]) -> None:
        for delay_ms in delays_ms:
            QTimer.singleShot(delay_ms, self.stage_controller.request_status_refresh)

    def _on_manual_terminal_command(self, command: str) -> None:
        stripped = command.strip().upper()
        if not stripped:
            return
        if re.match(r"^G5(?:4|5|6|7|8|9(?:\.[123])?)$", stripped):
            self.stage_controller.request_startup_sync(auto_home_a=False)
            return
        if (
            stripped.startswith("$#")
            or stripped.startswith("$G")
            or stripped.startswith("$10")
            or stripped.startswith("G10")
        ):
            self.stage_controller.request_startup_sync(auto_home_a=False)

    def on_move_finished(self, success: bool, message: str) -> None:
        if (
            self._planned_move_started_at is not None
            or self._planned_move_waiting_for_fresh_status
        ):
            logger.debug(
                "MOTION PREDICTION planned_move_finish success=%s stage=%s",
                success,
                self._format_optional_point(self._planned_move_stage_xy),
            )
            if success:
                if self._planned_move_target_xy is not None:
                    self._planned_move_stage_xy = self._planned_move_target_xy
                self._planned_move_origin_xy = None
                self._planned_move_target_xy = None
                self._planned_move_started_at = None
                self._planned_move_ends_at = None
                self._planned_move_waiting_for_fresh_status = (
                    self._planned_move_stage_xy is not None
                )
                self._planned_move_stop_status_timestamp = (
                    self.stage_controller.last_status_timestamp()
                )
            else:
                self._clear_planned_move_prediction(clear_wait_state=True)
        if self._pending_alignment_preparation is not None:
            preparation = self._pending_alignment_preparation
            self._pending_alignment_preparation = None
            if success:
                self._design_session.apply_prepared_alignment(preparation)
                self._set_design_snap_enabled(False)
                self._refresh_design_panel()
                self._refresh_design_position()
                self._collapse_alignment_panel_if_ready()
                self.view.clear_target_cross()
                self._show_status(
                    "Design calibration complete. "
                    f"Rotation {preparation.rotation_deg:+.3f} deg, "
                    f"spacing ratio {preparation.distance_ratio:.3f}.",
                    7000,
                )
            else:
                self._show_status(
                    f"Design calibration rotation failed: {message}",
                    7000,
                )
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
            return
        if self._pending_quick_alignment_rotation:
            self._pending_quick_alignment_rotation = False
            if success:
                self._collapse_alignment_panel_if_design_open()
        if success:
            self.view.clear_target_cross()
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
        if message:
            self._show_status(message, 5000)

    def on_autofocus_finished(self, success: bool, message: str) -> None:
        if message:
            self._show_status(message, 5000)
        if not success:
            logger.error("Autofocus failed: %s", message)

    def on_calibration_changed(self, mm_per_pixel_x: float, mm_per_pixel_y: float) -> None:
        self._show_status(
            f"Calibration: ΔX {mm_per_pixel_x:.6f} mm/px, ΔY {mm_per_pixel_y:.6f} mm/px",
            5000,
        )
        self.view.set_scale(mm_per_pixel_x, mm_per_pixel_y)

    def _on_measure_action_toggled(self, checked: bool) -> None:
        """Handle ruler/rect toggle — keep the two actions mutually exclusive."""
        sender = self.sender()
        if not checked:
            # Only exit if no other measure action is checked
            if (
                (self._ruler_action is None or not self._ruler_action.isChecked())
                and (self._rect_action is None or not self._rect_action.isChecked())
            ):
                self.view.set_measure_mode(None)
            return
        # Uncheck the other action without triggering this handler recursively
        if sender is self._ruler_action and self._rect_action is not None:
            self._rect_action.blockSignals(True)
            self._rect_action.setChecked(False)
            self._rect_action.blockSignals(False)
        elif sender is self._rect_action and self._ruler_action is not None:
            self._ruler_action.blockSignals(True)
            self._ruler_action.setChecked(False)
            self._ruler_action.blockSignals(False)
        mode = "ruler" if sender is self._ruler_action else "rect"
        self.view.set_measure_mode(mode)

    def _on_measure_mode_exited(self) -> None:
        """Exit measure mode (called on Esc or from the view's own signal)."""
        self.view.set_measure_mode(None)
        for action in (self._ruler_action, self._rect_action):
            if action is not None:
                action.blockSignals(True)
                action.setChecked(False)
                action.blockSignals(False)

    def _load_design_document(self, design_path: str) -> None:
        self._design_load_generation += 1
        generation = self._design_load_generation
        path_text = str(design_path)
        self._toggle_design_layout_window(True)
        if self.design_layout_window is not None:
            self.design_layout_window.set_status_message("Loading design...")
        elif self.design_navigator_panel:
            self.design_navigator_panel.set_status_message("Loading design...")
        self._show_status(f"Loading design '{Path(path_text).name}'...")

        def load_design() -> None:
            try:
                document = DesignDocument.load(path_text)
            except Exception as exc:
                self.design_document_loaded.emit(generation, None, exc)
                return
            self.design_document_loaded.emit(generation, document, None)

        threading.Thread(
            target=load_design,
            name="DesignDocumentLoad",
            daemon=True,
        ).start()

    def _on_design_document_loaded(
        self,
        generation: int,
        document: object,
        error: object,
    ) -> None:
        if generation != self._design_load_generation:
            return
        if error is not None:
            message = str(error)
            self._show_status(message, 6000)
            if self.design_layout_window is not None:
                self.design_layout_window.set_status_message(message)
            elif self.design_navigator_panel:
                self.design_navigator_panel.set_status_message(message)
            return
        if not isinstance(document, DesignDocument):
            message = "Loaded design has an unexpected type."
            self._show_status(message, 6000)
            if self.design_layout_window is not None:
                self.design_layout_window.set_status_message(message)
            elif self.design_navigator_panel:
                self.design_navigator_panel.set_status_message(message)
            return
        try:
            self._reset_manual_alignment(cancel_pick=True)
            self._design_session.load_document(document)
            self._pending_alignment_preparation = None
            self._last_selected_design_point = None
            self._set_design_snap_enabled(True)
            self.settings_manager.set_design_last_directory(document.path.parent)
            if self.design_navigator_panel is not None:
                self.design_navigator_panel.set_design_dialog_directory(
                    document.path.parent
                )
            if self.design_layout_window is not None:
                self.design_layout_window.set_status_message("Rendering design...")
                QApplication.processEvents()
            self._refresh_design_panel()
            self._refresh_design_position()
            self._toggle_design_layout_window(True)
            self._show_status(
                f"Loaded design '{document.path.name}' ({document.top_cell_name}).",
                5000,
            )
        except DesignModelError as exc:
            self._show_status(str(exc), 6000)

    def _unload_design_document(self) -> None:
        self._design_load_generation += 1
        if self._design_session.document is None:
            return
        document_name = self._design_session.document.path.name
        self._reset_manual_alignment(cancel_pick=True)
        self._design_session.unload_document()
        self._pending_alignment_preparation = None
        self._last_selected_design_point = None
        self._refresh_design_panel()
        self._update_design_position(None)
        self._show_status(f"Unloaded design '{document_name}'.", 5000)

    def _set_design_top_cell(self, top_cell_name: str) -> None:
        try:
            self._design_session.set_top_cell(top_cell_name)
        except DesignModelError as exc:
            self._show_status(str(exc), 6000)
            return
        self._pending_alignment_preparation = None
        self._last_selected_design_point = None
        self._refresh_design_panel()
        self._refresh_design_position()
        self._show_status(f"Switched design top cell to '{top_cell_name}'.", 5000)

    def _set_design_layer_visibility(
        self, layer: int, datatype: int, visible: bool
    ) -> None:
        document = self._design_session.document
        if document is None:
            return
        visible_layers = set(document.visible_layers)
        layer_key = (int(layer), int(datatype))
        if visible:
            visible_layers.add(layer_key)
        else:
            visible_layers.discard(layer_key)
        try:
            self._design_session.set_visible_layers(visible_layers)
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._refresh_design_panel()

    def _load_measurement_script(self, script_path: str) -> None:
        document = self._design_session.document
        if document is None:
            self._show_status("Load a design before loading a measurement plan.", 5000)
            return
        try:
            module, targets = load_measurement_plan(script_path, ScriptContext(document))
        except DesignModelError as exc:
            self._show_status(str(exc), 7000)
            return
        self._design_session.script_path = script_path
        self._design_session.script_module_name = module.__name__
        self._design_session.set_targets(targets)
        self._refresh_design_panel()
        self._show_status(
            f"Loaded measurement plan '{Path(script_path).name}' with {len(targets)} targets.",
            5000,
        )

    def _reload_measurement_script(self) -> None:
        script_path = self._design_session.script_path
        if not script_path:
            self._show_status("No measurement script is loaded.", 5000)
            return
        self._load_measurement_script(script_path)

    def _add_design_source_mark(self, x_value: float, y_value: float) -> None:
        self._design_session.add_source_design_mark((x_value, y_value))
        self._refresh_design_panel()
        self._show_status(
            f"Design source mark captured at X={x_value:.3f}, Y={y_value:.3f}.",
            4000,
        )

    def _add_design_check_mark(self, x_value: float, y_value: float) -> None:
        self._design_session.add_check_design_mark((x_value, y_value))
        self._refresh_design_panel()
        self._show_status(
            f"Design check mark captured at X={x_value:.3f}, Y={y_value:.3f}.",
            4000,
        )

    def _capture_stage_source_mark(self) -> None:
        self._capture_stage_registration_mark(check_mark=False)

    def _capture_stage_check_mark(self) -> None:
        self._capture_stage_registration_mark(check_mark=True)

    def _capture_stage_registration_mark(self, *, check_mark: bool) -> None:
        try:
            stage_position = self.stage_controller.current_stage_position()
        except Exception as exc:
            self._show_status(str(exc), 6000)
            return
        if len(stage_position) < 2:
            self._show_status("X/Y coordinates are unavailable.", 5000)
            return
        stage_xy = (float(stage_position[0]), float(stage_position[1]))
        if check_mark:
            self._design_session.add_check_stage_mark(stage_xy)
            label = "check"
        else:
            self._design_session.add_source_stage_mark(stage_xy)
            label = "source"
        self._refresh_design_panel()
        self._refresh_design_position()
        self._show_status(
            f"Stage {label} mark captured at X={stage_xy[0]:.3f}, Y={stage_xy[1]:.3f}.",
            4000,
        )

    def _design_spacing_ratio_is_reasonable(self, ratio: float) -> bool:
        return abs(float(ratio) - 1.0) <= self.DESIGN_SPACING_RATIO_TOLERANCE

    def _clear_design_registration(self) -> None:
        self._pending_alignment_preparation = None
        self._last_selected_design_point = None
        self._design_session.clear_registration()
        self._set_design_snap_enabled(True)
        self._refresh_design_panel()
        self._refresh_design_position()
        self._show_status("Design calibration restarted.", 4000)

    def _invalidate_design_registration(self, reason: str) -> None:
        self._pending_alignment_preparation = None
        self._design_session.invalidate_registration(reason)
        if self._design_session.document is not None:
            self._set_design_snap_enabled(True)
        self._refresh_design_panel()
        latest = self.stage_controller.latest_stage_position()
        if latest is not None and len(latest) >= 2:
            self._update_design_position((float(latest[0]), float(latest[1])))
        else:
            self._update_design_position(None)

    def _on_design_target_selected(self, target_id: str) -> None:
        self._design_session.select_target_by_id(target_id)
        self._refresh_design_panel()

    def _select_next_design_target(self) -> None:
        target = self._design_session.select_next_target()
        self._refresh_design_panel()
        if target is not None:
            self._show_status(f"Selected target '{target.label}'.", 3000)

    def _select_previous_design_target(self) -> None:
        target = self._design_session.select_previous_target()
        self._refresh_design_panel()
        if target is not None:
            self._show_status(f"Selected target '{target.label}'.", 3000)

    def _move_to_design_target(self, target_id: str) -> None:
        target = self._design_session.select_target_by_id(target_id)
        if target is None:
            self._show_status(f"Unknown target '{target_id}'.", 5000)
            return
        stage_xy = self._design_session.selected_target_stage_xy()
        if stage_xy is None:
            self._show_status(
                "Design registration is required before moving to a target.",
                6000,
            )
            return
        self._refresh_design_panel()
        self.stage_controller.request_move_to_xy(stage_xy[0], stage_xy[1])

    def _move_to_minimap_design_point(self, x_value: float, y_value: float) -> None:
        design_xy = (float(x_value), float(y_value))
        if not self._move_to_design_coordinate(design_xy, source_label="minimap point"):
            return
        self._show_status(
            f"Moving to minimap point X={design_xy[0]:.3f}, Y={design_xy[1]:.3f}.",
            3000,
        )

    def _move_to_design_coordinate(
        self, design_xy: tuple[float, float], *, source_label: str
    ) -> bool:
        document = self._design_session.document
        if document is None:
            return False
        if self.stage_controller.is_busy():
            self._show_status("Stage is busy. Ignoring design move request.", 3000)
            return False
        stage_xy = self._design_session.stage_from_design(design_xy)
        if stage_xy is None:
            self._show_status(
                "Design click-to-move requires completed registration.",
                5000,
            )
            return False
        self._last_selected_design_point = design_xy
        self._refresh_design_panel()
        self._start_planned_move_prediction(
            (float(stage_xy[0]), float(stage_xy[1])),
            source_label=source_label,
        )
        self.stage_controller.request_move_to_xy(stage_xy[0], stage_xy[1])
        logger.debug(
            "DESIGN MOVE source=%s design=(%.3f, %.3f) stage=(%.3f, %.3f)",
            source_label,
            design_xy[0],
            design_xy[1],
            stage_xy[0],
            stage_xy[1],
        )
        return True

    def _refresh_design_panel(self) -> None:
        panel = self.design_navigator_panel
        current_target = self._design_session.current_target()
        selected_target_id = current_target.id if current_target else None
        if panel is not None:
            panel.set_document(self._design_session.document)
            panel.set_script_path(self._design_session.script_path)
            panel.set_targets(
                self._design_session.targets,
                selected_target_id=selected_target_id,
            )
            if self._pending_alignment_preparation is not None:
                panel.set_calibration_prompt(
                    "Calibration step 4/4: chip rotation is in progress."
                )
            else:
                panel.set_calibration_prompt(self._design_session.calibration_prompt())
            panel.set_registration_status(self._design_session.registration_status)
            panel.set_registration_marks(
                self._design_session.source_design_marks,
                self._design_session.check_design_marks,
            )
            panel.set_stage_registration_marks(self._design_session.source_stage_marks)
        if self.design_layout_window is not None:
            self.design_layout_window.set_snap_enabled(self._design_snap_enabled)
            self.design_layout_window.set_document(self._design_session.document)
            self.design_layout_window.set_targets(
                self._design_session.targets,
                selected_target_id=selected_target_id,
            )
            self.design_layout_window.set_navigation_enabled(
                self._design_session.registration is not None
                and self._design_session.registration.valid
            )
            self.design_layout_window.set_registration_marks(
                self._design_session.source_design_marks,
                self._design_session.check_design_marks,
            )
            self.design_layout_window.set_stage_registration_marks(
                self._design_session.source_stage_marks
            )
        self._refresh_manual_alignment_ui()
        self._update_design_position(self._current_design_stage_xy)

    def _on_stage_position_changed(self, position: object) -> None:
        if not isinstance(position, tuple) or len(position) < 2:
            self._update_stage_position_display(position)
            return
        logger.debug("TIMING stage_position_changed position=%s", position)
        xy_homed = self.stage_controller.axes_are_homed({"X", "Y"})
        xyz_homed = self.stage_controller.axes_are_homed({"X", "Y", "Z"})
        if self.contact_calibration_window is not None:
            if len(position) >= 3 and xyz_homed:
                self.contact_calibration_window.set_current_stage_position(
                    (float(position[0]), float(position[1]), float(position[2]))
                )
            else:
                self.contact_calibration_window.set_current_stage_position(None)
        if not xy_homed:
            if not self._manual_jog_prediction_available():
                self._update_stage_position_display(position)
                self._manual_jog_stage_position = None
                self._manual_jog_stage_xy = None
                self._planned_move_stage_xy = None
                self._update_coordinate_display(center_xy=None)
                self._update_design_position(None)
                return
        if len(position) > 4:
            current_b = float(position[4])
            if (
                self._last_reported_b_position is not None
                and self._pending_alignment_preparation is None
                and abs(current_b - self._last_reported_b_position)
                > self.B_POSITION_CHANGE_TOLERANCE_DEG
                and self._design_session.registration is not None
                and self._design_session.registration.valid
            ):
                self._invalidate_design_registration(
                    "Design registration cleared after B-axis motion."
                )
            self._last_reported_b_position = current_b
        predicted_position = None
        if self._manual_jog_prediction_available():
            predicted_position = self._manual_jog_stage_position
        elif (
            self._planned_move_started_at is not None
            or self._planned_move_waiting_for_fresh_status
        ):
            if self._planned_move_stage_xy is not None:
                predicted_position = self._position_with_stage_xy(
                    self._planned_move_stage_xy,
                    base_position=position,
                )
        predicted_stage_xy = self._stage_xy_from_position(predicted_position)
        center_xy = (float(position[0]), float(position[1]))
        latest_state = (self.stage_controller.latest_stage_state() or "").lower()
        if self._manual_jog_waiting_for_fresh_status and latest_state != "idle":
            logger.debug(
                "MOTION PREDICTION deferred_stop_sample stage=%s state=%s",
                self._format_optional_point(center_xy),
                latest_state,
            )
            return
        if self._should_ignore_manual_jog_status_sample(center_xy):
            return
        if predicted_stage_xy is not None:
            self._log_design_position_reconcile(predicted_stage_xy, center_xy)
            center_xy = self._smooth_manual_jog_actual_position(predicted_stage_xy, center_xy)
        display_position = self._position_with_stage_xy(
            center_xy,
            base_position=position,
        )
        self._manual_jog_stage_position = display_position
        self._manual_jog_stage_xy = center_xy
        self._planned_move_stage_xy = center_xy
        if self._manual_jog_waiting_for_fresh_status and latest_state == "idle":
            self._learn_manual_jog_stop_tail(
                self._manual_jog_stop_tail_position or predicted_position,
                display_position,
            )
            self._manual_jog_waiting_for_fresh_status = False
            self._manual_jog_settle_until = 0.0
            self._manual_jog_stop_status_timestamp = None
            self._clear_manual_jog_stop_prediction()
        if self._planned_move_waiting_for_fresh_status:
            self._planned_move_waiting_for_fresh_status = False
            self._planned_move_stop_status_timestamp = None
        if self._manual_jog_prediction_active():
            self._manual_jog_last_timestamp = time.monotonic()
        self._publish_stage_position_estimate(display_position)

    def _update_stage_position_display(self, position: object | None) -> None:
        if not isinstance(position, tuple) or len(position) < 2:
            self._stage_unhomed_display_origins.clear()
            self._stage_position_label.setText("Position: unavailable")
            return
        homed_axes = self.stage_controller.homed_axes()
        parts: list[str] = []
        for axis_name, axis_value in zip(self.STAGE_AXIS_NAMES, position):
            try:
                raw_value = float(axis_value)
            except (TypeError, ValueError):
                continue
            if axis_name in homed_axes:
                display_value = raw_value
                background = "#1565c0"
                foreground = "#f5f5f5"
            else:
                origin = self._stage_unhomed_display_origins.setdefault(
                    axis_name, raw_value
                )
                display_value = raw_value - origin
                background = "#f0b429"
                foreground = "#1f1f1f"
            parts.append(
                "<span style="
                f"'background-color:{background}; color:{foreground}; "
                "padding:2px 6px;'"
                f">&nbsp;{axis_name}={display_value:.3f}&nbsp;</span>"
            )
        if not parts:
            self._stage_position_label.setText("Position: unavailable")
            return
        self._stage_position_label.setText("Position: " + "&nbsp;".join(parts))

    def _on_homing_status_changed(self, _homed_axes: object) -> None:
        if (
            self._manual_jog_prediction_available()
            and self._manual_jog_stage_position is not None
        ):
            self._update_stage_position_display(self._manual_jog_stage_position)
            return
        if (
            self._planned_move_stage_xy is not None
            and (
                self._planned_move_started_at is not None
                or self._planned_move_waiting_for_fresh_status
            )
        ):
            self._update_stage_position_display(
                self._position_with_stage_xy(self._planned_move_stage_xy)
            )
            return
        self._update_stage_position_display(self.stage_controller.latest_stage_position())

    def _refresh_controller_status(self) -> None:
        if self.serial_connection is None or not self.serial_connection.is_open:
            return
        self.stage_controller.request_status_refresh()

    def _refresh_design_position(self) -> None:
        if self._design_session.document is None:
            return
        if self.serial_connection is None or not self.serial_connection.is_open:
            self._update_design_position(None)
            return
        preferred_stage_xy = self._preferred_design_stage_xy()
        if preferred_stage_xy is None:
            self.stage_controller.request_status_refresh()
            return
        self._pending_design_stage_xy = preferred_stage_xy
        self._flush_pending_design_position()

    def _flush_pending_design_position(self) -> None:
        stage_xy = self._pending_design_stage_xy
        self._pending_design_stage_xy = None
        self._update_design_position(stage_xy)

    def _update_design_position(self, stage_xy: tuple[float, float] | None) -> None:
        self._current_design_stage_xy = stage_xy
        design_xy = None
        fov_design_size = None
        if stage_xy is not None:
            design_xy = self._design_session.design_from_stage(stage_xy)
            fov_design_size = self._resolve_design_fov_size()
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_current_position(
                stage_xy,
                design_xy,
                fov_design_size=fov_design_size,
            )
        if self.design_layout_window is not None:
            self.design_layout_window.set_current_design_position(
                design_xy,
                fov_design_size=fov_design_size,
            )
        current_target = self._design_session.current_target()
        self.view.set_design_minimap_data(
            document=self._design_session.document,
            targets=self._design_session.targets,
            selected_target_id=current_target.id if current_target else None,
            selected_design_point=self._last_selected_design_point,
            current_design_position=design_xy,
            fov_design_size=fov_design_size,
            source_design_marks=self._design_session.source_design_marks_compact(),
            check_design_marks=self._design_session.check_design_marks,
        )

    def _log_design_position_reconcile(
        self,
        predicted_stage_xy: tuple[float, float],
        actual_stage_xy: tuple[float, float],
    ) -> None:
        state = self.stage_controller.latest_stage_state()
        predicted_design_xy = self._design_session.design_from_stage(predicted_stage_xy)
        actual_design_xy = self._design_session.design_from_stage(actual_stage_xy)
        delta_x = float(actual_stage_xy[0] - predicted_stage_xy[0])
        delta_y = float(actual_stage_xy[1] - predicted_stage_xy[1])
        logger.debug(
            "MOTION PREDICTION reconcile predicted_stage=%s actual_stage=%s delta=(%.4f, %.4f) delta_norm=%.4f state=%s predicted_design=%s actual_design=%s",
            self._format_optional_point(predicted_stage_xy),
            self._format_optional_point(actual_stage_xy),
            delta_x,
            delta_y,
            math.hypot(delta_x, delta_y),
            state,
            self._format_optional_point(predicted_design_xy),
            self._format_optional_point(actual_design_xy),
        )

    def _should_ignore_manual_jog_status_sample(
        self,
        actual_stage_xy: tuple[float, float],
    ) -> bool:
        if not self._manual_jog_prediction_active():
            return False
        if self._manual_jog_waiting_for_fresh_status:
            return False
        state = (self.stage_controller.latest_stage_state() or "").lower()
        if state != "idle":
            return False
        last_jog_write = self.stage_controller.last_jog_write_timestamp()
        command_started_at = self._manual_jog_command_started_at
        timestamps = [
            timestamp
            for timestamp in (last_jog_write, command_started_at)
            if timestamp is not None
        ]
        if not timestamps:
            return False
        age = time.monotonic() - max(timestamps)
        if age > self.MANUAL_JOG_IGNORE_IDLE_AFTER_COMMAND_S:
            return False
        logger.debug(
            "MOTION PREDICTION ignored_idle_sample stage=%s age=%.3f state=%s",
            self._format_optional_point(actual_stage_xy),
            age,
            state,
        )
        return True

    def _smooth_manual_jog_actual_position(
        self,
        predicted_stage_xy: tuple[float, float],
        actual_stage_xy: tuple[float, float],
    ) -> tuple[float, float]:
        delta_x = float(actual_stage_xy[0] - predicted_stage_xy[0])
        delta_y = float(actual_stage_xy[1] - predicted_stage_xy[1])
        delta_norm = math.hypot(delta_x, delta_y)
        state = (self.stage_controller.latest_stage_state() or "").lower()
        if (
            delta_norm <= self.MANUAL_JOG_RECONCILE_SMOOTH_THRESHOLD_MM
            or state not in {"jog", "run"}
        ):
            return actual_stage_xy
        alpha = self.MANUAL_JOG_RECONCILE_SMOOTH_ALPHA
        smoothed = (
            float(predicted_stage_xy[0] + delta_x * alpha),
            float(predicted_stage_xy[1] + delta_y * alpha),
        )
        logger.debug(
            "MOTION PREDICTION reconcile_smoothed predicted_stage=%s actual_stage=%s smoothed_stage=%s delta_norm=%.4f alpha=%.2f",
            self._format_optional_point(predicted_stage_xy),
            self._format_optional_point(actual_stage_xy),
            self._format_optional_point(smoothed),
            delta_norm,
            alpha,
        )
        return smoothed

    @staticmethod
    def _format_optional_point(point: tuple[float, float] | None) -> str:
        if point is None:
            return "None"
        return f"({float(point[0]):.4f}, {float(point[1]):.4f})"

    def _resolve_design_fov_size(self) -> tuple[float, float] | None:
        import numpy as np

        registration = self._design_session.registration
        if registration is None or not registration.valid:
            return None
        stage_fov = self.stage_controller.current_fov_size_mm()
        if stage_fov is None:
            return None
        try:
            inverse = np.linalg.inv(registration.matrix)
        except np.linalg.LinAlgError:
            return None
        width_vec = inverse @ np.asarray([float(stage_fov[0]), 0.0], dtype=float)
        height_vec = inverse @ np.asarray([0.0, float(stage_fov[1])], dtype=float)
        return (float(np.linalg.norm(width_vec)), float(np.linalg.norm(height_vec)))

    def _on_homing_action_finished(
        self, success: bool, _message: str, axis_key: str
    ) -> None:
        if not success:
            return
        if axis_key.upper() in {"X", "Y", "B", "ALL"}:
            self._invalidate_design_registration(
                f"Design registration cleared after homing {axis_key.upper()}."
            )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._design_position_timer.stop()
        self._manual_jog_timer.stop()
        self._stop_jog_before_serial_close("application shutdown")
        self.grabber.stop()
        self.thread.quit()
        self.thread.wait()
        if self.serial_connection and self.serial_connection.is_open:
            self.serial_connection.close()
        if self.joystick_panel:
            self.joystick_panel.set_serial(None)
        if self.serial_terminal_panel:
            self.serial_terminal_panel.set_serial(None)
        self.stage_controller.request_stop_oscillation()
        self.stage_controller.shutdown()
        self.lcr_controller.shutdown()
        if self.design_layout_window is not None:
            self.design_layout_window.close()
        if self.contact_calibration_window is not None:
            self.contact_calibration_window.close()
        if self.serial_connection_panel:
            self.serial_connection_panel.shutdown()
        event.accept()

    def _stop_jog_before_serial_close(self, reason: str) -> None:
        if self.serial_connection is None or not self.serial_connection.is_open:
            return
        logger.warning("Stopping active jog before %s.", reason)
        if self.joystick_panel is not None:
            self.joystick_panel.stop_jog()
        try:
            self.stage_controller.force_jog_stop(timeout=0.8)
        except Exception:
            logger.exception("Failed to force jog stop before %s.", reason)

    def _create_dock_widgets(self) -> None:
        self.serial_connection_panel = SerialConnectionPanel(self)
        self.serial_connection_panel.connected.connect(self.on_serial_connected)
        self.serial_connection_panel.disconnected.connect(self.on_serial_disconnected)
        self.serial_connection_dock = CollapsibleDockWidget("Connection", self)
        self.serial_connection_dock.setObjectName("SerialConnectionDock")
        self.serial_connection_dock.setWidget(self.serial_connection_panel)
        self.serial_connection_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self.serial_connection_dock)

        self.joystick_panel = JoystickWindow(self)
        self.joystick_panel.set_stage_controller(self.stage_controller)
        jog = self.settings_manager.jog_configuration()
        self.joystick_panel.apply_jog_settings(
            jog.linear_distance_mm,
            jog.rotary_distance_deg,
            jog.motion_safety_disabled,
            jog.show_axis_a_controls,
            jog.show_axis_b_controls,
            jog.manual_axis_controls_enabled,
            jog.manual_axis,
            jog.manual_axis_distance_mm,
            jog.manual_axis_mode,
            jog.manual_axis_feedrate_mm_min,
        )
        self.stage_controller.set_motion_safety_disabled(jog.motion_safety_disabled)
        self.joystick_panel.set_serial(self.serial_connection)
        self.joystick_panel.autofocus_requested.connect(
            self.stage_controller.request_autofocus
        )
        self.joystick_panel.home_axis_requested.connect(
            self.stage_controller.request_home_axis
        )
        self.joystick_panel.home_all_requested.connect(
            self.stage_controller.request_home_all
        )
        self.joystick_panel.needles_raise_requested.connect(
            self.stage_controller.request_needles_raise
        )
        self.joystick_panel.needles_lower_requested.connect(
            self.stage_controller.request_needles_lower
        )
        self.joystick_panel.reset_calibration_requested.connect(
            self._reset_click_calibration
        )
        self.joystick_panel.zero_b_requested.connect(self._zero_b_axis)
        self.joystick_panel.manual_axis_move_requested.connect(
            self.stage_controller.request_manual_axis_move
        )
        self.joystick_panel.manual_axis_settings_changed.connect(
            self._save_manual_axis_jog_settings
        )
        self.joystick_panel.motion_axis_requested.connect(self._on_manual_motion_axis)
        self.joystick_panel.jog_command_changed.connect(
            self._on_manual_jog_command_changed
        )
        self.joystick_panel.jog_stopped.connect(self._on_manual_jog_stopped)
        self.joystick_panel.reset_requested.connect(
            lambda: self._invalidate_design_registration(
                "Design registration cleared after controller reset."
            )
        )
        self.stage_controller.homing_status_changed.connect(
            self.joystick_panel.set_homing_status
        )
        self.stage_controller.homing_status_changed.connect(self._on_homing_status_changed)
        self.stage_controller.homing_status_changed.connect(self._persist_controller_state)
        self.stage_controller.homing_action_started.connect(
            self.joystick_panel.set_homing_action_started
        )
        self.stage_controller.homing_action_finished.connect(
            self.joystick_panel.set_homing_action_finished
        )
        self.stage_controller.homing_action_finished.connect(self._on_homing_action_finished)
        self.stage_controller.axis_a_ready_changed.connect(
            self.joystick_panel.set_axis_a_ready
        )
        self.stage_controller.needles_state_changed.connect(
            self.joystick_panel.set_needles_state
        )
        self.stage_controller.needles_state_changed.connect(self._persist_controller_state)
        self.stage_controller.needles_action_started.connect(
            self.joystick_panel.set_needles_action_started
        )
        self.stage_controller.needles_action_finished.connect(
            self.joystick_panel.set_needles_action_finished
        )
        self.joystick_panel.reset_requested.connect(
            self.stage_controller.cancel_active_task
        )
        self.stage_controller.stage_position_changed.connect(self._persist_controller_state)
        self.joystick_dock = CollapsibleDockWidget("Joystick", self)
        self.joystick_dock.setObjectName("JoystickDock")
        self.joystick_dock.setWidget(self.joystick_panel)
        self.joystick_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self.joystick_dock)
        self.splitDockWidget(
            self.serial_connection_dock, self.joystick_dock, Qt.Vertical
        )

        self.contact_calibration_window = ContactOscillationWindow()
        self.contact_calibration_window.visibility_changed.connect(
            self._on_contact_calibration_window_visibility_changed
        )
        self.contact_calibration_window.autofocus_requested.connect(
            self.stage_controller.request_autofocus
        )
        self.contact_calibration_window.save_surface_position_requested.connect(
            self._save_surface_position
        )
        self.contact_calibration_window.move_to_surface_position_requested.connect(
            self._move_to_surface_position
        )

        self.needle_calibration_panel = self.contact_calibration_window.needle_panel
        self.needle_calibration_panel.connect_requested.connect(
            self.lcr_controller.request_connect
        )
        self.needle_calibration_panel.disconnect_requested.connect(
            self.lcr_controller.request_disconnect
        )
        self.needle_calibration_panel.start_requested.connect(
            self._start_needle_calibration
        )
        self.needle_calibration_panel.stop_requested.connect(
            self._stop_needle_calibration
        )
        self.needle_calibration_panel.adjust_requested.connect(
            self.stage_controller.request_needles_adjust
        )
        self.needle_calibration_panel.save_current_requested.connect(
            self._save_current_needle_height
        )
        self.needle_calibration_panel.lower_to_saved_requested.connect(
            self.stage_controller.request_needles_lower
        )
        self.needle_calibration_panel.raise_needles_requested.connect(
            self.stage_controller.request_needles_raise
        )
        self.lcr_controller.connection_changed.connect(
            self._on_lcr_connection_changed
        )
        self.lcr_controller.reading_updated.connect(
            self._on_lcr_reading_updated
        )

        self.serial_terminal_panel = SerialTerminalWindow(self)
        self.serial_terminal_panel.set_stage_controller(self.stage_controller)
        self.serial_terminal_panel.set_serial(self.serial_connection)
        self.serial_terminal_panel.manual_command_sent.connect(
            self._on_manual_terminal_command
        )
        self.serial_terminal_dock = CollapsibleDockWidget("Serial Terminal", self)
        self.serial_terminal_dock.setObjectName("SerialTerminalDock")
        self.serial_terminal_dock.setWidget(self.serial_terminal_panel)
        self.serial_terminal_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self.serial_terminal_dock)
        self.splitDockWidget(self.joystick_dock, self.serial_terminal_dock, Qt.Vertical)

        self.oscillation_panel = self.contact_calibration_window.oscillation_panel
        self.oscillation_panel.start_requested.connect(
            self.stage_controller.request_oscillation
        )
        self.oscillation_panel.start_requested.connect(self._save_oscillation_configuration)
        self.oscillation_panel.stop_requested.connect(
            self.stage_controller.request_stop_oscillation
        )
        self.oscillation_panel.configuration_changed.connect(
            self._save_oscillation_configuration
        )
        self.joystick_dock.raise_()

        self.alignment_panel = AlignmentPanel(self)
        self.alignment_panel.open_design_window_requested.connect(
            lambda: self._toggle_design_layout_window(True)
        )
        self.alignment_panel.capture_point_requested.connect(
            self._request_alignment_capture
        )
        self.alignment_panel.reset_points_requested.connect(
            self._reset_alignment_capture_points
        )
        self.alignment_panel.cancel_pick_requested.connect(
            self._cancel_manual_alignment_pick
        )
        self.alignment_panel.clear_registration_requested.connect(
            self._clear_design_registration
        )
        self.alignment_dock = CollapsibleDockWidget("Alignment", self)
        self.alignment_dock.setObjectName("AlignmentDock")
        self.alignment_dock.setWidget(self.alignment_panel)
        self.alignment_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.RightDockWidgetArea, self.alignment_dock)
        self.alignment_dock.hide()
        self._refresh_manual_alignment_ui()
        self._update_coordinate_display()
        self._refresh_design_panel()
        self.resizeDocks(
            [self.serial_connection_dock, self.joystick_dock, self.serial_terminal_dock],
            [150, 340, 220],
            Qt.Vertical,
        )
        self.resizeDocks(
            [self.joystick_dock, self.alignment_dock],
            [360, 520],
            Qt.Horizontal,
        )

    def _create_design_layout_window(
        self,
        design_layout_window_class: object | None = None,
    ) -> None:
        if self.design_layout_window is not None:
            return
        if design_layout_window_class is None:
            from probe_station_gui.views.design_navigator_panel import (
                DesignLayoutWindow as design_layout_window_class,
            )
        self.design_layout_window = design_layout_window_class()
        self.design_navigator_panel = self.design_layout_window.navigator_panel
        self.design_navigator_panel.load_design_requested.connect(self._load_design_document)
        self.design_navigator_panel.unload_design_requested.connect(
            self._unload_design_document
        )
        self.design_navigator_panel.top_cell_changed.connect(self._set_design_top_cell)
        self.design_navigator_panel.layer_visibility_changed.connect(
            self._set_design_layer_visibility
        )
        self.design_navigator_panel.load_script_requested.connect(
            self._load_measurement_script
        )
        self.design_navigator_panel.reload_script_requested.connect(
            self._reload_measurement_script
        )
        self.design_navigator_panel.move_to_target_requested.connect(
            self._move_to_design_target
        )
        self.design_navigator_panel.next_target_requested.connect(
            self._select_next_design_target
        )
        self.design_navigator_panel.previous_target_requested.connect(
            self._select_previous_design_target
        )
        self.design_navigator_panel.target_selected.connect(
            self._on_design_target_selected
        )
        self.design_navigator_panel.snap_enabled_changed.connect(
            self._on_design_snap_enabled_changed
        )
        self.design_layout_window.calibration_point_selected.connect(
            self._on_design_layout_point_selected
        )
        self.design_layout_window.move_requested.connect(
            lambda x_value, y_value: self._move_to_design_window_point(x_value, y_value)
        )
        self.design_layout_window.hover_snap_changed.connect(
            self.design_navigator_panel.set_hover_snap
        )
        self.design_layout_window.visibility_changed.connect(
            self._on_design_layout_window_visibility_changed
        )
        self.design_navigator_panel.set_design_dialog_directory(
            self.settings_manager.design_last_directory()
        )
        self._refresh_design_panel()
        if self._design_layout_window_requested:
            self.design_layout_window.show_and_raise()
            self._collapse_alignment_panel_if_ready()

    def _move_to_design_window_point(self, x_value: float, y_value: float) -> None:
        design_xy = (float(x_value), float(y_value))
        if not self._move_to_design_coordinate(design_xy, source_label="design window"):
            return
        self._show_status(
            f"Moving to design point X={design_xy[0]:.3f}, Y={design_xy[1]:.3f}.",
            3000,
        )

    def _start_needle_calibration(self) -> None:
        if self.serial_connection is None or not self.serial_connection.is_open:
            self._show_status("Connect the stage controller before needle calibration.")
            return
        if not self.lcr_controller.is_connected():
            self._show_status("Connect the LCR meter before starting calibration.")
            return
        self._needle_calibration_active = True
        if self.needle_calibration_panel:
            self.needle_calibration_panel.set_calibration_active(True)
        latest_a = self.stage_controller.latest_a_position()
        if latest_a is not None:
            self._on_needle_height_changed(latest_a)
        else:
            logger.debug(
                "Needle calibration started without cached A position; requesting status refresh."
            )
            self.stage_controller.request_status_refresh()
        self._show_status(
            "Needle calibration started. Move above metal and lower the needles in steps until the LCR reports a short."
        )

    def _stop_needle_calibration(self) -> None:
        self._needle_calibration_active = False
        if self.needle_calibration_panel:
            self.needle_calibration_panel.set_calibration_active(False)
        self._show_status("Needle calibration stopped.")

    def _refresh_needle_height(self) -> None:
        if not self._needle_calibration_active:
            return
        a_position = self.stage_controller.latest_a_position()
        if a_position is None:
            logger.debug(
                "Needle height refresh has no cached A position; requesting status refresh."
            )
            self.stage_controller.request_status_refresh()
            return
        self._on_needle_height_changed(a_position)

    def _on_needle_height_changed(self, a_position: float) -> None:
        if self.needle_calibration_panel:
            self.needle_calibration_panel.set_current_a(a_position)

    def _on_lcr_connection_changed(
        self, connected: bool, backend_name: str, description: str
    ) -> None:
        if self.needle_calibration_panel:
            self.needle_calibration_panel.set_connection_state(
                connected, backend_name, description
            )
            if not connected:
                self.needle_calibration_panel.set_reading(None, False)

    def _on_lcr_reading_updated(self, resistance_ohm: float, is_short: bool) -> None:
        if self.needle_calibration_panel:
            self.needle_calibration_panel.set_reading(resistance_ohm, is_short)

    def _set_design_snap_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        self._design_snap_enabled = enabled
        if self.design_layout_window is not None:
            self.design_layout_window.set_snap_enabled(enabled)

    def _on_design_snap_enabled_changed(self, enabled: bool) -> None:
        self._set_design_snap_enabled(enabled)

    def _save_current_needle_height(self) -> None:
        a_position = self.stage_controller.latest_a_position()
        if a_position is None:
            logger.debug("Saving needle height without cached A position; querying controller.")
            a_position = self.stage_controller.current_a_position()
        if a_position is None:
            reason = (
                self.stage_controller.last_a_position_read_failure()
                or "unknown reason"
            )
            if "stage task is active" in reason:
                status_reason = "stage is busy"
            elif "serial connection" in reason:
                status_reason = "serial connection is unavailable"
            elif "status query" in reason:
                status_reason = "controller status was unavailable"
            elif "does not include A axis" in reason:
                status_reason = "controller status did not include A"
            else:
                status_reason = "see log for details"
            logger.warning("Unable to save needle down height: %s", reason)
            self._show_status(f"Unable to read A position: {status_reason}.")
            return
        settings = self.settings_manager.settings.clone()
        settings.needle_calibration.down_position_mm = a_position
        settings.needle_calibration.down_position_configured = True
        self.settings_manager.replace(settings)
        self.settings_manager.save()
        self._apply_settings()
        self._show_status(f"Saved needle down height at A={a_position:.4f} mm.")

    def _save_surface_position(self, target: str) -> None:
        target_key = target.strip().lower()
        if target_key not in {"chip", "stone"}:
            self._show_status(f"Unknown calibration position '{target}'.")
            return
        try:
            current_position = self.stage_controller.current_stage_position()
        except Exception as exc:
            self._show_status(str(exc))
            return
        if len(current_position) < 3:
            self._show_status("Controller did not report X/Y/Z coordinates.")
            return
        settings = self.settings_manager.settings.clone()
        saved_position = (
            settings.needle_calibration.chip_position
            if target_key == "chip"
            else settings.needle_calibration.stone_position
        )
        saved_position.x_mm = float(current_position[0])
        saved_position.y_mm = float(current_position[1])
        saved_position.z_mm = float(current_position[2])
        saved_position.configured = True
        self.settings_manager.replace(settings)
        self.settings_manager.save()
        self._apply_settings()
        self._show_status(
            f"Saved {target_key} focus at "
            f"X={saved_position.x_mm:.4f}, "
            f"Y={saved_position.y_mm:.4f}, "
            f"Z={saved_position.z_mm:.4f} mm."
        )

    def _move_to_surface_position(self, target: str) -> None:
        target_key = target.strip().lower()
        if target_key not in {"chip", "stone"}:
            self._show_status(f"Unknown calibration position '{target}'.")
            return
        settings = self.settings_manager.needle_calibration_configuration()
        destination = (
            settings.chip_position if target_key == "chip" else settings.stone_position
        )
        other = (
            settings.stone_position if target_key == "chip" else settings.chip_position
        )
        if not destination.configured:
            self._show_status(f"Save the {target_key} focus position first.")
            return
        transit_z = destination.z_mm
        if other.configured:
            transit_z = min(destination.z_mm, other.z_mm)
        self.stage_controller.request_move_to_xyz(
            destination.x_mm,
            destination.y_mm,
            destination.z_mm,
            transit_z,
            f"{target_key} position",
        )

    def _on_oscillation_state_changed(self, running: bool, axis: str) -> None:
        if self.oscillation_panel:
            self.oscillation_panel.set_running(running, axis)

    def _save_oscillation_configuration(
        self,
        mode: str,
        amplitude_mm: float,
        feedrate_mm_min: float,
        turns_per_sweep: float,
    ) -> None:
        settings = self.settings_manager.settings.clone()
        settings.oscillation.mode = str(mode).strip().upper() or "X"
        settings.oscillation.amplitude_mm = float(amplitude_mm)
        settings.oscillation.feedrate_mm_min = float(feedrate_mm_min)
        settings.oscillation.turns_per_sweep = float(turns_per_sweep)
        self.settings_manager.replace(settings)
        self.settings_manager.save()


def _screen_available_geometry(window: QMainWindow):
    screen = window.screen() or QApplication.primaryScreen()
    if screen is None:
        return None
    return screen.availableGeometry()


def _set_initial_window_geometry(window: QMainWindow) -> None:
    available = _screen_available_geometry(window)
    if available is None:
        return
    bounds = available.adjusted(12, 12, -12, -12)
    if bounds.width() <= 0 or bounds.height() <= 0:
        bounds = available
    width = min(1600, bounds.width())
    height = min(1000, bounds.height())
    window.resize(width, height)
    window.move(bounds.left(), bounds.top())


def _fit_window_to_screen(window: QMainWindow) -> None:
    available = _screen_available_geometry(window)
    if available is None:
        return
    bounds = available.adjusted(4, 4, -4, -4)
    if bounds.width() <= 0 or bounds.height() <= 0:
        bounds = available
    if window.width() > bounds.width() or window.height() > bounds.height():
        window.resize(
            min(window.width(), bounds.width()),
            min(window.height(), bounds.height()),
        )
    frame = window.frameGeometry()
    target_x = frame.x()
    target_y = frame.y()
    if frame.right() > available.right():
        target_x -= frame.right() - available.right()
    if frame.bottom() > available.bottom():
        target_y -= frame.bottom() - available.bottom()
    if target_x < available.left():
        target_x = available.left()
    if target_y < available.top():
        target_y = available.top()
    delta = frame.topLeft() - window.pos()
    window.move(target_x - delta.x(), target_y - delta.y())


def main() -> int:
    app = QApplication(sys.argv)
    window = Main()
    _set_initial_window_geometry(window)
    window.show()
    QTimer.singleShot(0, lambda: _fit_window_to_screen(window))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
