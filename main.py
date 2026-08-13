"""Application entry point for the probe station GUI."""

from __future__ import annotations

import logging
import csv
import json
import math
import os
import threading
import time
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
import sys
from typing import Any, TYPE_CHECKING

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
    QThread,
    QTimer,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QIcon,
    QImage,
)
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
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
)
from probe_station_gui.coordinates import (
    CoordinateFrameStoreWorker,
    PhysicalMachinePose,
)
from probe_station_gui.coordinates.application_runtime import (
    create_application_coordinate_runtime,
)
from probe_station_gui.coordinates.coordinator_model import (
    AutofocusResult as CoordinateAutofocusResult,
    CoordinateAdapterCompletion,
    CoordinateMotionLease,
    DesignCoordinateLease,
    FocusCandidateRequest,
    FocusMoveResult,
    FocusReferenceRequest,
    FocusReferenceResetRequest,
    FirstContactRequest,
    MachinePoseCaptureResult,
    PhysicalAReadResult,
    ReadPhysicalAIntent,
    RegistrationCaptureRequest,
    RegistrationCheckMarkRequest,
    RegistrationInvalidationRequest,
    RegistrationOpticalObservation,
    RegistrationSourceMarkRequest,
)
from probe_station_gui.design.focus_candidate import select_central_focus_candidate
from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    StructureBoundsFailure,
    StructureBoundsRequest,
    StructureBoundsResult,
)
from probe_station_gui.design.klayout_structure_bounds_worker import (
    KLayoutStructureBoundsWorker,
)
from probe_station_gui.design.frame_registration import (
    DesignFrameMetadata,
)
from probe_station_gui.design.model import DesignModelError
from probe_station_gui.design import (
    navigation_targeting,
    route_editing,
)
from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.markup_store import (
    MarkupStoreWorker,
)
from probe_station_gui.design.session import DesignSession
from probe_station_gui.design.session_registration import AlignmentPreparation
from probe_station_gui.shared.diagnostics import configure_crash_diagnostics
from probe_station_gui.api.request_bridge import ApiRequestBridge
from probe_station_gui.api.stage_command_runtime import ApiStageCommandRuntime
from probe_station_gui.api.server import ProbeStationApiServer
from probe_station_gui.api.keys import API_KEY_FILENAME, ApiKeyStore
from probe_station_gui.camera.api_control import (
    CameraApiBroker,
    connect_camera_api_results,
)
from probe_station_gui.camera.auto_exposure import (
    CameraAutoExposureController,
)
from probe_station_gui.camera.live_correction import (
    LatestFrameProcessor,
    LiveCameraCorrectionPipeline,
)
from probe_station_gui.camera.flat_field_calibration import FlatFieldCalibrationStore
from probe_station_gui.instruments.meters.lcr import LCRMeterController
from probe_station_gui.instruments.meters.lcr_session_backend import LCRMeterError
from probe_station_gui.route.meter_config import (
    ROUTE_METER_GWINSTEK,  # noqa: F401 - imported for callers/tests
    ROUTE_METER_KEITHLEY,  # noqa: F401 - imported for callers/tests
    ROUTE_METER_KEITHLEY_2400,  # noqa: F401 - imported for callers/tests
)
from probe_station_gui.stage.coordinate_targets import (
    CoordinateTargetConfig,
    CoordinateTargetMoveState,
    normalize_api_coordinate_input_mode,
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
from probe_station_gui.stage import move_lifecycle as stage_move_lifecycle
from probe_station_gui.stage import position_update as stage_position_update
from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox
from probe_station_gui.stage.controller import StageControllerError
from probe_station_gui.views import (
    main_window_coordinate_entry as coordinate_entry,
    main_window_stage_position_panel as stage_position_panel_adapter,
)
from probe_station_gui.views.stage_position_panel import (
    StagePositionPanel,
)
from probe_station_gui.views.microscope_interaction import (
    ClickMoveBindings,
    ClickMoveConfig,
)
from probe_station_gui.design import objective_offsets as offsets
from probe_station_gui.route.model import (
    MeasurementRoute,
)
from probe_station_gui.route.measurement import (
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
from probe_station_gui.route.control_state import (
    ApiRouteControlState,
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
    restart_waiting_route_measurement,
    route_measurement_setup_changed,
    route_measurement_session_cancel_plan,
    route_measurement_session_start_plan,
)
from probe_station_gui.route.api_artifacts import (
    final_api_route_session_status,
)
from probe_station_gui.route.session_start import (
    GuiRouteLaunchState,
    GuiRouteStartPreflight,
    gui_route_camera_frame_preflight,
    gui_route_launch_state,
    gui_route_start_availability,
    gui_route_start_preflight,
    snapshot_route_design_frame,
)
from probe_station_gui.route.finish_flow import (
    route_finish_outcome_plan,
    route_finish_signal_plan,
)
from probe_station_gui.route.telegram_adapter import (
    combine_telegram_contact_photos,
    capture_route_photo,
    route_finish_telegram_payload,
    route_photo_focus_payload,
    route_start_telegram_text,
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
from probe_station_gui.camera.flat_field_processing import (
    apply_flat_field_correction,  # noqa: F401 - retained test/patch seam
)
from probe_station_gui.camera.imaging import (
    utc_timestamp,
)
from probe_station_gui.camera.microscope_artifacts import save_microscope_image
from probe_station_gui.camera.distortion import (
    DistortionCorrection,
)
from probe_station_gui.camera import microscope_scan
from probe_station_gui.camera.microscope_scan_runtime import (
    MicroscopeScanRunRequest,
    MicroscopeScanRuntime,
)
from probe_station_gui.camera.microscope_scan_runtime_adapters import (
    MicroscopeScanArtifactAdapter,
    MicroscopeScanCameraAdapter,
    MicroscopeDesignScanRequest,
    MicroscopeScanEventAdapter,
    MicroscopeScanSessionAdapter,
    MicroscopeScanStageAdapter,
    build_microscope_scan_plan,
)
from probe_station_gui.settings.manager import (
    SettingsManager,
)
from probe_station_gui.settings.dialog_transaction import (
    SettingsDialogTransaction,
)
from probe_station_gui.settings.software_coordinate_selection_store import (
    SoftwareCoordinateSelectionStoreWorker,
)
from probe_station_gui.settings.objective_config import (
    normalize_objective_name,
)
from probe_station_gui.notifications.telegram import (
    telegram_route_attention_alert_enabled,
)
from probe_station_gui.notifications.telegram_runtime import (
    TelegramCommandRuntime,
)
from probe_station_gui.views.alignment_panel import AlignmentPanel
from probe_station_gui.views.contact_oscillation_window import (
    ContactOscillationWindow,
)
from probe_station_gui.dialogs.click_calibration_dialog import ClickCalibrationDialog
from probe_station_gui.dialogs.lens_distortion_dialog import LensDistortionDialog
from probe_station_gui.dialogs.optical_calibration_wizard import (
    OpticalCalibrationWizard,
)
from probe_station_gui.views.dock_widgets import CollapsibleDockWidget
from probe_station_gui.views.oscillation_panel import OscillationPanel
from probe_station_gui.views.resistance_monitor_panel import ResistanceMonitorPanel
from probe_station_gui.views.serial_connection_panel import SerialConnectionPanel
from probe_station_gui.views.main_window_auxiliary import (
    toggle_design_layout_window,
)
from probe_station_gui.views.main_window_docks import create_main_window_docks
from probe_station_gui.views.main_window_menus import setup_main_window_menus
from probe_station_gui.views import (
    main_window_connection_flow as connection_flow,
    main_window_coordinate_flow as coordinate_flow,
    main_window_needle_calibration as needle_calibration_ui,
)
from probe_station_gui.views import main_window_shutdown as shutdown_ui
from probe_station_gui.application.bootstrap_api import _MainBootstrapApiMixin
from probe_station_gui.application.api_stage_contact import (
    _MainApiStageContactMixin,
)
from probe_station_gui.application.api_meter_visa import _MainApiMeterVisaMixin
from probe_station_gui.application.api_route_scan import _MainApiRouteScanMixin
from probe_station_gui.application.camera_pipeline import (
    _MainCameraPipelineMixin,
    _MicroscopeScanLaunchSnapshot,
)
from probe_station_gui.application.status_coordinate_ui import (
    _MainStatusCoordinateUiMixin,
)
from probe_station_gui.application.settings_apply import _MainSettingsApplyMixin
from probe_station_gui.application.objective_tools import _MainObjectiveToolsMixin
from probe_station_gui.application.optical_calibration import (
    _MainOpticalCalibrationMixin,
)
from probe_station_gui.application.alignment import (
    _MainAlignmentMixin,
    _ManualAlignmentCaptureContext,
)
from probe_station_gui.application.manual_jog import _MainManualJogMixin
from probe_station_gui.application.motion_prediction import (
    _MainMotionPredictionMixin,
)
from probe_station_gui.application.design_load import (
    _MainDesignLoadMixin,
    _PendingDesignMarkupLoad,
)
from probe_station_gui.application.design_markup import (
    _MainDesignMarkupMixin,
)
from probe_station_gui.application.design_edit_dialog import (
    _MainDesignEditDialogMixin,
)

_startup_trace("application imports done")


logger = logging.getLogger(__name__)

APP_ICON_RESOURCE = "assets/app_icon.ico"
WINDOWS_APP_USER_MODEL_ID = "ProbeStationGUI.ProbeStationGUI"


@dataclass
class _DesignContactArmDispatch:
    request: FirstContactRequest
    read_intent: ReadPhysicalAIntent | None = None


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
    from probe_station_gui.views.design_layout_window import DesignLayoutWindow
    from probe_station_gui.views.design_navigator_panel import DesignNavigatorPanel


class Main(
    _MainBootstrapApiMixin,
    _MainApiStageContactMixin,
    _MainApiMeterVisaMixin,
    _MainApiRouteScanMixin,
    _MainCameraPipelineMixin,
    _MainStatusCoordinateUiMixin,
    _MainSettingsApplyMixin,
    _MainObjectiveToolsMixin,
    _MainOpticalCalibrationMixin,
    _MainAlignmentMixin,
    _MainManualJogMixin,
    _MainMotionPredictionMixin,
    _MainDesignLoadMixin,
    _MainDesignMarkupMixin,
    _MainDesignEditDialogMixin,
    QMainWindow,
):
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
    design_registration_autofocus_finished: Signal = Signal(
        object,
        bool,
        object,
        str,
    )
    design_registration_focus_move_finished: Signal = Signal(
        object,
        object,
        bool,
        str,
    )
    design_contact_a_read_finished: Signal = Signal(object)
    design_contact_arm_requested: Signal = Signal(object)
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
    MICROSCOPE_AREA_SCAN_STITCH_DEBUG_STRUCTURE_MM = 1.0
    MICROSCOPE_AREA_SCAN_STITCH_DEBUG_PLACEMENT_FRACTION = 1.0
    MICROSCOPE_AREA_SCAN_STITCH_DEBUG_OVERLAP_FRACTION = 0.25
    MICROSCOPE_SCAN_CAMERA_SETTINGS_TIMEOUT_S = 5.0
    FOCUS_STRUCTURE_WORKER_SHUTDOWN_TIMEOUT_S = 0.25
    CONTACT_SEEK_STEP_MM = (
        needle_calibration_ui.manual_contact_seek.DEFAULT_MANUAL_CONTACT_SEEK_STEP_MM
    )
    CONTACT_SEEK_MAX_TOTAL_MM = needle_calibration_ui.manual_contact_seek.DEFAULT_MANUAL_CONTACT_SEEK_MAX_TOTAL_MM
    CONTACT_SEEK_QUICK_COUNT = needle_calibration_ui.manual_contact_seek.DEFAULT_MANUAL_CONTACT_SEEK_QUICK_COUNT
    CONTACT_SEEK_CONFIRM_COUNT = needle_calibration_ui.manual_contact_seek.DEFAULT_MANUAL_CONTACT_SEEK_CONFIRM_COUNT
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
        self._software_coordinate_selection_store = (
            SoftwareCoordinateSelectionStoreWorker(
                self.settings_manager.persist_software_coordinate_selection,
                self,
            )
        )
        self._software_coordinate_selection_store.failed.connect(
            self._on_software_coordinate_selection_store_failed
        )
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
        self._api_stage_command_runtime = ApiStageCommandRuntime(
            lambda command_request: self._dispatch_api_command_request(
                command_request,
                apply_route_control_guard=False,
            )
        )
        self._api_settings_signature: tuple[bool, str, int] | None = None
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
        self._manual_alignment_pick_generation = 0
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
        self._pending_coordinate_motion_lease: CoordinateMotionLease | None = None
        self._exact_step_accumulator = ExactStepAccumulator(self.STAGE_AXIS_NAMES)
        self._exact_step_pending_axes: set[str] = set()
        self._exact_step_motion_lease: CoordinateMotionLease | None = None
        self._exact_step_pose_rebase_allowed = False
        self._exact_step_window_elapsed = False
        self._stage_position_panel: StagePositionPanel | None = None
        self._latest_physical_machine_pose: PhysicalMachinePose | None = None
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
        self._route_measurement_optical_session_token: str | None = None
        self._route_measurement_point_numbers: list[int] = []
        self._route_measurement_current_point: int | None = None
        self._last_route_measurement_result: (
            tuple[
                RouteMeasurementRecord,
                int,
                int,
                bool,
            ]
            | None
        ) = None
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
        self._contact_seek_thread: threading.Thread | None = None
        self._contact_seek_stop_requested = threading.Event()
        software_coordinates = self.settings_manager.settings.software_coordinates
        coordinate_runtime = create_application_coordinate_runtime(
            restore_frame_id=software_coordinates.last_selected_frame_id,
        )
        self._coordinate_runtime = coordinate_runtime
        self._design_session = DesignSession()
        self._coordinate_system_coordinator = coordinate_runtime.coordinator
        self._settings_dialog_transaction = SettingsDialogTransaction(
            self.settings_manager,
            self._coordinate_system_coordinator,
            publish_notice=self._show_status,
            publish_transition=lambda transition: (
                coordinate_flow.apply_coordinate_transition(
                    self,
                    transition,
                )
            ),
        )
        self._coordinate_frame_store = CoordinateFrameStoreWorker(
            self,
            path=self.settings_manager.coordinate_frames_path(),
        )
        self._active_design_frame_metadata: DesignFrameMetadata | None = None
        self._active_route_design_frame_snapshot = None
        self._focus_structure_bounds_worker: KLayoutStructureBoundsWorker | None = None
        self._focus_structure_request_id = 0
        self._pending_focus_structure_request_id: int | None = None
        self._pending_focus_structure_context: object | None = None
        self._pending_focus_structure_fov: tuple[float, float] | None = None
        self._pending_focus_structure_design_bounds: (
            tuple[float, float, float, float] | None
        ) = None
        self._design_focus_signals_connected = False
        self._coordinate_frame_store.loaded.connect(
            self._on_coordinate_frame_document_loaded
        )
        self._coordinate_frame_store.saved.connect(
            self._on_coordinate_frame_document_saved
        )
        self._coordinate_frame_store.failed.connect(
            self._on_coordinate_frame_store_failed
        )
        coordinate_flow.request_coordinate_frame_load(self)
        self.statusBar()
        self._objective_widget = self._create_objective_widget()
        self.statusBar().addPermanentWidget(self._objective_widget, 0)
        self._stage_position_widget = (
            stage_position_panel_adapter.create_stage_position_widget(self)
        )
        self.statusBar().addPermanentWidget(self._stage_position_widget, 0)
        self._status_log = QPlainTextEdit(self)
        self._status_log.setReadOnly(True)
        self._status_log.setMaximumHeight(80)
        self._status_log.setLineWrapMode(QPlainTextEdit.NoWrap)
        self._status_log.document().setMaximumBlockCount(200)
        self.statusBar().addPermanentWidget(self._status_log, 1)
        self._status_log_path = self.settings_manager.log_file_path().with_name(
            "status-history.log"
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
        self.grabber.frame_gap_suppressed.connect(self._on_camera_frame_gap_suppressed)
        self.grabber.error.connect(self.on_error)
        self.design_layout_module_ready.connect(self._on_design_layout_module_ready)
        self.design_document_loaded.connect(self._on_design_document_loaded)
        self.route_measurement_started.connect(self._on_route_measurement_started)
        self.route_measurement_status.connect(self._on_route_measurement_status)
        self.route_measurement_progress.connect(self._on_route_measurement_progress)
        self.route_measurement_waiting_changed.connect(
            self._on_route_measurement_waiting_changed
        )
        self.route_measurement_result.connect(self._on_route_measurement_result)
        self.route_measurement_recorded.connect(self._on_route_measurement_recorded)
        self.route_measurement_finished.connect(self._on_route_measurement_finished)
        self.route_contact_move_finished.connect(self._on_route_contact_move_finished)
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
        self._telegram_runtime = TelegramCommandRuntime(
            request_publisher=self.telegram_bot_request_received.emit,
            command_snapshot_provider=self._telegram_command_snapshot,
            status_snapshot_provider=self._telegram_status_snapshot,
            latest_camera_photo_provider=self._latest_camera_frame_photo,
            route_action_submitter=self._submit_route_measurement_confirmation,
            api_route_confirmation_provider=lambda: (
                self._api_route_control_state_snapshot().accepts_route_confirmation
            ),
            settings_provider=self.settings_manager.telegram_configuration,
        )
        self.telegram_bot_request_received.connect(self._telegram_runtime.handle_on_gui)
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
        self.design_registration_autofocus_finished.connect(
            self._on_registration_focus_autofocus_finished
        )
        self.stage_controller.machine_coordinate_snapshot_finished.connect(
            self._on_registration_machine_coordinate_snapshot_finished,
            Qt.ConnectionType.QueuedConnection,
        )
        self.design_registration_focus_move_finished.connect(
            self._on_registration_focus_move_signal
        )
        self.design_contact_a_read_finished.connect(
            self._on_design_contact_a_read_finished,
            Qt.ConnectionType.QueuedConnection,
        )
        self.design_contact_arm_requested.connect(
            self._on_design_contact_arm_requested,
            Qt.ConnectionType.BlockingQueuedConnection,
        )
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
        self.stage_controller.needle_height_changed.connect(
            self._on_needle_height_changed
        )
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

    def showEvent(self, event) -> None:  # type: ignore[override]
        super().showEvent(event)
        QTimer.singleShot(0, self._prime_keyboard_focus)

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
            design_frame_snapshot=start_plan.design_frame_snapshot,
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
            self._telegram_runtime.send_alert(
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
        design_frame_snapshot: object | None = None,
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
            design_frame_snapshot=design_frame_snapshot,
            post_success_contact=self._design_contact_success_callback(
                design_frame_snapshot
            ),
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
        self._telegram_runtime.route_photos.reset_for_route_start()
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
            self._telegram_runtime.send_alert(
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
        frame_usability = self._coordinate_system_coordinator.current_design_lease()
        if not frame_usability.usable:
            self._show_status(
                str(
                    frame_usability.rejection_reason
                    or "Design coordinate frame is unavailable."
                ),
                6000,
            )
            return None
        route = self._design_session.route
        frame_snapshot = self._snapshot_active_route_design_frame(frame_usability)
        decision = route_measurement_start_decision(
            route=route,
            registration_valid=frame_usability.usable,
            points_factory=lambda selected_route: self._route_measurement_points(
                selected_route,
                frame_usability_snapshot=frame_usability,
            ),
            current_point=configuration.current_point,
            previous_ok_only=configuration.previous_ok_only,
            previous_csv_path=configuration.previous_csv_path,
            structure_number_for_point=self._api_structure_number_for_measurement_point,
            design_frame_snapshot=frame_snapshot,
        )
        if (
            decision.accepted
            and not self._coordinate_system_coordinator.design_lease_is_current(
                frame_usability
            )
        ):
            self._show_status(
                "Design coordinate frame changed before route start.",
                6000,
            )
            return None
        if decision.accepted:
            self._active_route_design_frame_snapshot = (
                decision.plan.design_frame_snapshot
                if decision.plan is not None
                else None
            )
            return decision.plan
        if decision.dialog_status:
            self._show_route_runtime_status(decision.message, decision.timeout_ms)
        else:
            self._show_status(decision.message, decision.timeout_ms)
        return None

    def _snapshot_active_route_design_frame(
        self,
        usability: DesignCoordinateLease | None = None,
    ):
        active = usability or self._coordinate_system_coordinator.current_design_lease()
        if not active.usable:
            return None
        return snapshot_route_design_frame(
            frame_id=active.frame_id,
            frame_version=active.frame_version,
        )

    def _design_contact_success_callback(self, frame_snapshot: object | None):
        frame_id = getattr(frame_snapshot, "frame_id", None)
        frame_version = getattr(frame_snapshot, "frame_version", None)
        if frame_id is None or frame_version is None:
            return None
        read = Main._request_design_contact_arm(
            self, FirstContactRequest(str(frame_id), int(frame_version))
        )
        if read is None:
            return None

        def capture(_placement: object):
            try:
                coordinates = self.stage_controller.run_external_current_physical_machine_coordinates(
                    ("A",)
                )
                physical_a = float(coordinates["A"])
                result = PhysicalAReadResult(
                    read.intent_id,
                    succeeded=True,
                    physical_a_mm=physical_a,
                )
            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                logger.exception(
                    "Unable to read physical A for Design contact reference"
                )
                result = PhysicalAReadResult(
                    read.intent_id,
                    succeeded=False,
                    message=str(exc),
                )

            def finalize() -> None:
                self.design_contact_a_read_finished.emit(result)

            return finalize

        return capture

    def _request_design_contact_arm(
        self,
        request: FirstContactRequest,
    ) -> ReadPhysicalAIntent | None:
        signal = getattr(self, "design_contact_arm_requested", None)
        application = QApplication.instance()
        if (
            signal is None
            or application is None
            or QThread.currentThread() == application.thread()
        ):
            return Main._arm_design_contact_on_gui(self, request)
        dispatch = _DesignContactArmDispatch(request)
        signal.emit(dispatch)
        return dispatch.read_intent

    def _on_design_contact_arm_requested(
        self,
        dispatch: object,
    ) -> None:
        if not isinstance(dispatch, _DesignContactArmDispatch):
            return
        dispatch.read_intent = Main._arm_design_contact_on_gui(
            self,
            dispatch.request,
        )

    def _arm_design_contact_on_gui(
        self,
        request: FirstContactRequest,
    ) -> ReadPhysicalAIntent | None:
        transition = self._coordinate_system_coordinator.arm_first_contact(request)
        coordinate_flow.apply_coordinate_transition(self, transition)
        return next(
            (
                intent
                for intent in transition.intents
                if isinstance(intent, ReadPhysicalAIntent)
            ),
            None,
        )

    def _on_design_contact_a_read_finished(self, result: object) -> None:
        if not isinstance(result, PhysicalAReadResult):
            return
        completed = self._coordinate_system_coordinator.complete(
            CoordinateAdapterCompletion(result.intent_id, result)
        )
        coordinate_flow.apply_coordinate_transition(self, completed)

    def _route_measurement_points(
        self,
        route: MeasurementRoute,
        *,
        frame_usability_snapshot: DesignCoordinateLease | None = None,
    ) -> list[RouteMeasurementPoint]:
        usability = (
            frame_usability_snapshot
            or self._coordinate_system_coordinator.current_design_lease()
        )
        if not usability.usable:
            raise DesignModelError(
                usability.rejection_reason or "Design coordinate frame is unavailable."
            )
        objective_settings = self.settings_manager.objectives_configuration()
        base_offset, active_offset = offsets.base_and_active_objective_offsets(
            objective_settings
        )
        return route_measurement_points_for_route(
            route,
            stage_from_design=lambda design_xy: (
                self._coordinate_system_coordinator.project_design_to_camera_stage(
                    usability,
                    design_xy,
                )
            ),
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
            parent_token=self._route_optical_session_token(),
        )

    def _route_optical_session_token(self) -> str:
        token = str(getattr(self, "_route_measurement_optical_session_token", "") or "")
        if not token:
            raise RuntimeError("Route optical session is unavailable.")
        return token

    def _record_route_photo(
        self,
        record: RoutePhotoRecord,
        position: int,
        total: int,
    ) -> None:
        self._telegram_runtime.route_photos.record_route_photo(
            record,
            position,
            total,
            route_name=self._current_route_name(),
            send_bot_message=self._telegram_runtime.send_bot_message,
            default_markup=(
                self._telegram_runtime.default_markup(
                    route_waiting=self._route_measurement_waiting
                )
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
        self._telegram_runtime.route_photos.capture_pre_contact_photo(
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
        self._telegram_runtime.route_photos.capture_contact_photo(
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
        success = False
        message = "Route measurement failed."
        self._route_measurement_optical_session_token = None
        try:
            if runner.requires_optical_session():
                with self._optical_session_manager.open(
                    "route photography"
                ) as optical_session:
                    self._route_measurement_optical_session_token = (
                        optical_session.token
                    )
                    success, message = runner.run()
            else:
                success, message = runner.run()
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            self.route_measurement_status.emit(f"Route measurement failed: {message}")
        finally:
            self._route_measurement_optical_session_token = None
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
        self._route_runtime_presenter().route_started(
            message, total_points, waiting=waiting, waiting_reason=waiting_reason
        )
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

    def _request_route_measurement_point_correction(
        self, pending_point_number: int | None = None
    ) -> None:
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
                show_status=lambda message, timeout_ms=5000: (
                    self._show_status(message, timeout_ms),
                    self._route_runtime_presenter().set_status(message),
                ),
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
            message = str(
                context_result.get("message") or "Route contact move rejected."
            )
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

    def _run_route_contact_move(
        self, point: RouteMeasurementPoint, needle_feedrate: float | None
    ) -> None:
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

    def _route_shift_save_plan(
        self, runner: object | None, point_number: int | None
    ) -> RouteShiftSavePlan:
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

    def _route_shift_adjustment_point(
        self, shift_plan: RouteShiftSavePlan, runner: object | None
    ) -> tuple[bool, RouteMeasurementPoint | None]:
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
                self._show_route_runtime_status(
                    position_plan.message, position_plan.timeout_ms
                )
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

    def _apply_route_shift_save_status(
        self, status_plan: RouteShiftSaveStatusPlan
    ) -> None:
        message = status_plan.message
        self._show_status(message, status_plan.timeout_ms)
        self._route_runtime_presenter().shift_status(
            message, mark_interrupt_pending=status_plan.mark_interrupt_pending
        )

    def _interrupt_route_measurement_runner(
        self, runner: object, *, reason: str
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
        self._submit_route_measurement_confirmation(f"jump:{int(pending_point_number)}")

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
        pending_contact_photos = (
            self._telegram_runtime.route_photos.take_pending_contact_photos()
        )
        if pending_contact_photos is not None:
            before_photo, after_photo = pending_contact_photos
            photo, caption = telegram_contact_photo_payload(
                before_photo,
                after_photo,
                combine_photos=lambda before_bytes, after_bytes: (
                    combine_telegram_contact_photos(
                        before_bytes,
                        after_bytes,
                        encode_image=Main._qimage_telegram_photo,
                    )
                ),
            )
            if before_photo is not None and photo[1] != "route-contact-comparison.jpg":
                logger.warning("Unable to combine route contact photos for Telegram.")
            self._telegram_runtime.send_bot_message(
                caption,
                photo=photo,
                reply_markup=self._telegram_runtime.default_markup(
                    route_waiting=self._route_measurement_waiting
                ),
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
        def build_contact_photo_payload(
            before_photo: tuple[bytes, str, str] | None,
            after_photo: tuple[bytes, str, str],
        ) -> tuple[tuple[bytes, str], str]:
            photo, caption = telegram_contact_photo_payload(
                before_photo,
                after_photo,
                combine_photos=lambda before_bytes, after_bytes: (
                    combine_telegram_contact_photos(
                        before_bytes,
                        after_bytes,
                        encode_image=Main._qimage_telegram_photo,
                    )
                ),
            )
            if before_photo is not None and photo[1] != "route-contact-comparison.jpg":
                logger.warning("Unable to combine route contact photos for Telegram.")
            return photo, caption

        self._telegram_runtime.route_photos.send_route_attention_alert(
            message,
            include_contact_photos=include_contact_photos,
            failure_photos=(
                self._telegram_runtime.route_photos.latest_contact_failure_photos()
                if include_contact_photos
                else None
            ),
            contact_photo_payload=build_contact_photo_payload,
            send_alert=self._telegram_runtime.send_alert,
            route_actions_markup=self._telegram_runtime.route_actions_markup(),
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
        reason_text = (
            ", ".join(str(reason) for reason in reasons) if reasons else "none"
        )
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
            self._telegram_runtime.send_alert(
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
        status = final_api_route_session_status(
            getattr(self, "_api_route_session_id", None), runner, logger=logger
        )
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
        self._telegram_runtime.route_photos.clear_for_route_finish()

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
        plan = route_editing.select_route_point_for_measurement(
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
        if not self._route_runtime_presenter().set_measurement_session_active(
            bool(pending)
        ):
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
        plan = route_editing.select_route_point(self._design_session, index)
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
        transition = self._coordinate_system_coordinator.set_registration_source_mark(
            RegistrationSourceMarkRequest((x_value, y_value))
        )
        coordinate_flow.apply_coordinate_transition(self, transition)
        self._refresh_design_panel()
        self._show_status(
            f"Design source mark captured at X={x_value:.3f}, Y={y_value:.3f}.",
            4000,
        )

    def _add_design_check_mark(self, x_value: float, y_value: float) -> None:
        if not self._design_mutation_ready():
            return
        transition = self._coordinate_system_coordinator.add_registration_check_mark(
            RegistrationCheckMarkRequest((x_value, y_value))
        )
        coordinate_flow.apply_coordinate_transition(self, transition)
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
            pivot_value = self._rotation_geometry_snapshot().pivot_machine_xy
            pivot = (float(pivot_value[0]), float(pivot_value[1]))
            camera_origin = self._camera_stage_xy_from_raw_stage_xy((0.0, 0.0))
            objective_offset = (
                -float(camera_origin[0]),
                -float(camera_origin[1]),
            )
        except (DesignModelError, TypeError, ValueError) as exc:
            self._show_status(str(exc), 6000)
            return
        transition = self._coordinate_system_coordinator.capture_registration_mark(
            RegistrationCaptureRequest(
                pivot_machine_xy=pivot,
                objective_xy_offset=objective_offset,
                check_mark=check_mark,
            )
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _on_registration_machine_coordinate_snapshot_finished(
        self,
        request_id: object,
        success: bool,
        snapshot: object,
        message: str,
    ) -> None:
        if not isinstance(request_id, int):
            return
        transition = self._coordinate_system_coordinator.complete(
            CoordinateAdapterCompletion(
                intent_id=request_id,
                result=MachinePoseCaptureResult(
                    request_id,
                    succeeded=bool(success),
                    snapshot=snapshot,
                    message=str(message or ""),
                    active_operator_pick_slot=getattr(
                        self,
                        "_manual_alignment_pick_slot",
                        None,
                    ),
                    active_operator_pick_generation=getattr(
                        self,
                        "_manual_alignment_pick_generation",
                        None,
                    ),
                ),
            )
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _design_spacing_ratio_is_reasonable(self, ratio: float) -> bool:
        return abs(float(ratio) - 1.0) <= self.DESIGN_SPACING_RATIO_TOLERANCE

    def _clear_design_registration(self) -> None:
        transition = coordinate_flow.activate_current_design(self, create_new=True)
        if transition is None or not transition.accepted:
            return
        self._pending_alignment_preparation = None
        self._last_selected_design_point = None
        self._set_design_snap_enabled(True)
        self._refresh_design_panel()
        self._refresh_design_position()
        self._show_status("Design calibration restarted.", 4000)

    def _design_registration_instances(self) -> tuple[tuple[str, str], ...]:
        return self._coordinate_system_coordinator.snapshot().registration.registration_instances

    def _reconcile_missing_design_registration_instance(self) -> None:
        snapshot = self._coordinate_system_coordinator.snapshot().registration
        if (
            snapshot.active_frame_id
            == self._coordinate_system_coordinator.current_design_lease().frame_id
        ):
            return
        coordinate_flow.activate_current_design(self)

    def _select_design_registration_instance(self, frame_id: str) -> None:
        if not self._design_edit_safe():
            self._show_status("Design editing is locked.", 4000)
            return
        selected_id = str(frame_id).strip()
        if (
            selected_id
            == self._coordinate_system_coordinator.snapshot().registration.active_frame_id
        ):
            return
        transition = coordinate_flow.activate_current_design(
            self,
            requested_frame_id=selected_id,
        )
        if transition is None or not transition.accepted or transition.notices:
            return
        self._pending_alignment_preparation = None
        self._last_selected_design_point = None
        self._clear_design_focus_overlay_state()
        self._refresh_design_panel()
        self._refresh_design_position()
        self._show_status("Registration selected.", 4000)

    def _new_design_registration_instance(self) -> None:
        if not self._design_edit_safe():
            self._show_status("Design editing is locked.", 4000)
            return
        self._clear_design_registration()

    def _invalidate_design_registration(self, reason: str) -> None:
        self._pending_alignment_preparation = None
        transition = self._coordinate_system_coordinator.invalidate_registration(
            RegistrationInvalidationRequest(reason)
        )
        coordinate_flow.apply_coordinate_transition(self, transition)
        if self._design_session.document is not None:
            self._set_design_snap_enabled(True)

    def _on_design_target_selected(self, target_id: str) -> None:
        navigation_targeting.select_design_target(self._design_session, target_id)
        self._refresh_design_panel()

    def _select_next_design_target(self) -> None:
        plan = navigation_targeting.select_next_design_target(self._design_session)
        self._refresh_design_panel()
        if plan.status_message is not None:
            self._show_status(plan.status_message, plan.status_timeout_ms)

    def _select_previous_design_target(self) -> None:
        plan = navigation_targeting.select_previous_design_target(self._design_session)
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
        plan = navigation_targeting.plan_design_target_move(
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
        self,
        design_xy: tuple[float, float],
        *,
        source_label: str,
        move_request: object | None = None,
    ) -> bool:
        document = self._design_session.document
        stage_xy = (
            self._raw_stage_xy_from_design_xy(design_xy)
            if document is not None
            else None
        )
        plan = navigation_targeting.plan_design_coordinate_move(
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
        request = move_request or self.stage_controller.request_move_to_xy
        started = request(plan.stage_xy[0], plan.stage_xy[1])
        if started is False:
            self._pending_planned_move_target_xy = None
            self._pending_planned_move_source_label = None
            return False
        logger.debug(
            "DESIGN MOVE source=%s design=(%.3f, %.3f) stage=(%.3f, %.3f)",
            plan.source_label,
            plan.design_xy[0],
            plan.design_xy[1],
            plan.stage_xy[0],
            plan.stage_xy[1],
        )
        return True

    def _find_design_focus_reference(self) -> None:
        document = self._design_session.document
        fov_size = self._resolve_design_fov_size()
        if document is None or fov_size is None:
            self._show_status("Current field of view is unavailable.", 5000)
            return
        self._observe_design_focus_context()
        self._focus_structure_request_id = (
            int(getattr(self, "_focus_structure_request_id", 0)) + 1
        )
        request_id = self._focus_structure_request_id
        optical = self._registration_optical_observation()
        context = self._coordinate_system_coordinator.focus_search_lease(optical)
        if context is None:
            self._show_status("Design focus context is unavailable.", 5000)
            return
        if document.file_backed:
            config = KLayoutConfig(
                path=Path(document.path).expanduser().resolve(),
                top_cell_name=document.top_cell_name,
                visible_layers=frozenset(document.visible_layers),
                source_bounds=tuple(document.cell_bounds[document.top_cell_name]),
                display_bounds=tuple(document.bounds),
                rotation_quarter_turns=document.rotation_quarter_turns,
                generation=request_id,
                source_load_id=document.source_load_id,
            )
            fixture_polygons: tuple[object, ...] = ()
        else:
            config = None
            fixture_polygons = tuple(
                polygon
                for layer in sorted(document.visible_layers)
                for polygon in document.polygons_by_layer.get(layer, ())
            )
        request = StructureBoundsRequest(
            request_id=request_id,
            generation=request_id,
            config=config,
            fixture_polygons=fixture_polygons,
        )
        self._pending_focus_structure_request_id = request_id
        self._pending_focus_structure_context = context
        self._pending_focus_structure_fov = tuple(fov_size)
        self._pending_focus_structure_design_bounds = tuple(document.bounds)
        worker = self._ensure_focus_structure_bounds_worker()
        worker.submit(request)
        self._show_status("Finding focus reference.", 3000)

    def _ensure_focus_structure_bounds_worker(self):
        worker = getattr(self, "_focus_structure_bounds_worker", None)
        if worker is not None:
            return worker
        worker = KLayoutStructureBoundsWorker(self)
        worker.ready.connect(self._on_focus_structure_bounds_ready)
        worker.failed.connect(self._on_focus_structure_bounds_failed)
        self._focus_structure_bounds_worker = worker
        return worker

    def _on_focus_structure_bounds_ready(
        self,
        result: StructureBoundsResult,
    ) -> None:
        pending_id = getattr(self, "_pending_focus_structure_request_id", None)
        pending_context = getattr(self, "_pending_focus_structure_context", None)
        if (
            result.request_id != pending_id
            or result.generation != pending_id
            or pending_context
            != self._coordinate_system_coordinator.focus_search_lease(
                self._registration_optical_observation()
            )
        ):
            return
        self._pending_focus_structure_request_id = None
        fov_size = getattr(self, "_pending_focus_structure_fov", None)
        design_bounds = getattr(self, "_pending_focus_structure_design_bounds", None)
        if fov_size is None or design_bounds is None:
            return
        try:
            candidate = select_central_focus_candidate(
                design_bounds=design_bounds,
                structure_bounds=result.structure_bounds,
                fov_size=fov_size,
            )
        except ValueError as exc:
            self._show_status(str(exc), 5000)
            return
        if candidate is None:
            self._show_status(
                "No focus structure fits the current field of view.", 5000
            )
            return
        transition = self._coordinate_system_coordinator.offer_focus_candidate(
            FocusCandidateRequest(
                candidate=candidate,
                optical=self._registration_optical_observation(),
                lease=pending_context,
            )
        )
        coordinate_flow.apply_coordinate_transition(self, transition)
        self._show_status(
            "Focus reference found. Review and use the selected point.", 5000
        )

    def _on_focus_structure_bounds_failed(
        self,
        failure: StructureBoundsFailure,
    ) -> None:
        if failure.request_id != getattr(
            self, "_pending_focus_structure_request_id", None
        ):
            return
        self._pending_focus_structure_request_id = None
        self._show_status("Unable to inspect visible design structures.", 5000)

    def _design_focus_optical_context_key(self) -> tuple[str, str]:
        try:
            objectives = self.settings_manager.objectives_configuration()
            objective_name = normalize_objective_name(objectives.active_name)
            profile = objectives.objectives.get(objective_name)
            payload = None if profile is None else profile.to_dict()
            identity = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        except (AttributeError, TypeError, ValueError):
            return "", ""
        return objective_name, identity

    def _clear_design_focus_overlay_state(self, *, clear_window: bool = True) -> None:
        self._pending_focus_structure_request_id = None
        self._pending_focus_structure_context = None
        self._pending_focus_structure_fov = None
        self._pending_focus_structure_design_bounds = None
        window = getattr(self, "design_layout_window", None)
        if window is not None and clear_window:
            window.set_focus_candidate(None)
            window.set_selected_focus_point(None)

    def _use_selected_design_focus_reference(
        self,
        design_point: tuple[float, float],
    ) -> None:
        snapshot = self.stage_controller.latest_machine_coordinate_snapshot()
        if snapshot is None:
            self._show_status("Current Machine coordinates are unavailable.", 5000)
            return
        try:
            pivot = self._rotation_geometry_snapshot().pivot_machine_xy
            objective_offset = self._active_objective_xy_offset()
        except (DesignModelError, TypeError, ValueError) as exc:
            self._show_status(str(exc), 5000)
            return
        transition = self._coordinate_system_coordinator.use_focus_reference(
            FocusReferenceRequest(
                design_point=(float(design_point[0]), float(design_point[1])),
                optical=self._registration_optical_observation(),
                machine_snapshot=snapshot,
                pivot_machine_xy=(float(pivot[0]), float(pivot[1])),
                objective_xy_offset=(
                    float(objective_offset[0]),
                    float(objective_offset[1]),
                ),
            )
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _observe_design_focus_context(self) -> None:
        transition = self._coordinate_system_coordinator.observe_focus_context(
            self._registration_optical_observation()
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _on_registration_focus_move_finished(
        self,
        completed_token: object,
        completed_target_xy: object,
        success: bool,
        message: str,
    ) -> None:
        if not isinstance(completed_token, int):
            return
        try:
            completed_target = (
                float(completed_target_xy[0]),
                float(completed_target_xy[1]),
            )
        except (IndexError, TypeError, ValueError):
            completed_target = None
        transition = self._coordinate_system_coordinator.complete(
            CoordinateAdapterCompletion(
                completed_token,
                FocusMoveResult(
                    completed_token,
                    succeeded=bool(success),
                    message=str(message or ""),
                    completed_target_xy=completed_target,
                ),
            )
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _on_registration_focus_move_signal(
        self,
        completed_token: object,
        completed_target_xy: object,
        success: bool,
        message: str,
    ) -> None:
        stage_move_lifecycle.on_move_finished(self, success, message)
        self._on_registration_focus_move_finished(
            completed_token,
            completed_target_xy,
            success,
            message,
        )

    def _on_registration_focus_autofocus_finished(
        self,
        token: object,
        success: bool,
        physical_z_mm: object,
        message: str,
    ) -> None:
        if not isinstance(token, int):
            return
        try:
            physical_z = None if physical_z_mm is None else float(physical_z_mm)
        except (TypeError, ValueError):
            physical_z = None
        transition = self._coordinate_system_coordinator.complete(
            CoordinateAdapterCompletion(
                token,
                CoordinateAutofocusResult(
                    token,
                    succeeded=bool(success),
                    physical_z_mm=physical_z,
                    message=str(message or ""),
                ),
            ),
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _registration_optical_observation(self) -> RegistrationOpticalObservation:
        fov_size = self._resolve_design_fov_size() or (0.0, 0.0)
        objective_name, identity = self._design_focus_optical_context_key()
        return RegistrationOpticalObservation(
            fov_size=(float(fov_size[0]), float(fov_size[1])),
            objective_name=objective_name,
            optical_calibration_identity=identity,
        )

    def _reset_design_focus_reference(self) -> None:
        snapshot = self.stage_controller.latest_machine_coordinate_snapshot()
        if snapshot is None:
            self._show_status("Current Machine coordinates are unavailable.", 5000)
            return
        try:
            pivot = self._rotation_geometry_snapshot().pivot_machine_xy
            objective_offset = self._active_objective_xy_offset()
        except (DesignModelError, TypeError, ValueError) as exc:
            self._show_status(str(exc), 5000)
            return
        transition = self._coordinate_system_coordinator.reset_focus_reference(
            FocusReferenceResetRequest(
                machine_snapshot=snapshot,
                pivot_machine_xy=(float(pivot[0]), float(pivot[1])),
                objective_xy_offset=(
                    float(objective_offset[0]),
                    float(objective_offset[1]),
                ),
            )
        )
        coordinate_flow.apply_coordinate_transition(self, transition)

    def _connect_design_focus_signals(self) -> None:
        window = getattr(self, "design_layout_window", None)
        if window is None or bool(
            getattr(self, "_design_focus_signals_connected", False)
        ):
            return
        window.find_focus_reference_requested.connect(self._find_design_focus_reference)
        window.focus_reference_requested.connect(
            lambda x_value, y_value: self._use_selected_design_focus_reference(
                (x_value, y_value)
            )
        )
        window.reset_focus_reference_requested.connect(
            self._reset_design_focus_reference
        )
        window.registration_instance_selected.connect(
            self._select_design_registration_instance
        )
        window.new_registration_requested.connect(
            self._new_design_registration_instance
        )
        self._design_focus_signals_connected = True

    def _refresh_design_panel(self) -> None:
        self._reconcile_missing_design_registration_instance()
        self._observe_design_focus_context()
        panel = self.design_navigator_panel
        self._connect_design_focus_signals()
        route_measurement_thread = getattr(self, "_route_measurement_thread", None)
        coordinate_snapshot = self._coordinate_system_coordinator.snapshot()
        p = navigation_targeting.design_panel_presentation(
            self._design_session,
            coordinate_snapshot.registration,
            route_running=route_measurement_thread is not None
            and route_measurement_thread.is_alive(),
            pending_alignment_preparation=self._pending_alignment_preparation
            is not None,
            design_snap_enabled=self._design_snap_enabled,
        )
        registration_instances = self._design_registration_instances()
        active_frame_id = coordinate_snapshot.registration.active_frame_id
        active_record = next(
            (
                record
                for record in coordinate_snapshot.records
                if record.frame_id == active_frame_id
            ),
            None,
        )
        if panel is not None:
            panel.set_document(p.document)
            panel.set_design_registration_active(p.registration_valid)
            panel.set_targets(p.targets, selected_target_id=p.selected_target_id)
            panel.set_route(
                p.route, selected_route_point_index=p.selected_route_point_index
            )
            panel.set_route_measurement_running(p.route_measurement_running)
            panel.set_calibration_prompt(p.calibration_prompt)
            panel.set_registration_status(p.registration_status)
            if hasattr(panel, "set_registration_instances"):
                panel.set_registration_instances(
                    registration_instances,
                    selected_frame_id=active_frame_id,
                )
            panel.set_registration_marks(p.source_design_marks, p.check_design_marks)
            panel.set_stage_registration_marks(p.source_stage_marks)
            if hasattr(panel, "set_focus_reference_state"):
                panel.set_focus_reference_state(
                    z_ready=bool(
                        active_record is not None
                        and active_record.readiness["Z"].available
                    ),
                    a_ready=bool(
                        active_record is not None
                        and active_record.readiness["A"].available
                    ),
                )
        if self.design_layout_window is not None:
            self.design_layout_window.set_snap_enabled(p.design_snap_enabled)
            self.design_layout_window.set_document(p.document)
            self.design_layout_window.set_targets(
                p.targets, selected_target_id=p.selected_target_id
            )
            self.design_layout_window.set_probe_route(
                p.route, selected_route_point_index=p.selected_route_point_index
            )
            self.design_layout_window.set_markup(getattr(self, "_design_markup", None))
            self.design_layout_window.set_guide_undo_available(
                bool(self._prune_design_guide_undo_stack())
            )
            self.design_layout_window.set_route_edit_enabled(self._design_edit_safe())
            self.design_layout_window.set_navigation_enabled(p.registration_valid)
            self.design_layout_window.set_registration_marks(
                p.source_design_marks, p.check_design_marks
            )
            self.design_layout_window.set_stage_registration_marks(p.source_stage_marks)
            self.design_layout_window.set_registration_instances(
                registration_instances,
                selected_frame_id=active_frame_id,
            )
        self._refresh_manual_alignment_ui()
        self._update_design_position(self._current_design_stage_xy)
        connection_flow.persist_controller_state_if_available(self)

    def _on_stage_axis_editing_finished(self, axis_name: str) -> bool | None:
        return coordinate_entry.on_stage_axis_editing_finished(self, axis_name)

    def _apply_pending_stage_coordinate_targets(self) -> None:
        coordinate_entry.apply_pending_stage_coordinate_targets(self)

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
        limit_targets: dict[str, float] | None = None,
        display_basis: object | None = None,
    ) -> bool:
        def limit_error(axis: str, display_target: float) -> str | None:
            target = (
                display_target
                if limit_targets is None
                else limit_targets.get(axis, display_target)
            )
            checker = (
                self._stage_axis_target_limit_error
                if limit_targets is None
                else self._machine_axis_target_limit_error
            )
            return checker(axis, target)

        decision = plan_coordinate_target_start(
            self._coordinate_targets.config,
            targets=targets,
            feedrate_mm_min=feedrate_mm_min,
            source_label=source_label,
            seed_position=stage_position_update.seed_motion_prediction_position(self),
            latest_stage_position=self.stage_controller.latest_stage_position(),
            axis_target_limit_error=limit_error,
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
        self._coordinate_targets.apply_start_plan(plan)
        self._coordinate_targets.display_basis = display_basis
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
        if (
            self._coordinate_targets.has_active_move()
            or not self._pending_stage_axis_targets
        ):
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

    def _api_machine_display_position(self) -> dict[str, float]:
        snapshot = self._api_machine_coordinate_snapshot()
        if snapshot is None:
            return {}
        return {
            axis: float(value)
            for axis, value in snapshot.physical_machine_pose.values.items()
            if axis in self.STAGE_AXIS_NAMES
        }

    def _api_machine_coordinate_snapshot(self) -> object | None:
        for getter_name in (
            "latest_motion_coordinate_snapshot",
            "latest_machine_coordinate_snapshot",
        ):
            getter = getattr(self.stage_controller, getter_name, None)
            if not callable(getter):
                continue
            snapshot = getter()
            if snapshot is not None:
                return snapshot
        return None

    def _resolve_api_stage_axis_target(
        self,
        axis_name: str,
        input_value: float,
        input_mode: str,
    ) -> tuple[float | None, float]:
        axis = str(axis_name).strip().upper()
        value = float(input_value)
        if axis not in self.STAGE_AXIS_NAMES:
            return None, value
        snapshot = self._api_machine_coordinate_snapshot()
        if snapshot is None:
            return None, value
        mode = normalize_api_coordinate_input_mode(input_mode) or "G90"
        if mode == "G91":
            current = snapshot.physical_machine_pose.values.get(axis)
            if current is None:
                return None, value
            physical_target = float(current) + value
        else:
            physical_target = value
        try:
            raw_target = snapshot.physical_machine_to_configured_controller(
                axis,
                physical_target,
            )
        except (TypeError, ValueError):
            return None, physical_target
        return float(raw_target), physical_target

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

    def _machine_axis_target_limit_error(
        self,
        axis_name: str,
        physical_machine_target: float,
    ) -> str | None:
        return stage_axis_target_limit_error(
            axis_name,
            physical_machine_target,
            homed_axes=self._stage_axis_homed,
            axis_display_limits=self.stage_controller.axis_machine_display_limits,
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
        coordinate_snapshot = self._coordinate_system_coordinator.snapshot()
        p = navigation_targeting.design_position_presentation(
            self._design_session,
            coordinate_snapshot.registration,
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
        return (
            "None"
            if point is None
            else f"({float(point[0]):.4f}, {float(point[1]):.4f})"
        )

    def _resolve_design_fov_size(self) -> tuple[float, float] | None:
        import numpy as np

        registration = self._coordinate_system_coordinator.snapshot().registration.registration_projection
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
        design_unit_mm = abs(float(registration.design_unit_mm))
        if not math.isfinite(design_unit_mm) or design_unit_mm <= 0.0:
            return None
        return (
            float(np.linalg.norm(width_vec)) / design_unit_mm,
            float(np.linalg.norm(height_vec)) / design_unit_mm,
        )

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
        frame_usability = self._coordinate_system_coordinator.current_design_lease()
        design_preflight = microscope_scan.start_design_decision(
            document=document,
            registration_valid=frame_usability.usable,
        )
        if self._show_microscope_scan_start_rejection(design_preflight):
            if document is not None and not frame_usability.usable:
                self._show_status(
                    str(
                        frame_usability.rejection_reason
                        or "Design coordinate frame is unavailable."
                    ),
                    6000,
                )
            return
        scale = self._active_microscope_scale()
        scale_preflight = microscope_scan.start_scale_decision(scale=scale)
        if self._show_microscope_scan_start_rejection(scale_preflight):
            return
        try:
            launch_snapshot = self._capture_microscope_scan_launch_snapshot(
                scale=scale,
                document=document,
                frame_usability_snapshot=frame_usability,
            )
            planning_request = MicroscopeDesignScanRequest(
                bounds=tuple(float(value) for value in document.bounds),
                overlap_fraction=float(configuration.overlap_fraction),
            )
        except (AttributeError, IndexError, TypeError, ValueError):
            self._show_status("Design registration is invalid.", 6000)
            return
        if not self._coordinate_system_coordinator.design_lease_is_current(
            frame_usability
        ):
            self._show_status(
                "Design coordinate frame changed before scan start.",
                6000,
            )
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
        if lcr_controller is not None and lcr_controller.live_polling_enabled():
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
            logger.debug(
                "Unable to read active objective for sample focus.", exc_info=True
            )
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
        return bool(
            self._coordinate_system_coordinator.snapshot().registration.registration_valid
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
