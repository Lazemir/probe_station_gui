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
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any, Mapping, TYPE_CHECKING

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
    QThread,
    QTimer,
    Qt,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QDesktopServices,
    QIcon,
    QImage,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
    QWidget,
)

_startup_trace("PySide imports done")

from probe_station_gui import (
    Grabber,
    JoystickWindow,
    MicroscopeView,
    StageController,
)
from probe_station_gui.design.model import DesignDocument, DesignModelError
from probe_station_gui.design.contact_navigation import (
    api_contact_context,
    api_contact_needles_plan,
    api_contact_needles_stage_error_response,
    api_contact_needles_success_response,
    api_move_to_contact_plan,
    api_move_to_contact_stage_error_response,
    api_move_to_contact_success_response,
    api_route_adjusted_stage_xy,
    api_route_point_payload,
)
from probe_station_gui.design import navigation_adapter as design_navigation
from probe_station_gui.design.markup import (
    MarkupDocument,
    MarkupLoadChoice,
    resolve_loaded_markup,
)
from probe_station_gui.design.markup_store import (
    MarkupStoreWorker,
)
from probe_station_gui.design.selection_model import (
    SelectionModel,
    apply_markup_entity_changes,
    plan_mixed_array,
    plan_mixed_delete,
    project_entities,
)
from probe_station_gui.design.session import AlignmentPreparation, DesignSession
from probe_station_gui.shared.diagnostics import configure_crash_diagnostics
from probe_station_gui.api.request_bridge import ApiRequestBridge, DeferredApiResponse
from probe_station_gui.api.server import ProbeStationApiServer
from probe_station_gui.api.keys import API_KEY_FILENAME, ApiKeyStore
from probe_station_gui.camera.api_control import (
    CameraApiBroker,
    connect_camera_api_results,
    encode_camera_frame_png,
)
from probe_station_gui.camera.auto_exposure import (
    AutoExposureFrame,
    CameraAutoExposureController,
)
from probe_station_gui.camera.exposure_policy import (
    ExposurePolicyController,
    OpticalSessionManager,
)
from probe_station_gui.camera.exposure_policy_qt import ExposurePolicyQtAdapter
from probe_station_gui.camera.live_correction import (
    LatestFrameProcessor,
    LiveCameraCorrectionPipeline,
    LiveCameraCorrectionRequest,
    LiveCameraCorrectionResult,
)
from probe_station_gui.camera.flat_field_calibration import FlatFieldCalibrationStore
from probe_station_gui.api.command_dispatch import (
    ApiBridgeRequestHandlers,
    ApiCommandDispatchHandlers,
    GUI_STAGE_WORKER_ACTIONS,
    api_command_action_payload as command_dispatch_action_payload,
    dispatch_api_command_request as command_dispatch_request,
    handle_api_request as command_dispatch_handle_api_request,
    submit_api_command_request_from_api_thread as command_dispatch_from_api_thread,
)
from probe_station_gui.instruments.api_sweep import (
    ApiRawVoltageSweepRequestError,
    api_raw_voltage_sweep_contact_plan,
    api_raw_voltage_sweep_error_response,
    api_raw_voltage_sweep_meter_payload,
    api_raw_voltage_sweep_request_from_payload,
    api_raw_voltage_sweep_success_response,
)
from probe_station_gui.instruments.meters.lcr import (
    LCRMeterController,
    LCRMeterError,
    ROUTE_METER_GWINSTEK,  # noqa: F401 - re-exported for legacy callers/tests
    ROUTE_METER_KEITHLEY,  # noqa: F401 - re-exported for legacy callers/tests
    ROUTE_METER_KEITHLEY_2400,  # noqa: F401 - re-exported for legacy callers/tests
    RouteMeterConfiguration,
)
from probe_station_gui.stage.api_moves import (
    api_axis_value_map, api_coordinate_move_busy_response, api_coordinate_move_plan,
    api_coordinate_move_start_failed_response, api_coordinate_move_success_response,
)
from probe_station_gui.stage.coordinate_targets import (
    CoordinateTargetConfig,
    CoordinateTargetMoveState,
    plan_coordinate_target_start,
    resolve_stage_axis_target,
    stage_axis_target_limit_error,
)
from probe_station_gui.stage.exact_step import ExactStepAccumulator
from probe_station_gui.stage.manual_jog_prediction import (
    ManualJogPredictionConfig,
    ManualJogPredictionState,
)
from probe_station_gui.stage import sample_handling
from probe_station_gui.stage.motion_prediction import motion_progress
from probe_station_gui.stage import move_lifecycle as stage_move_lifecycle
from probe_station_gui.stage import position_update as stage_position_update
from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox
from probe_station_gui.stage.controller import StageControllerError
from probe_station_gui.stage.types import StageTaskToken
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel_adapter,
)
from probe_station_gui.views.stage_position_panel import (
    StagePositionPanel,
    format_stage_axis_value,
)
from probe_station_gui.views.microscope_interaction import (
    ClickMoveBindings,
    ClickMoveConfig,
)
from probe_station_gui.design import objective_alignment as alignment, objective_offsets as offsets
from probe_station_gui.route.model import (
    MeasurementRoute,
    structure_number_from_labels,
)
from probe_station_gui.route.measurement import (
    ROUTE_OPERATION_MEASURE,
    RouteContactHeightRecord,
    RouteExternalMeasurementSessionRunner,
    RouteMeasurementPoint,
    RoutePhotoRecord,
    RouteMeasurementRecord,
    RouteMeasurementRunner,
    route_measurement_sample_from_raw,
    summarize_route_contact_quality,
)
from probe_station_gui.route.gui_measurement_adapter import (
    GuiRouteEventBindings,
    setup_gui_route_meter,
)
from probe_station_gui.route.point_execution import PointPhotoSettings
from probe_station_gui.route.point_execution_adapters import RouteMeasurementEvents
from probe_station_gui.route.control_state import (
    ApiRouteControlState,
    api_route_control_legacy_attrs,
    api_route_control_state_from_legacy_attrs,
)
from probe_station_gui.route.control_operation import (
    ApiRouteControlInterruptAdapter,
    ApiRouteControlOperationAdapters,
    ApiRouteControlRunnerAdapter,
    ApiRouteControlStateAdapter,
    ApiRouteControlWindowAdapter,
    execute_api_route_control_action,
)
from probe_station_gui.route.operation import (
    RouteMeasurementStartPlan,
    route_measurement_points_for_route,
    route_measurement_start_decision,
)
from probe_station_gui.route.adjustment_flow import (
    RouteShiftSavePlan,
    RouteShiftSaveStatusPlan,
    route_contact_move_plan,
    route_shift_runner_offset_update,
    route_shift_save_guard_plan,
    route_shift_save_plan,
    route_shift_save_status_plan,
    route_shift_stage_position_error_plan,
    route_shift_stage_xy_plan,
)
from probe_station_gui.route.confirmation_flow import (
    route_confirmation_runtime_plan,
    route_confirmation_submission_plan,
)
from probe_station_gui.route.dialog_adapter import (
    open_or_update_route_measurement_dialog,
    route_dialog_handlers,
    restart_waiting_route_measurement,
    route_dialog_restore_plan,
    route_measurement_setup_changed,
    route_measurement_session_cancel_plan,
    route_measurement_session_start_plan,
)
from probe_station_gui.route.runtime_presenter import RouteRuntimePresentationSink
from probe_station_gui.route.api_artifacts import (
    ApiRouteArtifactsStore,
    api_route_photo_artifact_metadata,
    api_route_photo_content_type,
    api_route_session_action_response,
    api_route_session_result_response,
    api_route_session_seek_response,
    api_route_session_status_response,
    final_api_route_session_status,
)
from probe_station_gui.route.api_window_guard import probe_route_api_requires_window
from probe_station_gui.route.api_measurement import (
    api_contact_number_from_payload,
    api_current_contact_error_response,
    api_current_contact_failure_alert,
    api_current_contact_response,
    api_current_contact_settings_from_payload,
)
from probe_station_gui.route.payload_parsing import payload_float, payload_optional_float
from probe_station_gui.route.meter_config import (
    route_meter_configuration_from_payload,
)
from probe_station_gui.route.measurement_settings import RouteMeasurementSettingsStore
from probe_station_gui.route.session_start import (
    GuiRouteLaunchState,
    GuiRouteStartPreflight,
    RouteExternalSessionStartSettings,
    api_route_existing_session_response,
    api_route_session_launch_state,
    api_route_session_start_decision,
    gui_route_camera_frame_preflight,
    gui_route_launch_state,
    gui_route_start_availability,
    gui_route_start_preflight,
)
from probe_station_gui.route.finish_flow import (
    route_finish_outcome_plan,
    route_finish_signal_plan,
)
from probe_station_gui.route.telegram_adapter import (
    RouteTelegramPhotoState,
    combine_telegram_contact_photos,
    capture_route_photo,
    route_finish_telegram_payload,
    route_photo_focus_payload,
    route_requested_photo_caption,
    route_start_telegram_text,
    route_telegram_state_from_legacy_owner,
    telegram_contact_photo_payload,
)
from probe_station_gui.route.shift import route_shift_from_stage_xy
from probe_station_gui.route.formatting import (
    format_route_ohm as _format_route_ohm,
    format_route_percent as _format_route_percent,
)
from probe_station_gui.route.artifact_rows import (
    ROUTE_CONTACT_HEIGHT_MAP_FIELDS,
    route_contact_height_map_path,
    route_contact_height_map_row,
)
from probe_station_gui.camera.imaging import (
    apply_flat_field_correction,  # noqa: F401 - retained test/patch seam
    objective_scale_calibration,
    save_microscope_image,
    utc_timestamp,
)
from probe_station_gui.camera.distortion import (
    DistortionCorrection,
    apply_distortion_correction,
    correction_from_payload,
)
from probe_station_gui.camera.optical_calibration_geometry import (
    LensFitLimits,
)
from probe_station_gui.camera.optical_calibration_runtime import (
    FlatFieldCalibrationRequest,
    LensDistortionCalibrationRequest,
    OpticalCalibrationOutcome,
    OpticalCalibrationProgress,
    OpticalCalibrationRuntime,
)
from probe_station_gui.camera.optical_calibration_adapters import (
    OpticalCalibrationCameraAdapter,
    OpticalCalibrationEventAdapter,
    OpticalCalibrationRequestAdapter,
    OpticalCalibrationSessionAdapter,
    OpticalCalibrationStageAdapter,
    OpticalCalibrationStoreAdapter,
    optical_calibration_blocks_mutation,
    optical_calibration_preflight_message,
    prepare_lens_completion,
)
from probe_station_gui.camera import microscope_scan
from probe_station_gui.camera.microscope_scan_runtime import (
    MicroscopeScanRunRequest,
    MicroscopeScanRuntime,
)
from probe_station_gui.camera.microscope_scan_runtime_adapters import (
    MicroscopeAreaScanRequest,
    MicroscopeScanArtifactAdapter,
    MicroscopeScanCameraAdapter,
    MicroscopeDesignScanRequest,
    MicroscopeScanEventAdapter,
    MicroscopeScanSessionAdapter,
    MicroscopeScanStageAdapter,
    build_microscope_scan_plan,
)
from probe_station_gui.settings.manager import (
    Settings,
    SettingsManager,
    ordered_objective_names,
)
from probe_station_gui.settings.objective_config import (
    normalize_objective_name,
    parse_pixels_to_mm_matrix,
)
from probe_station_gui.notifications.telegram import (
    TelegramBotCommandService,
    TelegramBotRequest,
    TelegramBotResponse,
    resolved_bot_token,
    send_telegram_alert_for_settings,
    send_telegram_bot_message_for_settings,
    telegram_route_attention_alert_enabled,
    telegram_inline_keyboard,
)
from probe_station_gui.notifications import telegram_commands
from probe_station_gui.views.alignment_panel import AlignmentPanel
from probe_station_gui.views.contact_oscillation_window import (
    ContactOscillationWindow,
)
from probe_station_gui.dialogs.click_calibration_dialog import ClickCalibrationDialog
from probe_station_gui.dialogs.lens_distortion_dialog import LensDistortionDialog
from probe_station_gui.dialogs.optical_calibration_wizard import (
    OpticalCalibrationMode,
    OpticalCalibrationWizard,
)
from probe_station_gui.views.dock_widgets import CollapsibleDockWidget
from probe_station_gui.views.oscillation_panel import OscillationPanel
from probe_station_gui.views.resistance_monitor_panel import ResistanceMonitorPanel
from probe_station_gui.views.serial_connection_panel import SerialConnectionPanel
from probe_station_gui.views.main_window_auxiliary import (
    create_design_layout_window,
    show_serial_terminal_window as show_serial_terminal_tool_window,
    toggle_design_layout_window,
)
from probe_station_gui.views.main_window_docks import create_main_window_docks
from probe_station_gui.views.main_window_menus import setup_main_window_menus
from probe_station_gui.views import (
    main_window_connection_flow as connection_flow,
)
from probe_station_gui.views import (
    main_window_needle_calibration as needle_calibration_ui,
)
from probe_station_gui.views import main_window_shutdown as shutdown_ui

_startup_trace("application imports done")


logger = logging.getLogger(__name__)

APP_ICON_RESOURCE = "assets/app_icon.ico"
WINDOWS_APP_USER_MODEL_ID = "ProbeStationGUI.ProbeStationGUI"


@dataclass(frozen=True)
class _MicroscopeScanLaunchSnapshot:
    objective_name: str
    magnification: float | None
    scan_name: str
    document_identity: str
    scale: object
    registration_matrix: (
        tuple[tuple[float, float], tuple[float, float]] | None
    )
    registration_offset: tuple[float, float] | None
    objective_xy_offset: tuple[float, float]

    def design_to_raw_stage(
        self,
        design_xy: tuple[float, float],
    ) -> tuple[float, float]:
        if self.registration_matrix is None or self.registration_offset is None:
            raise RuntimeError("Design registration is unavailable.")
        x_value = float(design_xy[0])
        y_value = float(design_xy[1])
        camera_x = (
            self.registration_matrix[0][0] * x_value
            + self.registration_matrix[0][1] * y_value
            + self.registration_offset[0]
        )
        camera_y = (
            self.registration_matrix[1][0] * x_value
            + self.registration_matrix[1][1] * y_value
            + self.registration_offset[1]
        )
        return (
            camera_x + self.objective_xy_offset[0],
            camera_y + self.objective_xy_offset[1],
        )

    def raw_stage_to_design(
        self,
        raw_stage_xy: tuple[float, float],
    ) -> tuple[float, float] | None:
        if self.registration_matrix is None or self.registration_offset is None:
            return None
        camera_x = float(raw_stage_xy[0]) - self.objective_xy_offset[0]
        camera_y = float(raw_stage_xy[1]) - self.objective_xy_offset[1]
        shifted_x = camera_x - self.registration_offset[0]
        shifted_y = camera_y - self.registration_offset[1]
        (a, b), (c, d) = self.registration_matrix
        determinant = a * d - b * c
        if abs(determinant) < 1e-18:
            raise RuntimeError("Design registration is singular.")
        return (
            (d * shifted_x - b * shifted_y) / determinant,
            (-c * shifted_x + a * shifted_y) / determinant,
        )

    def metadata(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "objective_name": self.objective_name,
            "magnification": self.magnification,
            "scan_name": self.scan_name,
            "document_identity": self.document_identity,
            "objective_xy_offset_mm": list(self.objective_xy_offset),
        }
        if self.registration_matrix is not None:
            payload["registration_matrix"] = [
                list(row) for row in self.registration_matrix
            ]
        if self.registration_offset is not None:
            payload["registration_offset"] = list(self.registration_offset)
        return payload


@dataclass(frozen=True)
class _PendingDesignMarkupLoad:
    generation: int
    session: DesignSession
    plan: design_navigation.DesignLoadResultPlan
    show_window: bool
    previous_markup: MarkupDocument | None


@dataclass(frozen=True)
class _ManualAlignmentCaptureContext:
    request_id: str
    slot: int
    cancelled: threading.Event


@dataclass
class _ApiStageCommandReservation:
    operation_id: str
    action: str
    completion: DeferredApiResponse
    thread: threading.Thread | None = None
    state: str = "reserved"


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
    )
    from probe_station_gui.route.measurement_config import (
        RouteMeasurementRunConfiguration,
    )
    from probe_station_gui.views.surface_map_panel import SurfaceMapWindow
    from probe_station_gui.views.serial_terminal_window import SerialTerminalWindow
    from probe_station_gui.views.design_navigator_panel import (
        DesignLayoutWindow,
        DesignNavigatorPanel,
    )


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
    flat_field_calibration_progress: Signal = Signal(object, str)
    flat_field_calibration_finished: Signal = Signal(object, bool, str, object)
    lens_distortion_calibration_progress: Signal = Signal(object, str)
    lens_distortion_calibration_finished: Signal = Signal(object, bool, str, object)
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
    EXACT_STEP_ACCUMULATION_MS = 80
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
    EXPOSURE_POLICY_START_RETRY_BACKOFF_S = 1.0
    MICROSCOPE_AREA_SCAN_DEFAULT_ROWS = 3
    MICROSCOPE_AREA_SCAN_DEFAULT_COLUMNS = 3
    MICROSCOPE_AREA_SCAN_MAX_TILES = 121
    MICROSCOPE_AREA_SCAN_DEFAULT_OVERLAP_FRACTION = 0.25
    MICROSCOPE_AREA_SCAN_DEFAULT_SETTLE_S = 0.2
    MICROSCOPE_AREA_SCAN_DEFAULT_TILE_APPROACH_MM = 0.010
    MICROSCOPE_AREA_SCAN_MAX_TILE_APPROACH_MM = 0.200
    MICROSCOPE_AREA_SCAN_STITCH_DEBUG_STRUCTURE_MM = 1.0
    MICROSCOPE_AREA_SCAN_STITCH_DEBUG_PLACEMENT_FRACTION = 1.0
    MICROSCOPE_AREA_SCAN_STITCH_DEBUG_OVERLAP_FRACTION = 0.25
    MICROSCOPE_SCAN_CAMERA_SETTINGS_TIMEOUT_S = 5.0
    CONTACT_SEEK_STEP_MM = (
        needle_calibration_ui.manual_contact_seek.DEFAULT_MANUAL_CONTACT_SEEK_STEP_MM
    )
    CONTACT_SEEK_MAX_TOTAL_MM = (
        needle_calibration_ui.manual_contact_seek.DEFAULT_MANUAL_CONTACT_SEEK_MAX_TOTAL_MM
    )
    CONTACT_SEEK_QUICK_COUNT = (
        needle_calibration_ui.manual_contact_seek.DEFAULT_MANUAL_CONTACT_SEEK_QUICK_COUNT
    )
    CONTACT_SEEK_CONFIRM_COUNT = (
        needle_calibration_ui.manual_contact_seek.DEFAULT_MANUAL_CONTACT_SEEK_CONFIRM_COUNT
    )
    SAMPLE_LOAD_X_MM = sample_handling.SAMPLE_LOAD_X_MM
    SAMPLE_LOAD_Y_MM = sample_handling.SAMPLE_LOAD_Y_MM
    SAMPLE_UNLOAD_X_MM = sample_handling.SAMPLE_UNLOAD_X_MM
    SAMPLE_UNLOAD_Y_MM = sample_handling.SAMPLE_UNLOAD_Y_MM
    LENS_DISTORTION_CAPTURE_GRID_SIZE = 3
    LENS_DISTORTION_FOV_FRACTION = 0.35
    LENS_DISTORTION_CLUSTER_TOLERANCE_PX = 12.0
    LENS_DISTORTION_MIN_FEATURE_COUNT = 4
    LENS_DISTORTION_MIN_OBSERVATION_COUNT = 12
    LENS_DISTORTION_MAX_RESIDUAL_MEAN_PX = 3.0
    LENS_DISTORTION_MAX_RESIDUAL_MAX_PX = 12.0
    LENS_DISTORTION_CAPTURE_SETTLE_S = 0.12
    LENS_DISTORTION_CAMERA_TIMEOUT_S = 2.0
    FLAT_FIELD_CAPTURE_GRID_SIZE = 3
    FLAT_FIELD_CAPTURE_OVERLAP_FRACTION = 0.8
    FLAT_FIELD_CAPTURE_SETTLE_S = 0.12
    FLAT_FIELD_CAMERA_TIMEOUT_S = 2.0

    def __init__(self) -> None:
        _startup_trace("Main.__init__ entered")
        super().__init__()
        self.setWindowTitle("Microscope control")
        app = QApplication.instance()
        if app is not None:
            self.setWindowIcon(app.windowIcon())
        self.menuBar().setNativeMenuBar(False)

        self.settings_manager: SettingsManager = SettingsManager()
        _startup_trace("SettingsManager created; logging configured")
        _flush_startup_trace()
        self.view = MicroscopeView(
            click_move_bindings=ClickMoveBindings(
                request_move=lambda dx, dy: self.stage_controller.request_move(dx, dy),
                stage_connected=self._stage_serial_ready,
                motion_blocked=self._objective_mutation_busy,
                mark_motion_axes=lambda axes: (
                    stage_position_panel_adapter.set_stage_motion_axes(self, axes)
                ),
                show_status=self._show_status,
                repaint=lambda: self.view.update(),
                preview_hover=lambda dx, dy: (
                    self.stage_controller.preview_clicked_point_xy(dx, dy)
                ),
                present_coordinates=self._update_coordinate_display,
                manual_alignment_active=lambda: (
                    self._manual_alignment_pick_slot is not None
                ),
                capture_manual_alignment=self._capture_manual_alignment_clicked,
                pending_state_changed=self._update_stage_coordinate_apply_state,
            ),
            click_move_config=ClickMoveConfig(
                pending_timeout_s=lambda: (
                    self.settings_manager.settings.click_to_move.pending_timeout_s
                ),
            ),
        )
        self._microscope_interaction = self.view.interaction
        central_container = QWidget(self)
        central_layout = QVBoxLayout(central_container)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self.view, 1)
        self.setCentralWidget(central_container)
        self.serial_connection = None
        self.serial_port_name: str | None = None
        self.serial_baud_rate: int | None = None
        self._api_key_store = ApiKeyStore(
            self.settings_manager.config_dir() / API_KEY_FILENAME
        )
        self._api_bridge: ApiRequestBridge | None = None
        self._api_server: ProbeStationApiServer | None = None
        self._api_stage_command_worker_lock = threading.Lock()
        self._api_stage_command_worker_changed = threading.Condition(
            self._api_stage_command_worker_lock
        )
        self._api_stage_command_reservation: _ApiStageCommandReservation | None = None
        self._api_settings_signature: tuple[bool, str, int] | None = None
        self._telegram_bot_service: TelegramBotCommandService | None = None
        self._telegram_bot_signature: tuple[str, str] | None = None
        self._latest_status_message = ""
        self.joystick_panel: JoystickWindow | None = None
        self.serial_terminal_panel: SerialTerminalWindow | None = None
        self.serial_connection_panel: SerialConnectionPanel | None = None
        self.serial_connection_dialog: QDialog | None = None
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
        self._design_markup: MarkupDocument | None = None
        self._design_markup_store: MarkupStoreWorker | None = None
        self._design_markup_request_id = 0
        self._design_markup_load_request_id: int | None = None
        self._design_load_pending = False
        self._design_markup_load_contexts: dict[
            int,
            _PendingDesignMarkupLoad,
        ] = {}
        self._design_markup_pending_visibility: bool | None = None
        self._design_markup_direct_guide_ids: list[str] = []
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
        self._lens_distortion_calibration_action: QAction | None = None
        self._click_calibration_dialog: ClickCalibrationDialog | None = None
        self._optical_calibration_wizard: OpticalCalibrationWizard | None = None
        self._lens_distortion_dialog: LensDistortionDialog | None = None
        self._sample_load_action: QAction | None = None
        self._sample_unload_action: QAction | None = None
        self._objective_offset_reference: offsets.ObjectiveOffsetReference | None = None
        self._ruler_action: QAction | None = None
        self._rect_action: QAction | None = None
        self._last_selected_design_point: tuple[float, float] | None = None
        self._current_design_stage_xy: tuple[float, float] | None = None
        self._pending_design_stage_xy: tuple[float, float] | None = None
        self._pending_alignment_preparation: AlignmentPreparation | None = None
        self._alignment_design_draft: tuple[tuple[float, float], ...] = ()
        self._alignment_stage_draft: list[tuple[float, float] | None] = []
        self._alignment_draft_fit_residuals: tuple[float, float] | None = None
        self._pending_quick_alignment_rotation = False
        self._manual_alignment_pick_slot: int | None = None
        self._manual_alignment_points: list[tuple[float, float] | None] = [None, None]
        self._manual_alignment_capture_context: (
            _ManualAlignmentCaptureContext | None
        ) = None
        self._manual_jog_prediction = ManualJogPredictionState(
            ManualJogPredictionConfig(
                axis_names=self.STAGE_AXIS_NAMES,
                ignore_idle_after_command_s=self.MANUAL_JOG_IGNORE_IDLE_AFTER_COMMAND_S,
                reconcile_smooth_threshold_mm=(
                    self.MANUAL_JOG_RECONCILE_SMOOTH_THRESHOLD_MM
                ),
                reconcile_smooth_alpha=self.MANUAL_JOG_RECONCILE_SMOOTH_ALPHA,
                status_settle_hold_s=self.MANUAL_JOG_STATUS_SETTLE_HOLD_S,
                default_stop_tail_s=self.MANUAL_JOG_DEFAULT_STOP_TAIL_S,
                stop_tail_min_s=self.MANUAL_JOG_STOP_TAIL_MIN_S,
                stop_tail_max_s=self.MANUAL_JOG_STOP_TAIL_MAX_S,
                stop_tail_learn_alpha=self.MANUAL_JOG_STOP_TAIL_LEARN_ALPHA,
            )
        )
        self._planned_move_origin_xy: tuple[float, float] | None = None
        self._planned_move_stage_xy: tuple[float, float] | None = None
        self._planned_move_target_xy: tuple[float, float] | None = None
        self._planned_move_started_at: float | None = None
        self._planned_move_ends_at: float | None = None
        self._planned_move_waiting_for_fresh_status = False
        self._planned_move_stop_status_timestamp: float | None = None
        self._pending_planned_move_target_xy: tuple[float, float] | None = None
        self._pending_planned_move_source_label: str | None = None
        self._coordinate_targets = CoordinateTargetMoveState(
            CoordinateTargetConfig(
                axis_names=self.STAGE_AXIS_NAMES,
                min_feedrate_mm_min=self.MIN_FEEDRATE_MM_MIN,
                duration_padding_s=self.PLANNED_MOVE_DURATION_PADDING_S,
                min_idle_accept_s=self.COORDINATE_MOVE_MIN_IDLE_ACCEPT_S,
                target_tolerance_mm=self.COORDINATE_MOVE_TARGET_TOLERANCE_MM,
            )
        )
        self._pending_stage_axis_targets: dict[str, tuple[float, float]] = {}
        self._exact_step_accumulator = ExactStepAccumulator(self.STAGE_AXIS_NAMES)
        self._exact_step_pending_axes: set[str] = set()
        self._exact_step_window_elapsed = False
        self._stage_position_panel: StagePositionPanel | None = None
        self._design_snap_enabled = True
        self._last_reported_b_position: float | None = None
        self._last_camera_frame_ui_timestamp: float | None = None
        self._suppress_next_camera_ui_gap = False
        self._latest_camera_frame: QImage | None = None
        self._latest_camera_frame_counter = 0
        self._latest_raw_camera_frame: QImage | None = None
        self._latest_raw_camera_frame_counter = 0
        self._exposure_policy_start_in_flight = False
        self._exposure_policy_started = False
        self._exposure_policy_start_retry_after = 0.0
        self._latest_camera_frame_condition = threading.Condition()
        self._latest_camera_frame_for_notifications: QImage | None = None
        self._live_camera_correction_pipeline = LiveCameraCorrectionPipeline(
            self.settings_manager.config_dir()
        )
        self._flat_field_calibration_store = FlatFieldCalibrationStore(
            self.settings_manager.config_dir()
        )
        self._live_camera_frame_processor = LatestFrameProcessor(
            self._live_camera_correction_pipeline.process
        )
        self._live_camera_frame_processor.frame_ready.connect(
            self._on_live_camera_frame_processed,
            Qt.ConnectionType.QueuedConnection,
        )
        self._live_camera_frame_processor.error.connect(
            self._on_live_camera_frame_processing_error,
            Qt.ConnectionType.QueuedConnection,
        )
        self._distortion_correction_cache_signature: tuple[str, str] | None = None
        self._distortion_correction_cache_model: DistortionCorrection | None = None
        self._stage_unhomed_display_origins: dict[str, float] = {}
        self._stage_axis_fields: dict[str, QLineEdit] = {}
        self._stage_axis_raw_values: dict[str, float] = {}
        self._stage_axis_display_values: dict[str, float] = {}
        self._stage_axis_homed: set[str] = set()
        self._stage_limit_axes: set[str] = set()
        self._stage_axis_base_styles: dict[str, tuple[str, str]] = {}
        self._stage_motion_axes: set[str] = set()
        self._stage_motion_blink_dimmed = False
        self._objective_combo: QComboBox | None = None
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
        self._route_measurement_waiting_reason = ""
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
        self._set_api_route_control_state(ApiRouteControlState())
        self._api_route_offset_xy: tuple[float, float] = (0.0, 0.0)
        self._microscope_scan_thread: threading.Thread | None = None
        self._microscope_scan_stop_requested = threading.Event()
        self._sample_handling_thread: threading.Thread | None = None
        self._last_sample_focus_z_by_objective: dict[str, float] = {}
        self._route_telegram = RouteTelegramPhotoState()
        self._contact_seek_thread: threading.Thread | None = None
        self._contact_seek_stop_requested = threading.Event()
        self._design_session = DesignSession()
        self.statusBar()
        self._objective_widget = self._create_objective_widget()
        self.statusBar().addPermanentWidget(self._objective_widget, 0)
        self._stage_position_widget = stage_position_panel_adapter.create_stage_position_widget(
            self
        )
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
        self._camera_api_broker = CameraApiBroker(
            snapshot_submit=self._submit_camera_settings_snapshot,
            batch_submit=self._submit_camera_settings_batch,
            frame_counter=self._latest_raw_camera_counter,
        )
        self._camera_auto_exposure_controller = CameraAutoExposureController(
            settings_read=self._camera_api_broker.read_settings,
            settings_write=self._write_camera_auto_exposure_settings,
            frame_read=self._read_camera_auto_exposure_frame,
        )
        self._compose_camera_exposure_policy()
        connect_camera_api_results(self.grabber, self._camera_api_broker)
        self.thread = QThread()
        self.grabber.moveToThread(self.thread)
        self.thread.started.connect(self.grabber.start)
        self.view.design_minimap_clicked.connect(
            self._open_design_window_from_minimap_point
        )
        self.view.design_minimap_double_clicked.connect(
            lambda: toggle_design_layout_window(self, True)
        )
        self.grabber.frame_ready.connect(self._on_camera_frame)
        self.grabber.frame_gap_suppressed.connect(
            self._on_camera_frame_gap_suppressed
        )
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
        self.flat_field_calibration_progress.connect(
            self._on_flat_field_calibration_progress
        )
        self.flat_field_calibration_finished.connect(
            self._on_flat_field_calibration_finished
        )
        self.lens_distortion_calibration_progress.connect(
            self._on_lens_distortion_calibration_progress
        )
        self.lens_distortion_calibration_finished.connect(
            self._on_lens_distortion_calibration_finished
        )
        self.contact_seek_status.connect(
            lambda message: needle_calibration_ui.on_contact_seek_status(
                self,
                message,
            )
        )
        self.contact_seek_calibration_found.connect(
            lambda lowering_mm, detail: (
                needle_calibration_ui.on_contact_seek_calibration_found(
                    self,
                    lowering_mm,
                    detail,
                )
            )
        )
        self.contact_seek_finished.connect(
            lambda success, message: needle_calibration_ui.on_contact_seek_finished(
                self,
                success,
                message,
            )
        )
        self.sample_handling_status.connect(self._show_status)
        self.sample_handling_finished.connect(
            lambda success, message, offer_autofocus, focus_z_mm: (
                needle_calibration_ui.on_sample_handling_finished(
                    self,
                    success,
                    message,
                    offer_autofocus,
                    focus_z_mm,
                    message_box=QMessageBox,
                )
            )
        )

        self.stage_controller = self._create_stage_controller()
        self._compose_optical_calibration_runtime()
        self.stage_controller.status_message.connect(self._show_status)
        self.stage_controller.movement_finished.connect(
            lambda success, message: stage_move_lifecycle.on_move_finished(
                self,
                success,
                message,
            )
        )
        self.stage_controller.b_rotation_started.connect(
            self._on_alignment_b_rotation_started
        )
        self.stage_controller.click_move_started.connect(
            self._microscope_interaction.start_target_motion
        )
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
        self.stage_controller.clicked_point_resolved.connect(
            self._on_manual_alignment_point_resolved,
            Qt.ConnectionType.QueuedConnection,
        )
        self.stage_controller.autofocus_finished.connect(self.on_autofocus_finished)
        self.stage_controller.stage_position_changed.connect(
            lambda position: stage_position_update.on_stage_position_changed(
                self,
                position,
            )
        )
        self.stage_controller.coordinate_confidence_changed.connect(
            lambda updates: stage_position_panel_adapter.update_coordinate_confidence(
                self,
                updates,
            )
        )
        self.stage_controller.needle_height_changed.connect(self._on_needle_height_changed)
        self.stage_controller.axis_max_feedrates_changed.connect(
            lambda rates: connection_flow.on_axis_max_feedrates_changed(self, rates)
        )
        self.stage_controller.controller_reboot_detected.connect(
            lambda: connection_flow.on_controller_reboot_detected(self)
        )
        self.stage_controller.controller_reboot_ready.connect(
            lambda: connection_flow.on_controller_reboot_ready(self)
        )
        self.stage_controller.oscillation_state_changed.connect(
            self._on_oscillation_state_changed
        )
        self.stage_controller.movement_started.connect(self._on_stage_task_started)
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
        self._stage_motion_blink_timer.timeout.connect(
            lambda: stage_position_panel_adapter.advance_stage_motion_blink(self)
        )
        self._linear_feedrate_save_timer = QTimer(self)
        self._linear_feedrate_save_timer.setSingleShot(True)
        self._linear_feedrate_save_timer.setInterval(400)
        self._linear_feedrate_save_timer.timeout.connect(
            self._save_pending_linear_feedrate_default
        )
        self._exact_step_timer = QTimer(self)
        self._exact_step_timer.setSingleShot(True)
        self._exact_step_timer.setInterval(self.EXACT_STEP_ACCUMULATION_MS)
        self._exact_step_timer.timeout.connect(self._on_exact_step_window_elapsed)

        create_main_window_docks(self)
        _startup_trace("dock widgets created")

        setup_main_window_menus(self)
        _startup_trace("menus created")
        self._apply_settings()
        _startup_trace("settings applied")
        self._api_bridge = ApiRequestBridge(self._handle_api_request, self)
        self._configure_api_server_from_settings(start_if_enabled=False)
        _startup_trace("API server configured")

        QTimer.singleShot(0, self._start_api_server)
        QTimer.singleShot(0, lambda: connection_flow.auto_connect_if_possible(self))
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

    def _compose_camera_exposure_policy(self) -> None:
        self._exposure_policy_controller = ExposurePolicyController(
            initial_policy=self.settings_manager.exposure_policy_configuration(),
            software_once=self._camera_auto_exposure_controller.run,
            settings_read=self._camera_api_broker.read_settings,
            settings_write=self._camera_api_broker.write_settings_trusted,
            frame_read=self._read_camera_auto_exposure_frame,
            persist=self.settings_manager.set_exposure_policy_configuration,
        )
        self._optical_session_manager = OpticalSessionManager(
            self._exposure_policy_controller
        )
        self._exposure_policy_adapter = ExposurePolicyQtAdapter(
            self._exposure_policy_controller,
            settings_write=self._camera_api_broker.write_settings_trusted,
        )
        self._exposure_policy_adapter.command_finished.connect(
            self._on_exposure_policy_command_finished
        )
        self._camera_api_broker.set_manual_exposure_write(
            lambda command: self._exposure_policy_controller.run_manual_exposure_write(
                command
            )
        )

    def _create_stage_controller(self) -> StageController:
        controller = StageController()
        controller.set_optical_session_manager(self._optical_session_manager)
        return controller

    def _compose_optical_calibration_runtime(self) -> None:
        camera = MicroscopeScanCameraAdapter(
            grabber=self.grabber,
            stop_event=self._microscope_scan_stop_requested,
            latest_frame_counter=self._latest_camera_counter,
            wait_for_frame=self._wait_for_camera_frame,
            latest_raw_frame_counter=self._latest_raw_camera_counter,
            wait_for_raw_frame=self._wait_for_raw_camera_frame,
            correct_lens=self._correct_camera_frame_for_active_objective,
            settings_timeout_s=self.MICROSCOPE_SCAN_CAMERA_SETTINGS_TIMEOUT_S,
        )
        self._optical_calibration_request_adapter = OpticalCalibrationRequestAdapter(
            objective_metadata=self._active_objective_metadata,
            objective_scale=self._active_microscope_scale,
        )
        self._optical_calibration_runtime = OpticalCalibrationRuntime(
            stage=OpticalCalibrationStageAdapter(
                reserve_task=self.stage_controller.reserve_external_task,
                read_position=self.stage_controller.run_external_current_stage_position,
                raise_action=self.stage_controller.run_external_needles_action,
                move_xy_callback=self.stage_controller.run_external_move_to_xy,
            ),
            camera=OpticalCalibrationCameraAdapter(
                apply_lock=camera.apply_lock,
                restore_lock=camera.restore_lock,
                raw_counter=self._latest_raw_camera_counter,
                wait_raw_callback=self._wait_for_raw_camera_frame,
            ),
            sessions=OpticalCalibrationSessionAdapter(
                self._optical_session_manager
            ),
            store=OpticalCalibrationStoreAdapter(
                self._flat_field_calibration_store.install
            ),
            events=OpticalCalibrationEventAdapter.from_signal_emitters(
                status=self.status_message_requested.emit,
                flat_progress=self.flat_field_calibration_progress.emit,
                lens_progress=self.lens_distortion_calibration_progress.emit,
                flat_completion=self.flat_field_calibration_finished.emit,
                lens_completion=self.lens_distortion_calibration_finished.emit,
            ),
        )

    def _on_exposure_policy_command_finished(self, result: object) -> None:
        if not isinstance(result, Mapping) or result.get("operation") != "start":
            return
        self._exposure_policy_start_in_flight = False
        if bool(result.get("accepted", False)):
            self._exposure_policy_started = True
            self._exposure_policy_start_retry_after = 0.0
            return
        self._exposure_policy_started = False
        self._exposure_policy_start_retry_after = (
            time.monotonic() + self.EXPOSURE_POLICY_START_RETRY_BACKOFF_S
        )
        message = str(result.get("message") or "Camera exposure setup failed.")
        logger.warning("Camera exposure policy startup failed: %s", message)
        self._show_status(f"Camera exposure setup failed: {message}", 8000)

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
            command_callback=self._submit_api_command_request_from_api_thread,
            auth_callback=self._authorize_api_request,
            camera_settings_read_callback=self._camera_api_broker.read_settings,
            camera_settings_write_callback=self._camera_api_broker.write_settings,
            camera_frame_callback=self._api_camera_frame,
            camera_exposure_policy_snapshot_callback=(
                self._exposure_policy_controller.snapshot
            ),
            camera_exposure_policy_set_callback=(
                self._exposure_policy_controller.set_policy
            ),
            camera_exposure_once_callback=self._exposure_policy_controller.run_once,
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

    def _submit_camera_settings_snapshot(
        self,
        request_id: str,
        names: list[str],
    ) -> None:
        self.grabber.request_camera_settings_snapshot(
            "camera",
            names,
            request_id=request_id,
        )

    def _submit_camera_settings_batch(
        self,
        request_id: str,
        settings: list[tuple[str, object]],
    ) -> None:
        self.grabber.request_camera_settings_batch(
            settings,
            request_id=request_id,
            map_key="camera",
        )

    def _write_camera_auto_exposure_settings(
        self,
        settings: list[tuple[str, object]],
    ) -> dict[str, Any]:
        return self._camera_api_broker.write_settings_trusted(settings)

    def _read_camera_auto_exposure_frame(
        self,
        after_counter: int,
        timeout_s: float,
    ) -> AutoExposureFrame:
        import numpy as np

        frame, counter = self._wait_for_raw_camera_frame(
            after_counter=int(after_counter),
            timeout_s=float(timeout_s),
        )
        if frame is None:
            raise RuntimeError("Fresh raw camera frame is unavailable.")
        image = frame.convertToFormat(QImage.Format_RGB888)
        width = int(image.width())
        height = int(image.height())
        stride = int(image.bytesPerLine())
        rows = np.frombuffer(
            image.bits(),
            dtype=np.uint8,
            count=height * stride,
        ).reshape((height, stride))
        rgb = rows[:, : width * 3].reshape((height, width, 3)).copy(order="C")
        return AutoExposureFrame(rgb=rgb, counter=int(counter))

    def _api_camera_frame(
        self,
        space: str,
        after_counter: int | None,
        timeout_s: float,
    ) -> dict[str, Any]:
        if space == "raw":
            frame, counter = self._wait_for_raw_camera_frame(
                after_counter=after_counter,
                timeout_s=timeout_s,
            )
        elif space == "corrected":
            frame, counter = self._wait_for_camera_frame(
                after_counter=after_counter,
                timeout_s=timeout_s,
            )
        else:
            return {
                "accepted": False,
                "status_code": 400,
                "message": f"Unsupported camera frame space: {space!r}.",
            }
        if frame is None:
            return {
                "accepted": False,
                "status_code": 504 if after_counter is not None else 503,
                "message": (
                    "No newer camera frame arrived before timeout."
                    if after_counter is not None
                    else "Camera frame is not available."
                ),
                "counter": int(counter),
                "space": space,
            }
        return encode_camera_frame_png(frame, counter=counter, space=space)

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
        return self._telegram_response_for_command_route(
            telegram_commands.route_message_command(request.text)
        )

    def _handle_telegram_callback(self, data: str) -> TelegramBotResponse:
        return self._telegram_response_for_command_route(
            telegram_commands.route_callback(data)
        ) or TelegramBotResponse("", reply_markup=self._telegram_default_markup())

    def _telegram_response_for_command_route(
        self,
        route: telegram_commands.TelegramCommandRoute | None,
    ) -> TelegramBotResponse | None:
        if route is None:
            return None
        handlers = {
            "status": self._telegram_status_response,
            "route_photo": self._telegram_request_next_route_photo_response,
            "contact_photo": self._telegram_request_next_contact_photo_response,
        }
        if route.kind == "route_action":
            return self._telegram_route_action_response(route.action)
        handler = handlers.get(route.kind)
        if handler is not None:
            return handler()
        return TelegramBotResponse(
            route.text,
            callback_answer=route.callback_answer,
            reply_markup=self._telegram_default_markup(),
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
        plan = telegram_commands.next_route_photo_response(
            route_active=telegram_commands.thread_alive(self._route_measurement_thread),
            structure_photos_enabled=self._route_measurement_photo_enabled,
        )
        if plan.request_photo:
            self._route_telegram_adapter().request_route_photo()
        return self._telegram_text_response(plan.text, plan.callback_answer)

    def _telegram_request_next_contact_photo_response(self) -> TelegramBotResponse:
        plan = telegram_commands.next_contact_photo_response(
            route_active=telegram_commands.thread_alive(self._route_measurement_thread),
            contact_measurement_enabled=self._route_measurement_measure_enabled,
        )
        if plan.request_photo:
            self._route_telegram_adapter().request_contact_photo()
        return self._telegram_text_response(plan.text, plan.callback_answer)

    def _telegram_route_action_response(self, action: str) -> TelegramBotResponse:
        runner = self._route_measurement_runner
        api_accepts_confirmation = False
        if (
            runner is None
            and self._route_measurement_waiting
            and telegram_commands.is_route_action(action)
        ):
            api_accepts_confirmation = (
                self._api_route_control_state_snapshot().accepts_route_confirmation
            )
        plan = telegram_commands.route_action_response(
            action,
            route_waiting=self._route_measurement_waiting,
            runner_available=runner is not None,
            api_route_control_accepts_confirmation=api_accepts_confirmation,
        )
        if plan.submit_action is not None:
            self._submit_route_measurement_confirmation(plan.submit_action)
        return self._telegram_text_response(plan.text, plan.callback_answer)

    def _telegram_text_response(
        self,
        text: str,
        callback_answer: str,
    ) -> TelegramBotResponse:
        return TelegramBotResponse(
            text,
            reply_markup=self._telegram_default_markup(),
            callback_answer=callback_answer,
        )

    def _telegram_default_markup(self) -> object | None:
        return telegram_inline_keyboard(
            telegram_commands.default_markup_rows(self._route_measurement_waiting)
        )

    @staticmethod
    def _telegram_route_actions_markup() -> object | None:
        return telegram_inline_keyboard(telegram_commands.route_action_markup_rows())

    def _route_telegram_adapter(self) -> RouteTelegramPhotoState:
        adapter = getattr(self, "_route_telegram", None)
        if adapter is not None:
            return adapter
        adapter = route_telegram_state_from_legacy_owner(self)
        self._route_telegram = adapter
        return adapter

    def _telegram_status_text(self) -> str:
        return telegram_commands.status_text(self._telegram_status_snapshot())

    def _telegram_status_snapshot(self) -> telegram_commands.TelegramStatusSnapshot:
        return telegram_commands.TelegramStatusSnapshot(
            latest_status_message=self._latest_status_message,
            route_thread_active=telegram_commands.thread_alive(self._route_measurement_thread),
            route_waiting=self._route_measurement_waiting,
            route_session_active=self._route_measurement_session_active,
            route_current_point=self._route_measurement_current_point,
            api_route_control_status_text=(
                self._api_route_control_state_snapshot().telegram_status_text()
            ),
            stage_status=self._api_stage_status(),
            microscope_scan_active=telegram_commands.thread_alive(self._microscope_scan_thread),
            contact_seek_active=telegram_commands.thread_alive(self._contact_seek_thread),
            camera_frame_available=self._latest_camera_frame_for_notifications is not None,
            stage_axis_names=self.STAGE_AXIS_NAMES,
        )

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

    def _submit_api_command_request(
        self,
        command_request: dict[str, Any],
    ) -> dict[str, Any] | DeferredApiResponse:
        action, payload = self._api_command_action_payload(command_request)
        if action in GUI_STAGE_WORKER_ACTIONS:
            route_guard = self._probe_route_api_window_guard(action, payload)
            if route_guard is not None:
                return route_guard
            return self._start_deferred_api_stage_command(command_request, action)
        return self._dispatch_api_command_request(
            command_request,
            apply_route_control_guard=True,
        )

    def _api_stage_command_worker_state(
        self,
    ) -> tuple[threading.Lock, threading.Condition]:
        lock = getattr(self, "_api_stage_command_worker_lock", None)
        changed = getattr(self, "_api_stage_command_worker_changed", None)
        if lock is None or changed is None:
            lock = threading.Lock()
            changed = threading.Condition(lock)
            self._api_stage_command_worker_lock = lock
            self._api_stage_command_worker_changed = changed
        if not hasattr(self, "_api_stage_command_reservation"):
            self._api_stage_command_reservation = None
        return lock, changed

    def _api_stage_command_worker_active(self) -> bool:
        lock, _changed = self._api_stage_command_worker_state()
        with lock:
            reservation = self._api_stage_command_reservation
            return bool(reservation is not None and reservation.state != "released")

    def _wait_for_api_stage_command_workers(self, *, timeout_s: float) -> bool:
        _lock, changed = self._api_stage_command_worker_state()
        with changed:
            return bool(
                changed.wait_for(
                    lambda: self._api_stage_command_reservation is None,
                    timeout=max(0.0, float(timeout_s)),
                )
            )

    def _release_api_stage_command_reservation(
        self,
        reservation: _ApiStageCommandReservation,
    ) -> bool:
        lock, changed = self._api_stage_command_worker_state()
        with lock:
            if (
                self._api_stage_command_reservation is not reservation
                or reservation.state == "released"
            ):
                return False
            reservation.state = "released"
            self._api_stage_command_reservation = None
            changed.notify_all()
            return True

    def _start_deferred_api_stage_command(
        self,
        command_request: dict[str, Any],
        action: str,
    ) -> dict[str, Any] | DeferredApiResponse:
        completion = DeferredApiResponse()
        reservation = _ApiStageCommandReservation(
            operation_id=uuid.uuid4().hex,
            action=str(action),
            completion=completion,
        )
        lock, changed = self._api_stage_command_worker_state()
        with lock:
            active = self._api_stage_command_reservation
            if active is not None and active.state != "released":
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": f"API stage command is already running: {active.action}.",
                }
            self._api_stage_command_reservation = reservation
            changed.notify_all()
        request = dict(command_request)
        try:
            thread = threading.Thread(
                target=self._run_deferred_api_stage_command,
                args=(request, reservation),
                name=f"ApiStageCommand-{action}",
                daemon=True,
            )
            with lock:
                reservation.thread = thread
                reservation.state = "running"
                changed.notify_all()
            thread.start()
        except Exception as exc:
            self._release_api_stage_command_reservation(reservation)
            return {
                "accepted": False,
                "status_code": 500,
                "message": f"API stage command could not start: {exc}",
            }
        return completion

    def _run_deferred_api_stage_command(
        self,
        command_request: dict[str, Any],
        reservation: _ApiStageCommandReservation,
    ) -> None:
        try:
            response = self._dispatch_api_command_request(
                command_request,
                apply_route_control_guard=False,
            )
        except Exception as exc:
            logger.exception("API stage command failed.")
            response = {
                "accepted": False,
                "status_code": 500,
                "message": str(exc),
            }
        lock, changed = self._api_stage_command_worker_state()
        with lock:
            if self._api_stage_command_reservation is reservation:
                reservation.state = "publishing"
                changed.notify_all()
        try:
            reservation.completion.complete(response)
        finally:
            self._release_api_stage_command_reservation(reservation)

    def _submit_api_command_request_from_api_thread(
        self,
        command_request: dict[str, Any],
    ) -> dict[str, Any]:
        return command_dispatch_from_api_thread(
            command_request,
            submit_on_gui_thread=self._submit_api_command_request_on_gui_thread,
            submit_probe_route_window_guard_on_gui_thread=(
                self._submit_probe_route_window_guard_on_gui_thread
            ),
            dispatch_direct=self._dispatch_api_command_request,
            route_window_required=self._probe_route_api_requires_window,
        )

    def _submit_api_command_request_on_gui_thread(
        self,
        command_request: dict[str, Any],
    ) -> dict[str, Any]:
        if self._api_bridge is None:
            return {
                "accepted": False,
                "status_code": 503,
                "message": "GUI API bridge is not ready.",
            }
        return self._api_bridge.submit(
            {
                "action": "command",
                "command": dict(command_request),
            },
            timeout_s=10.0,
        )

    def _submit_probe_route_window_guard_on_gui_thread(
        self,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self._api_bridge is None:
            return {
                "accepted": False,
                "status_code": 503,
                "message": "GUI API bridge is not ready.",
            }
        return self._api_bridge.submit(
            {
                "action": "probe_route_window_guard",
                "guard_action": action,
                "payload": dict(payload),
            },
            timeout_s=10.0,
        )

    @staticmethod
    def _api_command_action_payload(
        command_request: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        return command_dispatch_action_payload(command_request)

    def _api_command_dispatch_handlers(self) -> ApiCommandDispatchHandlers:
        return ApiCommandDispatchHandlers(
            route_control_guard=self._probe_route_api_window_guard,
            list_contacts=self._api_list_contacts,
            move_to_contact=self._api_move_to_contact,
            contact_needles=self._api_contact_needles,
            check_contact=self._api_check_contact,
            stage_local_focus=self._api_stage_local_focus,
            route_contact_focus=self._api_route_contact_focus,
            route_contact_photo=self._api_route_contact_photo,
            contact_seek=self._api_contact_seek,
            api_route_control_status=self._api_route_control_status,
            api_route_control_action=self._api_route_control_action,
            configure_meter=self._api_configure_meter,
            raw_voltage_sweep=self._api_raw_voltage_sweep,
            visa_list_resources=self._api_visa_list_resources,
            visa_operation=self._api_visa_operation,
            start_route_session=self._api_start_route_session,
            route_session_status=self._api_route_session_status,
            route_session_action=self._api_route_session_action,
            route_session_result=self._api_route_session_result,
            route_session_seek=self._api_route_session_seek,
            route_session_artifact=self._api_route_session_artifact,
            lens_distortion_calibration=self._api_lens_distortion_calibration,
            click_to_move_calibration=self._api_click_to_move_calibration,
            microscope_area_scan=self._api_microscope_area_scan,
        )

    def _dispatch_api_command_request(
        self,
        command_request: dict[str, Any],
        *,
        apply_route_control_guard: bool,
    ) -> dict[str, Any]:
        return command_dispatch_request(
            command_request,
            self._api_command_dispatch_handlers(),
            apply_route_control_guard=apply_route_control_guard,
        )

    def _probe_route_api_window_guard(
        self,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self._probe_route_api_requires_window(action, payload):
            return None
        if self._route_control_window_is_open():
            return None
        message = (
            "Probe route control window is closed. Open the route measurement "
            "window before using probe route API commands."
        )
        self._show_status(message, 8000)
        return {
            "accepted": False,
            "status_code": 409,
            "message": message,
            "route_control_window_open": False,
        }

    def _probe_route_api_requires_window(
        self,
        action: str,
        payload: dict[str, Any],
    ) -> bool:
        return probe_route_api_requires_window(action, payload)

    def _route_control_window_is_open(self) -> bool:
        dialog = getattr(self, "_route_measurement_dialog", None)
        if dialog is None:
            return False
        is_visible = getattr(dialog, "isVisible", None)
        if callable(is_visible):
            try:
                return bool(is_visible())
            except Exception:
                logger.exception("Failed to query route control window visibility.")
                return False
        return True

    def _handle_api_request(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any] | DeferredApiResponse:
        return command_dispatch_handle_api_request(
            request,
            ApiBridgeRequestHandlers(
                move_to_coordinates=self._api_move_to_coordinates,
                stage_status=self._api_stage_status,
                submit_command=self._submit_api_command_request,
                route_control_guard=self._probe_route_api_window_guard,
            ),
        )

    def _api_move_to_coordinates(self, targets: object, *, mode: object = "G90", feedrate: object = None) -> dict[str, Any]:
        move_plan = api_coordinate_move_plan(
            targets,
            axis_names=self.STAGE_AXIS_NAMES,
            mode=mode,
            feedrate=feedrate,
            current_feedrate=self._current_linear_feedrate(),
            min_feedrate=self.MIN_FEEDRATE_MM_MIN,
            resolve_axis_target=self._resolve_stage_axis_target,
            axis_target_limit_error=self._stage_axis_target_limit_error,
        )
        if isinstance(move_plan, dict):
            return move_plan
        if self._coordinate_targets.has_active_move() or self.stage_controller.is_busy():
            return api_coordinate_move_busy_response()
        if not self._start_coordinate_targets_move(move_plan.target_map, feedrate_mm_min=move_plan.feedrate_mm_min, source_label="API"):
            return api_coordinate_move_start_failed_response()
        return api_coordinate_move_success_response(move_plan, coordinate_display=self.stage_controller.coordinate_display_name())

    def _api_stage_status(self) -> dict[str, Any]:
        latest_position = self.stage_controller.latest_stage_position()
        return self._stage_status_payload(
            latest_position,
            display_position={
                axis: float(value)
                for axis, value in self._stage_axis_display_values.items()
            },
            accepted=True,
        )

    def _surface_map_stage_status(self) -> dict[str, Any]:
        latest_position = self.stage_controller.latest_stage_position()
        display_position = {
            axis: float(value)
            for axis, value in self._stage_axis_display_values.items()
        }
        if isinstance(latest_position, (tuple, list)):
            for axis, value in zip(self.STAGE_AXIS_NAMES, latest_position):
                display_position.setdefault(axis, float(value))
        return self._stage_status_payload(
            latest_position,
            display_position=display_position,
            accepted=False,
        )

    def _stage_status_payload(
        self,
        latest_position: object,
        *,
        display_position: dict[str, float],
        accepted: bool,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        if accepted:
            payload["accepted"] = True
        payload.update(
            {
                "connected": bool(
                    self.serial_connection is not None
                    and getattr(self.serial_connection, "is_open", False)
                ),
                "busy": self.stage_controller.is_busy(),
                "state": self.stage_controller.latest_stage_state(),
                "coordinate_display": self.stage_controller.coordinate_display_name(),
                "homed_axes": sorted(self.stage_controller.homed_axes()),
                "position": api_axis_value_map(
                    latest_position,
                    axis_names=self.STAGE_AXIS_NAMES,
                ),
                "display_position": display_position,
                "pending_targets": {
                    axis: float(values[1])
                    for axis, values in self._pending_stage_axis_targets.items()
                },
                "active_coordinate_axis": self._coordinate_targets.active_axis,
                "active_coordinate_axes": sorted(self._coordinate_targets.active_axes),
                "current_feedrate_mm_min": self._current_linear_feedrate(),
            }
        )
        return payload

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
        if self._coordinate_targets.has_active_move() or self.stage_controller.is_busy():
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
            api_route_point_payload(
                route_index=index,
                route_point=route_point,
                include_stage_xy=registration_valid,
                resolve_stage_xy=self._raw_stage_xy_from_design_xy,
                structure_number_for_route_point=self._api_structure_number_for_route_point,
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
        contact_plan_result = api_move_to_contact_plan(
            payload,
            contact_context=self._api_contact_context,
            default_needle_feedrate=lambda: self._api_needle_feedrate({}),
            min_feedrate=self.MIN_FEEDRATE_MM_MIN,
        )
        if isinstance(contact_plan_result, dict):
            return contact_plan_result
        contact_plan = contact_plan_result
        needles_lowered = False
        try:
            with self.stage_controller.reserve_external_task("API contact move"):
                try:
                    if contact_plan.request.lift_before_move:
                        self.stage_controller.run_external_needles_action(
                            "lift",
                            contact_plan.request.needle_feedrate_mm_min,
                        )
                    target_xy = self._api_route_adjusted_stage_xy(contact_plan.point)
                    self.stage_controller.run_external_move_to_xy(
                        target_xy[0],
                        target_xy[1],
                    )
                    if contact_plan.request.lower_needles:
                        self.stage_controller.run_external_needles_action(
                            "lower",
                            contact_plan.request.needle_feedrate_mm_min,
                        )
                        needles_lowered = True
                        if contact_plan.request.contact_settle_s > 0.0:
                            time.sleep(contact_plan.request.contact_settle_s)
                    return api_move_to_contact_success_response(
                        contact_plan,
                        timestamp_utc=self._api_timestamp_utc(),
                        route_offset_xy=self._api_route_offset_xy,
                        target_stage_xy=target_xy,
                        needles_lowered=needles_lowered,
                    )
                finally:
                    if contact_plan.request.lift_after and needles_lowered:
                        try:
                            self.stage_controller.run_external_needles_action(
                                "lift",
                                contact_plan.request.needle_feedrate_mm_min,
                            )
                        except StageControllerError:
                            logger.exception("API contact move failed to lift needles.")
        except StageControllerError as exc:
            return api_move_to_contact_stage_error_response(
                str(exc),
                contact=contact_plan.contact,
            )

    def _api_contact_needles(self, payload: dict[str, Any]) -> dict[str, Any]:
        contact_plan_result = api_contact_needles_plan(
            payload,
            contact_context=self._api_contact_context,
            default_needle_feedrate=lambda: self._api_needle_feedrate({}),
            min_feedrate=self.MIN_FEEDRATE_MM_MIN,
        )
        if isinstance(contact_plan_result, dict):
            return contact_plan_result
        contact_plan = contact_plan_result
        try:
            with self.stage_controller.reserve_external_task("API needle action"):
                self.stage_controller.run_external_needles_action(
                    contact_plan.request.action,
                    contact_plan.request.needle_feedrate_mm_min,
                )
                return api_contact_needles_success_response(
                    contact_plan,
                    timestamp_utc=self._api_timestamp_utc(),
                )
        except StageControllerError as exc:
            return api_contact_needles_stage_error_response(
                str(exc),
                contact=contact_plan.contact,
            )

    def _api_check_contact(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_measure_current_contact(payload, seek=False)

    def _api_focus_settings_from_payload(
        self,
        payload: dict[str, Any],
    ) -> tuple[float, float | None]:
        return (
            payload_float(
                payload,
                "range_mm",
                "focus_range_mm",
                "photo_autofocus_range_mm",
                default=0.03,
                minimum=0.001,
            ),
            payload_optional_float(
                payload,
                "step_mm",
                "focus_step_mm",
                minimum=0.001,
            ),
        )

    def _api_focus_needles_rejection(
        self,
        message: str,
        *,
        contact: Any | None = None,
    ) -> dict[str, Any] | None:
        needles_known = bool(getattr(self.stage_controller, "_needles_known", False))
        needles_up = bool(getattr(self.stage_controller, "_needles_up", False))
        needles_zone = getattr(self.stage_controller, "_needles_zone", None)
        if needles_known and needles_up and needles_zone == "raise":
            return None
        response: dict[str, Any] = {
            "accepted": False,
            "status_code": 409,
            "message": message,
        }
        if contact is not None:
            response["contact"] = contact
        response.update(
            {
                "needles_known": needles_known,
                "needles_up": needles_up,
                "needles_zone": needles_zone or "unknown",
            }
        )
        return response

    def _api_contact_context_from_payload(
        self,
        payload: dict[str, Any],
    ) -> tuple[int | None, Any | None, dict[str, Any] | None]:
        contact_number = api_contact_number_from_payload(payload)
        if contact_number is None:
            return (
                None,
                None,
                {
                    "accepted": False,
                    "status_code": 400,
                    "message": "Provide a positive contact_number.",
                },
            )
        context_result = self._api_contact_context(contact_number)
        if not context_result.get("accepted", False):
            return contact_number, None, context_result
        return contact_number, context_result["contact"], None

    def _api_stage_local_focus(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            focus_range_mm, focus_step_mm = self._api_focus_settings_from_payload(
                payload
            )
        except ValueError as exc:
            return {
                "accepted": False,
                "status_code": 400,
                "message": str(exc),
            }

        try:
            with self.stage_controller.reserve_external_task("API local autofocus"):
                needles_rejection = self._api_focus_needles_rejection(
                    "Local autofocus requires fully raised needles "
                    "(known needle zone 'raise')."
                )
                if needles_rejection is not None:
                    return needles_rejection
                result = self.stage_controller.run_external_local_autofocus(
                    range_mm=focus_range_mm,
                    step_mm=focus_step_mm,
                )
        except StageControllerError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        return {
            "accepted": True,
            "message": str(result.summary()),
            "timestamp_utc": self._api_timestamp_utc(),
            "focus_range_mm": focus_range_mm,
            "focus_step_mm": focus_step_mm,
            "focus": route_photo_focus_payload(result),
        }

    def _api_route_contact_focus(self, payload: dict[str, Any]) -> dict[str, Any]:
        _contact_number, contact, context_error = self._api_contact_context_from_payload(
            payload
        )
        if context_error is not None:
            return context_error
        try:
            focus_range_mm, focus_step_mm = self._api_focus_settings_from_payload(
                payload
            )
        except ValueError as exc:
            return {
                "accepted": False,
                "status_code": 400,
                "message": str(exc),
                "contact": contact,
            }

        try:
            with self.stage_controller.reserve_external_task("API route contact focus"):
                needles_rejection = self._api_focus_needles_rejection(
                    "Route contact focus requires fully raised needles "
                    "(known needle zone 'raise').",
                    contact=contact,
                )
                if needles_rejection is not None:
                    return needles_rejection
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
        return {
            "accepted": True,
            "message": str(result.summary()),
            "timestamp_utc": self._api_timestamp_utc(),
            "contact": contact,
            "focus_range_mm": focus_range_mm,
            "focus_step_mm": focus_step_mm,
            "focus": route_photo_focus_payload(result),
        }

    def _api_route_contact_photo(self, payload: dict[str, Any]) -> dict[str, Any]:
        contact_number, contact, context_error = self._api_contact_context_from_payload(
            payload
        )
        if context_error is not None:
            return context_error
        photo = self._latest_camera_frame_photo()
        if photo is None:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Camera frame is unavailable; cannot capture contact photo.",
                "contact": contact,
            }
        photo_bytes, photo_name = photo
        suffix = Path(photo_name).suffix or ".jpg"
        filename = f"contact_{contact_number:03d}_photo{suffix}"
        return {
            "accepted": True,
            "message": f"Captured contact {contact_number} photo.",
            "timestamp_utc": self._api_timestamp_utc(),
            "contact": contact,
            "filename": filename,
            "content_type": (
                "image/jpeg"
                if filename.lower().endswith((".jpg", ".jpeg"))
                else "image/png"
            ),
            "data": photo_bytes,
        }

    def _api_contact_seek(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_measure_current_contact(payload, seek=True)

    def _api_route_control_state_snapshot(self) -> ApiRouteControlState:
        state = getattr(self, "_api_route_control_state", ApiRouteControlState())
        if not isinstance(state, ApiRouteControlState):
            state = ApiRouteControlState()
        return api_route_control_state_from_legacy_attrs(self, fallback=state)

    def _set_api_route_control_state(self, state: ApiRouteControlState) -> None:
        self._api_route_control_state = state
        for name, value in api_route_control_legacy_attrs(state).items():
            setattr(self, name, value)

    def _api_route_control_status(self) -> dict[str, Any]:
        return self._api_route_control_state_snapshot().status_payload(
            route_control_window_open=self._route_control_window_is_open()
        )

    def _api_route_control_operation_adapters(self) -> ApiRouteControlOperationAdapters:
        return ApiRouteControlOperationAdapters(
            state=ApiRouteControlStateAdapter(
                snapshot=self._api_route_control_state_snapshot,
                set_state=self._set_api_route_control_state,
                status_payload=self._api_route_control_status,
                update_ui=self._update_api_route_control_ui,
                updated_utc=self._api_timestamp_utc,
            ),
            window=ApiRouteControlWindowAdapter(
                is_open=self._route_control_window_is_open,
                guard_closed=lambda action, payload: self._probe_route_api_window_guard(
                    action,
                    payload,
                ),
                open_for_api_start=self._show_route_measurement_dialog_for_api_session,
            ),
            runner=ApiRouteControlRunnerAdapter(
                clear_waiting_before_start=(
                    self._clear_waiting_route_runner_before_api_control
                ),
            ),
            interrupt=ApiRouteControlInterruptAdapter(
                perform=lambda reason, planned_state, planned_message: (
                    self._interrupt_api_route_controlled_operation(
                        reason,
                        planned_state=planned_state,
                        planned_message=planned_message,
                    )
                )
            ),
        )

    def _api_route_control_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        return execute_api_route_control_action(
            payload,
            self._api_route_control_operation_adapters(),
        )

    def _clear_waiting_route_runner_before_api_control(
        self,
    ) -> dict[str, Any] | None:
        runner = getattr(self, "_route_measurement_runner", None)
        thread = getattr(self, "_route_measurement_thread", None)
        thread_alive = bool(thread is not None and thread.is_alive())
        if runner is None and not thread_alive:
            return None
        if runner is not None and not thread_alive:
            self._clear_waiting_route_measurement_state()
            return None
        can_clear_waiting_gui_route = (
            runner is not None
            and bool(getattr(self, "_route_measurement_waiting", False))
            and not hasattr(runner, "submit_external_result")
        )
        if not can_clear_waiting_gui_route:
            return {
                "accepted": False,
                "status_code": 409,
                "message": (
                    "Route measurement is already active. Stop or pause it "
                    "before starting API route control."
                ),
            }
        try:
            runner.stop()
            if thread is not None:
                thread.join(timeout=2.0)
        except Exception as exc:
            logger.exception("Failed to clear waiting route measurement.")
            return {
                "accepted": False,
                "status_code": 409,
                "message": f"Failed to stop waiting route measurement: {exc}",
            }
        if thread is not None and thread.is_alive():
            return {
                "accepted": False,
                "status_code": 409,
                "message": (
                    "Waiting route measurement did not stop before API route "
                    "control start."
                ),
            }
        self._clear_waiting_route_measurement_state()
        return None

    def _clear_waiting_route_measurement_state(self) -> None:
        self._route_measurement_runner = None
        self._route_measurement_thread = None
        self._route_measurement_waiting = False
        self._route_measurement_waiting_reason = ""
        self._route_measurement_session_active = False
        self._pending_route_measure_point = None

    def _request_api_route_control_pause(self, message: str) -> dict[str, Any]:
        state, transition_message = (
            self._api_route_control_state_snapshot().request_pause(
                updated_utc=self._api_timestamp_utc(),
            )
        )
        self._set_api_route_control_state(state)
        message = message or transition_message
        self._update_api_route_control_ui(message)
        status = self._api_route_control_status()
        status["message"] = message
        return status

    def _ack_api_route_control_pause(self, message: str) -> dict[str, Any]:
        state, transition_message = self._api_route_control_state_snapshot().ack_pause(
            updated_utc=self._api_timestamp_utc(),
        )
        self._set_api_route_control_state(state)
        message = message or transition_message
        self._update_api_route_control_ui(message)
        status = self._api_route_control_status()
        status["message"] = message
        return status

    def _interrupt_api_route_controlled_operation(
        self,
        reason: str,
        *,
        planned_state: ApiRouteControlState | None = None,
        planned_message: str = "",
    ) -> dict[str, Any]:
        self._pending_route_measure_point = None
        self._contact_seek_stop_requested.set()
        try:
            self.stage_controller.cancel_active_task(reason)
        except Exception:
            logger.exception("Failed to cancel API route control stage task.")
        try:
            if self._controller_reports_active_motion():
                self.stage_controller.cancel_active_motion(reason)
        except Exception:
            logger.exception("Failed to cancel API route control active motion.")
        stage_position_panel_adapter.clear_stage_motion_axes(self)
        try:
            self._clear_planned_move_prediction(clear_wait_state=True)
        except Exception:
            logger.exception("Failed to clear planned move prediction after API route control interrupt.")
        self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
        if planned_state is None:
            state, message = self._api_route_control_state_snapshot().interrupt(
                updated_utc=self._api_timestamp_utc(),
            )
        else:
            state = planned_state
            message = planned_message or f"{state.display_label}: interrupted; paused."
        self._set_api_route_control_state(state)
        self._update_api_route_control_ui(message)
        status = self._api_route_control_status()
        status["message"] = message
        return status

    def _update_api_route_control_ui(self, message: str) -> None:
        ui_state = self._api_route_control_state_snapshot().ui_state()
        self._route_measurement_waiting = ui_state.waiting
        self._route_measurement_waiting_reason = ui_state.waiting_reason
        self._route_runtime_presenter().apply_api_control_update(ui_state, message)
        self._show_status(message, 5000)

    def _route_runtime_presenter(self) -> RouteRuntimePresentationSink:
        presenter = getattr(self, "_route_runtime_presenter_instance", None)
        if presenter is None:
            presenter = RouteRuntimePresentationSink(
                current_dialog=lambda: getattr(self, "_route_measurement_dialog", None),
                current_navigator=lambda: getattr(self, "design_navigator_panel", None),
            )
            self._route_runtime_presenter_instance = presenter
        return presenter

    def _api_measure_current_contact(
        self,
        payload: dict[str, Any],
        *,
        seek: bool,
    ) -> dict[str, Any]:
        contact_number = api_contact_number_from_payload(payload)
        if contact_number is None:
            return api_current_contact_error_response(
                "Provide a positive contact_number.",
                status_code=400,
            )
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
            settings = api_current_contact_settings_from_payload(
                payload,
                default_check_sample_count=RouteMeasurementRunner.SHORT_CHECK_SAMPLE_COUNT,
                default_contact_seek_range_mm=RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM,
                default_contact_seek_step_mm=RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM,
                default_contact_settle_s=RouteMeasurementRunner.DEFAULT_CONTACT_SETTLE_S,
            )
        except ValueError as exc:
            return api_current_contact_error_response(
                str(exc),
                status_code=400,
                contact=contact,
            )

        needle_feedrate = self._api_needle_feedrate(payload)
        runner = RouteMeasurementRunner(
            points=[point],
            csv_path=Path(os.devnull),
            stage_controller=self.stage_controller,
            lcr_controller=self.lcr_controller,
            needle_feedrate=needle_feedrate,
            measurement_count=settings.measurement_count,
            initial_measurement_count=settings.check_sample_count,
            start_point_number=int(point.index),
            max_relative_rms=settings.max_relative_rms,
            contact_quality_limits=settings.contact_quality_limits,
            auto_contact_seek_on_bad_contact=seek,
            auto_contact_seek_step_mm=settings.contact_seek_step_mm,
            auto_contact_seek_max_total_mm=settings.contact_seek_range_mm,
            contact_settle_s=settings.contact_settle_s,
            operation_mode=ROUTE_OPERATION_MEASURE,
            events=RouteMeasurementEvents(
                status=self.route_measurement_status.emit,
            ),
        )
        try:
            result = (
                runner.seek_contact(point)
                if seek
                else runner.check_contact(point)
            )
        except (StageControllerError, LCRMeterError, RuntimeError) as exc:
            return api_current_contact_error_response(
                str(exc),
                status_code=409,
                contact=contact,
            )
        except Exception as exc:
            prefix = "Contact seek failed" if seek else "Contact check failed"
            logger.exception("API %s.", prefix.lower())
            return self._api_instrument_exception_response(
                prefix,
                exc,
                contact=contact,
            )
        response = api_current_contact_response(
            result,
            contact=contact,
            settings=settings,
            needle_feedrate=needle_feedrate,
            timestamp_utc=self._api_timestamp_utc(),
            seek=seek,
        )
        if seek:
            alert = api_current_contact_failure_alert(
                payload,
                contact_number=contact_number,
                result=result,
                reply_markup=self._telegram_route_actions_markup(),
            )
            if alert is not None:
                self._send_telegram_alert(
                    alert.channel,
                    alert.text,
                    attach_photo=alert.attach_photo,
                    reply_markup=alert.reply_markup,
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
        try:
            request = api_raw_voltage_sweep_request_from_payload(payload)
            configuration = self._api_route_meter_configuration(
                api_raw_voltage_sweep_meter_payload(payload),
                voltages_v=request.voltage_values,
            )
        except ApiRawVoltageSweepRequestError as exc:
            return exc.response()
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

        context_result = None
        if request.contact_number is not None:
            context_result = self._api_contact_context(request.contact_number)
        contact_plan_result = api_raw_voltage_sweep_contact_plan(
            request,
            context_result=context_result,
        )
        if isinstance(contact_plan_result, dict):
            return contact_plan_result
        contact_plan = contact_plan_result
        needle_feedrate = self._api_needle_feedrate(payload)
        stage_lease = None
        needles_lowered = False
        started_at = time.monotonic()
        timestamp_utc = self._api_timestamp_utc()
        try:
            if request.move_to_contact or request.lower_needles or request.lift_after:
                stage_lease = self.stage_controller.reserve_external_task("API raw voltage sweep")
            if stage_lease is not None and request.lift_before_move:
                self.stage_controller.run_external_needles_action(
                    "lift",
                    needle_feedrate,
                )
            if request.move_to_contact and contact_plan.point is not None:
                target_xy = self._api_route_adjusted_stage_xy(contact_plan.point)
                self.stage_controller.run_external_move_to_xy(
                    target_xy[0],
                    target_xy[1],
                )
            if stage_lease is not None and request.lower_needles:
                self.stage_controller.run_external_needles_action(
                    "lower",
                    needle_feedrate,
                )
                needles_lowered = True
                if request.contact_settle_s > 0.0:
                    time.sleep(request.contact_settle_s)
            raw_measurement = self.lcr_controller.read_voltage_sweep_now(
                request.voltage_values
            )
            return api_raw_voltage_sweep_success_response(
                voltage_values=request.voltage_values,
                result=self._api_json_ready(raw_measurement),
                timestamp_utc=timestamp_utc,
                elapsed_s=time.monotonic() - started_at,
                contact=contact_plan.contact,
                meter_type=configuration.meter_type,
                lower_needles=request.lower_needles,
                needles_lowered=needles_lowered,
                lift_after=request.lift_after,
            )
        except (StageControllerError, LCRMeterError) as exc:
            return api_raw_voltage_sweep_error_response(
                str(exc),
                contact=contact_plan.contact,
            )
        except Exception as exc:
            logger.exception("API raw voltage sweep failed.")
            return self._api_instrument_exception_response(
                "Raw voltage sweep failed",
                exc,
                contact=contact_plan.contact,
            )
        finally:
            if stage_lease is not None:
                try:
                    if request.lift_after and needles_lowered:
                        self.stage_controller.run_external_needles_action(
                            "lift",
                            needle_feedrate,
                        )
                except StageControllerError:
                    logger.exception("API raw voltage sweep failed to lift needles.")
                finally:
                    stage_lease.release()

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

    def _api_route_session_thread_preflight(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, tuple[float, float]]:
        thread = self._route_measurement_thread
        if thread is None or not thread.is_alive():
            return None, (0.0, 0.0)
        runner = self._route_measurement_runner
        if isinstance(runner, RouteExternalMeasurementSessionRunner):
            return (
                api_route_existing_session_response(
                    payload=payload,
                    status=runner.status_payload(),
                ),
                (0.0, 0.0),
            )
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
            }, (0.0, 0.0)
        route_offset_xy = runner.route_offset_xy()
        runner.stop()
        thread.join(timeout=2.0)
        if thread.is_alive():
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Waiting GUI route measurement did not stop.",
            }, route_offset_xy
        self._route_measurement_thread = None
        self._route_measurement_runner = None
        self._route_measurement_waiting = False
        self._route_measurement_session_active = False
        return None, route_offset_xy

    def _build_api_route_session_runner(
        self,
        *,
        session_id: str,
        points: list[RouteMeasurementPoint],
        selected_point: RouteMeasurementPoint,
        start_settings: RouteExternalSessionStartSettings,
        meter_configuration: RouteMeterConfiguration,
        needle_feedrate: float | None,
    ) -> RouteExternalMeasurementSessionRunner:
        return RouteExternalMeasurementSessionRunner(
            session_id=session_id,
            points=points,
            stage_controller=self.stage_controller,
            lcr_controller=self._api_route_lcr_controller,
            needle_feedrate=needle_feedrate,
            measurement_count=start_settings.measurement_count,
            initial_measurement_count=start_settings.initial_measurement_count,
            start_point_number=int(selected_point.index),
            max_relative_rms=start_settings.max_relative_rms,
            contact_quality_limits=start_settings.contact_quality_limits,
            auto_contact_seek_step_mm=start_settings.contact_seek_step_mm,
            auto_contact_seek_max_total_mm=start_settings.contact_seek_range_mm,
            contact_settle_s=start_settings.contact_settle_s,
            nplc_label=meter_configuration.nplc_label(),
            measurement_type=meter_configuration.measurement_type_label(),
            status_callback=self.route_measurement_status.emit,
            progress_callback=self.route_measurement_progress.emit,
            photo_callback=self._capture_api_route_photo_artifact,
            photo_focus_callback=lambda point, position, total: self._api_route_photo_autofocus(
                point,
                position,
                total,
                range_mm=start_settings.photo_focus_range_mm,
            ),
            contact_photo_callback=self._capture_route_contact_photo,
            pre_contact_photo_callback=self._capture_route_pre_contact_photo,
            result_callback=self.route_measurement_result.emit,
            waiting_callback=self.route_measurement_waiting_changed.emit,
            photo_enabled=start_settings.photo_enabled,
            photo_focus_enabled=start_settings.photo_focus_enabled,
            photo_settle_s=start_settings.photo_settle_s,
            wait_before_first_point=True,
        )

    def _cleanup_failed_api_route_session_start(
        self,
        runner: RouteExternalMeasurementSessionRunner,
    ) -> dict[str, Any]:
        status = runner.status_payload()
        thread = self._route_measurement_thread
        if thread is not None and thread.is_alive():
            runner.stop()
            thread.join(timeout=2.0)
        self._route_measurement_thread = None
        self._route_measurement_runner = None
        self._api_route_lcr_controller = None
        self._route_measurement_waiting = False
        self._route_measurement_session_active = False
        return status

    def _api_start_route_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        active_response, route_offset_xy = self._api_route_session_thread_preflight(
            payload
        )
        if active_response is not None:
            return active_response
        if self.serial_connection is None or not self.serial_connection.is_open:
            return {
                "accepted": False,
                "status_code": 503,
                "message": "Serial connection is not available.",
            }
        start_decision = api_route_session_start_decision(
            route=self._design_session.route,
            registration_valid=bool(
                getattr(self._design_session.registration, "valid", False)
            ),
            payload=payload,
            current_point=int(self._route_measurement_current_point or 1),
            points_factory=self._route_measurement_points,
            default_contact_seek_range_mm=(
                RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM
            ),
            default_contact_seek_step_mm=RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM,
            default_contact_settle_s=RouteMeasurementRunner.DEFAULT_CONTACT_SETTLE_S,
        )
        if not start_decision.accepted:
            return start_decision.rejection_payload()
        assert start_decision.plan is not None
        points = start_decision.plan.points
        selected_point = start_decision.plan.selected_point
        start_settings = start_decision.plan.start_settings
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
        self._api_route_artifacts_store().clear()
        self._api_route_session_id = session_id
        self._api_route_lcr_controller = route_lcr_controller
        self._api_route_last_status = None
        runner = self._build_api_route_session_runner(
            session_id=session_id,
            points=points,
            selected_point=selected_point,
            start_settings=start_settings,
            meter_configuration=meter_configuration,
            needle_feedrate=self._api_needle_feedrate(payload),
        )
        runner.set_route_offset_xy(route_offset_xy)
        launch_state = api_route_session_launch_state(
            session_id=session_id,
            points=points,
            selected_point=selected_point,
            photo_enabled=start_settings.photo_enabled,
        )
        self._route_measurement_runner = runner
        self._route_measurement_waiting = False
        self._route_measurement_waiting_reason = ""
        self._pending_route_measure_point = None
        self._route_measurement_session_active = True
        self._route_measurement_photo_enabled = launch_state.photo_enabled
        self._route_measurement_measure_enabled = launch_state.measure_enabled
        self._route_measurement_point_numbers = launch_state.point_numbers
        self._route_telegram_adapter().reset_for_route_start()
        self._route_measurement_thread = threading.Thread(
            target=self._run_route_measurement,
            args=(runner,),
            name="RouteApiExternalSession",
            daemon=True,
        )
        self._last_route_measurement_result = None
        self._route_measurement_thread.start()
        if not runner.wait_until_initial_pause(timeout_s=10.0):
            status = self._cleanup_failed_api_route_session_start(runner)
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
            launch_state.start_message,
            len(points),
            launch_state.selected_point_number,
            True,
        )
        self._send_telegram_alert(
            "route_started",
            route_start_telegram_text(
                launch_state.start_message,
                api_session=True,
            ),
        )
        return runner.status_payload()

    def _show_route_measurement_dialog_for_api_session(self) -> bool:
        try:
            self._open_route_measurement_dialog(start_context=False)
        except Exception:
            logger.exception("Failed to open route measurement controls for API session.")
            return False
        return self._route_control_window_is_open()

    def _api_route_session_status(self) -> dict[str, Any]:
        return api_route_session_status_response(getattr(self, "_route_measurement_runner", None), self._api_route_last_status, self._api_route_artifacts_payload())

    def _api_route_session_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        return api_route_session_action_response(payload, runner=getattr(self, "_route_measurement_runner", None), interrupt_runner=self._interrupt_route_measurement_runner)

    def _api_route_session_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        return api_route_session_result_response(payload, runner=self._route_measurement_runner, timestamp_utc=self._api_timestamp_utc())

    def _api_route_session_seek(self) -> dict[str, Any]:
        return api_route_session_seek_response(self._route_measurement_runner)

    def _api_route_session_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_route_artifacts_store().artifact_response(payload)

    def _api_lens_distortion_calibration(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if bool(payload.get("reset", False)):
            accepted, message = self._reset_lens_distortion_calibration()
            return {
                "accepted": accepted,
                "status_code": 200 if accepted else 409,
                "message": (
                    "Lens distortion calibration reset requested."
                    if accepted
                    else message
                ),
            }
        return self._start_lens_distortion_calibration()

    def _api_click_to_move_calibration(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        force = bool(payload.get("force", False))
        try:
            dx_px = float(payload.get("dx_px", 0.0))
            dy_px = float(payload.get("dy_px", 0.0))
        except (TypeError, ValueError):
            return {
                "accepted": False,
                "status_code": 400,
                "message": "dx_px and dy_px must be numeric.",
            }
        if not math.isfinite(dx_px) or not math.isfinite(dy_px):
            return {
                "accepted": False,
                "status_code": 400,
                "message": "dx_px and dy_px must be finite.",
            }
        if not self._stage_serial_ready():
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Connect the stage controller before calibration.",
            }
        if self._objective_mutation_busy():
            return {
                "accepted": False,
                "status_code": 409,
                "message": (
                    "Click-to-move calibration cannot start while a scan, "
                    "calibration, or stage task is active."
                ),
            }
        if force:
            reset, message = self._reset_click_calibration()
            if not reset:
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": message,
                }
        if not self._microscope_interaction.try_start_api_move(dx_px, dy_px):
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Click-to-move calibration not started.",
            }
        return {
            "accepted": True,
            "status_code": 202,
            "message": "Click-to-move calibration started.",
        }

    def _api_microscope_area_scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "auto_exposure" in payload:
            return {
                "accepted": False,
                "status_code": 400,
                "message": "auto_exposure is no longer supported for area scans.",
            }
        if self._microscope_scan_running():
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Microscope scan is already running.",
            }
        if not self._stage_serial_ready():
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Connect the stage controller before scanning.",
            }
        if self.stage_controller.is_busy():
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Stage is busy; microscope area scan not started.",
            }
        scale = self._active_microscope_scale()
        if scale is None:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Calibrate click-to-move for the active objective before scanning.",
            }
        try:
            scan_pattern = self._microscope_area_scan_pattern(payload)
            if scan_pattern == "stitch_debug":
                rows = 3
                columns = 3
                overlap_default = self.MICROSCOPE_AREA_SCAN_STITCH_DEBUG_OVERLAP_FRACTION
                structure_size_mm = self._microscope_area_scan_float(
                    payload.get(
                        "structure_size_mm",
                        self.MICROSCOPE_AREA_SCAN_STITCH_DEBUG_STRUCTURE_MM,
                    ),
                    "structure_size_mm",
                    minimum=0.0,
                    maximum=100.0,
                )
                placement_fraction = self._microscope_area_scan_float(
                    payload.get(
                        "placement_fraction",
                        self.MICROSCOPE_AREA_SCAN_STITCH_DEBUG_PLACEMENT_FRACTION,
                    ),
                    "placement_fraction",
                    minimum=0.0,
                    maximum=1.0,
                )
            else:
                overlap_default = self.MICROSCOPE_AREA_SCAN_DEFAULT_OVERLAP_FRACTION
                rows = self._microscope_area_scan_count(
                    payload.get("rows", self.MICROSCOPE_AREA_SCAN_DEFAULT_ROWS),
                    "rows",
                )
                columns = self._microscope_area_scan_count(
                    payload.get("columns", self.MICROSCOPE_AREA_SCAN_DEFAULT_COLUMNS),
                    "columns",
                )
                if rows * columns > self.MICROSCOPE_AREA_SCAN_MAX_TILES:
                    raise ValueError(
                        f"Area scan is too large; maximum is {self.MICROSCOPE_AREA_SCAN_MAX_TILES} tiles."
                    )
                structure_size_mm = None
                placement_fraction = None
            overlap_fraction = self._microscope_area_scan_float(
                payload.get(
                    "overlap_fraction",
                    overlap_default,
                ),
                "overlap_fraction",
                minimum=0.0,
                maximum=0.95,
            )
            settle_s = self._microscope_area_scan_float(
                payload.get("settle_s", self.MICROSCOPE_AREA_SCAN_DEFAULT_SETTLE_S),
                "settle_s",
                minimum=0.0,
                maximum=10.0,
            )
            tile_approach_mm = self._microscope_area_scan_float(
                payload.get(
                    "tile_approach_mm",
                    payload.get(
                        "approach_mm",
                        self.MICROSCOPE_AREA_SCAN_DEFAULT_TILE_APPROACH_MM,
                    ),
                ),
                "tile_approach_mm",
                minimum=0.0,
                maximum=self.MICROSCOPE_AREA_SCAN_MAX_TILE_APPROACH_MM,
            )
            flat_field_options = microscope_scan.flat_field_options_from_payload(
                payload,
                default_enabled=True,
            )
            camera_lock_settings = microscope_scan.camera_lock_settings_from_payload(
                payload,
                default_enabled=True,
            )
        except ValueError as exc:
            return {
                "accepted": False,
                "status_code": 400,
                "message": str(exc),
            }

        output_dir = str(payload.get("output_dir") or "").strip()
        if not output_dir:
            output_dir = self._microscope_area_scan_default_output_dir()
        pixels_to_mm = getattr(scale, "pixels_to_mm", None)
        if scan_pattern == "stitch_debug":
            if pixels_to_mm is None:
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": "Stitch debug scan requires full pixel-to-stage calibration.",
                }
        design_session = getattr(self, "_design_session", None)
        try:
            launch_snapshot = self._capture_microscope_scan_launch_snapshot(
                scale=scale,
                document=getattr(design_session, "document", None),
                registration=getattr(design_session, "registration", None),
            )
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": f"Microscope scan launch state is invalid: {exc}",
            }
        configuration = SimpleNamespace(
            output_dir=output_dir,
            overlap_fraction=overlap_fraction,
            settle_s=settle_s,
            tile_approach_mm=tile_approach_mm,
            scan_pattern=scan_pattern,
            refine_scale_from_overlaps=(scan_pattern != "stitch_debug"),
            structure_size_mm=structure_size_mm,
            placement_fraction=placement_fraction,
            flat_field_options=flat_field_options,
            camera_lock_settings=camera_lock_settings,
        )
        planning_request = MicroscopeAreaScanRequest(
            row_count=rows,
            column_count=columns,
            overlap_fraction=overlap_fraction,
            scan_pattern=scan_pattern,
            structure_size_mm=structure_size_mm,
            placement_fraction=placement_fraction,
        )
        self._microscope_scan_stop_requested.clear()
        thread = threading.Thread(
            target=self._run_microscope_scan,
            args=(configuration, planning_request, launch_snapshot),
            name="MicroscopeAreaScan",
            daemon=True,
        )
        self._microscope_scan_thread = thread
        try:
            thread.start()
        except Exception as exc:
            if self._microscope_scan_thread is thread:
                self._microscope_scan_thread = None
            self._update_stage_coordinate_apply_state()
            return {
                "accepted": False,
                "status_code": 500,
                "message": f"Microscope area scan could not start: {exc}",
            }
        self._update_stage_coordinate_apply_state()
        return {
            "accepted": True,
            "status_code": 202,
            "message": "Microscope area scan started.",
            "output_dir": output_dir,
            "rows": rows,
            "columns": columns,
            "pattern": scan_pattern,
        }

    @staticmethod
    def _microscope_area_scan_pattern(payload: dict[str, Any]) -> str:
        raw = payload.get("pattern", payload.get("scan_pattern", "grid"))
        text = str(raw or "grid").strip().lower().replace("-", "_")
        if text in {"grid", "area", "area_scan", "centered_area"}:
            return "grid"
        if text in {"stitch_debug", "stitching_debug", "debug_stitch"}:
            return "stitch_debug"
        raise ValueError("pattern must be 'grid' or 'stitch_debug'.")

    @classmethod
    def _microscope_area_scan_count(cls, value: object, label: str) -> int:
        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a positive integer.") from exc
        if count <= 0:
            raise ValueError(f"{label} must be a positive integer.")
        if count > cls.MICROSCOPE_AREA_SCAN_MAX_TILES:
            raise ValueError(
                f"{label} is too large; maximum is {cls.MICROSCOPE_AREA_SCAN_MAX_TILES}."
            )
        return count

    @staticmethod
    def _microscope_area_scan_float(
        value: object,
        label: str,
        *,
        minimum: float,
        maximum: float,
    ) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be numeric.") from exc
        if not math.isfinite(number) or number < minimum or number > maximum:
            raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}.")
        return number

    @staticmethod
    def _microscope_area_scan_default_output_dir() -> str:
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(root / "ProbeStationGUI" / "MicroscopeScans" / f"area_scan_{timestamp}")

    def _api_route_artifacts_payload(self) -> list[dict[str, object]]:
        return self._api_route_artifacts_store().public_payloads()

    def _api_route_artifacts_store(self) -> ApiRouteArtifactsStore:
        return ApiRouteArtifactsStore(artifacts=self._api_route_artifacts, lock=self._api_route_artifacts_lock)

    def _capture_api_route_photo_artifact(self, point: RouteMeasurementPoint, position: int, total: int, focus_result: object | None) -> str:
        before_counter = self._latest_camera_counter()
        frame, _counter = self._wait_for_camera_frame(after_counter=before_counter, timeout_s=2.0)
        photo = self._qimage_telegram_photo(frame) or self._latest_camera_frame_photo()
        if photo is None:
            raise RuntimeError("Camera frame is unavailable.")
        photo_bytes, photo_name = photo
        artifact_id = self._api_route_artifacts_store().add(data=photo_bytes, filename=photo_name, content_type=api_route_photo_content_type(photo_name), kind="route_photo", metadata=api_route_photo_artifact_metadata(point, position=position, total=total, contact_number=self._api_structure_number_for_measurement_point(point), focus_result=route_photo_focus_payload(focus_result)), created_at_utc=self._api_timestamp_utc())
        if self._route_telegram_adapter().consume_route_photo_request():
            self._send_telegram_bot_message(route_requested_photo_caption(position=int(position), total=int(total), structure_number=self._api_structure_number_for_measurement_point(point), label=point.label), photo=(photo_bytes, photo_name), reply_markup=self._telegram_default_markup())
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
        return api_contact_context(
            int(contact_number),
            serial_available=bool(
                self.serial_connection is not None and self.serial_connection.is_open
            ),
            route=self._design_session.route,
            registration=self._design_session.registration,
            points_factory=self._route_measurement_points,
            route_offset_xy=getattr(self, "_api_route_offset_xy", (0.0, 0.0)),
            structure_number_for_point=self._api_structure_number_for_measurement_point,
        )

    def _api_route_adjusted_stage_xy(
        self,
        point: RouteMeasurementPoint,
    ) -> tuple[float, float]:
        return api_route_adjusted_stage_xy(
            point,
            route_offset_xy=getattr(self, "_api_route_offset_xy", (0.0, 0.0)),
        )

    def _api_route_meter_configuration(
        self,
        payload: object,
        *,
        voltages_v: list[float] | None,
    ) -> RouteMeterConfiguration:
        settings_manager = getattr(self, "settings_manager", None)
        gwinstek_resource_name = None
        if settings_manager is not None:
            gwinstek_resource_name = (
                settings_manager.needle_calibration_configuration().visa_resource
            )
        return route_meter_configuration_from_payload(
            payload,
            voltages_v=voltages_v,
            current_meter_type=self.lcr_controller.meter_type(),
            default_gwinstek_resource_name=gwinstek_resource_name,
        )

    def _api_needle_feedrate(self, payload: dict[str, Any]) -> float | None:
        for key in ("needle_feedrate_mm_min", "feedrate_mm_min", "feedrate"):
            if key not in payload or payload.get(key) is None:
                continue
            value = payload_float(payload, key, default=math.nan, minimum=0.0)
            return max(self.MIN_FEEDRATE_MM_MIN, value)
        return float(
            self.settings_manager.needle_calibration_configuration().feedrate_mm_min
        )

    @staticmethod
    def _api_structure_number_for_measurement_point(
        point: RouteMeasurementPoint,
    ) -> int:
        return structure_number_from_labels(
            point.label,
            point.point_id,
            default=point.index,
        )

    @staticmethod
    def _api_structure_number_for_route_point(
        route_index: int,
        route_point: object,
    ) -> int:
        return structure_number_from_labels(
            getattr(route_point, "label", ""),
            getattr(route_point, "id", ""),
            default=route_index,
        )

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
            create_design_layout_window(self, design_layout_window_class)

    def _stage_serial_ready(self) -> bool:
        return bool(
            self.serial_connection is not None
            and getattr(self.serial_connection, "is_open", False)
        )

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
        with self._latest_camera_frame_condition:
            self._latest_raw_camera_frame = qimg.copy()
            self._latest_raw_camera_frame_counter = (
                int(getattr(self, "_latest_raw_camera_frame_counter", 0)) + 1
            )
            sequence = int(self._latest_raw_camera_frame_counter)
            self._latest_camera_frame_condition.notify_all()
        exposure_adapter = getattr(self, "_exposure_policy_adapter", None)
        now = time.monotonic()
        if (
            not qimg.isNull()
            and exposure_adapter is not None
            and not getattr(self, "_exposure_policy_start_in_flight", False)
            and not getattr(self, "_exposure_policy_started", False)
            and now >= getattr(self, "_exposure_policy_start_retry_after", 0.0)
        ):
            self._exposure_policy_start_in_flight = True
            exposure_adapter.request_start()
        objective = self.settings_manager.active_objective_configuration()
        request = LiveCameraCorrectionRequest(
            sequence=sequence,
            frame=qimg.copy(),
            objective_name=str(getattr(objective, "name", "") or ""),
            distortion_configured=bool(
                getattr(objective, "distortion_correction_configured", False)
            ),
            distortion_payload=getattr(objective, "distortion_correction", {}),
            suppress_gap_warning=bool(
                getattr(self, "_suppress_next_camera_ui_gap", False)
            ),
        )
        if not self._live_camera_frame_processor.submit(request):
            logger.debug("Live camera frame ignored during processor shutdown.")

    def _on_live_camera_frame_processed(
        self,
        result: LiveCameraCorrectionResult,
    ) -> None:
        now = time.monotonic()
        suppress_gap_warning = bool(result.suppress_gap_warning)
        if self._last_camera_frame_ui_timestamp is not None:
            frame_gap = now - self._last_camera_frame_ui_timestamp
            if (
                frame_gap > self.CAMERA_UI_FRAME_GAP_WARNING_S
                and not suppress_gap_warning
            ):
                logger.warning(
                    "Camera UI frame gap %.3fs before display update",
                    frame_gap,
                )
        self._last_camera_frame_ui_timestamp = now
        frame = result.frame
        with self._latest_camera_frame_condition:
            if int(result.sequence) <= int(self._latest_camera_frame_counter):
                return
            self._latest_camera_frame = frame.copy()
            self._latest_camera_frame_counter = int(result.sequence)
            self._latest_camera_frame_condition.notify_all()
        if suppress_gap_warning:
            self._suppress_next_camera_ui_gap = False
        self._latest_camera_frame_for_notifications = frame
        self.stage_controller.on_frame_ready(frame)
        self.view.set_frame(frame)

    def _on_live_camera_frame_processing_error(self, message: str) -> None:
        logger.warning("Live camera frame correction failed: %s", message)

    def _on_camera_frame_gap_suppressed(self) -> None:
        self._suppress_next_camera_ui_gap = True

    def _correct_camera_frame_for_active_objective(self, qimg: QImage) -> QImage:
        objective = self.settings_manager.active_objective_configuration()
        if not bool(getattr(objective, "distortion_correction_configured", False)):
            self._clear_distortion_correction_cache()
            return qimg
        payload = getattr(objective, "distortion_correction", {})
        try:
            correction = self._distortion_correction_for_objective(objective, payload)
            return apply_distortion_correction(qimg, correction)
        except ValueError as exc:
            logger.warning("Unable to apply lens distortion correction: %s", exc)
            self._clear_distortion_correction_cache()
            return qimg

    def _distortion_correction_for_objective(
        self,
        objective: object,
        payload: object,
    ) -> DistortionCorrection:
        signature = (
            str(getattr(objective, "name", "")),
            self._distortion_payload_signature(payload),
        )
        cached_signature = getattr(
            self,
            "_distortion_correction_cache_signature",
            None,
        )
        cached_model = getattr(self, "_distortion_correction_cache_model", None)
        if cached_signature == signature and cached_model is not None:
            return cached_model
        correction = correction_from_payload(payload)
        self._distortion_correction_cache_signature = signature
        self._distortion_correction_cache_model = correction
        return correction

    @staticmethod
    def _distortion_payload_signature(payload: object) -> str:
        try:
            return json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except (TypeError, ValueError):
            return repr(payload)

    def _clear_distortion_correction_cache(self) -> None:
        self._distortion_correction_cache_signature = None
        self._distortion_correction_cache_model = None

    def _latest_camera_counter(self) -> int:
        with self._latest_camera_frame_condition:
            return int(self._latest_camera_frame_counter)

    def _latest_raw_camera_counter(self) -> int:
        direct_counter = getattr(
            getattr(self, "grabber", None),
            "latest_frame_counter",
            None,
        )
        if callable(direct_counter):
            return int(direct_counter())
        with self._latest_camera_frame_condition:
            return int(getattr(self, "_latest_raw_camera_frame_counter", 0))

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

    def _wait_for_raw_camera_frame(
        self,
        *,
        after_counter: int | None = None,
        timeout_s: float = 2.0,
    ) -> tuple[QImage | None, int]:
        direct_wait = getattr(
            getattr(self, "grabber", None),
            "wait_for_frame",
            None,
        )
        if callable(direct_wait):
            return direct_wait(
                after_counter=after_counter,
                timeout_s=timeout_s,
            )
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._latest_camera_frame_condition:
            while True:
                frame = getattr(self, "_latest_raw_camera_frame", None)
                counter = int(getattr(self, "_latest_raw_camera_frame_counter", 0))
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

    def _capture_microscope_scan_launch_snapshot(
        self,
        *,
        scale: object,
        document: object | None,
        registration: object | None,
    ) -> _MicroscopeScanLaunchSnapshot:
        objective_name, magnification = self._active_objective_metadata()
        objective_offset = tuple(
            float(value) for value in self._active_objective_xy_offset()
        )
        if len(objective_offset) != 2 or not all(
            math.isfinite(value) for value in objective_offset
        ):
            raise ValueError("Active objective offset is invalid.")

        registration_matrix = None
        registration_offset = None
        if registration is not None and bool(getattr(registration, "valid", False)):
            matrix = getattr(registration, "matrix")
            offset_value = getattr(registration, "offset")
            registration_matrix = (
                (float(matrix[0][0]), float(matrix[0][1])),
                (float(matrix[1][0]), float(matrix[1][1])),
            )
            registration_offset = (
                float(offset_value[0]),
                float(offset_value[1]),
            )
            transform_values = (
                *registration_matrix[0],
                *registration_matrix[1],
                *registration_offset,
            )
            determinant = (
                registration_matrix[0][0] * registration_matrix[1][1]
                - registration_matrix[0][1] * registration_matrix[1][0]
            )
            if not all(math.isfinite(value) for value in transform_values):
                raise ValueError("Design registration is invalid.")
            if abs(determinant) < 1e-18:
                raise ValueError("Design registration is singular.")

        document_path = (
            getattr(document, "path", None) if document is not None else None
        )
        document_identity = str(document_path or "")
        scan_name = (
            Path(document_identity).stem
            if document_identity
            else microscope_scan.scan_name_from_document(None)
        )
        return _MicroscopeScanLaunchSnapshot(
            objective_name=str(objective_name),
            magnification=magnification,
            scan_name=scan_name,
            document_identity=document_identity,
            scale=scale,
            registration_matrix=registration_matrix,
            registration_offset=registration_offset,
            objective_xy_offset=(objective_offset[0], objective_offset[1]),
        )

    def _stage_position_for_image_metadata(
        self,
        *,
        stage_xy: tuple[float, float] | None = None,
    ) -> tuple[float, ...] | None:
        latest = self.stage_controller.latest_stage_position()
        if stage_xy is not None:
            x_mm = float(stage_xy[0])
            y_mm = float(stage_xy[1])
            if latest is not None:
                values = list(latest)
                if len(values) >= 2:
                    values[0] = x_mm
                    values[1] = y_mm
                    return tuple(float(value) for value in values)
            return (x_mm, y_mm)
        if latest is not None:
            return latest
        return None

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

    def _show_route_runtime_status(self, message: str, timeout_ms: int = 0) -> None:
        self._show_status(message, timeout_ms)
        self._route_runtime_presenter().set_status(message)

    def _show_route_dialog_status(self, message: str, timeout_ms: int = 0) -> None:
        self._show_status(message, timeout_ms)
        dialog = getattr(self, "_route_measurement_dialog", None)
        if dialog is not None:
            dialog.set_status(message)

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
        return offsets.objective_xy_offset(settings.objectives, settings.active_name)

    def _camera_stage_xy_from_raw_stage_xy(
        self, raw_stage_xy: tuple[float, float]
    ) -> tuple[float, float]:
        return offsets.raw_stage_to_camera_stage(raw_stage_xy, self._active_objective_xy_offset())

    def _raw_stage_xy_from_camera_stage_xy(
        self, camera_stage_xy: tuple[float, float]
    ) -> tuple[float, float]:
        return offsets.camera_stage_to_raw_stage(camera_stage_xy, self._active_objective_xy_offset())

    def _design_xy_from_raw_stage_xy(
        self, raw_stage_xy: tuple[float, float]
    ) -> tuple[float, float] | None:
        camera_stage_xy = self._camera_stage_xy_from_raw_stage_xy(raw_stage_xy)
        return self._design_session.design_from_stage(camera_stage_xy)

    def _raw_stage_xy_from_design_xy(
        self, design_xy: tuple[float, float]
    ) -> tuple[float, float] | None:
        camera_stage_xy = self._design_session.stage_from_design(design_xy)
        if camera_stage_xy is None:
            return None
        return self._raw_stage_xy_from_camera_stage_xy(
            (float(camera_stage_xy[0]), float(camera_stage_xy[1]))
        )

    def _on_stage_axis_escape_pressed(self, axis_name: str) -> None:
        panel = getattr(self, "_stage_position_panel", None)
        if panel is None:
            return
        axis = axis_name.strip().upper()
        panel.discard_return_commit(axis)
        panel.pop_pending_target(axis)
        panel.reset_axis_field(axis, self._stage_axis_display_values.get(axis))
        field = panel.field(axis)
        if field is not None:
            field.deselect()
            field.clearFocus()
        stage_position_panel_adapter.refresh_stage_axis_styles(self)
        self._update_stage_coordinate_apply_state()
        self.view.setFocus(Qt.OtherFocusReason)

    def _on_stage_coordinate_mode_changed(self) -> None:
        self._clear_exact_step_targets()
        if self._pending_stage_axis_targets and self._stage_position_panel is not None:
            self._stage_position_panel.clear_pending_target_state()
            stage_position_panel_adapter.update_stage_position_display(
                self,
                self.stage_controller.latest_stage_position(),
            )
            self._show_status("Cleared pending coordinate edits after input mode change.", 2000)
        self._update_stage_coordinate_apply_state()

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

    def _update_stage_coordinate_apply_state(self, _pending: bool | None = None) -> None:
        panel = getattr(self, "_stage_position_panel", None)
        if panel is None:
            return
        controller_busy = hasattr(self, "stage_controller") and self.stage_controller.is_busy()
        active = self._coordinate_targets.has_active_move() or controller_busy
        available = panel.has_pending_or_modified_fields()
        panel.set_action_buttons_enabled(
            available and not active,
            available or stage_move_lifecycle.has_cancelable_operation(self),
        )

    def _clear_pending_stage_coordinate_targets(self) -> bool:
        panel = getattr(self, "_stage_position_panel", None)
        if panel is None:
            return False
        had_changes = panel.clear_pending_targets(self._stage_axis_display_values)
        self._update_stage_coordinate_apply_state()
        return had_changes

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

    def _prime_keyboard_focus(self) -> None:
        if not self.isVisible():
            return
        self.raise_()
        self.activateWindow()
        self.view.setFocus(Qt.ActiveWindowFocusReason)

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        QTimer.singleShot(0, self._prime_keyboard_focus)

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

    def _on_alignment_draft_accepted(self, points: object) -> None:
        if self._pending_alignment_preparation is not None:
            self._show_status("Chip rotation is already in progress.", 4000)
            return
        if not isinstance(points, (list, tuple)):
            return
        try:
            normalized = tuple(
                (float(point[0]), float(point[1]))
                for point in points
                if isinstance(point, (list, tuple)) and len(point) == 2
            )
        except (TypeError, ValueError):
            return
        if len(normalized) < 2 or len(set(normalized)) < 2:
            self._show_status("Align requires at least two distinct design points.", 5000)
            return
        self._alignment_design_draft = normalized
        self._alignment_stage_draft = [None] * len(normalized)
        self._alignment_draft_fit_residuals = None
        self._manual_alignment_pick_slot = None
        design_layout_window = getattr(self, "design_layout_window", None)
        if design_layout_window is not None:
            design_layout_window.set_alignment_capture_points(normalized)
        self._set_alignment_panel_expanded()
        self._refresh_manual_alignment_ui()
        self._update_stage_coordinate_apply_state()
        self._show_status(
            f"Align: {len(normalized)} design points ready. Capture S1 next.",
            5000,
        )

    def _on_alignment_draft_discarded(self) -> None:
        self._show_status("Align draft discarded.", 2500)

    def _apply_settings(self, *, apply_objective_runtime: bool = True) -> None:
        self._clear_exact_step_targets()
        connection_flow.apply_axis_feedrate_limits(
            self,
            self.stage_controller.axis_max_feedrates(),
        )
        if self.joystick_panel:
            bindings = self.settings_manager.control_bindings()
            self.joystick_panel.apply_control_bindings(bindings)
            logger.debug("Joystick bindings reapplied from settings")
            connection_flow.apply_joystick_feedrate_preferences(self)
        jog = self.settings_manager.jog_configuration()
        self.stage_controller.set_motion_safety_disabled(jog.motion_safety_disabled)
        needle_settings = self.settings_manager.needle_calibration_configuration()
        oscillation_settings = self.settings_manager.oscillation_configuration()
        self.stage_controller.apply_axis_calibrations(
            self.settings_manager.axis_calibrations_configuration()
        )
        self.stage_controller.apply_precision_approach_configuration(
            self.settings_manager.precision_approach_configuration()
        )
        needle_calibration_ui.apply_needle_calibration_runtime(self, needle_settings)
        coordinate_settings = self.settings_manager.coordinate_system_configuration()
        self.stage_controller.apply_coordinate_system_configuration(
            position_mode=coordinate_settings.position_mode,
            startup_mode=coordinate_settings.startup_mode,
            preferred_system=coordinate_settings.preferred_system,
        )
        if apply_objective_runtime:
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

    def _apply_settings_from_dialog(self, new_settings: object) -> None:
        if not isinstance(new_settings, Settings):
            return
        settings_to_apply = new_settings.clone()
        active_objective_update_rejected = False
        objective_mutation_busy = self._objective_mutation_busy()
        if objective_mutation_busy:
            current_objectives = self.settings_manager.objectives_configuration()
            current_active_name = normalize_objective_name(
                current_objectives.active_name
            )
            submitted_objectives = settings_to_apply.objectives
            submitted_active_name = normalize_objective_name(
                submitted_objectives.active_name
            )
            current_active_profile = current_objectives.objectives.get(
                current_active_name
            )
            submitted_active_profile = submitted_objectives.objectives.get(
                current_active_name
            )
            active_objective_update_rejected = (
                submitted_active_name != current_active_name
                or submitted_active_profile != current_active_profile
            )
            if active_objective_update_rejected:
                submitted_objectives.active_name = current_active_name
                if current_active_profile is None:
                    submitted_objectives.objectives.pop(current_active_name, None)
                else:
                    submitted_objectives.objectives[current_active_name] = (
                        current_active_profile.clone()
                    )
        self.settings_manager.replace_and_save(
            settings_to_apply,
            preserve_exposure_policy=True,
        )
        if objective_mutation_busy:
            self._apply_settings(apply_objective_runtime=False)
        else:
            self._apply_settings()
        if active_objective_update_rejected:
            self._show_status(
                "Stage is busy; active objective settings not changed.",
                4000,
            )
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
        send_telegram_alert_for_settings(
            self.settings_manager.telegram_configuration(),
            alert_key,
            message,
            attach_photo=attach_photo,
            photo=photo,
            document_path=document_path,
            reply_markup=reply_markup,
            latest_camera_frame_photo=self._latest_camera_frame_photo,
        )

    def _send_telegram_bot_message(
        self,
        message: str,
        *,
        photo: tuple[bytes, str] | None = None,
        document_path: str | Path | None = None,
        reply_markup: object | None = None,
    ) -> bool:
        return send_telegram_bot_message_for_settings(
            self.settings_manager.telegram_configuration(),
            message,
            photo=photo,
            document_path=document_path,
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
        current_names = [str(combo.itemData(index) or "") for index in range(combo.count())]
        plan = alignment.objective_combo_sync_plan(
            current_names, self._objective_names(), objective_name
        )
        if plan.rebuild_items:
            combo.blockSignals(True)
            combo.clear()
            for name in plan.names:
                combo.addItem(name, name)
            combo.blockSignals(False)
        if plan.selected_index < 0:
            return
        combo.blockSignals(True)
        combo.setCurrentIndex(plan.selected_index)
        combo.blockSignals(False)
        if plan.refresh_calibration_ui:
            self._refresh_objective_calibration_ui()

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
        allow_stage_task: bool = False,
    ) -> None:
        plan = alignment.select_active_objective(
            self.settings_manager.settings,
            objective_name,
            is_busy=self._objective_mutation_busy(
                allow_stage_task=allow_stage_task,
            ),
            apply_motion=apply_motion,
        )
        if plan.refresh_calibration_ui:
            self._refresh_objective_calibration_ui()
            return
        if plan.restore_combo_name is not None:
            self._sync_objective_combo(plan.restore_combo_name)
        if not self._persist_objective_plan(plan, show_status=False):
            self._show_plan_status(plan)
            return
        if plan.apply_offset_motion:
            self._apply_objective_change_offset(plan.old_name, plan.new_name)
        self._show_plan_status(plan)

    def _objective_mutation_busy(
        self,
        *,
        allow_stage_task: bool = False,
        optical_context: OpticalCalibrationOutcome | None = None,
    ) -> bool:
        if self._api_stage_command_worker_active():
            return True
        if self._microscope_scan_running():
            return True
        optical_owner = isinstance(optical_context, OpticalCalibrationOutcome)
        if optical_calibration_blocks_mutation(
            self._optical_calibration_runtime.state(),
            outcome_owns_mutation=optical_owner,
        ):
            return True
        stage_controller = getattr(self, "stage_controller", None)
        return (
            stage_controller is not None
            and stage_controller.is_busy()
            and not allow_stage_task
        )

    def _objective_profile_mutation_busy(
        self,
        objective_name: str,
        *,
        allow_stage_task: bool = False,
        optical_context: OpticalCalibrationOutcome | None = None,
    ) -> bool:
        name = normalize_objective_name(objective_name)
        active_name = normalize_objective_name(
            self.settings_manager.settings.objectives.active_name
        )
        if not name or name != active_name:
            return False
        return self._objective_mutation_busy(
            allow_stage_task=allow_stage_task,
            optical_context=optical_context,
        )

    def _apply_objective_change_offset(self, old_name: str, new_name: str) -> None:
        plan = alignment.objective_change_offset_plan(
            self.settings_manager.objectives_configuration(),
            old_name,
            new_name,
            self.stage_controller.latest_stage_position(),
            self.stage_controller.is_busy(),
            display_axis_value_from_raw=(
                lambda axis, raw: stage_position_panel_adapter.display_axis_value_from_raw(
                    self,
                    axis,
                    raw,
                )
            ),
            raw_axis_value_from_display=(
                lambda axis, display: stage_position_panel_adapter.raw_axis_value_from_display(
                    self,
                    axis,
                    display,
                )
            ),
        )
        if plan.status:
            self._show_plan_status(plan)
            return
        if plan.raw_targets is None:
            return
        accepted = self.stage_controller.request_absolute_axis_targets_move(
            plan.raw_targets,
            feedrate=self._current_linear_feedrate(),
            allow_unhomed=False,
        )
        status = plan.accepted_status if accepted else plan.rejected_status
        if status:
            self._show_status(status, plan.status_timeout_ms)

    def _apply_objective_settings(self) -> None:
        objective_settings = self.settings_manager.objectives_configuration()
        active_objective, objectives = alignment.active_objective_configuration(
            objective_settings
        )
        self.stage_controller.apply_objective_configuration(active_objective, objectives)
        self._sync_objective_combo(objective_settings.active_name)
        self._refresh_objective_calibration_ui()
        if self._design_session.document is not None:
            self._refresh_design_position()

    def _show_plan_status(self, plan) -> None:
        if plan.status:
            self._show_status(plan.status, plan.status_timeout_ms)

    def _persist_objective_plan(
        self,
        plan,
        *,
        show_status: bool = True,
        apply_objective_runtime: bool = True,
    ) -> bool:
        if plan.settings is None:
            if show_status:
                self._show_plan_status(plan)
            return False
        self.settings_manager.replace_and_save(
            plan.settings,
            preserve_exposure_policy=True,
        )
        if getattr(plan, "apply_settings", False) and apply_objective_runtime:
            self._apply_objective_settings()
        if getattr(plan, "refresh_design_position", False):
            self._refresh_design_position()
        if getattr(plan, "refresh_calibration_ui", False):
            self._refresh_objective_calibration_ui()
        if show_status:
            self._show_plan_status(plan)
        return True

    def _show_click_calibration_dialog(self) -> None:
        if self._click_calibration_dialog is None:
            dialog = ClickCalibrationDialog(self)
            dialog.objective_selected.connect(
                lambda name: self._set_active_objective(name, apply_motion=True)
            )
            dialog.reset_requested.connect(self._reset_click_calibration)
            dialog.add_requested.connect(self._add_objective_profile)
            dialog.delete_requested.connect(self._delete_objective_profile)
            dialog.offset_reference_requested.connect(self._set_objective_offset_reference)
            dialog.offset_save_requested.connect(self._save_active_objective_offset)
            dialog.offset_reset_requested.connect(self._reset_active_objective_offset)
            self._click_calibration_dialog = dialog
        self._refresh_click_calibration_ui()
        self._click_calibration_dialog.show()
        self._click_calibration_dialog.raise_()
        self._click_calibration_dialog.activateWindow()

    def _show_optical_calibration_wizard(
        self,
        mode: OpticalCalibrationMode | None = None,
    ) -> None:
        if self._optical_calibration_wizard is None:
            wizard = OpticalCalibrationWizard(self)
            wizard.start_flat_field_requested.connect(
                self._start_flat_field_calibration_from_wizard
            )
            wizard.start_lens_distortion_requested.connect(
                self._start_lens_distortion_calibration_from_wizard
            )
            wizard.cancel_requested.connect(self._cancel_optical_calibration_wizard)
            self._optical_calibration_wizard = wizard

        if self._optical_calibration_runtime.state().parent_session_token is not None:
            self._show_status("Optical calibration is still active.", 5000)
            self._optical_calibration_wizard.show()
            self._optical_calibration_wizard.raise_()
            self._optical_calibration_wizard.activateWindow()
            return

        objectives = self.settings_manager.objectives_configuration()
        active_name = normalize_objective_name(objectives.active_name)
        profile = objectives.objectives.get(active_name)
        flat_field_configured = False
        if active_name:
            try:
                flat_field_configured = (
                    self._flat_field_calibration_store.current_manifest_path(
                        active_name
                    ).is_file()
                )
            except (OSError, ValueError):
                flat_field_configured = False
        self._optical_calibration_wizard.set_objective(
            active_name,
            flat_field_configured=flat_field_configured,
            lens_configured=bool(
                profile is not None
                and profile.distortion_correction_configured
            ),
        )
        if not self._optical_calibration_wizard.prepare(mode):
            self._show_status("Optical calibration is already running.", 4000)
        self._optical_calibration_wizard.show()
        self._optical_calibration_wizard.raise_()
        self._optical_calibration_wizard.activateWindow()

    def _show_lens_distortion_dialog(self) -> None:
        if self._lens_distortion_dialog is None:
            dialog = LensDistortionDialog(self)
            dialog.calibrate_requested.connect(
                lambda: self._show_optical_calibration_wizard(
                    OpticalCalibrationMode.LENS_DISTORTION
                )
            )
            dialog.reset_requested.connect(self._reset_lens_distortion_calibration)
            self._lens_distortion_dialog = dialog
        self._refresh_lens_distortion_ui()
        self._lens_distortion_dialog.show()
        self._lens_distortion_dialog.raise_()
        self._lens_distortion_dialog.activateWindow()

    def _add_objective_profile(self) -> None:
        if self._objective_mutation_busy():
            self._show_status("Stage is busy; objective not added.", 4000)
            return
        raw_name, accepted = QInputDialog.getText(self, "Add Objective", "Objective name")
        if not accepted:
            return
        if self._objective_mutation_busy():
            self._show_status("Stage is busy; objective not added.", 4000)
            return
        plan = alignment.profile_add_plan(self.settings_manager.settings, raw_name)
        if plan.select_existing_name is not None:
            self._set_active_objective(plan.select_existing_name, apply_motion=True)
            self._show_plan_status(plan)
            return
        self._persist_objective_plan(plan)

    def _delete_objective_profile(self, objective_name: str) -> None:
        if self._objective_mutation_busy():
            self._show_status("Stage is busy; objective not deleted.", 4000)
            self._refresh_click_calibration_ui()
            return
        name = normalize_objective_name(objective_name)
        if not name:
            return
        preflight = alignment.profile_delete_plan(
            self.settings_manager.settings, name, confirmed=False
        )
        if preflight.status:
            self._show_plan_status(preflight)
            return
        response = QMessageBox.question(
            self, "Delete Objective", f"Delete objective profile {name}?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if response != QMessageBox.Yes:
            return
        if self._objective_mutation_busy():
            self._show_status("Stage is busy; objective not deleted.", 4000)
            self._refresh_click_calibration_ui()
            return
        plan = alignment.profile_delete_plan(
            self.settings_manager.settings, name, confirmed=True
        )
        self._persist_objective_plan(plan)

    def _set_objective_offset_reference(self) -> None:
        if self._objective_mutation_busy():
            self._show_status("Stage is busy; objective offset reference not set.", 4000)
            return
        raw_stage_xy = self._resolve_alignment_capture_stage_position()
        if raw_stage_xy is None:
            return
        plan = alignment.objective_offset_reference_plan(
            self.settings_manager.settings, raw_stage_xy
        )
        if plan.settings is not None:
            self._persist_objective_plan(plan, show_status=False)
        if plan.reference is not None:
            self._objective_offset_reference = plan.reference
            self._refresh_click_calibration_ui()
        elif plan.refresh_calibration_ui:
            self._refresh_click_calibration_ui()
        self._show_plan_status(plan)

    def _save_active_objective_offset(self) -> None:
        if self._objective_mutation_busy():
            self._show_status("Stage is busy; objective offset not saved.", 4000)
            return
        if self._objective_offset_reference is None:
            self._show_status("Set an objective offset reference first.", 5000)
            return
        raw_stage_xy = self._resolve_alignment_capture_stage_position()
        if raw_stage_xy is None:
            return
        plan = alignment.save_active_objective_offset(
            self.settings_manager.settings, self._objective_offset_reference, raw_stage_xy
        )
        self._persist_objective_plan(plan)

    def _reset_active_objective_offset(self) -> None:
        if self._objective_mutation_busy():
            self._show_status("Stage is busy; objective offset not reset.", 4000)
            return
        plan = alignment.reset_active_objective_offset(self.settings_manager.settings)
        if plan.settings is None:
            return
        self._persist_objective_plan(plan)

    def _refresh_click_calibration_ui(self) -> None:
        click_action = getattr(self, "_click_calibration_action", None)
        if click_action is not None:
            click_action.setText("Click-to-Move Calibration")
        click_dialog = getattr(self, "_click_calibration_dialog", None)
        if click_dialog is not None:
            click_dialog.set_objectives(
                self.settings_manager.objectives_configuration()
            )

    def _refresh_lens_distortion_ui(self) -> None:
        lens_action = getattr(self, "_lens_distortion_calibration_action", None)
        if lens_action is not None:
            lens_action.setText("Lens Distortion Calibration")
        lens_dialog = getattr(self, "_lens_distortion_dialog", None)
        if lens_dialog is not None:
            lens_dialog.set_objectives(
                self.settings_manager.objectives_configuration()
            )

    def _refresh_objective_calibration_ui(self) -> None:
        self._refresh_click_calibration_ui()
        self._refresh_lens_distortion_ui()

    def _start_flat_field_calibration_from_wizard(self) -> None:
        wizard = self._optical_calibration_wizard
        if wizard is None:
            return
        run_id = wizard.active_run_id()
        full_wizard = wizard.mode() is OpticalCalibrationMode.FULL
        if not self._optical_calibration_objective_matches_wizard(wizard):
            wizard.set_flat_field_result(False, "Active objective changed. Reopen optical calibration.", run_id=run_id)
            return
        if self._start_flat_field_calibration(wizard_run_id=run_id, full_wizard=full_wizard):
            return
        if full_wizard:
            self._cancel_optical_calibration_wizard(run_id)
        wizard.set_flat_field_result(False, "Flat-field calibration did not start.", run_id=run_id)

    def _cancel_optical_calibration_wizard(self, _run_id: object = None) -> None:
        wizard = self._optical_calibration_wizard
        if (
            _run_id is not None
            and (wizard is None or wizard.active_run_id() != _run_id)
        ):
            return
        self._optical_calibration_runtime.cancel()
        self._stop_lens_distortion_dialog()

    def _stop_lens_distortion_dialog(self) -> None:
        dialog = self._lens_distortion_dialog
        if dialog is not None:
            dialog.set_running(False)
            dialog.set_status("Lens distortion calibration stopped.")

    def _start_lens_distortion_calibration_from_wizard(self) -> None:
        wizard = self._optical_calibration_wizard
        if wizard is None:
            return
        run_id = wizard.active_run_id()
        full_wizard = wizard.mode() is OpticalCalibrationMode.FULL
        if not self._optical_calibration_objective_matches_wizard(wizard):
            if full_wizard:
                self._cancel_optical_calibration_wizard(run_id)
            wizard.set_lens_distortion_result(False, "Active objective changed. Reopen optical calibration.", run_id=run_id)
            return
        state = self._optical_calibration_runtime.state()
        parent_token = state.parent_session_token if full_wizard else None
        if full_wizard and parent_token is None:
            wizard.set_lens_distortion_result(False, "Optical calibration exposure session is unavailable.", run_id=run_id)
            return
        result = self._start_lens_distortion_calibration(
            wizard_run_id=run_id,
            parent_session_token=parent_token,
            full_wizard=full_wizard,
        )
        if bool(result["accepted"]):
            return
        if full_wizard:
            self._cancel_optical_calibration_wizard(run_id)
        wizard.set_lens_distortion_result(False, str(result["message"]), run_id=run_id)

    def _optical_calibration_objective_matches_wizard(
        self,
        wizard: OpticalCalibrationWizard,
    ) -> bool:
        current_name, _magnification = self._active_objective_metadata()
        return normalize_objective_name(current_name) == normalize_objective_name(
            wizard.objective_text()
        )

    def _start_flat_field_calibration(
        self,
        *,
        wizard_run_id: int | None = None,
        full_wizard: bool = False,
    ) -> bool:
        unavailable = self._optical_calibration_preflight("flat")
        if unavailable:
            self._show_status(unavailable, 5000)
            return False
        try:
            data = self._optical_calibration_request_adapter.capture()
        except RuntimeError as exc:
            self._show_status(str(exc), 5000)
            return False
        request = FlatFieldCalibrationRequest(
            run_id=uuid.uuid4().hex,
            wizard_run_id=wizard_run_id,
            objective_name=data.objective_name,
            magnification=data.magnification,
            pixels_to_mm=data.pixels_to_mm,
            pixel_size_mm=data.pixel_size_mm,
            linear_feedrate=self._coordinate_feedrate_for_axes(("X", "Y")),
            needle_feedrate=self._current_needle_feedrate(),
            full_wizard=bool(full_wizard),
            grid_size=self.FLAT_FIELD_CAPTURE_GRID_SIZE,
            overlap_fraction=self.FLAT_FIELD_CAPTURE_OVERLAP_FRACTION,
            settle_s=self.FLAT_FIELD_CAPTURE_SETTLE_S,
            camera_timeout_s=self.FLAT_FIELD_CAMERA_TIMEOUT_S,
        )
        decision = self._optical_calibration_runtime.start_flat(request)
        self._show_status(decision.message, 4000 if decision.accepted else 8000)
        return decision.accepted

    def _on_flat_field_calibration_progress(
        self,
        event: object,
        message: str,
    ) -> None:
        if not isinstance(event, OpticalCalibrationProgress):
            return
        wizard = getattr(self, "_optical_calibration_wizard", None)
        if wizard is not None and event.wizard_run_id is not None:
            wizard.set_progress(str(message), run_id=event.wizard_run_id)

    def _on_flat_field_calibration_finished(
        self,
        outcome: object,
        success: bool,
        message: str,
        _payload: object,
    ) -> None:
        if not isinstance(outcome, OpticalCalibrationOutcome):
            return
        if not self._optical_calibration_runtime.consume(
            outcome,
            blocked=self._microscope_scan_running(),
        ):
            return
        wizard = getattr(self, "_optical_calibration_wizard", None)
        if wizard is not None and outcome.wizard_run_id is not None:
            wizard.set_flat_field_result(
                bool(success), str(message), run_id=outcome.wizard_run_id
            )
        self._show_status(str(message), 10000 if success else 8000)
    def _start_lens_distortion_calibration(
        self,
        *,
        wizard_run_id: int | None = None,
        parent_session_token: str | None = None,
        full_wizard: bool = False,
    ) -> dict[str, object]:
        unavailable = self._optical_calibration_preflight("lens")
        if unavailable:
            self._show_status(unavailable, 5000)
            return {"accepted": False, "status_code": 409, "message": unavailable}
        try:
            data = self._optical_calibration_request_adapter.capture()
        except RuntimeError as exc:
            message = str(exc)
            self._show_status(message, 5000)
            return {"accepted": False, "status_code": 409, "message": message}
        request = LensDistortionCalibrationRequest(
            run_id=uuid.uuid4().hex,
            wizard_run_id=wizard_run_id,
            objective_name=data.objective_name,
            magnification=data.magnification,
            pixels_to_mm=data.pixels_to_mm,
            pixel_size_mm=data.pixel_size_mm,
            linear_feedrate=self._coordinate_feedrate_for_axes(("X", "Y")),
            needle_feedrate=self._current_needle_feedrate(),
            full_wizard=bool(full_wizard),
            parent_session_token=parent_session_token,
            grid_size=self.LENS_DISTORTION_CAPTURE_GRID_SIZE,
            fov_fraction=self.LENS_DISTORTION_FOV_FRACTION,
            settle_s=self.LENS_DISTORTION_CAPTURE_SETTLE_S,
            camera_timeout_s=self.LENS_DISTORTION_CAMERA_TIMEOUT_S,
            fit_limits=self._lens_fit_limits(),
        )
        decision = self._optical_calibration_runtime.start_lens(request)
        if decision.accepted and self._lens_distortion_dialog is not None:
            self._lens_distortion_dialog.set_running(True)
            self._lens_distortion_dialog.set_status(decision.message)
        self._show_status(decision.message, 4000 if decision.accepted else 8000)
        return {
            "accepted": decision.accepted,
            "status_code": decision.status_code,
            "message": decision.message,
        }

    def _optical_calibration_preflight(self, kind: str) -> str:
        return optical_calibration_preflight_message(
            kind,
            self._optical_calibration_runtime.state(),
            stage_ready=self._stage_serial_ready(),
            stage_busy=self._stage_serial_ready() and self.stage_controller.is_busy(),
        )

    def _lens_fit_limits(self) -> LensFitLimits:
        return LensFitLimits(
            cluster_tolerance_px=self.LENS_DISTORTION_CLUSTER_TOLERANCE_PX,
            min_feature_count=self.LENS_DISTORTION_MIN_FEATURE_COUNT,
            min_observation_count=self.LENS_DISTORTION_MIN_OBSERVATION_COUNT,
            max_residual_mean_px=self.LENS_DISTORTION_MAX_RESIDUAL_MEAN_PX,
            max_residual_max_px=self.LENS_DISTORTION_MAX_RESIDUAL_MAX_PX,
        )
    def _reset_lens_distortion_calibration(self) -> tuple[bool, str]:
        active_name = normalize_objective_name(
            self.settings_manager.settings.objectives.active_name
        )
        if self._objective_profile_mutation_busy(active_name):
            message = (
                "Lens correction cannot be reset while a scan or calibration is active."
            )
            self._show_status(message, 5000)
            return False, message
        try:
            self._save_active_objective_distortion(None)
        except Exception as exc:
            logger.exception("Unable to reset lens distortion correction")
            message = f"Lens correction reset failed: {exc}"
            self._show_status(message, 8000)
            return False, message
        message = "Lens correction cleared. Recalibrate click-to-move."
        self._show_status(message, 8000)
        return True, message

    def _on_lens_distortion_calibration_progress(
        self,
        event: object,
        message: str,
    ) -> None:
        if not isinstance(event, OpticalCalibrationProgress):
            return
        wizard = getattr(self, "_optical_calibration_wizard", None)
        if wizard is not None and event.wizard_run_id is not None:
            wizard.set_progress(str(message), run_id=event.wizard_run_id)

    def _on_lens_distortion_calibration_finished(
        self,
        outcome: object,
        success: bool,
        message: str,
        artifact: object,
    ) -> None:
        if not isinstance(outcome, OpticalCalibrationOutcome):
            return
        if not self._optical_calibration_runtime.consume(
            outcome,
            blocked=self._microscope_scan_running(),
            on_discarded=self._stop_lens_distortion_dialog,
        ):
            return
        presentation = prepare_lens_completion(
            outcome,
            bool(success),
            str(message),
            artifact,
            limits=self._lens_fit_limits(),
            save=lambda payload, objective_name, context: (
                self._save_objective_distortion(
                    payload,
                    objective_name,
                    optical_context=context,
                    allow_stage_task=True,
                )
            ),
        )
        if self._lens_distortion_dialog is not None:
            self._lens_distortion_dialog.set_running(False)
            self._lens_distortion_dialog.set_status(presentation.message)
        wizard = getattr(self, "_optical_calibration_wizard", None)
        if wizard is not None and outcome.wizard_run_id is not None:
            wizard.set_lens_distortion_result(
                presentation.success,
                presentation.message,
                run_id=outcome.wizard_run_id,
                **presentation.wizard_kwargs(),
            )
        self._show_status(
            presentation.message,
            10000 if presentation.success else 8000,
        )
    def _save_active_objective_distortion(
        self,
        payload: object | None,
        *,
        optical_context: OpticalCalibrationOutcome | None = None,
        allow_stage_task: bool = False,
    ) -> None:
        active_name = normalize_objective_name(
            self.settings_manager.settings.objectives.active_name
        )
        self._save_objective_distortion(
            payload,
            active_name,
            optical_context=optical_context,
            allow_stage_task=allow_stage_task,
        )

    def _save_objective_distortion(
        self,
        payload: object | None,
        objective_name: str,
        *,
        optical_context: OpticalCalibrationOutcome | None = None,
        allow_stage_task: bool = False,
    ) -> None:
        settings = self.settings_manager.settings.clone()
        objectives = settings.objectives
        objective_name = normalize_objective_name(objective_name)
        if not objective_name:
            raise RuntimeError("No active objective selected.")
        if (
            optical_context is not None
            and not isinstance(optical_context, OpticalCalibrationOutcome)
        ):
            raise RuntimeError(
                "Optical calibration result was canceled or is no longer current."
            )
        if self._objective_profile_mutation_busy(
            objective_name,
            allow_stage_task=allow_stage_task,
            optical_context=optical_context,
        ):
            raise RuntimeError(
                "Active objective correction cannot change while a scan or "
                "calibration is active."
            )
        profile = objectives.objectives.get(objective_name)
        if profile is None:
            raise RuntimeError(f"Objective profile {objective_name} is missing.")

        updated = profile.clone()
        if payload is None:
            updated.distortion_correction = {}
            updated.distortion_correction_configured = False
        elif isinstance(payload, dict):
            updated.distortion_correction = dict(payload)
            updated.distortion_correction_configured = True
            calibrated_matrix = self._calibrated_pixels_to_mm_from_distortion_payload(
                payload
            )
            if calibrated_matrix:
                updated.pixels_to_mm = calibrated_matrix
                updated.xy_calibration_configured = True
            else:
                updated.pixels_to_mm = []
                updated.xy_calibration_configured = False
        else:
            raise RuntimeError("Invalid lens correction payload.")
        objectives.objectives[objective_name] = updated
        self.settings_manager.replace_and_save(
            settings,
            preserve_exposure_policy=True,
        )
        self._apply_objective_settings()
        self._refresh_objective_calibration_ui()

    @staticmethod
    def _calibrated_pixels_to_mm_from_distortion_payload(
        payload: dict[str, object],
    ) -> list[list[float]]:
        return parse_pixels_to_mm_matrix(payload.get("calibrated_pixels_to_mm"))

    @staticmethod
    def _lens_distortion_payload_invalidates_click_calibration(payload: object) -> bool:
        if not isinstance(payload, dict):
            return False
        return not bool(Main._calibrated_pixels_to_mm_from_distortion_payload(payload))

    @staticmethod
    def _append_click_recalibration_message(message: str) -> str:
        suffix = "Recalibrate click-to-move."
        text = str(message)
        if suffix in text:
            return text
        if not text:
            return suffix
        return f"{text} {suffix}"

    def _on_objective_calibration_updated(
        self,
        objective_name: str,
        pixels_to_mm: object,
        task_token: object,
    ) -> None:
        reset_callback = (
            isinstance(task_token, StageTaskToken)
            and task_token.source == "click_calibration_reset"
        )
        expected_sources = (
            {"click_calibration_reset"}
            if reset_callback
            else {
                "_run_move",
                "click_calibration",
                "_run_clicked_point_resolution",
            }
        )
        if not self._calibration_callback_token_is_current(
            task_token,
            expected_sources=expected_sources,
        ):
            self._reject_objective_calibration_candidate(task_token)
            message = (
                "Click-to-move calibration result ignored because its stage task "
                "is no longer current."
            )
            logger.warning(message)
            self._show_status(message, 7000)
            return
        objective_name = normalize_objective_name(objective_name)
        if self._objective_profile_mutation_busy(
            objective_name,
            allow_stage_task=True,
        ):
            self._reject_objective_calibration_candidate(task_token)
            message = (
                "Click-to-move calibration result ignored because a scan or "
                "calibration is active."
            )
            logger.warning(message)
            self._show_status(message, 7000)
            return
        pixels_to_mm = self._objective_pixels_to_mm_for_calibration_update(
            objective_name,
            pixels_to_mm,
        )
        plan = alignment.update_objective_calibration(
            self.settings_manager.settings, objective_name, pixels_to_mm
        )
        if plan.settings is None:
            self._reject_objective_calibration_candidate(task_token)
            return
        if reset_callback:
            self._persist_objective_plan(plan)
            return

        accept = getattr(
            self.stage_controller,
            "accept_objective_calibration_candidate",
            None,
        )
        accepted = False
        if callable(accept):
            try:
                accepted = bool(accept(task_token, pixels_to_mm))
            except Exception:
                logger.exception("Unable to accept click calibration candidate")
        if not accepted:
            self._reject_objective_calibration_candidate(task_token)
            self._show_status(
                "Click-to-move calibration was cancelled before it could be applied.",
                7000,
            )
            return

        previous_settings = self.settings_manager.settings.clone()
        try:
            persisted = self._persist_objective_plan(
                plan,
                apply_objective_runtime=False,
            )
        except Exception as exc:
            logger.exception("Unable to persist accepted click calibration")
            self._reject_objective_calibration_candidate(task_token)
            message = (
                "Click-to-move calibration could not be saved: "
                f"{exc}"
            )
            try:
                self.settings_manager.replace_and_save(
                    previous_settings,
                    preserve_exposure_policy=True,
                )
            except Exception as restore_exc:
                logger.exception(
                    "Unable to restore settings after click calibration save failure"
                )
                message = f"{message} Settings restore failed: {restore_exc}"
            self._show_status(message, 7000)
            return
        if not persisted:
            self._reject_objective_calibration_candidate(task_token)
            return

        publish = getattr(
            self.stage_controller,
            "publish_objective_calibration_candidate",
            None,
        )
        published = False
        if callable(publish):
            try:
                published = bool(publish(task_token))
            except Exception:
                logger.exception("Unable to publish click calibration candidate")
        if published:
            return

        self._reject_objective_calibration_candidate(task_token)
        try:
            self.settings_manager.replace_and_save(
                previous_settings,
                preserve_exposure_policy=True,
            )
        except Exception as exc:
            logger.exception("Unable to roll back rejected click calibration settings")
            message = (
                "Click-to-move calibration was cancelled before it could be applied. "
                f"Settings restore failed: {exc}"
            )
        else:
            message = "Click-to-move calibration was cancelled before it could be applied."
        self._show_status(message, 7000)

    def _reject_objective_calibration_candidate(self, task_token: object) -> None:
        reject = getattr(
            getattr(self, "stage_controller", None),
            "reject_objective_calibration_candidate",
            None,
        )
        if not callable(reject):
            return
        try:
            reject(task_token)
        except Exception:
            logger.exception("Unable to reject click calibration candidate")

    def _calibration_callback_token_is_current(
        self,
        task_token: object,
        *,
        expected_sources: set[str] | frozenset[str] | None = None,
    ) -> bool:
        if not isinstance(task_token, StageTaskToken):
            return False
        if expected_sources is not None and task_token.source not in expected_sources:
            return False
        stage_controller = getattr(self, "stage_controller", None)
        validator = getattr(
            stage_controller,
            "is_calibration_task_token_current",
            None,
        )
        if not callable(validator):
            return False
        try:
            return bool(validator(task_token))
        except Exception:
            logger.exception("Unable to validate stage calibration callback token")
            return False

    def _objective_pixels_to_mm_for_calibration_update(
        self,
        objective_name: str,
        pixels_to_mm: object,
    ) -> object:
        if not parse_pixels_to_mm_matrix(pixels_to_mm):
            return pixels_to_mm
        settings = self.settings_manager.settings
        name = normalize_objective_name(objective_name)
        profile = settings.objectives.objectives.get(name)
        if profile is None or not bool(
            getattr(profile, "distortion_correction_configured", False)
        ):
            return pixels_to_mm
        payload = getattr(profile, "distortion_correction", {})
        if not isinstance(payload, dict):
            return pixels_to_mm
        calibrated = self._calibrated_pixels_to_mm_from_distortion_payload(payload)
        return calibrated or pixels_to_mm

    def _on_objective_mismatch_detected(
        self,
        suggested_name: str,
        message: str,
        task_token: object,
    ) -> None:
        if not self._calibration_callback_token_is_current(
            task_token,
            expected_sources={"_run_move", "click_calibration_check"},
        ):
            stale_message = (
                "Objective mismatch ignored because its calibration task is no "
                "longer current."
            )
            logger.warning(stale_message)
            self._show_status(stale_message, 7000)
            return
        name = normalize_objective_name(suggested_name)
        objective_settings = self.settings_manager.objectives_configuration()
        if name in objective_settings.objectives:
            self._set_active_objective(
                name,
                apply_motion=False,
                allow_stage_task=True,
            )
        if message:
            self._show_status(message, 7000)

    def _design_backed_alignment_active(self) -> bool:
        return len(getattr(self, "_alignment_design_draft", ())) >= 2

    def _alignment_capture_slot_count(self) -> int:
        if self._design_backed_alignment_active():
            return len(self._alignment_design_draft)
        return 2

    def _design_window_is_open(self) -> bool:
        return self.design_layout_window is not None and self.design_layout_window.isVisible()

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
        if not 0 <= slot < self._alignment_capture_slot_count():
            return
        if getattr(self, "_manual_alignment_capture_context", None) is not None:
            self._show_status("Alignment point capture is already running.", 4000)
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
        context = getattr(self, "_manual_alignment_capture_context", None)
        if self._manual_alignment_pick_slot is None and context is None:
            return
        if isinstance(context, _ManualAlignmentCaptureContext):
            context.cancelled.set()
            self.stage_controller.cancel_clicked_point_resolution(
                context.request_id,
                "Alignment point capture cancelled."
            )
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
            self._alignment_stage_draft = [None] * len(
                self._alignment_design_draft
            )
            self._alignment_draft_fit_residuals = None
            self._set_design_snap_enabled(True)
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

    def _resolve_alignment_capture_stage_position(self) -> tuple[float, float] | None:
        try:
            stage_position = self.stage_controller.current_stage_position()
        except Exception as exc:
            plan = alignment.alignment_capture_position_plan(
                error_message=str(exc),
                latest_position=self.stage_controller.latest_stage_position(),
            )
        else:
            plan = alignment.alignment_capture_position_plan(stage_position=stage_position)
        if plan.request_status_refresh:
            self.stage_controller.request_status_refresh()
        if plan.status:
            self._show_status(plan.status, plan.status_timeout_ms)
        return plan.stage_xy

    def _capture_manual_alignment_center(self, slot: int) -> None:
        if not 0 <= slot < self._alignment_capture_slot_count():
            return
        if getattr(self, "_manual_alignment_capture_context", None) is not None:
            self._show_status("Alignment point capture is already running.", 4000)
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
            self._clear_exact_step_targets()
            self._invalidate_design_registration(
                "Design registration cleared after B-axis zeroing."
            )
            self.stage_controller.zero_b_axis()
        except Exception as exc:
            self._show_status(str(exc), 5000)

    def _reset_click_calibration(self) -> tuple[bool, str]:
        active_name = normalize_objective_name(
            self.settings_manager.settings.objectives.active_name
        )
        if self._objective_profile_mutation_busy(active_name):
            message = (
                "Click-to-move calibration cannot be reset while a scan or "
                "calibration is active."
            )
            self._refresh_click_calibration_ui()
            self._show_status(message, 5000)
            return False, message
        message = (
            "Click-to-move calibration cleared. Click in the microscope view "
            "to recalibrate the active objective."
        )
        try:
            self.stage_controller.reset_calibration(message)
        except Exception as exc:
            error = str(exc)
            self._refresh_click_calibration_ui()
            self._show_status(error, 5000)
            return False, error
        return True, message

    def _capture_manual_alignment_clicked(
        self, dx_pixels: float = 0.0, dy_pixels: float = 0.0
    ) -> None:
        slot = self._manual_alignment_pick_slot
        if slot is None:
            return
        if getattr(self, "_manual_alignment_capture_context", None) is not None:
            self._show_status("Alignment point capture is already running.", 4000)
            return
        context = _ManualAlignmentCaptureContext(
            request_id=uuid.uuid4().hex,
            slot=slot,
            cancelled=threading.Event(),
        )
        self._manual_alignment_capture_context = context
        try:
            accepted = self.stage_controller.request_clicked_point_resolution(
                context.request_id,
                dx_pixels,
                dy_pixels,
            )
        except Exception as exc:
            accepted = False
            message = str(exc) or type(exc).__name__
        else:
            message = "Stage is busy; alignment point capture not started."
        if not accepted:
            if self._manual_alignment_capture_context is context:
                self._manual_alignment_capture_context = None
            self._refresh_manual_alignment_ui()
            self._update_stage_coordinate_apply_state()
            self._show_status(message, 5000)
            return
        self._refresh_manual_alignment_ui()
        self._update_stage_coordinate_apply_state()

    def _on_manual_alignment_point_resolved(
        self,
        request_id: object,
        success: bool,
        center_xy: object,
        captured_xy: object,
        message: str,
    ) -> None:
        context = getattr(self, "_manual_alignment_capture_context", None)
        if (
            not isinstance(context, _ManualAlignmentCaptureContext)
            or request_id != context.request_id
        ):
            return
        self._manual_alignment_capture_context = None
        if context.cancelled.is_set() or self._manual_alignment_pick_slot != context.slot:
            self._refresh_manual_alignment_ui()
            self._update_stage_coordinate_apply_state()
            return
        if not success:
            self._refresh_manual_alignment_ui()
            self._update_stage_coordinate_apply_state()
            self._show_status(
                str(message) or "Alignment point capture failed.",
                5000,
            )
            return
        try:
            center = (float(center_xy[0]), float(center_xy[1]))
            captured = (float(captured_xy[0]), float(captured_xy[1]))
        except (IndexError, TypeError, ValueError):
            self._refresh_manual_alignment_ui()
            self._update_stage_coordinate_apply_state()
            self._show_status("Alignment point capture returned invalid coordinates.", 5000)
            return
        self._update_coordinate_display(center_xy=center, cursor_xy=captured)
        self._capture_manual_alignment_point(context.slot, captured, source="image")

    def _capture_manual_alignment_point(
        self, slot: int, captured: tuple[float, float], *, source: str
    ) -> None:
        if not 0 <= slot < self._alignment_capture_slot_count():
            return
        self._manual_alignment_pick_slot = None
        self._refresh_manual_alignment_ui()
        self._update_stage_coordinate_apply_state()

        if self._design_backed_alignment_active():
            registration_stage_xy = self._camera_stage_xy_from_raw_stage_xy(captured)
            self._pending_alignment_preparation = None
            required_pair_count = len(self._alignment_design_draft)
            if len(self._alignment_stage_draft) != required_pair_count:
                self._alignment_stage_draft = [None] * required_pair_count
            self._alignment_stage_draft[slot] = registration_stage_xy
            pair_count = sum(
                point is not None for point in self._alignment_stage_draft
            )
            preparation = None
            preparation_error = None
            spacing_reasonable = True
            if pair_count == required_pair_count:
                try:
                    preparation = self._design_session.prepare_alignment_draft(
                        self._alignment_design_draft,
                        tuple(
                            point
                            for point in self._alignment_stage_draft
                            if point is not None
                        ),
                    )
                except DesignModelError as exc:
                    preparation_error = str(exc)
                else:
                    self._alignment_draft_fit_residuals = (
                        preparation.rms_residual_mm,
                        preparation.max_residual_mm,
                    )
                    spacing_reasonable = self._design_spacing_ratio_is_reasonable(
                        preparation.distance_ratio
                    )
            plan = alignment.design_alignment_capture_plan(
                slot=slot,
                stage_xy=registration_stage_xy,
                source=source,
                pair_count=pair_count,
                required_pair_count=required_pair_count,
                preparation=preparation,
                preparation_error=preparation_error,
                spacing_reasonable=spacing_reasonable,
            )
            self._apply_alignment_capture_plan(plan)
            return

        plan = alignment.manual_alignment_capture_plan(
            slot, captured,
            source=source,
            manual_points=self._manual_alignment_points,
            target_angles=self.ALIGNMENT_TARGET_ANGLES,
        )
        self._apply_alignment_capture_plan(plan)

    def _apply_alignment_capture_plan(self, plan) -> None:
        if plan.points is not None:
            self._manual_alignment_points = plan.points
        if plan.apply_prepared_alignment and plan.preparation is not None:
            self._design_session.apply_prepared_alignment(plan.preparation)
            self._finish_alignment_draft()
        if plan.pending_preparation is not None:
            self._pending_alignment_preparation = plan.pending_preparation
        if plan.pending_quick_alignment_rotation:
            self._pending_quick_alignment_rotation = True
        if plan.invalidate_design_registration:
            self._invalidate_design_registration(
                "Design registration cleared after B-axis rotation."
            )
        for enabled, callback in (
            (plan.disable_snap, lambda: self._set_design_snap_enabled(False)),
            (plan.refresh_manual_ui, self._refresh_manual_alignment_ui),
            (plan.refresh_design_panel, self._refresh_design_panel),
            (plan.refresh_design_position, self._refresh_design_position),
            (plan.expand_alignment, self._set_alignment_panel_expanded),
            (plan.collapse_alignment_if_ready, self._collapse_alignment_panel_if_ready),
            (plan.collapse_alignment_if_design_open, self._collapse_alignment_panel_if_design_open),
        ):
            if enabled:
                callback()
        if plan.status:
            self._show_status(plan.status, plan.status_timeout_ms)
        if plan.request_b_rotation and plan.rotation_deg is not None:
            self.stage_controller.request_rotate_b(plan.rotation_deg)

    def _on_alignment_b_rotation_started(self) -> None:
        if self._pending_alignment_preparation is None:
            return
        self._design_session.invalidate_registration(
            "Design registration stale after B-axis rotation started."
        )
        self._refresh_design_panel()
        self._refresh_design_position()

    def _finish_alignment_draft(self) -> None:
        self._alignment_design_draft = ()
        self._alignment_stage_draft = []
        self._alignment_draft_fit_residuals = None
        design_layout_window = getattr(self, "design_layout_window", None)
        if design_layout_window is not None:
            design_layout_window.set_alignment_capture_points(())

    def _refresh_manual_alignment_ui(self) -> None:
        presentation = alignment.alignment_presentation(
            design_backed=self._design_backed_alignment_active(),
            design_stage_marks=self._alignment_stage_draft,
            manual_points=self._manual_alignment_points,
            pick_slot=self._manual_alignment_pick_slot,
            required_design_mark_count=len(self._alignment_design_draft),
        )
        if self.alignment_panel is not None:
            self.alignment_panel.set_design_marks(self._alignment_design_draft)
            self.alignment_panel.set_captured_points(presentation.captured_points)
            self.alignment_panel.set_pick_slot(presentation.pick_slot)
            self.alignment_panel.set_capture_running(
                getattr(self, "_manual_alignment_capture_context", None) is not None
            )
            self.alignment_panel.set_registration_status(
                self._design_session.registration_status
            )
            if self._alignment_draft_fit_residuals is not None:
                self.alignment_panel.set_fit_residuals(
                    *self._alignment_draft_fit_residuals
                )
        self._microscope_interaction.set_alignment_mode(presentation.alignment_mode)
        self._microscope_interaction.set_alignment_instruction(presentation.instruction)

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
        show_serial_terminal_tool_window(self)

    def _on_manual_motion_axis(self, axis: str) -> None:
        axis_name = axis.upper()
        stage_position_panel_adapter.set_stage_motion_axes(self, {axis_name})
        if axis_name == "B":
            self._invalidate_design_registration(
                "Design registration cleared after manual B-axis motion."
            )

    def _on_manual_jog_command_changed(
        self, commanded_distances: object, feedrate: float
    ) -> None:
        if not isinstance(commanded_distances, tuple):
            return
        self._clear_exact_step_targets()
        self._clear_planned_move_prediction(clear_wait_state=True)
        if self.serial_terminal_panel is not None:
            self.serial_terminal_panel.set_live_poll_paused(True)
        result = self._manual_jog_prediction.handle_command(
            commanded_distances,
            feedrate=feedrate,
            now=time.monotonic(),
            coordinate_move_active=self._coordinate_targets.has_active_move(),
            coordinate_move_stage_position=self._coordinate_targets.stage_position,
            latest_stage_position=self.stage_controller.latest_stage_position(),
            current_design_stage_xy=self._current_design_stage_xy,
        )
        if result.zero_distance:
            logger.debug("MOTION PREDICTION stop_requested command=%s", commanded_distances)
            self._manual_jog_timer.stop()
            stage_position_panel_adapter.clear_stage_motion_axes(self)
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
            return
        if result.clear_coordinate_move_tracking:
            logger.debug(
                "Coordinate move tracking cleared after manual jog command: %s",
                commanded_distances,
            )
            stage_move_lifecycle.clear_coordinate_move_tracking(
                self,
                clear_pending=False,
                reset_override=False,
            )
        stage_position_panel_adapter.set_stage_motion_axes(
            self,
            set(result.motion_axes),
        )
        prediction = self._manual_jog_prediction
        design_stage_xy = (
            self._design_xy_from_raw_stage_xy(prediction.stage_xy)
            if prediction.stage_xy is not None
            else None
        )
        velocity_x, velocity_y = prediction.velocity_xy or (0.0, 0.0)
        logger.debug(
            "MOTION PREDICTION start stage=%s design=%s velocity=(%.4f, %.4f) feedrate=%.3f command=%s source=%s",
            self._format_optional_point(prediction.stage_xy),
            self._format_optional_point(design_stage_xy),
            velocity_x,
            velocity_y,
            float(feedrate),
            commanded_distances,
            result.stage_source,
        )
        if result.publish_position is not None:
            stage_position_update.publish_stage_position_estimate(
                self,
                result.publish_position,
            )
        if result.start_timer and not self._manual_jog_timer.isActive():
            self._manual_jog_timer.start()

    def _on_manual_jog_stopped(self) -> None:
        prediction = self._manual_jog_prediction
        result = prediction.handle_stop(
            now=time.monotonic(),
            last_status_timestamp=self.stage_controller.last_status_timestamp(),
        )
        design_stage_xy = (
            self._design_xy_from_raw_stage_xy(prediction.stage_xy)
            if prediction.stage_xy is not None
            else None
        )
        logger.debug(
            "MOTION PREDICTION stop_requested stage=%s design=%s stop_tail_s=%.4f",
            self._format_optional_point(prediction.stage_xy),
            self._format_optional_point(design_stage_xy),
            result.stop_tail_s,
        )
        if result.start_timer and not self._manual_jog_timer.isActive():
            self._manual_jog_timer.start()
        if result.stop_timer:
            self._manual_jog_timer.stop()
        if self.serial_terminal_panel is not None and result.resume_live_poll:
            QTimer.singleShot(
                self.TERMINAL_RESUME_AFTER_JOG_MS,
                lambda: self.serial_terminal_panel
                and self.serial_terminal_panel.set_live_poll_paused(False),
            )
        if result.schedule_status_refreshes:
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
        self.settings_manager.replace_and_save(
            settings,
            preserve_exposure_policy=True,
        )

    def _save_jog_control_mode(self, mode: str) -> None:
        control_mode = str(mode).strip().lower()
        if control_mode not in {"jog", "step"}:
            return
        if control_mode != "step":
            self._clear_exact_step_targets()
        settings = self.settings_manager.settings.clone()
        if settings.jog.mode == control_mode:
            return
        settings.jog.mode = control_mode
        self.settings_manager.replace_and_save(
            settings,
            preserve_exposure_policy=True,
        )

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
        self.settings_manager.replace_and_save(
            settings,
            preserve_exposure_policy=True,
        )

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
        """Accumulate exact Step targets and route them through coordinate moves."""

        axis = axis.strip().upper()
        if axis not in self.STAGE_AXIS_NAMES:
            return
        mode = mode.strip().upper()
        if mode not in {"G90", "G91"}:
            self._show_status(f"Unsupported manual move mode: {mode}.", 3000)
            return
        if self.stage_controller.is_busy() and not self._coordinate_targets.has_active_move():
            self._show_status("Stage is busy. Ignoring manual axis move.", 3000)
            return
        baseline = self._coordinate_targets.display_targets.get(axis)
        if baseline is None:
            baseline = self._stage_axis_display_values.get(axis)
        if baseline is None:
            self._show_status(f"{axis} coordinate is unavailable.", 3000)
            return
        current_pending = self._exact_step_accumulator.targets.get(axis)
        try:
            display_target = (
                float(value_mm)
                if mode == "G90"
                else float(
                    current_pending if current_pending is not None else baseline
                )
                + float(value_mm)
            )
        except (TypeError, ValueError):
            self._show_status(f"Invalid {axis} target coordinate.", 3000)
            return
        raw_target = self._raw_target_from_display_value(axis, display_target)
        if raw_target is None:
            self._show_status(f"{axis} coordinate is unavailable.", 3000)
            return
        limit_error = self._stage_axis_target_limit_error(axis, display_target)
        if limit_error is not None:
            self._show_status(limit_error, 4000)
            return
        self._exact_step_accumulator.set_absolute(axis, display_target)
        self._exact_step_pending_axes.add(axis)
        self._pending_stage_axis_targets[axis] = (float(raw_target), display_target)
        stage_position_panel_adapter.refresh_stage_axis_styles(self)
        self._update_stage_coordinate_apply_state()
        if (
            not self._exact_step_timer.isActive()
            and not self._exact_step_window_elapsed
        ):
            self._exact_step_timer.start()

    def _on_exact_step_window_elapsed(self) -> None:
        self._exact_step_window_elapsed = True
        self._dispatch_exact_step_targets()

    def _dispatch_exact_step_targets(self) -> bool:
        if not self._exact_step_window_elapsed:
            return False
        if self._coordinate_targets.has_active_move() or self.stage_controller.is_busy():
            return False
        display_targets = dict(self._exact_step_accumulator.targets)
        if not display_targets:
            self._exact_step_window_elapsed = False
            return False
        targets: dict[str, tuple[float, float]] = {}
        for axis, display_target in display_targets.items():
            raw_target = self._raw_target_from_display_value(axis, display_target)
            if raw_target is None:
                self._show_status(f"{axis} coordinate is unavailable.", 3000)
                self._clear_exact_step_targets()
                return False
            limit_error = self._stage_axis_target_limit_error(axis, display_target)
            if limit_error is not None:
                self._show_status(limit_error, 4000)
                self._clear_exact_step_targets()
                return False
            targets[axis] = (float(raw_target), float(display_target))
        self._exact_step_accumulator.drain()
        self._exact_step_window_elapsed = False
        accepted = self._start_coordinate_targets_move(
            targets,
            feedrate_mm_min=self._coordinate_feedrate_for_axes(targets),
            source_label="Step",
        )
        if accepted:
            self._exact_step_pending_axes.difference_update(targets)
        else:
            self._clear_exact_step_targets()
        return accepted

    def _on_coordinate_move_finished(
        self,
        success: bool,
        finished_display_targets: dict[str, float],
    ) -> None:
        if not success:
            self._clear_exact_step_targets()
            return
        accumulator = getattr(self, "_exact_step_accumulator", None)
        if accumulator is None:
            return
        for axis, reached_target in finished_display_targets.items():
            pending_target = accumulator.targets.get(axis)
            if pending_target is None or not math.isclose(
                pending_target,
                reached_target,
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                continue
            accumulator.targets.pop(axis, None)
            self._exact_step_pending_axes.discard(axis)
            self._pending_stage_axis_targets.pop(axis, None)
        stage_position_panel_adapter.refresh_stage_axis_styles(self)
        self._update_stage_coordinate_apply_state()
        if not self._exact_step_timer.isActive():
            self._dispatch_exact_step_targets()

    def _clear_exact_step_targets(self) -> None:
        timer = getattr(self, "_exact_step_timer", None)
        if timer is not None:
            timer.stop()
        accumulator = getattr(self, "_exact_step_accumulator", None)
        if accumulator is not None:
            accumulator.clear()
        for axis in getattr(self, "_exact_step_pending_axes", set()):
            self._pending_stage_axis_targets.pop(axis, None)
        self._exact_step_pending_axes = set()
        self._exact_step_window_elapsed = False
        if (
            getattr(self, "_stage_position_panel", None) is not None
            and hasattr(self, "_stage_motion_axes")
            and hasattr(self, "_stage_motion_blink_dimmed")
        ):
            stage_position_panel_adapter.refresh_stage_axis_styles(self)
            self._update_stage_coordinate_apply_state()

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
            self.settings_manager.replace_and_save(
                settings,
                preserve_exposure_policy=True,
            )
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
        self.settings_manager.replace_and_save(
            settings,
            preserve_exposure_policy=True,
        )

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
        if self._manual_jog_prediction.prediction_active():
            self._advance_manual_jog_prediction()
            return
        if self._coordinate_targets.started_at is not None:
            self._advance_coordinate_move_prediction()
            return
        if self._planned_move_started_at is not None:
            self._advance_planned_move_prediction()
            return
        self._manual_jog_timer.stop()

    def _advance_manual_jog_prediction(self) -> None:
        now = time.monotonic()
        result = self._manual_jog_prediction.advance(
            now=now,
            coordinate_move_stage_position=self._coordinate_targets.stage_position,
            latest_stage_position=self.stage_controller.latest_stage_position(),
            current_design_stage_xy=self._current_design_stage_xy,
        )
        if result.log_tick:
            design_stage_xy = (
                self._design_xy_from_raw_stage_xy(self._manual_jog_prediction.stage_xy)
                if self._manual_jog_prediction.stage_xy is not None
                else None
            )
            logger.debug(
                "MOTION PREDICTION tick stage=%s design=%s dt=%.4f velocity=(%.4f, %.4f)",
                self._format_optional_point(self._manual_jog_prediction.stage_xy),
                self._format_optional_point(design_stage_xy),
                result.dt,
                result.velocity_xy[0],
                result.velocity_xy[1],
            )
        if result.publish_position is not None:
            stage_position_update.publish_stage_position_estimate(
                self,
                result.publish_position,
            )
        if result.stop_timer:
            self._manual_jog_timer.stop()

    def _advance_coordinate_move_prediction(self) -> None:
        decision = self._coordinate_targets.advance_prediction(
            monotonic_s=time.monotonic(),
        )
        if decision.clear_tracking:
            stage_move_lifecycle.clear_coordinate_move_tracking(
                self,
                clear_pending=False,
                reset_override=True,
            )
            return
        if decision.publish_position is not None:
            stage_position_update.publish_stage_position_estimate(
                self,
                decision.publish_position,
            )

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
        stage_position_update.publish_stage_position_estimate(
            self,
            stage_position_update.position_with_stage_xy(
                self,
                self._planned_move_stage_xy,
            ),
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
        origin_stage_xy = stage_position_update.preferred_design_stage_xy(self)
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
        self._manual_jog_prediction.clear_waiting_status()
        self._planned_move_origin_xy = origin_stage_xy
        self._planned_move_stage_xy = origin_stage_xy
        self._planned_move_target_xy = target_stage_xy
        self._planned_move_started_at = started_at
        self._planned_move_ends_at = started_at + max(duration_s, 0.05)
        self._planned_move_waiting_for_fresh_status = False
        self._planned_move_stop_status_timestamp = None
        stage_position_panel_adapter.set_stage_motion_axes(self, {"X", "Y"})
        logger.debug(
            "MOTION PREDICTION planned_move_start source=%s origin=%s target=%s distance=%.4f duration=%.4f feedrate=%.3f",
            source_label,
            self._format_optional_point(origin_stage_xy),
            self._format_optional_point(target_stage_xy),
            distance_mm,
            duration_s,
            feedrate,
        )
        stage_position_update.publish_stage_position_estimate(
            self,
            stage_position_update.position_with_stage_xy(self, origin_stage_xy),
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
        self._clear_exact_step_targets()
        self.stage_controller.invalidate_needles_state()
        self.stage_controller.invalidate_coordinate_confidence(
            "Manual controller command."
        )
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
        self._start_design_document_load(design_path, restore_state=None, show_window=True)

    def _start_design_document_load(
        self, design_path: str, *, restore_state: dict[str, object] | None, show_window: bool
    ) -> None:
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
                document = DesignDocument.load(path_text)
            except Exception as exc:
                self.design_document_loaded.emit(generation, None, exc)
                return
            self.design_document_loaded.emit(generation, document, None)

        threading.Thread(target=load_design, name="DesignDocumentLoad", daemon=True).start()

    def _on_design_document_loaded(self, generation: int, document: object, error: object) -> None:
        if generation != self._design_load_generation:
            return
        restore_state = self._design_load_restore_states.pop(generation, None)
        show_window = self._design_load_show_window.pop(generation, True)
        candidate_session = self._snapshot_design_session()
        previous_markup = getattr(self, "_design_markup", None)
        try:
            plan = design_navigation.design_document_loaded_plan(
                candidate_session,
                document,
                error,
                restore_state,
            )
        except DesignModelError as exc:
            self._set_design_load_pending_ui(False)
            self._show_status(str(exc), 6000)
            if restore_state is not None:
                connection_flow.save_controller_state_without_design(self)
            return
        if not plan.accepted:
            self._set_design_load_pending_ui(False)
            self._apply_design_load_failure_plan(plan, show_window)
            return
        if plan.document is not None:
            self._begin_design_markup_load(
                plan.document,
                generation=generation,
                candidate_session=candidate_session,
                plan=plan,
                show_window=show_window,
                previous_markup=previous_markup,
            )

    def _snapshot_design_session(self) -> DesignSession:
        session = self._design_session
        return DesignSession(
            document=session.document,
            registration=session.registration,
            source_design_marks=list(session.source_design_marks),
            source_stage_marks=list(session.source_stage_marks),
            check_design_marks=list(session.check_design_marks),
            check_stage_marks=list(session.check_stage_marks),
            targets=list(session.targets),
            selected_target_index=session.selected_target_index,
            route=session.route,
            selected_route_point_index=session.selected_route_point_index,
            registration_status=session.registration_status,
        )

    def _apply_design_load_success_plan(self, plan: design_navigation.DesignLoadResultPlan, show_window: bool) -> None:
        self._reset_manual_alignment(cancel_pick=True)
        self._pending_alignment_preparation = None
        self._last_selected_design_point = plan.last_selected_design_point
        self._set_design_snap_enabled(True)
        if plan.document_directory is not None:
            self.settings_manager.set_design_last_directory(plan.document_directory)
            if self.design_navigator_panel is not None:
                self.design_navigator_panel.set_design_dialog_directory(plan.document_directory)
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

    def _apply_design_load_failure_plan(self, plan: design_navigation.DesignLoadResultPlan, show_window: bool) -> None:
        message = plan.status_message or ""
        self._show_status(message, plan.status_timeout_ms)
        if self.design_layout_window is not None and show_window:
            self.design_layout_window.set_status_message(message)
        elif self.design_navigator_panel:
            self.design_navigator_panel.set_status_message(message)
        if plan.clear_cached_design:
            connection_flow.save_controller_state_without_design(self)

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
        self._design_markup_request_id = int(
            getattr(self, "_design_markup_request_id", 0)
        ) + 1
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
        self._design_session = context.session
        self._design_markup = markup
        self._design_markup_direct_guide_ids = []
        self._design_markup_pending_visibility = None
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
        message_box.setText(
            f"'{source_path.name}' changed since its Markup was saved."
        )
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
        if (
            failure.operation == "load"
            and failure.request_id == getattr(self, "_design_markup_load_request_id", None)
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
        current_ids = {
            guide.id for guide in markup.guides
        } if markup is not None else set()
        direct_ids[:] = [
            guide_id for guide_id in direct_ids if guide_id in current_ids
        ]
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

    def _show_navigation_status(self, plan: object) -> None:
        message = getattr(plan, "status_message", None)
        if message is not None:
            self._show_status(message, getattr(plan, "status_timeout_ms", 5000))

    def _apply_route_edit_plan(self, plan: design_navigation.RouteEditPlan, *, empty_selection: bool = False, update_selection: bool = True) -> bool:
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
        plan = design_navigation.unload_design_document(self._design_session)
        if not plan.accepted:
            return
        if loaded_document is not None:
            self._delete_persisted_design_markup(loaded_document.path)
        self._design_markup_load_request_id = None
        getattr(self, "_design_markup_load_contexts", {}).clear()
        self._design_markup = None
        self._design_markup_direct_guide_ids = []
        self._design_markup_pending_visibility = None
        self._reset_manual_alignment(cancel_pick=True)
        self._pending_alignment_preparation = None
        self._last_selected_design_point = None
        self._refresh_design_panel()
        self._update_design_position(None)
        self._show_navigation_status(plan)

    def _set_design_top_cell(self, top_cell_name: str) -> None:
        if not self._design_mutation_ready():
            return
        try:
            plan = design_navigation.set_design_top_cell(self._design_session, top_cell_name)
        except DesignModelError as exc:
            self._show_status(str(exc), 6000)
            return
        self._pending_alignment_preparation = None
        self._last_selected_design_point = None
        self._refresh_design_panel()
        self._refresh_design_position()
        self._show_navigation_status(plan)

    def _set_design_layer_visibility(self, layer: int, datatype: int, visible: bool) -> None:
        if not self._design_mutation_ready():
            return
        try:
            plan = design_navigation.set_design_layer_visibility(
                self._design_session, layer, datatype, visible
            )
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        if not plan.accepted:
            return
        self._refresh_design_panel()

    def _rotate_design_document(self, quarter_turn_delta: int) -> None:
        if not self._design_mutation_ready():
            return
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
        if route_measurement_thread is not None and route_measurement_thread.is_alive():
            self._show_status("Stop route measurement before rotating the design.", 5000)
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
        try:
            plan = design_navigation.rotate_design_document(
                self._design_session,
                delta,
                self._last_selected_design_point,
                can_rotate=True,
            )
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._design_markup = rotated_markup
        self._last_selected_design_point = plan.last_selected_design_point
        self._pending_alignment_preparation = None
        self._refresh_design_panel()
        self._refresh_design_position()
        if rotated_markup != markup:
            self._publish_design_markup()
        self._show_navigation_status(plan)

    def _create_measurement_route(self) -> None:
        if not self._design_mutation_ready():
            return
        try:
            plan = design_navigation.create_measurement_route(self._design_session)
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
            plan = design_navigation.load_measurement_route(self._design_session, route_path)
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
            plan = design_navigation.save_measurement_route(self._design_session)
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._apply_route_edit_plan(plan, update_selection=False)

    def _save_measurement_route_as(self, route_path: str) -> None:
        if not self._design_mutation_ready():
            return
        try:
            plan = design_navigation.save_measurement_route(self._design_session, route_path)
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._apply_route_edit_plan(plan, update_selection=False)

    def _add_design_route_point(self, x_value: float, y_value: float) -> None:
        if not self._design_edit_safe():
            self._show_status("Design editing is locked.", 4000)
            return
        try:
            plan = design_navigation.add_design_route_point(self._design_session, x_value, y_value)
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._apply_route_edit_plan(plan)

    def _design_edit_safe(self) -> bool:
        if self._design_session.document is None:
            return False
        if bool(getattr(self, "_design_load_pending", False)):
            return False
        route_thread = getattr(self, "_route_measurement_thread", None)
        return not bool(route_thread is not None and route_thread.is_alive())

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
        self._design_markup = markup.remove_ids(
            {guide.id for guide in markup.guides}
        )
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
        markup = getattr(self, "_design_markup", None)
        entities = project_entities(self._design_session.route, markup)
        plan = plan_mixed_delete(
            entities,
            window.selection.ids,
            edit_safe=True,
        )
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
        markup = getattr(self, "_design_markup", None)
        entities = project_entities(self._design_session.route, markup)
        plan = plan_mixed_array(
            entities,
            request.source_ids,
            request,
            route=self._design_session.route,
            edit_safe=True,
        )
        self._commit_mixed_design_edit(
            plan,
            selection_after=SelectionModel(request.source_ids),
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
        route_plan = design_navigation.apply_route_entity_changes(
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
        self, origin_x: float, origin_y: float, step_x_dx: float, step_x_dy: float,
        count_x: int, step_y_dx: float, step_y_dy: float, count_y: int,
        serpentine: bool, replace_existing: bool, selected_indices: object = None,
    ) -> None:
        if not self._design_edit_safe():
            self._show_status("Design editing is locked.", 4000)
            return
        try:
            plan = design_navigation.add_route_array_points(
                self._design_session, origin_x, origin_y, step_x_dx, step_x_dy,
                count_x, step_y_dx, step_y_dy, count_y, serpentine, replace_existing,
                selected_indices,
            )
        except DesignModelError as exc:
            self._show_status(str(exc), 5000)
            return
        self._apply_route_edit_plan(plan)

    def _remove_selected_route_point(self) -> None:
        if not self._design_mutation_ready():
            return
        plan = design_navigation.remove_selected_route_point(self._design_session)
        self._apply_route_edit_plan(plan)

    def _clear_measurement_route_points(self) -> None:
        if not self._design_mutation_ready():
            return
        plan = design_navigation.clear_measurement_route_points(self._design_session)
        self._apply_route_edit_plan(plan, empty_selection=True)

    def _open_route_measurement_dialog(self, *, start_context: bool = True) -> None:
        route = self._design_session.route
        if route is None or not route.points:
            self._show_status("Create or load a probe route before measuring.", 5000)
            return
        thread = self._route_measurement_thread
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
            thread_active=bool(thread is not None and thread.is_alive()),
            waiting=self._route_measurement_waiting,
            handlers=route_dialog_handlers(self),
        )
        dialog = self._route_measurement_dialog
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
        plan = route_dialog_restore_plan(
            self._route_measurement_settings_store().load(),
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
        thread = self._route_measurement_thread
        runner = self._route_measurement_runner
        if runner is not None and thread is not None and thread.is_alive():
            self._route_measurement_context_close_requested = True
            runner.stop()

    def _start_route_measurement_session(self) -> None:
        thread = self._route_measurement_thread
        configuration = (
            self._route_measurement_dialog.current_configuration()
            if self._route_measurement_dialog is not None
            else None
        )
        plan = route_measurement_session_start_plan(
            thread_active=bool(thread is not None and thread.is_alive()),
            dialog_configuration=configuration,
            current_point=self._route_measurement_current_point,
        )
        if not plan.accepted:
            self._show_status(plan.status_message, plan.status_timeout_ms)
            return
        self._route_measurement_session_active = plan.session_active
        self._set_route_measurement_resume_point(plan.point_number)
        self._set_route_measurement_pending(plan.pending)
        self._save_route_measurement_session_metadata(configuration)
        self._show_route_dialog_status(plan.status_message, plan.status_timeout_ms)

    def _cancel_route_measurement_session(self) -> None:
        thread = self._route_measurement_thread
        plan = route_measurement_session_cancel_plan(
            thread_active=bool(thread is not None and thread.is_alive())
        )
        if not plan.accepted:
            self._show_status(plan.status_message, plan.status_timeout_ms)
            return
        self._pending_route_measure_point = None
        self._route_measurement_session_active = plan.session_active
        self._set_route_measurement_resume_point(plan.point_number)
        self._set_route_measurement_pending(plan.pending)
        self._show_route_dialog_status(plan.status_message, plan.status_timeout_ms)

    def _start_route_measurement(
        self,
        configuration: RouteMeasurementRunConfiguration,
        *,
        wait_before_first_point: bool = False,
    ) -> None:
        thread = self._route_measurement_thread
        availability = gui_route_start_availability(
            route_thread_active=thread is not None and thread.is_alive(),
            serial_connected=self._stage_serial_ready(),
        )
        if not self._apply_gui_route_start_preflight(availability):
            return
        start_plan = self._route_measurement_start_plan(configuration)
        if start_plan is None:
            return
        points = start_plan.points
        selected_point = start_plan.selected_point
        self._prepare_route_measurement_launch(configuration, selected_point)
        launch_state = gui_route_launch_state(
            operation_mode=configuration.operation_mode,
            points=points,
            selected_point=selected_point,
            wait_before_first_point=wait_before_first_point,
            previous_ok_skipped_count=start_plan.previous_ok_skipped_count,
        )
        if not self._route_measurement_photo_preflight(
            launch_state,
            photo_autofocus_enabled=configuration.photo_autofocus_enabled,
            wait_before_first_point=wait_before_first_point,
        ):
            return
        route_lcr_controller = setup_gui_route_meter(
            controller=self.lcr_controller,
            configuration=configuration.meter,
            measure_enabled=launch_state.measure_enabled,
            show_status=self._show_status,
            presenter=self._route_runtime_presenter(),
        )
        if route_lcr_controller is None:
            return
        runner = self._build_route_measurement_runner(
            configuration,
            points=points,
            route_lcr_controller=route_lcr_controller,
            wait_before_first_point=wait_before_first_point,
        )
        self._start_route_measurement_runner(
            runner,
            launch_state,
            configuration=configuration,
            point_count=len(points),
        )

    def _prepare_route_measurement_launch(
        self,
        configuration: RouteMeasurementRunConfiguration,
        selected_point: RouteMeasurementPoint,
    ) -> None:
        self._set_route_measurement_resume_point(int(selected_point.index))
        if not self._route_measurement_session_active:
            self._route_measurement_session_active = True
            self._set_route_measurement_pending(True)
        self._route_measurement_runtime_configuration = configuration
        self._save_route_measurement_session_metadata(configuration)

    def _apply_gui_route_start_preflight(
        self,
        preflight: GuiRouteStartPreflight,
    ) -> bool:
        if preflight.accepted:
            return True
        self._show_status(preflight.message, preflight.timeout_ms)
        if preflight.telegram_failure_text:
            self._send_telegram_alert(
                "route_failed",
                preflight.telegram_failure_text,
                attach_photo=preflight.attach_failure_photo,
            )
        if preflight.dialog_status:
            self._route_runtime_presenter().set_status(preflight.message)
        return False

    def _route_measurement_photo_preflight(
        self,
        launch_state: GuiRouteLaunchState,
        *,
        photo_autofocus_enabled: bool,
        wait_before_first_point: bool,
    ) -> bool:
        scale = self._active_microscope_scale()
        preflight = gui_route_start_preflight(
            photo_enabled=launch_state.photo_enabled,
            photo_autofocus_enabled=photo_autofocus_enabled,
            wait_before_first_point=wait_before_first_point,
            objective_scale_available=scale is not None,
        )
        if not self._apply_gui_route_start_preflight(preflight):
            return False
        if not preflight.check_camera_frame:
            return True
        frame, _counter = self._wait_for_camera_frame(timeout_s=0.1)
        camera_preflight = gui_route_camera_frame_preflight(
            photo_enabled=launch_state.photo_enabled,
            photo_autofocus_enabled=photo_autofocus_enabled,
            camera_frame_available=frame is not None,
        )
        return self._apply_gui_route_start_preflight(camera_preflight)

    def _build_route_measurement_runner(
        self,
        configuration: RouteMeasurementRunConfiguration,
        *,
        points: list[RouteMeasurementPoint],
        route_lcr_controller: object,
        wait_before_first_point: bool,
    ) -> RouteMeasurementRunner:
        callbacks = GuiRouteEventBindings(
            status=self.route_measurement_status.emit,
            progress=self.route_measurement_progress.emit,
            record=self.route_measurement_recorded.emit,
            capture_photo=self._capture_route_photo,
            autofocus=self._route_photo_autofocus,
            photo_record=self._record_route_photo,
            contact_height=self._record_route_contact_height,
            contact_photo=self._capture_route_contact_photo,
            pre_contact_photo=self._capture_route_pre_contact_photo,
            result=self.route_measurement_result.emit,
            waiting=self.route_measurement_waiting_changed.emit,
        ).events()
        return RouteMeasurementRunner(
            points=points,
            csv_path=configuration.csv_path,
            stage_controller=self.stage_controller,
            lcr_controller=route_lcr_controller,
            needle_feedrate=self._current_needle_feedrate(),
            measurement_count=configuration.measurement_count,
            initial_measurement_count=configuration.initial_measurement_count,
            start_point_number=configuration.start_point,
            max_relative_rms=configuration.max_relative_rms,
            contact_quality_limits=configuration.contact_quality_limits,
            confirm_each_point=True,
            auto_next_ok_or_short=True,
            auto_contact_seek_on_bad_contact=True,
            auto_contact_seek_step_mm=configuration.contact_seek_step_mm,
            auto_contact_seek_max_total_mm=configuration.contact_seek_range_mm,
            contact_settle_s=configuration.contact_settle_s,
            nplc_label=configuration.meter.nplc_label(),
            measurement_type=configuration.meter.measurement_type_label(),
            operation_mode=configuration.operation_mode,
            photo_settle_s=configuration.photo_settle_s,
            photo_focus_enabled=configuration.photo_autofocus_enabled,
            photo_focus_range_mm=configuration.photo_autofocus_range_mm,
            photo_output_dir=configuration.photo_output_dir,
            wait_before_first_point=wait_before_first_point,
            events=callbacks,
        )

    def _start_route_measurement_runner(
        self,
        runner: RouteMeasurementRunner,
        launch_state: GuiRouteLaunchState,
        *,
        configuration: RouteMeasurementRunConfiguration,
        point_count: int,
    ) -> None:
        self._route_measurement_runner = runner
        self._route_measurement_waiting = False
        self._pending_route_measure_point = None
        self._route_measurement_photo_enabled = launch_state.photo_enabled
        self._route_measurement_measure_enabled = launch_state.measure_enabled
        presentation = launch_state.presentation
        self._route_measurement_point_numbers = presentation.point_numbers
        self._route_telegram_adapter().reset_for_route_start()
        self._set_route_measurement_pending(True)
        self._route_measurement_thread = threading.Thread(
            target=self._run_route_measurement,
            args=(runner,),
            name="RouteMeasurement",
            daemon=True,
        )
        start_message = presentation.message
        self._route_runtime_presenter().route_runner_started(start_message, point_count)
        self._show_status(start_message)
        self._last_route_measurement_result = None
        if launch_state.send_start_telegram:
            self._send_telegram_alert(
                "route_started",
                route_start_telegram_text(
                    start_message,
                    str(configuration.csv_path),
                ),
            )
        self._route_measurement_thread.start()
        self._update_stage_coordinate_apply_state()

    def _route_measurement_start_plan(
        self,
        configuration: RouteMeasurementRunConfiguration,
    ) -> RouteMeasurementStartPlan | None:
        route = self._design_session.route
        registration = self._design_session.registration
        decision = route_measurement_start_decision(
            route=route,
            registration_valid=bool(registration is not None and registration.valid),
            points_factory=self._route_measurement_points,
            current_point=configuration.current_point,
            previous_ok_only=configuration.previous_ok_only,
            previous_csv_path=configuration.previous_csv_path,
            structure_number_for_point=self._api_structure_number_for_measurement_point,
        )
        if decision.accepted:
            return decision.plan
        if decision.dialog_status:
            self._show_route_runtime_status(decision.message, decision.timeout_ms)
        else:
            self._show_status(decision.message, decision.timeout_ms)
        return None

    def _route_measurement_points(
        self,
        route: MeasurementRoute,
    ) -> list[RouteMeasurementPoint]:
        objective_settings = self.settings_manager.objectives_configuration()
        base_offset, active_offset = offsets.base_and_active_objective_offsets(
            objective_settings
        )
        return route_measurement_points_for_route(
            route,
            stage_from_design=self._design_session.stage_from_design,
            contact_objective_offset=base_offset,
            photo_objective_offset=active_offset,
        )

    def _capture_route_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        settings: PointPhotoSettings,
        focus_result: object | None = None,
    ) -> str:
        route = self._design_session.route
        return capture_route_photo(
            point,
            position,
            total,
            photo_only_mode=settings.photo_only_mode,
            photo_output_dir=settings.output_dir,
            photo_autofocus_enabled=settings.autofocus_enabled,
            photo_autofocus_range_mm=settings.autofocus_range_mm,
            route_name=(route.name if route is not None else "route"),
            focus_result=focus_result,
            active_microscope_scale=self._active_microscope_scale,
            latest_camera_counter=self._latest_camera_counter,
            wait_for_camera_frame=self._wait_for_camera_frame,
            timestamp_utc=utc_timestamp,
            active_objective_metadata=self._active_objective_metadata,
            stage_position_for_image_metadata=self._stage_position_for_image_metadata,
            save_image=save_microscope_image,
            route_photo_focus_payload=route_photo_focus_payload,
        )

    def _route_photo_autofocus(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        settings: PointPhotoSettings,
    ) -> object:
        _ = point
        self.route_measurement_status.emit(
            "Route photo autofocus: "
            f"point {position}/{total}, "
            f"+/-{settings.autofocus_range_mm:.3f} mm."
        )
        return self.stage_controller.run_external_local_autofocus(
            range_mm=settings.autofocus_range_mm,
        )

    def _record_route_photo(
        self,
        record: RoutePhotoRecord,
        position: int,
        total: int,
    ) -> None:
        self._route_telegram_adapter().record_route_photo(
            record,
            position,
            total,
            route_name=self._current_route_name(),
            send_bot_message=self._send_telegram_bot_message,
            default_markup=(
                self._telegram_default_markup()
                if hasattr(self, "_route_measurement_waiting")
                else None
            ),
        )

    def _capture_route_pre_contact_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> None:
        route_attention_enabled = telegram_route_attention_alert_enabled(
            self.settings_manager.telegram_configuration()
        )
        self._route_telegram_adapter().capture_pre_contact_photo(
            point,
            position,
            total,
            structure_number=self._api_structure_number_for_measurement_point(point),
            route_attention_enabled=route_attention_enabled,
            latest_camera_counter=self._latest_camera_counter,
            wait_for_camera_frame=self._wait_for_camera_frame,
            qimage_telegram_photo=self._qimage_telegram_photo,
            latest_camera_frame_photo=self._latest_camera_frame_photo,
        )

    def _capture_route_contact_photo(
        self,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        saved: bool,
    ) -> None:
        contact_attention = self._route_record_needs_contact_attention(record)
        route_attention_enabled = telegram_route_attention_alert_enabled(
            self.settings_manager.telegram_configuration()
        )
        self._route_telegram_adapter().capture_contact_photo(
            point,
            record,
            position,
            total,
            saved=bool(saved),
            contact_attention=bool(contact_attention),
            route_attention_enabled=route_attention_enabled,
            latest_camera_counter=self._latest_camera_counter,
            wait_for_camera_frame=self._wait_for_camera_frame,
            qimage_telegram_photo=self._qimage_telegram_photo,
            latest_camera_frame_photo=self._latest_camera_frame_photo,
        )

    def _take_pending_telegram_contact_photos(
        self,
    ) -> tuple[tuple[bytes, str, str] | None, tuple[bytes, str, str]] | None:
        return self._route_telegram_adapter().take_pending_contact_photos()

    def _latest_route_contact_failure_telegram_photos(
        self,
    ) -> tuple[tuple[bytes, str, str] | None, tuple[bytes, str, str]] | None:
        return self._route_telegram_adapter().latest_contact_failure_photos()

    def _telegram_contact_photo_payload(
        self,
        before_photo: tuple[bytes, str, str] | None,
        after_photo: tuple[bytes, str, str],
    ) -> tuple[tuple[bytes, str], str]:
        photo, caption = telegram_contact_photo_payload(
            before_photo,
            after_photo,
            combine_photos=self._combine_telegram_contact_photos,
        )
        if before_photo is not None and photo[1] != "route-contact-comparison.jpg":
            logger.warning("Unable to combine route contact photos for Telegram.")
        return photo, caption

    @staticmethod
    def _combine_telegram_contact_photos(
        before_bytes: bytes,
        after_bytes: bytes,
    ) -> tuple[bytes, str] | None:
        return combine_telegram_contact_photos(
            before_bytes,
            after_bytes,
            encode_image=Main._qimage_telegram_photo,
        )

    def _record_route_contact_height(
        self,
        record: RouteContactHeightRecord,
        position: int,
        total: int,
        *,
        csv_path: str | Path,
    ) -> None:
        try:
            path = route_contact_height_map_path(csv_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            exists = path.exists() and path.stat().st_size > 0
            with path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=ROUTE_CONTACT_HEIGHT_MAP_FIELDS,
                )
                if not exists:
                    writer.writeheader()
                writer.writerow(
                    route_contact_height_map_row(
                        record,
                        position,
                        total,
                        route_name=self._current_route_name(),
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            logger.warning("Unable to write route contact height map: %s", exc)

    def _current_route_name(self) -> str:
        route = getattr(getattr(self, "_design_session", None), "route", None)
        if route is None:
            return ""
        return route.name

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
        waiting_reason = self._current_route_measurement_waiting_reason(waiting)
        self._route_runtime_presenter().route_started(message, total_points, waiting=waiting, waiting_reason=waiting_reason)
        self._show_status(message)
        self._update_stage_coordinate_apply_state()

    def _request_stop_route_measurement(self) -> None:
        if self._api_route_control_state_snapshot().active:
            self._api_route_control_action({"action": "stop"})
            return
        runner = self._route_measurement_runner
        if runner is None:
            self._show_status("No route measurement is running.", 3000)
            return
        self._pending_route_measure_point = None
        runner.stop()
        self._show_status("Stopping route measurement.")
        self._update_stage_coordinate_apply_state()
        self._route_runtime_presenter().stop_requested("Stopping route measurement.")

    def _request_route_measurement_point_correction(self, pending_point_number: int | None = None) -> None:
        if self._api_route_control_state_snapshot().active:
            self._interrupt_api_route_controlled_operation(
                "API route control interrupt requested."
            )
            return
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
        self._show_route_runtime_status(message, 5000)

    def _submit_route_measurement_confirmation(self, action: str) -> None:
        runner = self._route_measurement_runner
        move_thread = getattr(self, "_route_contact_move_thread", None)
        confirmation_plan = route_confirmation_submission_plan(
            action,
            api_route_control=self._api_route_control_state_snapshot(),
            runner_available=runner is not None,
            contact_move_active=move_thread is not None and move_thread.is_alive(),
            waiting=getattr(self, "_route_measurement_waiting", False),
            pending_point_number=getattr(self, "_pending_route_measure_point", None),
        )
        if confirmation_plan.api_action is not None:
            self._api_route_control_action({"action": confirmation_plan.api_action})
            return
        if confirmation_plan.message:
            self._show_status(
                confirmation_plan.message,
                confirmation_plan.timeout_ms,
            )
            return
        if self._route_measurement_dialog is not None:
            runner = self._apply_route_measurement_confirmation_runtime(runner)
            if runner is None:
                return
        confirmation = confirmation_plan.confirmation
        if confirmation is None:
            return
        if not runner.submit_confirmation(confirmation.action):
            self._show_status("Unknown route measurement action.", 3000)
            return
        if confirmation.replaced_pending_point:
            self._pending_route_measure_point = None
        self._route_runtime_presenter().clear_waiting()
        self._show_status(f"Route measurement: {confirmation.status_label}.")

    def _apply_route_measurement_confirmation_runtime(self, runner):
        if self._route_measurement_dialog is None:
            return runner
        configuration = self._route_measurement_dialog.current_configuration()
        external_session = isinstance(
            runner,
            RouteExternalMeasurementSessionRunner,
        )
        runtime_plan = route_confirmation_runtime_plan(
            configuration,
            external_session=external_session,
            waiting=self._route_measurement_waiting,
            setup_changed=route_measurement_setup_changed(
                self._route_measurement_runtime_configuration,
                configuration,
            ),
        )
        if runtime_plan.restart_required:
            route_offset_xy = (
                runner.route_offset_xy()
                if hasattr(runner, "route_offset_xy")
                else (0.0, 0.0)
            )
            if not restart_waiting_route_measurement(
                configuration=configuration,
                old_runner=self._route_measurement_runner,
                old_thread=self._route_measurement_thread,
                route_offset_xy=route_offset_xy,
                clear_waiting_state=self._clear_waiting_route_measurement_state,
                start_measurement=self._start_route_measurement,
                current_runner=lambda: self._route_measurement_runner,
                current_thread=lambda: self._route_measurement_thread,
                show_status=lambda message, timeout_ms=5000: (self._show_status(message, timeout_ms), self._route_runtime_presenter().set_status(message)),
            ):
                return None
            runner = self._route_measurement_runner
            if runner is None:
                return None
        self._route_measurement_runtime_configuration = configuration
        self._save_route_measurement_session_metadata(configuration)
        runner.update_runtime_settings(**runtime_plan.runtime_settings)
        if not external_session and runtime_plan.meter_configuration_required:
            try:
                runner.apply_meter_configuration(configuration.meter)
            except LCRMeterError as exc:
                message = f"Route measurement instrument setup failed: {exc}"
                self._show_status(message, 8000)
                self._route_runtime_presenter().set_status(message)
                return None
        return runner

    def _submit_route_measurement_jump(self, point_number: int) -> None:
        self._submit_route_measurement_confirmation(f"jump:{int(point_number)}")

    def _request_route_contact_move(self, point_number: int) -> None:
        move_thread = getattr(self, "_route_contact_move_thread", None)
        route_thread = self._route_measurement_thread
        move_plan = route_contact_move_plan(
            contact_move_active=move_thread is not None and move_thread.is_alive(),
            route_active=route_thread is not None and route_thread.is_alive(),
            route_waiting=getattr(self, "_route_measurement_waiting", False),
            api_route_control=self._api_route_control_state_snapshot(),
        )
        if move_plan.message:
            self._show_status(move_plan.message, move_plan.timeout_ms)
            return
        context_result = self._api_contact_context(int(point_number))
        if not context_result.get("accepted", False):
            message = str(context_result.get("message") or "Route contact move rejected.")
            self._show_route_runtime_status(message, 6000)
            return
        point = context_result["point"]
        if move_plan.set_resume_point:
            self._set_route_measurement_resume_point(int(point.index))
            runner = self._route_measurement_runner
            if runner is not None and move_plan.set_adjustment_point:
                self._pending_route_measure_point = int(point.index)
                if hasattr(runner, "set_current_adjustment_point"):
                    runner.set_current_adjustment_point(int(point.index))
        needle_feedrate = self._current_needle_feedrate()
        message = f"Route contact move: point {int(point.index)} {point.label}."
        self._show_route_runtime_status(message, 5000)
        thread = threading.Thread(
            target=self._run_route_contact_move,
            args=(point, needle_feedrate),
            name="RouteContactMove",
            daemon=True,
        )
        self._route_contact_move_thread = thread
        thread.start()
        self._update_stage_coordinate_apply_state()

    def _run_route_contact_move(self, point: RouteMeasurementPoint, needle_feedrate: float | None) -> None:
        success = False
        try:
            with self.stage_controller.reserve_external_task("route contact move"):
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
                target_xy = self._api_route_adjusted_stage_xy(point)
                self.stage_controller.run_external_move_to_xy(
                    target_xy[0],
                    target_xy[1],
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
        self.route_contact_move_finished.emit(success, message)

    def _on_route_contact_move_finished(self, success: bool, message: str) -> None:
        thread = getattr(self, "_route_contact_move_thread", None)
        if thread is not None and not thread.is_alive():
            thread.join(timeout=0.1)
        self._route_contact_move_thread = None
        self._update_stage_coordinate_apply_state()
        timeout_ms = 5000 if success else 8000
        self._show_route_runtime_status(message, timeout_ms)

    def _request_pause_route_measurement(self) -> None:
        api_route_pause_action = (
            self._api_route_control_state_snapshot().pause_control_action()
        )
        if api_route_pause_action:
            if api_route_pause_action == "resume":
                self._api_route_control_action({"action": "resume"})
            elif api_route_pause_action == "interrupt":
                self._interrupt_api_route_controlled_operation(
                    "API route control interrupt requested."
                )
            else:
                self._api_route_control_action({"action": "pause"})
            return
        runner = self._route_measurement_runner
        if runner is None:
            self._show_status("No route measurement is running.", 3000)
            return
        runner.request_pause_after_current_point()
        message = "Route measurement pause requested; will pause after current point."
        self._show_status(message, 5000)
        self._route_runtime_presenter().pause_requested(message)

    def _save_route_measurement_shift(self, point_number: int | None = None) -> None:
        runner = self._route_measurement_runner
        shift_plan = self._route_shift_save_plan(runner, point_number)
        if shift_plan.message:
            self._show_route_runtime_status(shift_plan.message, shift_plan.timeout_ms)
            return
        adjustment_selected, adjustment_point = self._route_shift_adjustment_point(
            shift_plan,
            runner,
        )
        if not adjustment_selected:
            return
        stage_xy = self._route_shift_current_stage_xy()
        if stage_xy is None:
            return
        if shift_plan.runner_active:
            saved, message = runner.save_current_position_adjustment(stage_xy)
            self._update_api_route_offset_from_runner(runner, saved)
        else:
            self._api_route_offset_xy, message = route_shift_from_stage_xy(
                stage_xy,
                adjustment_point.stage_xy,
            )
        status_plan = route_shift_save_status_plan(
            runner_active=shift_plan.runner_active,
            message=message,
        )
        self._apply_route_shift_save_status(status_plan)

    def _route_shift_save_plan(self, runner: object | None, point_number: int | None) -> RouteShiftSavePlan:
        thread = self._route_measurement_thread
        route_active = thread is not None and thread.is_alive()
        route_waiting = getattr(self, "_route_measurement_waiting", False)
        api_route_control = self._api_route_control_state_snapshot()
        guard_plan = route_shift_save_guard_plan(
            runner_available=runner is not None,
            route_active=route_active,
            route_waiting=route_waiting,
            api_route_control=api_route_control,
        )
        if guard_plan.message:
            return guard_plan
        dialog_current_point = (
            int(self._route_measurement_dialog.current_configuration().current_point)
            if point_number is None and self._route_measurement_dialog is not None
            else None
        )
        return route_shift_save_plan(
            runner_available=runner is not None,
            route_active=route_active,
            route_waiting=route_waiting,
            api_route_control=api_route_control,
            requested_point_number=point_number,
            dialog_current_point=dialog_current_point,
            current_point=getattr(self, "_route_measurement_current_point", None),
        )

    def _route_shift_adjustment_point(self, shift_plan: RouteShiftSavePlan, runner: object | None) -> tuple[bool, RouteMeasurementPoint | None]:
        point_number = shift_plan.point_number
        if shift_plan.needs_runner_adjustment and runner is not None:
            point_selected, message = runner.set_current_adjustment_point(
                int(point_number)
            )
            if not point_selected:
                self._show_route_runtime_status(message, 6000)
                return False, None
            return True, None
        if not shift_plan.needs_api_context:
            return True, None
        context_result = self._api_contact_context(int(point_number))
        if not context_result.get("accepted", False):
            message = str(
                context_result.get("message")
                or "Route point is unavailable for saving shift."
            )
            self._show_route_runtime_status(message, 6000)
            return False, None
        return True, context_result["point"]

    def _route_shift_current_stage_xy(self) -> tuple[float, float] | None:
        try:
            position = self.stage_controller.current_stage_position()
        except StageControllerError as exc:
            latest = self.stage_controller.latest_stage_position()
            position_plan = route_shift_stage_position_error_plan(
                error_message=str(exc),
                controller_busy=self.stage_controller.is_busy(),
                latest_position_available=latest is not None,
            )
            if position_plan.message:
                self._show_route_runtime_status(position_plan.message, position_plan.timeout_ms)
                return None
            position = latest
        stage_xy = stage_position_update.stage_xy_from_position(position)
        xy_plan = route_shift_stage_xy_plan(stage_xy_available=stage_xy is not None)
        if xy_plan.message:
            self._show_route_runtime_status(xy_plan.message, xy_plan.timeout_ms)
            return None
        return stage_xy

    def _update_api_route_offset_from_runner(
        self,
        runner: object,
        saved: bool,
    ) -> None:
        if not saved or not hasattr(runner, "route_offset_xy"):
            return
        try:
            raw_offset_xy = runner.route_offset_xy()
        except (TypeError, ValueError, IndexError):
            return
        offset_xy = route_shift_runner_offset_update(raw_offset_xy)
        if offset_xy is not None:
            self._api_route_offset_xy = offset_xy

    def _apply_route_shift_save_status(self, status_plan: RouteShiftSaveStatusPlan) -> None:
        message = status_plan.message
        self._show_status(message, status_plan.timeout_ms)
        self._route_runtime_presenter().shift_status(message, mark_interrupt_pending=status_plan.mark_interrupt_pending)

    def _interrupt_route_measurement_runner(self, runner: object, *, reason: str) -> None:
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
            stage_position_panel_adapter.clear_stage_motion_axes(self)
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)

    def _on_route_measurement_status(self, message: str) -> None:
        self._show_status(message)
        if self._route_attention_status(message):
            self._send_route_attention_alert(message)
        self._route_runtime_presenter().set_status(message)

    def _on_route_measurement_progress(
        self,
        position: int,
        total: int,
        point_number: int,
    ) -> None:
        self._set_route_measurement_resume_point(point_number)
        self._route_runtime_presenter().set_progress(position, total, point_number)

    def _on_route_measurement_current_point_changed(self, point_number: int) -> None:
        self._set_route_measurement_resume_point(point_number)
        runner = self._route_measurement_runner
        if runner is not None and self._route_measurement_waiting:
            self._pending_route_measure_point = int(point_number)
            if hasattr(runner, "set_current_adjustment_point"):
                runner.set_current_adjustment_point(point_number)

    def _on_route_measurement_waiting_changed(self, waiting: bool) -> None:
        self._route_measurement_waiting = bool(waiting)
        waiting_reason = self._current_route_measurement_waiting_reason(waiting)
        self._route_measurement_waiting_reason = waiting_reason
        self._route_runtime_presenter().waiting_changed(waiting, waiting_reason)
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

    def _current_route_measurement_waiting_reason(self, waiting: bool) -> str:
        if not waiting:
            return ""
        runner = getattr(self, "_route_measurement_runner", None)
        if runner is not None and hasattr(runner, "status_payload"):
            try:
                status = runner.status_payload()
            except Exception:
                logger.exception("Failed to read route waiting reason.")
            else:
                reason = str(status.get("waiting_reason") or "").strip()
                if reason:
                    return reason
        return "paused"

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
        self._route_runtime_presenter().set_result(record, position, total, saved)
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
            self._route_runtime_presenter().unsaved_result_status(message)

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
        self._route_telegram_adapter().send_route_attention_alert(
            message,
            include_contact_photos=include_contact_photos,
            failure_photos=(
                self._latest_route_contact_failure_telegram_photos()
                if include_contact_photos
                else None
            ),
            contact_photo_payload=self._telegram_contact_photo_payload,
            send_alert=self._send_telegram_alert,
            route_actions_markup=self._telegram_route_actions_markup(),
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
        self._route_runtime_presenter().recorded_result_status(message)
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
        failure_criteria = tuple(getattr(contact, "failure_criteria", ()) or ())
        failure_text = (
            ", failed_criterion=" + " | ".join(str(item) for item in failure_criteria)
            if failure_criteria
            else ""
        )
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
            f"{failure_text}"
        )

    def _on_route_measurement_finished(self, *args: object) -> None:
        finish_signal = route_finish_signal_plan(
            args,
            current_runner=self._route_measurement_runner,
        )
        if finish_signal.ignored:
            return
        success = finish_signal.success
        message = finish_signal.message
        csv_path = finish_signal.csv_path
        measure_enabled = self._route_measurement_measure_enabled
        context_close_requested = bool(
            getattr(self, "_route_measurement_context_close_requested", False)
        )
        finish_plan = route_finish_outcome_plan(
            success=success,
            message=message,
            csv_path=csv_path,
            measure_enabled=measure_enabled,
            context_close_requested=context_close_requested,
            point_numbers=list(self._route_measurement_point_numbers),
            current_point=self._route_measurement_current_point,
        )
        self._route_measurement_context_close_requested = False
        self._join_finished_route_measurement_thread()
        runner = self._route_measurement_runner
        self._store_final_api_route_session_status(runner)
        self._clear_finished_route_measurement_state()
        self._update_stage_coordinate_apply_state()
        self._route_runtime_presenter().finished_ui(success, message)

        session_measurement_count = (
            self._route_measurement_csv_record_count(csv_path)
            if finish_plan.needs_csv_record_count
            else None
        )
        if finish_plan.resume_point is not None:
            self._set_route_measurement_resume_point(finish_plan.resume_point)
        self._set_route_measurement_pending(finish_plan.pending)
        if finish_plan.clear_point_numbers:
            self._route_measurement_point_numbers = []
        self._show_status(finish_plan.status_text, finish_plan.status_timeout_ms)
        telegram_payload = route_finish_telegram_payload(
            finish_plan.telegram,
            session_measurement_count=session_measurement_count,
        )
        if telegram_payload is not None and finish_plan.telegram is not None:
            telegram_message, telegram_kwargs = telegram_payload
            self._send_telegram_alert(
                finish_plan.telegram.key,
                telegram_message,
                **telegram_kwargs,
            )

    def _join_finished_route_measurement_thread(self) -> None:
        thread = self._route_measurement_thread
        if thread is not None and not thread.is_alive():
            thread.join(timeout=0.1)
        self._route_measurement_thread = None

    def _store_final_api_route_session_status(self, runner: object | None) -> None:
        status = final_api_route_session_status(getattr(self, "_api_route_session_id", None), runner, logger=logger)
        if status is not None:
            self._api_route_last_status = status

    def _clear_finished_route_measurement_state(self) -> None:
        self._route_measurement_runner = None
        self._api_route_lcr_controller = None
        self._route_measurement_runtime_configuration = None
        self._route_measurement_waiting = False
        self._route_measurement_waiting_reason = ""
        self._last_route_measurement_result = None
        self._pending_route_measure_point = None
        self._route_measurement_photo_enabled = False
        self._route_measurement_measure_enabled = False
        self._resume_resistance_standby_polling()
        self._route_telegram_adapter().clear_for_route_finish()

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
        if not self._route_runtime_presenter().set_current_point(value):
            self._save_route_measurement_current_point(value)

    def _select_route_point_for_measurement(self, point_number: int) -> None:
        plan = design_navigation.select_route_point_for_measurement(
            self._design_session,
            point_number,
        )
        if not plan.selected:
            return
        self._last_selected_design_point = plan.last_selected_design_point
        self._refresh_design_panel()
        connection_flow.persist_controller_state_if_available(self)

    def _save_route_measurement_current_point(self, point_number: int) -> None:
        try:
            self._route_measurement_settings_store().save_current_point(
                int(point_number),
                session_active=bool(self._route_measurement_session_active),
            )
        except OSError:
            logger.exception("Failed to persist route measurement resume point.")

    def _set_route_measurement_pending(self, pending: bool) -> None:
        self._route_measurement_session_active = bool(pending)
        if not self._route_runtime_presenter().set_measurement_session_active(bool(pending)):
            self._save_route_measurement_pending(bool(pending))

    def _save_route_measurement_pending(self, pending: bool) -> None:
        try:
            self._route_measurement_settings_store().save_pending(bool(pending))
        except OSError:
            logger.exception("Failed to persist route measurement pending state.")

    def _save_route_measurement_session_metadata(
        self,
        configuration: RouteMeasurementRunConfiguration | None,
    ) -> None:
        try:
            self._route_measurement_settings_store().save_session_metadata(
                route=self._design_session.route,
                configuration=configuration,
                session_active=bool(self._route_measurement_session_active),
            )
        except OSError:
            logger.exception("Failed to persist route measurement session metadata.")

    def _select_route_point(self, index: int) -> None:
        if not self._design_mutation_ready():
            return
        plan = design_navigation.select_route_point(self._design_session, index)
        self._last_selected_design_point = plan.last_selected_design_point
        self._refresh_design_panel()

    def _set_route_needle_offsets(
        self,
        needle_1_dx: float,
        needle_1_dy: float,
        needle_2_dx: float,
        needle_2_dy: float,
    ) -> None:
        if not self._design_mutation_ready():
            return
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
        if not self._design_mutation_ready():
            return
        self._design_session.add_source_design_mark((x_value, y_value))
        self._refresh_design_panel()
        self._show_status(
            f"Design source mark captured at X={x_value:.3f}, Y={y_value:.3f}.",
            4000,
        )

    def _add_design_check_mark(self, x_value: float, y_value: float) -> None:
        if not self._design_mutation_ready():
            return
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
        design_navigation.select_design_target(self._design_session, target_id)
        self._refresh_design_panel()

    def _select_next_design_target(self) -> None:
        plan = design_navigation.select_next_design_target(self._design_session)
        self._refresh_design_panel()
        if plan.status_message is not None:
            self._show_status(plan.status_message, plan.status_timeout_ms)

    def _select_previous_design_target(self) -> None:
        plan = design_navigation.select_previous_design_target(self._design_session)
        self._refresh_design_panel()
        if plan.status_message is not None:
            self._show_status(plan.status_message, plan.status_timeout_ms)

    def _move_to_design_target(self, target_id: str) -> None:
        target = self._design_session.select_target_by_id(target_id)
        stage_xy = (
            self._raw_stage_xy_from_design_xy(target.design_center)
            if target is not None
            else None
        )
        plan = design_navigation.plan_design_target_move(
            self._design_session,
            target_id,
            stage_xy,
        )
        if not plan.accepted:
            if plan.status_message is not None:
                self._show_status(plan.status_message, plan.status_timeout_ms)
            return
        self._refresh_design_panel()
        assert plan.stage_xy is not None
        self.stage_controller.request_move_to_xy(plan.stage_xy[0], plan.stage_xy[1])

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
        toggle_design_layout_window(self, True)

    def _move_to_design_coordinate(
        self, design_xy: tuple[float, float], *, source_label: str
    ) -> bool:
        document = self._design_session.document
        stage_xy = (
            self._raw_stage_xy_from_design_xy(design_xy)
            if document is not None
            else None
        )
        plan = design_navigation.plan_design_coordinate_move(
            document is not None,
            self.stage_controller.is_busy() if document is not None else False,
            design_xy,
            stage_xy,
            source_label,
        )
        if not plan.accepted:
            if plan.status_message is not None:
                self._show_status(plan.status_message, plan.status_timeout_ms)
            return False
        self._last_selected_design_point = plan.last_selected_design_point
        self._refresh_design_panel()
        self._clear_planned_move_prediction(clear_wait_state=True)
        self._pending_planned_move_target_xy = plan.pending_planned_move_target_xy
        self._pending_planned_move_source_label = plan.source_label
        assert plan.stage_xy is not None and plan.design_xy is not None
        self.stage_controller.request_move_to_xy(plan.stage_xy[0], plan.stage_xy[1])
        logger.debug(
            "DESIGN MOVE source=%s design=(%.3f, %.3f) stage=(%.3f, %.3f)",
            plan.source_label,
            plan.design_xy[0],
            plan.design_xy[1],
            plan.stage_xy[0],
            plan.stage_xy[1],
        )
        return True

    def _refresh_design_panel(self) -> None:
        panel = self.design_navigator_panel
        route_measurement_thread = getattr(self, "_route_measurement_thread", None)
        p = design_navigation.design_panel_presentation(
            self._design_session,
            route_running=route_measurement_thread is not None and route_measurement_thread.is_alive(),
            pending_alignment_preparation=self._pending_alignment_preparation is not None,
            design_snap_enabled=self._design_snap_enabled,
        )
        if panel is not None:
            panel.set_document(p.document)
            panel.set_design_registration_active(p.registration_valid)
            panel.set_targets(p.targets, selected_target_id=p.selected_target_id)
            panel.set_route(p.route, selected_route_point_index=p.selected_route_point_index)
            panel.set_route_measurement_running(p.route_measurement_running)
            panel.set_calibration_prompt(p.calibration_prompt)
            panel.set_registration_status(p.registration_status)
            panel.set_registration_marks(p.source_design_marks, p.check_design_marks)
            panel.set_stage_registration_marks(p.source_stage_marks)
        if self.design_layout_window is not None:
            self.design_layout_window.set_snap_enabled(p.design_snap_enabled)
            self.design_layout_window.set_document(p.document)
            self.design_layout_window.set_targets(p.targets, selected_target_id=p.selected_target_id)
            self.design_layout_window.set_probe_route(p.route, selected_route_point_index=p.selected_route_point_index)
            self.design_layout_window.set_markup(
                getattr(self, "_design_markup", None)
            )
            self.design_layout_window.set_guide_undo_available(
                bool(self._prune_design_guide_undo_stack())
            )
            self.design_layout_window.set_route_edit_enabled(
                self._design_edit_safe()
            )
            self.design_layout_window.set_navigation_enabled(p.registration_valid)
            self.design_layout_window.set_registration_marks(p.source_design_marks, p.check_design_marks)
            self.design_layout_window.set_stage_registration_marks(p.source_stage_marks)
        self._refresh_manual_alignment_ui()
        self._update_design_position(self._current_design_stage_xy)
        connection_flow.persist_controller_state_if_available(self)

    def _on_stage_axis_editing_finished(self, axis_name: str) -> bool | None:
        panel = getattr(self, "_stage_position_panel", None)
        if panel is None or panel.is_programmatic_update:
            return None
        axis = axis_name.strip().upper()
        commit_from_return = panel.consume_return_commit(axis)
        field = panel.field(axis)
        if field is None or not field.isEnabled() or not field.isModified():
            return None

        def reject(message: str, timeout_ms: int) -> bool:
            panel.pop_pending_target(axis)
            panel.reset_axis_field(axis, self._stage_axis_display_values.get(axis))
            stage_position_panel_adapter.refresh_stage_axis_styles(self)
            self._update_stage_coordinate_apply_state()
            self._show_status(message, timeout_ms)
            return False

        text = field.text().strip().replace(",", ".")
        try:
            display_target = float(text)
        except (TypeError, ValueError):
            return reject(f"Invalid {axis} target coordinate.", 3000)
        input_mode = panel.selected_input_mode()
        raw_target, resolved_display_target = self._resolve_stage_axis_target(axis, display_target, input_mode)
        if raw_target is None:
            return reject(f"{axis} coordinate is unavailable.", 3000)
        limit_error = self._stage_axis_target_limit_error(axis, resolved_display_target)
        if limit_error is not None:
            return reject(limit_error, 4000)
        field.blockSignals(True)
        field.setText(format_stage_axis_value(display_target))
        field.setModified(False)
        if commit_from_return:
            field.clearFocus()
        field.blockSignals(False)
        if commit_from_return:
            self.view.setFocus(Qt.OtherFocusReason)
        panel.set_pending_target(axis, raw_target, resolved_display_target)
        stage_position_panel_adapter.refresh_stage_axis_styles(self)
        self._update_stage_coordinate_apply_state()
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
        if self._coordinate_targets.has_active_move() or self.stage_controller.is_busy():
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
        decision = plan_coordinate_target_start(
            self._coordinate_targets.config,
            targets=targets,
            feedrate_mm_min=feedrate_mm_min,
            source_label=source_label,
            seed_position=stage_position_update.seed_motion_prediction_position(self),
            latest_stage_position=self.stage_controller.latest_stage_position(),
            axis_target_limit_error=self._stage_axis_target_limit_error,
            axis_max_feedrates=self.stage_controller.axis_max_feedrates(),
            monotonic_s=time.monotonic(),
        )
        if not decision.accepted:
            if decision.status is not None:
                self._show_status(
                    decision.status.message,
                    decision.status.timeout_ms,
                )
            return False
        plan = decision.plan
        if plan is None:
            return False
        for axis in plan.remove_pending_axes:
            self._pending_stage_axis_targets.pop(axis, None)
        if plan.invalidate_design_registration:
            self._invalidate_design_registration(
                "Design registration cleared after B-axis coordinate motion."
            )
        self._coordinate_targets.apply_start_plan(plan)
        stage_position_panel_adapter.set_stage_motion_axes(
            self,
            set(plan.ordered_axes),
        )
        self._apply_coordinate_common_feedrate_plan(plan.common_feedrate)
        self._update_stage_coordinate_apply_state()
        accepted = self.stage_controller.request_absolute_axis_targets_move(
            plan.raw_targets,
            feedrate=plan.feedrate_mm_min,
        )
        if not accepted:
            stage_move_lifecycle.clear_coordinate_move_tracking(
                self,
                clear_pending=False,
                reset_override=True,
            )
            return False
        self._show_status(plan.status.message, plan.status.timeout_ms)
        stage_position_update.publish_stage_position_estimate(
            self,
            plan.publish_position,
        )
        if not self._manual_jog_timer.isActive():
            self._manual_jog_timer.start()
        return True

    def _apply_coordinate_common_feedrate_plan(self, plan: object) -> None:
        if self.joystick_panel is None:
            return
        clear_common_target = bool(getattr(plan, "clear_common_target", False))
        if clear_common_target:
            if hasattr(self.joystick_panel, "clear_common_feedrate_target"):
                self.joystick_panel.clear_common_feedrate_target()
            return
        if hasattr(self.joystick_panel, "set_common_feedrate_target"):
            self.joystick_panel.set_common_feedrate_target(
                float(getattr(plan, "feedrate_mm_min")),
                float(getattr(plan, "max_feedrate_mm_min")),
            )

    def _apply_coordinate_move_feedrate(self, feedrate_mm_min: float) -> None:
        latest_state = (self.stage_controller.latest_stage_state() or "").lower()
        decision = self._coordinate_targets.plan_feedrate_reissue(
            controller_busy=self.stage_controller.is_busy(),
            latest_stage_state=latest_state,
            requested_feedrate_mm_min=feedrate_mm_min,
            monotonic_s=time.monotonic(),
        )
        if decision.log_debug_message is not None:
            logger.debug(decision.log_debug_message)
        if decision.clear_stale_tracking:
            stage_move_lifecycle.clear_coordinate_move_tracking(
                self,
                clear_pending=False,
                reset_override=False,
            )
            if decision.clear_stage_motion_axes:
                stage_position_panel_adapter.clear_stage_motion_axes(self)
            return
        self._advance_coordinate_move_prediction()
        if not self._coordinate_targets.has_active_move():
            return
        decision = self._coordinate_targets.plan_feedrate_reissue(
            controller_busy=self.stage_controller.is_busy(),
            latest_stage_state=latest_state,
            requested_feedrate_mm_min=feedrate_mm_min,
            monotonic_s=time.monotonic(),
        )
        request = decision.request
        if request is None:
            return
        try:
            self._coordinate_targets.reissue_cancel_pending = (
                request.set_reissue_cancel_pending
            )
            accepted = self.stage_controller.queue_absolute_axis_targets_jog(
                request.raw_targets,
                feedrate=request.requested_feedrate_mm_min,
                replace_active=True,
            )
        except Exception as error:  # pragma: no cover - UI safety guard
            logger.exception("Failed to update coordinate move feedrate.")
            accepted = False
            self._show_status(str(error), 3000)
        if not accepted:
            self._coordinate_targets.reissue_cancel_pending = False
            self._show_status("Unable to update coordinate move feedrate.", 3000)
            self._schedule_status_refreshes(self.MANUAL_JOG_SETTLE_POLL_DELAYS_MS)
            return
        self._coordinate_targets.apply_feedrate_reissue_success(request)
        self._show_status(
            f"Active coordinate move feedrate: F{request.requested_feedrate_mm_min:.1f}.",
            1500,
        )

    def _start_next_pending_stage_axis_move(self) -> None:
        if self._coordinate_targets.has_active_move() or not self._pending_stage_axis_targets:
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

    def _raw_target_from_display_value(
        self, axis_name: str, display_target: float
    ) -> float | None:
        axis = axis_name.strip().upper()
        if axis not in self._stage_axis_raw_values:
            return None
        return stage_position_panel_adapter.raw_axis_value_from_display(
            self,
            axis,
            display_target,
        )

    def _resolve_stage_axis_target(
        self,
        axis_name: str,
        input_value: float,
        input_mode: str,
    ) -> tuple[float | None, float]:
        return resolve_stage_axis_target(
            self.STAGE_AXIS_NAMES,
            raw_target_from_display_value=self._raw_target_from_display_value,
            display_values=self._stage_axis_display_values,
            axis_name=axis_name,
            input_value=input_value,
            input_mode=input_mode,
        )

    def _stage_axis_target_limit_error(
        self,
        axis_name: str,
        display_target: float,
    ) -> str | None:
        return stage_axis_target_limit_error(
            axis_name,
            display_target,
            homed_axes=self._stage_axis_homed,
            axis_display_limits=self.stage_controller.axis_display_limits,
        )

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
        preferred_stage_xy = stage_position_update.preferred_design_display_stage_xy(
            self
        )
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
        p = design_navigation.design_position_presentation(
            self._design_session,
            stage_xy=stage_xy,
            design_xy=design_xy,
            fov_design_size=fov_design_size,
            last_selected_design_point=self._last_selected_design_point,
        )
        if self.design_navigator_panel is not None:
            self.design_navigator_panel.set_current_position(
                p.stage_xy, p.current_design_position, fov_design_size=p.fov_design_size
            )
        if self.design_layout_window is not None:
            self.design_layout_window.set_current_design_position(
                p.current_design_position, fov_design_size=p.fov_design_size
            )
        self.view.set_design_minimap_data(
            document=p.document,
            targets=p.targets,
            selected_target_id=p.selected_target_id,
            probe_route=p.probe_route,
            selected_route_point_index=p.selected_route_point_index,
            selected_design_point=p.selected_design_point,
            current_design_position=p.current_design_position,
            fov_design_size=p.fov_design_size,
            source_design_marks=p.source_design_marks,
            check_design_marks=p.check_design_marks,
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

    @staticmethod
    def _format_optional_point(point: tuple[float, float] | None) -> str:
        return "None" if point is None else f"({float(point[0]):.4f}, {float(point[1]):.4f})"

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

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._clear_exact_step_targets()
        shutdown_ui.close_event(self, event)

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

    def _show_microscope_scan_start_rejection(self, decision: object) -> bool:
        if bool(getattr(decision, "accepted", False)):
            return False
        status = getattr(decision, "status", None)
        if status is not None:
            self._show_status(status.message, status.timeout_ms)
        return True

    def _start_microscope_scan(
        self,
        configuration: MicroscopeScanConfiguration,
    ) -> None:
        preflight = microscope_scan.start_environment_decision(
            scan_running=self._microscope_scan_running(),
            serial_connected=(
                self.serial_connection is not None and self.serial_connection.is_open
            ),
        )
        if self._show_microscope_scan_start_rejection(preflight):
            return
        document = self._design_session.document
        registration = self._design_session.registration
        design_preflight = microscope_scan.start_design_decision(
            document=document,
            registration_valid=bool(
                registration is not None and registration.valid
            ),
        )
        if self._show_microscope_scan_start_rejection(design_preflight):
            return
        scale = self._active_microscope_scale()
        scale_preflight = microscope_scan.start_scale_decision(scale=scale)
        if self._show_microscope_scan_start_rejection(scale_preflight):
            return
        try:
            launch_snapshot = self._capture_microscope_scan_launch_snapshot(
                scale=scale,
                document=document,
                registration=registration,
            )
            planning_request = MicroscopeDesignScanRequest(
                bounds=tuple(float(value) for value in document.bounds),
                overlap_fraction=float(configuration.overlap_fraction),
            )
        except (AttributeError, IndexError, TypeError, ValueError):
            self._show_status("Design registration is invalid.", 6000)
            return
        self._microscope_scan_stop_requested.clear()
        if self.microscope_scan_dialog is not None:
            self.microscope_scan_dialog.set_running(True)
            self.microscope_scan_dialog.set_status("Microscope scan starting.")
        thread = threading.Thread(
            target=self._run_microscope_scan,
            args=(configuration, planning_request, launch_snapshot),
            name="MicroscopeDesignScan",
            daemon=True,
        )
        self._microscope_scan_thread = thread
        try:
            thread.start()
        except Exception as exc:
            if self._microscope_scan_thread is thread:
                self._microscope_scan_thread = None
            message = f"Microscope scan could not start: {exc}"
            if self.microscope_scan_dialog is not None:
                self.microscope_scan_dialog.set_running(False)
                self.microscope_scan_dialog.set_status(message)
            self._show_status(message, 8000)
        self._update_stage_coordinate_apply_state()

    def _run_microscope_scan(
        self,
        configuration: MicroscopeScanConfiguration,
        planning_request: object,
        launch_snapshot: _MicroscopeScanLaunchSnapshot,
    ) -> None:
        flat_field_options = getattr(
            configuration,
            "flat_field_options",
            microscope_scan.FlatFieldScanOptions(enabled=False),
        )
        camera_lock_settings = getattr(
            configuration,
            "camera_lock_settings",
            microscope_scan.CameraLockSettings(enabled=False),
        )
        request = MicroscopeScanRunRequest(
            output_dir=microscope_scan.output_dir_from_configuration(configuration),
            scale=launch_snapshot.scale,
            plan_factory=lambda frame_size, start_xy: build_microscope_scan_plan(
                planning_request,
                frame_size,
                launch=launch_snapshot,
                center_stage_xy=start_xy,
            ),
            flat_field_options=flat_field_options,
            camera_lock_settings=camera_lock_settings,
            settle_s=float(configuration.settle_s),
            tile_approach_mm=float(getattr(configuration, "tile_approach_mm", 0.0)),
            scan_pattern=str(getattr(configuration, "scan_pattern", "grid") or "grid"),
            refine_scale_from_overlaps=bool(
                getattr(configuration, "refine_scale_from_overlaps", True)
            ),
        )
        runtime = MicroscopeScanRuntime(
            stage=MicroscopeScanStageAdapter(
                reserve_task=self.stage_controller.reserve_external_task,
                read_reserved_position=self.stage_controller.run_external_current_stage_position,
                raise_action=self.stage_controller.run_external_needles_action,
                needle_feedrate=self._current_needle_feedrate,
                move_xy=self.stage_controller.run_external_move_to_xy,
                latest_position=self.stage_controller.latest_stage_position,
            ),
            camera=MicroscopeScanCameraAdapter(
                grabber=self.grabber,
                stop_event=self._microscope_scan_stop_requested,
                latest_frame_counter=self._latest_camera_counter,
                wait_for_frame=self._wait_for_camera_frame,
                latest_raw_frame_counter=self._latest_raw_camera_counter,
                wait_for_raw_frame=self._wait_for_raw_camera_frame,
                correct_lens=self._correct_camera_frame_for_active_objective,
                settings_timeout_s=self.MICROSCOPE_SCAN_CAMERA_SETTINGS_TIMEOUT_S,
            ),
            artifacts=MicroscopeScanArtifactAdapter(
                launch=launch_snapshot,
                stage_position_for_metadata=self._stage_position_for_image_metadata,
            ),
            event_sink=MicroscopeScanEventAdapter(
                status_callback=self.microscope_scan_status.emit,
                finished_callback=self.microscope_scan_finished.emit,
            ),
            session=MicroscopeScanSessionAdapter(self._optical_session_manager),
        )
        runtime.run(request)

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
            connection_flow.persist_lcr_connection_state(
                self,
                True,
                description=description,
            )
        if self.serial_connection_panel is not None:
            self.serial_connection_panel.set_lcr_connection_state(
                connected, backend_name, description
            )
        if self.resistance_panel is not None:
            self.resistance_panel.set_standby_enabled(
                self.lcr_controller.live_polling_enabled()
            )
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
        if self.resistance_panel is not None:
            self.resistance_panel.set_standby_enabled(
                self.lcr_controller.live_polling_enabled()
            )

    def _resume_resistance_standby_polling(self) -> None:
        lcr_controller = getattr(self, "lcr_controller", None)
        if (
            lcr_controller is not None
            and lcr_controller.live_polling_enabled()
        ):
            lcr_controller.set_live_polling_enabled(True)

    def _cancel_contact_seek(self) -> None:
        self._contact_seek_stop_requested.set()
        self.stage_controller.cancel_active_motion("Contact seek cancel requested.")
        self._show_status("Contact seek cancel requested.")

    def _contact_seek_measure_quality(self, count: int):
        raw_batch = self.lcr_controller.read_route_measurement_batch_now(int(count))
        samples = tuple(
            route_measurement_sample_from_raw(raw, index)
            for index, raw in enumerate(raw_batch, start=1)
        )
        return summarize_route_contact_quality(samples)

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

    def _sample_handling_active(self) -> bool:
        thread = getattr(self, "_sample_handling_thread", None)
        return thread is not None and thread.is_alive()

    def _latest_stage_z(self) -> float | None:
        return sample_handling.latest_stage_z(
            self.stage_controller.latest_stage_position()
        )

    def _active_sample_objective_name(self) -> str:
        try:
            objectives = self.settings_manager.objectives_configuration()
            raw_name = getattr(objectives, "active_name", "")
        except Exception:
            logger.debug("Unable to read active objective for sample focus.", exc_info=True)
            raw_name = ""
        return sample_handling.active_sample_objective_name(raw_name)

    def _remember_sample_focus_from_latest(self) -> float | None:
        return sample_handling.remember_sample_focus(
            self._sample_focus_cache(),
            raw_objective_name=self._active_sample_objective_name(),
            latest_position=self.stage_controller.latest_stage_position(),
        )

    def _sample_load_focus_z(self) -> float | None:
        return sample_handling.sample_load_focus_z(
            self._sample_focus_cache(),
            raw_objective_name=self._active_sample_objective_name(),
            latest_position=self.stage_controller.latest_stage_position(),
        )

    def _sample_focus_cache(self) -> dict[str, float]:
        focus_by_objective = getattr(self, "_last_sample_focus_z_by_objective", None)
        if focus_by_objective is None:
            focus_by_objective = {}
            self._last_sample_focus_z_by_objective = focus_by_objective
        return focus_by_objective

    def _sample_workflow_can_start(self, action: str) -> bool:
        decision = sample_handling.sample_start_decision(
            action,
            stage_ready=self._stage_serial_ready(),
            sample_active=self._sample_handling_active(),
            cancelable_operation=stage_move_lifecycle.has_cancelable_operation(self),
        )
        if not decision.accepted:
            self._show_status(decision.status_message, 4000)
            return False
        return True

    def _design_registration_is_active(self) -> bool:
        return sample_handling.design_registration_is_active(
            getattr(self, "_design_session", None)
        )

    def _run_sample_unload(
        self,
        xy_feedrate: float,
        needle_feedrate: float,
    ) -> None:
        sample_handling.run_sample_unload(
            self.stage_controller,
            xy_feedrate=xy_feedrate,
            needle_feedrate=needle_feedrate,
            emit_status=self.sample_handling_status.emit,
            emit_finished=self.sample_handling_finished.emit,
            unload_x_mm=self.SAMPLE_UNLOAD_X_MM,
            unload_y_mm=self.SAMPLE_UNLOAD_Y_MM,
        )

    def _run_sample_load(
        self,
        focus_z_mm: float | None,
        xy_feedrate: float,
        focus_feedrate: float,
        needle_feedrate: float,
    ) -> None:
        sample_handling.run_sample_load(
            self.stage_controller,
            focus_z_mm=focus_z_mm,
            xy_feedrate=xy_feedrate,
            focus_feedrate=focus_feedrate,
            needle_feedrate=needle_feedrate,
            emit_status=self.sample_handling_status.emit,
            emit_finished=self.sample_handling_finished.emit,
            load_x_mm=self.SAMPLE_LOAD_X_MM,
            load_y_mm=self.SAMPLE_LOAD_Y_MM,
        )


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
        self.settings_manager.replace_and_save(
            settings,
            preserve_exposure_policy=True,
        )


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
