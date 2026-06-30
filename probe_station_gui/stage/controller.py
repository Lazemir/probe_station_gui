"""Stage controller coordinating calibration and click-to-move actions."""

from __future__ import annotations

import logging
import math
import importlib
from collections.abc import Iterable
from collections import deque
from queue import PriorityQueue
import re
import threading
import time
from typing import Callable, Optional

import serial
from PySide6.QtCore import QObject, Signal

from probe_station_gui.stage.autofocus_flow import StageControllerAutofocusMixin
from probe_station_gui.stage.click_move import StageControllerClickMoveMixin
from probe_station_gui.stage.axis_coordinates import StageControllerAxisCoordinatesMixin
from probe_station_gui.stage.connection_state import StageControllerConnectionMixin
from probe_station_gui.stage.autofocus_math import (
    autofocus_sweep_feedrate_mm_min as autofocus_sweep_feedrate_mm_min,
    estimate_shift as estimate_shift,
    estimate_shift_with_response as estimate_shift_with_response,
    focus_metric as focus_metric,
    frame_rate_from_timestamps as frame_rate_from_timestamps,
    parabolic_focus_peak as parabolic_focus_peak,
    qimage_to_gray as qimage_to_gray,
    static_focus_candidates as static_focus_candidates,
)
from probe_station_gui.stage.motion_prediction import interpolate_position as interpolate_position
from probe_station_gui.stage.errors import (
    AxisStateError as AxisStateError,
    SERIAL_IO_EXCEPTIONS,
    StageControllerError,
)
from probe_station_gui.stage.feedrate_limits import (
    clean_axis_max_feedrates,
    max_feedrate_for_axes,
)
from probe_station_gui.stage.fluidnc_config_io import (
    StageControllerFluidNCConfigIOMixin,
)
from probe_station_gui.stage.jog_commands import (
    JOG_AXIS_WORD_PATTERN,
    JOG_FEEDRATE_WORD_PATTERN,
    format_gcode_value as format_gcode_value,
)
from probe_station_gui.stage.homing_startup import StageControllerHomingStartupMixin
from probe_station_gui.stage.jog_queue import StageControllerJogQueueMixin
from probe_station_gui.stage.motion_commands import StageControllerMotionCommandsMixin
from probe_station_gui.stage.motion_timing import (
    absolute_move_distance_for_timeout as absolute_move_distance_for_timeout,
    idle_timeout_for_distance as idle_timeout_for_distance,
    move_distance_for_timeout as move_distance_for_timeout,
)
from probe_station_gui.stage.serial_write_queue import (
    StageControllerSerialWriteQueueMixin,
)
from probe_station_gui.stage.safety_state import StageControllerSafetyStateMixin
from probe_station_gui.stage.status_io import StageControllerStatusIOMixin
from probe_station_gui.stage.needle_actions import StageControllerNeedleActionsMixin
from probe_station_gui.stage.needle_targets import (
    normalise_needle_contact_zone,
)
from probe_station_gui.stage.needle_status import StageControllerNeedleStatusMixin
from probe_station_gui.stage.types import (
    AutofocusResult as AutofocusResult,
    MoveVector,
    _AutofocusContext as _AutofocusContext,
    _FocusSweepResult as _FocusSweepResult,
    _QueuedSerialWrite,
    _Status,
)


logger = logging.getLogger(__name__)

_SERIAL_IO_EXCEPTIONS = SERIAL_IO_EXCEPTIONS


class _LazyModule:
    def __init__(self, module_name: str) -> None:
        self._module_name = module_name
        self._module: object | None = None

    def __getattr__(self, name: str) -> object:
        if self._module is None:
            self._module = importlib.import_module(self._module_name)
        return getattr(self._module, name)


np = _LazyModule("numpy")


class StageController(
    StageControllerConnectionMixin,
    StageControllerHomingStartupMixin,
    StageControllerSerialWriteQueueMixin,
    StageControllerStatusIOMixin,
    StageControllerFluidNCConfigIOMixin,
    StageControllerNeedleStatusMixin,
    StageControllerNeedleActionsMixin,
    StageControllerJogQueueMixin,
    StageControllerMotionCommandsMixin,
    StageControllerAxisCoordinatesMixin,
    StageControllerSafetyStateMixin,
    StageControllerClickMoveMixin,
    StageControllerAutofocusMixin,
    QObject,
):
    """Translate mouse clicks into stage movements via serial commands."""

    calibration_changed: Signal = Signal(float, float)
    movement_started: Signal = Signal()
    movement_finished: Signal = Signal(bool, str)
    click_move_started: Signal = Signal(float, float, float)
    absolute_xy_move_started: Signal = Signal(float, float, float)
    stage_position_changed: Signal = Signal(object)
    autofocus_finished: Signal = Signal(bool, str)
    objective_calibration_updated: Signal = Signal(str, object)
    objective_mismatch_detected: Signal = Signal(str, str)
    homing_status_changed: Signal = Signal(object)
    limit_axes_changed: Signal = Signal(object)
    axis_a_ready_changed: Signal = Signal(bool)
    homing_action_started: Signal = Signal(str)
    homing_action_finished: Signal = Signal(bool, str, str)
    needles_state_changed: Signal = Signal(bool, bool)
    needles_zone_changed: Signal = Signal(str)
    needles_action_started: Signal = Signal(str)
    needles_action_finished: Signal = Signal(bool, str, str)
    needle_height_changed: Signal = Signal(float)
    axis_max_feedrates_changed: Signal = Signal(object)
    oscillation_state_changed: Signal = Signal(bool, str)
    status_message: Signal = Signal(str)
    controller_reboot_detected: Signal = Signal()
    controller_reboot_ready: Signal = Signal()

    CALIBRATION_PIXEL_TARGET = 120.0
    CALIBRATION_MIN_VERIFY_PIXELS = 15.0
    CALIBRATION_MAX_STEPS = 25
    CALIBRATION_VERIFY_STEP_MM = 0.05
    CALIBRATION_VERIFY_ERROR_RATIO = 0.35
    CALIBRATION_VERIFY_MIN_ERROR_MM = 0.01
    CALIBRATION_PROBE_STEP_MM = 0.005
    CALIBRATION_MAX_UNVERIFIED_STEP_MM = 0.04
    CALIBRATION_MAX_ADAPTIVE_STEP_MM = 0.08
    CALIBRATION_MIN_OBSERVATION_PIXELS = 1.0
    CALIBRATION_MIN_RESPONSE = 0.05
    CALIBRATION_VERIFY_TARGET_PIXELS = 40.0
    DEFAULT_FEEDRATE = 600.0
    MIN_FEEDRATE = 1.0
    DEFAULT_NEEDLE_CONTACT_ZONE_MM = 0.05
    MOVE_IDLE_TIMEOUT_MARGIN_S = 5.0
    MOVE_IDLE_TIMEOUT_MIN_S = 10.0
    MOVE_IDLE_TIMEOUT_MAX_S = 3600.0
    AUTOFOCUS_FINE_STEP_MM = 0.02
    AUTOFOCUS_MIN_SWEEP_FRAMES = 4
    AUTOFOCUS_BACKLASH_MM = 0.03
    AUTOFOCUS_STATIC_REFINEMENT_POINTS = 9
    AUTOFOCUS_STATIC_EDGE_REFINEMENT_ROUNDS = 2
    AUTOFOCUS_STATIC_SETTLE_FRAMES = 1
    AUTOFOCUS_FRAME_RATE_SAMPLE_FRAMES = 6
    AUTOFOCUS_FRAME_RATE_MAX_AGE_S = 2.0
    AUTOFOCUS_FRAME_RATE_SAMPLE_TIMEOUT_S = 1.0
    AUTOFOCUS_MIN_SWEEP_FEEDRATE_MM_MIN = MIN_FEEDRATE
    CALIBRATION_MIN_OBSERVATIONS = 4
    CALIBRATION_MAX_OBSERVATIONS_PER_AXIS = 8
    A_ZERO_TOLERANCE = 1e-3
    OSCILLATION_MIN_AMPLITUDE_MM = 0.001
    OSCILLATION_MAX_AMPLITUDE_MM = 10.0
    OSCILLATION_MIN_FEEDRATE = 1.0
    OSCILLATION_MAX_FEEDRATE = 5000.0
    LINEAR_SEGMENTS_PER_SWEEP = 40
    SPIRAL_SEGMENTS_PER_TURN = 180
    SPIRAL_MIN_TURNS_PER_SWEEP = 0.25
    SPIRAL_MAX_TURNS_PER_SWEEP = 50.0
    B_AXIS_SOFT_LIMIT_DEG = 45.0
    MAX_CLICK_MOVE_MM = 10.0
    SERIAL_PRIORITY_JOG_STOP = 0
    SERIAL_PRIORITY_FEED_OVERRIDE = 5
    SERIAL_PRIORITY_JOG_COMMAND = 10
    SERIAL_PRIORITY_SOFT_RESET = 20
    SERIAL_PRIORITY_TERMINAL = 30
    SERIAL_JOG_COMMAND_SETTLE_S = 0.03
    CONTROLLER_SESSION_MARKER_READ_ATTEMPTS = 2
    COORDINATE_STATUS_READ_ATTEMPTS = 3
    COORDINATE_STATUS_RETRY_DELAY_S = 0.05
    COORDINATE_TARGET_STATUS_TOLERANCE = 7.5e-4
    FEED_OVERRIDE_RESET = b"\x90"
    FEED_OVERRIDE_PLUS_10 = b"\x91"
    FEED_OVERRIDE_MINUS_10 = b"\x92"
    FEED_OVERRIDE_PLUS_1 = b"\x93"
    FEED_OVERRIDE_MINUS_1 = b"\x94"
    FEED_OVERRIDE_MIN_PERCENT = 10
    FEED_OVERRIDE_MAX_PERCENT = 200
    LIMIT_HIT_TOLERANCE = 0.05
    SOFT_LIMIT_AXIS_PATTERN = re.compile(r"Soft limit on\s+(?P<axis>[A-Za-z])\b")
    JOG_AXIS_WORD_PATTERN = JOG_AXIS_WORD_PATTERN
    JOG_FEEDRATE_WORD_PATTERN = JOG_FEEDRATE_WORD_PATTERN
    HOMED_PATTERN = re.compile(r"\|H:([A-Za-z]+)")
    HOMED_MSG_PATTERN = re.compile(r"^\[MSG:Homed:(?P<axes>[A-Za-z]+)\]")
    MODAL_STATE_PATTERN = re.compile(r"^\[GC:(?P<modal>[^\]]+)\]$")
    COORDINATE_OFFSET_PATTERN = re.compile(
        r"^\[(?P<system>G5(?:4|5|6|7|8|9(?:\.[123])?)):(?P<coords>[^\]]+)\]$"
    )
    AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}
    CONTROLLER_LIMIT_AXES = ("X", "Y", "Z", "A")
    WORK_COORDINATE_SYSTEMS = (
        "G54",
        "G55",
        "G56",
        "G57",
        "G58",
        "G59",
        "G59.1",
        "G59.2",
        "G59.3",
    )
    WORK_COORDINATE_SYSTEM_P_VALUES = {
        "G54": 1,
        "G55": 2,
        "G56": 3,
        "G57": 4,
        "G58": 5,
        "G59": 6,
        "G59.1": 7,
        "G59.2": 8,
        "G59.3": 9,
    }
    DEFAULT_WORK_COORDINATE_SYSTEM = "G54"

    def __init__(self) -> None:
        super().__init__()
        self._serial: Optional[serial.Serial] = None
        self._pixels_to_mm: Optional[np.ndarray] = None
        self._last_stage_position: Optional[tuple[float, ...]] = None
        self._last_machine_position: Optional[tuple[float, ...]] = None
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_counter = 0
        self._frame_history: deque[tuple[int, float, np.ndarray]] = deque(maxlen=256)
        self._frame_condition = threading.Condition()
        self._task_lock = threading.RLock()
        self._active_thread: Optional[threading.Thread] = None
        self._status_refresh_thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._axis_limits: dict[str, tuple[float, float]] = {}
        self._homed_axes: set[str] = set()
        self._limit_axes: set[str] = set()
        self._relative_warning_emitted = False
        self._axis_a_ready = False
        self._needles_up = False
        self._needles_known = False
        self._needles_zone: str | None = None
        self._needle_raise_lowering_mm: Optional[float] = None
        self._needle_down_lowering_mm: Optional[float] = None
        self._needle_contact_zone_mm = self.DEFAULT_NEEDLE_CONTACT_ZONE_MM
        self._axis_max_feedrates: dict[str, float] = {}
        self._axis_a_calibration: dict[str, float | str] | None = None
        self._axis_z_calibration: dict[str, float | str | tuple[float, ...]] | None = None
        self._active_objective_name = "X5"
        self._objective_calibration_target_pixels = self.CALIBRATION_PIXEL_TARGET
        self._objective_autofocus_range_mm = 1.0
        self._objective_autofocus_fine_step_mm = self.AUTOFOCUS_FINE_STEP_MM
        self._objective_matrices: dict[str, np.ndarray] = {}
        self._objective_calibration_verified: dict[str, bool] = {}
        self._active_needles_action: str | None = None
        self._active_needles_programmed_feedrate: float | None = None
        self._oscillation_active = False
        self._motion_safety_disabled = False
        self._queued_needles_actions: deque[
            tuple[str, float | None, float | None]
        ] = deque()
        self._oscillation_needles_actions: deque[
            tuple[str, float | None, float | None]
        ] = deque()
        self._b_axis_zero_position: Optional[float] = None
        self._serial_session_lock = threading.RLock()
        self._serial_session_state = threading.local()
        self._feed_override_lock = threading.Lock()
        self._queued_write_sequence = 0
        self._queued_jog_generation = 0
        self._queued_feed_override_generation = 0
        self._active_feed_override_percent: Optional[int] = None
        self._last_stage_state: Optional[str] = None
        self._last_status_timestamp: Optional[float] = None
        self._last_jog_write_timestamp: Optional[float] = None
        self._last_a_position_read_failure: Optional[str] = None
        self._controller_state_stale = False
        self._jog_motion_active = False
        self._position_reporting_mode = "work"
        self._current_status_report_mask: Optional[int] = None
        self._controller_session_marker: Optional[int] = None
        self._controller_reboot_recovery_pending = False
        self._controller_reboot_ready_notified = False
        self._coordinate_startup_mode = "controller"
        self._preferred_work_coordinate_system = self.DEFAULT_WORK_COORDINATE_SYSTEM
        self._active_work_coordinate_system: Optional[str] = None
        self._controller_coordinate_offsets: dict[str, tuple[float, ...]] = {}
        self._async_write_queue: PriorityQueue[_QueuedSerialWrite] = PriorityQueue()
        self._async_write_clear_epoch = 0
        self._async_write_shutdown = threading.Event()
        self._async_write_thread = threading.Thread(
            target=self._run_async_write_worker,
            daemon=True,
        )
        self._async_write_thread.start()

    # Jog stop confirmation is handled in the joystick layer to avoid serial contention.
    def shutdown(self) -> None:
        """Stop any outstanding background task before application exit."""

        with self._task_lock:
            thread = self._active_thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)
        self._async_write_shutdown.set()
        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=9999,
                sequence=self._next_queued_write_sequence(),
                kind="shutdown",
                payload=b"",
                description="shutdown",
            )
        )
        if self._async_write_thread.is_alive():
            self._async_write_thread.join(timeout=2.0)

    def _poll_status_once(self) -> None:
        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            return
        try:
            if not self._serial_session_lock.acquire(blocking=False):
                return
            try:
                with self._serial_session(serial_connection):
                    self._query_status(serial_connection, check_cancelled=False)
            finally:
                self._serial_session_lock.release()
        except StageControllerError:
            return

    def _run_status_refresh(self) -> None:
        try:
            self._poll_status_once()
        finally:
            with self._task_lock:
                self._status_refresh_thread = None

    def request_needles_raise(self, feedrate: float | None = None) -> None:
        """Raise the needles by homing the A axis."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                if self._oscillation_active:
                    self._queue_oscillation_needles_action_locked(
                        "raise",
                        feedrate=feedrate,
                    )
                    return
                self.status_message.emit("Stage is busy. Ignoring needle raise request.")
                return
            self._start_needles_action_locked("raise", feedrate=feedrate)

    def request_needles_lift(self, feedrate: float | None = None) -> None:
        """Lift the needles out of the contact zone without fully raising A."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                if self._oscillation_active:
                    self._queue_oscillation_needles_action_locked(
                        "lift",
                        feedrate=feedrate,
                    )
                    return
                self.status_message.emit("Stage is busy. Ignoring needle lift request.")
                return
            self._start_needles_action_locked("lift", feedrate=feedrate)

    def request_needles_lower(self, feedrate: float | None = None) -> None:
        """Lower the needles to the calibrated down position."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                if self._oscillation_active:
                    self._queue_oscillation_needles_action_locked(
                        "lower",
                        feedrate=feedrate,
                    )
                    return
                self.status_message.emit("Stage is busy. Ignoring needle lower request.")
                return
            self._start_needles_action_locked("lower", feedrate=feedrate)

    def apply_needle_calibration(
        self,
        *,
        raise_position_mm: Optional[float] = None,
        down_position_mm: Optional[float],
        contact_zone_mm: Optional[float] = None,
    ) -> None:
        """Apply the persisted physical needle raise/lower targets."""

        self._needle_raise_lowering_mm = self._normalise_needle_lowering_target(
            raise_position_mm
        )
        self._needle_down_lowering_mm = self._normalise_needle_lowering_target(
            down_position_mm
        )
        if contact_zone_mm is not None:
            self._needle_contact_zone_mm = normalise_needle_contact_zone(
                contact_zone_mm,
                default=self.DEFAULT_NEEDLE_CONTACT_ZONE_MM,
            )

    def apply_axis_max_feedrates(self, rates: dict[str, float] | None) -> None:
        """Apply axis maximum feedrates parsed from the active FluidNC config."""

        self._axis_max_feedrates = clean_axis_max_feedrates(
            rates,
            axis_index=self.AXIS_INDEX,
            min_feedrate=self.MIN_FEEDRATE,
        )

    def axis_max_feedrates(self) -> dict[str, float]:
        """Return the currently applied per-axis maximum feedrates."""

        return dict(self._axis_max_feedrates)

    def max_feedrate_for_axes(self, axes: object) -> float:
        """Return the highest configured feedrate usable for a multi-axis move."""

        return max_feedrate_for_axes(
            axes,
            self._axis_max_feedrates,
            axis_index=self.AXIS_INDEX,
            default_feedrate=self.DEFAULT_FEEDRATE,
            min_feedrate=self.MIN_FEEDRATE,
        )

    def query_axis_max_feedrates(self) -> dict[str, float]:
        """Query the live FluidNC configuration for per-axis maximum feedrates."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                raise StageControllerError(
                    "Stage is busy. Cannot read controller feedrate limits."
                )
            with self._serial_session():
                rates = self._query_axis_max_feedrates_locked()
        self.apply_axis_max_feedrates(rates)
        self.axis_max_feedrates_changed.emit(dict(rates))
        return rates

    def apply_objective_configuration(
        self,
        objective: object | None,
        candidates: object | None = None,
    ) -> None:
        """Apply the active objective profile to calibration and autofocus."""

        if objective is None:
            self._active_objective_name = "X5"
            self._pixels_to_mm = None
            self._objective_matrices.clear()
            self._objective_calibration_verified.clear()
            return
        self._objective_matrices = self._candidate_matrices(candidates)
        name = str(getattr(objective, "name", "X5")).strip().upper() or "X5"
        self._active_objective_name = name
        self._objective_calibration_target_pixels = self.CALIBRATION_PIXEL_TARGET
        self._objective_autofocus_range_mm = self._positive_profile_value(
            getattr(objective, "autofocus_range_mm", 1.0),
            1.0,
        )
        self._objective_autofocus_fine_step_mm = self._positive_profile_value(
            getattr(objective, "autofocus_fine_step_mm", self.AUTOFOCUS_FINE_STEP_MM),
            self.AUTOFOCUS_FINE_STEP_MM,
        )
        matrix = self._matrix_from_objective(objective)
        self._pixels_to_mm = matrix
        if matrix is not None:
            self._objective_matrices[name] = matrix
            self._objective_calibration_verified[name] = False
        else:
            self._objective_calibration_verified[name] = False
        if matrix is not None:
            mm_per_pixel_x, mm_per_pixel_y = self._calibration_magnitudes()
            self.calibration_changed.emit(mm_per_pixel_x, mm_per_pixel_y)

    @staticmethod
    def _positive_profile_value(value: object, default: float) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return float(default)
        if not math.isfinite(result) or result <= 0.0:
            return float(default)
        return result

    def _matrix_from_objective(self, objective: object) -> Optional[np.ndarray]:
        configured = bool(getattr(objective, "xy_calibration_configured", False))
        raw_matrix = getattr(objective, "pixels_to_mm", None)
        if not configured or not raw_matrix:
            return None
        try:
            matrix = np.asarray(raw_matrix, dtype=float)
        except (TypeError, ValueError):
            return None
        if matrix.shape != (2, 2) or not np.isfinite(matrix).all():
            return None
        determinant = float(np.linalg.det(matrix))
        if abs(determinant) < 1e-18:
            return None
        return matrix

    def _candidate_matrices(self, candidates: object | None) -> dict[str, np.ndarray]:
        matrices: dict[str, np.ndarray] = {}
        if candidates is None:
            return matrices
        if isinstance(candidates, dict):
            iterable = candidates.items()
        else:
            iterable = (
                (str(getattr(candidate, "name", "")).strip().upper(), candidate)
                for candidate in candidates
                if candidate is not None
            )
        for name, candidate in iterable:
            objective_name = str(name).strip().upper()
            if not objective_name:
                objective_name = str(getattr(candidate, "name", "")).strip().upper()
            if not objective_name:
                continue
            matrix = self._matrix_from_objective(candidate)
            if matrix is None:
                continue
            matrices[objective_name] = matrix
        return matrices

    def apply_coordinate_system_configuration(
        self,
        *,
        position_mode: str,
        startup_mode: str,
        preferred_system: str,
    ) -> None:
        """Apply coordinate-system preferences loaded from persistent settings."""

        reporting_mode = position_mode.strip().lower()
        if reporting_mode not in {"work", "machine"}:
            reporting_mode = "work"
        mode = startup_mode.strip().lower()
        if mode not in {"controller", "fixed"}:
            mode = "controller"
        system = preferred_system.strip().upper()
        if system not in self.WORK_COORDINATE_SYSTEMS:
            system = self.DEFAULT_WORK_COORDINATE_SYSTEM
        self._position_reporting_mode = reporting_mode
        self._coordinate_startup_mode = mode
        self._preferred_work_coordinate_system = system

    def active_coordinate_system(self) -> str | None:
        """Return the currently active work coordinate system, if known."""

        return self._active_work_coordinate_system

    def coordinate_display_name(self) -> str:
        """Return the label that matches the coordinates exposed to the GUI."""

        if self._position_reporting_mode == "machine":
            return "Machine"
        active = self._active_work_coordinate_system
        if active:
            return active
        return "Work"

    def axis_display_limits(self, axis: str) -> tuple[float, float] | None:
        """Return software limits in the same coordinate basis as the GUI."""

        return self._axis_limits_for_configured_mode(axis.upper().strip(), None)

    def set_current_axis_work_coordinate(
        self,
        axis: str,
        value: float = 0.0,
    ) -> None:
        """Shift the active work offset so the current axis position reads value."""

        axis_key = axis.upper().strip()
        if axis_key not in self.AXIS_INDEX:
            raise StageControllerError(f"Unsupported axis: {axis}")
        try:
            target_value = float(value)
        except (TypeError, ValueError) as exc:
            raise StageControllerError(f"Unsupported {axis_key} coordinate: {value}") from exc
        if not math.isfinite(target_value):
            raise StageControllerError(f"Unsupported {axis_key} coordinate: {value}")
        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                raise StageControllerError(
                    "Stage is busy. Wait for the current operation to finish."
                )
            with self._serial_session():
                self._set_current_axis_work_coordinate_locked(
                    axis_key,
                    target_value,
                )

    def _set_current_axis_work_coordinate_locked(
        self,
        axis: str,
        value: float,
    ) -> None:
        serial_connection = self._current_serial()
        status = self._query_status_with_required_coordinates(
            serial_connection,
            axes=(axis,),
        )
        if status is None:
            raise StageControllerError("Unable to read controller status.")
        if status.state.lower() in {"jog", "run"}:
            raise StageControllerError(
                "Wait for the stage to stop before setting a work coordinate."
            )
        coordinate_system = (
            status.coordinate_system
            or self._active_work_coordinate_system
            or self._preferred_work_coordinate_system
        )
        coordinate_system = coordinate_system.strip().upper()
        p_value = self.WORK_COORDINATE_SYSTEM_P_VALUES.get(coordinate_system)
        if p_value is None:
            raise StageControllerError(
                f"Unsupported work coordinate system: {coordinate_system}."
            )
        self._require_homed_axes(status, {axis})
        command = (
            f"G10 L20 P{p_value} {axis}"
            f"{self._format_gcode_value(value, decimals=4)}"
        )
        self._write_current_command_and_wait(command)
        self._active_work_coordinate_system = coordinate_system
        if self._position_reporting_mode != "machine":
            try:
                self._controller_coordinate_offsets = self._query_work_coordinate_offsets()
            except StageControllerError as exc:
                logger.warning("Unable to refresh work coordinate offsets: %s", exc)
        refreshed = self._query_status_with_required_coordinates(
            serial_connection,
            axes=(axis,),
        )
        if axis == "A" and refreshed is not None:
            self._update_needles_from_status(refreshed)
        self.status_message.emit(
            f"{axis} work coordinate set to {self._format_gcode_value(value)} "
            f"in {coordinate_system}."
        )

    def request_needles_adjust(
        self,
        step_mm: float,
        feedrate: float | None = None,
    ) -> None:
        """Adjust the A axis for needle calibration without the XY safety gate."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                if self._oscillation_active:
                    self._queue_oscillation_needles_action_locked(
                        "adjust",
                        float(step_mm),
                        feedrate=feedrate,
                    )
                    return
                self.status_message.emit("Stage is busy. Ignoring needle adjustment.")
                return
            self._start_needles_action_locked(
                "adjust",
                float(step_mm),
                feedrate=feedrate,
            )

    def current_a_position(self) -> Optional[float]:
        """Return the current A coordinate when it can be queried safely."""

        self._last_a_position_read_failure = None
        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            self._record_a_position_read_failure("serial connection is not available")
            return None
        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                thread_name = self._active_thread.name or "<unnamed>"
                latest = self._last_stage_position
                timestamp = self._last_status_timestamp
                age_s = None if timestamp is None else max(0.0, time.monotonic() - timestamp)
                reason = (
                    f"stage task is active ({thread_name}); "
                    f"latest_state={self._last_stage_state!r}, "
                    f"latest_position={latest!r}, "
                    f"last_status_age_s={age_s!r}"
                )
                self._record_a_position_read_failure(reason)
                return None
        with self._serial_session():
            a_position = self._read_current_a_position()
        if a_position is None:
            return None
        self.needle_height_changed.emit(
            self._axis_a_lowering_for_configured_coordinate(a_position)
        )
        return a_position

    def last_a_position_read_failure(self) -> Optional[str]:
        """Return the last reason why reading A position failed, if any."""

        return self._last_a_position_read_failure

    def read_pending_serial_output(self, max_bytes: int | None = None) -> bytes:
        """Read currently buffered controller output without contending with active tasks."""

        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            return b""
        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                return b""
        if not self._serial_session_lock.acquire(blocking=False):
            return b""
        try:
            session = self._fluidnc_session_for(serial_connection)
            return session.read_pending_output(max_bytes=max_bytes)
        finally:
            self._serial_session_lock.release()

    def invalidate_needles_state(self, reason: str = "") -> None:
        """Mark needles state unknown after manual A-axis changes."""

        if reason:
            self.status_message.emit(reason)
        self._set_needles_state(False, known=False)

    def cancel_active_task(self, reason: str = "Operation cancelled.") -> None:
        """Signal any active task to stop without forcing a controller reset."""

        self._cancel_event.set()
        with self._task_lock:
            had_needles_action = (
                self._active_needles_action is not None
                or bool(self._queued_needles_actions)
                or bool(self._oscillation_needles_actions)
            )
            self._queued_needles_actions.clear()
            self._oscillation_needles_actions.clear()
            self._active_needles_action = None
            self._active_needles_programmed_feedrate = None
        self.status_message.emit(reason)
        self.queue_jog_stop()
        if had_needles_action:
            self._set_needles_state(False, known=False)

    def cancel_active_motion(self, reason: str = "Motion cancel requested.") -> None:
        """Cancel a jog-backed motion without resetting controller state."""

        self._cancel_event.set()
        self.queue_jog_stop()
        self.status_message.emit(reason)

    def reset_controller(
        self,
        *,
        source: str = "unknown",
        reason: str = "Controller reset requested.",
    ) -> None:
        """Abort current work and request a FluidNC soft reset."""

        self._cancel_event.set()
        with self._task_lock:
            self._queued_needles_actions.clear()
            self._oscillation_needles_actions.clear()
            self._active_needles_action = None
            self._active_needles_programmed_feedrate = None
        self.status_message.emit(reason)
        self.queue_soft_reset(source=source)

    def is_busy(self) -> bool:
        """Return True when a background movement task is currently running."""

        with self._task_lock:
            return bool(self._active_thread and self._active_thread.is_alive())

    def _start_background_task(
        self,
        *,
        target: Callable[..., None],
        args: tuple[object, ...] = (),
        busy_message: str,
        before_create: Callable[[], None] | None = None,
        before_start: Callable[[], None] | None = None,
    ) -> bool:
        with self._task_lock:
            active_thread = getattr(self, "_active_thread", None)
            if active_thread and active_thread.is_alive():
                self.status_message.emit(busy_message)
                return False
            if before_create is not None:
                before_create()
            self._cancel_event.clear()
            thread = threading.Thread(target=target, args=args, daemon=True)
            self._active_thread = thread
            if before_start is not None:
                before_start()
            thread.start()
            return True

    def begin_external_task(self, label: str) -> None:
        """Reserve the controller for a higher-level blocking workflow."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                raise StageControllerError(
                    f"Stage is busy. Cannot start {label}."
                )
            self._cancel_event.clear()
            self._active_thread = threading.current_thread()

    def finish_external_task(self) -> None:
        """Release a controller reservation created by begin_external_task."""

        current_thread = threading.current_thread()
        with self._task_lock:
            if self._active_thread is current_thread:
                self._active_thread = None

    def run_external_needles_action(
        self,
        action: str,
        feedrate: float | None = None,
    ) -> str:
        """Run a blocking needle action inside an external controller reservation."""

        action_key = str(action).strip().lower()
        self.needles_action_started.emit(action_key)
        try:
            self._check_cancelled()
            message = self._perform_needles_action(action_key, feedrate)
            self.needles_action_finished.emit(True, message, action_key)
            return message
        except StageControllerError as exc:
            self.needles_action_finished.emit(False, str(exc), action_key)
            raise

    def run_external_needles_adjust(
        self,
        step_mm: float,
        feedrate: float | None = None,
    ) -> str:
        """Run a blocking A-axis needle adjustment inside an external reservation."""

        action = "adjust"
        self.needles_action_started.emit(action)
        try:
            self._check_cancelled()
            message = self._perform_needles_adjust(float(step_mm), feedrate)
            self.needles_action_finished.emit(True, message, action)
            return message
        except StageControllerError as exc:
            self.needles_action_finished.emit(False, str(exc), action)
            raise

    def run_external_needles_lower_to_depth_below_down(
        self,
        depth_mm: float,
        feedrate: float | None = None,
    ) -> str:
        """Lower needles directly to a depth below the saved down position."""

        action = "lower"
        self.needles_action_started.emit(action)
        try:
            self._check_cancelled()
            message = self._perform_needles_lower_to_depth_below_down(
                float(depth_mm),
                feedrate,
            )
            self.needles_action_finished.emit(True, message, action)
            return message
        except StageControllerError as exc:
            self.needles_action_finished.emit(False, str(exc), action)
            raise

    def current_stage_position(self) -> tuple[float, ...]:
        """Return the latest controller position in the active GUI coordinate space."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                raise StageControllerError("Stage is busy. Wait for the current operation to finish.")
            with self._serial_session():
                status = self._query_synced_status_for_absolute_motion(min_axes=3)
        if status is None or status.display_position is None:
            raise StageControllerError("Unable to read stage position.")
        if len(status.display_position) < 3:
            raise StageControllerError("Controller did not report complete X/Y/Z coordinates.")
        if status.state.lower() in {"jog", "run"}:
            raise StageControllerError("Wait for the stage to stop before capturing a marker.")
        self._ensure_b_axis_zero_reference(status)
        return tuple(float(value) for value in status.display_position)

    def latest_stage_position(self) -> tuple[float, ...] | None:
        """Return the most recently observed GUI position, if any."""

        if self._last_stage_position is None:
            return None
        return tuple(self._last_stage_position)

    def latest_a_position(self) -> float | None:
        """Return the latest cached A position, if known."""

        latest = self.latest_stage_position()
        if latest is None or len(latest) <= 3:
            return None
        return float(latest[3])

    def latest_axis_a_lowering(self) -> float | None:
        """Return the latest cached physical A-axis lowering, if known."""

        a_position = self.latest_a_position()
        if a_position is None:
            return None
        return self._axis_a_lowering_for_configured_coordinate(a_position)

    def latest_stage_state(self) -> str | None:
        """Return the most recently observed controller motion state."""

        return self._last_stage_state

    def last_status_timestamp(self) -> float | None:
        """Return monotonic time of the most recent parsed status frame."""

        return self._last_status_timestamp

    def last_jog_write_timestamp(self) -> float | None:
        """Return monotonic time when the last jog command was flushed."""

        return self._last_jog_write_timestamp

    def request_status_refresh(self) -> None:
        """Poll controller position in a background thread when idle."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                return
            if not self._async_write_queue.empty():
                return
            if self._jog_motion_active:
                return
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                return
            if (
                self._status_refresh_thread is not None
                and self._status_refresh_thread.is_alive()
            ):
                return
            thread = threading.Thread(target=self._run_status_refresh, daemon=True)
            self._status_refresh_thread = thread
            thread.start()

    def _query_synced_status_for_absolute_motion(
        self,
        *,
        refresh_coordinate_state: bool = True,
        axes: Iterable[str] | None = None,
        min_axes: int | None = None,
    ) -> Optional[_Status]:
        """Refresh coordinate-system state before absolute position reads and moves."""

        serial_connection = self._current_serial()
        with self._serial_session(serial_connection):
            if not refresh_coordinate_state:
                state_was_stale = self._controller_state_stale
                status = self._query_status_with_required_coordinates(
                    serial_connection,
                    axes=axes,
                    min_axes=min_axes,
                )
                if (
                    status is not None
                    and not state_was_stale
                ):
                    return status

            if self._position_reporting_mode != "machine" or self._controller_state_stale:
                self._refresh_coordinate_system_state(apply_preference=True)
            return self._query_status_with_required_coordinates(
                serial_connection,
                axes=axes,
                min_axes=min_axes,
            )

    def _update_cached_positions(self, status: _Status) -> None:
        if status.coordinate_system:
            self._active_work_coordinate_system = status.coordinate_system
        if status.position is not None:
            self._last_machine_position = tuple(float(v) for v in status.position)
        if status.display_position is not None:
            coords = tuple(float(v) for v in status.display_position)
            self._last_stage_position = coords
            self.stage_position_changed.emit(coords)
        if (
            status.coordinate_system
            and status.work_offset is not None
            and status.coordinate_system in self.WORK_COORDINATE_SYSTEMS
        ):
            self._controller_coordinate_offsets[status.coordinate_system] = tuple(
                float(v) for v in status.work_offset
            )

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise StageControllerError("Operation cancelled.")

__all__ = ["StageController", "MoveVector"]
