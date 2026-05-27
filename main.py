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
from typing import Any, Callable, TYPE_CHECKING

from PySide6.QtCore import QObject, QLocale, QThread, QTimer, Qt, QUrl, Signal
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QDoubleValidator,
    QImage,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
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
from probe_station_gui.diagnostics import configure_crash_diagnostics
from probe_station_gui.dialogs.settings_dialog import SettingsDialog
from probe_station_gui.api_server import ProbeStationApiServer
from probe_station_gui.lcr_meter import LCRMeterController
from probe_station_gui.motion_prediction import interpolate_position, motion_progress
from probe_station_gui.objective_offsets import (
    ObjectiveOffsetReference,
    base_objective_name,
    calibrated_objective_offset,
    camera_stage_to_raw_stage,
    objective_xy_offset,
    objective_xy_offset_is_configured,
    raw_stage_to_camera_stage,
)
from probe_station_gui.route_model import MeasurementRoute
from probe_station_gui.route_measurement import (
    RouteMeasurementPoint,
    RouteMeasurementRecord,
    RouteMeasurementRunner,
)
from probe_station_gui.settings_manager import (
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
    Settings,
    SettingsManager,
    default_objective,
    normalize_objective_name,
    ordered_objective_names,
)
from probe_station_gui.views.alignment_panel import AlignmentPanel
from probe_station_gui.views.contact_oscillation_window import (
    ContactOscillationWindow,
)
from probe_station_gui.views.dock_widgets import CollapsibleDockWidget
from probe_station_gui.views.oscillation_panel import OscillationPanel
from probe_station_gui.views.serial_connection_panel import SerialConnectionPanel


logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from probe_station_gui.views.surface_map_panel import SurfaceMapWindow
    from probe_station_gui.views.design_navigator_panel import (
        DesignLayoutWindow,
        DesignNavigatorPanel,
    )


class _ApiRequestBridge(QObject):
    """Route API thread requests onto the Qt GUI thread."""

    request_received: Signal = Signal(object)

    def __init__(
        self,
        handler: Callable[[dict[str, Any]], dict[str, Any]],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._handler = handler
        self.request_received.connect(
            self._handle_request,
            Qt.ConnectionType.QueuedConnection,
        )

    def submit(
        self,
        request: dict[str, Any],
        *,
        timeout_s: float = 5.0,
    ) -> dict[str, Any]:
        event = threading.Event()
        envelope: dict[str, Any] = {
            "request": dict(request),
            "result": None,
            "event": event,
        }
        self.request_received.emit(envelope)
        if not event.wait(timeout_s):
            return {
                "accepted": False,
                "status_code": 503,
                "message": "GUI did not process the API request in time.",
            }
        result = envelope.get("result")
        if isinstance(result, dict):
            return result
        return {
            "accepted": False,
            "status_code": 500,
            "message": "GUI returned an invalid API response.",
        }

    def _handle_request(self, envelope: object) -> None:
        if not isinstance(envelope, dict):
            return
        event = envelope.get("event")
        try:
            request = envelope.get("request")
            if not isinstance(request, dict):
                raise ValueError("Invalid API request envelope.")
            envelope["result"] = self._handler(request)
        except Exception as exc:
            logger.exception("Failed to handle API request.")
            envelope["result"] = {
                "accepted": False,
                "status_code": 500,
                "message": str(exc),
            }
        finally:
            if isinstance(event, threading.Event):
                event.set()


class ClickCalibrationDialog(QDialog):
    """Small objective-aware click-to-move calibration editor."""

    objective_selected: Signal = Signal(str)
    reset_requested: Signal = Signal()
    add_requested: Signal = Signal()
    delete_requested: Signal = Signal(str)
    offset_reference_requested: Signal = Signal()
    offset_save_requested: Signal = Signal()
    offset_reset_requested: Signal = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Click-to-Move Calibration")
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self._updating = False

        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self._objective_combo = QComboBox(self)
        self._objective_combo.currentIndexChanged.connect(
            self._on_objective_changed
        )
        form.addRow(QLabel("Objective", self), self._objective_combo)

        self._matrix_cells: list[list[QLabel]] = []
        form.addRow(QLabel("pixels_to_mm", self), self._create_matrix_table())
        self._offset_label = QLabel("--", self)
        self._offset_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(QLabel("objective_xy_offset", self), self._offset_label)
        layout.addLayout(form)

        offset_button_layout = QHBoxLayout()
        self._set_offset_reference_button = QPushButton("Set Reference", self)
        self._save_offset_button = QPushButton("Save Offset", self)
        self._reset_offset_button = QPushButton("Reset Offset", self)
        offset_button_layout.addWidget(self._set_offset_reference_button)
        offset_button_layout.addWidget(self._save_offset_button)
        offset_button_layout.addWidget(self._reset_offset_button)
        offset_button_layout.addStretch(1)
        layout.addLayout(offset_button_layout)

        button_layout = QHBoxLayout()
        self._reset_button = QPushButton("Reset", self)
        self._add_button = QPushButton("Add", self)
        self._delete_button = QPushButton("Delete", self)
        close_button = QPushButton("Close", self)
        button_layout.addWidget(self._reset_button)
        button_layout.addStretch(1)
        button_layout.addWidget(self._add_button)
        button_layout.addWidget(self._delete_button)
        button_layout.addWidget(close_button)
        layout.addLayout(button_layout)

        self._reset_button.clicked.connect(self.reset_requested.emit)
        self._add_button.clicked.connect(self.add_requested.emit)
        self._delete_button.clicked.connect(self._request_delete)
        self._set_offset_reference_button.clicked.connect(
            self.offset_reference_requested.emit
        )
        self._save_offset_button.clicked.connect(self.offset_save_requested.emit)
        self._reset_offset_button.clicked.connect(self.offset_reset_requested.emit)
        close_button.clicked.connect(self.close)

    def set_objectives(self, objectives: ObjectivesSettings) -> None:
        names = ordered_objective_names(objectives.objectives)
        active_name = normalize_objective_name(objectives.active_name)
        if active_name and active_name not in names:
            names.append(active_name)

        self._updating = True
        try:
            self._objective_combo.clear()
            for name in names:
                self._objective_combo.addItem(name, name)
            index = self._objective_combo.findData(active_name)
            if index < 0:
                index = 0
            self._objective_combo.setCurrentIndex(index)
        finally:
            self._updating = False

        self._delete_button.setEnabled(len(names) > 1)
        name = str(self._objective_combo.currentData())
        profile = objectives.objectives.get(name)
        self._set_matrix(profile)
        self._set_offset(objectives, name, profile)

    def _on_objective_changed(self, _index: int) -> None:
        if self._updating:
            return
        name = str(self._objective_combo.currentData() or "")
        if name:
            self.objective_selected.emit(name)

    def _request_delete(self) -> None:
        name = str(self._objective_combo.currentData() or "")
        if name:
            self.delete_requested.emit(name)

    def _create_matrix_table(self) -> QWidget:
        table = QWidget(self)
        layout = QGridLayout(table)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(12)
        layout.setVerticalSpacing(6)

        headers = (
            (0, 0, "bed \\ camera"),
            (0, 1, "X_camera px"),
            (0, 2, "Y_camera px"),
            (1, 0, "X_bed mm"),
            (2, 0, "Y_bed mm"),
        )
        for row, column, text in headers:
            label = QLabel(text, table)
            label.setStyleSheet("font-weight: 600;")
            label.setAlignment(Qt.AlignCenter)
            layout.addWidget(label, row, column)

        for row in range(2):
            row_cells: list[QLabel] = []
            for column in range(2):
                value = QLabel("--", table)
                value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                value.setMinimumWidth(120)
                value.setTextInteractionFlags(Qt.TextSelectableByMouse)
                value.setStyleSheet(
                    "font-family: Consolas, monospace; padding: 2px 6px;"
                )
                layout.addWidget(value, row + 1, column + 1)
                row_cells.append(value)
            self._matrix_cells.append(row_cells)
        return table

    def _set_matrix(self, profile: ObjectiveCalibrationSettings | None) -> None:
        matrix = self._matrix_from_profile(profile)
        for row, cells in enumerate(self._matrix_cells):
            for column, cell in enumerate(cells):
                if matrix is None:
                    cell.setText("--")
                else:
                    cell.setText(self._format_matrix_value(matrix[row][column]))

    def _set_offset(
        self,
        objectives: ObjectivesSettings,
        objective_name: str,
        profile: ObjectiveCalibrationSettings | None,
    ) -> None:
        name = normalize_objective_name(objective_name)
        if not name:
            self._offset_label.setText("--")
            return
        if name == base_objective_name(objectives.objectives):
            self._offset_label.setText("X +0.0000 mm, Y +0.0000 mm (base)")
            return
        if profile is None or not profile.xy_offset_configured:
            self._offset_label.setText("--")
            return
        self._offset_label.setText(
            f"X {float(profile.xy_offset_x_mm):+.4f} mm, "
            f"Y {float(profile.xy_offset_y_mm):+.4f} mm"
        )

    @staticmethod
    def _matrix_from_profile(
        profile: ObjectiveCalibrationSettings | None,
    ) -> tuple[tuple[float, float], tuple[float, float]] | None:
        if profile is None or not profile.xy_calibration_configured:
            return None
        try:
            row_x = profile.pixels_to_mm[0]
            row_y = profile.pixels_to_mm[1]
            matrix = (
                (float(row_x[0]), float(row_x[1])),
                (float(row_y[0]), float(row_y[1])),
            )
        except (TypeError, ValueError, IndexError):
            return None
        if not all(math.isfinite(value) for row in matrix for value in row):
            return None
        return matrix

    @staticmethod
    def _format_matrix_value(value: float) -> str:
        return f"{value:.9g}"


class Main(QMainWindow):
    """Main application window wiring the camera view and serial dialog."""

    design_layout_module_ready: Signal = Signal(object, object)
    design_document_loaded: Signal = Signal(int, object, object)
    route_measurement_status: Signal = Signal(str)
    route_measurement_recorded: Signal = Signal(object, int, int)
    route_measurement_finished: Signal = Signal(bool, str, str)

    ALIGNMENT_CAPTURE_SHORTCUT = "Space"
    ALIGNMENT_TARGET_ANGLES = (-180.0, -90.0, 0.0, 90.0, 180.0)
    DESIGN_POSITION_REFRESH_MS = 800
    CONTROLLER_STATUS_REFRESH_MS = 800
    DESIGN_OVERLAY_UPDATE_MS = 120
    DESIGN_RESTORE_POSITION_TOLERANCE = 1e-3
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
    STAGE_COORDINATE_BLINK_MS = 250
    PLANNED_MOVE_DURATION_PADDING_S = 0.12
    COORDINATE_MOVE_MIN_IDLE_ACCEPT_S = 0.15
    TERMINAL_REFRESH_DELAYS_MS = (180, 500)
    TERMINAL_RESET_REFRESH_DELAYS_MS = (500, 1100, 1800)
    TERMINAL_RESUME_AFTER_JOG_MS = 180
    STAGE_AXIS_NAMES = ("X", "Y", "Z", "A", "B", "C")
    STAGE_AXIS_DIMMED_BACKGROUNDS = {
        "#1565c0": "#6f8fb8",
        "#f0b429": "#cda75a",
        "#c62828": "#ad6b6b",
    }
    STAGE_AXIS_PENDING_BACKGROUNDS = {
        "#1565c0": "#8aa5c9",
        "#f0b429": "#d8bd78",
        "#c62828": "#b98585",
    }
    STAGE_AXIS_EDITED_BACKGROUND = "#d7b8ff"
    STAGE_AXIS_EDITED_FOREGROUND = "#1f1233"
    B_POSITION_CHANGE_TOLERANCE_DEG = 1e-3
    CAMERA_UI_FRAME_GAP_WARNING_S = 0.25
    CLICK_TO_MOVE_PENDING_RETRY_MS = 150
    CLICK_TARGET_ANIMATION_PADDING_S = 0.03

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Microscope control")
        self.menuBar().setNativeMenuBar(False)

        self.view = MicroscopeView()
        self.view.set_target_pending_blink_interval(self.STAGE_COORDINATE_BLINK_MS)
        self.view.set_target_motion_update_interval(self.MANUAL_JOG_UPDATE_MS)
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
        self._api_bridge: _ApiRequestBridge | None = None
        self._api_server: ProbeStationApiServer | None = None
        self._api_settings_signature: tuple[bool, str, int] | None = None
        self.joystick_panel: JoystickWindow | None = None
        self.serial_terminal_panel: SerialTerminalWindow | None = None
        self.serial_connection_panel: SerialConnectionPanel | None = None
        self.oscillation_panel: OscillationPanel | None = None
        self.surface_map_window: SurfaceMapWindow | None = None
        self.contact_calibration_window: ContactOscillationWindow | None = None
        self.alignment_panel: AlignmentPanel | None = None
        self.design_navigator_panel: DesignNavigatorPanel | None = None
        self.design_layout_window: DesignLayoutWindow | None = None
        self._design_layout_preload_started = False
        self._design_layout_window_class: object | None = None
        self._design_layout_window_requested = False
        self._design_load_generation = 0
        self._design_load_restore_states: dict[int, dict[str, object]] = {}
        self._design_load_show_window: dict[int, bool] = {}
        self._pending_persisted_design_state: dict[str, object] | None = None
        self._pending_persisted_design_position: tuple[float, ...] | None = None
        self.joystick_dock: CollapsibleDockWidget | None = None
        self.serial_terminal_dock: CollapsibleDockWidget | None = None
        self.serial_connection_dock: CollapsibleDockWidget | None = None
        self.oscillation_dock: CollapsibleDockWidget | None = None
        self.alignment_dock: CollapsibleDockWidget | None = None
        self._alignment_capture_action: QAction | None = None
        self._alignment_exit_action: QAction | None = None
        self._contact_calibration_window_action: QAction | None = None
        self._surface_map_window_action: QAction | None = None
        self._design_layout_window_action: QAction | None = None
        self._click_calibration_action: QAction | None = None
        self._click_calibration_dialog: ClickCalibrationDialog | None = None
        self._objective_offset_reference: ObjectiveOffsetReference | None = None
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
        self._pending_planned_move_target_xy: tuple[float, float] | None = None
        self._pending_planned_move_source_label: str | None = None
        self._coordinate_move_axis: str | None = None
        self._coordinate_move_axes: set[str] = set()
        self._coordinate_move_origin_position: tuple[float, ...] | None = None
        self._coordinate_move_stage_position: tuple[float, ...] | None = None
        self._coordinate_move_target_position: tuple[float, ...] | None = None
        self._coordinate_move_started_at: float | None = None
        self._coordinate_move_ends_at: float | None = None
        self._coordinate_move_programmed_feedrate: float | None = None
        self._coordinate_move_effective_feedrate: float | None = None
        self._pending_click_to_move: tuple[float, float, float, float] | None = None
        self._pending_click_deadline: float | None = None
        self._pending_stage_axis_targets: dict[str, tuple[float, float]] = {}
        self._design_snap_enabled = True
        self._last_reported_b_position: float | None = None
        self._last_camera_frame_ui_timestamp: float | None = None
        self._stage_unhomed_display_origins: dict[str, float] = {}
        self._stage_axis_fields: dict[str, QLineEdit] = {}
        self._stage_axis_raw_values: dict[str, float] = {}
        self._stage_axis_display_values: dict[str, float] = {}
        self._stage_axis_homed: set[str] = set()
        self._stage_limit_axes: set[str] = set()
        self._stage_axis_base_styles: dict[str, tuple[str, str]] = {}
        self._stage_motion_axes: set[str] = set()
        self._stage_motion_blink_dimmed = False
        self._stage_axis_return_commits: set[str] = set()
        self._stage_axis_escape_shortcuts: list[QShortcut] = []
        self._objective_combo: QComboBox | None = None
        self._stage_coordinate_mode_combo: QComboBox | None = None
        self._stage_coordinate_apply_button: QPushButton | None = None
        self._stage_coordinate_cancel_button: QPushButton | None = None
        self._updating_stage_position_fields = False
        self._pending_linear_feedrate_default: float | None = None
        self._homing_active_key: str | None = None
        self._pending_homing_axes: list[str] = []
        self._controller_state_persistence_suspended = False
        self._controller_reboot_recovery_scheduled = False
        self._route_measurement_runner: RouteMeasurementRunner | None = None
        self._route_measurement_thread: threading.Thread | None = None
        self._design_session = DesignSession()
        self.statusBar()
        self._objective_widget = self._create_objective_widget()
        self.statusBar().addPermanentWidget(self._objective_widget, 0)
        self._stage_position_widget = self._create_stage_position_widget()
        self.statusBar().addPermanentWidget(self._stage_position_widget, 0)
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
        self.route_measurement_status.connect(self._on_route_measurement_status)
        self.route_measurement_recorded.connect(self._on_route_measurement_recorded)
        self.route_measurement_finished.connect(self._on_route_measurement_finished)

        self.stage_controller = StageController()
        self.stage_controller.status_message.connect(self._show_status)
        self.stage_controller.movement_finished.connect(self.on_move_finished)
        self.stage_controller.click_move_started.connect(self._on_click_move_started)
        self.stage_controller.absolute_xy_move_started.connect(
            self._on_absolute_xy_move_started
        )
        self.stage_controller.calibration_changed.connect(self.on_calibration_changed)
        self.stage_controller.objective_calibration_updated.connect(
            self._on_objective_calibration_updated
        )
        self.stage_controller.objective_mismatch_detected.connect(
            self._on_objective_mismatch_detected
        )
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
        self._stage_motion_blink_timer = QTimer(self)
        self._stage_motion_blink_timer.setInterval(self.STAGE_COORDINATE_BLINK_MS)
        self._stage_motion_blink_timer.timeout.connect(self._advance_stage_motion_blink)
        self._pending_click_timer = QTimer(self)
        self._pending_click_timer.setInterval(self.CLICK_TO_MOVE_PENDING_RETRY_MS)
        self._pending_click_timer.timeout.connect(self._retry_pending_click_to_move)
        self._linear_feedrate_save_timer = QTimer(self)
        self._linear_feedrate_save_timer.setSingleShot(True)
        self._linear_feedrate_save_timer.setInterval(400)
        self._linear_feedrate_save_timer.timeout.connect(
            self._save_pending_linear_feedrate_default
        )

        self._create_dock_widgets()

        self._setup_menus()
        self._apply_settings()
        self._api_bridge = _ApiRequestBridge(self._handle_api_request, self)
        self._configure_api_server_from_settings(start_if_enabled=False)

        QTimer.singleShot(0, self._start_api_server)
        QTimer.singleShot(0, self._auto_connect_if_possible)
        QTimer.singleShot(0, self._prime_keyboard_focus)
        QTimer.singleShot(0, self._start_camera_thread)
        QTimer.singleShot(1500, self._preload_design_layout_window)

        self.setStyleSheet(
            """
            QMainWindow::separator { width: 8px; height: 8px; background: palette(window); }
            """
        )

    def _start_camera_thread(self) -> None:
        if not self.thread.isRunning():
            self.thread.start()

    def _start_api_server(self) -> None:
        if self._api_server is None:
            return
        _started, message = self._api_server.start()
        if message:
            self._show_status(message, 5000)

    def _configure_api_server_from_settings(self, *, start_if_enabled: bool) -> None:
        api_settings = self.settings_manager.api_configuration()
        signature = (
            bool(api_settings.enabled),
            str(api_settings.host),
            int(api_settings.port),
        )
        if self._api_settings_signature == signature:
            return
        if self._api_server is not None:
            self._api_server.stop()
        self._api_settings_signature = signature
        if not api_settings.enabled:
            self._api_server = None
            if start_if_enabled:
                self._show_status("FastAPI control API is disabled.", 3000)
            return
        self._api_server = ProbeStationApiServer(
            move_callback=self._submit_api_move_request,
            status_callback=self._submit_api_status_request,
            host=api_settings.host,
            port=api_settings.port,
        )
        if start_if_enabled:
            self._start_api_server()

    def _submit_api_move_request(self, move_request: dict[str, Any]) -> dict[str, Any]:
        if self._api_bridge is None:
            return {
                "accepted": False,
                "status_code": 503,
                "message": "GUI API bridge is not ready.",
            }
        targets = move_request.get("targets")
        if not isinstance(targets, dict):
            targets = {}
        return self._api_bridge.submit(
            {
                "action": "move_to_coordinates",
                "targets": dict(targets),
                "mode": move_request.get("mode", "G90"),
                "feedrate": move_request.get("feedrate"),
            }
        )

    def _submit_api_status_request(self) -> dict[str, Any]:
        if self._api_bridge is None:
            return {
                "accepted": False,
                "status_code": 503,
                "message": "GUI API bridge is not ready.",
            }
        return self._api_bridge.submit({"action": "status"})

    def _handle_api_request(self, request: dict[str, Any]) -> dict[str, Any]:
        action = str(request.get("action", "")).strip().lower()
        if action == "move_to_coordinates":
            return self._api_move_to_coordinates(
                request.get("targets"),
                mode=request.get("mode", "G90"),
                feedrate=request.get("feedrate"),
            )
        if action == "status":
            return self._api_stage_status()
        return {
            "accepted": False,
            "status_code": 400,
            "message": f"Unsupported API action: {action}",
        }

    def _api_move_to_coordinates(
        self,
        targets: object,
        *,
        mode: object = "G90",
        feedrate: object = None,
    ) -> dict[str, Any]:
        if not isinstance(targets, dict):
            return {
                "accepted": False,
                "status_code": 400,
                "message": "Coordinate targets must be an object.",
            }
        input_mode = self._normalize_coordinate_input_mode(mode)
        if input_mode is None:
            return {
                "accepted": False,
                "status_code": 400,
                "message": f"Unsupported coordinate mode: {mode}.",
            }
        move_feedrate = self._api_move_feedrate(feedrate)
        if move_feedrate is None:
            return {
                "accepted": False,
                "status_code": 400,
                "message": f"Invalid feedrate: {feedrate}.",
            }

        parsed_targets: dict[str, tuple[float, float]] = {}
        invalid_axes: list[str] = []
        invalid_values: list[str] = []
        unavailable_axes: list[str] = []
        limit_errors: list[str] = []
        for raw_axis, raw_value in targets.items():
            axis = str(raw_axis).strip().upper()
            if axis not in self.STAGE_AXIS_NAMES:
                invalid_axes.append(str(raw_axis))
                continue
            try:
                display_target = float(raw_value)
            except (TypeError, ValueError):
                invalid_values.append(axis)
                continue
            if not math.isfinite(display_target):
                invalid_values.append(axis)
                continue
            raw_target, resolved_display_target = self._resolve_stage_axis_target(
                axis,
                display_target,
                input_mode,
            )
            if raw_target is None:
                unavailable_axes.append(axis)
                continue
            limit_error = self._stage_axis_target_limit_error(
                axis,
                resolved_display_target,
            )
            if limit_error is not None:
                limit_errors.append(limit_error)
                continue
            parsed_targets[axis] = (
                float(raw_target),
                float(resolved_display_target),
            )

        if invalid_axes:
            return {
                "accepted": False,
                "status_code": 400,
                "message": f"Unsupported axes: {', '.join(invalid_axes)}.",
            }
        if invalid_values:
            return {
                "accepted": False,
                "status_code": 400,
                "message": f"Invalid coordinate values for: {', '.join(invalid_values)}.",
            }
        if unavailable_axes:
            return {
                "accepted": False,
                "status_code": 409,
                "message": (
                    "Coordinates are unavailable in the GUI for: "
                    f"{', '.join(unavailable_axes)}."
                ),
            }
        if limit_errors:
            return {
                "accepted": False,
                "status_code": 409,
                "message": " ".join(limit_errors),
            }
        ordered_targets = [
            (axis, *parsed_targets[axis])
            for axis in self.STAGE_AXIS_NAMES
            if axis in parsed_targets
        ]
        if not ordered_targets:
            return {
                "accepted": False,
                "status_code": 400,
                "message": "Provide at least one target coordinate.",
            }

        current_feedrate = move_feedrate
        if self._coordinate_move_axis is not None:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Stage is busy. Ignoring API coordinate target.",
            }

        if self.stage_controller.is_busy():
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Stage is busy. Ignoring API coordinate target.",
            }

        target_map = {
            axis: (raw_target, display_target)
            for axis, raw_target, display_target in ordered_targets
        }
        if not self._start_coordinate_targets_move(
            target_map,
            feedrate_mm_min=current_feedrate,
            source_label="API",
        ):
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Unable to start coordinate move.",
            }
        axes = [axis for axis, _raw, _display in ordered_targets]
        return {
            "accepted": True,
            "message": f"API coordinate move accepted: {', '.join(axes)}.",
            "started_axes": axes,
            "queued_axes": [],
            "mode": input_mode,
            "current_feedrate_mm_min": current_feedrate,
            "coordinate_display": self.stage_controller.coordinate_display_name(),
            "targets": {axis: display for axis, _raw, display in ordered_targets},
        }

    def _api_stage_status(self) -> dict[str, Any]:
        latest_position = self.stage_controller.latest_stage_position()
        return {
            "accepted": True,
            "connected": bool(
                self.serial_connection is not None
                and getattr(self.serial_connection, "is_open", False)
            ),
            "busy": self.stage_controller.is_busy(),
            "state": self.stage_controller.latest_stage_state(),
            "coordinate_display": self.stage_controller.coordinate_display_name(),
            "homed_axes": sorted(self.stage_controller.homed_axes()),
            "position": self._api_axis_value_map(latest_position),
            "display_position": {
                axis: float(value)
                for axis, value in self._stage_axis_display_values.items()
            },
            "pending_targets": {
                axis: float(values[1])
                for axis, values in self._pending_stage_axis_targets.items()
            },
            "active_coordinate_axis": self._coordinate_move_axis,
            "active_coordinate_axes": sorted(self._coordinate_move_axes),
            "current_feedrate_mm_min": self._current_linear_feedrate(),
            "api_default_feedrate_mm_min": (
                self.settings_manager.api_configuration().default_feedrate_mm_min
            ),
        }

    def _surface_map_stage_status(self) -> dict[str, Any]:
        latest_position = self.stage_controller.latest_stage_position()
        display_position = {
            axis: float(value)
            for axis, value in self._stage_axis_display_values.items()
        }
        if isinstance(latest_position, (tuple, list)):
            for axis, value in zip(self.STAGE_AXIS_NAMES, latest_position):
                display_position.setdefault(axis, float(value))
        return {
            "connected": bool(
                self.serial_connection is not None
                and getattr(self.serial_connection, "is_open", False)
            ),
            "busy": self.stage_controller.is_busy(),
            "state": self.stage_controller.latest_stage_state(),
            "coordinate_display": self.stage_controller.coordinate_display_name(),
            "homed_axes": sorted(self.stage_controller.homed_axes()),
            "position": self._api_axis_value_map(latest_position),
            "display_position": display_position,
            "pending_targets": {
                axis: float(values[1])
                for axis, values in self._pending_stage_axis_targets.items()
            },
            "active_coordinate_axis": self._coordinate_move_axis,
            "active_coordinate_axes": sorted(self._coordinate_move_axes),
            "current_feedrate_mm_min": self._current_linear_feedrate(),
        }

    def _surface_map_move_to_xy(
        self,
        x_mm: float,
        y_mm: float,
    ) -> dict[str, Any]:
        if self.serial_connection is None or not self.serial_connection.is_open:
            return {
                "accepted": False,
                "message": "Serial connection is not available.",
            }
        if self._coordinate_move_axis is not None or self.stage_controller.is_busy():
            return {
                "accepted": False,
                "message": "Stage is busy. Ignoring surface-map target.",
            }
        feedrate = max(0.1, float(self._current_linear_feedrate()))
        targets: dict[str, tuple[float, float]] = {}
        for axis, display_target in (("X", x_mm), ("Y", y_mm)):
            try:
                display_value = float(display_target)
            except (TypeError, ValueError):
                return {
                    "accepted": False,
                    "message": f"Invalid {axis} target: {display_target}.",
                }
            if not math.isfinite(display_value):
                return {
                    "accepted": False,
                    "message": f"Invalid {axis} target: {display_target}.",
                }
            raw_target, resolved_display_target = self._resolve_stage_axis_target(
                axis,
                display_value,
                "G90",
            )
            if raw_target is None:
                return {
                    "accepted": False,
                    "message": f"{axis} coordinate is unavailable.",
                }
            limit_error = self._stage_axis_target_limit_error(
                axis,
                resolved_display_target,
            )
            if limit_error is not None:
                return {"accepted": False, "message": limit_error}
            targets[axis] = (float(raw_target), float(resolved_display_target))
        accepted = self._start_coordinate_targets_move(
            targets,
            feedrate_mm_min=feedrate,
            source_label="Surface Map",
        )
        return {
            "accepted": bool(accepted),
            "message": "Surface-map XY move accepted." if accepted else "Unable to start XY move.",
            "targets": {"X": float(x_mm), "Y": float(y_mm)},
            "current_feedrate_mm_min": feedrate,
        }

    def _api_axis_value_map(self, position: object) -> dict[str, float] | None:
        if not isinstance(position, (tuple, list)):
            return None
        values: dict[str, float] = {}
        for axis, value in zip(self.STAGE_AXIS_NAMES, position):
            try:
                values[axis] = float(value)
            except (TypeError, ValueError):
                continue
        return values or None

    def _normalize_coordinate_input_mode(self, mode: object) -> str | None:
        raw_mode = str(mode or "G90").strip().lower()
        if raw_mode in {"", "absolute", "abs", "g90"}:
            return "G90"
        if raw_mode in {"relative", "rel", "g91"}:
            return "G91"
        return None

    def _api_move_feedrate(self, feedrate: object) -> float | None:
        if feedrate is None:
            return self.settings_manager.api_configuration().default_feedrate_mm_min
        try:
            value = float(feedrate)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value <= 0.0:
            return None
        return max(0.1, value)

    def _preload_design_layout_window(self) -> None:
        if (
            self.design_layout_window is not None
            or self._design_layout_window_class is not None
            or self._design_layout_preload_started
        ):
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
        self._design_layout_preload_started = False
        self._design_layout_window_class = design_layout_window_class
        if self._design_layout_window_requested:
            self._create_design_layout_window(design_layout_window_class)

    def on_click(self, dx: float, dy: float, rel_x: float, rel_y: float) -> None:
        if self._manual_alignment_pick_slot is not None:
            self._capture_manual_alignment_clicked(dx, dy)
            return
        if self._start_click_to_move(dx, dy):
            self._clear_pending_click_to_move(clear_cross=False)
            return
        self._queue_pending_click_to_move(dx, dy, rel_x, rel_y)

    def _stage_serial_ready(self) -> bool:
        return bool(
            self.serial_connection is not None
            and getattr(self.serial_connection, "is_open", False)
        )

    def _click_to_move_pending_timeout_s(self) -> float:
        default_timeout_s = (
            self.settings_manager.DEFAULT_CLICK_TO_MOVE_PENDING_TIMEOUT_S
        )
        timeout_s = self.settings_manager.settings.click_to_move.pending_timeout_s
        try:
            timeout_s = float(timeout_s)
        except (TypeError, ValueError):
            timeout_s = default_timeout_s
        if not math.isfinite(timeout_s):
            timeout_s = default_timeout_s
        return min(
            self.settings_manager.MAX_CLICK_TO_MOVE_PENDING_TIMEOUT_S,
            max(
                self.settings_manager.MIN_CLICK_TO_MOVE_PENDING_TIMEOUT_S,
                timeout_s,
            ),
        )

    def _start_click_to_move(self, dx: float, dy: float) -> bool:
        if not self._stage_serial_ready() or self.stage_controller.is_busy():
            return False
        accepted = self.stage_controller.request_move(dx, dy)
        if accepted:
            self._set_stage_motion_axes({"X", "Y"})
        return bool(accepted)

    def _queue_pending_click_to_move(
        self,
        dx: float,
        dy: float,
        rel_x: float,
        rel_y: float,
    ) -> None:
        self._pending_click_to_move = (dx, dy, rel_x, rel_y)
        self._pending_click_deadline = (
            time.monotonic() + self._click_to_move_pending_timeout_s()
        )
        self.view.set_target_pending(True)
        if not self._pending_click_timer.isActive():
            self._pending_click_timer.start()
        if self._stage_serial_ready():
            self._show_status(
                "Stage is busy; click-to-move will start when it is ready.",
                3000,
            )
        else:
            self._show_status(
                "Stage is not connected; click-to-move will wait for it.",
                3000,
            )

    def _retry_pending_click_to_move(self) -> None:
        pending = self._pending_click_to_move
        if pending is None:
            self._clear_pending_click_to_move(clear_cross=False)
            return
        deadline = self._pending_click_deadline
        if deadline is not None and time.monotonic() >= deadline:
            self._clear_pending_click_to_move(clear_cross=True)
            self._show_status(
                "Click-to-move timed out waiting for the stage.",
                5000,
            )
            return
        dx, dy, _rel_x, _rel_y = pending
        if self._start_click_to_move(dx, dy):
            self._clear_pending_click_to_move(clear_cross=False)

    def _clear_pending_click_to_move(self, *, clear_cross: bool) -> None:
        self._pending_click_to_move = None
        self._pending_click_deadline = None
        if self._pending_click_timer.isActive():
            self._pending_click_timer.stop()
        self.view.set_target_pending(False)
        if clear_cross:
            self.view.clear_target_cross()

    def _on_click_move_started(
        self,
        move_x_mm: float,
        move_y_mm: float,
        feedrate_mm_min: float,
    ) -> None:
        try:
            distance_mm = math.hypot(float(move_x_mm), float(move_y_mm))
            feedrate = max(0.1, float(feedrate_mm_min))
        except (TypeError, ValueError):
            return
        if distance_mm <= 1e-9:
            self.view.finish_target_motion_to_center()
            return
        duration_s = (distance_mm / feedrate) * 60.0
        duration_s += self.CLICK_TARGET_ANIMATION_PADDING_S
        self.view.animate_target_cross_to_center(max(duration_s, 0.05))

    def _on_absolute_xy_move_started(
        self,
        target_x_mm: float,
        target_y_mm: float,
        feedrate_mm_min: float,
    ) -> None:
        pending_target = self._pending_planned_move_target_xy
        if pending_target is None:
            return
        try:
            target = (float(target_x_mm), float(target_y_mm))
            feedrate = float(feedrate_mm_min)
        except (TypeError, ValueError):
            self._pending_planned_move_target_xy = None
            self._pending_planned_move_source_label = None
            return
        if (
            math.hypot(
                target[0] - pending_target[0],
                target[1] - pending_target[1],
            )
            > 1e-4
        ):
            logger.debug(
                "MOTION PREDICTION planned_move_start_ignored pending=%s actual=%s",
                self._format_optional_point(pending_target),
                self._format_optional_point(target),
            )
            self._pending_planned_move_target_xy = None
            self._pending_planned_move_source_label = None
            return
        source_label = self._pending_planned_move_source_label or "absolute XY move"
        self._pending_planned_move_target_xy = None
        self._pending_planned_move_source_label = None
        self._start_planned_move_prediction(
            target,
            source_label=source_label,
            feedrate_mm_min=feedrate,
        )

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

    def _create_objective_widget(self) -> QWidget:
        widget = QWidget(self)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        label = QLabel("Objective:", widget)
        label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(label)
        self._objective_combo = QComboBox(widget)
        for name in self._objective_names():
            self._objective_combo.addItem(name, name)
        self._objective_combo.setToolTip(
            "Select the installed microscope objective. Click-to-move and autofocus use this profile."
        )
        self._objective_combo.currentIndexChanged.connect(
            self._on_objective_combo_changed
        )
        layout.addWidget(self._objective_combo)
        return widget

    def _objective_names(self) -> list[str]:
        settings = self.settings_manager.objectives_configuration()
        return ordered_objective_names(settings.objectives)

    def _active_objective_xy_offset(self) -> tuple[float, float]:
        settings = self.settings_manager.objectives_configuration()
        return objective_xy_offset(settings.objectives, settings.active_name)

    def _camera_stage_xy_from_raw_stage_xy(
        self,
        raw_stage_xy: tuple[float, float],
    ) -> tuple[float, float]:
        return raw_stage_to_camera_stage(raw_stage_xy, self._active_objective_xy_offset())

    def _raw_stage_xy_from_camera_stage_xy(
        self,
        camera_stage_xy: tuple[float, float],
    ) -> tuple[float, float]:
        return camera_stage_to_raw_stage(
            camera_stage_xy,
            self._active_objective_xy_offset(),
        )

    def _design_xy_from_raw_stage_xy(
        self,
        raw_stage_xy: tuple[float, float],
    ) -> tuple[float, float] | None:
        camera_stage_xy = self._camera_stage_xy_from_raw_stage_xy(raw_stage_xy)
        return self._design_session.design_from_stage(camera_stage_xy)

    def _raw_stage_xy_from_design_xy(
        self,
        design_xy: tuple[float, float],
    ) -> tuple[float, float] | None:
        camera_stage_xy = self._design_session.stage_from_design(design_xy)
        if camera_stage_xy is None:
            return None
        return self._raw_stage_xy_from_camera_stage_xy(
            (float(camera_stage_xy[0]), float(camera_stage_xy[1]))
        )

    def _create_stage_position_widget(self) -> QWidget:
        widget = QWidget(self)
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        label = QLabel("Position:", widget)
        label.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        layout.addWidget(label)
        for axis_name in self.STAGE_AXIS_NAMES:
            axis_label = QLabel(axis_name, widget)
            axis_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            layout.addWidget(axis_label)
            field = QLineEdit(widget)
            field.setAlignment(Qt.AlignCenter)
            field.setFixedWidth(72)
            field.setPlaceholderText("---")
            field.setToolTip(
                f"Current {axis_name} coordinate. Enter target and press Enter."
            )
            validator = QDoubleValidator(-1000000.0, 1000000.0, 6, field)
            validator.setNotation(QDoubleValidator.StandardNotation)
            validator.setLocale(QLocale.c())
            field.setValidator(validator)
            field.returnPressed.connect(
                lambda axis=axis_name: self._on_stage_axis_return_pressed(axis)
            )
            escape_shortcut = QShortcut(QKeySequence(Qt.Key_Escape), field)
            escape_shortcut.setContext(Qt.WidgetShortcut)
            escape_shortcut.setAutoRepeat(False)
            escape_shortcut.activated.connect(
                lambda axis=axis_name: self._on_stage_axis_escape_pressed(axis)
            )
            self._stage_axis_escape_shortcuts.append(escape_shortcut)
            field.editingFinished.connect(
                lambda axis=axis_name: self._on_stage_axis_editing_finished(axis)
            )
            field.textEdited.connect(
                lambda _text, axis=axis_name: self._on_stage_axis_text_edited(axis)
            )
            self._stage_axis_fields[axis_name] = field
            layout.addWidget(field)
        mode_label = QLabel("Input:", widget)
        mode_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        layout.addWidget(mode_label)
        self._stage_coordinate_mode_combo = QComboBox(widget)
        self._stage_coordinate_mode_combo.addItem("Absolute", "G90")
        self._stage_coordinate_mode_combo.addItem("Relative", "G91")
        self._stage_coordinate_mode_combo.setToolTip(
            "Coordinate input mode. Idle fields always show absolute coordinates."
        )
        self._stage_coordinate_mode_combo.currentIndexChanged.connect(
            self._on_stage_coordinate_mode_changed
        )
        layout.addWidget(self._stage_coordinate_mode_combo)
        self._stage_coordinate_apply_button = QPushButton("Apply", widget)
        self._stage_coordinate_apply_button.setEnabled(False)
        self._stage_coordinate_apply_button.setToolTip(
            "Apply changed coordinate fields as one move."
        )
        self._stage_coordinate_apply_button.clicked.connect(
            self._apply_pending_stage_coordinate_targets
        )
        layout.addWidget(self._stage_coordinate_apply_button)
        self._stage_coordinate_cancel_button = QPushButton("Cancel", widget)
        self._stage_coordinate_cancel_button.setEnabled(False)
        self._stage_coordinate_cancel_button.setToolTip(
            "Clear edited coordinate fields or stop the active coordinate move."
        )
        self._stage_coordinate_cancel_button.clicked.connect(
            self._cancel_stage_coordinate_action
        )
        layout.addWidget(self._stage_coordinate_cancel_button)
        self._set_stage_position_fields_available(False)
        return widget

    def _set_stage_position_fields_available(self, available: bool) -> None:
        self._updating_stage_position_fields = True
        try:
            for axis_name, field in self._stage_axis_fields.items():
                field.blockSignals(True)
                if not available:
                    field.clear()
                    field.setPlaceholderText("---")
                    field.setEnabled(False)
                    field.setModified(False)
                    self._style_stage_axis_field(field, "#e6e6e6", "#666666")
                else:
                    field.setEnabled(True)
                field.blockSignals(False)
        finally:
            self._updating_stage_position_fields = False
        self._update_stage_coordinate_apply_state()

    @staticmethod
    def _style_stage_axis_field(field: QLineEdit, background: str, foreground: str) -> None:
        field.setStyleSheet(
            "QLineEdit {"
            f"background-color: {background}; color: {foreground}; "
            f"border: 1px solid {background}; border-radius: 4px; "
            "padding: 2px 5px;"
            "}"
            "QLineEdit:disabled {"
            f"background-color: {background}; color: {foreground};"
            "}"
        )

    @staticmethod
    def _format_stage_axis_value(value: float) -> str:
        numeric_value = float(value)
        if abs(numeric_value) < 0.0005:
            numeric_value = 0.0
        text = f"{numeric_value:.3f}"
        return text.rstrip("0").rstrip(".") if "." in text else text

    def _display_axis_value_from_raw(self, axis_name: str, raw_value: float) -> float:
        axis = axis_name.strip().upper()
        if not hasattr(self, "stage_controller"):
            return float(raw_value)
        return self.stage_controller.calibrated_axis_display_value(
            axis,
            float(raw_value),
        )

    def _raw_axis_value_from_display(
        self,
        axis_name: str,
        display_value: float,
    ) -> float:
        axis = axis_name.strip().upper()
        if not hasattr(self, "stage_controller"):
            return float(display_value)
        return self.stage_controller.calibrated_axis_raw_value(
            axis,
            float(display_value),
        )

    def _apply_stage_axis_field_style(
        self, axis_name: str, field: QLineEdit | None = None
    ) -> None:
        axis = axis_name.strip().upper()
        target = field or self._stage_axis_fields.get(axis)
        if target is None:
            return
        background, foreground = self._stage_axis_base_styles.get(
            axis, ("#e6e6e6", "#666666")
        )
        if axis in self._pending_stage_axis_targets:
            background = self.STAGE_AXIS_EDITED_BACKGROUND
            foreground = self.STAGE_AXIS_EDITED_FOREGROUND
        elif axis in self._stage_motion_axes and self._stage_motion_blink_dimmed:
            background = self.STAGE_AXIS_DIMMED_BACKGROUNDS.get(background, background)
        self._style_stage_axis_field(target, background, foreground)

    def _refresh_stage_axis_styles(self) -> None:
        for axis_name, field in self._stage_axis_fields.items():
            if axis_name not in self._stage_axis_base_styles:
                continue
            self._apply_stage_axis_field_style(axis_name, field)

    def _set_stage_motion_axes(self, axes: object) -> None:
        if isinstance(axes, str):
            raw_axes = [axes]
        elif isinstance(axes, (set, list, tuple)):
            raw_axes = list(axes)
        else:
            raw_axes = []
        motion_axes = {
            str(axis).strip().upper()
            for axis in raw_axes
            if str(axis).strip().upper() in self.STAGE_AXIS_NAMES
        }
        if not motion_axes:
            self._clear_stage_motion_axes()
            return
        self._stage_motion_axes = motion_axes
        self._stage_motion_blink_dimmed = False
        if not self._stage_motion_blink_timer.isActive():
            self._stage_motion_blink_timer.start()
        self._refresh_stage_axis_styles()

    def _clear_stage_motion_axes(self) -> None:
        if self._stage_motion_blink_timer.isActive():
            self._stage_motion_blink_timer.stop()
        if not self._stage_motion_axes and not self._stage_motion_blink_dimmed:
            return
        self._stage_motion_axes.clear()
        self._stage_motion_blink_dimmed = False
        self._refresh_stage_axis_styles()

    def _advance_stage_motion_blink(self) -> None:
        if not self._stage_motion_axes:
            self._stage_motion_blink_timer.stop()
            self._stage_motion_blink_dimmed = False
            return
        self._stage_motion_blink_dimmed = not self._stage_motion_blink_dimmed
        self._refresh_stage_axis_styles()

    def _on_stage_axis_return_pressed(self, axis_name: str) -> None:
        axis = axis_name.strip().upper()
        if axis in self.STAGE_AXIS_NAMES:
            self._stage_axis_return_commits.add(axis)

    def _on_stage_axis_text_edited(self, _axis_name: str) -> None:
        if self._updating_stage_position_fields:
            return
        self._update_stage_coordinate_apply_state()

    def _on_stage_axis_escape_pressed(self, axis_name: str) -> None:
        axis = axis_name.strip().upper()
        self._stage_axis_return_commits.discard(axis)
        self._pending_stage_axis_targets.pop(axis, None)
        self._reset_stage_axis_field(axis)
        field = self._stage_axis_fields.get(axis)
        if field is not None:
            field.deselect()
            field.clearFocus()
        self._refresh_stage_axis_styles()
        self._update_stage_coordinate_apply_state()
        self.view.setFocus(Qt.OtherFocusReason)

    def _on_stage_coordinate_mode_changed(self) -> None:
        if self._pending_stage_axis_targets:
            self._pending_stage_axis_targets.clear()
            self._update_stage_position_display(self.stage_controller.latest_stage_position())
            self._show_status("Cleared pending coordinate edits after input mode change.", 2000)
        self._update_stage_coordinate_apply_state()

    def _selected_stage_coordinate_input_mode(self) -> str:
        combo = self._stage_coordinate_mode_combo
        if combo is None:
            return "G90"
        mode = self._normalize_coordinate_input_mode(combo.currentData())
        return mode or "G90"

    def _stage_axis_fields_have_modified_text(self) -> bool:
        return any(
            field.isEnabled() and field.isModified()
            for field in self._stage_axis_fields.values()
        )

    def _update_stage_coordinate_apply_state(self) -> None:
        apply_button = self._stage_coordinate_apply_button
        cancel_button = self._stage_coordinate_cancel_button
        available = (
            bool(self._pending_stage_axis_targets)
            or self._stage_axis_fields_have_modified_text()
        )
        controller_busy = (
            hasattr(self, "stage_controller") and self.stage_controller.is_busy()
        )
        active = self._coordinate_move_axis is not None or controller_busy
        if apply_button is not None:
            apply_button.setEnabled(available and not active)
        if cancel_button is not None:
            cancel_button.setEnabled(
                available or self._coordinate_move_axis is not None
            )

    def _clear_pending_stage_coordinate_targets(self) -> bool:
        had_changes = (
            bool(self._pending_stage_axis_targets)
            or self._stage_axis_fields_have_modified_text()
        )
        self._stage_axis_return_commits.clear()
        self._pending_stage_axis_targets.clear()
        for axis in self.STAGE_AXIS_NAMES:
            self._reset_stage_axis_field(axis)
        self._refresh_stage_axis_styles()
        self._update_stage_coordinate_apply_state()
        return had_changes

    def _cancel_stage_coordinate_action(self) -> None:
        if self._coordinate_move_axis is not None:
            self.stage_controller.cancel_active_motion(
                "Coordinate move cancel requested."
            )
            self._clear_coordinate_move_tracking(
                clear_pending=True,
                reset_override=True,
            )
            self._clear_stage_motion_axes()
            self._clear_pending_stage_coordinate_targets()
            self.view.setFocus(Qt.OtherFocusReason)
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
            return
        if self._clear_pending_stage_coordinate_targets():
            self.view.setFocus(Qt.OtherFocusReason)
            self._show_status("Cleared pending coordinate edits.", 2000)

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
        self._persist_serial_connection_state(False)
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
        self._clear_coordinate_move_tracking(clear_pending=True, reset_override=False)
        self._clear_pending_homing_queue()
        self._clear_stage_motion_axes()
        self._clear_planned_move_prediction(clear_wait_state=True)
        logger.info("Serial disconnected")
        self.stage_controller.request_stop_oscillation()
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
        if self.contact_calibration_window is not None:
            self.contact_calibration_window.set_current_stage_position(None)
            self.contact_calibration_window.set_current_needle_lowering(None)
        if self.oscillation_panel:
            self.oscillation_panel.set_running(False, "")
        self._reset_manual_alignment(cancel_pick=True)
        self._invalidate_design_registration(
            "Design registration cleared after serial disconnect."
        )
        self._update_design_position(None)

    def _auto_connect_if_possible(self) -> None:
        if self.serial_connection_panel and not self.serial_connection:
            if self.settings_manager.serial_auto_connect_enabled():
                logger.debug(
                    "Attempting serial auto-connect because previous session closed connected"
                )
                self.serial_connection_panel.auto_connect()
            else:
                logger.debug(
                    "Skipping serial auto-connect because previous session was disconnected"
                )

    def _run_serial_startup_sync(self) -> None:
        if self.serial_connection is None or not self.serial_connection.is_open:
            return
        self.stage_controller.request_startup_sync(
            auto_home_a=True,
            clear_unverified_state=False,
        )

    def _on_controller_reboot_detected(self) -> None:
        self._stage_unhomed_display_origins.clear()
        self._pending_persisted_design_state = None
        self._pending_persisted_design_position = None
        self._invalidate_design_registration(
            "Design registration cleared after controller reboot."
        )

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
            self._pending_persisted_design_state = None
            self._pending_persisted_design_position = None
            self._show_status(
                "Controller session changed. Cleared cached homing state."
            )
            return
        logger.info(
            "Controller session marker matches; restoring cached homing state pending live status."
        )
        self._prepare_persisted_design_restore(cached_state)
        self.stage_controller.import_cached_controller_state(cached_state)
        self._show_status("Restored cached homing state; reading live coordinates.")

    def _persist_controller_state(self, *_args) -> None:
        if self._controller_state_persistence_suspended:
            logger.debug("Skipping controller state persistence while serial state resets.")
            return
        state = self._controller_state_with_design()
        self.settings_manager.save_controller_state(state)

    def _persist_controller_state_if_available(self) -> None:
        if self._controller_state_persistence_suspended:
            return
        state = self._controller_state_with_design()
        if state is None:
            return
        self.settings_manager.save_controller_state(state)

    def _controller_state_with_design(self) -> dict[str, object] | None:
        state = self.stage_controller.export_cached_controller_state()
        if state is None:
            return None
        design_state = self._design_session.export_persisted_state()
        if design_state is not None:
            state["design_session"] = design_state
        return state

    def _prepare_persisted_design_restore(self, cached_state: dict[str, object]) -> None:
        design_state = cached_state.get("design_session")
        cached_position = self._coerce_position_tuple(
            cached_state.get("last_stage_position")
        )
        if not isinstance(design_state, dict) or cached_position is None:
            self._pending_persisted_design_state = None
            self._pending_persisted_design_position = None
            return
        self._pending_persisted_design_state = dict(design_state)
        self._pending_persisted_design_position = cached_position

    def _maybe_restore_persisted_design(self, position: tuple[float, ...]) -> None:
        design_state = self._pending_persisted_design_state
        expected_position = self._pending_persisted_design_position
        if design_state is None:
            return
        self._pending_persisted_design_state = None
        self._pending_persisted_design_position = None
        if self._design_session.document is not None:
            return
        if expected_position is None or not self._positions_match(
            expected_position,
            position,
        ):
            self._show_status(
                "Controller coordinates changed. Cleared cached design selection.",
                5000,
            )
            self._save_controller_state_without_design()
            return
        if not self._persisted_design_file_is_current(design_state):
            self._show_status(
                "Cached design file changed or is unavailable. Cleared cached design selection.",
                5000,
            )
            self._save_controller_state_without_design()
            return
        design_path = str(design_state.get("document_path") or "").strip()
        if not design_path:
            self._save_controller_state_without_design()
            return
        self._start_design_document_load(
            design_path,
            restore_state=design_state,
            show_window=False,
        )

    def _save_controller_state_without_design(self) -> None:
        state = self.stage_controller.export_cached_controller_state()
        if state is None:
            cached_state = self.settings_manager.load_controller_state()
            if isinstance(cached_state, dict):
                cached_state.pop("design_session", None)
                self.settings_manager.save_controller_state(cached_state)
            return
        state.pop("design_session", None)
        self.settings_manager.save_controller_state(state)

    @classmethod
    def _positions_match(
        cls,
        expected: tuple[float, ...],
        actual: tuple[float, ...],
    ) -> bool:
        if not expected or len(actual) < len(expected):
            return False
        tolerance = cls.DESIGN_RESTORE_POSITION_TOLERANCE
        for expected_value, actual_value in zip(expected, actual):
            if not math.isfinite(expected_value) or not math.isfinite(actual_value):
                return False
            if abs(expected_value - actual_value) > tolerance:
                return False
        return True

    @staticmethod
    def _coerce_position_tuple(value: object) -> tuple[float, ...] | None:
        if not isinstance(value, (list, tuple)) or not value:
            return None
        values: list[float] = []
        for item in value:
            try:
                coordinate = float(item)
            except (TypeError, ValueError):
                return None
            if not math.isfinite(coordinate):
                return None
            values.append(coordinate)
        return tuple(values)

    @staticmethod
    def _persisted_design_file_is_current(state: dict[str, object]) -> bool:
        path_text = str(state.get("document_path") or "").strip()
        if not path_text:
            return False
        path = Path(path_text).expanduser()
        try:
            stat = path.stat()
        except OSError:
            return False
        saved_size = state.get("document_size")
        if saved_size is not None:
            try:
                if int(saved_size) != int(stat.st_size):
                    return False
            except (TypeError, ValueError):
                return False
        saved_mtime = state.get("document_mtime_ns")
        if saved_mtime is not None:
            try:
                if int(saved_mtime) != int(stat.st_mtime_ns):
                    return False
            except (TypeError, ValueError):
                return False
        return True

    def _persist_serial_connection_state(self, connected: bool) -> None:
        self.settings_manager.save_serial_connection_state(
            connected,
            port=self.serial_port_name,
            baud_rate=self.serial_baud_rate,
        )

    def _setup_menus(self) -> None:
        app_menu = self.menuBar().addMenu("Application")
        panels_menu = self.menuBar().addMenu("Tools")
        calibration_menu = self.menuBar().addMenu("Calibration")

        settings_action = QAction("Settings…", self)
        settings_action.setText("Settings")
        settings_action.triggered.connect(self._open_settings_dialog)
        app_menu.addAction(settings_action)

        api_settings_action = QAction("API Settings", self)
        api_settings_action.triggered.connect(
            lambda _checked=False: self._open_settings_dialog("API")
        )
        app_menu.addAction(api_settings_action)

        open_log_action = QAction("Open Status Log…", self)
        open_log_action.setText("Open Status Log")
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

        self._surface_map_window_action = QAction("Surface Map", self)
        self._surface_map_window_action.triggered.connect(self._show_surface_map_window)
        calibration_menu.addAction(self._surface_map_window_action)

        self._click_calibration_action = QAction(
            self._click_calibration_action_text(),
            self,
        )
        self._click_calibration_action.triggered.connect(
            self._show_click_calibration_dialog
        )
        calibration_menu.addAction(self._click_calibration_action)

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
                if self._design_layout_window_class is not None:
                    self._create_design_layout_window(self._design_layout_window_class)
                    return
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
            latest_lowering = self.stage_controller.latest_axis_a_lowering()
            if latest_lowering is not None:
                self.contact_calibration_window.set_current_needle_lowering(
                    latest_lowering
                )
            elif self.serial_connection is not None and self.serial_connection.is_open:
                self.stage_controller.request_status_refresh()
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
        self._set_alignment_panel_expanded()
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
            needle_settings = self.settings_manager.needle_calibration_configuration()
            feedrates = self.settings_manager.feedrate_configuration()
            self.joystick_panel.apply_feedrate_settings(
                feedrates.linear.presets,
                feedrates.linear.default,
                feedrates.rotary.presets,
                feedrates.rotary.default,
            )
            self.joystick_panel.apply_needle_settings(
                needle_settings.feedrate_mm_min
            )
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
        self.stage_controller.apply_axis_a_calibration(
            self.settings_manager.axis_a_calibration_configuration()
        )
        self.stage_controller.apply_axis_z_calibration(
            self.settings_manager.axis_z_calibration_configuration()
        )
        self.stage_controller.apply_needle_calibration(
            raise_position_mm=(
                needle_settings.raise_position_mm
                if needle_settings.raise_position_configured
                else None
            ),
            down_position_mm=(
                needle_settings.down_position_mm
                if needle_settings.down_position_configured
                else None
            ),
        )
        if self.joystick_panel is not None:
            self.joystick_panel.set_needle_contact_coordinate(
                "raise",
                self._display_a_for_needle_lowering(
                    needle_settings.raise_position_mm
                    if needle_settings.raise_position_configured
                    else None
                ),
            )
            self.joystick_panel.set_needle_contact_coordinate(
                "lower",
                self._display_a_for_needle_lowering(
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
        self._apply_objective_settings()
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
        if self.serial_connection_panel is not None:
            self.serial_connection_panel.set_lcr_resource(needle_settings.visa_resource)
        if self.contact_calibration_window is not None:
            self.contact_calibration_window.set_saved_needle_height(
                needle_settings.down_position_mm
                if needle_settings.down_position_configured
                else None
            )
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
        if self._api_bridge is not None:
            self._configure_api_server_from_settings(start_if_enabled=True)
        self._update_coordinate_display(cursor_xy=None)

    def _open_settings_dialog(self, initial_tab: object = None) -> None:
        tab_name = initial_tab if isinstance(initial_tab, str) else None
        dialog = SettingsDialog(
            self.settings_manager.settings,
            self,
            initial_tab=tab_name,
        )
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

    def _sync_objective_combo(self, objective_name: str) -> None:
        combo = self._objective_combo
        if combo is None:
            return
        objective_name = normalize_objective_name(objective_name)
        current_names = [
            str(combo.itemData(index) or "")
            for index in range(combo.count())
        ]
        names = self._objective_names()
        if current_names != names:
            combo.blockSignals(True)
            combo.clear()
            for name in names:
                combo.addItem(name, name)
            combo.blockSignals(False)
        index = combo.findData(objective_name)
        if index < 0:
            return
        combo.blockSignals(True)
        combo.setCurrentIndex(index)
        combo.blockSignals(False)
        self._refresh_click_calibration_ui()

    def _on_objective_combo_changed(self, _index: int) -> None:
        combo = self._objective_combo
        if combo is None:
            return
        objective_name = str(combo.currentData() or "").strip().upper()
        if objective_name:
            self._set_active_objective(objective_name, apply_motion=True)

    def _set_active_objective(
        self,
        objective_name: str,
        *,
        apply_motion: bool,
        allow_busy: bool = False,
    ) -> None:
        objective_name = normalize_objective_name(objective_name)
        if not objective_name:
            return
        settings = self.settings_manager.settings.clone()
        old_name = settings.objectives.active_name
        if old_name == objective_name:
            self._refresh_click_calibration_ui()
            return
        if not allow_busy and self.stage_controller.is_busy():
            self._sync_objective_combo(old_name)
            self._show_status("Stage is busy; objective not changed.", 4000)
            return
        if objective_name not in settings.objectives.objectives:
            settings.objectives.objectives[objective_name] = default_objective(
                objective_name
            )
        settings.objectives.active_name = objective_name
        self.settings_manager.replace(settings)
        self.settings_manager.save()
        self._apply_objective_settings()
        if apply_motion:
            self._apply_objective_change_offset(old_name, objective_name)
        self._show_status(f"Objective selected: {objective_name}.", 3000)

    def _apply_objective_change_offset(self, old_name: str, new_name: str) -> None:
        objective_settings = self.settings_manager.objectives_configuration()
        if not objective_settings.apply_offsets_on_change:
            return
        old_profile = objective_settings.objectives.get(old_name)
        new_profile = objective_settings.objectives.get(new_name)
        if old_profile is None or new_profile is None:
            return
        if not (
            objective_xy_offset_is_configured(objective_settings.objectives, old_name)
            and objective_xy_offset_is_configured(
                objective_settings.objectives,
                new_name,
            )
        ):
            self._show_status(
                "Objective XY offset is not configured for both objectives.",
                4000,
            )
            return
        old_offset = objective_xy_offset(objective_settings.objectives, old_name)
        new_offset = objective_xy_offset(objective_settings.objectives, new_name)
        latest = self.stage_controller.latest_stage_position()
        if latest is None or len(latest) < 2:
            self._show_status("Stage position unavailable; objective offset not applied.", 4000)
            return
        raw_targets: dict[str, float] = {
            "X": float(latest[0]) + float(new_offset[0]) - float(old_offset[0]),
            "Y": float(latest[1]) + float(new_offset[1]) - float(old_offset[1]),
        }
        if (
            old_profile.z_offset_configured
            and new_profile.z_offset_configured
            and len(latest) >= 3
        ):
            current_z_display = self._display_axis_value_from_raw("Z", float(latest[2]))
            z_display = (
                current_z_display
                + float(new_profile.z_offset_mm)
                - float(old_profile.z_offset_mm)
            )
            raw_targets["Z"] = self._raw_axis_value_from_display("Z", z_display)
        if self.stage_controller.is_busy():
            self._show_status("Stage is busy; objective offset not applied.", 4000)
            return
        accepted = self.stage_controller.request_absolute_axis_targets_move(
            raw_targets,
            feedrate=self._current_linear_feedrate(),
            allow_unhomed=False,
        )
        if accepted:
            axes = ", ".join(sorted(raw_targets))
            self._show_status(f"Applying {new_name} objective offset on {axes}.", 4000)
        else:
            self._show_status("Objective offset move was not accepted.", 4000)

    def _apply_objective_settings(self) -> None:
        objective_settings = self.settings_manager.objectives_configuration()
        active_objective = objective_settings.objectives.get(
            objective_settings.active_name
        )
        if active_objective is None:
            active_objective = default_objective(objective_settings.active_name)
        self.stage_controller.apply_objective_configuration(
            active_objective,
            objective_settings.objectives,
        )
        self._sync_objective_combo(objective_settings.active_name)
        if self._design_session.document is not None:
            self._refresh_design_position()

    def _show_click_calibration_dialog(self) -> None:
        if self._click_calibration_dialog is None:
            dialog = ClickCalibrationDialog(self)
            dialog.objective_selected.connect(
                lambda name: self._set_active_objective(name, apply_motion=True)
            )
            dialog.reset_requested.connect(self._reset_click_calibration)
            dialog.add_requested.connect(self._add_objective_profile)
            dialog.delete_requested.connect(self._delete_objective_profile)
            dialog.offset_reference_requested.connect(
                self._set_objective_offset_reference
            )
            dialog.offset_save_requested.connect(self._save_active_objective_offset)
            dialog.offset_reset_requested.connect(self._reset_active_objective_offset)
            self._click_calibration_dialog = dialog
        self._refresh_click_calibration_ui()
        self._click_calibration_dialog.show()
        self._click_calibration_dialog.raise_()
        self._click_calibration_dialog.activateWindow()

    def _add_objective_profile(self) -> None:
        if self.stage_controller.is_busy():
            self._show_status("Stage is busy; objective not added.", 4000)
            return
        raw_name, accepted = QInputDialog.getText(
            self,
            "Add Objective",
            "Objective name",
        )
        if not accepted:
            return
        name = normalize_objective_name(raw_name)
        if not name:
            self._show_status(
                "Objective name must use letters, digits, dot, dash, or underscore.",
                5000,
            )
            return
        settings = self.settings_manager.settings.clone()
        if name in settings.objectives.objectives:
            self._set_active_objective(name, apply_motion=True)
            self._show_status(f"Objective already exists: {name}.", 3000)
            return
        settings.objectives.objectives[name] = default_objective(name)
        settings.objectives.active_name = name
        self.settings_manager.replace(settings)
        self.settings_manager.save()
        self._apply_objective_settings()
        self._show_status(f"Objective added: {name}.", 3000)

    def _delete_objective_profile(self, objective_name: str) -> None:
        if self.stage_controller.is_busy():
            self._show_status("Stage is busy; objective not deleted.", 4000)
            self._refresh_click_calibration_ui()
            return
        name = normalize_objective_name(objective_name)
        if not name:
            return
        settings = self.settings_manager.settings.clone()
        profiles = settings.objectives.objectives
        if name not in profiles:
            self._show_status(f"Objective does not exist: {name}.", 4000)
            return
        if len(profiles) <= 1:
            self._show_status("At least one objective profile is required.", 4000)
            return
        response = QMessageBox.question(
            self,
            "Delete Objective",
            f"Delete objective profile {name}?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if response != QMessageBox.Yes:
            return
        del profiles[name]
        if settings.objectives.active_name == name:
            remaining = ordered_objective_names(profiles)
            settings.objectives.active_name = remaining[0]
        self.settings_manager.replace(settings)
        self.settings_manager.save()
        self._apply_objective_settings()
        self._show_status(f"Objective deleted: {name}.", 3000)

    def _set_objective_offset_reference(self) -> None:
        if self.stage_controller.is_busy():
            self._show_status("Stage is busy; objective offset reference not set.", 4000)
            return
        raw_stage_xy = self._resolve_alignment_capture_stage_position()
        if raw_stage_xy is None:
            return
        settings = self.settings_manager.settings.clone()
        active_name = normalize_objective_name(settings.objectives.active_name)
        if not active_name:
            return
        profiles = settings.objectives.objectives
        if active_name not in profiles:
            profiles[active_name] = default_objective(active_name)
        base_name = base_objective_name(profiles)
        if active_name == base_name:
            profile = profiles[active_name]
            profile.xy_offset_x_mm = 0.0
            profile.xy_offset_y_mm = 0.0
            profile.xy_offset_configured = True
            profiles[active_name] = profile
            self.settings_manager.replace(settings)
            self.settings_manager.save()
            self._apply_objective_settings()
        elif not objective_xy_offset_is_configured(profiles, active_name):
            self._show_status(
                "Set the objective offset reference with the base objective first.",
                6000,
            )
            return
        reference_offset = objective_xy_offset(profiles, active_name)
        self._objective_offset_reference = ObjectiveOffsetReference(
            objective_name=active_name,
            stage_xy=raw_stage_xy,
            offset_xy=reference_offset,
        )
        self._refresh_click_calibration_ui()
        self._show_status(
            f"Objective offset reference set with {active_name}. "
            "Center the same feature under another objective and press Save Offset.",
            8000,
        )

    def _save_active_objective_offset(self) -> None:
        if self.stage_controller.is_busy():
            self._show_status("Stage is busy; objective offset not saved.", 4000)
            return
        reference = self._objective_offset_reference
        if reference is None:
            self._show_status("Set an objective offset reference first.", 5000)
            return
        raw_stage_xy = self._resolve_alignment_capture_stage_position()
        if raw_stage_xy is None:
            return
        settings = self.settings_manager.settings.clone()
        active_name = normalize_objective_name(settings.objectives.active_name)
        if not active_name:
            return
        profiles = settings.objectives.objectives
        if active_name not in profiles:
            profiles[active_name] = default_objective(active_name)
        profile = profiles[active_name]
        base_name = base_objective_name(profiles)
        if active_name == base_name:
            offset_xy = (0.0, 0.0)
        else:
            offset_xy = calibrated_objective_offset(reference, raw_stage_xy)
        profile.xy_offset_x_mm = float(offset_xy[0])
        profile.xy_offset_y_mm = float(offset_xy[1])
        profile.xy_offset_configured = True
        profiles[active_name] = profile
        self.settings_manager.replace(settings)
        self.settings_manager.save()
        self._apply_objective_settings()
        self._refresh_design_position()
        self._show_status(
            f"Saved {active_name} objective offset: "
            f"X={offset_xy[0]:+.4f}, Y={offset_xy[1]:+.4f} mm.",
            6000,
        )

    def _reset_active_objective_offset(self) -> None:
        if self.stage_controller.is_busy():
            self._show_status("Stage is busy; objective offset not reset.", 4000)
            return
        settings = self.settings_manager.settings.clone()
        active_name = normalize_objective_name(settings.objectives.active_name)
        if not active_name:
            return
        profiles = settings.objectives.objectives
        if active_name not in profiles:
            profiles[active_name] = default_objective(active_name)
        profile = profiles[active_name]
        profile.xy_offset_x_mm = 0.0
        profile.xy_offset_y_mm = 0.0
        profile.xy_offset_configured = active_name == base_objective_name(profiles)
        profiles[active_name] = profile
        self.settings_manager.replace(settings)
        self.settings_manager.save()
        self._apply_objective_settings()
        self._refresh_design_position()
        self._refresh_click_calibration_ui()
        self._show_status(f"Reset {active_name} objective offset.", 4000)

    def _refresh_click_calibration_ui(self) -> None:
        if self._click_calibration_action is not None:
            self._click_calibration_action.setText(
                self._click_calibration_action_text()
            )
        if self._click_calibration_dialog is not None:
            self._click_calibration_dialog.set_objectives(
                self.settings_manager.objectives_configuration()
            )

    def _click_calibration_action_text(self) -> str:
        return "Click-to-Move Calibration"

    def _on_objective_calibration_updated(
        self,
        objective_name: str,
        pixels_to_mm: object,
    ) -> None:
        name = normalize_objective_name(objective_name)
        if not name:
            return
        settings = self.settings_manager.settings.clone()
        profile = settings.objectives.objectives.get(name)
        if profile is None:
            profile = default_objective(name)
        matrix: list[list[float]] = []
        if isinstance(pixels_to_mm, (list, tuple)):
            try:
                matrix = [
                    [float(pixels_to_mm[0][0]), float(pixels_to_mm[0][1])],
                    [float(pixels_to_mm[1][0]), float(pixels_to_mm[1][1])],
                ]
            except (TypeError, ValueError, IndexError):
                matrix = []
        profile.pixels_to_mm = matrix
        profile.xy_calibration_configured = bool(matrix)
        settings.objectives.objectives[name] = profile
        self.settings_manager.replace(settings)
        self.settings_manager.save()
        self._refresh_click_calibration_ui()

    def _on_objective_mismatch_detected(
        self,
        suggested_name: str,
        message: str,
    ) -> None:
        name = normalize_objective_name(suggested_name)
        objective_settings = self.settings_manager.objectives_configuration()
        if name in objective_settings.objectives:
            self._set_active_objective(name, apply_motion=False, allow_busy=True)
        if message:
            self._show_status(message, 7000)

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
            self.stage_controller.reset_calibration(
                "Click-to-move calibration cleared. Click in the microscope view to recalibrate the active objective."
            )
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
            registration_stage_xy = self._camera_stage_xy_from_raw_stage_xy(captured)
            self._pending_alignment_preparation = None
            self._design_session.set_source_stage_mark(slot, registration_stage_xy)
            self._refresh_design_panel()
            self._refresh_design_position()
            pair_count = self._design_session.source_pair_count()
            if pair_count < 2:
                self._set_alignment_panel_expanded()
                self._show_status(
                    f"Design alignment: point {slot + 1} captured from {label} "
                    f"at X={registration_stage_xy[0]:.3f}, "
                    f"Y={registration_stage_xy[1]:.3f}. "
                    "Capture the other point next.",
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

    def _position_with_axis_value(
        self,
        axis_name: str,
        raw_value: float,
        *,
        base_position: object | None = None,
    ) -> tuple[float, ...] | None:
        axis = axis_name.strip().upper()
        try:
            axis_index = self.STAGE_AXIS_NAMES.index(axis)
        except ValueError:
            return None
        position = base_position
        if not isinstance(position, (tuple, list)) or len(position) <= axis_index:
            position = self._seed_motion_prediction_position()
        values: list[float] = []
        if isinstance(position, (tuple, list)):
            try:
                values = [float(value) for value in position]
            except (TypeError, ValueError):
                values = []
        if len(values) <= axis_index:
            return None
        values[axis_index] = float(raw_value)
        return tuple(values)

    def _position_with_axis_values(
        self,
        raw_targets: dict[str, float],
        *,
        base_position: object | None = None,
    ) -> tuple[float, ...] | None:
        position = base_position
        if not isinstance(position, (tuple, list)):
            position = self._seed_motion_prediction_position()
        values: list[float] = []
        if isinstance(position, (tuple, list)):
            try:
                values = [float(value) for value in position]
            except (TypeError, ValueError):
                values = []
        for axis_name, raw_value in raw_targets.items():
            axis = axis_name.strip().upper()
            try:
                axis_index = self.STAGE_AXIS_NAMES.index(axis)
            except ValueError:
                return None
            if len(values) <= axis_index:
                return None
            values[axis_index] = float(raw_value)
        return tuple(values)

    def _seed_motion_prediction_position(self) -> tuple[float, ...] | None:
        if self._coordinate_move_stage_position is not None:
            return tuple(float(value) for value in self._coordinate_move_stage_position)
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
            stage_xy
            if stage_xy is not None and self._can_display_design_position()
            else None
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
        if self._coordinate_move_stage_position is not None:
            stage_xy = self._stage_xy_from_position(self._coordinate_move_stage_position)
            if stage_xy is not None:
                return stage_xy
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

    def _preferred_design_display_stage_xy(self) -> tuple[float, float] | None:
        stage_xy = self._preferred_design_stage_xy()
        if stage_xy is not None:
            return stage_xy
        if not self._can_display_design_position():
            return None
        latest = self.stage_controller.latest_stage_position()
        if latest is not None and len(latest) >= 2:
            try:
                return (float(latest[0]), float(latest[1]))
            except (TypeError, ValueError):
                return None
        return self._current_design_stage_xy

    def _can_display_design_position(self) -> bool:
        registration = self._design_session.registration
        return bool(
            self._design_session.document is not None
            and registration is not None
            and registration.valid
        )

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
        camera_xy = self._camera_stage_xy_from_raw_stage_xy(fluidnc_xy)
        origin = registration.source_stage_marks[0]
        return (
            float(camera_xy[0]) - float(origin[0]),
            float(camera_xy[1]) - float(origin[1]),
        )

    def _resolve_design_coordinates(
        self, fluidnc_xy: tuple[float, float]
    ) -> tuple[float, float] | None:
        try:
            return self._design_xy_from_raw_stage_xy(fluidnc_xy)
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
        axis_name = axis.upper()
        self._set_stage_motion_axes({axis_name})
        if axis_name == "B":
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
            self._clear_stage_motion_axes()
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
            return
        self._set_stage_motion_axes(
            {
                axis
                for axis, distance in axis_components.items()
                if abs(distance) > 1e-9
            }
        )
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
                self._design_xy_from_raw_stage_xy(self._manual_jog_stage_xy)
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
                self._design_xy_from_raw_stage_xy(self._manual_jog_stage_xy)
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

    def _on_manual_axis_move_requested(
        self,
        axis: str,
        value_mm: float,
        mode: str,
        _feedrate_mm_min: float,
    ) -> None:
        """Route manual +/- axis controls through the coordinate move path."""

        axis = axis.strip().upper()
        if axis not in self.STAGE_AXIS_NAMES:
            return
        mode = mode.strip().upper()
        if mode not in {"G90", "G91"}:
            self._show_status(f"Unsupported manual move mode: {mode}.", 3000)
            return
        current_display = self._stage_axis_display_values.get(axis)
        if current_display is None:
            self._show_status(f"{axis} coordinate is unavailable.", 3000)
            return
        display_target = (
            float(value_mm)
            if mode == "G90"
            else float(current_display) + float(value_mm)
        )
        raw_target = self._raw_target_from_display_value(axis, display_target)
        if raw_target is None:
            self._show_status(f"{axis} coordinate is unavailable.", 3000)
            return
        if self._coordinate_move_axis is not None:
            self._show_status("Stage is busy. Ignoring manual axis move.", 3000)
            return
        if self.stage_controller.is_busy():
            self._show_status("Stage is busy. Ignoring manual axis move.", 3000)
            return
        self._start_coordinate_axis_move(axis, raw_target, display_target)

    def _schedule_linear_feedrate_save(self, feedrate_mm_min: float) -> None:
        try:
            self._pending_linear_feedrate_default = max(0.1, float(feedrate_mm_min))
        except (TypeError, ValueError):
            return
        self._linear_feedrate_save_timer.start()

    def _on_linear_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._schedule_linear_feedrate_save(feedrate_mm_min)
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

    def _on_needle_feedrate_changed(self, feedrate_mm_min: float) -> None:
        try:
            feedrate = max(0.1, float(feedrate_mm_min))
        except (TypeError, ValueError):
            return
        settings = self.settings_manager.settings.clone()
        if abs(settings.needle_calibration.feedrate_mm_min - feedrate) > 1e-9:
            settings.needle_calibration.feedrate_mm_min = feedrate
            self.settings_manager.replace(settings)
            self.settings_manager.save()
        self.stage_controller.queue_active_needles_feedrate(feedrate)

    def _save_pending_linear_feedrate_default(self) -> None:
        feedrate = self._pending_linear_feedrate_default
        self._pending_linear_feedrate_default = None
        if feedrate is None:
            return
        settings = self.settings_manager.settings.clone()
        if abs(settings.feedrates.linear.default - feedrate) <= 1e-9:
            return
        settings.feedrates.linear.default = feedrate
        self.settings_manager.replace(settings)
        self.settings_manager.save()

    def _current_linear_feedrate(self) -> float:
        if self.joystick_panel is not None:
            return self.joystick_panel.current_linear_feedrate()
        return float(self.settings_manager.feedrate_configuration().linear.default)

    def _current_needle_feedrate(self) -> float:
        if self.joystick_panel is not None:
            return self.joystick_panel.current_needle_feedrate()
        return float(
            self.settings_manager.needle_calibration_configuration().feedrate_mm_min
        )

    def _advance_motion_prediction(self) -> None:
        if self._manual_jog_prediction_active():
            self._advance_manual_jog_prediction()
            return
        if self._coordinate_move_started_at is not None:
            self._advance_coordinate_move_prediction()
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
                    self._design_xy_from_raw_stage_xy(self._manual_jog_stage_xy)
                    if self._manual_jog_stage_xy is not None
                    else None
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

    def _advance_coordinate_move_prediction(self) -> None:
        if (
            self._coordinate_move_origin_position is None
            or self._coordinate_move_target_position is None
            or self._coordinate_move_started_at is None
            or self._coordinate_move_ends_at is None
        ):
            self._clear_coordinate_move_tracking(clear_pending=False, reset_override=True)
            return
        now = time.monotonic()
        self._coordinate_move_stage_position = interpolate_position(
            self._coordinate_move_origin_position,
            self._coordinate_move_target_position,
            self._coordinate_move_started_at,
            self._coordinate_move_ends_at,
            now,
        )
        self._publish_stage_position_estimate(self._coordinate_move_stage_position)

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
        progress = motion_progress(
            self._planned_move_started_at,
            self._planned_move_ends_at,
            now,
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
        self._pending_planned_move_target_xy = None
        self._pending_planned_move_source_label = None
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
        feedrate_mm_min: float | None = None,
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
        feedrate = (
            float(self.stage_controller.DEFAULT_FEEDRATE)
            if feedrate_mm_min is None
            else max(0.1, float(feedrate_mm_min))
        )
        speed_mm_per_s = feedrate / 60.0
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
        self._set_stage_motion_axes({"X", "Y"})
        logger.debug(
            "MOTION PREDICTION planned_move_start source=%s origin=%s target=%s distance=%.4f duration=%.4f feedrate=%.3f",
            source_label,
            self._format_optional_point(origin_stage_xy),
            self._format_optional_point(target_stage_xy),
            distance_mm,
            duration_s,
            feedrate,
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
        message_lower = message.lower() if message else ""
        if self._pending_planned_move_target_xy is not None:
            logger.debug(
                "MOTION PREDICTION planned_move_pending_cleared success=%s message=%s",
                success,
                message,
            )
            self._pending_planned_move_target_xy = None
            self._pending_planned_move_source_label = None
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
            if self._pending_click_to_move is None:
                self.view.finish_target_motion_to_center()
                self.view.clear_target_cross()
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
        elif self._pending_click_to_move is None:
            self.view.clear_target_cross()
        if (
            not success
            or "skipped" in message_lower
            or "already" in message_lower
            or "unchanged" in message_lower
        ):
            self._clear_coordinate_move_tracking(
                clear_pending=not success,
                reset_override=True,
            )
            self._clear_stage_motion_axes()
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
        self._start_design_document_load(
            design_path,
            restore_state=None,
            show_window=True,
        )

    def _start_design_document_load(
        self,
        design_path: str,
        *,
        restore_state: dict[str, object] | None,
        show_window: bool,
    ) -> None:
        self._design_load_generation += 1
        generation = self._design_load_generation
        path_text = str(design_path)
        if restore_state is not None:
            self._design_load_restore_states[generation] = dict(restore_state)
        self._design_load_show_window[generation] = bool(show_window)
        if show_window:
            self._toggle_design_layout_window(True)
        if self.design_layout_window is not None and show_window:
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
        restore_state = self._design_load_restore_states.pop(generation, None)
        show_window = self._design_load_show_window.pop(generation, True)
        if error is not None:
            message = str(error)
            self._show_status(message, 6000)
            if self.design_layout_window is not None and show_window:
                self.design_layout_window.set_status_message(message)
            elif self.design_navigator_panel:
                self.design_navigator_panel.set_status_message(message)
            if restore_state is not None:
                self._save_controller_state_without_design()
            return
        if not isinstance(document, DesignDocument):
            message = "Loaded design has an unexpected type."
            self._show_status(message, 6000)
            if self.design_layout_window is not None and show_window:
                self.design_layout_window.set_status_message(message)
            elif self.design_navigator_panel:
                self.design_navigator_panel.set_status_message(message)
            if restore_state is not None:
                self._save_controller_state_without_design()
            return
        try:
            if restore_state is not None:
                document = self._document_with_persisted_design_view(
                    document,
                    restore_state,
                )
            self._reset_manual_alignment(cancel_pick=True)
            if restore_state is None:
                self._design_session.load_document(document)
            else:
                self._design_session.restore_persisted_state(document, restore_state)
            self._pending_alignment_preparation = None
            self._last_selected_design_point = None
            self._set_design_snap_enabled(True)
            self.settings_manager.set_design_last_directory(document.path.parent)
            if self.design_navigator_panel is not None:
                self.design_navigator_panel.set_design_dialog_directory(
                    document.path.parent
                )
            if self.design_layout_window is not None and show_window:
                self.design_layout_window.set_status_message("Rendering design...")
                QApplication.processEvents()
            self._refresh_design_panel()
            self._refresh_design_position()
            if show_window:
                self._toggle_design_layout_window(True)
            self._persist_controller_state_if_available()
            self._show_status(
                f"Loaded design '{document.path.name}' ({document.top_cell_name}).",
                5000,
            )
        except DesignModelError as exc:
            self._show_status(str(exc), 6000)
            if restore_state is not None:
                self._save_controller_state_without_design()

    def _document_with_persisted_design_view(
        self,
        document: DesignDocument,
        state: dict[str, object],
    ) -> DesignDocument:
        top_cell_name = str(state.get("top_cell_name") or "").strip()
        if top_cell_name and top_cell_name != document.top_cell_name:
            document = document.with_top_cell(top_cell_name)
        try:
            rotation_quarter_turns = int(state.get("rotation_quarter_turns", 0))
        except (TypeError, ValueError):
            rotation_quarter_turns = 0
        if rotation_quarter_turns:
            document = document.with_rotation_delta(rotation_quarter_turns)
        visible_layers = self._parse_persisted_visible_layers(
            state.get("visible_layers")
        )
        if visible_layers:
            document = document.with_visible_layers(visible_layers)
        return document

    @staticmethod
    def _parse_persisted_visible_layers(value: object) -> set[tuple[int, int]]:
        layers: set[tuple[int, int]] = set()
        if not isinstance(value, list):
            return layers
        for item in value:
            if not isinstance(item, (list, tuple)) or len(item) != 2:
                continue
            try:
                layers.add((int(item[0]), int(item[1])))
            except (TypeError, ValueError):
                continue
        return layers

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

    def _rotate_design_document(self, quarter_turn_delta: int) -> None:
        document = self._design_session.document
        if document is None:
            self._show_status("Load a design before rotating it.", 4000)
            return
        registration = self._design_session.registration
        if registration is not None and registration.valid:
            self._show_status(
                "Clear design registration before rotating the design.",
                5000,
            )
            return
        if self._pending_alignment_preparation is not None:
            self._show_status(
                "Wait for chip rotation to finish before rotating the design.",
                5000,
            )
            return
        route_measurement_thread = getattr(self, "_route_measurement_thread", None)
        if (
            route_measurement_thread is not None
            and route_measurement_thread.is_alive()
        ):
            self._show_status("Stop route measurement before rotating the design.", 5000)
            return
        _ = quarter_turn_delta
        delta = 1
        previous_selected_point = self._last_selected_design_point
        try:
            rotated_document = self._design_session.rotate_document(delta)
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        if previous_selected_point is not None:
            self._last_selected_design_point = document.rotate_point(
                previous_selected_point,
                delta,
            )
        self._pending_alignment_preparation = None
        self._refresh_design_panel()
        self._refresh_design_position()
        self._show_status(
            f"Rotated design counterclockwise: "
            f"{rotated_document.rotation_quarter_turns * 90} deg.",
            4000,
        )

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

    def _create_measurement_route(self) -> None:
        try:
            route = self._design_session.create_route()
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._last_selected_design_point = None
        self._refresh_design_panel()
        self._show_status(f"Created route '{route.name}'.", 4000)

    def _load_measurement_route(self, route_path: str) -> None:
        document = self._design_session.document
        if document is None:
            self._show_status("Load a design before loading a route.", 5000)
            return
        try:
            route = MeasurementRoute.load(route_path)
            self._design_session.set_route(route)
        except DesignModelError as exc:
            self._show_status(str(exc), 7000)
            return
        current_point = self._design_session.current_route_point()
        self._last_selected_design_point = (
            current_point.camera_center if current_point is not None else None
        )
        self._refresh_design_panel()
        self._show_status(
            f"Loaded route '{route.name}' with {len(route.points)} points.",
            5000,
        )

    def _save_measurement_route(self) -> None:
        route = self._design_session.route
        if route is None:
            self._show_status("No route is loaded.", 4000)
            return
        try:
            path = route.save()
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._refresh_design_panel()
        self._show_status(f"Saved route '{path.name}'.", 4000)

    def _save_measurement_route_as(self, route_path: str) -> None:
        route = self._design_session.route
        if route is None:
            self._show_status("No route is loaded.", 4000)
            return
        try:
            path = route.save(route_path)
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._refresh_design_panel()
        self._show_status(f"Saved route '{path.name}'.", 4000)

    def _add_design_route_point(self, x_value: float, y_value: float) -> None:
        try:
            point = self._design_session.add_route_point((float(x_value), float(y_value)))
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._last_selected_design_point = point.camera_center
        self._refresh_design_panel()
        self._show_status(
            f"Added route point {point.label} at X={point.camera_center[0]:.3f}, "
            f"Y={point.camera_center[1]:.3f}.",
            3000,
        )

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
    ) -> None:
        if self._design_session.document is None:
            self._show_status("Load a design before adding route points.", 5000)
            return
        route = self._design_session.route
        if route is None:
            try:
                route = self._design_session.create_route()
            except DesignModelError as exc:
                self._show_status(str(exc), 5000)
                return
        try:
            added = route.add_grid_points(
                (float(origin_x), float(origin_y)),
                (float(step_x_dx), float(step_x_dy)),
                int(count_x),
                (float(step_y_dx), float(step_y_dy)),
                int(count_y),
                serpentine=bool(serpentine),
                clear_existing=bool(replace_existing),
            )
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        if added:
            self._design_session.selected_route_point_index = len(route.points) - 1
            self._last_selected_design_point = added[-1].camera_center
        else:
            self._design_session.selected_route_point_index = -1
            self._last_selected_design_point = None
        self._refresh_design_panel()
        mode = "Replaced route with" if replace_existing else "Added"
        self._show_status(
            f"{mode} {len(added)} array route points "
            f"from X={float(origin_x):.3f}, Y={float(origin_y):.3f}.",
            4000,
        )

    def _remove_selected_route_point(self) -> None:
        point = self._design_session.remove_selected_route_point()
        if point is None:
            self._show_status("No route point is selected.", 3000)
            return
        current_point = self._design_session.current_route_point()
        self._last_selected_design_point = (
            current_point.camera_center if current_point is not None else None
        )
        self._refresh_design_panel()
        self._show_status(f"Removed route point {point.label}.", 3000)

    def _clear_measurement_route_points(self) -> None:
        route = self._design_session.route
        if route is None:
            self._show_status("No route is loaded.", 3000)
            return
        route.clear_points()
        self._design_session.selected_route_point_index = -1
        self._last_selected_design_point = None
        self._refresh_design_panel()
        self._show_status("Cleared route points.", 3000)

    def _start_route_measurement(
        self,
        csv_path: str,
        n_measurements: int = 1,
    ) -> None:
        thread = self._route_measurement_thread
        if thread is not None and thread.is_alive():
            self._show_status("Route measurement is already running.", 4000)
            return
        if self.serial_connection is None or not self.serial_connection.is_open:
            self._show_status("Connect the stage controller before running a route.", 5000)
            return
        if not self.lcr_controller.is_connected():
            self._show_status("Connect the LCR meter before running a route.", 5000)
            return
        route = self._design_session.route
        if route is None or not route.points:
            self._show_status("Create or load a probe route before running it.", 5000)
            return
        registration = self._design_session.registration
        if registration is None or not registration.valid:
            self._show_status(
                "Design registration is required before running a route.",
                6000,
            )
            return
        try:
            points = self._route_measurement_points(route)
        except DesignModelError as exc:
            self._show_status(str(exc), 6000)
            return
        if not points:
            self._show_status("Route has no enabled points.", 5000)
            return
        runner = RouteMeasurementRunner(
            points=points,
            csv_path=csv_path,
            stage_controller=self.stage_controller,
            lcr_controller=self.lcr_controller,
            needle_feedrate=self._current_needle_feedrate(),
            measurement_count=n_measurements,
            confirm_each_point=True,
            status_callback=self.route_measurement_status.emit,
            record_callback=self.route_measurement_recorded.emit,
        )
        self._route_measurement_runner = runner
        self._route_measurement_thread = threading.Thread(
            target=self._run_route_measurement,
            args=(runner,),
            name="RouteMeasurement",
            daemon=True,
        )
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_running(True)
            self.design_navigator_panel.set_route_measurement_waiting(False)
            self.design_navigator_panel.set_route_measurement_status(
                f"Route measurement starting: {len(points)} points."
            )
        self._show_status(f"Route measurement starting: {len(points)} points.")
        self._route_measurement_thread.start()

    def _route_measurement_points(
        self,
        route: MeasurementRoute,
    ) -> list[RouteMeasurementPoint]:
        points: list[RouteMeasurementPoint] = []
        for route_index, route_point in enumerate(route.points, start=1):
            if not route_point.enabled:
                continue
            stage_xy = self._raw_stage_xy_from_design_xy(route_point.camera_center)
            if stage_xy is None:
                raise DesignModelError(
                    "Design registration is required before running a route."
                )
            hits = route.needle_hits_for_point(route_point)
            needle_1_design = (
                hits[0][1] if len(hits) > 0 else route_point.camera_center
            )
            needle_2_design = (
                hits[1][1] if len(hits) > 1 else route_point.camera_center
            )
            points.append(
                RouteMeasurementPoint(
                    index=route_index,
                    point_id=route_point.id,
                    label=route_point.label,
                    design_center=(
                        float(route_point.camera_center[0]),
                        float(route_point.camera_center[1]),
                    ),
                    stage_xy=(float(stage_xy[0]), float(stage_xy[1])),
                    needle_1_design=(
                        float(needle_1_design[0]),
                        float(needle_1_design[1]),
                    ),
                    needle_2_design=(
                        float(needle_2_design[0]),
                        float(needle_2_design[1]),
                    ),
                )
            )
        return points

    def _run_route_measurement(self, runner: RouteMeasurementRunner) -> None:
        success, message = runner.run()
        self.route_measurement_finished.emit(success, message, str(runner.csv_path))

    def _request_stop_route_measurement(self) -> None:
        runner = self._route_measurement_runner
        if runner is None:
            self._show_status("No route measurement is running.", 3000)
            return
        runner.stop()
        self._show_status("Route measurement stop requested.")
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_waiting(False)
            self.design_navigator_panel.set_route_measurement_status(
                "Route measurement will stop after the current action."
            )

    def _submit_route_measurement_confirmation(self, action: str) -> None:
        runner = self._route_measurement_runner
        if runner is None:
            self._show_status("No route measurement is waiting.", 3000)
            return
        runner.submit_confirmation(action)
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_waiting(False)
        action_label = "remeasure" if action == "remeasure" else "next"
        self._show_status(f"Route measurement: {action_label}.")

    def _on_route_measurement_status(self, message: str) -> None:
        self._show_status(message)
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_status(message)

    def _on_route_measurement_recorded(
        self,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
    ) -> None:
        message = (
            f"Measured route point {position}/{total}: "
            f"{record.resistance_ohm:g} ohm."
        )
        self._show_status(message)
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_waiting(True)
            self.design_navigator_panel.set_route_measurement_status(message)

    def _on_route_measurement_finished(
        self,
        success: bool,
        message: str,
        csv_path: str,
    ) -> None:
        thread = self._route_measurement_thread
        if thread is not None and not thread.is_alive():
            thread.join(timeout=0.1)
        self._route_measurement_thread = None
        self._route_measurement_runner = None
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_running(False)
            self.design_navigator_panel.set_route_measurement_waiting(False)
            self.design_navigator_panel.set_route_measurement_status(message)
        if success:
            self._show_status(f"{message} CSV: {csv_path}", 8000)
        else:
            self._show_status(message, 8000)

    def _select_route_point(self, index: int) -> None:
        point = self._design_session.select_route_point(index)
        self._last_selected_design_point = point.camera_center if point is not None else None
        self._refresh_design_panel()

    def _set_route_needle_offsets(
        self,
        needle_1_dx: float,
        needle_1_dy: float,
        needle_2_dx: float,
        needle_2_dy: float,
    ) -> None:
        route = self._design_session.route
        if route is None:
            return
        route.set_needle_offsets(
            needle_1_dx,
            needle_1_dy,
            needle_2_dx,
            needle_2_dy,
        )
        self._refresh_design_panel()

    def _set_route_edit_enabled(self, enabled: bool) -> None:
        if self.design_layout_window is not None:
            self.design_layout_window.set_route_edit_enabled(enabled)

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
        raw_stage_xy = (float(stage_position[0]), float(stage_position[1]))
        stage_xy = self._camera_stage_xy_from_raw_stage_xy(raw_stage_xy)
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
        stage_xy = self._raw_stage_xy_from_design_xy(target.design_center)
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
        stage_xy = self._raw_stage_xy_from_design_xy(design_xy)
        if stage_xy is None:
            self._show_status(
                "Design click-to-move requires completed registration.",
                5000,
            )
            return False
        self._last_selected_design_point = design_xy
        self._refresh_design_panel()
        self._clear_planned_move_prediction(clear_wait_state=True)
        self._pending_planned_move_target_xy = (float(stage_xy[0]), float(stage_xy[1]))
        self._pending_planned_move_source_label = source_label
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
        registration_valid = (
            self._design_session.registration is not None
            and self._design_session.registration.valid
        )
        if panel is not None:
            panel.set_document(self._design_session.document)
            panel.set_design_registration_active(registration_valid)
            panel.set_script_path(self._design_session.script_path)
            panel.set_targets(
                self._design_session.targets,
                selected_target_id=selected_target_id,
            )
            panel.set_route(
                self._design_session.route,
                selected_route_point_index=self._design_session.selected_route_point_index,
            )
            panel.set_route_measurement_running(
                self._route_measurement_thread is not None
                and self._route_measurement_thread.is_alive()
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
            self.design_layout_window.set_probe_route(
                self._design_session.route,
                selected_route_point_index=self._design_session.selected_route_point_index,
            )
            self.design_layout_window.set_navigation_enabled(registration_valid)
            self.design_layout_window.set_registration_marks(
                self._design_session.source_design_marks,
                self._design_session.check_design_marks,
            )
            self.design_layout_window.set_stage_registration_marks(
                self._design_session.source_stage_marks
            )
        self._refresh_manual_alignment_ui()
        self._update_design_position(self._current_design_stage_xy)
        self._persist_controller_state_if_available()

    def _on_stage_position_changed(self, position: object) -> None:
        if not isinstance(position, tuple) or len(position) < 2:
            self._update_stage_position_display(position)
            return
        logger.debug("TIMING stage_position_changed position=%s", position)
        current_position = self._coerce_position_tuple(position)
        if current_position is not None:
            self._maybe_restore_persisted_design(current_position)
        latest_state = (self.stage_controller.latest_stage_state() or "").lower()
        xy_homed = self.stage_controller.axes_are_homed({"X", "Y"})
        xyz_homed = self.stage_controller.axes_are_homed({"X", "Y", "Z"})
        if self.contact_calibration_window is not None:
            if len(position) >= 3 and xyz_homed:
                self.contact_calibration_window.set_current_stage_position(
                    (float(position[0]), float(position[1]), float(position[2]))
                )
            else:
                self.contact_calibration_window.set_current_stage_position(None)
        center_xy = (float(position[0]), float(position[1]))
        if not xy_homed:
            if not self._manual_jog_prediction_available():
                self._update_stage_position_display(position)
                self._manual_jog_stage_position = None
                self._manual_jog_stage_xy = None
                self._planned_move_stage_xy = None
                self._update_coordinate_display(center_xy=None)
                self._update_design_position(
                    center_xy if self._can_display_design_position() else None
                )
                if latest_state == "idle":
                    self._finish_coordinate_move_if_idle(position)
                    self._clear_stage_motion_axes()
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
        smooth_predicted_status = False
        if self._manual_jog_prediction_available():
            predicted_position = self._manual_jog_stage_position
            smooth_predicted_status = True
        elif self._coordinate_move_stage_position is not None:
            predicted_position = self._coordinate_move_stage_position
        elif (
            self._planned_move_started_at is not None
            or self._planned_move_waiting_for_fresh_status
        ):
            if self._planned_move_stage_xy is not None:
                predicted_position = self._position_with_stage_xy(
                    self._planned_move_stage_xy,
                    base_position=position,
                )
                smooth_predicted_status = True
        predicted_stage_xy = self._stage_xy_from_position(predicted_position)
        planned_move_active = (
            self._planned_move_started_at is not None
            and predicted_stage_xy is not None
        )
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
            if smooth_predicted_status:
                if planned_move_active:
                    center_xy = predicted_stage_xy
                else:
                    center_xy = self._smooth_manual_jog_actual_position(
                        predicted_stage_xy, center_xy
                    )
        display_position = self._position_with_stage_xy(
            center_xy,
            base_position=position,
        )
        self._manual_jog_stage_position = display_position
        self._manual_jog_stage_xy = center_xy
        if not planned_move_active:
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
        if latest_state == "idle":
            self._finish_coordinate_move_if_idle(display_position)
            self._clear_stage_motion_axes()

    def _update_stage_position_display(self, position: object | None) -> None:
        if not isinstance(position, tuple) or len(position) < 2:
            self._stage_unhomed_display_origins.clear()
            self._stage_axis_raw_values.clear()
            self._stage_axis_display_values.clear()
            self._stage_axis_homed.clear()
            self._stage_axis_base_styles.clear()
            self._pending_stage_axis_targets.clear()
            self._clear_stage_motion_axes()
            self._set_stage_position_fields_available(False)
            return
        homed_axes = self.stage_controller.homed_axes()
        self._stage_axis_homed = set(homed_axes)
        updated_axes: set[str] = set()
        self._updating_stage_position_fields = True
        for axis_name, axis_value in zip(self.STAGE_AXIS_NAMES, position):
            field = self._stage_axis_fields.get(axis_name)
            if field is None:
                continue
            try:
                raw_value = float(axis_value)
            except (TypeError, ValueError):
                continue
            updated_axes.add(axis_name)
            self._stage_axis_raw_values[axis_name] = raw_value
            axis_display_value = self._display_axis_value_from_raw(
                axis_name,
                raw_value,
            )
            display_value = axis_display_value
            if axis_name in homed_axes:
                background = "#1565c0"
                foreground = "#f5f5f5"
            else:
                self._stage_unhomed_display_origins.setdefault(axis_name, raw_value)
                background = "#f0b429"
                foreground = "#1f1f1f"
            if axis_name in self._stage_limit_axes:
                background = "#c62828"
                foreground = "#ffffff"
            self._stage_axis_base_styles[axis_name] = (background, foreground)
            self._stage_axis_display_values[axis_name] = display_value
            pending_target = self._pending_stage_axis_targets.get(axis_name)
            visible_value = (
                pending_target[1] if pending_target is not None else display_value
            )
            field.blockSignals(True)
            field.setEnabled(True)
            if not field.hasFocus():
                field.setText(self._format_stage_axis_value(visible_value))
                field.setModified(False)
            field.setToolTip(
                f"{axis_name} coordinate. Enter targets and press Apply. "
                f"Move feedrate: {self._current_linear_feedrate():.1f} mm/min."
            )
            self._apply_stage_axis_field_style(axis_name, field)
            field.blockSignals(False)
        for axis_name, field in self._stage_axis_fields.items():
            if axis_name in updated_axes:
                continue
            self._stage_axis_base_styles.pop(axis_name, None)
            self._pending_stage_axis_targets.pop(axis_name, None)
            field.blockSignals(True)
            field.clear()
            field.setEnabled(False)
            field.setModified(False)
            self._style_stage_axis_field(field, "#e6e6e6", "#666666")
            field.blockSignals(False)
        self._updating_stage_position_fields = False
        if not updated_axes:
            self._set_stage_position_fields_available(False)
            return
        self._update_stage_coordinate_apply_state()

    def _on_stage_axis_editing_finished(self, axis_name: str) -> bool | None:
        if self._updating_stage_position_fields:
            return None
        axis = axis_name.strip().upper()
        commit_from_return = axis in self._stage_axis_return_commits
        self._stage_axis_return_commits.discard(axis)
        field = self._stage_axis_fields.get(axis)
        if field is None or not field.isEnabled() or not field.isModified():
            return None
        text = field.text().strip().replace(",", ".")
        try:
            display_target = float(text)
        except (TypeError, ValueError):
            self._pending_stage_axis_targets.pop(axis, None)
            self._reset_stage_axis_field(axis)
            self._show_status(f"Invalid {axis} target coordinate.", 3000)
            return False
        input_mode = self._selected_stage_coordinate_input_mode()
        raw_target, resolved_display_target = self._resolve_stage_axis_target(
            axis,
            display_target,
            input_mode,
        )
        if raw_target is None:
            self._pending_stage_axis_targets.pop(axis, None)
            self._reset_stage_axis_field(axis)
            self._show_status(f"{axis} coordinate is unavailable.", 3000)
            return False
        limit_error = self._stage_axis_target_limit_error(
            axis,
            resolved_display_target,
        )
        if limit_error is not None:
            self._pending_stage_axis_targets.pop(axis, None)
            self._reset_stage_axis_field(axis)
            self._show_status(limit_error, 4000)
            return False
        field.blockSignals(True)
        field.setText(self._format_stage_axis_value(display_target))
        field.setModified(False)
        if commit_from_return:
            field.clearFocus()
        field.blockSignals(False)
        if commit_from_return:
            self.view.setFocus(Qt.OtherFocusReason)
        self._set_pending_stage_axis_target(
            axis,
            raw_target,
            resolved_display_target,
        )
        return True

    def _apply_pending_stage_coordinate_targets(self) -> None:
        had_error = False
        for axis in self.STAGE_AXIS_NAMES:
            field = self._stage_axis_fields.get(axis)
            if field is not None and field.isEnabled() and field.isModified():
                if self._on_stage_axis_editing_finished(axis) is False:
                    had_error = True
        if had_error:
            self._update_stage_coordinate_apply_state()
            return
        if not self._pending_stage_axis_targets:
            self._show_status("No coordinate changes to apply.", 2000)
            return
        if self._coordinate_move_axis is not None or self.stage_controller.is_busy():
            self._show_status("Stage is busy. Ignoring coordinate targets.", 3000)
            self._update_stage_coordinate_apply_state()
            return
        targets = dict(self._pending_stage_axis_targets)
        self.view.setFocus(Qt.OtherFocusReason)
        self._start_coordinate_targets_move(
            targets,
            feedrate_mm_min=self._current_linear_feedrate(),
            source_label="coordinate fields",
        )
        self._update_stage_coordinate_apply_state()

    def _set_pending_stage_axis_target(
        self, axis_name: str, raw_target: float, display_target: float
    ) -> None:
        axis = axis_name.strip().upper()
        if axis not in self.STAGE_AXIS_NAMES:
            return
        self._pending_stage_axis_targets[axis] = (
            float(raw_target),
            float(display_target),
        )
        field = self._stage_axis_fields.get(axis)
        if field is not None and not field.hasFocus():
            field.blockSignals(True)
            field.setText(self._format_stage_axis_value(display_target))
            field.setModified(False)
            field.blockSignals(False)
        self._refresh_stage_axis_styles()
        self._update_stage_coordinate_apply_state()

    def _start_coordinate_axis_move(
        self, axis: str, raw_target: float, display_target: float
    ) -> bool:
        return self._start_coordinate_targets_move(
            {axis: (raw_target, display_target)},
            feedrate_mm_min=self._current_linear_feedrate(),
            source_label="coordinate field",
        )

    def _start_coordinate_targets_move(
        self,
        targets: dict[str, tuple[float, float]],
        *,
        feedrate_mm_min: float,
        source_label: str,
    ) -> bool:
        ordered_targets = {
            axis: targets[axis]
            for axis in self.STAGE_AXIS_NAMES
            if axis in targets
        }
        if not ordered_targets:
            return False
        feedrate = max(0.1, float(feedrate_mm_min))
        origin_position = self._seed_motion_prediction_position()
        if origin_position is None:
            origin_position = self.stage_controller.latest_stage_position()
        if not isinstance(origin_position, (tuple, list)):
            self._show_status("Stage coordinates are unavailable.", 3000)
            return False
        raw_targets = {
            axis: float(values[0])
            for axis, values in ordered_targets.items()
        }
        display_targets = {
            axis: float(values[1])
            for axis, values in ordered_targets.items()
        }
        for axis, display_target in display_targets.items():
            limit_error = self._stage_axis_target_limit_error(axis, display_target)
            if limit_error is not None:
                self._show_status(limit_error, 4000)
                return False
        target_position = self._position_with_axis_values(
            raw_targets,
            base_position=origin_position,
        )
        if target_position is None:
            self._show_status("Stage coordinates are unavailable.", 3000)
            return False
        for axis in ordered_targets:
            self._pending_stage_axis_targets.pop(axis, None)
        axes = list(ordered_targets)
        self._coordinate_move_axis = axes[0]
        self._coordinate_move_axes = set(axes)
        if "B" in self._coordinate_move_axes:
            self._invalidate_design_registration(
                "Design registration cleared after B-axis coordinate motion."
            )
        self._coordinate_move_origin_position = tuple(float(v) for v in origin_position)
        self._coordinate_move_stage_position = self._coordinate_move_origin_position
        self._coordinate_move_target_position = target_position
        self._coordinate_move_started_at = time.monotonic()
        self._coordinate_move_programmed_feedrate = feedrate
        self._coordinate_move_effective_feedrate = feedrate
        self._coordinate_move_ends_at = self._coordinate_move_started_at + max(
            self._coordinate_move_duration_s(
                self._coordinate_move_origin_position,
                target_position,
                feedrate,
            ),
            0.05,
        )
        self._set_stage_motion_axes(set(axes))
        self._update_stage_coordinate_apply_state()
        accepted = self.stage_controller.request_absolute_axis_targets_move(
            raw_targets,
            feedrate=feedrate,
        )
        if not accepted:
            self._clear_coordinate_move_tracking(
                clear_pending=False,
                reset_override=True,
            )
            return False
        target_text = ", ".join(
            f"{axis}={display_targets[axis]:.3f}" for axis in axes
        )
        self._show_status(
            f"Moving {target_text} at F{feedrate:.1f} from {source_label}.",
            3000,
        )
        self._publish_stage_position_estimate(self._coordinate_move_stage_position)
        if not self._manual_jog_timer.isActive():
            self._manual_jog_timer.start()
        return True

    def _coordinate_move_duration_s(
        self,
        origin_position: tuple[float, ...],
        target_position: tuple[float, ...],
        feedrate_mm_min: float,
    ) -> float:
        axes = self._coordinate_move_axes
        if not axes and self._coordinate_move_axis is not None:
            axes = {self._coordinate_move_axis}
        squared = 0.0
        for axis in axes:
            try:
                axis_index = self.STAGE_AXIS_NAMES.index(axis)
            except ValueError:
                continue
            if axis_index >= len(origin_position) or axis_index >= len(target_position):
                continue
            delta = float(target_position[axis_index]) - float(origin_position[axis_index])
            squared += delta * delta
        distance = math.sqrt(squared)
        speed_mm_per_s = max(0.1, float(feedrate_mm_min)) / 60.0
        return (distance / speed_mm_per_s) + self.PLANNED_MOVE_DURATION_PADDING_S

    def _apply_coordinate_move_feedrate(self, feedrate_mm_min: float) -> None:
        active_axes = set(self._coordinate_move_axes)
        if not active_axes and self._coordinate_move_axis is not None:
            active_axes.add(self._coordinate_move_axis)
        if (
            not active_axes
            or self._coordinate_move_programmed_feedrate is None
            or self._coordinate_move_target_position is None
        ):
            return
        try:
            requested_feedrate = max(0.1, float(feedrate_mm_min))
        except (TypeError, ValueError):
            return
        target_position = self._coordinate_move_target_position
        raw_targets: dict[str, float] = {}
        for axis in self.STAGE_AXIS_NAMES:
            if axis not in active_axes:
                continue
            try:
                axis_index = self.STAGE_AXIS_NAMES.index(axis)
            except ValueError:
                continue
            if axis_index >= len(target_position):
                continue
            raw_targets[axis] = float(target_position[axis_index])
        if not raw_targets:
            return
        self._advance_coordinate_move_prediction()
        current_position = (
            self._coordinate_move_stage_position
            or self._coordinate_move_origin_position
        )
        if current_position is None:
            return
        try:
            accepted = self.stage_controller.queue_absolute_axis_targets_jog(
                raw_targets,
                feedrate=requested_feedrate,
            )
        except Exception as error:  # pragma: no cover - UI safety guard
            logger.exception("Failed to update coordinate move feedrate.")
            accepted = False
            self._show_status(str(error), 3000)
        if not accepted:
            self._show_status("Unable to update coordinate move feedrate.", 3000)
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
            return
        now = time.monotonic()
        self._coordinate_move_origin_position = tuple(float(v) for v in current_position)
        self._coordinate_move_programmed_feedrate = requested_feedrate
        self._coordinate_move_effective_feedrate = requested_feedrate
        self._coordinate_move_started_at = now
        self._coordinate_move_ends_at = now + max(
            self._coordinate_move_duration_s(
                self._coordinate_move_origin_position,
                self._coordinate_move_target_position,
                requested_feedrate,
            ),
            0.05,
        )
        self._show_status(
            f"Active coordinate move feedrate: F{requested_feedrate:.1f}.",
            1500,
        )

    def _clear_coordinate_move_tracking(
        self, *, clear_pending: bool, reset_override: bool
    ) -> None:
        self._coordinate_move_axis = None
        self._coordinate_move_axes.clear()
        self._coordinate_move_origin_position = None
        self._coordinate_move_stage_position = None
        self._coordinate_move_target_position = None
        self._coordinate_move_started_at = None
        self._coordinate_move_ends_at = None
        self._coordinate_move_programmed_feedrate = None
        self._coordinate_move_effective_feedrate = None
        if clear_pending:
            self._pending_stage_axis_targets.clear()
        if reset_override:
            self.stage_controller.queue_feed_override_reset()
        if self.joystick_panel is not None:
            self.joystick_panel.clear_temporary_linear_feedrate_bounds()
        self._refresh_stage_axis_styles()
        self._update_stage_coordinate_apply_state()

    def _start_next_pending_stage_axis_move(self) -> None:
        if self._coordinate_move_axis is not None or not self._pending_stage_axis_targets:
            return
        if self.stage_controller.is_busy():
            QTimer.singleShot(200, self._start_next_pending_stage_axis_move)
            return
        latest_state = (self.stage_controller.latest_stage_state() or "").lower()
        if latest_state not in {"", "idle"}:
            QTimer.singleShot(200, self._start_next_pending_stage_axis_move)
            return
        axis = next(iter(self._pending_stage_axis_targets.keys()))
        raw_target, display_target = self._pending_stage_axis_targets.pop(axis)
        self._start_coordinate_axis_move(axis, raw_target, display_target)

    def _finish_coordinate_move_if_idle(self, position: object | None) -> None:
        if self._coordinate_move_axis is None:
            return
        latest_state = (self.stage_controller.latest_stage_state() or "").lower()
        if latest_state != "idle":
            return
        if (
            self._coordinate_move_started_at is not None
            and time.monotonic() - self._coordinate_move_started_at
            < self.COORDINATE_MOVE_MIN_IDLE_ACCEPT_S
        ):
            return
        if isinstance(position, tuple):
            self._coordinate_move_stage_position = tuple(float(v) for v in position)
        self._clear_coordinate_move_tracking(clear_pending=False, reset_override=True)
        if self._pending_homing_axes:
            QTimer.singleShot(0, self._start_next_pending_homing_action)

    def _raw_target_from_display_value(
        self, axis_name: str, display_target: float
    ) -> float | None:
        axis = axis_name.strip().upper()
        if axis not in self._stage_axis_raw_values:
            return None
        return self._raw_axis_value_from_display(axis, display_target)

    def _resolve_stage_axis_target(
        self,
        axis_name: str,
        input_value: float,
        input_mode: str,
    ) -> tuple[float | None, float]:
        axis = axis_name.strip().upper()
        if axis not in self.STAGE_AXIS_NAMES:
            return None, float(input_value)
        mode = self._normalize_coordinate_input_mode(input_mode) or "G90"
        if mode == "G91":
            current_display = self._stage_axis_display_values.get(axis)
            if current_display is None:
                return None, float(input_value)
            display_target = float(current_display) + float(input_value)
        else:
            display_target = float(input_value)
        raw_target = self._raw_target_from_display_value(axis, display_target)
        return raw_target, display_target

    def _stage_axis_target_limit_error(
        self,
        axis_name: str,
        display_target: float,
    ) -> str | None:
        axis = axis_name.strip().upper()
        if axis not in self._stage_axis_homed:
            return None
        limits = self.stage_controller.axis_display_limits(axis)
        if limits is None:
            return None
        min_value, max_value = limits
        target = float(display_target)
        if min_value <= target <= max_value:
            return None
        return (
            f"{axis} target {target:+.3f} exceeds software limits "
            f"({min_value:.3f}..{max_value:.3f})."
        )

    def _reset_stage_axis_field(self, axis_name: str) -> None:
        axis = axis_name.strip().upper()
        field = self._stage_axis_fields.get(axis)
        if field is None:
            return
        value = self._stage_axis_display_values.get(axis)
        field.blockSignals(True)
        if value is None:
            field.clear()
        else:
            field.setText(self._format_stage_axis_value(value))
        field.setModified(False)
        field.blockSignals(False)
        self._refresh_stage_axis_styles()
        self._update_stage_coordinate_apply_state()

    def _on_limit_axes_changed(self, axes: object) -> None:
        if isinstance(axes, (set, list, tuple)):
            self._stage_limit_axes = {
                str(axis).strip().upper()
                for axis in axes
                if str(axis).strip().upper() in self.STAGE_AXIS_NAMES
            }
        else:
            self._stage_limit_axes = set()
        if (
            self._manual_jog_prediction_available()
            and self._manual_jog_stage_position is not None
        ):
            self._update_stage_position_display(self._manual_jog_stage_position)
            return
        self._update_stage_position_display(self.stage_controller.latest_stage_position())

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
        preferred_stage_xy = self._preferred_design_display_stage_xy()
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
            design_xy = self._design_xy_from_raw_stage_xy(stage_xy)
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
            probe_route=self._design_session.route,
            selected_route_point_index=self._design_session.selected_route_point_index,
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
        predicted_design_xy = self._design_xy_from_raw_stage_xy(predicted_stage_xy)
        actual_design_xy = self._design_xy_from_raw_stage_xy(actual_stage_xy)
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

    def _request_home_axis_from_ui(self, axis: str) -> None:
        axis_name = axis.strip().upper()
        if axis_name not in {"X", "Y", "Z", "A"}:
            return
        self._queue_or_start_homing_axes([axis_name])

    def _request_home_all_from_ui(self) -> None:
        if self._coordinate_move_axis is not None:
            self.stage_controller.status_message.emit(
                "Stage is busy. Ignoring home request."
            )
            return
        latest_state = (self.stage_controller.latest_stage_state() or "").lower()
        if latest_state not in {"", "idle"}:
            self.stage_controller.status_message.emit(
                "Stage is busy. Ignoring home request."
            )
            return
        if self.stage_controller.request_home_all():
            self._pending_homing_axes.clear()
            self._refresh_pending_homing_ui()

    def _queue_or_start_homing_axes(self, axes: list[str]) -> None:
        normalized: list[str] = []
        for axis in axes:
            axis_name = axis.strip().upper()
            if axis_name not in {"X", "Y", "Z", "A"}:
                continue
            if axis_name == self._homing_active_key:
                continue
            if axis_name in self._pending_homing_axes:
                continue
            normalized.append(axis_name)
        if not normalized:
            return
        latest_state = (self.stage_controller.latest_stage_state() or "").lower()
        if (
            self._homing_active_key is None
            and not self.stage_controller.is_busy()
            and latest_state in {"", "idle"}
            and self._coordinate_move_axis is None
        ):
            first_axis = normalized.pop(0)
            if not self.stage_controller.request_home_axis(first_axis):
                normalized.insert(0, first_axis)
        self._pending_homing_axes.extend(normalized)
        self._refresh_pending_homing_ui()
        if self._pending_homing_axes and self._homing_active_key is None:
            QTimer.singleShot(200, self._start_next_pending_homing_action)

    def _start_next_pending_homing_action(self) -> None:
        if self._homing_active_key is not None or not self._pending_homing_axes:
            return
        if self.stage_controller.is_busy() or self._coordinate_move_axis is not None:
            QTimer.singleShot(200, self._start_next_pending_homing_action)
            return
        latest_state = (self.stage_controller.latest_stage_state() or "").lower()
        if latest_state not in {"", "idle"}:
            QTimer.singleShot(200, self._start_next_pending_homing_action)
            return
        axis = self._pending_homing_axes.pop(0)
        self._refresh_pending_homing_ui()
        if not self.stage_controller.request_home_axis(axis):
            self._pending_homing_axes.insert(0, axis)
            self._refresh_pending_homing_ui()
            QTimer.singleShot(200, self._start_next_pending_homing_action)

    def _clear_pending_homing_queue(self) -> None:
        self._homing_active_key = None
        self._pending_homing_axes.clear()
        self._refresh_pending_homing_ui()

    def _refresh_pending_homing_ui(self) -> None:
        if self.joystick_panel is not None:
            self.joystick_panel.set_pending_homing_actions(
                set(self._pending_homing_axes)
            )

    def _on_homing_action_finished(
        self, success: bool, _message: str, axis_key: str
    ) -> None:
        key = axis_key.strip().upper()
        if key == self._homing_active_key or key == "ALL":
            self._homing_active_key = None
        if not success:
            self._pending_homing_axes.clear()
            self._refresh_pending_homing_ui()
            self._clear_stage_motion_axes()
            return
        if key in {"X", "Y", "B", "ALL"}:
            self._invalidate_design_registration(
                f"Design registration cleared after homing {key}."
            )
        self._clear_stage_motion_axes()
        self._refresh_pending_homing_ui()
        if self._pending_homing_axes:
            QTimer.singleShot(0, self._start_next_pending_homing_action)

    def _on_homing_action_started(self, axis_key: str) -> None:
        key = axis_key.strip().upper()
        self._homing_active_key = key
        if key in self._pending_homing_axes:
            self._pending_homing_axes.remove(key)
            self._refresh_pending_homing_ui()
        if key == "ALL":
            self._set_stage_motion_axes({"X", "Y", "Z", "A"})
        elif key in self.STAGE_AXIS_NAMES:
            self._set_stage_motion_axes({key})

    def _on_needles_action_started(self, _action: str) -> None:
        self._set_stage_motion_axes({"A"})

    def _on_needles_action_finished(
        self, _success: bool, _message: str, _action: str
    ) -> None:
        self._clear_stage_motion_axes()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        serial_was_connected = bool(
            self.serial_connection is not None and self.serial_connection.is_open
        )
        self._persist_serial_connection_state(serial_was_connected)
        if serial_was_connected:
            self._persist_controller_state()
        if self._api_server is not None:
            self._api_server.stop()
        self._design_position_timer.stop()
        self._manual_jog_timer.stop()
        self._stage_motion_blink_timer.stop()
        if self._linear_feedrate_save_timer.isActive():
            self._linear_feedrate_save_timer.stop()
        self._save_pending_linear_feedrate_default()
        if self._route_measurement_runner is not None:
            self._route_measurement_runner.stop()
        if (
            self._route_measurement_thread is not None
            and self._route_measurement_thread.is_alive()
        ):
            self._route_measurement_thread.join(timeout=2.0)
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
        if self.surface_map_window is not None:
            self.surface_map_window.close()
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
        self.serial_connection_panel.lcr_connect_requested.connect(
            self.lcr_controller.request_connect
        )
        self.serial_connection_panel.lcr_disconnect_requested.connect(
            self.lcr_controller.request_disconnect
        )
        self.lcr_controller.status_message.connect(
            self.serial_connection_panel.set_lcr_status_message
        )
        self.serial_connection_dock = CollapsibleDockWidget("Connection", self)
        self.serial_connection_dock.setObjectName("SerialConnectionDock")
        self.serial_connection_dock.setWidget(self.serial_connection_panel)
        self.serial_connection_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self.serial_connection_dock)

        self.joystick_panel = JoystickWindow(self)
        self.joystick_panel.set_stage_controller(self.stage_controller)
        feedrates = self.settings_manager.feedrate_configuration()
        self.joystick_panel.apply_feedrate_settings(
            feedrates.linear.presets,
            feedrates.linear.default,
            feedrates.rotary.presets,
            feedrates.rotary.default,
        )
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
        needle_settings = self.settings_manager.needle_calibration_configuration()
        self.joystick_panel.apply_needle_settings(needle_settings.feedrate_mm_min)
        self.stage_controller.set_motion_safety_disabled(jog.motion_safety_disabled)
        self.joystick_panel.set_serial(self.serial_connection)
        self.joystick_panel.autofocus_requested.connect(
            self.stage_controller.request_autofocus
        )
        self.joystick_panel.home_axis_requested.connect(
            self._request_home_axis_from_ui
        )
        self.joystick_panel.home_all_requested.connect(
            self._request_home_all_from_ui
        )
        self.joystick_panel.needles_raise_requested.connect(
            self.stage_controller.request_needles_raise
        )
        self.joystick_panel.needles_lower_requested.connect(
            self.stage_controller.request_needles_lower
        )
        self.joystick_panel.needle_contact_coordinate_save_requested.connect(
            self._save_needle_position_from_display_a_coordinate
        )
        self.joystick_panel.zero_b_requested.connect(self._zero_b_axis)
        self.joystick_panel.manual_axis_move_requested.connect(
            self._on_manual_axis_move_requested
        )
        self.joystick_panel.manual_axis_settings_changed.connect(
            self._save_manual_axis_jog_settings
        )
        self.joystick_panel.linear_feedrate_changed.connect(
            self._on_linear_feedrate_changed
        )
        self.joystick_panel.needle_feedrate_changed.connect(
            self._on_needle_feedrate_changed
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
        self.stage_controller.limit_axes_changed.connect(self._on_limit_axes_changed)
        self.stage_controller.limit_axes_changed.connect(self.joystick_panel.set_limit_axes)
        self.stage_controller.homing_action_started.connect(
            self.joystick_panel.set_homing_action_started
        )
        self.stage_controller.homing_action_started.connect(
            self._on_homing_action_started
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
        self.stage_controller.needles_action_started.connect(
            self._on_needles_action_started
        )
        self.stage_controller.needles_action_finished.connect(
            self.joystick_panel.set_needles_action_finished
        )
        self.stage_controller.needles_action_finished.connect(
            self._on_needles_action_finished
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
        self.contact_calibration_window.save_current_needle_height_requested.connect(
            self._save_current_needle_height
        )
        self.contact_calibration_window.lower_needles_requested.connect(
            lambda: self.stage_controller.request_needles_lower(
                self._current_needle_feedrate()
            )
        )
        self.contact_calibration_window.raise_needles_requested.connect(
            lambda: self.stage_controller.request_needles_raise(
                self._current_needle_feedrate()
            )
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

    def _show_surface_map_window(self) -> None:
        if self.surface_map_window is None:
            from probe_station_gui.views.surface_map_panel import SurfaceMapWindow

            self.surface_map_window = SurfaceMapWindow(
                stage_status_provider=self._surface_map_stage_status,
                stage_move_requester=self._surface_map_move_to_xy,
                settings_path=self.settings_manager.config_dir() / "surface-map-settings.json",
                parent=None,
            )
        self.surface_map_window.showNormal()
        self.surface_map_window.raise_()

    def _create_design_layout_window(
        self,
        design_layout_window_class: object | None = None,
    ) -> None:
        if self.design_layout_window is not None:
            return
        if design_layout_window_class is None:
            design_layout_window_class = self._design_layout_window_class
        if design_layout_window_class is None:
            from probe_station_gui.views.design_navigator_panel import (
                DesignLayoutWindow as imported_design_layout_window_class,
            )

            design_layout_window_class = imported_design_layout_window_class
        self._design_layout_window_class = design_layout_window_class
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
        self.design_navigator_panel.design_rotate_requested.connect(
            self._rotate_design_document
        )
        self.design_navigator_panel.load_script_requested.connect(
            self._load_measurement_script
        )
        self.design_navigator_panel.reload_script_requested.connect(
            self._reload_measurement_script
        )
        self.design_navigator_panel.route_new_requested.connect(
            self._create_measurement_route
        )
        self.design_navigator_panel.route_open_requested.connect(
            self._load_measurement_route
        )
        self.design_navigator_panel.route_save_requested.connect(
            self._save_measurement_route
        )
        self.design_navigator_panel.route_save_as_requested.connect(
            self._save_measurement_route_as
        )
        self.design_navigator_panel.route_add_current_requested.connect(
            self._add_current_design_route_point
        )
        self.design_navigator_panel.route_remove_selected_requested.connect(
            self._remove_selected_route_point
        )
        self.design_navigator_panel.route_clear_requested.connect(
            self._clear_measurement_route_points
        )
        self.design_navigator_panel.route_selected.connect(
            self._select_route_point
        )
        self.design_navigator_panel.route_offsets_changed.connect(
            self._set_route_needle_offsets
        )
        self.design_navigator_panel.route_edit_enabled_changed.connect(
            self._set_route_edit_enabled
        )
        self.design_navigator_panel.route_array_requested.connect(
            self._add_route_array_points
        )
        self.design_navigator_panel.route_measurement_run_requested.connect(
            self._start_route_measurement
        )
        self.design_navigator_panel.route_measurement_stop_requested.connect(
            self._request_stop_route_measurement
        )
        self.design_navigator_panel.route_measurement_confirmation_requested.connect(
            self._submit_route_measurement_confirmation
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
        self.design_layout_window.route_point_requested.connect(
            self._add_design_route_point
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

    def _on_needle_height_changed(self, lowering_mm: float) -> None:
        if self.contact_calibration_window is not None:
            self.contact_calibration_window.set_current_needle_lowering(lowering_mm)

    def _on_lcr_connection_changed(
        self, connected: bool, backend_name: str, description: str
    ) -> None:
        if self.serial_connection_panel is not None:
            self.serial_connection_panel.set_lcr_connection_state(
                connected, backend_name, description
            )

    def _on_lcr_reading_updated(self, resistance_ohm: float, is_short: bool) -> None:
        if self.serial_connection_panel is not None:
            self.serial_connection_panel.set_lcr_reading(resistance_ohm, is_short)

    def _display_a_for_needle_lowering(self, lowering_mm: float | None) -> float | None:
        if lowering_mm is None:
            return None
        target_raw_a = self.stage_controller.axis_a_gcode_coordinate_for_lowering(
            lowering_mm
        )
        return self.stage_controller.calibrated_axis_display_value("A", target_raw_a)

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
        self._save_needle_down_position_from_raw_a_coordinate(a_position)

    def _save_needle_position_from_display_a_coordinate(
        self,
        action: str,
        a_coordinate: float,
    ) -> None:
        try:
            display_a = float(a_coordinate)
        except (TypeError, ValueError):
            self._show_status("Invalid A coordinate.")
            return
        if not math.isfinite(display_a):
            self._show_status("Invalid A coordinate.")
            return
        raw_a = self.stage_controller.calibrated_axis_raw_value("A", display_a)
        self._save_needle_position_from_raw_a_coordinate(action, raw_a)

    def _save_needle_down_position_from_raw_a_coordinate(
        self,
        a_coordinate: float,
    ) -> None:
        self._save_needle_position_from_raw_a_coordinate("lower", a_coordinate)

    def _save_needle_position_from_raw_a_coordinate(
        self,
        action: str,
        a_coordinate: float,
    ) -> None:
        action_key = action.strip().lower()
        if action_key not in {"raise", "lower"}:
            self._show_status(f"Unknown needle target '{action}'.")
            return
        try:
            raw_a = float(a_coordinate)
        except (TypeError, ValueError):
            self._show_status("Invalid A coordinate.")
            return
        if not math.isfinite(raw_a):
            self._show_status("Invalid A coordinate.")
            return
        lowering_mm = self.stage_controller.axis_a_lowering_for_gcode_coordinate(raw_a)
        settings = self.settings_manager.settings.clone()
        if action_key == "raise":
            settings.needle_calibration.raise_position_mm = lowering_mm
            settings.needle_calibration.raise_position_configured = True
        else:
            settings.needle_calibration.down_position_mm = lowering_mm
            settings.needle_calibration.down_position_configured = True
        self.settings_manager.replace(settings)
        self.settings_manager.save()
        self._apply_settings()
        target_display_a = self._display_a_for_needle_lowering(lowering_mm)
        self._show_status(
            f"Saved needle {action_key} target "
            f"A={target_display_a:.4f} ({lowering_mm:.4f} mm lowering)."
        )

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
    diagnostics_path = configure_crash_diagnostics()
    logger.debug("Crash diagnostics enabled: %s", diagnostics_path)
    app = QApplication(sys.argv)
    window = Main()
    _set_initial_window_geometry(window)
    window.show()
    QTimer.singleShot(0, lambda: _fit_window_to_screen(window))
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
