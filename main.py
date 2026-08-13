"""Application entry point for the probe station GUI."""

from __future__ import annotations

import logging
import sys
import threading
import time
from importlib import resources
from typing import TYPE_CHECKING, Any

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
    Qt,
    QThread,
    QTimer,
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
from probe_station_gui.api.keys import API_KEY_FILENAME, ApiKeyStore
from probe_station_gui.api.request_bridge import ApiRequestBridge
from probe_station_gui.api.server import ProbeStationApiServer
from probe_station_gui.api.stage_command_runtime import ApiStageCommandRuntime
from probe_station_gui.application.alignment import (
    _MainAlignmentMixin,
    _ManualAlignmentCaptureContext,
)
from probe_station_gui.application.api_meter_visa import _MainApiMeterVisaMixin
from probe_station_gui.application.api_route_scan import _MainApiRouteScanMixin
from probe_station_gui.application.api_stage_contact import (
    _MainApiStageContactMixin,
)
from probe_station_gui.application.bootstrap_api import _MainBootstrapApiMixin
from probe_station_gui.application.camera_pipeline import (
    _MainCameraPipelineMixin,
)
from probe_station_gui.application.design_edit_dialog import (
    _MainDesignEditDialogMixin,
)
from probe_station_gui.application.design_load import (
    _MainDesignLoadMixin,
    _PendingDesignMarkupLoad,
)
from probe_station_gui.application.design_markup import (
    _MainDesignMarkupMixin,
)
from probe_station_gui.application.manual_jog import _MainManualJogMixin
from probe_station_gui.application.motion_prediction import (
    _MainMotionPredictionMixin,
)
from probe_station_gui.application.objective_tools import _MainObjectiveToolsMixin
from probe_station_gui.application.optical_calibration import (
    _MainOpticalCalibrationMixin,
)
from probe_station_gui.application.registration_focus import (
    _MainRegistrationFocusMixin,
)
from probe_station_gui.application.route_capture_run import (
    _MainRouteCaptureRunMixin,
)
from probe_station_gui.application.route_control import _MainRouteControlMixin
from probe_station_gui.application.route_launch_setup import (
    _MainRouteLaunchSetupMixin,
)
from probe_station_gui.application.route_results import _MainRouteResultsMixin
from probe_station_gui.application.scan_sample_meter import (
    _MainScanSampleMeterMixin,
)
from probe_station_gui.application.settings_apply import _MainSettingsApplyMixin
from probe_station_gui.application.stage_design_position import (
    _MainStageDesignPositionMixin,
)
from probe_station_gui.application.status_coordinate_ui import (
    _MainStatusCoordinateUiMixin,
)
from probe_station_gui.camera.api_control import (
    CameraApiBroker,
    connect_camera_api_results,
)
from probe_station_gui.camera.auto_exposure import (
    CameraAutoExposureController,
)
from probe_station_gui.camera.distortion import (
    DistortionCorrection,
)
from probe_station_gui.camera.flat_field_calibration import FlatFieldCalibrationStore
from probe_station_gui.camera.flat_field_processing import (
    apply_flat_field_correction,  # noqa: F401 - retained test/patch seam
)
from probe_station_gui.camera.live_correction import (
    LatestFrameProcessor,
    LiveCameraCorrectionPipeline,
)
from probe_station_gui.coordinates import (
    CoordinateFrameStoreWorker,
    PhysicalMachinePose,
)
from probe_station_gui.coordinates.application_runtime import (
    create_application_coordinate_runtime,
)
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateMotionLease,
)
from probe_station_gui.design import objective_offsets as offsets
from probe_station_gui.design.frame_registration import (
    DesignFrameMetadata,
)
from probe_station_gui.design.klayout_structure_bounds_worker import (
    KLayoutStructureBoundsWorker,
)
from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.design.markup_store import (
    MarkupStoreWorker,
)
from probe_station_gui.design.session import DesignSession
from probe_station_gui.design.session_registration import AlignmentPreparation
from probe_station_gui.dialogs.click_calibration_dialog import ClickCalibrationDialog
from probe_station_gui.dialogs.lens_distortion_dialog import LensDistortionDialog
from probe_station_gui.dialogs.optical_calibration_wizard import (
    OpticalCalibrationWizard,
)
from probe_station_gui.instruments.meters.lcr import LCRMeterController
from probe_station_gui.notifications.telegram_runtime import (
    TelegramCommandRuntime,
)
from probe_station_gui.route.control_state import (
    ApiRouteControlState,
)
from probe_station_gui.route.measurement import (
    RouteMeasurementRecord,
    RouteMeasurementRunner,
)
from probe_station_gui.route.meter_config import (
    ROUTE_METER_GWINSTEK,  # noqa: F401 - imported for callers/tests
    ROUTE_METER_KEITHLEY,  # noqa: F401 - imported for callers/tests
    ROUTE_METER_KEITHLEY_2400,  # noqa: F401 - imported for callers/tests
)
from probe_station_gui.settings.dialog_transaction import (
    SettingsDialogTransaction,
)
from probe_station_gui.settings.manager import (
    SettingsManager,
)
from probe_station_gui.settings.software_coordinate_selection_store import (
    SoftwareCoordinateSelectionStoreWorker,
)
from probe_station_gui.shared.diagnostics import configure_crash_diagnostics
from probe_station_gui.shared.wheel_guard import GuardedComboBox as QComboBox
from probe_station_gui.stage import move_lifecycle as stage_move_lifecycle
from probe_station_gui.stage import position_update as stage_position_update
from probe_station_gui.stage import sample_handling
from probe_station_gui.stage.coordinate_targets import (
    CoordinateTargetConfig,
    CoordinateTargetMoveState,
)
from probe_station_gui.stage.exact_step import ExactStepAccumulator
from probe_station_gui.stage.manual_jog_prediction import (
    ManualJogPredictionConfig,
    ManualJogPredictionState,
)
from probe_station_gui.views import (
    main_window_connection_flow as connection_flow,
)
from probe_station_gui.views import (
    main_window_coordinate_flow as coordinate_flow,
)
from probe_station_gui.views import (
    main_window_needle_calibration as needle_calibration_ui,
)
from probe_station_gui.views import main_window_shutdown as shutdown_ui
from probe_station_gui.views import (
    main_window_stage_position_panel as stage_position_panel_adapter,
)
from probe_station_gui.views.alignment_panel import AlignmentPanel
from probe_station_gui.views.contact_oscillation_window import (
    ContactOscillationWindow,
)
from probe_station_gui.views.dock_widgets import CollapsibleDockWidget
from probe_station_gui.views.main_window_auxiliary import (
    toggle_design_layout_window,
)
from probe_station_gui.views.main_window_docks import create_main_window_docks
from probe_station_gui.views.main_window_menus import setup_main_window_menus
from probe_station_gui.views.microscope_interaction import (
    ClickMoveBindings,
    ClickMoveConfig,
)
from probe_station_gui.views.oscillation_panel import OscillationPanel
from probe_station_gui.views.resistance_monitor_panel import ResistanceMonitorPanel
from probe_station_gui.views.serial_connection_panel import SerialConnectionPanel
from probe_station_gui.views.stage_position_panel import (
    StagePositionPanel,
)

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
        MicroscopeScanDialog,
    )
    from probe_station_gui.dialogs.route_measurement_dialog import (
        RouteMeasurementDialog,
    )
    from probe_station_gui.route.measurement_config import (
        RouteMeasurementRunConfiguration,
    )
    from probe_station_gui.views.design_layout_window import DesignLayoutWindow
    from probe_station_gui.views.design_navigator_panel import DesignNavigatorPanel
    from probe_station_gui.views.serial_terminal_window import SerialTerminalWindow
    from probe_station_gui.views.surface_map_panel import SurfaceMapWindow


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
    _MainRouteLaunchSetupMixin,
    _MainRouteCaptureRunMixin,
    _MainRouteControlMixin,
    _MainRouteResultsMixin,
    _MainRegistrationFocusMixin,
    _MainStageDesignPositionMixin,
    _MainScanSampleMeterMixin,
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

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._clear_exact_step_targets()
        shutdown_ui.close_event(self, event)


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
