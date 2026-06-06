"""Application entry point for the probe station GUI."""

from __future__ import annotations

import logging
import csv
import importlib
import json
import math
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
import sys
from typing import Any, Callable, TYPE_CHECKING

_STARTUP_T0 = time.perf_counter()
_STARTUP_LAST_ELAPSED_MS = 0.0
_STARTUP_EVENTS: list[tuple[float, float, str]] = []
_STARTUP_FLUSHED = False


def _startup_trace(label: str) -> None:
    """Record startup timing before the normal logger is ready."""

    global _STARTUP_LAST_ELAPSED_MS

    elapsed_ms = (time.perf_counter() - _STARTUP_T0) * 1000.0
    delta_ms = elapsed_ms - _STARTUP_LAST_ELAPSED_MS
    _STARTUP_LAST_ELAPSED_MS = elapsed_ms
    if _STARTUP_FLUSHED:
        logging.getLogger(__name__).info(
            "STARTUP TRACE +%.1fms (+%.1fms) %s",
            elapsed_ms,
            delta_ms,
            label,
        )
        return
    _STARTUP_EVENTS.append((elapsed_ms, delta_ms, label))


def _flush_startup_trace() -> None:
    """Write buffered startup timing into the configured application log."""

    global _STARTUP_FLUSHED

    if _STARTUP_FLUSHED:
        return
    log = logging.getLogger(__name__)
    for elapsed_ms, delta_ms, label in _STARTUP_EVENTS:
        log.info(
            "STARTUP TRACE +%.1fms (+%.1fms) %s",
            elapsed_ms,
            delta_ms,
            label,
        )
    _STARTUP_EVENTS.clear()
    _STARTUP_FLUSHED = True


_startup_trace("stdlib imports done")

from PySide6.QtCore import (
    QBuffer,
    QIODevice,
    QObject,
    QLocale,
    QThread,
    QTimer,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QDoubleValidator,
    QIcon,
    QImage,
    QKeySequence,
    QPainter,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
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
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

_startup_trace("PySide imports done")

from probe_station_gui import (
    Grabber,
    JoystickWindow,
    MicroscopeView,
    StageController,
    SerialTerminalWindow,
)
from probe_station_gui.design_model import DesignDocument, DesignModelError
from probe_station_gui.design_session import AlignmentPreparation, DesignSession
from probe_station_gui.diagnostics import configure_crash_diagnostics
from probe_station_gui.api_server import ProbeStationApiServer
from probe_station_gui.api_keys import API_KEY_FILENAME, ApiKeyStore
from probe_station_gui.lcr_meter import (
    GWInstekRouteMeterSettings,
    KeithleyRouteMeterSettings,
    LCRMeterController,
    LCRMeterError,
    ROUTE_METER_GWINSTEK,
    ROUTE_METER_KEITHLEY,
    RouteMeterConfiguration,
)
from probe_station_gui.motion_prediction import interpolate_position, motion_progress
from probe_station_gui.wheel_guard import GuardedComboBox as QComboBox
from probe_station_gui.stage_controller import StageControllerError
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
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_PHOTO,
    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
    RouteContactHeightRecord,
    RouteExternalMeasurementSessionRunner,
    RouteMeasurementPoint,
    RoutePhotoRecord,
    RouteMeasurementRecord,
    RouteMeasurementRunner,
    filter_route_points_by_previous_status,
    route_measurement_sample_from_raw,
    summarize_route_contact_quality,
)
from probe_station_gui.microscope_imaging import (
    MicroscopeCaptureResult,
    MicroscopeImageMetadata,
    MicroscopeScanPlan,
    MicroscopeScanTile,
    build_design_scan_plan,
    objective_scale_calibration,
    route_photo_filename,
    save_microscope_image,
    scan_tile_filename,
    stage_bounds_from_design_bounds,
    stitch_scan_tiles,
    utc_timestamp,
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
from probe_station_gui.telegram_notifications import (
    TelegramBotCommandService,
    TelegramBotRequest,
    TelegramBotResponse,
    resolved_bot_token,
    send_telegram_message_in_thread,
    telegram_inline_keyboard,
)
from probe_station_gui.views.alignment_panel import AlignmentPanel
from probe_station_gui.views.contact_oscillation_window import (
    ContactOscillationWindow,
)
from probe_station_gui.views.dock_widgets import CollapsibleDockWidget
from probe_station_gui.views.oscillation_panel import OscillationPanel
from probe_station_gui.views.resistance_monitor_panel import ResistanceMonitorPanel
from probe_station_gui.views.serial_connection_panel import SerialConnectionPanel

_startup_trace("application imports done")


logger = logging.getLogger(__name__)

APP_ICON_RESOURCE = "assets/app_icon.ico"
WINDOWS_APP_USER_MODEL_ID = "ProbeStationGUI.ProbeStationGUI"


def _application_icon() -> QIcon:
    icon_path = resources.files("probe_station_gui").joinpath(APP_ICON_RESOURCE)
    icon = QIcon(str(icon_path))
    if icon.isNull():
        logger.warning("Application icon could not be loaded: %s", icon_path)
    return icon


def _configure_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            WINDOWS_APP_USER_MODEL_ID
        )
    except Exception:
        logger.debug("Unable to set Windows application user model ID", exc_info=True)


if TYPE_CHECKING:
    from probe_station_gui.dialogs.microscope_scan_dialog import (
        MicroscopeScanConfiguration,
        MicroscopeScanDialog,
    )
    from probe_station_gui.dialogs.route_measurement_dialog import (
        RouteMeasurementDialog,
        RouteMeasurementRunConfiguration,
    )
    from probe_station_gui.dialogs.settings_dialog import SettingsDialog
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
    route_measurement_started: Signal = Signal(str, int, int, bool)
    route_measurement_status: Signal = Signal(str)
    route_measurement_progress: Signal = Signal(int, int, int)
    route_measurement_waiting_changed: Signal = Signal(bool)
    route_measurement_result: Signal = Signal(object, int, int, bool)
    route_measurement_recorded: Signal = Signal(object, int, int)
    route_measurement_finished: Signal = Signal(object, bool, str, str)
    route_contact_move_finished: Signal = Signal(bool, str)
    status_message_requested: Signal = Signal(str, int)
    telegram_bot_request_received: Signal = Signal(object)
    microscope_scan_status: Signal = Signal(str)
    microscope_scan_finished: Signal = Signal(bool, str)
    contact_seek_status: Signal = Signal(str)
    contact_seek_calibration_found: Signal = Signal(float, str)
    contact_seek_finished: Signal = Signal(bool, str)
    sample_handling_status: Signal = Signal(str)
    sample_handling_finished: Signal = Signal(bool, str, bool, object)

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
    COORDINATE_MOVE_TARGET_TOLERANCE_MM = 7.5e-4
    TERMINAL_REFRESH_DELAYS_MS = (180, 500)
    TERMINAL_RESET_REFRESH_DELAYS_MS = (500, 1100, 1800)
    TERMINAL_RESUME_AFTER_JOG_MS = 180
    CONTROLLER_ACTIVE_STATE_STALE_S = 2.0
    STAGE_AXIS_NAMES = ("X", "Y", "Z", "A", "B", "C")
    MIN_FEEDRATE_MM_MIN = 1.0
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
    CONTACT_SEEK_STEP_MM = -0.001
    CONTACT_SEEK_MAX_TOTAL_MM = 0.020
    CONTACT_SEEK_QUICK_COUNT = 25
    CONTACT_SEEK_CONFIRM_COUNT = 250
    SAMPLE_LOAD_X_MM = 0.0
    SAMPLE_LOAD_Y_MM = 0.0
    SAMPLE_UNLOAD_X_MM = -32.0
    SAMPLE_UNLOAD_Y_MM = 32.0

    def __init__(self) -> None:
        _startup_trace("Main.__init__ entered")
        super().__init__()
        self.setWindowTitle("Microscope control")
        app = QApplication.instance()
        if app is not None:
            self.setWindowIcon(app.windowIcon())
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
        _startup_trace("SettingsManager created; logging configured")
        _flush_startup_trace()
        self._api_key_store = ApiKeyStore(
            self.settings_manager.config_dir() / API_KEY_FILENAME
        )
        self._api_bridge: _ApiRequestBridge | None = None
        self._api_server: ProbeStationApiServer | None = None
        self._api_settings_signature: tuple[bool, str, int] | None = None
        self._telegram_bot_service: TelegramBotCommandService | None = None
        self._telegram_bot_signature: tuple[str, str] | None = None
        self._latest_status_message = ""
        self.joystick_panel: JoystickWindow | None = None
        self.serial_terminal_panel: SerialTerminalWindow | None = None
        self.serial_connection_panel: SerialConnectionPanel | None = None
        self.serial_connection_dialog: QDialog | None = None
        self.serial_connection_tabs: QTabWidget | None = None
        self.resistance_panel: ResistanceMonitorPanel | None = None
        self.oscillation_panel: OscillationPanel | None = None
        self.surface_map_window: SurfaceMapWindow | None = None
        self.microscope_scan_dialog: MicroscopeScanDialog | None = None
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
        self.resistance_dock: CollapsibleDockWidget | None = None
        self.oscillation_dock: CollapsibleDockWidget | None = None
        self.alignment_dock: CollapsibleDockWidget | None = None
        self._alignment_capture_action: QAction | None = None
        self._alignment_exit_action: QAction | None = None
        self._contact_calibration_window_action: QAction | None = None
        self._surface_map_window_action: QAction | None = None
        self._microscope_scan_action: QAction | None = None
        self._design_layout_window_action: QAction | None = None
        self._click_calibration_action: QAction | None = None
        self._click_calibration_dialog: ClickCalibrationDialog | None = None
        self._sample_load_action: QAction | None = None
        self._sample_unload_action: QAction | None = None
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
        self._coordinate_move_seen_active_state = False
        self._coordinate_move_reissue_cancel_pending = False
        self._pending_click_to_move: tuple[float, float, float, float] | None = None
        self._pending_click_deadline: float | None = None
        self._pending_stage_axis_targets: dict[str, tuple[float, float]] = {}
        self._design_snap_enabled = True
        self._last_reported_b_position: float | None = None
        self._last_camera_frame_ui_timestamp: float | None = None
        self._latest_camera_frame: QImage | None = None
        self._latest_camera_frame_counter = 0
        self._latest_camera_frame_condition = threading.Condition()
        self._latest_camera_frame_for_notifications: QImage | None = None
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
        self._route_contact_move_thread: threading.Thread | None = None
        self._route_measurement_dialog: RouteMeasurementDialog | None = None
        self._route_measurement_runtime_configuration: (
            RouteMeasurementRunConfiguration | None
        ) = None
        self._route_measurement_waiting = False
        self._route_measurement_photo_enabled = False
        self._route_measurement_measure_enabled = False
        self._route_measurement_point_numbers: list[int] = []
        self._route_measurement_current_point: int | None = None
        self._last_route_measurement_result: tuple[
            RouteMeasurementRecord,
            int,
            int,
            bool,
        ] | None = None
        self._pending_route_measure_point: int | None = None
        self._route_measurement_session_active = False
        self._route_measurement_context_close_requested = False
        self._api_route_session_id: str | None = None
        self._api_route_last_status: dict[str, Any] | None = None
        self._api_route_lcr_controller: object | None = None
        self._api_route_artifacts: dict[str, dict[str, object]] = {}
        self._api_route_artifacts_lock = threading.Lock()
        self._microscope_scan_thread: threading.Thread | None = None
        self._microscope_scan_stop_requested = threading.Event()
        self._sample_handling_thread: threading.Thread | None = None
        self._last_sample_focus_z_by_objective: dict[str, float] = {}
        self._last_telegram_attention_message = ""
        self._telegram_route_photo_requested = False
        self._telegram_contact_photo_requested = False
        self._telegram_photo_lock = threading.Lock()
        self._telegram_pending_contact_photo: tuple[bytes, str, str] | None = None
        self._telegram_pending_contact_before_photo: tuple[bytes, str, str] | None = None
        self._last_route_pre_contact_photo: tuple[int, int, bytes, str, str] | None = None
        self._last_route_contact_failure_photo: tuple[bytes, str, str] | None = None
        self._last_route_contact_failure_before_photo: tuple[bytes, str, str] | None = None
        self._contact_seek_thread: threading.Thread | None = None
        self._contact_seek_stop_requested = threading.Event()
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
        self.status_message_requested.connect(
            self._show_status,
            Qt.ConnectionType.QueuedConnection,
        )
        self.grabber = Grabber()
        self.thread = QThread()
        self.grabber.moveToThread(self.thread)
        self.thread.started.connect(self.grabber.start)
        self.view.clicked.connect(self.on_click)
        self.view.hovered.connect(self._on_view_hover)
        self.view.hover_left.connect(self._on_view_hover_left)
        self.view.design_minimap_clicked.connect(
            self._open_design_window_from_minimap_point
        )
        self.view.design_minimap_double_clicked.connect(
            lambda: self._toggle_design_layout_window(True)
        )
        self.grabber.frame_ready.connect(self._on_camera_frame)
        self.grabber.error.connect(self.on_error)
        self.design_layout_module_ready.connect(self._on_design_layout_module_ready)
        self.design_document_loaded.connect(self._on_design_document_loaded)
        self.route_measurement_started.connect(
            self._on_route_measurement_started
        )
        self.route_measurement_status.connect(self._on_route_measurement_status)
        self.route_measurement_progress.connect(self._on_route_measurement_progress)
        self.route_measurement_waiting_changed.connect(
            self._on_route_measurement_waiting_changed
        )
        self.route_measurement_result.connect(self._on_route_measurement_result)
        self.route_measurement_recorded.connect(self._on_route_measurement_recorded)
        self.route_measurement_finished.connect(self._on_route_measurement_finished)
        self.route_contact_move_finished.connect(self._on_route_contact_move_finished)
        self.telegram_bot_request_received.connect(
            self._on_telegram_bot_request_received
        )
        self.microscope_scan_status.connect(self._on_microscope_scan_status)
        self.microscope_scan_finished.connect(self._on_microscope_scan_finished)
        self.contact_seek_status.connect(self._on_contact_seek_status)
        self.contact_seek_calibration_found.connect(
            self._on_contact_seek_calibration_found
        )
        self.contact_seek_finished.connect(self._on_contact_seek_finished)
        self.sample_handling_status.connect(self._show_status)
        self.sample_handling_finished.connect(self._on_sample_handling_finished)

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
        self.stage_controller.axis_max_feedrates_changed.connect(
            self._on_axis_max_feedrates_changed
        )
        self.stage_controller.controller_reboot_detected.connect(
            self._on_controller_reboot_detected
        )
        self.stage_controller.controller_reboot_ready.connect(
            self._on_controller_reboot_ready
        )
        self.stage_controller.oscillation_state_changed.connect(
            self._on_oscillation_state_changed
        )
        self.stage_controller.movement_started.connect(self._on_stage_task_started)
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
        _startup_trace("dock widgets created")

        self._setup_menus()
        _startup_trace("menus created")
        self._apply_settings()
        _startup_trace("settings applied")
        self._api_bridge = _ApiRequestBridge(self._handle_api_request, self)
        self._configure_api_server_from_settings(start_if_enabled=False)
        _startup_trace("API server configured")

        QTimer.singleShot(0, self._start_api_server)
        QTimer.singleShot(0, self._auto_connect_if_possible)
        QTimer.singleShot(0, self._prime_keyboard_focus)
        QTimer.singleShot(0, self._start_camera_thread)
        QTimer.singleShot(1500, self._preload_design_layout_window)
        QTimer.singleShot(2500, self._preload_lazy_dialog_modules)

        self.setStyleSheet(
            """
            QMainWindow::separator { width: 8px; height: 8px; background: palette(window); }
            """
        )
        _startup_trace("Main.__init__ finished")

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
            command_callback=self._submit_api_command_request,
            auth_callback=self._authorize_api_request,
            host=api_settings.host,
            port=api_settings.port,
        )
        if start_if_enabled:
            self._start_api_server()

    def _authorize_api_request(
        self,
        api_key: str | None,
        permission: str,
    ) -> dict[str, Any]:
        return self._api_key_store.authorize(api_key, permission)

    def _configure_telegram_bot_from_settings(self) -> None:
        telegram_settings = self.settings_manager.telegram_configuration()
        bot_token = resolved_bot_token(telegram_settings)
        chat_id = telegram_settings.chat_id.strip()
        signature = (
            bot_token,
            chat_id,
        )
        should_run = bool(telegram_settings.enabled and bot_token and chat_id)
        if (
            should_run
            and self._telegram_bot_service is not None
            and self._telegram_bot_signature == signature
        ):
            if self._telegram_bot_service.is_running():
                return
            logger.warning("Telegram command bot thread is not running; restarting.")
        if self._telegram_bot_service is not None:
            self._stop_telegram_bot_service()
        self._telegram_bot_signature = signature if should_run else None
        if not should_run:
            return
        try:
            service = TelegramBotCommandService(
                bot_token=bot_token,
                chat_id=chat_id,
                request_handler=self._submit_telegram_bot_request,
            )
            service.start()
        except Exception as exc:
            logger.warning("Telegram command bot was not started: %s", exc)
            self._telegram_bot_signature = None
            return
        self._telegram_bot_service = service
        logger.info("Telegram command bot started for chat %s.", chat_id)

    def _stop_telegram_bot_service(self) -> None:
        if self._telegram_bot_service is None:
            return
        self._telegram_bot_service.stop()
        self._telegram_bot_service = None
        self._telegram_bot_signature = None

    def _submit_telegram_bot_request(
        self,
        request: TelegramBotRequest,
    ) -> TelegramBotResponse | None:
        self.telegram_bot_request_received.emit(request)
        response = request.wait_for_response(15.0)
        if response is None:
            return TelegramBotResponse(
                "Telegram command timed out in the GUI thread.",
                callback_answer="Command timed out.",
            )
        return response

    def _on_telegram_bot_request_received(
        self,
        request: TelegramBotRequest,
    ) -> None:
        try:
            response = self._handle_telegram_bot_request(request)
        except Exception as exc:
            logger.exception("Telegram command failed.")
            response = TelegramBotResponse(
                f"Telegram command failed: {exc}",
                callback_answer="Command failed.",
            )
        request.set_response(response)

    def _handle_telegram_bot_request(
        self,
        request: TelegramBotRequest,
    ) -> TelegramBotResponse | None:
        if request.kind == "callback":
            return self._handle_telegram_callback(request.callback_data)
        command, _args = self._parse_telegram_command(request.text)
        if command in {"start", "help", "commands"}:
            return TelegramBotResponse(
                self._telegram_help_text(),
                reply_markup=self._telegram_default_markup(),
                callback_answer="Commands.",
            )
        if command in {"status", "статус"}:
            return self._telegram_status_response()
        if command in {"next_photo", "photo", "route_photo", "фото"}:
            return self._telegram_request_next_route_photo_response()
        if command in {"next_contact", "contact", "контакт"}:
            return self._telegram_request_next_contact_photo_response()
        if command in {
            "measure",
            "remeasure",
            "skip",
            "next",
        }:
            return self._telegram_route_action_response(command)
        return None

    def _handle_telegram_callback(self, data: str) -> TelegramBotResponse:
        key = str(data or "").strip().lower()
        if key == "status":
            return self._telegram_status_response()
        if key == "watch:photo":
            return self._telegram_request_next_route_photo_response()
        if key == "watch:contact":
            return self._telegram_request_next_contact_photo_response()
        if key.startswith("route:"):
            return self._telegram_route_action_response(key.split(":", 1)[1])
        return TelegramBotResponse(
            "Unknown Telegram action.",
            callback_answer="Unknown action.",
            reply_markup=self._telegram_default_markup(),
        )

    @staticmethod
    def _parse_telegram_command(text: str) -> tuple[str, str]:
        stripped = str(text or "").strip()
        if not stripped:
            return "", ""
        if stripped.startswith("/"):
            parts = stripped.split(maxsplit=1)
            command = parts[0].lstrip("/").split("@", 1)[0].lower()
            args = parts[1] if len(parts) > 1 else ""
            return command, args.strip()
        parts = stripped.split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1] if len(parts) > 1 else ""
        return command, args.strip()

    @staticmethod
    def _telegram_help_text() -> str:
        return (
            "Probe Station Telegram commands:\n"
            "/status - current state and microscope frame\n"
            "/next_photo - send the next route structure photo\n"
            "/next_contact - send the next route contact attempt photo\n"
            "/measure, /remeasure, /skip, /next - answer a waiting route prompt"
        )

    def _telegram_status_response(self) -> TelegramBotResponse:
        photo = self._latest_camera_frame_photo()
        return TelegramBotResponse(
            self._telegram_status_text(),
            photo_bytes=photo[0] if photo is not None else None,
            photo_name=photo[1] if photo is not None else "microscope.jpg",
            reply_markup=self._telegram_default_markup(),
            callback_answer="Status sent.",
        )

    def _telegram_request_next_route_photo_response(self) -> TelegramBotResponse:
        route_active = (
            self._route_measurement_thread is not None
            and self._route_measurement_thread.is_alive()
        )
        if not route_active:
            return TelegramBotResponse(
                "No route measurement is running.",
                reply_markup=self._telegram_default_markup(),
                callback_answer="No active route.",
            )
        if not self._route_measurement_photo_enabled:
            return TelegramBotResponse(
                "The active route is not configured to capture structure photos.",
                reply_markup=self._telegram_default_markup(),
                callback_answer="No route photos.",
            )
        with self._telegram_photo_lock:
            self._telegram_route_photo_requested = True
        return TelegramBotResponse(
            "The next route structure photo will be sent here.",
            reply_markup=self._telegram_default_markup(),
            callback_answer="Waiting for route photo.",
        )

    def _telegram_request_next_contact_photo_response(self) -> TelegramBotResponse:
        route_active = (
            self._route_measurement_thread is not None
            and self._route_measurement_thread.is_alive()
        )
        if not route_active:
            return TelegramBotResponse(
                "No route measurement is running.",
                reply_markup=self._telegram_default_markup(),
                callback_answer="No active route.",
            )
        if not self._route_measurement_measure_enabled:
            return TelegramBotResponse(
                "The active route is not configured to measure contacts.",
                reply_markup=self._telegram_default_markup(),
                callback_answer="No contact measurements.",
            )
        with self._telegram_photo_lock:
            self._telegram_contact_photo_requested = True
        return TelegramBotResponse(
            "The next route contact attempt photo will be sent here.",
            reply_markup=self._telegram_default_markup(),
            callback_answer="Waiting for contact photo.",
        )

    def _telegram_route_action_response(self, action: str) -> TelegramBotResponse:
        action_key = str(action or "").strip().lower()
        if action_key not in {"measure", "remeasure", "skip", "next"}:
            return TelegramBotResponse(
                "Unknown route action.",
                reply_markup=self._telegram_default_markup(),
                callback_answer="Unknown action.",
            )
        if not self._route_measurement_waiting:
            return TelegramBotResponse(
                "Route measurement is not waiting for an action.",
                reply_markup=self._telegram_default_markup(),
                callback_answer="Route is not waiting.",
            )
        runner = self._route_measurement_runner
        if runner is None:
            return TelegramBotResponse(
                "No route measurement is running.",
                reply_markup=self._telegram_default_markup(),
                callback_answer="No active route.",
            )
        self._submit_route_measurement_confirmation(action_key)
        return TelegramBotResponse(
            f"Route measurement action submitted: {action_key}.",
            reply_markup=self._telegram_default_markup(),
            callback_answer=f"{action_key} submitted.",
        )

    def _telegram_default_markup(self) -> object | None:
        rows: list[list[tuple[str, str]]] = [
            [("Status", "status")],
            [
                ("Next photo", "watch:photo"),
                ("Next contact", "watch:contact"),
            ],
        ]
        if self._route_measurement_waiting:
            rows.append(
                [
                    ("Measure", "route:measure"),
                    ("Remeasure", "route:remeasure"),
                    ("Skip", "route:skip"),
                ]
            )
        return telegram_inline_keyboard(rows)

    @staticmethod
    def _telegram_route_actions_markup() -> object | None:
        return telegram_inline_keyboard(
            [
                [
                    ("Measure", "route:measure"),
                    ("Remeasure", "route:remeasure"),
                    ("Skip", "route:skip"),
                ],
                [("Status", "status")],
            ]
        )

    def _telegram_status_text(self) -> str:
        stage_status = self._api_stage_status()
        route_active = (
            self._route_measurement_thread is not None
            and self._route_measurement_thread.is_alive()
        )
        route_state = "idle"
        if route_active:
            route_state = "waiting" if self._route_measurement_waiting else "running"
            if self._route_measurement_current_point is not None:
                route_state = (
                    f"{route_state}, point {self._route_measurement_current_point}"
                )
        elif self._route_measurement_session_active:
            route_state = "session active"
            if self._route_measurement_current_point is not None:
                route_state = (
                    f"{route_state}, point {self._route_measurement_current_point}"
                )
        lines = [
            "Probe Station status",
            f"Current: {self._latest_status_message or 'idle'}",
            f"Route: {route_state}",
            f"Serial: {'connected' if stage_status.get('connected') else 'disconnected'}",
            f"Stage: {stage_status.get('state') or 'unknown'}"
            f"{' busy' if stage_status.get('busy') else ''}",
            f"Coordinates: {stage_status.get('coordinate_display') or 'unknown'}",
        ]
        position = stage_status.get("display_position")
        if isinstance(position, dict) and position:
            values = []
            for axis in self.STAGE_AXIS_NAMES:
                if axis in position:
                    try:
                        values.append(f"{axis}={float(position[axis]):.4f}")
                    except (TypeError, ValueError):
                        pass
            if values:
                lines.append("Position: " + ", ".join(values))
        homed_axes = stage_status.get("homed_axes")
        if isinstance(homed_axes, list):
            lines.append("Homed: " + (", ".join(homed_axes) if homed_axes else "none"))
        if self._microscope_scan_thread is not None and self._microscope_scan_thread.is_alive():
            lines.append("Microscope scan: running")
        if self._contact_seek_thread is not None and self._contact_seek_thread.is_alive():
            lines.append("Contact seek: running")
        if self._latest_camera_frame_for_notifications is None:
            lines.append("Camera frame: unavailable")
        return "\n".join(lines)

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

    def _submit_api_command_request(self, command_request: dict[str, Any]) -> dict[str, Any]:
        action = str(command_request.get("action", "")).strip().lower()
        payload = command_request.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if action == "list_contacts":
            return self._api_list_contacts()
        if action == "move_to_contact":
            return self._api_move_to_contact(payload)
        if action == "contact_needles":
            return self._api_contact_needles(payload)
        if action == "check_contact":
            return self._api_check_contact(payload)
        if action == "route_contact_focus":
            return self._api_route_contact_focus(payload)
        if action == "contact_seek":
            return self._api_contact_seek(payload)
        if action == "configure_meter":
            return self._api_configure_meter(payload)
        if action == "raw_voltage_sweep":
            return self._api_raw_voltage_sweep(payload)
        if action == "visa_list_resources":
            return self._api_visa_list_resources()
        if action == "visa_operation":
            return self._api_visa_operation(payload)
        if action == "start_route_session":
            return self._api_start_route_session(payload)
        if action == "route_session_status":
            return self._api_route_session_status()
        if action == "route_session_action":
            return self._api_route_session_action(payload)
        if action == "route_session_result":
            return self._api_route_session_result(payload)
        if action == "route_session_seek":
            return self._api_route_session_seek()
        if action == "route_session_artifact":
            return self._api_route_session_artifact(payload)
        return {
            "accepted": False,
            "status_code": 400,
            "message": f"Unsupported API command: {action}",
        }

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
        feedrate = self._coordinate_feedrate_for_axes(("X", "Y"))
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
            return max(self.MIN_FEEDRATE_MM_MIN, float(self._current_linear_feedrate()))
        try:
            value = float(feedrate)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value <= 0.0:
            return None
        return max(self.MIN_FEEDRATE_MM_MIN, value)

    def _api_list_contacts(self) -> dict[str, Any]:
        route = self._design_session.route
        if route is None:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "No probe route is loaded.",
            }
        registration_valid = (
            self._design_session.registration is not None
            and self._design_session.registration.valid
        )
        contacts = [
            self._api_route_point_payload(
                route_index=index,
                route_point=route_point,
                include_stage_xy=registration_valid,
            )
            for index, route_point in enumerate(route.points, start=1)
        ]
        return {
            "accepted": True,
            "route_name": route.name,
            "route_path": str(route.path) if route.path is not None else None,
            "registration_valid": registration_valid,
            "contacts": contacts,
        }

    def _api_move_to_contact(self, payload: dict[str, Any]) -> dict[str, Any]:
        contact_number = self._api_contact_number(payload)
        if contact_number is None:
            return {
                "accepted": False,
                "status_code": 400,
                "message": "Provide a positive contact_number.",
            }
        context_result = self._api_contact_context(contact_number)
        if not context_result.get("accepted", False):
            return context_result
        point = context_result["point"]
        contact = context_result["contact"]
        lower_needles = self._api_bool(
            payload,
            "lower_needles",
            "lower",
            default=False,
        )
        lift_before_move = self._api_bool(
            payload,
            "lift_before_move",
            default=True,
        )
        lift_after = self._api_bool(payload, "lift_after", default=False)
        contact_settle_s = self._api_float(
            payload,
            "contact_settle_s",
            "settle_s",
            default=0.2,
            minimum=0.0,
        )
        needle_feedrate = self._api_needle_feedrate(payload)
        active_stage_task = False
        needles_lowered = False
        try:
            self.stage_controller.begin_external_task("API contact move")
            active_stage_task = True
            if lift_before_move:
                self.stage_controller.run_external_needles_action(
                    "lift",
                    needle_feedrate,
                )
            self.stage_controller.run_external_move_to_xy(
                point.stage_xy[0],
                point.stage_xy[1],
            )
            if lower_needles:
                self.stage_controller.run_external_needles_action(
                    "lower",
                    needle_feedrate,
                )
                needles_lowered = True
                if contact_settle_s > 0.0:
                    time.sleep(contact_settle_s)
            return {
                "accepted": True,
                "message": (
                    f"Moved to contact {contact['contact_number']}"
                    + (" and lowered needles." if lower_needles else ".")
                ),
                "timestamp_utc": self._api_timestamp_utc(),
                "contact": contact,
                "needles_lowered": lower_needles,
                "lifted_before_move": lift_before_move,
                "lifted_after": lift_after and needles_lowered,
                "needle_feedrate_mm_min": needle_feedrate,
            }
        except StageControllerError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
                "contact": contact,
            }
        finally:
            if active_stage_task:
                if lift_after and needles_lowered:
                    try:
                        self.stage_controller.run_external_needles_action(
                            "lift",
                            needle_feedrate,
                        )
                    except StageControllerError:
                        logger.exception("API contact move failed to lift needles.")
                self.stage_controller.finish_external_task()

    def _api_contact_needles(self, payload: dict[str, Any]) -> dict[str, Any]:
        contact_number = self._api_contact_number(payload)
        if contact_number is None:
            return {
                "accepted": False,
                "status_code": 400,
                "message": "Provide a positive contact_number.",
            }
        action = str(payload.get("action", "lower")).strip().lower()
        context_result = self._api_contact_context(contact_number)
        if not context_result.get("accepted", False):
            return context_result
        contact = context_result["contact"]
        if action == "raise":
            action = "raise"
        elif action in {"lift", "up"}:
            action = "lift"
        elif action in {"lower", "down"}:
            action = "lower"
        else:
            return {
                "accepted": False,
                "status_code": 400,
                "message": "Needle action must be lower, lift, or raise.",
            }
        needle_feedrate = self._api_needle_feedrate(payload)
        active_stage_task = False
        try:
            self.stage_controller.begin_external_task("API needle action")
            active_stage_task = True
            self.stage_controller.run_external_needles_action(action, needle_feedrate)
            return {
                "accepted": True,
                "message": f"Needle action '{action}' completed.",
                "timestamp_utc": self._api_timestamp_utc(),
                "contact": contact,
                "needle_action": action,
                "needle_feedrate_mm_min": needle_feedrate,
            }
        except StageControllerError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
                "contact": contact,
            }
        finally:
            if active_stage_task:
                self.stage_controller.finish_external_task()

    def _api_check_contact(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_measure_current_contact(payload, seek=False)

    def _api_route_contact_focus(self, payload: dict[str, Any]) -> dict[str, Any]:
        contact_number = self._api_contact_number(payload)
        if contact_number is None:
            return {
                "accepted": False,
                "status_code": 400,
                "message": "Provide a positive contact_number.",
            }
        context_result = self._api_contact_context(contact_number)
        if not context_result.get("accepted", False):
            return context_result
        contact = context_result["contact"]
        try:
            focus_range_mm = self._api_float(
                payload,
                "range_mm",
                "focus_range_mm",
                "photo_autofocus_range_mm",
                default=0.03,
                minimum=0.001,
            )
            focus_step_mm = self._api_optional_float(
                payload,
                "step_mm",
                "focus_step_mm",
                minimum=0.001,
            )
        except ValueError as exc:
            return {
                "accepted": False,
                "status_code": 400,
                "message": str(exc),
                "contact": contact,
            }

        active_stage_task = False
        try:
            self.stage_controller.begin_external_task("API route contact focus")
            active_stage_task = True
            needles_known = bool(getattr(self.stage_controller, "_needles_known", False))
            needles_up = bool(getattr(self.stage_controller, "_needles_up", False))
            needles_zone = getattr(self.stage_controller, "_needles_zone", None)
            if not (needles_known and needles_up and needles_zone == "raise"):
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": (
                        "Route contact focus requires fully raised needles "
                        "(known needle zone 'raise')."
                    ),
                    "contact": contact,
                    "needles_known": needles_known,
                    "needles_up": needles_up,
                    "needles_zone": needles_zone or "unknown",
                }
            result = self.stage_controller.run_external_local_autofocus(
                range_mm=focus_range_mm,
                step_mm=focus_step_mm,
            )
        except StageControllerError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
                "contact": contact,
            }
        finally:
            if active_stage_task:
                self.stage_controller.finish_external_task()
        return {
            "accepted": True,
            "message": str(result.summary()),
            "timestamp_utc": self._api_timestamp_utc(),
            "contact": contact,
            "focus_range_mm": focus_range_mm,
            "focus_step_mm": focus_step_mm,
            "focus": self._route_photo_focus_payload(result),
        }

    def _api_contact_seek(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_measure_current_contact(payload, seek=True)

    def _api_measure_current_contact(
        self,
        payload: dict[str, Any],
        *,
        seek: bool,
    ) -> dict[str, Any]:
        contact_number = self._api_contact_number(payload)
        if contact_number is None:
            return {
                "accepted": False,
                "status_code": 400,
                "message": "Provide a positive contact_number.",
            }
        context_result = self._api_contact_context(contact_number)
        if not context_result.get("accepted", False):
            return context_result
        point = context_result["point"]
        contact = context_result["contact"]

        connect_result = self._api_ensure_measurement_instrument_connected()
        if connect_result is not None:
            connect_result["contact"] = contact
            return connect_result

        try:
            check_sample_count = self._api_int(
                payload,
                "check_sample_count",
                "initial_measurement_count",
                "initial_samples",
                default=RouteMeasurementRunner.SHORT_CHECK_SAMPLE_COUNT,
                minimum=2,
            )
            measurement_count = self._api_int(
                payload,
                "measurement_count",
                "sample_count",
                "samples",
                default=check_sample_count,
                minimum=check_sample_count,
            )
            contact_seek_range_mm = self._api_float(
                payload,
                "contact_seek_range_mm",
                "contact_seek_max_total_mm",
                "seek_range_mm",
                default=RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM,
                minimum=0.0,
            )
            contact_seek_step_mm = self._api_float(
                payload,
                "contact_seek_step_mm",
                "seek_step_mm",
                default=abs(RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM),
                minimum=0.0,
            )
            contact_settle_s = self._api_float(
                payload,
                "contact_settle_s",
                "settle_s",
                default=RouteMeasurementRunner.DEFAULT_CONTACT_SETTLE_S,
                minimum=0.0,
            )
        except ValueError as exc:
            return {
                "accepted": False,
                "status_code": 400,
                "message": str(exc),
                "contact": contact,
            }
        max_relative_rms = None
        if any(
            key in payload
            for key in ("max_relative_rms", "max_rel_rms", "max_relative_rms_percent")
        ):
            try:
                if "max_relative_rms_percent" in payload:
                    max_relative_rms = (
                        self._api_float(
                            payload,
                            "max_relative_rms_percent",
                            default=math.nan,
                            minimum=0.0,
                        )
                        / 100.0
                    )
                else:
                    max_relative_rms = self._api_float(
                        payload,
                        "max_relative_rms",
                        "max_rel_rms",
                        default=math.nan,
                        minimum=0.0,
                    )
            except ValueError as exc:
                return {
                    "accepted": False,
                    "status_code": 400,
                    "message": str(exc),
                    "contact": contact,
                }

        needle_feedrate = self._api_needle_feedrate(payload)
        runner = RouteMeasurementRunner(
            points=[point],
            csv_path=Path(os.devnull),
            stage_controller=self.stage_controller,
            lcr_controller=self.lcr_controller,
            needle_feedrate=needle_feedrate,
            measurement_count=measurement_count,
            initial_measurement_count=check_sample_count,
            start_point_number=int(point.index),
            max_relative_rms=max_relative_rms,
            auto_contact_seek_on_bad_contact=seek,
            auto_contact_seek_step_mm=contact_seek_step_mm,
            auto_contact_seek_max_total_mm=contact_seek_range_mm,
            contact_settle_s=contact_settle_s,
            operation_mode=ROUTE_OPERATION_MEASURE,
            status_callback=self.route_measurement_status.emit,
        )
        try:
            result = (
                runner.seek_contact(point)
                if seek
                else runner.check_contact(point)
            )
        except (StageControllerError, LCRMeterError, RuntimeError) as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
                "contact": contact,
            }
        except Exception as exc:
            prefix = "Contact seek failed" if seek else "Contact check failed"
            logger.exception("API %s.", prefix.lower())
            return self._api_instrument_exception_response(
                prefix,
                exc,
                contact=contact,
            )
        record = result.record
        response = {
            "accepted": True,
            "message": result.message,
            "timestamp_utc": self._api_timestamp_utc(),
            "contact": contact,
            "needle_feedrate_mm_min": needle_feedrate,
            "contact_ok": bool(result.success),
            "check_sample_count": check_sample_count,
            "measurement_count": measurement_count,
            "contact_settle_s": contact_settle_s,
            "contact_seek_range_mm": contact_seek_range_mm,
            "contact_seek_step_mm": contact_seek_step_mm,
            "measurement": self._api_route_measurement_record_payload(record),
            "contact_seek": self._api_contact_seek_payload(result.contact_seek),
        }
        if seek:
            contact_seek = result.contact_seek
            response["contact_found"] = bool(
                result.success
                or (
                    contact_seek is not None
                    and bool(getattr(contact_seek, "found", False))
                )
            )
        return response

    def _api_ensure_measurement_instrument_connected(self) -> dict[str, Any] | None:
        if self.lcr_controller.is_connected():
            return None
        connector = getattr(self.lcr_controller, "connect_now", None)
        if not callable(connector):
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Measurement instrument is not connected.",
            }
        try:
            connector()
        except LCRMeterError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        except Exception as exc:
            logger.exception("API measurement instrument connection failed.")
            return self._api_instrument_exception_response(
                "Measurement instrument connection failed",
                exc,
            )
        if not self.lcr_controller.is_connected():
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Measurement instrument is not connected.",
            }
        return None

    def _api_prepare_route_meter_controller(
        self,
        configuration: RouteMeterConfiguration,
        *,
        prefix: str = "Measurement instrument setup failed",
    ) -> dict[str, Any] | None:
        wait_until_idle = getattr(self.lcr_controller, "wait_until_idle", None)
        if callable(wait_until_idle):
            try:
                ready = bool(wait_until_idle(45.0))
            except Exception as exc:
                logger.exception("API measurement instrument wait failed.")
                return self._api_instrument_exception_response(
                    "Measurement instrument wait failed",
                    exc,
                )
            if not ready:
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": "Measurement instrument task is still running.",
                }
        if not self.lcr_controller.is_connected():
            runtime_config = getattr(
                self.lcr_controller,
                "apply_route_meter_runtime_configuration",
                None,
            )
            if callable(runtime_config):
                runtime_config(configuration)
            connect_result = self._api_ensure_measurement_instrument_connected()
            if connect_result is not None:
                return connect_result
        try:
            self.lcr_controller.apply_route_meter_configuration(configuration)
        except LCRMeterError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        except Exception as exc:
            logger.exception("%s.", prefix)
            return self._api_instrument_exception_response(prefix, exc)
        return None

    @staticmethod
    def _api_instrument_exception_response(
        prefix: str,
        exc: Exception,
        **extra: Any,
    ) -> dict[str, Any]:
        message = str(exc).strip()
        response: dict[str, Any] = {
            "accepted": False,
            "status_code": 409,
            "message": f"{prefix}: {message}" if message else prefix,
            "error_type": type(exc).__name__,
        }
        response.update(extra)
        return response

    def _api_configure_meter(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            configuration = self._api_route_meter_configuration(
                payload,
                voltages_v=None,
            )
        except ValueError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        setup_result = self._api_prepare_route_meter_controller(
            configuration,
            prefix="Measurement instrument setup failed",
        )
        if setup_result is not None:
            return setup_result
        return {
            "accepted": True,
            "message": "Measurement instrument configured.",
            "timestamp_utc": self._api_timestamp_utc(),
            "meter_type": configuration.meter_type,
            "nplc": configuration.nplc_label(),
        }

    def _api_raw_voltage_sweep(self, payload: dict[str, Any]) -> dict[str, Any]:
        voltages = payload.get("voltages_v")
        if not isinstance(voltages, list) or not voltages:
            return {
                "accepted": False,
                "status_code": 400,
                "message": "Provide voltages_v as a non-empty array.",
            }
        try:
            voltage_values = [float(value) for value in voltages]
            configuration = self._api_route_meter_configuration(
                payload.get("meter", payload.get("meter_configuration", {})),
                voltages_v=voltage_values,
            )
        except (TypeError, ValueError) as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        setup_result = self._api_prepare_route_meter_controller(
            configuration,
            prefix="Measurement instrument setup failed",
        )
        if setup_result is not None:
            return setup_result

        contact_number = self._api_contact_number(payload, required=False)
        move_to_contact = self._api_bool(
            payload,
            "move_to_contact",
            "move",
            default=contact_number is not None,
        )
        lower_needles = self._api_bool(
            payload,
            "lower_needles",
            "lower",
            default=contact_number is not None,
        )
        lift_after = self._api_bool(payload, "lift_after", default=lower_needles)
        lift_before_move = self._api_bool(
            payload,
            "lift_before_move",
            default=move_to_contact,
        )
        contact_settle_s = self._api_float(
            payload,
            "contact_settle_s",
            "settle_s",
            default=0.2,
            minimum=0.0,
        )
        point: RouteMeasurementPoint | None = None
        contact: dict[str, Any] | None = None
        if contact_number is not None:
            context_result = self._api_contact_context(contact_number)
            if not context_result.get("accepted", False):
                return context_result
            point = context_result["point"]
            contact = context_result["contact"]
        if move_to_contact and point is None:
            return {
                "accepted": False,
                "status_code": 400,
                "message": "move_to_contact requires contact_number.",
            }

        needle_feedrate = self._api_needle_feedrate(payload)
        active_stage_task = False
        needles_lowered = False
        started_at = time.monotonic()
        timestamp_utc = self._api_timestamp_utc()
        try:
            if move_to_contact or lower_needles or lift_after:
                self.stage_controller.begin_external_task("API raw voltage sweep")
                active_stage_task = True
            if active_stage_task and lift_before_move:
                self.stage_controller.run_external_needles_action(
                    "lift",
                    needle_feedrate,
                )
            if move_to_contact and point is not None:
                self.stage_controller.run_external_move_to_xy(
                    point.stage_xy[0],
                    point.stage_xy[1],
                )
            if active_stage_task and lower_needles:
                self.stage_controller.run_external_needles_action(
                    "lower",
                    needle_feedrate,
                )
                needles_lowered = True
                if contact_settle_s > 0.0:
                    time.sleep(contact_settle_s)
            raw_measurement = self.lcr_controller.read_voltage_sweep_now(voltage_values)
            elapsed_s = time.monotonic() - started_at
            result = self._api_json_ready(raw_measurement)
            points = result.get("points", [])
            if isinstance(points, list):
                iv_pairs = [
                    {
                        "voltage_v": item.get("measured_voltage_v"),
                        "current_a": item.get("current_a"),
                    }
                    for item in points
                    if isinstance(item, dict)
                ]
            else:
                iv_pairs = []
            return {
                "accepted": True,
                "message": f"Raw voltage sweep complete: {len(voltage_values)} points.",
                "timestamp_utc": timestamp_utc,
                "elapsed_s": elapsed_s,
                "contact": contact,
                "meter_type": configuration.meter_type,
                "measurement_kind": "voltage_sweep",
                "voltages_v": voltage_values,
                "iv_pairs": iv_pairs,
                "result": result,
                "needles_lowered": lower_needles,
                "lifted_after": lift_after and needles_lowered,
            }
        except (StageControllerError, LCRMeterError) as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
                "contact": contact,
            }
        except Exception as exc:
            logger.exception("API raw voltage sweep failed.")
            return self._api_instrument_exception_response(
                "Raw voltage sweep failed",
                exc,
                contact=contact,
            )
        finally:
            if active_stage_task:
                if lift_after and needles_lowered:
                    try:
                        self.stage_controller.run_external_needles_action(
                            "lift",
                            needle_feedrate,
                        )
                    except StageControllerError:
                        logger.exception("API raw voltage sweep failed to lift needles.")
                self.stage_controller.finish_external_task()

    def _api_visa_list_resources(self) -> dict[str, Any]:
        controller = self._api_visa_controller()
        if controller is self.lcr_controller:
            connect_result = self._api_ensure_measurement_instrument_connected()
            if connect_result is not None:
                return connect_result
        try:
            roles_getter = getattr(controller, "visa_resource_roles", None)
            if not callable(roles_getter):
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": "Measurement instrument does not expose VISA roles.",
                }
            roles = dict(roles_getter())
        except LCRMeterError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        except Exception as exc:
            logger.exception("API VISA resource listing failed.")
            return self._api_instrument_exception_response(
                "VISA resource listing failed",
                exc,
            )
        resources = sorted(
            (dict(item) for item in roles.values()),
            key=lambda item: str(item.get("role") or ""),
        )
        return {
            "accepted": True,
            "meter_type": self._api_visa_meter_type(resources),
            "resources": resources,
        }

    def _api_visa_operation(self, payload: dict[str, Any]) -> dict[str, Any]:
        role = str(payload.get("role", "")).strip()
        operation = str(payload.get("operation", "")).strip().lower()
        if not role:
            return {
                "accepted": False,
                "status_code": 400,
                "message": "VISA role is required.",
            }
        if operation not in {"write", "query", "ask", "read", "read_raw", "clear"}:
            return {
                "accepted": False,
                "status_code": 400,
                "message": f"Unsupported VISA operation: {operation}.",
            }
        controller = self._api_visa_controller()
        if controller is self.lcr_controller:
            connect_result = self._api_ensure_measurement_instrument_connected()
            if connect_result is not None:
                return connect_result
        timeout_ms = self._api_optional_timeout_ms(payload)
        command_value = payload.get("command", payload.get("query"))
        command = None if command_value is None else str(command_value)
        read_termination = (
            str(payload.get("read_termination"))
            if payload.get("read_termination") is not None
            else None
        )
        write_termination = (
            str(payload.get("write_termination"))
            if payload.get("write_termination") is not None
            else None
        )
        try:
            operation_runner = getattr(controller, "visa_operation", None)
            if not callable(operation_runner):
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": "Measurement instrument does not expose VISA operations.",
                }
            result = operation_runner(
                role,
                operation,
                command=command,
                timeout_ms=timeout_ms,
                read_termination=read_termination,
                write_termination=write_termination,
            )
        except LCRMeterError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        except Exception as exc:
            logger.exception("API VISA operation failed.")
            return self._api_instrument_exception_response(
                "VISA operation failed",
                exc,
                role=role,
                operation=operation,
            )
        response: dict[str, Any] = {
            "accepted": True,
            "role": role,
            "operation": operation,
            "timestamp_utc": self._api_timestamp_utc(),
        }
        if isinstance(result, (bytes, bytearray)):
            response["data"] = bytes(result)
            response["size_bytes"] = len(result)
        elif result is not None:
            response["response"] = str(result)
        return response

    def _api_visa_controller(self) -> object:
        if (
            self._route_measurement_runner is not None
            and self._api_route_lcr_controller is not None
        ):
            return self._api_route_lcr_controller
        return self.lcr_controller

    @staticmethod
    def _api_visa_meter_type(resources: list[dict[str, object]]) -> str:
        for item in resources:
            meter_type = item.get("meter_type")
            if meter_type:
                return str(meter_type)
        return ""

    @staticmethod
    def _api_optional_timeout_ms(payload: dict[str, Any]) -> int | None:
        value = payload.get("timeout_ms", payload.get("timeout"))
        if value is None:
            return None
        try:
            timeout = int(float(value))
        except (TypeError, ValueError):
            return None
        return timeout if timeout > 0 else None

    def _api_start_route_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        thread = self._route_measurement_thread
        if thread is not None and thread.is_alive():
            runner = self._route_measurement_runner
            if isinstance(runner, RouteExternalMeasurementSessionRunner):
                return runner.status_payload()
            can_take_over_waiting_gui_runner = (
                isinstance(runner, RouteMeasurementRunner)
                and self._route_measurement_waiting
                and self._last_route_measurement_result is None
            )
            if not can_take_over_waiting_gui_runner:
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": "Route measurement is already active.",
                }
            route_offset_xy = runner.route_offset_xy()
            runner.stop()
            thread.join(timeout=2.0)
            if thread.is_alive():
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": "Waiting GUI route measurement did not stop.",
                }
            self._route_measurement_thread = None
            self._route_measurement_runner = None
            self._route_measurement_waiting = False
            self._route_measurement_session_active = False
        else:
            route_offset_xy = (0.0, 0.0)
        if self.serial_connection is None or not self.serial_connection.is_open:
            return {
                "accepted": False,
                "status_code": 503,
                "message": "Serial connection is not available.",
            }
        route = self._design_session.route
        if route is None or not route.points:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Create or load a probe route before starting a route session.",
            }
        registration = self._design_session.registration
        if registration is None or not registration.valid:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Design registration is required before using contacts.",
            }
        try:
            points = self._route_measurement_points(route)
        except DesignModelError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        if not points:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Route has no enabled points.",
            }
        try:
            start_point = self._api_int(
                payload,
                "start_point",
                "current_point",
                "contact_number",
                default=int(self._route_measurement_current_point or 1),
                minimum=1,
            )
            initial_count = self._api_int(
                payload,
                "initial_measurement_count",
                "initial_samples",
                "check_sample_count",
                default=10,
                minimum=1,
            )
            followup_count = self._api_int(
                payload,
                "followup_measurement_count",
                "followup_samples",
                default=240,
                minimum=0,
            )
            measurement_count = self._api_int(
                payload,
                "measurement_count",
                "sample_count",
                "samples",
                default=initial_count + followup_count,
                minimum=initial_count,
            )
            contact_seek_range_mm = self._api_float(
                payload,
                "contact_seek_range_mm",
                "contact_seek_max_total_mm",
                "seek_range_mm",
                default=RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM,
                minimum=0.0,
            )
            contact_seek_step_mm = self._api_float(
                payload,
                "contact_seek_step_mm",
                "seek_step_mm",
                default=abs(RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM),
                minimum=0.0,
            )
            contact_settle_s = self._api_float(
                payload,
                "contact_settle_s",
                "settle_s",
                default=RouteMeasurementRunner.DEFAULT_CONTACT_SETTLE_S,
                minimum=0.0,
            )
            photo_settle_s = self._api_float(
                payload,
                "photo_settle_s",
                default=0.2,
                minimum=0.0,
            )
            photo_enabled = self._api_bool(
                payload,
                "photo_enabled",
                "photo",
                default=True,
            )
            photo_focus_enabled = self._api_bool(
                payload,
                "photo_autofocus_enabled",
                "autofocus",
                "focus",
                default=True,
            )
            photo_focus_range_mm = self._api_float(
                payload,
                "photo_autofocus_range_mm",
                "focus_range_mm",
                default=0.03,
                minimum=0.001,
            )
        except ValueError as exc:
            return {
                "accepted": False,
                "status_code": 400,
                "message": str(exc),
            }
        max_relative_rms = None
        if any(
            key in payload
            for key in ("max_relative_rms", "max_rel_rms", "max_relative_rms_percent")
        ):
            try:
                if "max_relative_rms_percent" in payload:
                    max_relative_rms = (
                        self._api_float(
                            payload,
                            "max_relative_rms_percent",
                            default=math.nan,
                            minimum=0.0,
                        )
                        / 100.0
                    )
                else:
                    max_relative_rms = self._api_float(
                        payload,
                        "max_relative_rms",
                        "max_rel_rms",
                        default=math.nan,
                        minimum=0.0,
                    )
            except ValueError as exc:
                return {
                    "accepted": False,
                    "status_code": 400,
                    "message": str(exc),
                }
        selected_point = self._api_find_contact_point(points, start_point)
        if selected_point is None:
            return {
                "accepted": False,
                "status_code": 409,
                "message": (
                    f"Contact {start_point} is not enabled or not included "
                    "by the current route filter."
                ),
            }
        try:
            meter_configuration = self._api_route_meter_configuration(
                payload.get("meter", payload.get("meter_configuration", {})),
                voltages_v=None,
            )
        except ValueError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        setup_result = self._api_prepare_route_meter_controller(
            meter_configuration,
            prefix="Route measurement instrument setup failed",
        )
        if setup_result is not None:
            return setup_result
        route_lcr_controller: object = self.lcr_controller
        session_id = uuid.uuid4().hex
        with self._api_route_artifacts_lock:
            self._api_route_artifacts.clear()
        self._api_route_session_id = session_id
        self._api_route_lcr_controller = route_lcr_controller
        self._api_route_last_status = None
        runner = RouteExternalMeasurementSessionRunner(
            session_id=session_id,
            points=points,
            stage_controller=self.stage_controller,
            lcr_controller=route_lcr_controller,
            needle_feedrate=self._current_needle_feedrate(),
            measurement_count=measurement_count,
            initial_measurement_count=initial_count,
            start_point_number=int(selected_point.index),
            max_relative_rms=max_relative_rms,
            auto_contact_seek_step_mm=contact_seek_step_mm,
            auto_contact_seek_max_total_mm=contact_seek_range_mm,
            contact_settle_s=contact_settle_s,
            nplc_label=meter_configuration.nplc_label(),
            measurement_type=meter_configuration.measurement_type_label(),
            status_callback=self.route_measurement_status.emit,
            progress_callback=self.route_measurement_progress.emit,
            photo_callback=self._capture_api_route_photo_artifact,
            photo_focus_callback=lambda point, position, total: self._api_route_photo_autofocus(
                point,
                position,
                total,
                range_mm=photo_focus_range_mm,
            ),
            contact_photo_callback=self._capture_route_contact_photo,
            pre_contact_photo_callback=self._capture_route_pre_contact_photo,
            result_callback=self.route_measurement_result.emit,
            waiting_callback=self.route_measurement_waiting_changed.emit,
            photo_enabled=photo_enabled,
            photo_focus_enabled=photo_focus_enabled,
            photo_settle_s=photo_settle_s,
            wait_before_first_point=True,
        )
        runner.set_route_offset_xy(route_offset_xy)
        self._route_measurement_runner = runner
        self._route_measurement_waiting = False
        self._pending_route_measure_point = None
        self._route_measurement_session_active = True
        self._route_measurement_photo_enabled = photo_enabled
        self._route_measurement_measure_enabled = True
        self._route_measurement_point_numbers = [int(point.index) for point in points]
        self._last_telegram_attention_message = ""
        with self._telegram_photo_lock:
            self._telegram_pending_contact_photo = None
            self._telegram_pending_contact_before_photo = None
            self._last_route_pre_contact_photo = None
            self._last_route_contact_failure_photo = None
            self._last_route_contact_failure_before_photo = None
        self._route_measurement_thread = threading.Thread(
            target=self._run_route_measurement,
            args=(runner,),
            name="RouteApiExternalSession",
            daemon=True,
        )
        start_message = (
            "Route API session ready at "
            f"point {int(selected_point.index)} {selected_point.label}; "
            f"{len(points)} points selected."
        )
        self._last_route_measurement_result = None
        self._route_measurement_thread.start()
        if not runner.wait_until_initial_pause(timeout_s=10.0):
            status = runner.status_payload()
            if self._route_measurement_thread.is_alive():
                runner.stop()
                self._route_measurement_thread.join(timeout=2.0)
            self._route_measurement_thread = None
            self._route_measurement_runner = None
            self._api_route_lcr_controller = None
            self._route_measurement_waiting = False
            self._route_measurement_session_active = False
            return {
                "accepted": False,
                "status_code": 409,
                "message": (
                    status.get("message")
                    or "Route API session did not reach initial pause."
                ),
                "status": status,
            }
        self._route_measurement_waiting = True
        self.route_measurement_started.emit(
            start_message,
            len(points),
            int(selected_point.index),
            True,
        )
        self._send_telegram_alert(
            "route_started",
            f"Probe route API session started:\n{start_message}",
        )
        return runner.status_payload()

    def _show_route_measurement_dialog_for_api_session(self) -> None:
        try:
            self._open_route_measurement_dialog(start_context=False)
        except Exception:
            logger.exception("Failed to open route measurement controls for API session.")

    def _api_route_session_status(self) -> dict[str, Any]:
        runner = self._route_measurement_runner
        if runner is not None and hasattr(runner, "status_payload"):
            status = runner.status_payload()
        elif self._api_route_last_status is not None:
            status = dict(self._api_route_last_status)
        else:
            return {
                "accepted": False,
                "status_code": 404,
                "message": "No API route session is active.",
            }
        status["artifacts"] = self._api_route_artifacts_payload()
        return status

    def _api_route_session_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        runner = self._route_measurement_runner
        if runner is None:
            return {
                "accepted": False,
                "status_code": 404,
                "message": "No route session is active.",
            }
        action = str(payload.get("action", payload.get("command", "next"))).strip()
        if action.lower() == "pause":
            runner.request_pause_after_current_point()
            return {
                "accepted": True,
                "message": "Route session pause requested.",
                "action": "pause",
            }
        if action.lower() == "interrupt":
            self._interrupt_route_measurement_runner(
                runner,
                reason="Route API session interrupt requested.",
            )
            return {
                "accepted": True,
                "message": "Route session interrupt requested.",
                "action": "interrupt",
            }
        if action.lower() == "stop":
            runner.stop()
            return {
                "accepted": True,
                "message": "Route session stop requested.",
                "action": "stop",
            }
        if not runner.submit_confirmation(action):
            return {
                "accepted": False,
                "status_code": 400,
                "message": "Unknown route session action.",
            }
        return {
            "accepted": True,
            "message": f"Route session action submitted: {action}.",
            "action": action,
        }

    def _api_route_session_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        runner = self._route_measurement_runner
        if runner is None or not hasattr(runner, "submit_external_result"):
            return {
                "accepted": False,
                "status_code": 404,
                "message": "No external route session is waiting for a result.",
            }
        result = {
            "status": str(payload.get("status", "ok")).strip().lower() or "ok",
            "summary": payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
            "files": payload.get("files") if isinstance(payload.get("files"), list) else [],
            "message": str(payload.get("message", "")).strip(),
            "timestamp_utc": self._api_timestamp_utc(),
        }
        request_id = payload.get("external_measurement_request_id")
        if request_id is None:
            request_id = payload.get("request_id")
        if request_id is not None:
            result["external_measurement_request_id"] = request_id
        if not runner.submit_external_result(result):
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Route session is not waiting for an external result.",
            }
        return {
            "accepted": True,
            "message": "External result submitted.",
            "result": result,
        }

    def _api_route_session_seek(self) -> dict[str, Any]:
        runner = self._route_measurement_runner
        if runner is None or not hasattr(runner, "request_contact_seek"):
            return {
                "accepted": False,
                "status_code": 404,
                "message": "No route session is active.",
            }
        if not runner.request_contact_seek():
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Route session is not waiting for contact seek.",
            }
        return {
            "accepted": True,
            "message": "Contact seek requested for current route contact.",
        }

    def _api_route_session_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        artifact_id = str(payload.get("artifact_id", "")).strip()
        with self._api_route_artifacts_lock:
            artifact = dict(self._api_route_artifacts.get(artifact_id) or {})
        if not artifact:
            return {
                "accepted": False,
                "status_code": 404,
                "message": "Route session artifact was not found.",
            }
        return {
            "accepted": True,
            "artifact_id": artifact_id,
            **artifact,
        }

    def _api_route_artifacts_payload(self) -> list[dict[str, object]]:
        with self._api_route_artifacts_lock:
            artifacts = [
                {
                    key: value
                    for key, value in artifact.items()
                    if key != "data"
                }
                for artifact in self._api_route_artifacts.values()
            ]
        return artifacts

    def _add_api_route_artifact(
        self,
        *,
        data: bytes,
        filename: str,
        content_type: str,
        kind: str,
        metadata: dict[str, object],
    ) -> str:
        artifact_id = uuid.uuid4().hex
        artifact = {
            "artifact_id": artifact_id,
            "filename": filename,
            "content_type": content_type,
            "kind": kind,
            "metadata": dict(metadata),
            "created_at_utc": self._api_timestamp_utc(),
            "size_bytes": len(data),
            "data": bytes(data),
        }
        with self._api_route_artifacts_lock:
            self._api_route_artifacts[artifact_id] = artifact
        return artifact_id

    def _capture_api_route_photo_artifact(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        focus_result: object | None,
    ) -> str:
        before_counter = self._latest_camera_counter()
        frame, _counter = self._wait_for_camera_frame(
            after_counter=before_counter,
            timeout_s=2.0,
        )
        photo = self._qimage_telegram_photo(frame) or self._latest_camera_frame_photo()
        if photo is None:
            raise RuntimeError("Camera frame is unavailable.")
        photo_bytes, photo_name = photo
        artifact_id = self._add_api_route_artifact(
            data=photo_bytes,
            filename=photo_name,
            content_type="image/jpeg" if photo_name.lower().endswith(".jpg") else "image/png",
            kind="route_photo",
            metadata={
                "position": int(position),
                "total": int(total),
                "point_index": int(point.index),
                "contact_number": self._api_structure_number_for_measurement_point(point),
                "label": point.label,
                "focus": self._route_photo_focus_payload(focus_result),
            },
        )
        with self._telegram_photo_lock:
            route_photo_requested = self._telegram_route_photo_requested
            if route_photo_requested:
                self._telegram_route_photo_requested = False
        if route_photo_requested:
            self._send_telegram_bot_message(
                "Next route structure photo:\n"
                f"Point {position}/{total}, structure "
                f"{self._api_structure_number_for_measurement_point(point)}, "
                f"{point.label}.",
                photo=(photo_bytes, photo_name),
                reply_markup=self._telegram_default_markup(),
            )
        return artifact_id

    def _api_route_photo_autofocus(
        self,
        _point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        range_mm: float,
    ) -> object:
        self.route_measurement_status.emit(
            "Route photo autofocus: "
            f"point {position}/{total}, +/-{range_mm:.3f} mm."
        )
        return self.stage_controller.run_external_local_autofocus(
            range_mm=range_mm,
        )

    def _api_contact_context(self, contact_number: int) -> dict[str, Any]:
        if self.serial_connection is None or not self.serial_connection.is_open:
            return {
                "accepted": False,
                "status_code": 503,
                "message": "Serial connection is not available.",
            }
        route = self._design_session.route
        if route is None or not route.points:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Create or load a probe route before using contacts.",
            }
        registration = self._design_session.registration
        if registration is None or not registration.valid:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Design registration is required before using contacts.",
            }
        try:
            points = self._route_measurement_points(route)
        except DesignModelError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        point = self._api_find_contact_point(points, contact_number)
        if point is None:
            return {
                "accepted": False,
                "status_code": 404,
                "message": f"Contact {contact_number} is not enabled or not found.",
            }
        return {
            "accepted": True,
            "point": point,
            "contact": self._api_measurement_point_payload(
                point,
                requested_contact_number=contact_number,
            ),
        }

    def _api_find_contact_point(
        self,
        points: list[RouteMeasurementPoint],
        contact_number: int,
    ) -> RouteMeasurementPoint | None:
        for point in points:
            if int(point.index) == int(contact_number):
                return point
        for point in points:
            if self._api_structure_number_for_measurement_point(point) == int(contact_number):
                return point
        return None

    def _api_route_point_payload(
        self,
        *,
        route_index: int,
        route_point: object,
        include_stage_xy: bool,
    ) -> dict[str, Any]:
        design_center = tuple(getattr(route_point, "camera_center", (0.0, 0.0)))
        stage_xy = None
        if include_stage_xy:
            try:
                resolved = self._raw_stage_xy_from_design_xy(
                    (float(design_center[0]), float(design_center[1]))
                )
            except (TypeError, ValueError, IndexError):
                resolved = None
            if resolved is not None:
                stage_xy = {"x_mm": float(resolved[0]), "y_mm": float(resolved[1])}
        return {
            "contact_number": int(route_index),
            "structure_number": self._api_structure_number_for_route_point(
                route_index,
                route_point,
            ),
            "point_id": str(getattr(route_point, "id", "")),
            "label": str(getattr(route_point, "label", "")),
            "enabled": bool(getattr(route_point, "enabled", True)),
            "design_center": {
                "x": float(design_center[0]),
                "y": float(design_center[1]),
            },
            "stage_xy": stage_xy,
        }

    def _api_measurement_point_payload(
        self,
        point: RouteMeasurementPoint,
        *,
        requested_contact_number: int,
    ) -> dict[str, Any]:
        return {
            "contact_number": int(requested_contact_number),
            "route_index": int(point.index),
            "structure_number": self._api_structure_number_for_measurement_point(point),
            "point_id": point.point_id,
            "label": point.label,
            "design_center": {
                "x": float(point.design_center[0]),
                "y": float(point.design_center[1]),
            },
            "stage_xy": {
                "x_mm": float(point.stage_xy[0]),
                "y_mm": float(point.stage_xy[1]),
            },
            "needle_contacts": [
                {
                    "needle": 1,
                    "design": {
                        "x": float(point.needle_1_design[0]),
                        "y": float(point.needle_1_design[1]),
                    },
                },
                {
                    "needle": 2,
                    "design": {
                        "x": float(point.needle_2_design[0]),
                        "y": float(point.needle_2_design[1]),
                    },
                },
            ],
        }

    def _api_route_meter_configuration(
        self,
        payload: object,
        *,
        voltages_v: list[float] | None,
    ) -> RouteMeterConfiguration:
        meter_payload = payload if isinstance(payload, dict) else {}
        meter_type = self._api_meter_type(meter_payload.get("meter_type", meter_payload.get("type")))
        if meter_type is None:
            meter_type = self.lcr_controller.meter_type()
        if meter_type == ROUTE_METER_KEITHLEY:
            keithley_payload = meter_payload.get("keithley")
            if not isinstance(keithley_payload, dict):
                keithley_payload = meter_payload
            range_payload = dict(keithley_payload)
            nested_ranges = keithley_payload.get("ranges")
            if isinstance(nested_ranges, dict):
                range_payload.update(nested_ranges)
            defaults = KeithleyRouteMeterSettings()
            max_voltage = (
                max(abs(float(value)) for value in voltages_v)
                if voltages_v
                else defaults.measurement_voltage_v
            )
            measurement_voltage = self._api_float(
                range_payload,
                "measurement_voltage_v",
                "voltage_v",
                default=max(max_voltage, 1e-12),
                minimum=1e-12,
            )
            range_mode = str(
                range_payload.get(
                    "range_mode",
                    range_payload.get("mode", defaults.range_mode),
                )
            )
            voltage_range = self._api_optional_float(
                range_payload,
                "voltage_range_v",
                minimum=1e-12,
            )
            source_voltage_range = self._api_optional_float(
                range_payload,
                "source_voltage_range_v",
                "source_range_v",
                "keithley_source_voltage_range_v",
                minimum=1e-12,
            )
            voltmeter_range = self._api_optional_float(
                range_payload,
                "voltmeter_range_v",
                "meter_voltage_range_v",
                "nanovoltmeter_range_v",
                "keithley_voltmeter_range_v",
                minimum=1e-12,
            )
            if voltage_range is not None:
                source_voltage_range = voltage_range
                voltmeter_range = voltage_range
            if (
                voltage_range is None
                and source_voltage_range is None
                and voltmeter_range is None
                and range_mode.strip().lower() not in {
                "code_auto",
                "auto",
                "software_auto",
                "computed_auto",
                }
            ):
                voltage_range = max(measurement_voltage, max_voltage)
                source_voltage_range = voltage_range
                voltmeter_range = voltage_range
            settings = KeithleyRouteMeterSettings(
                measurement_voltage_v=measurement_voltage,
                range_mode=range_mode,
                expected_resistance_ohm=self._api_optional_float(
                    range_payload,
                    "expected_resistance_ohm",
                    "resistance_ohm",
                    minimum=1e-12,
                ),
                minimum_resistance_ohm=self._api_optional_float(
                    range_payload,
                    "minimum_resistance_ohm",
                    "min_resistance_ohm",
                    "resistance_floor_ohm",
                    minimum=1e-12,
                ),
                maximum_current_a=self._api_optional_float(
                    range_payload,
                    "maximum_current_a",
                    "max_current_a",
                    "current_limit_a",
                    minimum=1e-12,
                ),
                voltage_range_v=voltage_range,
                source_voltage_range_v=(
                    source_voltage_range
                    if source_voltage_range is not None
                    else defaults.source_voltage_range_v
                ),
                voltmeter_range_v=(
                    voltmeter_range
                    if voltmeter_range is not None
                    else defaults.voltmeter_range_v
                ),
                current_range_a=self._api_float(
                    range_payload,
                    "current_range_a",
                    default=defaults.current_range_a,
                    minimum=1e-12,
                ),
                compliance_current_a=self._api_float(
                    range_payload,
                    "compliance_current_a",
                    "current_limit_a",
                    "max_current_a",
                    default=defaults.compliance_current_a,
                    minimum=1e-12,
                ),
                range_voltage_headroom=self._api_float(
                    range_payload,
                    "range_voltage_headroom",
                    "voltage_headroom",
                    default=defaults.range_voltage_headroom,
                    minimum=1.0,
                ),
                range_current_headroom=self._api_float(
                    range_payload,
                    "range_current_headroom",
                    "current_headroom",
                    default=defaults.range_current_headroom,
                    minimum=1.0,
                ),
                nplc=self._api_float(
                    keithley_payload,
                    "nplc",
                    default=defaults.nplc,
                    minimum=0.01,
                ),
                terminals=str(
                    keithley_payload.get(
                        "terminals",
                        defaults.terminals,
                    )
                ),
                trigger_delay_s=self._api_float(
                    keithley_payload,
                    "trigger_delay_s",
                    "delay_s",
                    default=defaults.trigger_delay_s,
                    minimum=0.0,
                ),
            )
            return RouteMeterConfiguration(
                meter_type=ROUTE_METER_KEITHLEY,
                keithley=settings,
            )
        if meter_type == ROUTE_METER_GWINSTEK:
            gw_payload = meter_payload.get("gwinstek")
            if not isinstance(gw_payload, dict):
                gw_payload = meter_payload
            defaults = GWInstekRouteMeterSettings(
                resource_name=self.settings_manager.needle_calibration_configuration().visa_resource
            )
            settings = GWInstekRouteMeterSettings(
                resource_name=str(gw_payload.get("resource_name", defaults.resource_name)),
                measurement_function=str(
                    gw_payload.get("measurement_function", defaults.measurement_function)
                ),
                range_mode=str(gw_payload.get("range_mode", defaults.range_mode)),
                impedance_range=int(
                    self._api_float(
                        gw_payload,
                        "impedance_range",
                        default=defaults.impedance_range,
                    )
                ),
                dcr_range=int(
                    self._api_float(gw_payload, "dcr_range", default=defaults.dcr_range)
                ),
                frequency_hz=self._api_float(
                    gw_payload,
                    "frequency_hz",
                    default=defaults.frequency_hz,
                    minimum=10.0,
                ),
                level_mode=str(gw_payload.get("level_mode", defaults.level_mode)),
                voltage_level_v=self._api_float(
                    gw_payload,
                    "voltage_level_v",
                    default=defaults.voltage_level_v,
                    minimum=0.0,
                ),
                current_level_a=self._api_float(
                    gw_payload,
                    "current_level_a",
                    default=defaults.current_level_a,
                    minimum=0.0,
                ),
                source_resistance_ohm=int(
                    self._api_float(
                        gw_payload,
                        "source_resistance_ohm",
                        default=defaults.source_resistance_ohm,
                    )
                ),
                aperture_rate=str(gw_payload.get("aperture_rate", defaults.aperture_rate)),
                aperture_averages=int(
                    self._api_float(
                        gw_payload,
                        "aperture_averages",
                        default=defaults.aperture_averages,
                        minimum=1.0,
                    )
                ),
                trigger_delay_s=self._api_float(
                    gw_payload,
                    "trigger_delay_s",
                    default=defaults.trigger_delay_s,
                    minimum=0.0,
                ),
                bias_enabled=self._api_bool(gw_payload, "bias_enabled", default=defaults.bias_enabled),
                bias_level_v=self._api_float(
                    gw_payload,
                    "bias_level_v",
                    default=defaults.bias_level_v,
                ),
                monitor1=str(gw_payload.get("monitor1", defaults.monitor1)),
                monitor2=str(gw_payload.get("monitor2", defaults.monitor2)),
                alc_enabled=self._api_bool(gw_payload, "alc_enabled", default=defaults.alc_enabled),
            )
            return RouteMeterConfiguration(
                meter_type=ROUTE_METER_GWINSTEK,
                gwinstek=settings,
            )
        raise ValueError(f"Unsupported meter_type: {meter_type!r}")

    def _api_meter_type(self, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip().lower()
        if text in {"", "current", "configured"}:
            return None
        if text in {"keithley", "keithley_2400_2182a", "2400_2182a"}:
            return ROUTE_METER_KEITHLEY
        if text in {"gwinstek", "lcr", "gwinstek_lcr_76200"}:
            return ROUTE_METER_GWINSTEK
        return text

    def _api_contact_number(
        self,
        payload: dict[str, Any],
        *,
        required: bool = True,
    ) -> int | None:
        for key in ("contact_number", "contact", "point_number", "structure_number"):
            if key not in payload:
                continue
            try:
                value = int(payload[key])
            except (TypeError, ValueError):
                return None
            return value if value > 0 else None
        return None if not required else None

    def _api_needle_feedrate(self, payload: dict[str, Any]) -> float | None:
        for key in ("needle_feedrate_mm_min", "feedrate_mm_min", "feedrate"):
            if key not in payload or payload.get(key) is None:
                continue
            value = self._api_float(payload, key, default=math.nan, minimum=0.0)
            return max(self.MIN_FEEDRATE_MM_MIN, value)
        return float(
            self.settings_manager.needle_calibration_configuration().feedrate_mm_min
        )

    @staticmethod
    def _api_bool(
        payload: dict[str, Any],
        *keys: str,
        default: bool,
    ) -> bool:
        for key in keys:
            if key not in payload:
                continue
            value = payload.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)):
                return bool(value)
            if isinstance(value, str):
                text = value.strip().lower()
                if text in {"1", "true", "yes", "y", "on"}:
                    return True
                if text in {"0", "false", "no", "n", "off"}:
                    return False
        return default

    @staticmethod
    def _api_float(
        payload: dict[str, Any],
        *keys: str,
        default: float,
        minimum: float | None = None,
    ) -> float:
        value: object = default
        for key in keys:
            if key in payload and payload.get(key) is not None:
                value = payload.get(key)
                break
        try:
            parsed = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid numeric value for {keys[0]}.") from exc
        if not math.isfinite(parsed):
            raise ValueError(f"Invalid numeric value for {keys[0]}.")
        if minimum is not None and parsed < minimum:
            raise ValueError(f"{keys[0]} must be at least {minimum}.")
        return parsed

    @staticmethod
    def _api_optional_float(
        payload: dict[str, Any],
        *keys: str,
        minimum: float | None = None,
    ) -> float | None:
        for key in keys:
            if key in payload and payload.get(key) is not None:
                return Main._api_float(
                    payload,
                    key,
                    default=0.0,
                    minimum=minimum,
                )
        return None

    @staticmethod
    def _api_int(
        payload: dict[str, Any],
        *keys: str,
        default: int,
        minimum: int | None = None,
    ) -> int:
        value: object = default
        for key in keys:
            if key in payload and payload.get(key) is not None:
                value = payload.get(key)
                break
        try:
            parsed = int(round(float(value)))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid integer value for {keys[0]}.") from exc
        if minimum is not None and parsed < minimum:
            raise ValueError(f"{keys[0]} must be at least {minimum}.")
        return parsed

    @classmethod
    def _api_route_measurement_record_payload(
        cls,
        record: RouteMeasurementRecord,
    ) -> dict[str, Any]:
        return {
            "timestamp": record.timestamp,
            "structure_number": int(record.structure_number),
            "n_measurements": int(record.n_measurements),
            "resistance_ohm": cls._api_json_ready(record.resistance_ohm),
            "resistance_rms_ohm": cls._api_json_ready(record.resistance_rms_ohm),
            "relative_rms": cls._api_json_ready(record.relative_rms),
            "status": record.status,
            "contact_quality": cls._api_contact_quality_payload(
                record.contact_quality
            ),
            "raw_samples": [
                cls._api_route_sample_payload(sample)
                for sample in record.raw_samples
            ],
        }

    @classmethod
    def _api_route_sample_payload(cls, sample: object) -> dict[str, Any]:
        fields = (
            "sample_index",
            "differential_resistance_ohm",
            "compliance_hit",
            "negative_source_voltage_v",
            "negative_measured_voltage_v",
            "negative_current_a",
            "negative_resistance_ohm",
            "positive_source_voltage_v",
            "positive_measured_voltage_v",
            "positive_current_a",
            "positive_resistance_ohm",
        )
        return {
            field: cls._api_json_ready(getattr(sample, field, None))
            for field in fields
        }

    @classmethod
    def _api_contact_quality_payload(cls, quality: object | None) -> dict[str, Any] | None:
        if quality is None:
            return None
        return {
            "assessed": bool(getattr(quality, "assessed", False)),
            "good": getattr(quality, "good", None),
            "status": str(getattr(quality, "status", "")),
            "median_ohm": cls._api_json_ready(
                getattr(quality, "median_ohm", math.nan)
            ),
            "mad_sigma_ohm": cls._api_json_ready(
                getattr(quality, "mad_sigma_ohm", math.nan)
            ),
            "p95_abs_step_ohm": cls._api_json_ready(
                getattr(quality, "p95_abs_step_ohm", math.nan)
            ),
            "span_ohm": cls._api_json_ready(
                getattr(quality, "span_ohm", math.nan)
            ),
            "compliance_hits": int(getattr(quality, "compliance_hits", 0)),
            "polarity_sign_mismatch_count": int(
                getattr(quality, "polarity_sign_mismatch_count", 0)
            ),
            "reasons": list(getattr(quality, "reasons", ()) or ()),
        }

    @classmethod
    def _api_contact_seek_payload(cls, seek: object | None) -> dict[str, Any] | None:
        if seek is None:
            return None
        return {
            "found": bool(getattr(seek, "found", False)),
            "status": str(getattr(seek, "status", "")),
            "attempts": int(getattr(seek, "attempts", 0)),
            "initial_status": str(getattr(seek, "initial_status", "")),
            "final_status": str(getattr(seek, "final_status", "")),
            "depth_below_down_mm": cls._api_json_ready(
                getattr(seek, "depth_below_down_mm", math.nan)
            ),
            "axis_a_lowering_mm": cls._api_json_ready(
                getattr(seek, "axis_a_lowering_mm", math.nan)
            ),
            "step_mm": cls._api_json_ready(getattr(seek, "step_mm", math.nan)),
            "max_depth_mm": cls._api_json_ready(
                getattr(seek, "max_depth_mm", math.nan)
            ),
        }

    @staticmethod
    def _api_structure_number_for_measurement_point(
        point: RouteMeasurementPoint,
    ) -> int:
        for value in (point.label, point.point_id):
            match = re.search(r"(\d+)\s*$", str(value).strip())
            if match is not None:
                try:
                    return int(match.group(1))
                except ValueError:
                    pass
        return int(point.index)

    @staticmethod
    def _api_structure_number_for_route_point(
        route_index: int,
        route_point: object,
    ) -> int:
        for value in (
            getattr(route_point, "label", ""),
            getattr(route_point, "id", ""),
        ):
            match = re.search(r"(\d+)\s*$", str(value).strip())
            if match is not None:
                try:
                    return int(match.group(1))
                except ValueError:
                    pass
        return int(route_index)

    @staticmethod
    def _api_timestamp_utc() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    @classmethod
    def _api_json_ready(cls, value: object) -> Any:
        if isinstance(value, dict):
            return {str(key): cls._api_json_ready(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._api_json_ready(item) for item in value]
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        return value

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

    def _preload_lazy_dialog_modules(self) -> None:
        """Warm non-critical dialogs after the main window is already visible."""

        modules = (
            "probe_station_gui.dialogs.settings_dialog",
            "probe_station_gui.dialogs.route_measurement_dialog",
            "probe_station_gui.dialogs.microscope_scan_dialog",
        )

        def preload() -> None:
            for module_name in modules:
                try:
                    importlib.import_module(module_name)
                except Exception:
                    logger.debug(
                        "Lazy dialog preload failed: %s",
                        module_name,
                        exc_info=True,
                    )

        threading.Thread(
            target=preload,
            name="LazyDialogPreload",
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
        self._update_stage_coordinate_apply_state()
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
        self._update_stage_coordinate_apply_state()

    def _on_click_move_started(
        self,
        move_x_mm: float,
        move_y_mm: float,
        feedrate_mm_min: float,
    ) -> None:
        try:
            distance_mm = math.hypot(float(move_x_mm), float(move_y_mm))
            feedrate = max(self.MIN_FEEDRATE_MM_MIN, float(feedrate_mm_min))
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
        self._send_telegram_alert(
            "camera_error",
            f"Probe station camera error:\n{message}",
            attach_photo=True,
        )

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
        with self._latest_camera_frame_condition:
            self._latest_camera_frame = qimg.copy()
            self._latest_camera_frame_counter += 1
            self._latest_camera_frame_condition.notify_all()
        self._latest_camera_frame_for_notifications = qimg
        self.view.set_frame(qimg)

    def _latest_camera_counter(self) -> int:
        with self._latest_camera_frame_condition:
            return int(self._latest_camera_frame_counter)

    def _wait_for_camera_frame(
        self,
        *,
        after_counter: int | None = None,
        timeout_s: float = 2.0,
    ) -> tuple[QImage | None, int]:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._latest_camera_frame_condition:
            while True:
                frame = self._latest_camera_frame
                counter = int(self._latest_camera_frame_counter)
                fresh_enough = after_counter is None or counter > int(after_counter)
                if frame is not None and fresh_enough:
                    return frame.copy(), counter
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    if frame is not None and after_counter is None:
                        return frame.copy(), counter
                    return None, counter
                self._latest_camera_frame_condition.wait(min(remaining, 0.1))

    def _active_microscope_scale(self):
        objective = self.settings_manager.active_objective_configuration()
        return objective_scale_calibration(objective)

    def _active_objective_metadata(self) -> tuple[str, float | None]:
        objective = self.settings_manager.active_objective_configuration()
        name = str(getattr(objective, "name", "") or "")
        try:
            magnification = float(getattr(objective, "magnification"))
        except (TypeError, ValueError):
            magnification = None
        if magnification is not None and not math.isfinite(magnification):
            magnification = None
        return name, magnification

    def _stage_position_for_image_metadata(
        self,
        *,
        stage_xy: tuple[float, float] | None = None,
    ) -> tuple[float, ...] | None:
        latest = self.stage_controller.latest_stage_position()
        if latest is not None:
            return latest
        if stage_xy is not None:
            return (float(stage_xy[0]), float(stage_xy[1]))
        return None

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
            status_text = str(message)
            try:
                timeout = int(timeout_ms)
            except (TypeError, ValueError):
                timeout = 0
            app = QApplication.instance()
            if app is not None and QThread.currentThread() != app.thread():
                self.status_message_requested.emit(status_text, timeout)
                return
            self._latest_status_message = status_text
            self.statusBar().showMessage(status_text, timeout)
            self._status_log.appendPlainText(status_text)
            self._append_status_log(status_text)

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
            "Clear edited fields or cancel the active stage workflow."
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

    def _surface_map_capture_running(self) -> bool:
        window = self.surface_map_window
        if window is None or not hasattr(window, "is_capture_running"):
            return False
        try:
            return bool(window.is_capture_running())
        except Exception:
            return False

    def _microscope_scan_running(self) -> bool:
        thread = getattr(self, "_microscope_scan_thread", None)
        return thread is not None and thread.is_alive()

    def _has_cancelable_operation(self) -> bool:
        controller_busy = (
            hasattr(self, "stage_controller") and self.stage_controller.is_busy()
        )
        route_contact_move_thread = getattr(self, "_route_contact_move_thread", None)
        route_contact_move_active = (
            route_contact_move_thread is not None
            and route_contact_move_thread.is_alive()
        )
        route_measurement_thread = getattr(self, "_route_measurement_thread", None)
        route_measurement_active = (
            route_measurement_thread is not None
            and route_measurement_thread.is_alive()
        )
        return (
            self._coordinate_move_axis is not None
            or controller_busy
            or self._controller_reports_active_motion()
            or route_contact_move_active
            or route_measurement_active
            or self._surface_map_capture_running()
            or self._microscope_scan_running()
            or self._sample_handling_active()
            or self._manual_alignment_pick_slot is not None
            or self._pending_click_to_move is not None
            or bool(self._pending_homing_axes)
            or self._homing_active_key is not None
            or self._pending_alignment_preparation is not None
            or self._pending_quick_alignment_rotation
        )

    def _controller_reports_active_motion(self) -> bool:
        if not hasattr(self, "stage_controller"):
            return False
        state = (self.stage_controller.latest_stage_state() or "").strip().lower()
        if state not in {"run", "jog"}:
            return False
        if self._controller_latest_state_is_stale():
            logger.debug(
                "Ignoring stale controller active state %r after %.3f s.",
                state,
                self._controller_latest_state_age_s() or 0.0,
            )
            return False
        return True

    def _controller_latest_state_blocks_motion(self) -> bool:
        if not hasattr(self, "stage_controller"):
            return False
        state = (self.stage_controller.latest_stage_state() or "").strip().lower()
        if state in {"", "idle"}:
            return False
        if state in {"run", "jog"}:
            return self._controller_reports_active_motion()
        return True

    def _controller_latest_state_age_s(self) -> float | None:
        timestamp_getter = getattr(
            getattr(self, "stage_controller", None),
            "last_status_timestamp",
            None,
        )
        if not callable(timestamp_getter):
            return None
        timestamp = timestamp_getter()
        if timestamp is None:
            return None
        try:
            return max(0.0, time.monotonic() - float(timestamp))
        except (TypeError, ValueError):
            return None

    def _controller_latest_state_is_stale(self) -> bool:
        age_s = self._controller_latest_state_age_s()
        return (
            age_s is not None
            and age_s > self.CONTROLLER_ACTIVE_STATE_STALE_S
        )

    def _schedule_cancel_state_refresh(self) -> None:
        for delay_ms in (0, 100, 300, 1000, 2500):
            QTimer.singleShot(delay_ms, self._update_stage_coordinate_apply_state)

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
            cancel_button.setEnabled(available or self._has_cancelable_operation())

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
        cancelled_any = False
        cleared_edits = False
        if self._pending_click_to_move is not None:
            self._clear_pending_click_to_move(clear_cross=True)
            cancelled_any = True
        if self._manual_alignment_pick_slot is not None:
            self._cancel_manual_alignment_pick()
            cancelled_any = True
        if self._pending_alignment_preparation is not None:
            self._pending_alignment_preparation = None
            cancelled_any = True
        if self._pending_quick_alignment_rotation:
            self._pending_quick_alignment_rotation = False
            cancelled_any = True
        if self._pending_homing_axes or self._homing_active_key is not None:
            self._clear_pending_homing_queue()
            cancelled_any = True
        runner = self._route_measurement_runner
        if runner is not None:
            runner.stop()
            cancelled_any = True
            if self.design_navigator_panel is not None:
                self.design_navigator_panel.set_route_measurement_waiting(False)
                self.design_navigator_panel.set_route_measurement_status(
                    "Route measurement cancel requested."
                )
        if self._surface_map_capture_running():
            try:
                self.surface_map_window.stop_capture()
            except Exception:
                logger.exception("Failed to stop surface map capture from Cancel.")
            cancelled_any = True
        if self._microscope_scan_running():
            self._microscope_scan_stop_requested.set()
            if self.microscope_scan_dialog is not None:
                self.microscope_scan_dialog.set_status(
                    "Microscope scan stop requested."
                )
            cancelled_any = True
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
            self._schedule_cancel_state_refresh()
            return
        if self._controller_reports_active_motion():
            self.stage_controller.cancel_active_motion("Motion cancel requested.")
            self._clear_stage_motion_axes()
            self._clear_planned_move_prediction(clear_wait_state=True)
            cancelled_any = True
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
        if self.stage_controller.is_busy():
            self.stage_controller.cancel_active_task("Operation cancel requested.")
            self._clear_stage_motion_axes()
            self._clear_planned_move_prediction(clear_wait_state=True)
            cancelled_any = True
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
        if self._clear_pending_stage_coordinate_targets():
            cleared_edits = True
            self.view.setFocus(Qt.OtherFocusReason)
        if cancelled_any:
            self.view.setFocus(Qt.OtherFocusReason)
            self._show_status("Cancel requested.", 3000)
            self._schedule_cancel_state_refresh()
            return
        if cleared_edits:
            self._show_status("Cleared pending coordinate edits.", 2000)
            self._schedule_cancel_state_refresh()

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
        if self.serial_terminal_panel:
            self.serial_terminal_panel.set_serial(self.serial_connection)
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
        self._update_stage_coordinate_apply_state()
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
        if self.lcr_controller is not None and not self.lcr_controller.is_connected():
            if self.settings_manager.meter_auto_connect_enabled():
                logger.debug(
                    "Attempting measurement-instrument auto-connect because "
                    "previous session closed connected"
                )
                self.lcr_controller.request_connect()
            else:
                logger.debug(
                    "Skipping measurement-instrument auto-connect because previous "
                    "session was disconnected"
                )

    def _run_serial_startup_sync(self) -> None:
        if self.serial_connection is None or not self.serial_connection.is_open:
            return
        self.stage_controller.request_startup_sync(
            auto_home_a=True,
            clear_unverified_state=False,
        )
        self._schedule_cancel_state_refresh()

    def _apply_axis_feedrate_limits(self, rates: object) -> None:
        if not isinstance(rates, dict):
            return
        self.stage_controller.apply_axis_max_feedrates(rates)
        applied_rates = self.stage_controller.axis_max_feedrates()
        if self.joystick_panel is not None:
            self.joystick_panel.set_axis_feedrate_limits(applied_rates)

    def _current_axis_feedrate_limits(self) -> dict[str, float]:
        return self.stage_controller.axis_max_feedrates()

    def _on_axis_max_feedrates_changed(self, rates: object) -> None:
        self._apply_axis_feedrate_limits(rates)
        if self.stage_controller.axis_max_feedrates():
            self._apply_joystick_feedrate_preferences()

    def _apply_joystick_feedrate_preferences(self) -> None:
        if self.joystick_panel is None:
            return
        needle_settings = self.settings_manager.needle_calibration_configuration()
        feedrates = self.settings_manager.feedrate_configuration()
        self.joystick_panel.apply_feedrate_settings(
            feedrates.linear.presets,
            feedrates.linear.default,
            feedrates.rotary.presets,
            feedrates.rotary.default,
        )
        self.joystick_panel.apply_needle_settings(needle_settings.feedrate_mm_min)
        jog = self.settings_manager.jog_configuration()
        self.joystick_panel.apply_jog_settings(
            jog.linear_distance_mm,
            jog.rotary_distance_deg,
            jog.motion_safety_disabled,
            jog.manual_axis,
            jog.manual_axis_distance_mm,
            jog.manual_axis_mode,
            jog.manual_axis_feedrate_mm_min,
            jog.focus_feedrate_mm_min,
            jog.turntable_feedrate_mm_min,
            jog.mode,
            focus_step_feedrate_mm_min=jog.focus_step_feedrate_mm_min,
            needle_step_feedrate_mm_min=jog.needles_step_feedrate_mm_min,
            turntable_step_feedrate_mm_min=jog.turntable_step_feedrate_mm_min,
        )
        logger.debug(
            "Joystick jog settings reapplied: mode=%s linear_distance_mm=%s rotary_distance_deg=%s safety_disabled=%s manual_axis=%s manual_axis_distance_mm=%s manual_mode=%s xy_step_feedrate_mm_min=%s focus_jog_feedrate_mm_min=%s focus_step_feedrate_mm_min=%s needle_step_feedrate_mm_min=%s turntable_jog_feedrate_mm_min=%s turntable_step_feedrate_mm_min=%s",
            jog.mode,
            jog.linear_distance_mm,
            jog.rotary_distance_deg,
            jog.motion_safety_disabled,
            jog.manual_axis,
            jog.manual_axis_distance_mm,
            jog.manual_axis_mode,
            jog.manual_axis_feedrate_mm_min,
            jog.focus_feedrate_mm_min,
            jog.focus_step_feedrate_mm_min,
            jog.needles_step_feedrate_mm_min,
            jog.turntable_feedrate_mm_min,
            jog.turntable_step_feedrate_mm_min,
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
        self._apply_axis_feedrate_limits(self._current_axis_feedrate_limits())
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
        axis_mismatches = self._position_axis_mismatches(
            expected_position,
            position,
        )
        xy_mismatches = axis_mismatches.intersection({"X", "Y"})
        if expected_position is None or xy_mismatches:
            if xy_mismatches:
                removed_axes = self.stage_controller.mark_axes_unhomed(xy_mismatches)
                if removed_axes:
                    axes_label = ", ".join(sorted(removed_axes))
                    self._show_status(
                        f"Controller {axes_label} coordinate changed. Cleared cached homing.",
                        5000,
                    )
            self._show_status(
                "Controller X/Y coordinates changed. Cleared cached design selection.",
                5000,
            )
            self._save_controller_state_without_design()
            return
        if "Z" in axis_mismatches:
            removed_axes = self.stage_controller.mark_axes_unhomed({"Z"})
            if removed_axes:
                self._show_status(
                    "Controller Z coordinate changed. Cleared cached Z homing.",
                    5000,
                )
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
    def _position_axis_mismatches(
        cls,
        expected: tuple[float, ...] | None,
        actual: tuple[float, ...],
    ) -> set[str]:
        if not expected:
            return {"X", "Y"}
        tolerance = cls.DESIGN_RESTORE_POSITION_TOLERANCE
        mismatches: set[str] = set()
        for index, axis_name in enumerate(cls.STAGE_AXIS_NAMES):
            if index >= len(expected):
                break
            if index >= len(actual):
                mismatches.add(axis_name)
                continue
            try:
                expected_value = float(expected[index])
                actual_value = float(actual[index])
            except (TypeError, ValueError):
                mismatches.add(axis_name)
                continue
            if (
                not math.isfinite(expected_value)
                or not math.isfinite(actual_value)
                or abs(expected_value - actual_value) > tolerance
            ):
                mismatches.add(axis_name)
        return mismatches

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

    def _persist_lcr_connection_state(
        self,
        connected: bool,
        *,
        description: str = "",
    ) -> None:
        self.settings_manager.save_meter_connection_state(
            connected,
            meter_type=self.lcr_controller.meter_type(),
            description=description or self.lcr_controller.connection_label(),
        )

    def _request_lcr_disconnect(self) -> None:
        self._persist_lcr_connection_state(False)
        self.lcr_controller.request_disconnect()

    def _setup_menus(self) -> None:
        app_menu = self.menuBar().addMenu("Application")
        navigation_menu = self.menuBar().addMenu("Navigation")
        panels_menu = self.menuBar().addMenu("Tools")
        calibration_menu = self.menuBar().addMenu("Calibration")

        settings_action = QAction("Settings…", self)
        settings_action.setText("Settings")
        settings_action.triggered.connect(self._open_settings_dialog)
        app_menu.addAction(settings_action)

        open_log_action = QAction("Open Status Log…", self)
        open_log_action.setText("Open Status Log")
        open_log_action.triggered.connect(self._open_status_log)
        app_menu.addAction(open_log_action)

        serial_connection_action = QAction("Connection", self)
        serial_connection_action.triggered.connect(self._show_connection_dialog)
        app_menu.addAction(serial_connection_action)

        self._sample_load_action = QAction("Load Sample", self)
        self._sample_load_action.triggered.connect(self._request_sample_load)
        navigation_menu.addAction(self._sample_load_action)

        self._sample_unload_action = QAction("Unload Sample", self)
        self._sample_unload_action.triggered.connect(self._request_sample_unload)
        navigation_menu.addAction(self._sample_unload_action)

        self._design_layout_window_action = QAction("Design Window", self)
        self._design_layout_window_action.setCheckable(True)
        self._design_layout_window_action.toggled.connect(
            self._toggle_design_layout_window
        )
        navigation_menu.addAction(self._design_layout_window_action)

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

        self._microscope_scan_action = QAction("Microscope Scan", self)
        self._microscope_scan_action.triggered.connect(self._show_microscope_scan_dialog)
        calibration_menu.addAction(self._microscope_scan_action)

        self._click_calibration_action = QAction(
            self._click_calibration_action_text(),
            self,
        )
        self._click_calibration_action.triggered.connect(
            self._show_click_calibration_dialog
        )
        calibration_menu.addAction(self._click_calibration_action)

        for dock, title in (
            (self.resistance_dock, "Resistance"),
            (self.oscillation_dock, "Oscillation"),
            (self.joystick_dock, "Joystick"),
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
        self._apply_axis_feedrate_limits(self._current_axis_feedrate_limits())
        if self.joystick_panel:
            bindings = self.settings_manager.control_bindings()
            self.joystick_panel.apply_control_bindings(bindings)
            logger.debug("Joystick bindings reapplied from settings")
            self._apply_joystick_feedrate_preferences()
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
        self._apply_needle_calibration_runtime(needle_settings)
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
            meter_type=needle_settings.meter_type,
            resource_name=needle_settings.visa_resource,
            keithley_source_resource=needle_settings.keithley_source_resource,
            keithley_voltmeter_resource=needle_settings.keithley_voltmeter_resource,
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
            self.serial_connection_panel.set_lcr_resource(
                self.lcr_controller.connection_label()
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
            self._schedule_cancel_state_refresh()
        if self._api_bridge is not None:
            self._configure_api_server_from_settings(start_if_enabled=True)
        self._configure_telegram_bot_from_settings()
        self._update_coordinate_display(cursor_xy=None)

    def _apply_needle_calibration_runtime(self, needle_settings: object) -> None:
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
            contact_zone_mm=needle_settings.contact_zone_mm,
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

    def _open_settings_dialog(self, initial_tab: object = None) -> None:
        from probe_station_gui.dialogs.settings_dialog import SettingsDialog

        tab_name = initial_tab if isinstance(initial_tab, str) else None
        self._stop_telegram_bot_service()
        dialog = SettingsDialog(
            self.settings_manager.settings,
            self,
            initial_tab=tab_name,
            camera_settings_source=self.grabber,
            api_key_store=self._api_key_store,
        )
        dialog.settings_applied.connect(self._apply_settings_from_dialog)
        try:
            if dialog.exec() != QDialog.Accepted:
                if not dialog.was_applied():
                    logger.debug("Settings dialog cancelled")
        finally:
            self._configure_telegram_bot_from_settings()

    def _show_connection_dialog(self, tab_name: object = None) -> None:
        if self.serial_connection_dialog is None:
            return
        if self.serial_connection_panel is not None:
            self.serial_connection_panel.set_lcr_resource(
                self.lcr_controller.connection_label()
            )
        if self.serial_connection_tabs is not None:
            self.serial_connection_tabs.setCurrentIndex(
                1 if tab_name == "terminal" else 0
            )
        self.serial_connection_dialog.show()
        self.serial_connection_dialog.raise_()
        self.serial_connection_dialog.activateWindow()

    def _apply_settings_from_dialog(self, new_settings: object) -> None:
        if not isinstance(new_settings, Settings):
            return
        self.settings_manager.replace(new_settings)
        self.settings_manager.save()
        self._apply_settings()
        logger.info("Settings updated from dialog")

    def _send_telegram_alert(
        self,
        alert_key: str,
        message: str,
        *,
        attach_photo: bool = False,
        photo: tuple[bytes, str] | None = None,
        document_path: str | Path | None = None,
        reply_markup: object | None = None,
    ) -> None:
        telegram_settings = self.settings_manager.telegram_configuration()
        if not telegram_settings.enabled:
            logger.debug("Telegram alert skipped: disabled alert=%s", alert_key)
            return
        if not telegram_settings.alert_enabled(alert_key):
            logger.debug("Telegram alert skipped: alert=%s is disabled", alert_key)
            return
        if not telegram_settings.chat_id.strip():
            logger.warning(
                "Telegram alert skipped: chat is not linked alert=%s",
                alert_key,
            )
            return
        bot_token = resolved_bot_token(telegram_settings)
        if not bot_token:
            logger.warning(
                "Telegram alert skipped: bot token is not configured alert=%s",
                alert_key,
            )
            return
        resolved_photo: tuple[bytes, str] | None = (
            photo if photo is not None else self._latest_camera_frame_photo()
            if attach_photo
            else None
        )
        document = Path(document_path).expanduser() if document_path is not None else None
        if document is not None and (not document.exists() or not document.is_file()):
            document = None
        send_telegram_message_in_thread(
            bot_token=bot_token,
            chat_id=telegram_settings.chat_id,
            text=message,
            photo_bytes=resolved_photo[0] if resolved_photo is not None else None,
            photo_name=resolved_photo[1] if resolved_photo is not None else "microscope.jpg",
            document_path=document,
            reply_markup=reply_markup,
        )

    def _send_telegram_bot_message(
        self,
        message: str,
        *,
        photo: tuple[bytes, str] | None = None,
        document_path: str | Path | None = None,
        reply_markup: object | None = None,
    ) -> bool:
        telegram_settings = self.settings_manager.telegram_configuration()
        if not telegram_settings.enabled or not telegram_settings.chat_id.strip():
            return False
        bot_token = resolved_bot_token(telegram_settings)
        if not bot_token:
            return False
        document = Path(document_path).expanduser() if document_path is not None else None
        if document is not None and (not document.exists() or not document.is_file()):
            document = None
        return send_telegram_message_in_thread(
            bot_token=bot_token,
            chat_id=telegram_settings.chat_id,
            text=message,
            photo_bytes=photo[0] if photo is not None else None,
            photo_name=photo[1] if photo is not None else "microscope.jpg",
            document_path=document,
            reply_markup=reply_markup,
        )

    def _latest_camera_frame_photo(self) -> tuple[bytes, str] | None:
        frame = self._latest_camera_frame_for_notifications
        return self._qimage_telegram_photo(frame)

    @staticmethod
    def _qimage_telegram_photo(frame: QImage | None) -> tuple[bytes, str] | None:
        if frame is None or frame.isNull():
            return None
        buffer = QBuffer()
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            return None
        if frame.save(buffer, "JPG", 88):
            return bytes(buffer.data()), "microscope.jpg"
        buffer.close()
        buffer = QBuffer()
        if not buffer.open(QIODevice.OpenModeFlag.WriteOnly):
            return None
        if frame.save(buffer, "PNG"):
            return bytes(buffer.data()), "microscope.png"
        return None

    @staticmethod
    def _route_attention_status(message: str) -> bool:
        text = str(message or "")
        if not text.startswith("Route measurement: point "):
            return False
        return "interrupted" in text or "Save Shift" in text

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
        self._update_stage_coordinate_apply_state()
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
        self._update_stage_coordinate_apply_state()
        self._show_status("Chip alignment image pick cancelled.", 3000)

    def _reset_manual_alignment(self, *, cancel_pick: bool = True) -> None:
        self._manual_alignment_points = [None, None]
        if cancel_pick:
            self._manual_alignment_pick_slot = None
        self._refresh_manual_alignment_ui()
        self._update_coordinate_display(cursor_xy=None)
        self._update_stage_coordinate_apply_state()

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
        self._update_stage_coordinate_apply_state()

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
        self._update_stage_coordinate_apply_state()

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
        if not self.serial_terminal_panel:
            return
        self._show_connection_dialog("terminal")
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
        if self._coordinate_move_axis is not None:
            logger.debug(
                "Coordinate move tracking cleared after manual jog command: %s",
                commanded_distances,
            )
            self._clear_coordinate_move_tracking(
                clear_pending=False,
                reset_override=False,
            )
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
        feedrate = max(self.MIN_FEEDRATE_MM_MIN, float(feedrate_mm_min))
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

    def _save_jog_control_mode(self, mode: str) -> None:
        control_mode = str(mode).strip().lower()
        if control_mode not in {"jog", "step"}:
            return
        settings = self.settings_manager.settings.clone()
        if settings.jog.mode == control_mode:
            return
        settings.jog.mode = control_mode
        self.settings_manager.replace(settings)
        self.settings_manager.save()

    def _save_jog_feedrate_setting(self, key: str, feedrate_mm_min: float) -> None:
        try:
            feedrate = max(self.MIN_FEEDRATE_MM_MIN, float(feedrate_mm_min))
        except (TypeError, ValueError):
            return
        settings = self.settings_manager.settings.clone()
        current = getattr(settings.jog, key, None)
        if current is not None and abs(float(current) - feedrate) <= 1e-9:
            return
        setattr(settings.jog, key, feedrate)
        self.settings_manager.replace(settings)
        self.settings_manager.save()

    def _on_step_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting("manual_axis_feedrate_mm_min", feedrate_mm_min)
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

    def _on_focus_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting("focus_feedrate_mm_min", feedrate_mm_min)
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

    def _on_focus_step_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting(
            "focus_step_feedrate_mm_min",
            feedrate_mm_min,
        )
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

    def _on_turntable_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting("turntable_feedrate_mm_min", feedrate_mm_min)
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

    def _on_turntable_step_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting(
            "turntable_step_feedrate_mm_min",
            feedrate_mm_min,
        )
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

    def _on_manual_axis_move_requested(
        self,
        axis: str,
        value_mm: float,
        mode: str,
        feedrate_mm_min: float,
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
        self._start_coordinate_axis_move(
            axis,
            raw_target,
            display_target,
            feedrate_mm_min=feedrate_mm_min,
        )

    def _schedule_linear_feedrate_save(self, feedrate_mm_min: float) -> None:
        try:
            self._pending_linear_feedrate_default = max(
                self.MIN_FEEDRATE_MM_MIN,
                float(feedrate_mm_min),
            )
        except (TypeError, ValueError):
            return
        self._linear_feedrate_save_timer.start()

    def _on_linear_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._schedule_linear_feedrate_save(feedrate_mm_min)
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

    def _on_needle_feedrate_changed(self, feedrate_mm_min: float) -> None:
        try:
            feedrate = max(self.MIN_FEEDRATE_MM_MIN, float(feedrate_mm_min))
        except (TypeError, ValueError):
            return
        settings = self.settings_manager.settings.clone()
        if abs(settings.needle_calibration.feedrate_mm_min - feedrate) > 1e-9:
            settings.needle_calibration.feedrate_mm_min = feedrate
            self.settings_manager.replace(settings)
            self.settings_manager.save()
        self.stage_controller.queue_active_needles_feedrate(feedrate)

    def _on_needle_step_feedrate_changed(self, feedrate_mm_min: float) -> None:
        self._save_jog_feedrate_setting(
            "needles_step_feedrate_mm_min",
            feedrate_mm_min,
        )
        self._apply_coordinate_move_feedrate(feedrate_mm_min)

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

    def _coordinate_feedrate_for_axes(self, axes: object) -> float:
        axes_tuple = tuple(str(axis).strip().upper() for axis in axes)
        selector = getattr(
            self.joystick_panel,
            "select_coordinate_feedrate_for_axes",
            None,
        )
        if callable(selector):
            try:
                return max(
                    self.MIN_FEEDRATE_MM_MIN,
                    float(selector(axes_tuple)),
                )
            except (TypeError, ValueError):
                logger.exception("Invalid coordinate feedrate selected for %s.", axes_tuple)
        return max(self.MIN_FEEDRATE_MM_MIN, float(self._current_linear_feedrate()))

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
            else max(self.MIN_FEEDRATE_MM_MIN, float(feedrate_mm_min))
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
            self._schedule_cancel_state_refresh()
            return
        if (
            stripped.startswith("$#")
            or stripped.startswith("$G")
            or stripped.startswith("$10")
            or stripped.startswith("G10")
        ):
            self.stage_controller.request_startup_sync(auto_home_a=False)
            self._schedule_cancel_state_refresh()

    def _on_stage_task_started(self) -> None:
        self._show_status("Moving stage...")
        self._update_stage_coordinate_apply_state()

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
        if (
            not success
            and getattr(self, "_coordinate_move_reissue_cancel_pending", False)
            and "operation cancelled" in message_lower
        ):
            self._coordinate_move_reissue_cancel_pending = False
            logger.debug(
                "Coordinate move worker cancelled for feedrate reissue; "
                "keeping coordinate tracking active."
            )
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
            self._schedule_cancel_state_refresh()
            return
        self._coordinate_move_reissue_cancel_pending = False
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
        self._schedule_cancel_state_refresh()

    def on_autofocus_finished(self, success: bool, message: str) -> None:
        if message:
            self._show_status(message, 5000)
        if success:
            self._remember_sample_focus_from_latest()
        else:
            logger.error("Autofocus failed: %s", message)
        self._schedule_cancel_state_refresh()

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
            current_route_point = self._design_session.current_route_point()
            self._last_selected_design_point = (
                current_route_point.camera_center
                if current_route_point is not None
                else None
            )
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
            self._restore_route_measurement_state_after_design_load()
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
        self._restore_route_measurement_state_after_design_load()

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

    def _open_route_measurement_dialog(self, *, start_context: bool = True) -> None:
        route = self._design_session.route
        if route is None or not route.points:
            self._show_status("Create or load a probe route before measuring.", 5000)
            return
        from probe_station_gui.dialogs.route_measurement_dialog import (
            RouteMeasurementDialog,
        )

        default_path = "probe_route_measurements.csv"
        default_photo_dir = "probe_route_photos"
        if route.path is not None:
            default_path = str(
                route.path.with_name(f"{route.path.stem}-measurements.csv")
            )
            default_photo_dir = str(route.path.with_name(f"{route.path.stem}-photos"))
        elif self._design_session.document is not None:
            default_path = str(
                self._design_session.document.path.parent
                / "probe_route_measurements.csv"
            )
            default_photo_dir = str(
                self._design_session.document.path.parent / "probe_route_photos"
            )
        dialog = self._route_measurement_dialog
        if dialog is None:
            dialog = RouteMeasurementDialog(
                route_name=route.name,
                route_point_count=len(route.points),
                default_csv_path=default_path,
                default_photo_dir=default_photo_dir,
                default_meter_type=self.lcr_controller.meter_type(),
                settings_path=(
                    self.settings_manager.config_dir()
                    / "route-measurement-settings.json"
                ),
                parent=self,
            )
            dialog.measure_requested.connect(self._start_route_measurement)
            dialog.start_session_requested.connect(
                self._start_route_measurement_session
            )
            dialog.cancel_session_requested.connect(
                self._cancel_route_measurement_session
            )
            dialog.next_requested.connect(
                lambda: self._submit_route_measurement_confirmation("next")
            )
            dialog.remeasure_requested.connect(
                lambda: self._submit_route_measurement_confirmation("remeasure")
            )
            dialog.measure_current_requested.connect(
                lambda: self._submit_route_measurement_confirmation("measure")
            )
            dialog.skip_requested.connect(
                lambda: self._submit_route_measurement_confirmation("skip")
            )
            dialog.save_shift_requested.connect(self._save_route_measurement_shift)
            dialog.interrupt_requested.connect(
                self._request_route_measurement_point_correction
            )
            dialog.pause_requested.connect(self._request_pause_route_measurement)
            dialog.stop_requested.connect(self._request_stop_route_measurement)
            dialog.jump_requested.connect(self._submit_route_measurement_jump)
            dialog.move_requested.connect(self._request_route_contact_move)
            dialog.current_point_changed.connect(
                self._on_route_measurement_current_point_changed
            )
            dialog.finished.connect(
                lambda _result: self._clear_route_measurement_dialog()
            )
            self._route_measurement_dialog = dialog
        else:
            dialog.set_route(
                route_name=route.name,
                route_point_count=len(route.points),
                default_csv_path=default_path,
                default_photo_dir=default_photo_dir,
            )
        dialog.set_measurement_session_active(
            self._route_measurement_session_active,
            save=False,
        )
        if self._route_measurement_current_point is not None:
            dialog.set_current_point(
                self._route_measurement_current_point,
                save=False,
            )
        if (
            self._route_measurement_thread is not None
            and self._route_measurement_thread.is_alive()
        ):
            dialog.set_running(True)
            dialog.set_waiting(self._route_measurement_waiting)
        elif dialog.measurement_session_active():
            dialog.set_status(
                "Choose a point, then Measure or Move."
            )
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        thread = self._route_measurement_thread
        if start_context and (thread is None or not thread.is_alive()):
            configuration = dialog.current_configuration()
            self._start_route_measurement(
                configuration,
                wait_before_first_point=True,
            )

    def _restore_route_measurement_state_after_design_load(self) -> None:
        route = self._design_session.route
        if route is None or not route.points:
            return
        state = self._load_route_measurement_settings()
        current_point = self._route_measurement_current_point_from_settings(state)
        session_active = self._route_measurement_session_active_from_settings(state)
        if session_active and not self._route_measurement_settings_match_route(
            state,
            route,
        ):
            session_active = False
        self._route_measurement_session_active = session_active
        if session_active and current_point is not None:
            self._set_route_measurement_resume_point(current_point)
        if session_active:
            QTimer.singleShot(0, self._open_route_measurement_dialog)

    def _load_route_measurement_settings(self) -> dict[str, object]:
        settings_path = (
            self.settings_manager.config_dir() / "route-measurement-settings.json"
        )
        if not settings_path.exists():
            return {}
        try:
            with settings_path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return {}
        return dict(loaded) if isinstance(loaded, dict) else {}

    @staticmethod
    def _route_measurement_current_point_from_settings(
        state: dict[str, object],
    ) -> int | None:
        value = state.get("current_point", state.get("start_point"))
        try:
            point_number = int(value)
        except (TypeError, ValueError):
            return None
        return point_number if point_number >= 1 else None

    @staticmethod
    def _route_measurement_session_active_from_settings(
        state: dict[str, object],
    ) -> bool:
        return bool(
            state.get(
                "measurement_session_active",
                state.get("measurement_pending", False),
            )
        )

    def _route_measurement_settings_match_route(
        self,
        state: dict[str, object],
        route: MeasurementRoute,
    ) -> bool:
        stored_count = state.get("session_route_point_count")
        try:
            if stored_count is not None and int(stored_count) != len(route.points):
                return False
        except (TypeError, ValueError):
            return False
        stored_path = state.get("session_route_path")
        if isinstance(stored_path, str) and stored_path.strip() and route.path is not None:
            try:
                return Path(stored_path).expanduser().resolve() == route.path.resolve()
            except OSError:
                return str(stored_path).strip() == str(route.path)
        stored_name = state.get("session_route_name")
        if isinstance(stored_name, str) and stored_name.strip():
            return stored_name.strip() == route.name
        return True

    def _clear_route_measurement_dialog(self) -> None:
        self._route_measurement_dialog = None
        thread = self._route_measurement_thread
        runner = self._route_measurement_runner
        if runner is not None and thread is not None and thread.is_alive():
            self._route_measurement_context_close_requested = True
            runner.stop()

    def _start_route_measurement_session(self) -> None:
        thread = self._route_measurement_thread
        if thread is not None and thread.is_alive():
            self._show_status("Route measurement is already active.", 4000)
            return
        configuration = (
            self._route_measurement_dialog.current_configuration()
            if self._route_measurement_dialog is not None
            else None
        )
        point_number = (
            int(configuration.current_point)
            if configuration is not None
            else int(self._route_measurement_current_point or 1)
        )
        self._route_measurement_session_active = True
        self._set_route_measurement_resume_point(point_number)
        self._set_route_measurement_pending(True)
        self._save_route_measurement_session_metadata(configuration)
        message = f"Route point set to point {point_number}."
        self._show_status(message, 5000)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_status(message)

    def _cancel_route_measurement_session(self) -> None:
        thread = self._route_measurement_thread
        if thread is not None and thread.is_alive():
            self._show_status("Stop route measurement before canceling the session.", 5000)
            return
        self._pending_route_measure_point = None
        self._route_measurement_session_active = False
        self._set_route_measurement_resume_point(1)
        self._set_route_measurement_pending(False)
        message = "Route measurement session cancelled."
        self._show_status(message, 5000)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_status(message)

    def _request_route_measurement_for_point(self, point_number: int) -> None:
        point_number = int(point_number)
        thread = self._route_measurement_thread
        if thread is not None and thread.is_alive():
            if self._route_measurement_waiting:
                self._pending_route_measure_point = None
                self._submit_route_measurement_confirmation(f"jump:{point_number}")
                return
            self._pending_route_measure_point = point_number
            self._request_route_measurement_point_correction(
                pending_point_number=point_number
            )
            return
        self._open_route_measurement_dialog(start_context=False)
        dialog = self._route_measurement_dialog
        if dialog is None:
            return
        dialog.set_current_point(point_number)
        self._start_route_measurement(dialog.current_configuration())

    def _current_route_measurement_configuration(
        self,
        fallback: RouteMeasurementRunConfiguration,
    ) -> RouteMeasurementRunConfiguration:
        return self._route_measurement_runtime_configuration or fallback

    @staticmethod
    def _route_measurement_setup_changed(
        previous: RouteMeasurementRunConfiguration | None,
        current: RouteMeasurementRunConfiguration,
    ) -> bool:
        if previous is None:
            return False
        return (
            previous.operation_mode != current.operation_mode
            or bool(previous.previous_ok_only) != bool(current.previous_ok_only)
            or str(previous.previous_csv_path).strip()
            != str(current.previous_csv_path).strip()
        )

    def _restart_waiting_route_measurement(
        self,
        configuration: RouteMeasurementRunConfiguration,
        *,
        route_offset_xy: tuple[float, float],
    ) -> bool:
        old_runner = self._route_measurement_runner
        old_thread = self._route_measurement_thread
        if old_runner is not None:
            old_runner.stop()
        if old_thread is not None and old_thread.is_alive():
            old_thread.join(timeout=2.0)
            if old_thread.is_alive():
                message = "Waiting route measurement did not stop."
                self._show_status(message, 8000)
                if self._route_measurement_dialog is not None:
                    self._route_measurement_dialog.set_status(message)
                return False
        self._route_measurement_thread = None
        self._route_measurement_runner = None
        self._route_measurement_waiting = False
        self._start_route_measurement(
            configuration,
            wait_before_first_point=True,
        )
        new_runner = self._route_measurement_runner
        if not isinstance(new_runner, RouteMeasurementRunner):
            return False
        new_runner.set_route_offset_xy(route_offset_xy)
        if not new_runner.wait_until_waiting(timeout_s=10.0):
            message = "Route measurement did not reach waiting state."
            new_runner.stop()
            thread = self._route_measurement_thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
            self._show_status(message, 8000)
            if self._route_measurement_dialog is not None:
                self._route_measurement_dialog.set_status(message)
            return False
        return True

    def _start_route_measurement(
        self,
        configuration: RouteMeasurementRunConfiguration,
        *,
        wait_before_first_point: bool = False,
    ) -> None:
        thread = self._route_measurement_thread
        if thread is not None and thread.is_alive():
            self._show_status("Route measurement is already active.", 4000)
            return
        if self.serial_connection is None or not self.serial_connection.is_open:
            self._show_status("Connect the stage controller before measuring a route.", 5000)
            return
        route = self._design_session.route
        if route is None or not route.points:
            self._show_status("Create or load a probe route before measuring.", 5000)
            return
        registration = self._design_session.registration
        if registration is None or not registration.valid:
            self._show_status(
                "Design registration is required before measuring a route.",
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
        previous_ok_skipped_count: int | None = None
        if configuration.previous_ok_only:
            original_point_count = len(points)
            try:
                points = filter_route_points_by_previous_status(
                    points,
                    configuration.previous_csv_path,
                    allowed_statuses={"ok"},
                )
            except (OSError, csv.Error) as exc:
                message = f"Unable to read previous route CSV: {exc}"
                self._show_status(message, 8000)
                if self._route_measurement_dialog is not None:
                    self._route_measurement_dialog.set_status(message)
                return
            previous_ok_skipped_count = original_point_count - len(points)
            if not points:
                message = "Previous route CSV has no OK points for this route."
                self._show_status(message, 8000)
                if self._route_measurement_dialog is not None:
                    self._route_measurement_dialog.set_status(message)
                return
        selected_point_number = int(configuration.current_point)
        selected_point = self._api_find_contact_point(points, selected_point_number)
        if selected_point is None:
            message = (
                f"Contact {selected_point_number} is not enabled or not included "
                "by the current route filter."
            )
            self._show_status(message, 6000)
            if self._route_measurement_dialog is not None:
                self._route_measurement_dialog.set_status(message)
            return
        self._set_route_measurement_resume_point(int(selected_point.index))
        if not self._route_measurement_session_active:
            self._route_measurement_session_active = True
            self._set_route_measurement_pending(True)
        self._route_measurement_runtime_configuration = configuration
        self._save_route_measurement_session_metadata(configuration)
        photo_enabled = configuration.operation_mode in {
            ROUTE_OPERATION_PHOTO,
            ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        }
        measure_enabled = configuration.operation_mode in {
            ROUTE_OPERATION_MEASURE,
            ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        }
        scale = self._active_microscope_scale()
        if photo_enabled and scale is None:
            message = (
                "Calibrate click-to-move for the active objective before saving "
                "microscope photos with a scale bar."
            )
            self._show_status(message, 8000)
            if self._route_measurement_dialog is not None:
                self._route_measurement_dialog.set_status(message)
            return
        if (
            not wait_before_first_point
            and (photo_enabled or configuration.photo_autofocus_enabled)
        ):
            frame, _counter = self._wait_for_camera_frame(timeout_s=0.1)
            if frame is None:
                message = (
                    "Camera frame is unavailable; cannot capture route photos."
                    if photo_enabled
                    else "Camera frame is unavailable; cannot autofocus route points."
                )
                self._show_status(message, 8000)
                self._send_telegram_alert(
                    "route_failed",
                    f"Probe route could not start:\n{message}",
                    attach_photo=True,
                )
                if self._route_measurement_dialog is not None:
                    self._route_measurement_dialog.set_status(message)
                return
        route_lcr_controller: object
        if measure_enabled:
            try:
                if self.lcr_controller.is_connected():
                    self.lcr_controller.apply_route_meter_configuration(
                        configuration.meter
                    )
                else:
                    self.lcr_controller.apply_route_meter_runtime_configuration(
                        configuration.meter
                    )
            except LCRMeterError as exc:
                message = f"Route measurement instrument setup failed: {exc}"
                self._show_status(message, 8000)
                if self._route_measurement_dialog is not None:
                    self._route_measurement_dialog.set_running(False)
                    self._route_measurement_dialog.set_status(message)
                return
            route_lcr_controller = self.lcr_controller
        else:
            route_lcr_controller = object()

        def on_contact_height_record(
            record: RouteContactHeightRecord,
            position: int,
            total: int,
        ) -> None:
            active_configuration = self._current_route_measurement_configuration(
                configuration
            )
            self._record_route_contact_height(
                record,
                position,
                total,
                csv_path=active_configuration.csv_path,
            )

        runner = RouteMeasurementRunner(
            points=points,
            csv_path=configuration.csv_path,
            stage_controller=self.stage_controller,
            lcr_controller=route_lcr_controller,
            needle_feedrate=self._current_needle_feedrate(),
            measurement_count=configuration.measurement_count,
            initial_measurement_count=configuration.initial_measurement_count,
            start_point_number=configuration.start_point,
            max_relative_rms=configuration.max_relative_rms,
            confirm_each_point=True,
            auto_next_ok_or_short=True,
            auto_contact_seek_on_bad_contact=True,
            auto_contact_seek_step_mm=configuration.contact_seek_step_mm,
            auto_contact_seek_max_total_mm=configuration.contact_seek_range_mm,
            contact_settle_s=configuration.contact_settle_s,
            nplc_label=configuration.meter.nplc_label(),
            measurement_type=configuration.meter.measurement_type_label(),
            status_callback=self.route_measurement_status.emit,
            progress_callback=self.route_measurement_progress.emit,
            record_callback=self.route_measurement_recorded.emit,
            photo_callback=lambda point, position, total, focus_result: self._capture_route_photo(
                point,
                position,
                total,
                configuration=self._current_route_measurement_configuration(
                    configuration
                ),
                focus_result=focus_result,
            ),
            photo_focus_callback=lambda point, position, total: self._route_photo_autofocus(
                point,
                position,
                total,
                configuration=self._current_route_measurement_configuration(
                    configuration
                ),
            ),
            photo_record_callback=self._record_route_photo,
            contact_height_record_callback=on_contact_height_record,
            contact_photo_callback=self._capture_route_contact_photo,
            pre_contact_photo_callback=self._capture_route_pre_contact_photo,
            result_callback=self.route_measurement_result.emit,
            waiting_callback=self.route_measurement_waiting_changed.emit,
            operation_mode=configuration.operation_mode,
            photo_settle_s=configuration.photo_settle_s,
            photo_focus_enabled=configuration.photo_autofocus_enabled,
            wait_before_first_point=wait_before_first_point,
        )
        self._route_measurement_runner = runner
        self._route_measurement_waiting = False
        self._pending_route_measure_point = None
        self._route_measurement_photo_enabled = photo_enabled
        self._route_measurement_measure_enabled = measure_enabled
        self._route_measurement_point_numbers = [int(point.index) for point in points]
        self._last_telegram_attention_message = ""
        with self._telegram_photo_lock:
            self._telegram_pending_contact_photo = None
            self._telegram_pending_contact_before_photo = None
            self._last_route_pre_contact_photo = None
            self._last_route_contact_failure_photo = None
            self._last_route_contact_failure_before_photo = None
        self._set_route_measurement_pending(True)
        self._route_measurement_thread = threading.Thread(
            target=self._run_route_measurement,
            args=(runner,),
            name="RouteMeasurement",
            daemon=True,
        )
        try:
            start_offset = next(
                index
                for index, point in enumerate(points)
                if int(point.index) == int(selected_point.index)
            )
        except StopIteration:
            start_offset = 0
        remaining_count = max(1, len(points) - start_offset)
        if wait_before_first_point:
            start_message = (
                "Preparing route measurement: "
                f"point {int(selected_point.index)} {selected_point.label}; "
                f"{remaining_count} points selected."
            )
        else:
            start_message = (
                "Route measurement starting at "
                f"point {int(selected_point.index)} {selected_point.label}; "
                f"{remaining_count} points remaining."
            )
        if previous_ok_skipped_count is not None:
            start_message = (
                f"{start_message} Previous filter skipped "
                f"{previous_ok_skipped_count} points."
            )
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_running(True)
            self.design_navigator_panel.set_route_measurement_waiting(False)
            self.design_navigator_panel.set_route_measurement_status(
                start_message
            )
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_running(True)
            self._route_measurement_dialog.reset_progress(len(points))
            self._route_measurement_dialog.set_status(start_message)
        self._show_status(start_message)
        self._last_route_measurement_result = None
        if not wait_before_first_point:
            self._send_telegram_alert(
                "route_started",
                f"Probe route started:\n{start_message}\nCSV: {configuration.csv_path}",
            )
        self._route_measurement_thread.start()
        self._update_stage_coordinate_apply_state()

    def _route_measurement_points(
        self,
        route: MeasurementRoute,
    ) -> list[RouteMeasurementPoint]:
        points: list[RouteMeasurementPoint] = []
        objective_settings = self.settings_manager.objectives_configuration()
        objective_profiles = objective_settings.objectives
        base_objective = base_objective_name(objective_profiles)
        active_objective = objective_settings.active_name
        base_offset = objective_xy_offset(objective_profiles, base_objective)
        active_offset = objective_xy_offset(objective_profiles, active_objective)
        for route_index, route_point in enumerate(route.points, start=1):
            if not route_point.enabled:
                continue
            camera_stage_xy = self._design_session.stage_from_design(
                route_point.camera_center
            )
            if camera_stage_xy is None:
                raise DesignModelError(
                    "Design registration is required before measuring a route."
                )
            contact_stage_xy = camera_stage_to_raw_stage(
                (float(camera_stage_xy[0]), float(camera_stage_xy[1])),
                base_offset,
            )
            photo_stage_xy = camera_stage_to_raw_stage(
                (float(camera_stage_xy[0]), float(camera_stage_xy[1])),
                active_offset,
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
                    stage_xy=(
                        float(contact_stage_xy[0]),
                        float(contact_stage_xy[1]),
                    ),
                    needle_1_design=(
                        float(needle_1_design[0]),
                        float(needle_1_design[1]),
                    ),
                    needle_2_design=(
                        float(needle_2_design[0]),
                        float(needle_2_design[1]),
                    ),
                    photo_stage_xy=(
                        float(photo_stage_xy[0]),
                        float(photo_stage_xy[1]),
                    ),
                )
            )
        return points

    def _capture_route_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        configuration: RouteMeasurementRunConfiguration,
        focus_result: object | None = None,
    ) -> str:
        scale = self._active_microscope_scale()
        if scale is None:
            raise RuntimeError("Active objective has no calibrated microscope scale.")
        before_counter = self._latest_camera_counter()
        frame, _counter = self._wait_for_camera_frame(
            after_counter=before_counter,
            timeout_s=2.0,
        )
        if frame is None:
            raise RuntimeError("Camera frame is unavailable.")
        captured_at = utc_timestamp()
        objective_name, magnification = self._active_objective_metadata()
        route = self._design_session.route
        route_name = route.name if route is not None else "route"
        filename = route_photo_filename(
            route_name=route_name,
            point_index=int(point.index),
            point_label=point.label,
            captured_at=captured_at,
        )
        photo_stage_xy = (
            point.photo_stage_xy
            if point.photo_stage_xy is not None
            else point.stage_xy
        )
        stage_position = self._stage_position_for_image_metadata(
            stage_xy=photo_stage_xy
        )
        focus_data = self._route_photo_focus_payload(focus_result)
        metadata = MicroscopeImageMetadata(
            title="Probe Station Microscope",
            mode="route photo"
            if configuration.operation_mode == ROUTE_OPERATION_PHOTO
            else "route photo before measurement",
            captured_at=captured_at,
            objective_name=objective_name,
            magnification=magnification,
            route_name=route_name,
            route_point_index=int(point.index),
            route_point_label=point.label,
            route_position=int(position),
            route_total=int(total),
            design_xy=point.design_center,
            stage_position=stage_position,
            stage_xy=photo_stage_xy,
            notes=(
                "needles raised before capture",
                *(
                    ("local autofocus before capture",)
                    if configuration.photo_autofocus_enabled
                    else ()
                ),
            ),
            extra={
                "point_id": point.point_id,
                "needle_1_design": list(point.needle_1_design),
                "needle_2_design": list(point.needle_2_design),
                "photo_autofocus_enabled": bool(
                    configuration.photo_autofocus_enabled
                ),
                "photo_autofocus_range_mm": float(
                    configuration.photo_autofocus_range_mm
                ),
                "autofocus": focus_data,
            },
        )
        result = save_microscope_image(
            frame=frame,
            output_dir=configuration.photo_output_dir,
            filename_stem=filename,
            metadata=metadata,
            scale=scale,
        )
        return str(result.image_path)

    def _route_photo_autofocus(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        configuration: RouteMeasurementRunConfiguration,
    ) -> object:
        _ = point
        self.route_measurement_status.emit(
            "Route photo autofocus: "
            f"point {position}/{total}, "
            f"+/-{configuration.photo_autofocus_range_mm:.3f} mm."
        )
        return self.stage_controller.run_external_local_autofocus(
            range_mm=configuration.photo_autofocus_range_mm,
        )

    @staticmethod
    def _route_photo_focus_payload(focus_result: object | None) -> dict[str, object] | None:
        if focus_result is None:
            return None
        if isinstance(focus_result, dict):
            return dict(focus_result)
        to_dict = getattr(focus_result, "to_dict", None)
        if callable(to_dict):
            data = to_dict()
            return dict(data) if isinstance(data, dict) else None
        return None

    def _record_route_photo(
        self,
        record: RoutePhotoRecord,
        position: int,
        total: int,
    ) -> None:
        self._maybe_send_requested_route_photo(record, position, total)
        if not record.focus:
            return
        try:
            path = self._route_photo_focus_map_path(record)
            path.parent.mkdir(parents=True, exist_ok=True)
            exists = path.exists() and path.stat().st_size > 0
            with path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=self._ROUTE_PHOTO_FOCUS_MAP_FIELDS,
                )
                if not exists:
                    writer.writeheader()
                writer.writerow(
                    self._route_photo_focus_map_row(record, position, total)
                )
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            logger.warning("Unable to write route photo focus map: %s", exc)

    def _maybe_send_requested_route_photo(
        self,
        record: RoutePhotoRecord,
        position: int,
        total: int,
    ) -> None:
        lock = getattr(self, "_telegram_photo_lock", None)
        if lock is None:
            return
        with lock:
            if not self._telegram_route_photo_requested:
                return
            self._telegram_route_photo_requested = False
        path = Path(record.path).expanduser()
        try:
            photo = (path.read_bytes(), path.name)
        except OSError as exc:
            logger.warning("Unable to read route photo for Telegram: %s", exc)
            self._send_telegram_bot_message(
                f"Route photo is saved but could not be read for Telegram: {path}",
                reply_markup=self._telegram_default_markup(),
            )
            return
        self._send_telegram_bot_message(
            "Next route structure photo:\n"
            f"Point {position}/{total}, structure {record.structure_number}, "
            f"{record.label}.",
            photo=photo,
            reply_markup=self._telegram_default_markup(),
        )

    def _telegram_route_attention_alert_enabled(self) -> bool:
        telegram_settings = self.settings_manager.telegram_configuration()
        return bool(
            telegram_settings.enabled
            and telegram_settings.chat_id.strip()
            and telegram_settings.alert_enabled("route_attention")
            and resolved_bot_token(telegram_settings)
        )

    def _capture_route_pre_contact_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> None:
        lock = getattr(self, "_telegram_photo_lock", None)
        if lock is None:
            return
        with lock:
            should_capture = self._telegram_contact_photo_requested
        if not should_capture and not self._telegram_route_attention_alert_enabled():
            return
        before_counter = self._latest_camera_counter()
        frame, _counter = self._wait_for_camera_frame(
            after_counter=before_counter,
            timeout_s=0.5,
        )
        photo = self._qimage_telegram_photo(frame) or self._latest_camera_frame_photo()
        if photo is None:
            logger.warning("Unable to capture route pre-contact photo for Telegram.")
            return
        caption = (
            "Route contact before needle press:\n"
            f"Point {position}/{total}, structure "
            f"{self._api_structure_number_for_measurement_point(point)}, "
            f"{point.label}."
        )
        with lock:
            self._last_route_pre_contact_photo = (
                int(position),
                int(point.index),
                photo[0],
                photo[1],
                caption,
            )

    def _capture_route_contact_photo(
        self,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        saved: bool,
    ) -> None:
        lock = getattr(self, "_telegram_photo_lock", None)
        if lock is None:
            return
        contact_attention = self._route_record_needs_contact_attention(record)
        with lock:
            should_capture = (
                self._telegram_contact_photo_requested
                or not saved
                or (
                    contact_attention
                    and self._telegram_route_attention_alert_enabled()
                )
            )
        if not should_capture:
            return
        before_counter = self._latest_camera_counter()
        frame, _counter = self._wait_for_camera_frame(
            after_counter=before_counter,
            timeout_s=1.0,
        )
        photo = self._qimage_telegram_photo(frame) or self._latest_camera_frame_photo()
        if photo is None:
            logger.warning("Unable to capture route contact photo for Telegram.")
            return
        caption = (
            "Route contact attempt photo:\n"
            f"Point {position}/{total}, structure {record.structure_number}, "
            f"{point.label}, status={record.status}."
        )
        before_photo = self._matching_route_pre_contact_photo(point, position)
        with self._telegram_photo_lock:
            if not saved or contact_attention:
                self._last_route_contact_failure_before_photo = before_photo
                self._last_route_contact_failure_photo = (
                    photo[0],
                    photo[1],
                    caption,
                )
            if self._telegram_contact_photo_requested:
                self._telegram_contact_photo_requested = False
                self._telegram_pending_contact_before_photo = before_photo
                self._telegram_pending_contact_photo = (
                    photo[0],
                    photo[1],
                    caption,
                )

    def _matching_route_pre_contact_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
    ) -> tuple[bytes, str, str] | None:
        with self._telegram_photo_lock:
            pre_photo = self._last_route_pre_contact_photo
        if pre_photo is None:
            return None
        photo_position, point_index, photo_bytes, photo_name, caption = pre_photo
        if photo_position != int(position) or point_index != int(point.index):
            return None
        return photo_bytes, photo_name, caption

    def _take_pending_telegram_contact_photos(
        self,
    ) -> tuple[tuple[bytes, str, str] | None, tuple[bytes, str, str]] | None:
        lock = getattr(self, "_telegram_photo_lock", None)
        if lock is None:
            return None
        with lock:
            before_photo = self._telegram_pending_contact_before_photo
            after_photo = self._telegram_pending_contact_photo
            self._telegram_pending_contact_before_photo = None
            self._telegram_pending_contact_photo = None
            if after_photo is None:
                return None
            return before_photo, after_photo

    def _latest_route_contact_failure_telegram_photos(
        self,
    ) -> tuple[tuple[bytes, str, str] | None, tuple[bytes, str, str]] | None:
        lock = getattr(self, "_telegram_photo_lock", None)
        if lock is None:
            return None
        with lock:
            after_photo = self._last_route_contact_failure_photo
            if after_photo is None:
                return None
            return self._last_route_contact_failure_before_photo, after_photo

    def _telegram_contact_photo_payload(
        self,
        before_photo: tuple[bytes, str, str] | None,
        after_photo: tuple[bytes, str, str],
    ) -> tuple[tuple[bytes, str], str]:
        caption = self._combined_route_contact_caption(
            before_photo[2] if before_photo is not None else "",
            after_photo[2],
        )
        if before_photo is None:
            return (after_photo[0], after_photo[1]), caption
        combined_photo = self._combine_telegram_contact_photos(
            before_photo[0],
            after_photo[0],
        )
        if combined_photo is None:
            logger.warning("Unable to combine route contact photos for Telegram.")
            return (after_photo[0], after_photo[1]), caption
        return combined_photo, caption

    @staticmethod
    def _combined_route_contact_caption(
        before_caption: str,
        after_caption: str,
    ) -> str:
        after_detail = "\n".join(str(after_caption or "").splitlines()[1:]).strip()
        if not after_detail:
            after_detail = str(after_caption or "").strip()
        if before_caption:
            prefix = (
                "Route contact check:\n"
                "Left: before needle press. Right: contact attempt."
            )
            return f"{prefix}\n{after_detail}" if after_detail else prefix
        return str(after_caption or "").strip()

    @staticmethod
    def _combine_telegram_contact_photos(
        before_bytes: bytes,
        after_bytes: bytes,
    ) -> tuple[bytes, str] | None:
        before_image = QImage()
        after_image = QImage()
        if not before_image.loadFromData(before_bytes):
            return None
        if not after_image.loadFromData(after_bytes):
            return None
        if before_image.isNull() or after_image.isNull():
            return None
        target_height = min(before_image.height(), after_image.height())
        if target_height <= 0:
            return None
        if before_image.height() != target_height:
            before_image = before_image.scaledToHeight(
                target_height,
                Qt.TransformationMode.SmoothTransformation,
            )
        if after_image.height() != target_height:
            after_image = after_image.scaledToHeight(
                target_height,
                Qt.TransformationMode.SmoothTransformation,
            )
        combined = QImage(
            before_image.width() + after_image.width(),
            target_height,
            QImage.Format.Format_RGB32,
        )
        combined.fill(Qt.GlobalColor.black)
        painter = QPainter(combined)
        painter.drawImage(0, 0, before_image)
        painter.drawImage(before_image.width(), 0, after_image)
        painter.end()
        encoded = Main._qimage_telegram_photo(combined)
        if encoded is None:
            return None
        return encoded[0], "route-contact-comparison.jpg"

    _ROUTE_PHOTO_FOCUS_MAP_FIELDS = (
        "timestamp",
        "route_name",
        "route_position",
        "route_total",
        "structure_number",
        "point_index",
        "point_id",
        "label",
        "design_x",
        "design_y",
        "stage_x",
        "stage_y",
        "photo_path",
        "objective_name",
        "focus_start_z_mm",
        "focus_best_z_mm",
        "focus_delta_um",
        "focus_score",
        "focus_sample_count",
        "focus_edge_peak",
        "autofocus_range_mm",
        "autofocus_fine_step_mm",
        "autofocus_lower_z_mm",
        "autofocus_upper_z_mm",
    )

    @staticmethod
    def _route_photo_focus_map_path(record: RoutePhotoRecord) -> Path:
        return Path(record.path).expanduser().resolve().parent / "route-photo-focus-map.csv"

    def _route_photo_focus_map_row(
        self,
        record: RoutePhotoRecord,
        position: int,
        total: int,
    ) -> dict[str, object]:
        route = self._design_session.route
        route_name = route.name if route is not None else ""
        focus = record.focus or {}
        return {
            "timestamp": record.timestamp,
            "route_name": route_name,
            "route_position": int(position),
            "route_total": int(total),
            "structure_number": int(record.structure_number),
            "point_index": int(record.point_index),
            "point_id": record.point_id,
            "label": record.label,
            "design_x": float(record.design_center[0]),
            "design_y": float(record.design_center[1]),
            "stage_x": float(record.stage_xy[0]),
            "stage_y": float(record.stage_xy[1]),
            "photo_path": record.path,
            "objective_name": focus.get("objective_name", ""),
            "focus_start_z_mm": focus.get("focus_start_z_mm", ""),
            "focus_best_z_mm": focus.get("focus_best_z_mm", ""),
            "focus_delta_um": focus.get("focus_delta_um", ""),
            "focus_score": focus.get("focus_score", ""),
            "focus_sample_count": focus.get("focus_sample_count", ""),
            "focus_edge_peak": focus.get("focus_edge_peak", ""),
            "autofocus_range_mm": focus.get("autofocus_range_mm", ""),
            "autofocus_fine_step_mm": focus.get("autofocus_fine_step_mm", ""),
            "autofocus_lower_z_mm": focus.get("autofocus_lower_z_mm", ""),
            "autofocus_upper_z_mm": focus.get("autofocus_upper_z_mm", ""),
        }

    def _record_route_contact_height(
        self,
        record: RouteContactHeightRecord,
        position: int,
        total: int,
        *,
        csv_path: str | Path,
    ) -> None:
        try:
            path = self._route_contact_height_map_path(csv_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            exists = path.exists() and path.stat().st_size > 0
            with path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=self._ROUTE_CONTACT_HEIGHT_MAP_FIELDS,
                )
                if not exists:
                    writer.writeheader()
                writer.writerow(
                    self._route_contact_height_map_row(record, position, total)
                )
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            logger.warning("Unable to write route contact height map: %s", exc)

    _ROUTE_CONTACT_HEIGHT_MAP_FIELDS = (
        "timestamp",
        "route_name",
        "route_position",
        "route_total",
        "structure_number",
        "point_index",
        "point_id",
        "label",
        "design_x",
        "design_y",
        "stage_x",
        "stage_y",
        "measurement_status",
        "resistance_ohm",
        "resistance_rms_ohm",
        "relative_rms",
        "contact_quality",
        "contact_median_ohm",
        "contact_mad_sigma_ohm",
        "contact_p95_abs_step_ohm",
        "contact_span_ohm",
        "contact_compliance_hits",
        "contact_found",
        "contact_depth_below_down_mm",
        "contact_axis_a_lowering_mm",
        "contact_seek_used",
        "contact_seek_found",
        "contact_seek_status",
        "contact_seek_attempts",
        "contact_seek_initial_status",
        "contact_seek_final_status",
        "contact_seek_depth_below_down_mm",
        "contact_seek_axis_a_lowering_mm",
        "contact_seek_step_mm",
        "contact_seek_max_depth_mm",
    )

    @staticmethod
    def _route_contact_height_map_path(csv_path: str | Path) -> Path:
        return (
            Path(csv_path)
            .expanduser()
            .resolve()
            .parent
            / "route-contact-height-map.csv"
        )

    def _route_contact_height_map_row(
        self,
        record: RouteContactHeightRecord,
        position: int,
        total: int,
    ) -> dict[str, object]:
        route = self._design_session.route
        route_name = route.name if route is not None else ""
        contact = record.contact_quality
        seek = record.contact_seek
        return {
            "timestamp": record.timestamp,
            "route_name": route_name,
            "route_position": int(position),
            "route_total": int(total),
            "structure_number": int(record.structure_number),
            "point_index": int(record.point_index),
            "point_id": record.point_id,
            "label": record.label,
            "design_x": float(record.design_center[0]),
            "design_y": float(record.design_center[1]),
            "stage_x": float(record.stage_xy[0]),
            "stage_y": float(record.stage_xy[1]),
            "measurement_status": record.measurement_status,
            "resistance_ohm": _csv_float(record.resistance_ohm),
            "resistance_rms_ohm": _csv_float(record.resistance_rms_ohm),
            "relative_rms": _csv_float(record.relative_rms),
            "contact_quality": "" if contact is None else contact.status,
            "contact_median_ohm": ""
            if contact is None
            else _csv_float(contact.median_ohm),
            "contact_mad_sigma_ohm": ""
            if contact is None
            else _csv_float(contact.mad_sigma_ohm),
            "contact_p95_abs_step_ohm": ""
            if contact is None
            else _csv_float(contact.p95_abs_step_ohm),
            "contact_span_ohm": ""
            if contact is None
            else _csv_float(contact.span_ohm),
            "contact_compliance_hits": ""
            if contact is None
            else int(contact.compliance_hits),
            "contact_found": _csv_bool(record.contact_found),
            "contact_depth_below_down_mm": _csv_float(
                record.contact_depth_below_down_mm
            ),
            "contact_axis_a_lowering_mm": _csv_float(
                record.contact_axis_a_lowering_mm
            ),
            "contact_seek_used": _csv_bool(seek is not None),
            "contact_seek_found": "" if seek is None else _csv_bool(seek.found),
            "contact_seek_status": "" if seek is None else seek.status,
            "contact_seek_attempts": "" if seek is None else int(seek.attempts),
            "contact_seek_initial_status": ""
            if seek is None
            else seek.initial_status,
            "contact_seek_final_status": "" if seek is None else seek.final_status,
            "contact_seek_depth_below_down_mm": ""
            if seek is None
            else _csv_float(seek.depth_below_down_mm),
            "contact_seek_axis_a_lowering_mm": ""
            if seek is None
            else _csv_float(seek.axis_a_lowering_mm),
            "contact_seek_step_mm": ""
            if seek is None
            else _csv_float(seek.step_mm),
            "contact_seek_max_depth_mm": ""
            if seek is None
            else _csv_float(seek.max_depth_mm),
        }

    def _run_route_measurement(self, runner: RouteMeasurementRunner) -> None:
        success, message = runner.run()
        csv_path = (
            ""
            if isinstance(runner, RouteExternalMeasurementSessionRunner)
            else str(runner.csv_path)
        )
        self.route_measurement_finished.emit(runner, success, message, csv_path)

    def _on_route_measurement_started(
        self,
        message: str,
        total: int,
        start_point: int,
        open_controls: bool,
    ) -> None:
        self._set_route_measurement_resume_point(int(start_point))
        self._set_route_measurement_pending(True)
        if open_controls:
            self._show_route_measurement_dialog_for_api_session()
        total_points = max(0, int(total))
        waiting = bool(getattr(self, "_route_measurement_waiting", False))
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_running(True)
            self.design_navigator_panel.set_route_measurement_waiting(waiting)
            self.design_navigator_panel.set_route_measurement_status(message)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_running(True)
            self._route_measurement_dialog.set_waiting(waiting)
            self._route_measurement_dialog.reset_progress(total_points)
            self._route_measurement_dialog.set_status(message)
        self._show_status(message)
        self._update_stage_coordinate_apply_state()

    def _request_stop_route_measurement(self) -> None:
        runner = self._route_measurement_runner
        if runner is None:
            self._show_status("No route measurement is running.", 3000)
            return
        self._pending_route_measure_point = None
        runner.stop()
        self._show_status("Stopping route measurement.")
        self._update_stage_coordinate_apply_state()
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_waiting(False)
            self.design_navigator_panel.set_route_measurement_status(
                "Stopping route measurement."
            )
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_waiting(False)
            self._route_measurement_dialog.set_status(
                "Stopping route measurement."
            )

    def _request_route_measurement_point_correction(
        self,
        pending_point_number: int | None = None,
    ) -> None:
        runner = self._route_measurement_runner
        if runner is None:
            self._show_status("No route measurement is running.", 3000)
            return
        if pending_point_number is None:
            self._pending_route_measure_point = None
        self._interrupt_route_measurement_runner(
            runner,
            reason="Route measurement interrupt requested.",
        )
        if pending_point_number is None:
            message = "Stopping contact measurement."
        else:
            message = (
                "Stopping contact measurement, then measuring "
                f"point {int(pending_point_number)}."
            )
        self._show_status(message, 5000)
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_status(message)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_status(message)

    def _submit_route_measurement_confirmation(self, action: str) -> None:
        runner = self._route_measurement_runner
        if runner is None:
            self._show_status("No route measurement is waiting.", 3000)
            return
        move_thread = getattr(self, "_route_contact_move_thread", None)
        if move_thread is not None and move_thread.is_alive():
            self._show_status("Wait for route contact move to finish.", 3000)
            return
        if self._route_measurement_dialog is not None:
            configuration = self._route_measurement_dialog.current_configuration()
            previous_configuration = self._route_measurement_runtime_configuration
            if (
                not isinstance(runner, RouteExternalMeasurementSessionRunner)
                and self._route_measurement_waiting
                and self._route_measurement_setup_changed(
                    previous_configuration,
                    configuration,
                )
            ):
                route_offset_xy = (
                    runner.route_offset_xy()
                    if hasattr(runner, "route_offset_xy")
                    else (0.0, 0.0)
                )
                if not self._restart_waiting_route_measurement(
                    configuration,
                    route_offset_xy=route_offset_xy,
                ):
                    return
                runner = self._route_measurement_runner
                if runner is None:
                    return
            self._route_measurement_runtime_configuration = configuration
            self._save_route_measurement_session_metadata(configuration)
            if isinstance(runner, RouteExternalMeasurementSessionRunner):
                runner.update_runtime_settings(
                    measurement_count=configuration.measurement_count,
                    initial_measurement_count=configuration.initial_measurement_count,
                    max_relative_rms=configuration.max_relative_rms,
                    auto_contact_seek_step_mm=configuration.contact_seek_step_mm,
                    auto_contact_seek_max_total_mm=configuration.contact_seek_range_mm,
                    contact_settle_s=configuration.contact_settle_s,
                    photo_settle_s=configuration.photo_settle_s,
                )
            else:
                runner.update_runtime_settings(
                    measurement_count=configuration.measurement_count,
                    initial_measurement_count=configuration.initial_measurement_count,
                    max_relative_rms=configuration.max_relative_rms,
                    auto_contact_seek_step_mm=configuration.contact_seek_step_mm,
                    auto_contact_seek_max_total_mm=configuration.contact_seek_range_mm,
                    contact_settle_s=configuration.contact_settle_s,
                    photo_settle_s=configuration.photo_settle_s,
                    photo_focus_enabled=configuration.photo_autofocus_enabled,
                    csv_path=configuration.csv_path,
                    nplc_label=configuration.meter.nplc_label(),
                    measurement_type=configuration.meter.measurement_type_label(),
                )
                measure_enabled = configuration.operation_mode in {
                    ROUTE_OPERATION_MEASURE,
                    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
                }
                if measure_enabled:
                    try:
                        runner.apply_meter_configuration(configuration.meter)
                    except LCRMeterError as exc:
                        message = f"Route measurement instrument setup failed: {exc}"
                        self._show_status(message, 8000)
                        self._route_measurement_dialog.set_status(message)
                        return
        if not runner.submit_confirmation(action):
            self._show_status("Unknown route measurement action.", 3000)
            return
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_waiting(False)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_waiting(False)
        action_key = str(action).strip().lower()
        if action_key == "measure":
            action_label = "measure"
        elif action_key == "remeasure":
            action_label = "remeasure"
        elif action_key == "skip":
            action_label = "skip"
        elif action_key.startswith("jump:") or action_key.isdigit():
            point_number = action_key.split(":", 1)[-1]
            action_label = f"measure from point {point_number}"
        else:
            action_label = "next"
        self._show_status(f"Route measurement: {action_label}.")

    def _submit_route_measurement_jump(self, point_number: int) -> None:
        self._submit_route_measurement_confirmation(f"jump:{int(point_number)}")

    def _request_route_contact_move(self, point_number: int) -> None:
        move_thread = getattr(self, "_route_contact_move_thread", None)
        if move_thread is not None and move_thread.is_alive():
            self._show_status("Route contact move is already active.", 3000)
            return
        route_thread = self._route_measurement_thread
        route_active = route_thread is not None and route_thread.is_alive()
        if route_active and not self._route_measurement_waiting:
            self._show_status(
                "Pause or wait for route measurement before moving to a contact.",
                5000,
            )
            return
        context_result = self._api_contact_context(int(point_number))
        if not context_result.get("accepted", False):
            message = str(context_result.get("message") or "Route contact move rejected.")
            self._show_status(message, 6000)
            if self._route_measurement_dialog is not None:
                self._route_measurement_dialog.set_status(message)
            if self.design_navigator_panel is not None:
                self.design_navigator_panel.set_route_measurement_status(message)
            return
        point = context_result["point"]
        if not route_active or self._route_measurement_waiting:
            self._set_route_measurement_resume_point(int(point.index))
            runner = self._route_measurement_runner
            if runner is not None and self._route_measurement_waiting:
                runner.set_current_adjustment_point(int(point.index))
        needle_feedrate = self._current_needle_feedrate()
        message = f"Route contact move: point {int(point.index)} {point.label}."
        self._show_status(message, 5000)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_status(message)
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_status(message)
        thread = threading.Thread(
            target=self._run_route_contact_move,
            args=(point, needle_feedrate),
            name="RouteContactMove",
            daemon=True,
        )
        self._route_contact_move_thread = thread
        thread.start()
        self._update_stage_coordinate_apply_state()

    def _run_route_contact_move(
        self,
        point: RouteMeasurementPoint,
        needle_feedrate: float | None,
    ) -> None:
        success = False
        message = "Route contact move stopped."
        active_stage_task = False
        try:
            self.stage_controller.begin_external_task("route contact move")
            active_stage_task = True
            self.route_measurement_status.emit(
                f"Route contact move: point {int(point.index)} {point.label}, "
                "raising needles."
            )
            self.stage_controller.run_external_needles_action(
                "raise",
                needle_feedrate,
            )
            self.route_measurement_status.emit(
                f"Route contact move: point {int(point.index)} {point.label}, moving."
            )
            self.stage_controller.run_external_move_to_xy(
                point.stage_xy[0],
                point.stage_xy[1],
            )
            success = True
            message = (
                f"Route contact move complete: point {int(point.index)} "
                f"{point.label}."
            )
        except StageControllerError as exc:
            message = f"Route contact move failed: {exc}"
        except Exception as exc:
            logger.exception("Route contact move failed")
            message = f"Route contact move failed: {exc}"
        finally:
            if active_stage_task:
                self.stage_controller.finish_external_task()
            self.route_contact_move_finished.emit(success, message)

    def _on_route_contact_move_finished(self, success: bool, message: str) -> None:
        thread = getattr(self, "_route_contact_move_thread", None)
        if thread is not None and not thread.is_alive():
            thread.join(timeout=0.1)
        self._route_contact_move_thread = None
        self._update_stage_coordinate_apply_state()
        timeout_ms = 5000 if success else 8000
        self._show_status(message, timeout_ms)
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_status(message)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_status(message)

    def _request_pause_route_measurement(self) -> None:
        runner = self._route_measurement_runner
        if runner is None:
            self._show_status("No route measurement is running.", 3000)
            return
        runner.request_pause_after_current_point()
        message = "Route measurement pause requested; will pause after current point."
        self._show_status(message, 5000)
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_status(message)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_status(message)

    def _save_route_measurement_shift(self, point_number: int | None = None) -> None:
        runner = self._route_measurement_runner
        thread = self._route_measurement_thread
        if runner is None or thread is None or not thread.is_alive():
            message = "Route measurement is not ready."
            self._show_status(message, 5000)
            if self.design_navigator_panel is not None:
                self.design_navigator_panel.set_route_measurement_status(message)
            if self._route_measurement_dialog is not None:
                self._route_measurement_dialog.set_status(message)
            return
        if not self._route_measurement_waiting:
            message = "Pause route measurement before saving shift."
            self._show_status(message, 5000)
            if self.design_navigator_panel is not None:
                self.design_navigator_panel.set_route_measurement_status(message)
            if self._route_measurement_dialog is not None:
                self._route_measurement_dialog.set_status(message)
            return
        if point_number is None:
            if self._route_measurement_dialog is not None:
                point_number = int(
                    self._route_measurement_dialog.current_configuration().current_point
                )
            elif self._route_measurement_current_point is not None:
                point_number = int(self._route_measurement_current_point)
        if point_number is not None:
            point_selected, message = runner.set_current_adjustment_point(
                int(point_number)
            )
            if not point_selected:
                self._show_status(message, 6000)
                if self.design_navigator_panel is not None:
                    self.design_navigator_panel.set_route_measurement_status(message)
                if self._route_measurement_dialog is not None:
                    self._route_measurement_dialog.set_status(message)
                return
        try:
            position = self.stage_controller.current_stage_position()
        except StageControllerError as exc:
            latest = self.stage_controller.latest_stage_position()
            if self.stage_controller.is_busy() or latest is None:
                message = str(exc)
                self._show_status(message, 6000)
                if self._route_measurement_dialog is not None:
                    self._route_measurement_dialog.set_status(message)
                return
            position = latest
        stage_xy = self._stage_xy_from_position(position)
        if stage_xy is None:
            message = "Current stage X/Y position is unavailable."
            self._show_status(message, 5000)
            if self._route_measurement_dialog is not None:
                self._route_measurement_dialog.set_status(message)
            return
        saved, message = runner.save_current_position_adjustment(stage_xy)
        self._show_status(message, 5000)
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_status(message)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_status(message)

    def _interrupt_route_measurement_runner(
        self,
        runner: object,
        *,
        reason: str,
    ) -> None:
        try:
            waiting = bool(
                runner.status_payload().get("waiting")
                if hasattr(runner, "status_payload")
                else self._route_measurement_waiting
            )
        except Exception:
            waiting = bool(self._route_measurement_waiting)
        if hasattr(runner, "request_current_point_correction"):
            runner.request_current_point_correction()
        if not waiting:
            self.stage_controller.cancel_active_task(reason)
            self._clear_stage_motion_axes()
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)

    def _on_route_measurement_status(self, message: str) -> None:
        self._show_status(message)
        if self._route_attention_status(message):
            self._send_route_attention_alert(message)
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_status(message)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_status(message)

    def _on_route_measurement_progress(
        self,
        position: int,
        total: int,
        point_number: int,
    ) -> None:
        self._set_route_measurement_resume_point(point_number)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_progress(
                position,
                total,
                point_number,
            )

    def _on_route_measurement_current_point_changed(self, point_number: int) -> None:
        self._set_route_measurement_resume_point(point_number)
        runner = self._route_measurement_runner
        if runner is not None and self._route_measurement_waiting:
            runner.set_current_adjustment_point(point_number)

    def _on_route_measurement_waiting_changed(self, waiting: bool) -> None:
        self._route_measurement_waiting = bool(waiting)
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_waiting(waiting)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_waiting(waiting)
        if not self._route_measurement_waiting:
            return
        pending_point_number = self._pending_route_measure_point
        if pending_point_number is None:
            self._send_route_waiting_attention_from_last_result()
            return
        self._pending_route_measure_point = None
        self._submit_route_measurement_confirmation(
            f"jump:{int(pending_point_number)}"
        )

    def _on_route_measurement_result(
        self,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        saved: bool,
    ) -> None:
        self._last_route_measurement_result = (
            record,
            int(position),
            int(total),
            bool(saved),
        )
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_result(
                record,
                position,
                total,
                saved,
            )
        pending_contact_photos = self._take_pending_telegram_contact_photos()
        if pending_contact_photos is not None:
            before_photo, after_photo = pending_contact_photos
            photo, caption = self._telegram_contact_photo_payload(
                before_photo,
                after_photo,
            )
            self._send_telegram_bot_message(
                caption,
                photo=photo,
                reply_markup=self._telegram_default_markup(),
            )
        if not saved:
            message = self._format_route_measurement_record(
                record,
                position,
                total,
                saved=saved,
            )
            self._show_status(message, 8000)
            if self.design_navigator_panel is not None:
                self.design_navigator_panel.set_route_measurement_status(message)
            if self._route_measurement_dialog is not None:
                self._route_measurement_dialog.set_status(message)

    @staticmethod
    def _route_record_needs_contact_attention(
        record: RouteMeasurementRecord,
    ) -> bool:
        if record.status == "bad_contact":
            return True
        contact = record.contact_quality
        return contact is not None and contact.good is False

    def _send_route_waiting_attention_from_last_result(self) -> None:
        last_result = self._last_route_measurement_result
        if last_result is None:
            return
        record, position, total, saved = last_result
        if not self._route_record_needs_contact_attention(record):
            return
        message = self._format_route_measurement_record(
            record,
            position,
            total,
            saved=saved,
        )
        self._send_route_attention_alert(message, include_contact_photos=True)

    def _send_route_attention_alert(
        self,
        message: str,
        *,
        include_contact_photos: bool = False,
    ) -> None:
        if message == self._last_telegram_attention_message:
            return
        self._last_telegram_attention_message = message
        failure_photos = (
            self._latest_route_contact_failure_telegram_photos()
            if include_contact_photos
            else None
        )
        before_photo = failure_photos[0] if failure_photos is not None else None
        failure_photo = failure_photos[1] if failure_photos is not None else None
        alert_message = f"Probe route needs attention:\n{message}"
        alert_photo: tuple[bytes, str] | None = None
        if failure_photo is not None:
            alert_photo, contact_caption = self._telegram_contact_photo_payload(
                before_photo,
                failure_photo,
            )
            if contact_caption:
                alert_message = f"{alert_message}\n{contact_caption}"
        self._send_telegram_alert(
            "route_attention",
            alert_message,
            attach_photo=alert_photo is None,
            photo=alert_photo,
            reply_markup=self._telegram_route_actions_markup(),
        )

    def _on_route_measurement_recorded(
        self,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
    ) -> None:
        message = self._format_route_measurement_record(
            record,
            position,
            total,
            saved=True,
        )
        self._show_status(message)
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_status(message)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_status(message)
        next_point_number = self._route_measurement_next_point_number(position)
        if next_point_number is not None:
            self._set_route_measurement_resume_point(next_point_number)

    @staticmethod
    def _format_route_measurement_record(
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        *,
        saved: bool,
    ) -> str:
        prefix = "Measured" if saved else "Rejected"
        if saved and record.status == "short":
            prefix = "Short"
        contact = record.contact_quality
        contact_text = Main._format_route_contact_diagnostics(contact)
        return (
            f"{prefix} route point {position}/{total}: "
            f"n={int(record.n_measurements)}, "
            f"R={_format_route_ohm(record.resistance_ohm)}, "
            f"RMS={_format_route_ohm(record.resistance_rms_ohm)}, "
            f"rel={_format_route_percent(record.relative_rms)}, "
            f"status={record.status}{contact_text}."
        )

    @staticmethod
    def _format_route_contact_diagnostics(contact: object | None) -> str:
        if contact is None or not bool(getattr(contact, "assessed", False)):
            return ""
        reasons = tuple(getattr(contact, "reasons", ()) or ())
        reason_text = ", ".join(str(reason) for reason in reasons) if reasons else "none"
        return (
            f", contact={getattr(contact, 'status', 'unknown')}, "
            f"reasons={reason_text}, "
            f"median={_format_route_ohm(float(getattr(contact, 'median_ohm', math.nan)))}, "
            f"MAD={_format_route_ohm(float(getattr(contact, 'mad_sigma_ohm', math.nan)))}, "
            f"p95_step={_format_route_ohm(float(getattr(contact, 'p95_abs_step_ohm', math.nan)))}, "
            f"span={_format_route_ohm(float(getattr(contact, 'span_ohm', math.nan)))}, "
            f"compliance_hits={int(getattr(contact, 'compliance_hits', 0))}, "
            "polarity_mismatches="
            f"{int(getattr(contact, 'polarity_sign_mismatch_count', 0))}"
        )

    def _on_route_measurement_finished(self, *args: object) -> None:
        if len(args) == 4:
            finished_runner, success, message, csv_path = args
        elif len(args) == 3:
            finished_runner = None
            success, message, csv_path = args
        else:
            return
        if (
            finished_runner is not None
            and finished_runner is not self._route_measurement_runner
        ):
            return
        success = bool(success)
        message = str(message)
        csv_path = str(csv_path)
        measure_enabled = self._route_measurement_measure_enabled
        context_close_requested = bool(
            getattr(self, "_route_measurement_context_close_requested", False)
        )
        self._route_measurement_context_close_requested = False
        thread = self._route_measurement_thread
        if thread is not None and not thread.is_alive():
            thread.join(timeout=0.1)
        self._route_measurement_thread = None
        runner = self._route_measurement_runner
        if (
            getattr(self, "_api_route_session_id", None)
            and runner is not None
            and hasattr(runner, "status_payload")
        ):
            try:
                self._api_route_last_status = runner.status_payload()
            except Exception:
                logger.exception("Failed to store final API route session status.")
        self._route_measurement_runner = None
        self._api_route_lcr_controller = None
        self._route_measurement_runtime_configuration = None
        self._route_measurement_waiting = False
        self._last_route_measurement_result = None
        self._pending_route_measure_point = None
        self._route_measurement_photo_enabled = False
        self._route_measurement_measure_enabled = False
        self._resume_resistance_standby_polling()
        with self._telegram_photo_lock:
            self._telegram_route_photo_requested = False
            self._telegram_contact_photo_requested = False
            self._telegram_pending_contact_before_photo = None
            self._telegram_pending_contact_photo = None
            self._last_route_pre_contact_photo = None
        self._update_stage_coordinate_apply_state()
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_route_measurement_running(False)
            self.design_navigator_panel.set_route_measurement_waiting(False)
            self.design_navigator_panel.set_route_measurement_status(message)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_running(False)
            self._route_measurement_dialog.finish_progress(success)
            self._route_measurement_dialog.set_status(message)
        if success:
            session_measurement_count = (
                self._route_measurement_csv_record_count(csv_path)
                if measure_enabled and csv_path
                else None
            )
            if len(self._route_measurement_point_numbers) > 1:
                self._set_route_measurement_resume_point(1)
            self._set_route_measurement_pending(False)
            self._route_measurement_point_numbers = []
            suffix = (
                f" CSV: {csv_path}"
                if "CSV:" not in message
                and not message.startswith("Route photo capture")
                and csv_path
                else ""
            )
            self._show_status(f"{message}{suffix}", 8000)
            completion_message = f"Probe route completed:\n{message}"
            if session_measurement_count is not None:
                completion_message = (
                    f"{completion_message}\n"
                    f"Session total: {session_measurement_count} measurements in CSV."
                )
            if csv_path:
                completion_message = f"{completion_message}\nCSV: {csv_path}"
            self._send_telegram_alert(
                "route_completed",
                completion_message,
                document_path=Path(csv_path) if csv_path else None,
            )
        else:
            if self._route_measurement_current_point is not None:
                self._set_route_measurement_resume_point(
                    self._route_measurement_current_point
                )
            self._set_route_measurement_pending(True)
            self._show_status(message, 8000)
            if not context_close_requested:
                failure_message = f"Probe route stopped or failed:\n{message}"
                if csv_path:
                    failure_message = f"{failure_message}\nCSV: {csv_path}"
                self._send_telegram_alert(
                    "route_failed",
                    failure_message,
                    attach_photo=True,
                )

    @staticmethod
    def _route_measurement_csv_record_count(csv_path: str | Path) -> int | None:
        try:
            path = Path(csv_path).expanduser()
        except TypeError:
            return None
        if not path.exists():
            return 0
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                return sum(1 for row in reader if row)
        except OSError:
            logger.exception("Failed to count route measurement CSV records.")
            return None

    def _route_measurement_next_point_number(self, position: int) -> int | None:
        try:
            ordinal = int(position)
        except (TypeError, ValueError):
            return None
        index = ordinal
        if index < 0 or index >= len(self._route_measurement_point_numbers):
            return None
        return int(self._route_measurement_point_numbers[index])

    def _set_route_measurement_resume_point(self, point_number: int) -> None:
        try:
            value = int(point_number)
        except (TypeError, ValueError):
            return
        if value < 1:
            return
        self._route_measurement_current_point = value
        self._select_route_point_for_measurement(value)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_current_point(value)
            return
        self._save_route_measurement_current_point(value)

    def _select_route_point_for_measurement(self, point_number: int) -> None:
        route = self._design_session.route
        if route is None or not route.points:
            return
        index = int(point_number) - 1
        if not 0 <= index < len(route.points):
            return
        if self._design_session.selected_route_point_index == index:
            return
        point = self._design_session.select_route_point(index)
        self._last_selected_design_point = (
            point.camera_center if point is not None else None
        )
        self._refresh_design_panel()
        self._persist_controller_state_if_available()

    def _save_route_measurement_current_point(self, point_number: int) -> None:
        settings_path = (
            self.settings_manager.config_dir() / "route-measurement-settings.json"
        )
        data: dict[str, object] = {}
        if settings_path.exists():
            try:
                with settings_path.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    data = dict(loaded)
            except (OSError, json.JSONDecodeError):
                data = {}
        data["current_point"] = int(point_number)
        data["start_point"] = int(point_number)
        data["measurement_session_active"] = bool(
            self._route_measurement_session_active
        )
        data["measurement_pending"] = bool(self._route_measurement_session_active)
        try:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            with settings_path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
        except OSError:
            logger.exception("Failed to persist route measurement resume point.")

    def _set_route_measurement_pending(self, pending: bool) -> None:
        self._route_measurement_session_active = bool(pending)
        if self._route_measurement_dialog is not None:
            self._route_measurement_dialog.set_measurement_session_active(bool(pending))
            return
        self._save_route_measurement_pending(bool(pending))

    def _save_route_measurement_pending(self, pending: bool) -> None:
        settings_path = (
            self.settings_manager.config_dir() / "route-measurement-settings.json"
        )
        data: dict[str, object] = {}
        if settings_path.exists():
            try:
                with settings_path.open("r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    data = dict(loaded)
            except (OSError, json.JSONDecodeError):
                data = {}
        data["measurement_session_active"] = bool(pending)
        data["measurement_pending"] = bool(pending)
        try:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            with settings_path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
        except OSError:
            logger.exception("Failed to persist route measurement pending state.")

    def _save_route_measurement_session_metadata(
        self,
        configuration: RouteMeasurementRunConfiguration | None,
    ) -> None:
        settings_path = (
            self.settings_manager.config_dir() / "route-measurement-settings.json"
        )
        data = self._load_route_measurement_settings()
        route = self._design_session.route
        if route is not None:
            data["session_route_name"] = route.name
            data["session_route_point_count"] = len(route.points)
            if route.path is not None:
                data["session_route_path"] = str(route.path)
        if configuration is not None:
            data["csv_path"] = configuration.csv_path
            data["operation_mode"] = configuration.operation_mode
            data["photo_output_dir"] = configuration.photo_output_dir
            data["current_point"] = int(configuration.current_point)
            data["start_point"] = int(configuration.current_point)
        data["measurement_session_active"] = bool(
            self._route_measurement_session_active
        )
        data["measurement_pending"] = bool(self._route_measurement_session_active)
        try:
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            with settings_path.open("w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, ensure_ascii=False)
        except OSError:
            logger.exception("Failed to persist route measurement session metadata.")

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

    def _open_design_window_from_minimap_point(
        self,
        x_value: float,
        y_value: float,
    ) -> None:
        _ = float(x_value), float(y_value)
        self._toggle_design_layout_window(True)

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
        if (
            getattr(self, "_coordinate_move_axis", None) is not None
            and latest_state in {"run", "jog"}
        ):
            self._coordinate_move_seen_active_state = True
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
        self._set_joystick_control_mode_for_coordinate_apply()
        feedrate = self._coordinate_feedrate_for_axes(targets)
        self._start_coordinate_targets_move(
            targets,
            feedrate_mm_min=feedrate,
            source_label="coordinate fields",
        )
        self._update_stage_coordinate_apply_state()

    def _set_joystick_control_mode_for_coordinate_apply(self) -> None:
        joystick = self.joystick_panel
        if joystick is None:
            return
        setter = getattr(joystick, "set_control_mode", None)
        if callable(setter):
            try:
                setter("jog", emit_changed=True)
                return
            except TypeError:
                setter("jog")
                return
        private_setter = getattr(joystick, "_set_control_mode", None)
        if callable(private_setter):
            private_setter("jog", emit_changed=True)

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
        self,
        axis: str,
        raw_target: float,
        display_target: float,
        *,
        feedrate_mm_min: float | None = None,
    ) -> bool:
        return self._start_coordinate_targets_move(
            {axis: (raw_target, display_target)},
            feedrate_mm_min=(
                self._coordinate_feedrate_for_axes((axis,))
                if feedrate_mm_min is None
                else feedrate_mm_min
            ),
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
        feedrate = max(self.MIN_FEEDRATE_MM_MIN, float(feedrate_mm_min))
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
        self._coordinate_move_seen_active_state = False
        self._coordinate_move_ends_at = self._coordinate_move_started_at + max(
            self._coordinate_move_duration_s(
                self._coordinate_move_origin_position,
                target_position,
                feedrate,
            ),
            0.05,
        )
        self._set_stage_motion_axes(set(axes))
        self._activate_coordinate_common_feedrate(axes, feedrate)
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

    def _activate_coordinate_common_feedrate(
        self,
        axes: list[str],
        feedrate: float,
    ) -> None:
        if self.joystick_panel is None:
            return
        axis_set = {str(axis).strip().upper() for axis in axes}
        if len(axis_set) <= 1 or axis_set <= {"X", "Y"}:
            if hasattr(self.joystick_panel, "clear_common_feedrate_target"):
                self.joystick_panel.clear_common_feedrate_target()
            return
        limits = self.stage_controller.axis_max_feedrates()
        axis_limits: list[float] = []
        for axis in axes:
            try:
                value = float(limits.get(axis, 0.0))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0.0:
                axis_limits.append(value)
        max_feedrate = (
            max(axis_limits)
            if axis_limits
            else max(self.MIN_FEEDRATE_MM_MIN, float(feedrate))
        )
        if hasattr(self.joystick_panel, "set_common_feedrate_target"):
            self.joystick_panel.set_common_feedrate_target(feedrate, max_feedrate)

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
        speed_mm_per_s = max(self.MIN_FEEDRATE_MM_MIN, float(feedrate_mm_min)) / 60.0
        return (distance / speed_mm_per_s) + self.PLANNED_MOVE_DURATION_PADDING_S

    def _coordinate_position_is_at_target(self, position: object | None) -> bool:
        target_position = self._coordinate_move_target_position
        if target_position is None or not isinstance(position, (tuple, list)):
            return False
        active_axes = set(self._coordinate_move_axes)
        if not active_axes and self._coordinate_move_axis is not None:
            active_axes.add(self._coordinate_move_axis)
        if not active_axes:
            return False
        for axis in active_axes:
            try:
                axis_index = self.STAGE_AXIS_NAMES.index(axis)
            except ValueError:
                return False
            if axis_index >= len(position) or axis_index >= len(target_position):
                return False
            if (
                abs(float(position[axis_index]) - float(target_position[axis_index]))
                > self.COORDINATE_MOVE_TARGET_TOLERANCE_MM
            ):
                return False
        return True

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
        latest_state = (self.stage_controller.latest_stage_state() or "").lower()
        if not self.stage_controller.is_busy() and latest_state in {"", "idle"}:
            logger.debug(
                "Ignoring feedrate change for stale coordinate move tracking."
            )
            self._clear_coordinate_move_tracking(
                clear_pending=False,
                reset_override=False,
            )
            self._clear_stage_motion_axes()
            return
        try:
            requested_feedrate = max(
                self.MIN_FEEDRATE_MM_MIN,
                float(feedrate_mm_min),
            )
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
            replace_active = self.stage_controller.is_busy()
            if replace_active:
                self._coordinate_move_reissue_cancel_pending = True
            accepted = self.stage_controller.queue_absolute_axis_targets_jog(
                raw_targets,
                feedrate=requested_feedrate,
                replace_active=True,
            )
        except Exception as error:  # pragma: no cover - UI safety guard
            logger.exception("Failed to update coordinate move feedrate.")
            accepted = False
            self._show_status(str(error), 3000)
        if not accepted:
            self._coordinate_move_reissue_cancel_pending = False
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
        self._coordinate_move_seen_active_state = False
        if clear_pending:
            self._pending_stage_axis_targets.clear()
        if reset_override:
            self.stage_controller.queue_feed_override_reset()
        if self.joystick_panel is not None:
            self.joystick_panel.clear_temporary_linear_feedrate_bounds()
            if hasattr(self.joystick_panel, "clear_common_feedrate_target"):
                self.joystick_panel.clear_common_feedrate_target()
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
        if not self._coordinate_position_is_at_target(position):
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
        if (
            self._homing_active_key is None
            and not self.stage_controller.is_busy()
            and not self._controller_latest_state_blocks_motion()
            and self._coordinate_move_axis is None
        ):
            first_axis = normalized.pop(0)
            if not self.stage_controller.request_home_axis(first_axis):
                normalized.insert(0, first_axis)
        self._pending_homing_axes.extend(normalized)
        self._refresh_pending_homing_ui()
        self._update_stage_coordinate_apply_state()
        if self._pending_homing_axes and self._homing_active_key is None:
            QTimer.singleShot(200, self._start_next_pending_homing_action)

    def _start_next_pending_homing_action(self) -> None:
        if self._homing_active_key is not None or not self._pending_homing_axes:
            return
        if self.stage_controller.is_busy() or self._coordinate_move_axis is not None:
            QTimer.singleShot(200, self._start_next_pending_homing_action)
            return
        if self._controller_latest_state_blocks_motion():
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
        self._update_stage_coordinate_apply_state()

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
            self._update_stage_coordinate_apply_state()
            return
        if key in {"X", "Y", "B", "ALL"}:
            self._invalidate_design_registration(
                f"Design registration cleared after homing {key}."
            )
        self._clear_stage_motion_axes()
        self._refresh_pending_homing_ui()
        self._update_stage_coordinate_apply_state()
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
        self._update_stage_coordinate_apply_state()

    def _on_needles_action_started(self, _action: str) -> None:
        self._set_stage_motion_axes({"A"})
        self._update_stage_coordinate_apply_state()

    def _on_needles_action_finished(
        self, _success: bool, _message: str, _action: str
    ) -> None:
        self._clear_stage_motion_axes()
        self._update_stage_coordinate_apply_state()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        serial_was_connected = bool(
            self.serial_connection is not None and self.serial_connection.is_open
        )
        lcr_was_connected = bool(self.lcr_controller.is_connected())
        self._persist_serial_connection_state(serial_was_connected)
        self._persist_lcr_connection_state(lcr_was_connected)
        if serial_was_connected:
            self._persist_controller_state()
        if self._api_server is not None:
            self._api_server.stop()
        self._stop_telegram_bot_service()
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
        if self._microscope_scan_thread is not None:
            self._microscope_scan_stop_requested.set()
        if (
            self._microscope_scan_thread is not None
            and self._microscope_scan_thread.is_alive()
        ):
            self._microscope_scan_thread.join(timeout=2.0)
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
        self._close_auxiliary_windows(force_route_dialog=True)
        if self.serial_connection_panel:
            self.serial_connection_panel.shutdown()
        event.accept()

    def _close_auxiliary_windows(self, *, force_route_dialog: bool = False) -> None:
        if self._route_measurement_dialog is not None:
            if force_route_dialog:
                self._route_measurement_dialog.set_running(False)
            self._route_measurement_dialog.close()
        if self.design_layout_window is not None:
            self.design_layout_window.close()
        if self.contact_calibration_window is not None:
            self.contact_calibration_window.close()
        if self.surface_map_window is not None:
            self.surface_map_window.close()
        if self.microscope_scan_dialog is not None:
            self.microscope_scan_dialog.close()
        serial_connection_dialog = getattr(self, "serial_connection_dialog", None)
        if serial_connection_dialog is not None:
            serial_connection_dialog.close()

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
        self.serial_connection_dialog = QDialog(self)
        self.serial_connection_dialog.setWindowTitle("Connection")
        self.serial_connection_dialog.setModal(False)
        self.serial_connection_dialog.setMinimumWidth(420)
        self.serial_connection_dialog.resize(640, 520)

        dialog_layout = QVBoxLayout(self.serial_connection_dialog)
        dialog_layout.setContentsMargins(8, 8, 8, 8)
        dialog_layout.setSpacing(8)

        self.serial_connection_tabs = QTabWidget(self.serial_connection_dialog)
        dialog_layout.addWidget(self.serial_connection_tabs)

        self.serial_connection_panel = SerialConnectionPanel(
            self.serial_connection_tabs
        )
        self.serial_connection_tabs.addTab(self.serial_connection_panel, "Connection")

        self.serial_terminal_panel = SerialTerminalWindow(self.serial_connection_tabs)
        self.serial_terminal_panel.set_stage_controller(self.stage_controller)
        self.serial_terminal_panel.set_serial(self.serial_connection)
        self.serial_terminal_panel.manual_command_sent.connect(
            self._on_manual_terminal_command
        )
        self.serial_connection_tabs.addTab(self.serial_terminal_panel, "Terminal")

        close_button_row = QHBoxLayout()
        close_button_row.addStretch(1)
        close_button = QPushButton("Close", self.serial_connection_dialog)
        close_button.clicked.connect(self.serial_connection_dialog.close)
        close_button_row.addWidget(close_button)
        dialog_layout.addLayout(close_button_row)

        self.serial_connection_panel.connected.connect(self.on_serial_connected)
        self.serial_connection_panel.disconnected.connect(self.on_serial_disconnected)
        self.serial_connection_panel.lcr_connect_requested.connect(
            self.lcr_controller.request_connect
        )
        self.serial_connection_panel.lcr_disconnect_requested.connect(
            self._request_lcr_disconnect
        )

        self.resistance_panel = ResistanceMonitorPanel(self)
        self.resistance_panel.set_standby_enabled(
            self.lcr_controller.live_polling_enabled()
        )
        self.resistance_panel.standby_enabled_changed.connect(
            self._on_resistance_standby_enabled_changed
        )
        self.resistance_dock = CollapsibleDockWidget("Resistance", self)
        self.resistance_dock.setObjectName("ResistanceDock")
        self.resistance_dock.setWidget(self.resistance_panel)
        self.resistance_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self.resistance_dock)

        self.lcr_controller.status_message.connect(
            self.serial_connection_panel.set_lcr_status_message
        )
        self.lcr_controller.status_message.connect(
            self.resistance_panel.set_status_message
        )

        self.joystick_panel = JoystickWindow(self)
        self.joystick_panel.set_stage_controller(self.stage_controller)
        self._apply_axis_feedrate_limits(self._current_axis_feedrate_limits())
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
            jog.manual_axis,
            jog.manual_axis_distance_mm,
            jog.manual_axis_mode,
            jog.manual_axis_feedrate_mm_min,
            jog.focus_feedrate_mm_min,
            jog.turntable_feedrate_mm_min,
            jog.mode,
            focus_step_feedrate_mm_min=jog.focus_step_feedrate_mm_min,
            needle_step_feedrate_mm_min=jog.needles_step_feedrate_mm_min,
            turntable_step_feedrate_mm_min=jog.turntable_step_feedrate_mm_min,
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
        self.joystick_panel.needles_lift_requested.connect(
            self.stage_controller.request_needles_lift
        )
        self.joystick_panel.needles_lower_requested.connect(
            self.stage_controller.request_needles_lower
        )
        self.joystick_panel.needle_current_lower_contact_save_requested.connect(
            self._save_current_needle_height
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
        self.joystick_panel.control_mode_changed.connect(
            self._save_jog_control_mode
        )
        self.joystick_panel.linear_feedrate_changed.connect(
            self._on_linear_feedrate_changed
        )
        self.joystick_panel.common_feedrate_changed.connect(
            self._apply_coordinate_move_feedrate
        )
        self.joystick_panel.step_feedrate_changed.connect(
            self._on_step_feedrate_changed
        )
        self.joystick_panel.focus_feedrate_changed.connect(
            self._on_focus_feedrate_changed
        )
        self.joystick_panel.focus_step_feedrate_changed.connect(
            self._on_focus_step_feedrate_changed
        )
        self.joystick_panel.needle_feedrate_changed.connect(
            self._on_needle_feedrate_changed
        )
        self.joystick_panel.needle_step_feedrate_changed.connect(
            self._on_needle_step_feedrate_changed
        )
        self.joystick_panel.turntable_feedrate_changed.connect(
            self._on_turntable_feedrate_changed
        )
        self.joystick_panel.turntable_step_feedrate_changed.connect(
            self._on_turntable_step_feedrate_changed
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
        self.stage_controller.needles_zone_changed.connect(
            self.joystick_panel.set_needles_zone
        )
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
            lambda: self.stage_controller.reset_controller(
                source="joystick_reset_button"
            )
        )
        self.stage_controller.stage_position_changed.connect(self._persist_controller_state)
        self.joystick_dock = CollapsibleDockWidget("Joystick", self)
        self.joystick_dock.setObjectName("JoystickDock")
        self.joystick_dock.setWidget(self.joystick_panel)
        self.joystick_dock.setAllowedAreas(
            Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea
        )
        self.addDockWidget(Qt.LeftDockWidgetArea, self.joystick_dock)
        self.splitDockWidget(self.resistance_dock, self.joystick_dock, Qt.Vertical)

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
        self.contact_calibration_window.contact_seek_requested.connect(
            self._request_contact_seek
        )
        self.contact_calibration_window.contact_seek_cancel_requested.connect(
            self._cancel_contact_seek
        )
        self.lcr_controller.connection_changed.connect(
            self._on_lcr_connection_changed
        )
        self.lcr_controller.reading_started.connect(
            self._on_lcr_reading_started
        )
        self.lcr_controller.reading_summary_updated.connect(
            self._on_lcr_reading_summary_updated
        )
        self.lcr_controller.reading_updated.connect(
            self._on_lcr_reading_updated
        )

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
            [self.resistance_dock, self.joystick_dock],
            [130, 430],
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
            self.surface_map_window.capture_running_changed.connect(
                lambda _running: self._update_stage_coordinate_apply_state()
            )
        self.surface_map_window.showNormal()
        self.surface_map_window.raise_()

    def _show_microscope_scan_dialog(self) -> None:
        from probe_station_gui.dialogs.microscope_scan_dialog import (
            MicroscopeScanDialog,
        )

        default_dir = self._default_microscope_scan_output_dir()
        if self.microscope_scan_dialog is None:
            dialog = MicroscopeScanDialog(
                default_output_dir=default_dir,
                parent=None,
            )
            dialog.scan_requested.connect(self._start_microscope_scan)
            dialog.stop_requested.connect(self._request_stop_microscope_scan)
            dialog.finished.connect(lambda _result: self._clear_microscope_scan_dialog())
            self.microscope_scan_dialog = dialog
        self.microscope_scan_dialog.show()
        self.microscope_scan_dialog.raise_()
        self.microscope_scan_dialog.activateWindow()

    def _default_microscope_scan_output_dir(self) -> str:
        document = self._design_session.document
        if document is not None:
            return str(document.path.with_name(f"{document.path.stem}-microscope-scan"))
        return str(Path.cwd() / "microscope-scan")

    def _clear_microscope_scan_dialog(self) -> None:
        self.microscope_scan_dialog = None

    def _request_stop_microscope_scan(self) -> None:
        if not self._microscope_scan_running():
            self._show_status("No microscope scan is running.", 3000)
            return
        self._microscope_scan_stop_requested.set()
        message = "Microscope scan stop requested."
        self._show_status(message, 5000)
        if self.microscope_scan_dialog is not None:
            self.microscope_scan_dialog.set_status(message)

    def _start_microscope_scan(
        self,
        configuration: MicroscopeScanConfiguration,
    ) -> None:
        if self._microscope_scan_running():
            self._show_status("Microscope scan is already running.", 4000)
            return
        if self.serial_connection is None or not self.serial_connection.is_open:
            self._show_status("Connect the stage controller before scanning.", 5000)
            return
        document = self._design_session.document
        if document is None:
            self._show_status("Load a design before scanning.", 5000)
            return
        registration = self._design_session.registration
        if registration is None or not registration.valid:
            self._show_status(
                "Design registration is required before scanning.",
                6000,
            )
            return
        scale = self._active_microscope_scale()
        if scale is None:
            self._show_status(
                "Calibrate click-to-move for the active objective before scanning.",
                8000,
            )
            return
        frame, _counter = self._wait_for_camera_frame(timeout_s=0.1)
        if frame is None:
            self._show_status("Camera frame is unavailable; cannot scan.", 8000)
            return
        fov_size_mm = (
            frame.width() * scale.pixel_size_x_mm,
            frame.height() * scale.pixel_size_y_mm,
        )
        try:
            stage_bounds = stage_bounds_from_design_bounds(
                document.bounds,
                self._raw_stage_xy_from_design_xy,
            )
            plan = build_design_scan_plan(
                stage_bounds=stage_bounds,
                fov_size_mm=fov_size_mm,
                overlap_fraction=configuration.overlap_fraction,
            )
        except (ValueError, DesignModelError) as exc:
            self._show_status(str(exc), 8000)
            return
        if not plan.tiles:
            self._show_status("Microscope scan plan has no tiles.", 5000)
            return
        self._microscope_scan_stop_requested.clear()
        if self.microscope_scan_dialog is not None:
            self.microscope_scan_dialog.set_running(True)
            self.microscope_scan_dialog.set_status(
                f"Microscope scan starting: {len(plan.tiles)} tiles."
            )
        self._microscope_scan_thread = threading.Thread(
            target=self._run_microscope_scan,
            args=(configuration, plan),
            name="MicroscopeDesignScan",
            daemon=True,
        )
        self._microscope_scan_thread.start()
        self._update_stage_coordinate_apply_state()

    def _run_microscope_scan(
        self,
        configuration: MicroscopeScanConfiguration,
        plan: MicroscopeScanPlan,
    ) -> None:
        success = False
        message = "Microscope scan stopped."
        output_dir = Path(configuration.output_dir).expanduser().resolve()
        scale = self._active_microscope_scale()
        if scale is None:
            self.microscope_scan_finished.emit(
                False,
                "Active objective has no calibrated microscope scale.",
            )
            return
        captured_tiles: list[tuple[MicroscopeScanTile, QImage]] = []
        tile_results: list[MicroscopeCaptureResult] = []
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            self.stage_controller.begin_external_task("microscope design scan")
            self.microscope_scan_status.emit("Microscope scan: raising needles.")
            self.stage_controller.run_external_needles_action(
                "raise",
                self._current_needle_feedrate(),
            )
            for tile in plan.tiles:
                if self._microscope_scan_stop_requested.is_set():
                    message = "Microscope scan stopped by user."
                    break
                total = len(plan.tiles)
                self.microscope_scan_status.emit(
                    f"Microscope scan: tile {tile.index}/{total}."
                )
                self.stage_controller.run_external_move_to_xy(
                    tile.stage_xy[0],
                    tile.stage_xy[1],
                )
                if not self._sleep_microscope_scan_settle(configuration.settle_s):
                    message = "Microscope scan stopped by user."
                    break
                result = self._capture_microscope_scan_tile(
                    tile,
                    plan,
                    output_dir=output_dir,
                    scale=scale,
                )
                tile_results.append(result)
                captured_tiles.append((tile, result.raw_image))
            else:
                mosaic = stitch_scan_tiles(
                    plan=plan,
                    tile_images=captured_tiles,
                    scale=scale,
                )
                mosaic_result = self._save_microscope_scan_mosaic(
                    mosaic,
                    plan,
                    output_dir=output_dir,
                    scale=scale,
                )
                manifest_path = self._write_microscope_scan_manifest(
                    output_dir=output_dir,
                    plan=plan,
                    tile_results=tile_results,
                    mosaic_result=mosaic_result,
                )
                success = True
                message = (
                    f"Microscope scan complete: {len(tile_results)} tiles, "
                    f"mosaic {mosaic_result.image_path}, manifest {manifest_path}."
                )
        except Exception as exc:
            logger.exception("Microscope scan failed")
            message = f"Microscope scan failed: {exc}"
        finally:
            self.stage_controller.finish_external_task()
            self.microscope_scan_finished.emit(success, message)

    def _sleep_microscope_scan_settle(self, settle_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(settle_s))
        while True:
            if self._microscope_scan_stop_requested.is_set():
                return False
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return True
            time.sleep(min(remaining, 0.05))

    def _capture_microscope_scan_tile(
        self,
        tile: MicroscopeScanTile,
        plan: MicroscopeScanPlan,
        *,
        output_dir: Path,
        scale,
    ) -> MicroscopeCaptureResult:
        before_counter = self._latest_camera_counter()
        frame, _counter = self._wait_for_camera_frame(
            after_counter=before_counter,
            timeout_s=2.0,
        )
        if frame is None:
            raise RuntimeError("Camera frame is unavailable.")
        captured_at = utc_timestamp()
        objective_name, magnification = self._active_objective_metadata()
        document = self._design_session.document
        scan_name = document.path.stem if document is not None else "design_scan"
        design_xy = self._design_xy_from_raw_stage_xy(tile.stage_xy)
        metadata = MicroscopeImageMetadata(
            title="Probe Station Design Scan",
            mode="design scan tile",
            captured_at=captured_at,
            objective_name=objective_name,
            magnification=magnification,
            scan_tile_index=tile.index,
            scan_tile_total=len(plan.tiles),
            scan_row=tile.row,
            scan_column=tile.column,
            design_xy=design_xy,
            stage_position=self._stage_position_for_image_metadata(
                stage_xy=tile.stage_xy
            ),
            stage_xy=tile.stage_xy,
            notes=("needles raised before scan",),
            extra={
                "overlap_fraction": plan.overlap_fraction,
                "row_count": plan.row_count,
                "column_count": plan.column_count,
            },
        )
        return save_microscope_image(
            frame=frame,
            output_dir=output_dir / "tiles",
            filename_stem=scan_tile_filename(
                scan_name=scan_name,
                tile=tile,
                captured_at=captured_at,
            ),
            metadata=metadata,
            scale=scale,
        )

    def _save_microscope_scan_mosaic(
        self,
        mosaic: QImage,
        plan: MicroscopeScanPlan,
        *,
        output_dir: Path,
        scale,
    ) -> MicroscopeCaptureResult:
        captured_at = utc_timestamp()
        objective_name, magnification = self._active_objective_metadata()
        document = self._design_session.document
        scan_name = document.path.stem if document is not None else "design_scan"
        metadata = MicroscopeImageMetadata(
            title="Probe Station Design Scan Mosaic",
            mode="design scan mosaic",
            captured_at=captured_at,
            objective_name=objective_name,
            magnification=magnification,
            scan_tile_total=len(plan.tiles),
            notes=("stage-coordinate tile mosaic",),
            extra={
                "overlap_fraction": plan.overlap_fraction,
                "row_count": plan.row_count,
                "column_count": plan.column_count,
                "stage_bounds": list(plan.stage_bounds),
                "covered_stage_bounds": list(plan.covered_stage_bounds),
                "fov_size_mm": list(plan.fov_size_mm),
            },
        )
        return save_microscope_image(
            frame=mosaic,
            output_dir=output_dir,
            filename_stem=f"{scan_name}_mosaic_{captured_at}",
            metadata=metadata,
            scale=scale,
        )

    def _write_microscope_scan_manifest(
        self,
        *,
        output_dir: Path,
        plan: MicroscopeScanPlan,
        tile_results: list[MicroscopeCaptureResult],
        mosaic_result: MicroscopeCaptureResult,
    ) -> Path:
        manifest_path = output_dir / "microscope-scan-manifest.json"
        data = {
            "version": 1,
            "created_at": utc_timestamp(),
            "tile_count": len(tile_results),
            "row_count": plan.row_count,
            "column_count": plan.column_count,
            "overlap_fraction": plan.overlap_fraction,
            "stage_bounds": list(plan.stage_bounds),
            "covered_stage_bounds": list(plan.covered_stage_bounds),
            "fov_size_mm": list(plan.fov_size_mm),
            "mosaic": {
                "image": str(mosaic_result.image_path),
                "metadata": str(mosaic_result.metadata_path),
            },
            "tiles": [
                {
                    "index": tile.index,
                    "row": tile.row,
                    "column": tile.column,
                    "stage_xy": list(tile.stage_xy),
                    "image": str(result.image_path),
                    "metadata": str(result.metadata_path),
                }
                for tile, result in zip(plan.tiles, tile_results)
            ],
        }
        with manifest_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
        return manifest_path

    def _on_microscope_scan_status(self, message: str) -> None:
        self._show_status(message)
        if self.microscope_scan_dialog is not None:
            self.microscope_scan_dialog.set_status(message)

    def _on_microscope_scan_finished(self, success: bool, message: str) -> None:
        thread = self._microscope_scan_thread
        if thread is not None and not thread.is_alive():
            thread.join(timeout=0.1)
        self._microscope_scan_thread = None
        self._microscope_scan_stop_requested.clear()
        self._update_stage_coordinate_apply_state()
        if self.microscope_scan_dialog is not None:
            self.microscope_scan_dialog.set_running(False)
            self.microscope_scan_dialog.set_status(message)
        self._show_status(message, 10000 if success else 8000)

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
            self._open_route_measurement_dialog
        )
        self.design_navigator_panel.route_measurement_measure_requested.connect(
            self._request_route_measurement_for_point
        )
        self.design_navigator_panel.route_measurement_stop_requested.connect(
            self._request_stop_route_measurement
        )
        self.design_navigator_panel.route_measurement_pause_requested.connect(
            self._request_pause_route_measurement
        )
        self.design_navigator_panel.route_measurement_interrupt_requested.connect(
            self._request_route_measurement_point_correction
        )
        self.design_navigator_panel.route_measurement_save_shift_requested.connect(
            self._save_route_measurement_shift
        )
        self.design_navigator_panel.route_measurement_confirmation_requested.connect(
            self._submit_route_measurement_confirmation
        )
        self.design_navigator_panel.route_measurement_jump_requested.connect(
            self._submit_route_measurement_jump
        )
        self.design_navigator_panel.route_measurement_move_requested.connect(
            self._request_route_contact_move
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
        if connected:
            self._persist_lcr_connection_state(True, description=description)
        if self.serial_connection_panel is not None:
            self.serial_connection_panel.set_lcr_connection_state(
                connected, backend_name, description
            )
        if self.resistance_panel is not None:
            self.resistance_panel.set_connection_state(
                connected, backend_name, description
            )

    def _on_lcr_reading_updated(self, resistance_ohm: float, is_short: bool) -> None:
        if self.serial_connection_panel is not None:
            self.serial_connection_panel.set_lcr_reading(resistance_ohm, is_short)

    def _on_lcr_reading_started(self, sample_count: int) -> None:
        if self.resistance_panel is not None:
            self.resistance_panel.set_reading_pending(int(sample_count))

    def _on_lcr_reading_summary_updated(
        self, resistance_ohm: float, is_short: bool, sample_count: int
    ) -> None:
        if self.resistance_panel is not None:
            self.resistance_panel.set_reading_summary(
                resistance_ohm, is_short, sample_count
            )

    def _on_resistance_standby_enabled_changed(self, enabled: bool) -> None:
        self.lcr_controller.set_live_polling_enabled(bool(enabled))

    def _resume_resistance_standby_polling(self) -> None:
        lcr_controller = getattr(self, "lcr_controller", None)
        if (
            lcr_controller is not None
            and lcr_controller.live_polling_enabled()
        ):
            lcr_controller.set_live_polling_enabled(True)

    def _request_contact_seek(self) -> None:
        thread = self._contact_seek_thread
        if thread is not None and thread.is_alive():
            self._show_status("Contact seek is already running.")
            return
        if self._route_measurement_thread is not None and self._route_measurement_thread.is_alive():
            self._show_status("Stop route measurement before contact seek.")
            return
        if not self.lcr_controller.is_connected():
            self._show_status("Connect the measurement instrument before contact seek.")
            if self.contact_calibration_window is not None:
                self.contact_calibration_window.set_contact_seek_result(
                    "Measurement instrument is not connected."
                )
            return
        self._contact_seek_stop_requested.clear()
        if self.contact_calibration_window is not None:
            self.contact_calibration_window.set_contact_seek_running(True)
            self.contact_calibration_window.set_contact_seek_result("Starting.")
        thread = threading.Thread(target=self._run_contact_seek, daemon=True)
        self._contact_seek_thread = thread
        thread.start()

    def _cancel_contact_seek(self) -> None:
        self._contact_seek_stop_requested.set()
        self.lcr_controller.abort_current_measurement()
        self.stage_controller.cancel_active_motion("Contact seek cancel requested.")
        self._show_status("Contact seek cancel requested.")

    def _run_contact_seek(self) -> None:
        stage_reserved = False
        moved_mm = 0.0
        try:
            self.stage_controller.begin_external_task("contact seek")
            stage_reserved = True
            feedrate = self._current_needle_feedrate()
            quick_quality = self._contact_seek_measure_quality(
                self.CONTACT_SEEK_QUICK_COUNT
            )
            self.contact_seek_status.emit(
                "Contact seek: current position "
                f"{quick_quality.status}, median="
                f"{_format_route_ohm(quick_quality.median_ohm)}."
            )
            if quick_quality.good is True:
                if self._confirm_and_save_contact_seek("current position", moved_mm):
                    return

            max_steps = int(
                math.ceil(
                    self.CONTACT_SEEK_MAX_TOTAL_MM
                    / abs(self.CONTACT_SEEK_STEP_MM)
                )
            )
            for step_index in range(max_steps):
                if self._contact_seek_stop_requested.is_set():
                    self.contact_seek_finished.emit(False, "Contact seek cancelled.")
                    return
                self.contact_seek_status.emit(
                    "Contact seek: lowering A "
                    f"{step_index + 1}/{max_steps}."
                )
                self.stage_controller.run_external_needles_adjust(
                    self.CONTACT_SEEK_STEP_MM,
                    feedrate,
                )
                moved_mm += abs(self.CONTACT_SEEK_STEP_MM)
                if self._contact_seek_stop_requested.is_set():
                    self.contact_seek_finished.emit(False, "Contact seek cancelled.")
                    return
                quick_quality = self._contact_seek_measure_quality(
                    self.CONTACT_SEEK_QUICK_COUNT
                )
                self.contact_seek_status.emit(
                    "Contact seek: "
                    f"{moved_mm:.4f} mm down, {quick_quality.status}, "
                    f"median={_format_route_ohm(quick_quality.median_ohm)}, "
                    f"MAD={_format_route_ohm(quick_quality.mad_sigma_ohm)}."
                )
                if quick_quality.good is True:
                    if self._confirm_and_save_contact_seek(
                        f"{moved_mm:.4f} mm down",
                        moved_mm,
                    ):
                        return
            self.contact_seek_finished.emit(
                False,
                "Contact seek did not find a stable contact within "
                f"{self.CONTACT_SEEK_MAX_TOTAL_MM:.3f} mm.",
            )
        except Exception as exc:
            logger.exception("Contact seek failed.")
            self.contact_seek_finished.emit(False, f"Contact seek failed: {exc}")
        finally:
            if stage_reserved:
                self.stage_controller.finish_external_task()

    def _contact_seek_measure_quality(self, count: int):
        raw_batch = self.lcr_controller.read_route_measurement_batch_now(int(count))
        samples = tuple(
            route_measurement_sample_from_raw(raw, index)
            for index, raw in enumerate(raw_batch, start=1)
        )
        return summarize_route_contact_quality(samples)

    def _confirm_and_save_contact_seek(self, label: str, moved_mm: float) -> bool:
        self.contact_seek_status.emit(
            "Contact seek: confirming stable contact with "
            f"{self.CONTACT_SEEK_CONFIRM_COUNT} readings."
        )
        confirm_quality = self._contact_seek_measure_quality(
            self.CONTACT_SEEK_CONFIRM_COUNT
        )
        if confirm_quality.good is not True:
            self.contact_seek_status.emit(
                "Contact seek: quick check was good, confirmation failed "
                f"({confirm_quality.status})."
            )
            return False
        lowering_mm = self.stage_controller.latest_axis_a_lowering()
        if lowering_mm is None:
            raise StageControllerError("Unable to read A lowering after contact seek.")
        self.stage_controller.finish_external_task()
        try:
            self.stage_controller.set_current_axis_work_coordinate("A", 0.0)
        except StageControllerError:
            raise
        detail = (
            f"{label}; moved {moved_mm:.4f} mm; "
            f"median={_format_route_ohm(confirm_quality.median_ohm)}, "
            f"MAD={_format_route_ohm(confirm_quality.mad_sigma_ohm)}, "
            f"p95 step={_format_route_ohm(confirm_quality.p95_abs_step_ohm)}."
        )
        self.contact_seek_calibration_found.emit(float(lowering_mm), detail)
        self.contact_seek_finished.emit(True, f"Contact seek found stable contact: {detail}")
        return True

    def _on_contact_seek_status(self, message: str) -> None:
        if self.contact_calibration_window is not None:
            self.contact_calibration_window.set_contact_seek_result(message)
        self._show_status(message, 5000)

    def _on_contact_seek_calibration_found(
        self,
        lowering_mm: float,
        detail: str,
    ) -> None:
        self._save_needle_down_position_from_lowering(float(lowering_mm))
        if self.contact_calibration_window is not None:
            self.contact_calibration_window.set_contact_seek_result(detail)

    def _on_contact_seek_finished(self, success: bool, message: str) -> None:
        thread = self._contact_seek_thread
        if thread is not None and not thread.is_alive():
            thread.join(timeout=0.1)
        self._contact_seek_thread = None
        self._resume_resistance_standby_polling()
        if self.contact_calibration_window is not None:
            self.contact_calibration_window.set_contact_seek_running(False)
            self.contact_calibration_window.set_contact_seek_result(message)
        self._show_status(message, 8000 if not success else 5000)
        if not success:
            self._send_telegram_alert(
                "contact_seek_failed",
                f"Contact seek needs attention:\n{message}",
                attach_photo=True,
            )

    def _display_a_for_needle_lowering(self, lowering_mm: float | None) -> float | None:
        if lowering_mm is None:
            return None
        target_raw_a = self.stage_controller.axis_a_configured_coordinate_for_lowering(
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
        latest_state = self.stage_controller.latest_stage_state()
        if latest_state not in (None, "Idle"):
            self._show_status("Wait for the stage to stop before saving needle contact.")
            return
        if a_position is None:
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
        latest_state = self.stage_controller.latest_stage_state()
        if latest_state not in (None, "Idle"):
            self._show_status("Wait for the stage to stop before saving needle contact.")
            return
        lowering_mm = self.stage_controller.axis_a_lowering_for_configured_coordinate(
            a_position
        )
        try:
            self.stage_controller.set_current_axis_work_coordinate("A", 0.0)
        except StageControllerError as exc:
            logger.warning("Unable to zero A work coordinate for needle contact: %s", exc)
            self._show_status(f"Unable to set A0 at needle contact: {exc}")
            return
        self._save_needle_down_position_from_lowering(lowering_mm)

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

    def _save_needle_down_position_from_lowering(
        self,
        lowering_mm: float,
    ) -> None:
        settings = self.settings_manager.settings.clone()
        settings.needle_calibration.down_position_mm = max(0.0, float(lowering_mm))
        settings.needle_calibration.down_position_configured = True
        self.settings_manager.replace(settings)
        self.settings_manager.save()
        self._apply_needle_calibration_runtime(settings.needle_calibration)
        self._show_status(
            "Saved needle down target and set current A position to A0."
        )

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
        lowering_mm = self.stage_controller.axis_a_lowering_for_configured_coordinate(
            raw_a
        )
        settings = self.settings_manager.settings.clone()
        if action_key == "raise":
            settings.needle_calibration.raise_position_mm = lowering_mm
            settings.needle_calibration.raise_position_configured = True
        else:
            settings.needle_calibration.down_position_mm = lowering_mm
            settings.needle_calibration.down_position_configured = True
        self.settings_manager.replace(settings)
        self.settings_manager.save()
        self._apply_needle_calibration_runtime(settings.needle_calibration)
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
        self._apply_needle_calibration_runtime(settings.needle_calibration)
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

    def _sample_handling_active(self) -> bool:
        thread = getattr(self, "_sample_handling_thread", None)
        return thread is not None and thread.is_alive()

    def _latest_stage_z(self) -> float | None:
        latest = self.stage_controller.latest_stage_position()
        if latest is None or len(latest) < 3:
            return None
        try:
            z_mm = float(latest[2])
        except (TypeError, ValueError):
            return None
        if not math.isfinite(z_mm):
            return None
        return z_mm

    def _active_sample_objective_name(self) -> str:
        try:
            objectives = self.settings_manager.objectives_configuration()
            raw_name = getattr(objectives, "active_name", "")
        except Exception:
            logger.debug("Unable to read active objective for sample focus.", exc_info=True)
            raw_name = ""
        name = normalize_objective_name(raw_name)
        if name:
            return name
        fallback = str(raw_name or "").strip().upper()
        return fallback or "UNKNOWN"

    def _remember_sample_focus_from_latest(self) -> float | None:
        z_mm = self._latest_stage_z()
        if z_mm is not None:
            focus_by_objective = getattr(
                self,
                "_last_sample_focus_z_by_objective",
                None,
            )
            if focus_by_objective is None:
                focus_by_objective = {}
                self._last_sample_focus_z_by_objective = focus_by_objective
            focus_by_objective[self._active_sample_objective_name()] = z_mm
        return z_mm

    def _sample_load_focus_z(self) -> float | None:
        objective_name = self._active_sample_objective_name()
        focus_by_objective = getattr(
            self,
            "_last_sample_focus_z_by_objective",
            {},
        )
        if objective_name in focus_by_objective:
            return focus_by_objective[objective_name]
        return self._latest_stage_z()

    def _sample_workflow_can_start(self, action: str) -> bool:
        if not self._stage_serial_ready():
            self._show_status("Stage is not connected; sample action not started.", 4000)
            return False
        if self._sample_handling_active() or self._has_cancelable_operation():
            self._show_status(f"Stage is busy. Ignoring sample {action} request.", 4000)
            return False
        return True

    def _design_registration_is_active(self) -> bool:
        registration = getattr(
            getattr(self, "_design_session", None),
            "registration",
            None,
        )
        return bool(registration is not None and getattr(registration, "valid", False))

    def _request_sample_unload(self) -> None:
        if not self._sample_workflow_can_start("unload"):
            return
        if self._design_registration_is_active():
            response = QMessageBox.question(
                self,
                "Unload Sample",
                "Unload sample and clear design registration?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if response != QMessageBox.Yes:
                return
            self._invalidate_design_registration(
                "Design registration cleared before sample unload."
            )
        self._remember_sample_focus_from_latest()
        xy_feedrate = self.stage_controller.max_feedrate_for_axes(("X", "Y"))
        needle_feedrate = self._current_needle_feedrate()
        thread = threading.Thread(
            target=self._run_sample_unload,
            args=(xy_feedrate, needle_feedrate),
            daemon=True,
            name="SampleUnload",
        )
        self._sample_handling_thread = thread
        thread.start()

    def _request_sample_load(self) -> None:
        if not self._sample_workflow_can_start("load"):
            return
        focus_z_mm = self._sample_load_focus_z()
        xy_feedrate = self.stage_controller.max_feedrate_for_axes(("X", "Y"))
        focus_feedrate = self.stage_controller.max_feedrate_for_axes(("Z",))
        needle_feedrate = self._current_needle_feedrate()
        thread = threading.Thread(
            target=self._run_sample_load,
            args=(focus_z_mm, xy_feedrate, focus_feedrate, needle_feedrate),
            daemon=True,
            name="SampleLoad",
        )
        self._sample_handling_thread = thread
        thread.start()

    def _run_sample_unload(
        self,
        xy_feedrate: float,
        needle_feedrate: float,
    ) -> None:
        success = False
        message = ""
        try:
            self.stage_controller.begin_external_task("sample unload")
            self.sample_handling_status.emit("Sample unload: raising needles.")
            self.stage_controller.run_external_needles_action(
                "raise",
                needle_feedrate,
            )
            self.sample_handling_status.emit(
                "Sample unload: moving to "
                f"X={self.SAMPLE_UNLOAD_X_MM:.3f}, "
                f"Y={self.SAMPLE_UNLOAD_Y_MM:.3f}."
            )
            self.stage_controller.run_external_move_to_xy(
                self.SAMPLE_UNLOAD_X_MM,
                self.SAMPLE_UNLOAD_Y_MM,
                feedrate=xy_feedrate,
            )
            message = (
                "Sample unloaded at "
                f"X={self.SAMPLE_UNLOAD_X_MM:.3f}, "
                f"Y={self.SAMPLE_UNLOAD_Y_MM:.3f}; needles are raised."
            )
            success = True
        except StageControllerError as exc:
            message = f"Sample unload failed: {exc}"
        except Exception as exc:
            logger.exception("Sample unload failed.")
            message = f"Sample unload failed: {exc}"
        finally:
            self.stage_controller.finish_external_task()
            self.sample_handling_finished.emit(success, message, False, None)

    def _run_sample_load(
        self,
        focus_z_mm: float | None,
        xy_feedrate: float,
        focus_feedrate: float,
        needle_feedrate: float,
    ) -> None:
        success = False
        message = ""
        try:
            self.stage_controller.begin_external_task("sample load")
            self.sample_handling_status.emit("Sample load: raising needles.")
            self.stage_controller.run_external_needles_action(
                "raise",
                needle_feedrate,
            )
            self.sample_handling_status.emit(
                "Sample load: moving to "
                f"X={self.SAMPLE_LOAD_X_MM:.3f}, "
                f"Y={self.SAMPLE_LOAD_Y_MM:.3f}."
            )
            self.stage_controller.run_external_move_to_xy(
                self.SAMPLE_LOAD_X_MM,
                self.SAMPLE_LOAD_Y_MM,
                feedrate=xy_feedrate,
            )
            if focus_z_mm is not None:
                self.sample_handling_status.emit(
                    f"Sample load: moving Z to last focus {focus_z_mm:.4f} mm."
                )
                self.stage_controller.run_external_absolute_axis_targets_move(
                    {"Z": focus_z_mm},
                    feedrate=focus_feedrate,
                )
                message = (
                    "Sample loaded at "
                    f"X={self.SAMPLE_LOAD_X_MM:.3f}, "
                    f"Y={self.SAMPLE_LOAD_Y_MM:.3f}, "
                    f"Z={focus_z_mm:.4f}."
                )
            else:
                message = (
                    "Sample loaded at "
                    f"X={self.SAMPLE_LOAD_X_MM:.3f}, "
                    f"Y={self.SAMPLE_LOAD_Y_MM:.3f}; last focus is unavailable."
                )
            success = True
        except StageControllerError as exc:
            message = f"Sample load failed: {exc}"
        except Exception as exc:
            logger.exception("Sample load failed.")
            message = f"Sample load failed: {exc}"
        finally:
            self.stage_controller.finish_external_task()
            self.sample_handling_finished.emit(
                success,
                message,
                success,
                focus_z_mm,
            )

    def _on_sample_handling_finished(
        self,
        success: bool,
        message: str,
        offer_autofocus: bool,
        focus_z_mm: object,
    ) -> None:
        self._sample_handling_thread = None
        if message:
            self._show_status(message, 7000)
        self._clear_stage_motion_axes()
        self._update_stage_coordinate_apply_state()
        self._schedule_cancel_state_refresh()
        if not success or not offer_autofocus:
            return
        prompt = "Run autofocus now?"
        try:
            focus_z = float(focus_z_mm)
        except (TypeError, ValueError):
            focus_z = math.nan
        if math.isfinite(focus_z):
            prompt = f"Sample is near Z={focus_z:.4f} mm. Run autofocus now?"
        response = QMessageBox.question(
            self,
            "Autofocus",
            prompt,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if response != QMessageBox.Yes:
            return
        if self.stage_controller.is_busy():
            self._show_status("Stage is busy; autofocus not started.", 4000)
            return
        self.stage_controller.request_autofocus()

    def _on_oscillation_state_changed(self, running: bool, axis: str) -> None:
        if self.oscillation_panel:
            self.oscillation_panel.set_running(running, axis)
        self._update_stage_coordinate_apply_state()

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


def _csv_float(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(number):
        return ""
    return f"{number:.12g}"


def _csv_bool(value: object) -> str:
    return "true" if bool(value) else "false"


def _format_route_ohm(value: float) -> str:
    if not math.isfinite(value):
        return "nan Ohm"
    abs_value = abs(value)
    for scale, unit in (
        (1e9, "GOhm"),
        (1e6, "MOhm"),
        (1e3, "kOhm"),
        (1.0, "Ohm"),
        (1e-3, "mOhm"),
        (1e-6, "uOhm"),
    ):
        if abs_value >= scale:
            return f"{value / scale:.3g} {unit}"
    return f"{value:.3g} Ohm"


def _format_route_percent(value: float) -> str:
    if not math.isfinite(value):
        return "nan%"
    return f"{value * 100.0:.3g}%"


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
    _startup_trace("main() entered")
    diagnostics_path = configure_crash_diagnostics()
    _startup_trace("crash diagnostics configured")
    logger.debug("Crash diagnostics enabled: %s", diagnostics_path)
    _configure_windows_app_id()
    _startup_trace("platform application identity configured")
    app = QApplication(sys.argv)
    _startup_trace("QApplication created")
    app.setApplicationName("Probe Station GUI")
    app.setWindowIcon(_application_icon())
    _startup_trace("application icon set")
    window = Main()
    _startup_trace("Main created")
    _set_initial_window_geometry(window)
    _startup_trace("initial window geometry set")
    window.show()
    _startup_trace("window.show() called")
    QTimer.singleShot(0, lambda: _fit_window_to_screen(window))
    _startup_trace("screen fit scheduled")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
