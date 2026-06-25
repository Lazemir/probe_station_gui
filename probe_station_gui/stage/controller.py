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
from PySide6.QtGui import QImage

from probe_station_gui.stage.motion_prediction import interpolate_position
from probe_station_gui.stage.axis_calibration import StageAxisCalibrationMapper
from probe_station_gui.stage.axis_mapping import evaluate_polynomial
from probe_station_gui.stage.connection_state import StageControllerConnectionMixin
from probe_station_gui.stage.autofocus_math import (
    autofocus_sweep_feedrate_mm_min,
    estimate_shift,
    estimate_shift_with_response,
    focus_metric,
    frame_rate_from_timestamps,
    parabolic_focus_peak,
    qimage_to_gray,
    static_focus_candidates,
)
from probe_station_gui.stage.errors import (
    AxisStateError,
    SERIAL_IO_EXCEPTIONS,
    StageControllerError,
)
from probe_station_gui.stage.feed_override import (
    clamp_feed_override_percent,
    feed_override_payload_for_percent_change,
    feed_override_percent_for_feedrates,
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
    absolute_axis_targets_jog_command,
    format_gcode_value,
    jog_command_feedrate,
    move_vector_from_axis_distances,
    move_vector_from_jog_command,
    relative_jog_command_to_absolute,
)
from probe_station_gui.stage.motion_timing import (
    absolute_move_distance_for_timeout,
    idle_timeout_for_distance,
    move_distance_for_timeout,
)
from probe_station_gui.stage.serial_write_queue import (
    StageControllerSerialWriteQueueMixin,
)
from probe_station_gui.stage.status_io import StageControllerStatusIOMixin
from probe_station_gui.stage.needle_actions import StageControllerNeedleActionsMixin
from probe_station_gui.stage.needle_targets import (
    normalise_needle_contact_zone,
    normalise_needle_lowering_target,
)
from probe_station_gui.stage.needle_status import StageControllerNeedleStatusMixin
from probe_station_gui.stage.types import (
    AutofocusResult,
    MoveVector,
    _AutofocusContext,
    _FocusSweepResult,
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
    StageControllerSerialWriteQueueMixin,
    StageControllerStatusIOMixin,
    StageControllerFluidNCConfigIOMixin,
    StageControllerNeedleStatusMixin,
    StageControllerNeedleActionsMixin,
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

    def request_startup_sync(
        self, *, auto_home_a: bool = True, clear_unverified_state: bool = False
    ) -> None:
        """Load controller state after connect and optionally home A."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Skipping startup sync.")
                return
            if clear_unverified_state:
                self._clear_unverified_controller_state_locked()
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_startup_sync,
                args=(bool(auto_home_a),),
                daemon=True,
            )
            self._active_thread = thread
            thread.start()

    def check_motion_safety(self) -> None:
        """Public motion safety gate; raises StageControllerError when unsafe."""

        self._require_open_serial()
        self._move_safety_check()

    # Jog stop confirmation is handled in the joystick layer to avoid serial contention.

    def set_motion_safety_disabled(self, disabled: bool) -> None:
        """Enable or disable the explicit motion safety bypass."""

        self._motion_safety_disabled = bool(disabled)
        if self._motion_safety_disabled:
            logger.warning("Motion safety disabled")
        else:
            logger.info("Motion safety enabled")

    def set_unsafe_motion_enabled(self, enabled: bool) -> None:
        """Backward-compatible alias for persisted pre-split settings."""

        self.set_motion_safety_disabled(enabled)

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

    def on_frame_ready(self, frame: QImage) -> None:
        """Receive camera frames and cache them as grayscale numpy arrays."""

        gray = self._qimage_to_gray(frame)
        timestamp = time.monotonic()
        with self._frame_condition:
            self._latest_frame = gray
            self._frame_counter += 1
            self._frame_history.append((self._frame_counter, timestamp, gray.copy()))
            self._frame_condition.notify_all()

    def _poll_status_once(self) -> None:
        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            return
        try:
            if not self._serial_session_lock.acquire(blocking=False):
                return
            try:
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

    def request_move(self, dx_pixels: float, dy_pixels: float) -> bool:
        """Begin an asynchronous move so the clicked point aligns with the cross."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring the new click.")
                return False
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_move,
                args=(dx_pixels, dy_pixels),
                daemon=True,
            )
            self._active_thread = thread
            thread.start()
            return True

    def request_move_to_xy(self, target_x_mm: float, target_y_mm: float) -> None:
        """Move to an absolute X/Y coordinate in the configured report mode."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring absolute move request.")
                return
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_move_to_xy,
                args=(float(target_x_mm), float(target_y_mm)),
                daemon=True,
            )
            self._active_thread = thread
            thread.start()

    def request_move_to_xyz(
        self,
        target_x_mm: float,
        target_y_mm: float,
        target_z_mm: float,
        transit_z_mm: float | None = None,
        label: str = "saved position",
    ) -> None:
        """Move to an absolute X/Y/Z point using a safe intermediate Z level."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring XYZ move request.")
                return
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_move_to_xyz,
                args=(
                    float(target_x_mm),
                    float(target_y_mm),
                    float(target_z_mm),
                    None if transit_z_mm is None else float(transit_z_mm),
                    str(label),
                ),
                daemon=True,
            )
            self._active_thread = thread
            thread.start()

    def request_rotate_b(self, delta_deg: float) -> None:
        """Rotate the B axis by a relative angle in the background."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring B rotation request.")
                return
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_rotate_b,
                args=(float(delta_deg),),
                daemon=True,
            )
            self._active_thread = thread
            thread.start()

    def request_manual_axis_move(
        self,
        axis: str,
        distance_mm: float,
        mode: str,
        feedrate: float | None = None,
        allow_unhomed: bool = False,
    ) -> bool:
        """Move an arbitrary axis from the manual jog controls.

        G91 is accepted as a UI-relative input mode, but it is resolved to an
        absolute G90 target before anything is sent to the controller.
        """

        axis = axis.upper().strip()
        if axis not in self.AXIS_INDEX:
            self.status_message.emit(f"Unsupported axis: {axis}")
            return False
        mode = mode.upper().strip()
        if mode not in {"G90", "G91"}:
            self.status_message.emit(f"Unsupported manual move mode: {mode}")
            return False
        try:
            effective_feedrate = (
                None if feedrate is None else max(self.MIN_FEEDRATE, float(feedrate))
            )
        except (TypeError, ValueError):
            self.status_message.emit(f"Unsupported manual feedrate: {feedrate}")
            return False
        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring manual axis move.")
                return False
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_manual_axis_move,
                args=(
                    axis,
                    float(distance_mm),
                    mode,
                    effective_feedrate,
                    bool(allow_unhomed),
                ),
                daemon=True,
            )
            self._active_thread = thread
            thread.start()
            return True

    def request_absolute_axis_move(
        self,
        axis: str,
        target_mm: float,
        feedrate: float | None = None,
        allow_unhomed: bool = True,
    ) -> bool:
        """Move one axis to an absolute coordinate in the configured report mode."""

        return self.request_absolute_axis_targets_move(
            {axis: target_mm},
            feedrate=feedrate,
            allow_unhomed=allow_unhomed,
        )

    def request_absolute_axis_targets_move(
        self,
        targets: dict[str, float],
        *,
        feedrate: float | None = None,
        allow_unhomed: bool = True,
    ) -> bool:
        """Move multiple axes to absolute coordinates in one controller command."""

        normalized: dict[str, float] = {}
        for raw_axis, raw_value in targets.items():
            axis = str(raw_axis).upper().strip()
            if axis not in self.AXIS_INDEX:
                self.status_message.emit(f"Unsupported axis: {axis}")
                return False
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                self.status_message.emit(f"Unsupported target for {axis}: {raw_value}")
                return False
            if not math.isfinite(value):
                self.status_message.emit(f"Unsupported target for {axis}: {raw_value}")
                return False
            normalized[axis] = value
        if not normalized:
            self.status_message.emit("No coordinate targets provided.")
            return False
        try:
            effective_feedrate = (
                None if feedrate is None else max(self.MIN_FEEDRATE, float(feedrate))
            )
        except (TypeError, ValueError):
            self.status_message.emit(f"Unsupported manual feedrate: {feedrate}")
            return False
        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring coordinate move.")
                return False
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_absolute_axis_targets_move,
                args=(dict(normalized), effective_feedrate, bool(allow_unhomed)),
                daemon=True,
            )
            self._active_thread = thread
            thread.start()
            return True

    def request_autofocus(self) -> None:
        """Begin an asynchronous autofocus sweep along the Z axis."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring autofocus request.")
                return
            self._cancel_event.clear()
            thread = threading.Thread(target=self._run_autofocus, daemon=True)
            self._active_thread = thread
            thread.start()

    def request_home_axis(self, axis: str) -> bool:
        """Home a specific axis via a background task."""

        axis = axis.upper().strip()
        if not axis:
            return False
        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring home request.")
                return False
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_home, args=(f"$H{axis}", axis), daemon=True
            )
            self._active_thread = thread
            self.homing_action_started.emit(axis)
            thread.start()
            return True

    def request_home_all(self) -> bool:
        """Home all axes via a background task."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring home request.")
                return False
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_home, args=("$H", "ALL"), daemon=True
            )
            self._active_thread = thread
            self.homing_action_started.emit("ALL")
            thread.start()
            return True

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
            with self._serial_session() as serial_connection:
                rates = self._query_axis_max_feedrates_locked(serial_connection)
        self.apply_axis_max_feedrates(rates)
        self.axis_max_feedrates_changed.emit(dict(rates))
        return rates

    def apply_axis_a_calibration(self, calibration: object | None) -> None:
        """Apply the compact nonlinear A-axis calibration model."""

        if calibration is None or not bool(getattr(calibration, "configured", False)):
            self._axis_a_calibration = None
            return
        model = str(getattr(calibration, "model", "")).strip()
        if model != "cosine_displacement":
            self._axis_a_calibration = None
            return
        try:
            offset = float(getattr(calibration, "offset_mm"))
            if offset > 0.0:
                offset = -offset
            amplitude = float(getattr(calibration, "amplitude_mm"))
            if amplitude > 0.0:
                amplitude = -amplitude
            values: dict[str, float | str] = {
                "model": model,
                "steps_per_mm": float(getattr(calibration, "steps_per_mm")),
                "min": float(getattr(calibration, "commanded_lowering_min_mm")),
                "max": float(getattr(calibration, "commanded_lowering_max_mm")),
                "offset": offset,
                "amplitude": amplitude,
                "angular_frequency": float(
                    getattr(calibration, "angular_frequency_rad_per_mm")
                ),
                "phase": float(getattr(calibration, "phase_rad")),
            }
        except (TypeError, ValueError):
            self._axis_a_calibration = None
            return
        if (
            float(values["steps_per_mm"]) <= 0
            or float(values["max"]) <= float(values["min"])
            or abs(float(values["amplitude"])) <= 1e-12
            or float(values["angular_frequency"]) <= 0
        ):
            self._axis_a_calibration = None
            return
        self._axis_a_calibration = values

    def apply_axis_z_calibration(self, calibration: object | None) -> None:
        """Apply the compact nonlinear Z-axis calibration model."""

        if calibration is None or not bool(getattr(calibration, "configured", False)):
            self._axis_z_calibration = None
            return
        model = str(getattr(calibration, "model", "")).strip()
        if model != "quintic_polynomial":
            self._axis_z_calibration = None
            return
        try:
            coefficients = tuple(
                float(value) for value in getattr(calibration, "coefficients_mm")
            )
            values: dict[str, float | str | tuple[float, ...]] = {
                "model": model,
                "steps_per_mm": float(getattr(calibration, "steps_per_mm")),
                "min": float(getattr(calibration, "gcode_min_mm")),
                "max": float(getattr(calibration, "gcode_max_mm")),
                "coefficients": coefficients,
            }
        except (TypeError, ValueError):
            self._axis_z_calibration = None
            return
        if (
            float(values["steps_per_mm"]) <= 0
            or float(values["max"]) <= float(values["min"])
            or len(coefficients) != 6
            or not all(math.isfinite(value) for value in coefficients)
        ):
            self._axis_z_calibration = None
            return
        self._axis_z_calibration = values

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

    def request_oscillation(
        self, mode: str, amplitude_mm: float, feedrate: float, turns_per_sweep: float = 3.0
    ) -> None:
        """Start one of the repeated motion patterns."""

        mode_key = mode.upper().strip()
        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring oscillation request.")
                return
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_oscillation,
                args=(
                    mode_key,
                    float(amplitude_mm),
                    float(feedrate),
                    float(turns_per_sweep),
                ),
                daemon=True,
            )
            self._active_thread = thread
            thread.start()

    def request_stop_oscillation(self) -> None:
        """Stop the active oscillation task if one is running."""

        if not self._oscillation_active:
            return
        self._cancel_event.set()
        self.status_message.emit("Oscillation stop requested.")

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
            try:
                waiting = serial_connection.in_waiting
            except _SERIAL_IO_EXCEPTIONS as exc:  # pragma: no cover - hardware interaction
                raise StageControllerError(f"Serial read failed: {exc}") from exc
            if waiting <= 0:
                return b""
            if max_bytes is not None:
                waiting = min(waiting, max(1, int(max_bytes)))
            logger.debug("SERIAL TRACE terminal_in_waiting bytes=%s", waiting)
            try:
                data = serial_connection.read(waiting)
            except _SERIAL_IO_EXCEPTIONS as exc:  # pragma: no cover - hardware interaction
                raise StageControllerError(f"Serial read failed: {exc}") from exc
            if data:
                logger.debug("SERIAL TRACE terminal_read bytes=%r", data[:200])
                self._handle_pending_serial_data_side_effects(
                    data, "terminal pending read"
                )
            return data
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

    def run_external_move_to_xy(
        self,
        target_x_mm: float,
        target_y_mm: float,
        *,
        feedrate: float | None = None,
    ) -> str:
        """Run a blocking X/Y move inside an external controller reservation."""

        self.movement_started.emit()
        try:
            self._check_cancelled()
            with self._serial_session():
                self._move_safety_check()
                message = self._move_to_xy_locked(
                    float(target_x_mm),
                    float(target_y_mm),
                    feedrate=feedrate,
                )
            self.movement_finished.emit(True, message)
            return message
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
            raise

    def run_external_absolute_axis_targets_move(
        self,
        targets: dict[str, float],
        *,
        feedrate: float | None = None,
        allow_unhomed: bool = False,
    ) -> str:
        """Run a blocking absolute coordinate move inside an external reservation."""

        normalized_targets: dict[str, float] = {}
        for raw_axis, raw_value in targets.items():
            axis = str(raw_axis).upper().strip()
            if axis not in self.AXIS_INDEX:
                raise StageControllerError(f"Unsupported axis: {raw_axis}")
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise StageControllerError(
                    f"Unsupported target for {axis}: {raw_value}"
                ) from exc
            if not math.isfinite(value):
                raise StageControllerError(
                    f"Unsupported target for {axis}: {raw_value}"
                )
            normalized_targets[axis] = value
        ordered_targets = {
            axis: normalized_targets[axis]
            for axis in self.AXIS_INDEX
            if axis in normalized_targets
        }
        if not ordered_targets:
            raise StageControllerError("No coordinate targets provided.")

        self.movement_started.emit()
        try:
            self._check_cancelled()
            feedrate_text = (
                self.DEFAULT_FEEDRATE
                if feedrate is None
                else max(self.MIN_FEEDRATE, float(feedrate))
            )
            target_text = " ".join(
                f"{axis}{value:+.3f}" for axis, value in ordered_targets.items()
            )
            self.status_message.emit(
                "Coordinate move (G90): "
                f"{target_text} F{self._format_gcode_value(feedrate_text)}."
            )
            with self._serial_session() as serial_connection:
                self._send_absolute_axis_targets_move(
                    ordered_targets,
                    ignore_needle_safety=self._motion_safety_disabled,
                    feedrate=feedrate,
                    wait_for_completion=True,
                    allow_unhomed=allow_unhomed,
                    as_jog=True,
                )
                self._query_status(serial_connection)
            message = f"Coordinate move complete (G90 {target_text})."
            self.movement_finished.emit(True, message)
            return message
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
            raise

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

    def run_external_local_autofocus(
        self,
        *,
        range_mm: float,
        step_mm: float | None = None,
    ) -> AutofocusResult:
        """Run a fast local Z autofocus inside an external reservation."""

        self.movement_started.emit()
        try:
            self._check_cancelled()
            with self._serial_session():
                result = self._run_local_autofocus_locked(
                    range_mm=range_mm,
                    step_mm=step_mm,
                )
            message = result.summary()
            self.autofocus_finished.emit(True, message)
            self.movement_finished.emit(True, message)
            return result
        except StageControllerError as exc:
            message = str(exc)
            self.autofocus_finished.emit(False, message)
            self.movement_finished.emit(False, message)
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

    def homed_axes(self) -> set[str]:
        """Return a snapshot of axes that the live controller reported as homed."""

        return set(self._homed_axes)

    def axes_are_homed(self, axes: set[str]) -> bool:
        """Return True when every requested axis is known to be homed."""

        return set(axes).issubset(self._homed_axes)

    def mark_axes_unhomed(self, axes: Iterable[str]) -> set[str]:
        """Clear cached homing trust for specific axes.

        Returns the axes that were actually removed from the cached homed set.
        """

        normalized_axes = {
            str(axis).strip().upper()
            for axis in axes
            if str(axis).strip()
        }
        removed_axes = self._homed_axes.intersection(normalized_axes)
        if not removed_axes:
            return set()
        self._update_homing_status(self._homed_axes - removed_axes)
        return set(removed_axes)

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

    def current_fov_size_mm(self) -> tuple[float, float] | None:
        """Estimate the camera field of view in millimeters from click calibration."""

        if self._pixels_to_mm is None:
            return None
        with self._frame_condition:
            frame = self._latest_frame
            if frame is None:
                return None
            height_px, width_px = frame.shape[:2]
        column_x = self._pixels_to_mm[:, 0]
        column_y = self._pixels_to_mm[:, 1]
        width_mm = float(np.linalg.norm(column_x) * float(width_px))
        height_mm = float(np.linalg.norm(column_y) * float(height_px))
        return (width_mm, height_mm)

    def resolve_clicked_point_xy(
        self, dx_pixels: float, dy_pixels: float
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        """Resolve the center and clicked image point to absolute FluidNC XY."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                raise StageControllerError(
                    "Stage is busy. Wait for the current operation to finish."
                )
            with self._serial_session():
                status = self._query_current_status_with_required_coordinates(
                    axes=("X", "Y"),
                )
                if status is None or status.display_position is None:
                    raise StageControllerError("Unable to read stage position.")
                self._ensure_calibration()
        if self._pixels_to_mm is None:
            raise StageControllerError("Calibration failed. Cannot resolve clicked position.")
        return self._resolve_xy_from_center(
            tuple(float(value) for value in status.display_position),
            dx_pixels,
            dy_pixels,
        )

    def preview_clicked_point_xy(
        self, dx_pixels: float, dy_pixels: float
    ) -> tuple[tuple[float, float], tuple[float, float]] | None:
        """Project a hovered image point using cached position/calibration only."""

        if self._pixels_to_mm is None or self._last_stage_position is None:
            return None
        return self._resolve_xy_from_center(
            self._last_stage_position,
            dx_pixels,
            dy_pixels,
        )

    def zero_b_axis(self) -> None:
        """Set the current B coordinate as the application zero reference."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                raise StageControllerError(
                    "Stage is busy. Wait for the current operation to finish."
                )
            with self._serial_session():
                status = self._query_current_status_with_required_coordinates(
                    axes=("B",),
                )
        if status is None or self._axis_value_for_configured_mode(status, "B") is None:
            raise StageControllerError("Unable to read B axis position.")
        self._set_b_axis_zero_reference(status)

    def queue_jog_command(self, command: str) -> None:
        """Queue the latest jog command for asynchronous serial delivery."""

        if self.is_busy():
            raise StageControllerError("Stage is busy. Wait for the current operation to finish.")
        stripped = command.strip()
        if not stripped:
            return
        move = self._move_vector_from_jog_command(stripped)
        if move is not None and not self._motion_safety_disabled:
            checked_move = self._check_cached_jog_move_limits(move)
            stripped = self._absolute_jog_command_for_relative_move(
                stripped,
                checked_move,
            )
        self._queued_jog_generation += 1
        self._jog_motion_active = True
        self.queue_feed_override_reset()
        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_JOG_COMMAND,
                sequence=self._next_queued_write_sequence(),
                kind="jog_command",
                payload=(stripped + "\n").encode("ascii"),
                description=stripped,
                generation=self._queued_jog_generation,
                clear_epoch=self._async_write_clear_epoch,
            )
        )

    def queue_absolute_axis_targets_jog(
        self,
        targets: dict[str, float],
        *,
        feedrate: float,
        replace_active: bool = False,
    ) -> bool:
        """Stop and requeue an absolute jog target through the jog command path."""

        active_busy = self.is_busy()
        if active_busy and not replace_active:
            raise StageControllerError("Stage is busy. Wait for the current operation to finish.")
        normalized: dict[str, float] = {}
        for raw_axis, raw_value in targets.items():
            axis = str(raw_axis).upper().strip()
            if axis not in self.AXIS_INDEX:
                self.status_message.emit(f"Unsupported axis: {axis}")
                return False
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                self.status_message.emit(f"Unsupported target for {axis}: {raw_value}")
                return False
            if not math.isfinite(value):
                self.status_message.emit(f"Unsupported target for {axis}: {raw_value}")
                return False
            normalized[axis] = value
        if not normalized:
            self.status_message.emit("No coordinate targets provided.")
            return False
        try:
            effective_feedrate = max(self.MIN_FEEDRATE, float(feedrate))
        except (TypeError, ValueError):
            self.status_message.emit(f"Unsupported manual feedrate: {feedrate}")
            return False
        command = self._absolute_axis_targets_jog_command(
            normalized,
            effective_feedrate,
        )
        if not command:
            return False
        if active_busy:
            self._cancel_event.set()
        self.queue_jog_stop()
        self._queued_jog_generation += 1
        self._jog_motion_active = False
        self.queue_feed_override_reset()
        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_JOG_COMMAND,
                sequence=self._next_queued_write_sequence(),
                kind="jog_command",
                payload=(command + "\n").encode("ascii"),
                description=command,
                generation=self._queued_jog_generation,
                clear_epoch=self._async_write_clear_epoch,
            )
        )
        return True

    @classmethod
    def feed_override_percent_for_feedrates(
        cls, programmed_feedrate: float, target_feedrate: float
    ) -> int:
        return feed_override_percent_for_feedrates(
            programmed_feedrate,
            target_feedrate,
            min_feedrate=cls.MIN_FEEDRATE,
            min_percent=cls.FEED_OVERRIDE_MIN_PERCENT,
            max_percent=cls.FEED_OVERRIDE_MAX_PERCENT,
        )

    @classmethod
    def _feed_override_payload_for_percent_change(
        cls,
        current_percent: int,
        target_percent: int,
        *,
        reset_first: bool = False,
    ) -> tuple[bytes, int]:
        return feed_override_payload_for_percent_change(
            current_percent,
            target_percent,
            reset_first=reset_first,
            min_percent=cls.FEED_OVERRIDE_MIN_PERCENT,
            max_percent=cls.FEED_OVERRIDE_MAX_PERCENT,
            reset_payload=cls.FEED_OVERRIDE_RESET,
            plus_10_payload=cls.FEED_OVERRIDE_PLUS_10,
            minus_10_payload=cls.FEED_OVERRIDE_MINUS_10,
            plus_1_payload=cls.FEED_OVERRIDE_PLUS_1,
            minus_1_payload=cls.FEED_OVERRIDE_MINUS_1,
        )

    def queue_feed_override_for_feedrate(
        self, programmed_feedrate: float, target_feedrate: float
    ) -> int | None:
        """Queue realtime feed override bytes for an already-running G1 move."""

        try:
            target_percent = self.feed_override_percent_for_feedrates(
                programmed_feedrate, target_feedrate
            )
        except (TypeError, ValueError, ZeroDivisionError):
            return None
        return self._queue_feed_override_percent(target_percent)

    def queue_active_needles_feedrate(self, target_feedrate: float) -> int | None:
        """Apply a realtime feed override to an active needle G1 move."""

        with self._task_lock:
            programmed_feedrate = self._active_needles_programmed_feedrate
            action = self._active_needles_action
        if action not in {"raise", "lower", "adjust"} or programmed_feedrate is None:
            return None
        return self.queue_feed_override_for_feedrate(
            programmed_feedrate,
            target_feedrate,
        )

    def queue_feed_override_reset(self) -> int | None:
        """Return feed override to 100% without blocking the UI."""

        with self._feed_override_lock:
            current = self._active_feed_override_percent
        if current in (None, 100):
            return current
        return self._queue_feed_override_percent(100)

    def _queue_feed_override_percent(self, target_percent: int) -> int | None:
        target = clamp_feed_override_percent(
            target_percent,
            min_percent=self.FEED_OVERRIDE_MIN_PERCENT,
            max_percent=self.FEED_OVERRIDE_MAX_PERCENT,
        )
        with self._feed_override_lock:
            current = self._active_feed_override_percent
            reset_first = current is None
            payload, applied = self._feed_override_payload_for_percent_change(
                100 if current is None else current,
                target,
                reset_first=reset_first,
            )
            if not payload:
                self._active_feed_override_percent = applied
                return applied
            self._active_feed_override_percent = applied
        self._queued_feed_override_generation += 1
        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_FEED_OVERRIDE,
                sequence=self._next_queued_write_sequence(),
                kind="feed_override",
                payload=payload,
                description=f"feed override {applied}%",
                generation=self._queued_feed_override_generation,
                clear_epoch=self._async_write_clear_epoch,
            )
        )
        return applied

    def constrain_jog_distances(
        self,
        commanded_distances: object,
    ) -> tuple[tuple[str, float], ...]:
        """Clip UI jog distances so homed axes do not cross software limits."""

        if not isinstance(commanded_distances, (tuple, list)):
            return tuple()
        normalized: list[tuple[str, float]] = []
        for item in commanded_distances:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                continue
            axis = str(item[0]).strip().upper()
            if axis not in self.AXIS_INDEX:
                continue
            try:
                distance = float(item[1])
            except (TypeError, ValueError):
                continue
            if abs(distance) < 1e-6:
                continue
            normalized.append((axis, distance))
        if not normalized or self._motion_safety_disabled:
            return tuple(normalized)

        self._refresh_cached_jog_status_if_missing()
        move = self._move_vector_from_axis_distances(normalized)
        clipped = self._clip_relative_move_to_software_limits(move, emit_status=True)
        clipped_values = {axis: value for axis, value in clipped.items()}
        return tuple(
            (axis, clipped_values[axis])
            for axis, _distance in normalized
            if abs(clipped_values.get(axis, 0.0)) >= 1e-6
        )

    def _move_vector_from_jog_command(self, command: str) -> MoveVector | None:
        return move_vector_from_jog_command(command, axis_index=self.AXIS_INDEX)

    def _jog_command_feedrate(self, command: str) -> float | None:
        return jog_command_feedrate(command, min_feedrate=self.MIN_FEEDRATE)

    def _absolute_jog_command_for_relative_move(
        self,
        command: str,
        move: MoveVector,
    ) -> str:
        return relative_jog_command_to_absolute(
            command,
            move,
            position=self._last_stage_position,
            axis_index=self.AXIS_INDEX,
            min_feedrate=self.MIN_FEEDRATE,
            machine_position_mode=self._position_reporting_mode == "machine",
            axis_skip_reason=self._cached_jog_axis_skip_reason,
        )

    def _move_vector_from_axis_distances(
        self, distances: list[tuple[str, float]] | tuple[tuple[str, float], ...]
    ) -> MoveVector:
        return move_vector_from_axis_distances(
            distances,
            axis_index=self.AXIS_INDEX,
        )

    def _clip_relative_move_to_software_limits(
        self,
        move: MoveVector,
        *,
        emit_status: bool = False,
    ) -> MoveVector:
        if move.is_zero() or self._motion_safety_disabled:
            return move
        position = self._last_stage_position
        if position is None:
            return move
        values = {axis: delta for axis, delta in move.items()}
        for axis, delta in move.items():
            if abs(delta) < 1e-6:
                continue
            idx = self.AXIS_INDEX.get(axis)
            reason = self._cached_jog_axis_skip_reason(axis, position)
            if reason:
                if emit_status:
                    self.status_message.emit(
                        f"Jog soft limit unavailable on {axis}: {reason}."
                    )
                continue
            if axis == "B":
                clipped_delta = self._clip_b_relative_delta(delta)
                if emit_status and abs(clipped_delta - delta) >= 1e-6:
                    self.status_message.emit(
                        "Jog limited by software soft limit: "
                        f"B delta {delta:+.3f} clipped to {clipped_delta:+.3f}."
                    )
                values[axis] = clipped_delta
                continue
            limits = self._axis_limits_for_configured_mode(axis, None)
            current = float(position[idx])
            min_value, max_value = limits
            target = current + float(delta)
            clipped_target = min(max(target, min_value), max_value)
            clipped_delta = clipped_target - current
            if abs(clipped_delta - delta) >= 1e-6 and emit_status:
                self.status_message.emit(
                    "Jog limited by software soft limit: "
                    f"{axis} target {target:+.3f} clipped to "
                    f"{clipped_target:+.3f} "
                    f"(limit {min_value:.3f}..{max_value:.3f})."
                )
            values[axis] = clipped_delta
        return MoveVector(
            x=values["X"],
            y=values["Y"],
            z=values["Z"],
            a=values["A"],
            b=values["B"],
            c=values["C"],
        )

    def _refresh_cached_jog_status_if_missing(self) -> None:
        if self._last_stage_position is not None:
            return
        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            return
        if not self._serial_session_lock.acquire(blocking=False):
            return
        try:
            self._query_status(serial_connection, timeout=0.5)
        except StageControllerError:
            return
        finally:
            self._serial_session_lock.release()

    def _cached_jog_axis_skip_reason(
        self,
        axis: str,
        position: tuple[float, ...],
    ) -> str | None:
        axis = axis.upper().strip()
        idx = self.AXIS_INDEX.get(axis)
        if idx is None:
            return "unsupported axis"
        if idx >= len(position):
            return "current position is unavailable"
        if axis == "B":
            if self._b_axis_zero_position is None:
                return "B zero reference is unavailable"
            return None
        if axis not in self._axis_limits:
            return "software limits are unavailable"
        limits = self._axis_limits_for_configured_mode(axis, None)
        if limits is None:
            return "software limits are unavailable in the current coordinate system"
        if not self._axis_software_limit_ready(None, axis):
            return "homing state is unavailable"
        return None

    def _clip_b_relative_delta(self, delta: float) -> float:
        if self._last_stage_position is None:
            return float(delta)
        idx = self.AXIS_INDEX.get("B")
        if idx is None or idx >= len(self._last_stage_position):
            return float(delta)
        if self._b_axis_zero_position is None:
            return float(delta)
        current_b = float(self._last_stage_position[idx]) - float(
            self._b_axis_zero_position
        )
        limit = self.B_AXIS_SOFT_LIMIT_DEG
        target_b = current_b + float(delta)
        clipped_target = min(max(target_b, -limit), limit)
        return clipped_target - current_b

    def _check_cached_jog_move_limits(self, move: MoveVector) -> MoveVector:
        clipped = self._clip_relative_move_to_software_limits(move)
        for axis, original_delta in move.items():
            clipped_delta = dict(clipped.items()).get(axis, 0.0)
            if abs(original_delta - clipped_delta) >= 1e-6:
                raise StageControllerError(
                    f"Jog exceeds software soft limit on {axis}."
                )
        return clipped

    def queue_jog_stop(self) -> None:
        """Queue a jog stop command without blocking the UI thread."""

        # Invalidate any queued-but-not-yet-written jog command so a late $J
        # cannot arrive after the stop and keep motion alive.
        self._queued_jog_generation += 1
        self._jog_motion_active = False
        job = _QueuedSerialWrite(
            priority=self.SERIAL_PRIORITY_JOG_STOP,
            sequence=self._next_queued_write_sequence(),
            kind="jog_stop",
            payload=b"\x85",
            description="0x85",
            generation=self._queued_jog_generation,
            clear_epoch=self._async_write_clear_epoch,
        )
        if self._try_write_jog_stop_immediately(job):
            return
        self._async_write_queue.put(job)

    def force_jog_stop(self, timeout: float = 0.5) -> bool:
        """Best-effort synchronous jog stop before closing the serial port."""

        self._queued_jog_generation += 1
        self._jog_motion_active = False
        self._clear_pending_async_writes()
        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            return False
        acquired = self._serial_session_lock.acquire(timeout=max(0.0, float(timeout)))
        if not acquired:
            logger.warning("Unable to acquire serial lock for emergency jog stop.")
            return False
        try:
            job = _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_JOG_STOP,
                sequence=self._next_queued_write_sequence(),
                kind="jog_stop",
                payload=b"\x85",
                description="0x85 emergency",
                generation=self._queued_jog_generation,
                clear_epoch=self._async_write_clear_epoch,
            )
            self._write_async_job(serial_connection, job)
            logger.warning("Emergency jog stop written before serial shutdown.")
            return True
        except StageControllerError as exc:
            logger.warning("Emergency jog stop failed before serial shutdown: %s", exc)
            self.status_message.emit(str(exc))
            return False
        finally:
            self._serial_session_lock.release()

    def _try_write_jog_stop_immediately(self, job: _QueuedSerialWrite) -> bool:
        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            return True
        if not self._serial_session_lock.acquire(blocking=False):
            return False
        try:
            self._write_async_job(serial_connection, job)
            return True
        except StageControllerError as exc:
            self.status_message.emit(str(exc))
            return True
        finally:
            self._serial_session_lock.release()

    def queue_soft_reset(self, *, source: str = "unknown") -> None:
        """Queue a FluidNC soft reset without blocking the UI thread."""

        with self._task_lock:
            self._clear_unverified_controller_state_locked()
        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_SOFT_RESET,
                sequence=self._next_queued_write_sequence(),
                kind="soft_reset",
                payload=b"\x18",
                description=f"CTRL-X source={source}",
                clear_epoch=self._async_write_clear_epoch,
            )
        )

    def queue_manual_command(self, command: str) -> None:
        """Queue a manual terminal command without blocking the UI thread."""

        if self.is_busy():
            raise StageControllerError("Cannot send while automated move is running.")
        payload = command if command.endswith("\n") else f"{command}\n"
        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_TERMINAL,
                sequence=self._next_queued_write_sequence(),
                kind="terminal",
                payload=payload.encode("utf-8"),
                description=payload.rstrip(),
                clear_epoch=self._async_write_clear_epoch,
            )
        )

    def reset_calibration(self, reason: str = "Click calibration reset.") -> None:
        """Clear the click-to-move calibration so it is rebuilt on next use."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                raise StageControllerError(
                    "Stage is busy. Wait for the current operation to finish."
                )
            self._pixels_to_mm = None
            self._objective_matrices.pop(self._active_objective_name, None)
            self._objective_calibration_verified[self._active_objective_name] = False
        self.objective_calibration_updated.emit(self._active_objective_name, [])
        self.status_message.emit(reason)

    def _run_move(self, dx_pixels: float, dy_pixels: float) -> None:
        self.movement_started.emit()
        try:
            self._check_cancelled()
            with self._serial_session():
                self._move_safety_check()
                before_counter = self._prepare_click_move_without_status_locked(
                    dx_pixels,
                    dy_pixels,
                )
            if before_counter is None:
                self.movement_finished.emit(True, "Target already centered.")
                return
            after_frame, _ = self._wait_for_new_frame(before_counter, timeout=4.0)
            if after_frame is None:
                self.movement_finished.emit(
                    False,
                    "Movement command sent but camera did not provide an updated frame.",
                )
                return

            self.movement_finished.emit(True, "Move complete.")
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                self._active_thread = None

    def _run_move_to_xy(self, target_x_mm: float, target_y_mm: float) -> None:
        self.movement_started.emit()
        try:
            self._check_cancelled()
            with self._serial_session():
                self._move_safety_check()
                message = self._move_to_xy_locked(target_x_mm, target_y_mm)
            self.movement_finished.emit(True, message)
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                self._active_thread = None

    def _run_move_to_xyz(
        self,
        target_x_mm: float,
        target_y_mm: float,
        target_z_mm: float,
        transit_z_mm: float | None,
        label: str,
    ) -> None:
        self.movement_started.emit()
        try:
            self._check_cancelled()
            with self._serial_session():
                self._move_safety_check()
                message = self._move_to_xyz_locked(
                    target_x_mm,
                    target_y_mm,
                    target_z_mm,
                    transit_z_mm=transit_z_mm,
                    label=label,
                )
            self.movement_finished.emit(True, message)
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                self._active_thread = None

    def _move_to_xy_locked(
        self,
        target_x_mm: float,
        target_y_mm: float,
        *,
        feedrate: float | None = None,
    ) -> str:
        with self._serial_session_lock:
            status = self._query_synced_status_for_absolute_motion(
                refresh_coordinate_state=False,
                min_axes=2,
            )
            if status is None:
                raise StageControllerError("Unable to read current stage position.")
            self._require_homed_axes(status, {"X", "Y"})
            current_position = self._require_position_for_absolute_motion(
                status,
                required_axes=2,
            )
            target_position = (float(target_x_mm), float(target_y_mm))
            delta_x = float(target_position[0]) - float(current_position[0])
            delta_y = float(target_position[1]) - float(current_position[1])
            move = MoveVector(x=delta_x, y=delta_y)
            if move.is_zero(tol=1e-5):
                return "Target already at requested X/Y."
            self.status_message.emit(
                f"Moving to X={target_x_mm:.3f} mm, Y={target_y_mm:.3f} mm"
            )
            self._send_relative_move(
                move,
                feedrate=feedrate,
                as_jog=True,
                motion_started_callback=lambda _move, feedrate: (
                    self.absolute_xy_move_started.emit(
                        float(target_x_mm),
                        float(target_y_mm),
                        float(feedrate),
                    )
                ),
            )
            self._wait_for_idle()
            self._query_current_status()
            return f"Arrived at X={target_x_mm:.3f} mm, Y={target_y_mm:.3f} mm."

    def _move_to_xyz_locked(
        self,
        target_x_mm: float,
        target_y_mm: float,
        target_z_mm: float,
        *,
        transit_z_mm: float | None,
        label: str,
    ) -> str:
        with self._serial_session_lock:
            status = self._query_synced_status_for_absolute_motion(
                refresh_coordinate_state=False,
                min_axes=3,
            )
            if status is None:
                raise StageControllerError("Unable to read current stage position.")
            self._require_homed_axes(status, {"X", "Y", "Z"})
            current_position = self._require_position_for_absolute_motion(
                status,
                required_axes=3,
            )
            current_x = float(current_position[0])
            current_y = float(current_position[1])
            current_z = float(current_position[2])
            target_position = (float(target_x_mm), float(target_y_mm), float(target_z_mm))
            transit_z = (
                float(target_position[2])
                if transit_z_mm is None
                else float(transit_z_mm)
            )
            moved = False

            if abs(transit_z - current_z) >= 1e-5:
                self.status_message.emit(
                    f"Moving Z to safe transfer level {transit_z:.3f} mm before {label}."
                )
                self._send_relative_move(
                    MoveVector(z=transit_z - current_z),
                    as_jog=True,
                )
                current_z = transit_z
                moved = True

            delta_x = float(target_position[0]) - current_x
            delta_y = float(target_position[1]) - current_y
            xy_move = MoveVector(x=delta_x, y=delta_y)
            if not xy_move.is_zero(tol=1e-5):
                self.status_message.emit(
                    f"Moving to {label} X={target_x_mm:.3f} mm, Y={target_y_mm:.3f} mm"
                )
                self._send_relative_move(xy_move, as_jog=True)
                current_x = float(target_position[0])
                current_y = float(target_position[1])
                moved = True

            delta_z = float(target_position[2]) - current_z
            if abs(delta_z) >= 1e-5:
                self.status_message.emit(
                    f"Moving Z to {label} focus height {target_z_mm:.3f} mm"
                )
                self._send_relative_move(
                    MoveVector(z=delta_z),
                    as_jog=True,
                )
                moved = True

            self._wait_for_idle()
            self._query_current_status()

            if not moved:
                return f"{label.capitalize()} already reached."
            return (
                f"Arrived at {label}: X={target_x_mm:.3f} mm, "
                f"Y={target_y_mm:.3f} mm, Z={target_z_mm:.3f} mm."
            )

    def _query_synced_status_for_absolute_motion(
        self,
        *,
        refresh_coordinate_state: bool = True,
        axes: Iterable[str] | None = None,
        min_axes: int | None = None,
    ) -> Optional[_Status]:
        """Refresh coordinate-system state before absolute position reads and moves."""

        serial_connection = self._current_serial()
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
            self._refresh_coordinate_system_state(
                serial_connection,
                apply_preference=True,
            )
        return self._query_status_with_required_coordinates(
            serial_connection,
            axes=axes,
            min_axes=min_axes,
        )

    def _prepare_click_move_without_status_locked(
        self,
        dx_pixels: float,
        dy_pixels: float,
    ) -> int | None:
        with self._serial_session_lock:
            pixel_vector = np.array([dx_pixels, dy_pixels], dtype=float)
            target_handled, before_counter = self._ensure_calibration(
                target_pixels=pixel_vector,
            )
            self._check_cancelled()
            if target_handled:
                return before_counter
            if self._pixels_to_mm is None:
                raise StageControllerError("Calibration failed. Cannot move stage.")

            if abs(dx_pixels) < 1e-3 and abs(dy_pixels) < 1e-3:
                return None

            _, before_counter = self._get_frame_snapshot()
            mm_vector = -(self._pixels_to_mm @ pixel_vector)
            move = MoveVector(x=float(mm_vector[0]), y=float(mm_vector[1]))
            move_magnitude = float(np.linalg.norm(mm_vector))
            if move_magnitude > self.MAX_CLICK_MOVE_MM:
                self._pixels_to_mm = None
                raise StageControllerError(
                    "Predicted click move is too large; calibration was reset. Recalibrate and try again."
                )

            self.status_message.emit(
                f"Jogging stage dX={move.x:.3f} mm dY={move.y:.3f} mm"
            )
            self._send_relative_move(
                move,
                as_jog=True,
                motion_started_callback=self._emit_click_move_started,
            )
            return before_counter

    def _run_rotate_b(self, delta_deg: float) -> None:
        self.movement_started.emit()
        try:
            self._check_cancelled()
            with self._serial_session() as serial_connection:
                if abs(delta_deg) < 1e-3:
                    self.movement_finished.emit(True, "Chip is already aligned.")
                    return
                self.status_message.emit(f"Chip alignment: rotating B by {delta_deg:+.3f} deg.")
                self._send_relative_move(
                    MoveVector(b=delta_deg),
                    allow_relative=True,
                    as_jog=True,
                )
            self.movement_finished.emit(
                True,
                f"Chip alignment rotation complete (B {delta_deg:+.3f} deg).",
            )
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                self._active_thread = None

    def _run_autofocus(self) -> None:
        self.movement_started.emit()
        try:
            with self._serial_session():
                self._run_autofocus_locked()
        except StageControllerError as exc:
            self.autofocus_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                self._active_thread = None

    def _run_autofocus_locked(self) -> None:
        """Run autofocus while the caller owns serial access."""

        context = self._prepare_autofocus_context_locked(
            range_mm=self._objective_autofocus_range_mm,
            step_mm=self._objective_autofocus_fine_step_mm,
        )
        objective_name = context.objective_name
        local_range = context.local_range_mm
        fine_step = context.fine_step_mm
        sweep_feedrate = self._autofocus_sweep_feedrate_mm_min(fine_step)

        logger.debug(
            "Autofocus %s parameters start_z=%.6f range=%.6f fine_step=%.6f "
            "sweep_feedrate=%.6f",
            objective_name,
            context.start_z,
            local_range,
            fine_step,
            sweep_feedrate,
        )
        self.status_message.emit(
            f"Autofocus {objective_name}: continuous sweep within +/-{local_range:.3f} mm."
        )
        coarse = self._run_focus_sweep_locked(
            context.lower_z,
            context.upper_z,
            feedrate=sweep_feedrate,
        )
        fine_half_range = min(
            max(fine_step * 6.0, local_range * 0.15),
            local_range,
        )
        fine_lower = max(context.min_z, coarse.best_z - fine_half_range)
        fine_upper = min(context.max_z, coarse.best_z + fine_half_range)
        best = coarse
        if fine_upper - fine_lower >= fine_step * 2.0:
            self.status_message.emit(
                f"Autofocus {objective_name}: fine sweep."
            )
            best = self._run_focus_sweep_locked(
                fine_lower,
                fine_upper,
                feedrate=sweep_feedrate,
            )

        self.status_message.emit(
            f"Autofocus {objective_name}: static verification."
        )
        best = self._run_static_focus_refinement_locked(
            best.best_z,
            min_z=context.min_z,
            max_z=context.max_z,
            step_mm=fine_step,
        )
        self._approach_z_from_below_locked(
            best.best_z,
            min_z=context.min_z,
            fine_step_mm=fine_step,
        )
        message = (
            f"Autofocus {objective_name} complete. "
            f"Best score {best.best_score:.2f} at Z={best.best_z:.4f} mm "
            f"from {best.sample_count} verified frames."
        )
        if best.edge_peak:
            message += " Peak was near a search edge; consider increasing range."
        self.autofocus_finished.emit(True, message)

    def _run_local_autofocus_locked(
        self,
        *,
        range_mm: float,
        step_mm: float | None = None,
    ) -> AutofocusResult:
        context = self._prepare_autofocus_context_locked(
            range_mm=range_mm,
            step_mm=step_mm,
        )
        self.status_message.emit(
            "Autofocus "
            f"{context.objective_name}: local search within "
            f"+/-{context.local_range_mm:.3f} mm."
        )
        try:
            best = self._run_static_focus_refinement_locked(
                context.start_z,
                min_z=context.lower_z,
                max_z=context.upper_z,
                step_mm=context.fine_step_mm,
            )
            self._approach_z_from_below_locked(
                best.best_z,
                min_z=context.min_z,
                fine_step_mm=context.fine_step_mm,
            )
        except StageControllerError as exc:
            if str(exc) != "Operation cancelled.":
                raise
            self._restore_autofocus_start_z_after_cancel_locked(
                context,
            )
            raise
        return AutofocusResult(
            objective_name=context.objective_name,
            mode="local",
            start_z_mm=float(context.start_z),
            best_z_mm=float(best.best_z),
            best_score=float(best.best_score),
            sample_count=int(best.sample_count),
            edge_peak=bool(best.edge_peak),
            range_mm=float(context.local_range_mm),
            fine_step_mm=float(context.fine_step_mm),
            lower_z_mm=float(context.lower_z),
            upper_z_mm=float(context.upper_z),
        )

    def _restore_autofocus_start_z_after_cancel_locked(
        self,
        context: _AutofocusContext,
    ) -> None:
        """Return local autofocus to its starting Z after a user interrupt."""

        was_cancelled = self._cancel_event.is_set()
        self._cancel_event.clear()
        self._queued_jog_generation += 1
        self._clear_pending_async_writes()
        try:
            self.status_message.emit(
                f"Autofocus {context.objective_name}: returning to start Z."
            )
            self._write_realtime_payload(
                b"\x85",
                "autofocus cancel jog stop 0x85",
            )
            try:
                self._wait_for_idle(timeout=5.0)
            except StageControllerError:
                logger.warning(
                    "Could not confirm idle before restoring autofocus start Z.",
                    exc_info=True,
                )
            status = self._query_current_status_with_required_coordinates(
                axes=("Z",),
            )
            position = self._position_for_configured_mode(status)
            if status is None or position is None or len(position) < 3:
                raise StageControllerError(
                    "Unable to read Z position after autofocus cancel."
                )
            current_z = float(position[2])
            delta_z = float(context.start_z) - current_z
            if abs(delta_z) >= 1e-5:
                self._send_relative_move(
                    MoveVector(z=delta_z),
                    allow_relative=True,
                    as_jog=True,
                )
        finally:
            if was_cancelled:
                self._cancel_event.set()

    def _prepare_autofocus_context_locked(
        self,
        *,
        range_mm: float,
        step_mm: float | None,
    ) -> _AutofocusContext:
        serial_connection = self._current_serial()
        self._relative_warning_emitted = False
        objective_name = str(self._active_objective_name)
        if not self._needles_up:
            self.status_message.emit("Autofocus: homing A axis.")
            self._write_current_command_and_wait("$HA", timeout=30.0)
            self._wait_for_idle(timeout=30.0)
            self._set_needles_state(True, known=True, zone="raise")
        self._move_safety_check()

        self._ensure_axis_limits(serial_connection, required_axes=("Z",))
        local_range = self._positive_profile_value(
            range_mm,
            self._objective_autofocus_range_mm,
        )
        fine_step = self._positive_profile_value(
            self._objective_autofocus_fine_step_mm
            if step_mm is None
            else step_mm,
            self._objective_autofocus_fine_step_mm,
        )
        if fine_step <= 0:
            raise StageControllerError("Autofocus parameters are invalid.")

        status = self._query_synced_status_for_absolute_motion(axes=("Z",))
        position = self._position_for_configured_mode(status)
        if status is None or position is None or len(position) < 3:
            raise StageControllerError("Unable to read Z position for autofocus.")
        self._require_homed_axes(status, {"Z"}, allow_relative=True)
        start_z = float(position[2])
        z_limits = self._axis_limits_for_configured_mode("Z", status)
        if not z_limits:
            raise StageControllerError("Z axis limits unavailable.")
        min_z, max_z = z_limits
        if start_z < min_z or start_z > max_z:
            raise StageControllerError(
                f"Current Z position {start_z:.3f} is outside limits ({min_z:.3f}, {max_z:.3f})."
            )
        lower_z = max(min_z, start_z - local_range)
        upper_z = min(max_z, start_z + local_range)
        if upper_z <= lower_z:
            raise StageControllerError("Z axis range near current position is empty.")
        return _AutofocusContext(
            objective_name=objective_name,
            start_z=start_z,
            min_z=float(min_z),
            max_z=float(max_z),
            lower_z=float(lower_z),
            upper_z=float(upper_z),
            local_range_mm=float(local_range),
            fine_step_mm=float(fine_step),
        )

    def _run_focus_sweep_locked(
        self,
        lower_z: float,
        upper_z: float,
        *,
        feedrate: float,
    ) -> _FocusSweepResult:
        lower_z = float(lower_z)
        upper_z = float(upper_z)
        if upper_z <= lower_z:
            raise StageControllerError("Autofocus sweep range is empty.")

        status = self._query_current_status_with_required_coordinates(
            axes=("Z",),
        )
        position = self._position_for_configured_mode(status)
        if status is None or position is None or len(position) < 3:
            raise StageControllerError("Unable to read Z position for autofocus.")
        current_z = float(position[2])
        if abs(lower_z - current_z) >= 1e-5:
            self._send_relative_move(
                MoveVector(z=lower_z - current_z),
                allow_relative=True,
                as_jog=True,
            )

        with self._frame_condition:
            start_counter = self._frame_counter
        sweep_started_at = time.monotonic()
        sweep_distance = abs(upper_z - lower_z)
        sweep_ended_at = sweep_started_at + max(
            sweep_distance
            / (max(self.AUTOFOCUS_MIN_SWEEP_FEEDRATE_MM_MIN, float(feedrate)) / 60.0),
            1e-6,
        )
        self._send_relative_move(
            MoveVector(z=upper_z - lower_z),
            allow_relative=True,
            feedrate=feedrate,
            as_jog=True,
        )
        actual_ended_at = time.monotonic()
        samples = self._frame_samples(
            start_counter=start_counter,
            started_at=sweep_started_at,
            ended_at=actual_ended_at,
        )
        if len(samples) < self.AUTOFOCUS_MIN_SWEEP_FRAMES:
            raise StageControllerError(
                "Camera did not provide enough frames during autofocus sweep."
            )

        scored: list[tuple[float, float]] = []
        for timestamp, frame in samples:
            z_value = interpolate_position(
                (lower_z,),
                (upper_z,),
                sweep_started_at,
                sweep_ended_at,
                timestamp,
            )[0]
            scored.append((float(z_value), self._focus_metric(frame)))

        best_index = max(range(len(scored)), key=lambda index: scored[index][1])
        best_z, best_score = scored[best_index]
        fitted_z = self._parabolic_focus_peak(scored, best_index)
        if fitted_z is not None:
            best_z = max(lower_z, min(upper_z, fitted_z))
        edge_margin = max(2, len(scored) // 10)
        edge_peak = best_index < edge_margin or best_index >= len(scored) - edge_margin
        logger.debug(
            "Autofocus sweep lower=%.6f upper=%.6f feedrate=%.6f frames=%d "
            "best_z=%.6f best_score=%.6f edge=%s",
            lower_z,
            upper_z,
            feedrate,
            len(scored),
            best_z,
            best_score,
            edge_peak,
        )
        return _FocusSweepResult(
            best_z=float(best_z),
            best_score=float(best_score),
            sample_count=len(scored),
            edge_peak=edge_peak,
        )

    def _autofocus_sweep_feedrate_mm_min(self, fine_step_mm: float) -> float:
        frame_rate_hz = self._autofocus_frame_rate_hz()
        feedrate = autofocus_sweep_feedrate_mm_min(
            fine_step_mm,
            frame_rate_hz,
            min_feedrate_mm_min=self.AUTOFOCUS_MIN_SWEEP_FEEDRATE_MM_MIN,
            error_factory=StageControllerError,
        )
        logger.debug(
            "Autofocus dynamic sweep feedrate %.6f mm/min from %.3f fps and %.6f mm step",
            feedrate,
            frame_rate_hz,
            fine_step_mm,
        )
        return float(feedrate)

    def _autofocus_frame_rate_hz(self) -> float | None:
        frame_rate = self._frame_rate_from_timestamps(
            self._recent_frame_timestamps(max_age_s=self.AUTOFOCUS_FRAME_RATE_MAX_AGE_S)
        )
        if frame_rate is not None:
            return frame_rate

        timestamps: list[float] = []
        with self._frame_condition:
            frame_counter = self._frame_counter
        for _ in range(max(2, self.AUTOFOCUS_FRAME_RATE_SAMPLE_FRAMES)):
            frame, frame_counter = self._wait_for_new_frame(
                frame_counter,
                timeout=self.AUTOFOCUS_FRAME_RATE_SAMPLE_TIMEOUT_S,
            )
            if frame is None:
                break
            timestamps.append(time.monotonic())
        return self._frame_rate_from_timestamps(timestamps)

    def _recent_frame_timestamps(self, *, max_age_s: float) -> list[float]:
        now = time.monotonic()
        with self._frame_condition:
            timestamps = [
                timestamp
                for _counter, timestamp, _frame in self._frame_history
                if now - timestamp <= max_age_s
            ]
        return timestamps[-max(2, self.AUTOFOCUS_FRAME_RATE_SAMPLE_FRAMES) :]

    @staticmethod
    def _frame_rate_from_timestamps(timestamps: list[float]) -> float | None:
        return frame_rate_from_timestamps(timestamps)

    def _run_static_focus_refinement_locked(
        self,
        center_z: float,
        *,
        min_z: float,
        max_z: float,
        step_mm: float,
    ) -> _FocusSweepResult:
        scored: list[tuple[float, float]] = []
        frame_counter: int | None = None
        center = float(center_z)
        for round_index in range(self.AUTOFOCUS_STATIC_EDGE_REFINEMENT_ROUNDS + 1):
            candidates = self._static_focus_candidates(
                center,
                min_z=min_z,
                max_z=max_z,
                step_mm=step_mm,
                max_points=self.AUTOFOCUS_STATIC_REFINEMENT_POINTS,
            )
            if len(candidates) < 3:
                raise StageControllerError(
                    "Autofocus static verification range is empty."
                )
            round_scored: list[tuple[float, float]] = []
            for target_z in candidates:
                if any(
                    abs(target_z - previous_z) <= abs(step_mm) * 0.1
                    for previous_z, _score in scored
                ):
                    continue
                self._check_cancelled()
                self._approach_z_from_below_locked(
                    target_z,
                    min_z=min_z,
                    fine_step_mm=step_mm,
                )
                with self._frame_condition:
                    frame_counter = self._frame_counter
                frame = None
                for _ in range(max(1, self.AUTOFOCUS_STATIC_SETTLE_FRAMES + 1)):
                    frame, frame_counter = self._wait_for_new_frame(
                        frame_counter,
                        timeout=2.0,
                    )
                    if frame is None:
                        break
                if frame is None:
                    raise StageControllerError(
                        "Camera did not provide frames during autofocus static verification."
                    )
                score = self._focus_metric(frame)
                logger.debug(
                    "Autofocus static candidate round=%d z=%.6f score=%.6f",
                    round_index,
                    target_z,
                    score,
                )
                sample = (float(target_z), float(score))
                scored.append(sample)
                round_scored.append(sample)

            if not round_scored:
                break
            round_best_index = max(
                range(len(round_scored)),
                key=lambda index: round_scored[index][1],
            )
            round_edge_peak = (
                round_best_index == 0
                or round_best_index == len(round_scored) - 1
            )
            if not round_edge_peak:
                break
            if round_index >= self.AUTOFOCUS_STATIC_EDGE_REFINEMENT_ROUNDS:
                break
            direction = -1.0 if round_best_index == 0 else 1.0
            next_center = round_scored[round_best_index][0] + (
                direction * abs(step_mm) * max(1, len(candidates) // 2)
            )
            next_center = max(float(min_z), min(float(max_z), next_center))
            if abs(next_center - center) <= abs(step_mm) * 0.5:
                break
            logger.debug(
                "Autofocus static edge peak; expanding center %.6f -> %.6f",
                center,
                next_center,
            )
            center = next_center

        if len(scored) < 3:
            raise StageControllerError("Autofocus static verification range is empty.")
        scored.sort(key=lambda sample: sample[0])
        best_index = max(range(len(scored)), key=lambda index: scored[index][1])
        best_z, best_score = scored[best_index]
        fitted_z = self._parabolic_focus_peak(scored, best_index)
        if fitted_z is not None:
            best_z = max(scored[0][0], min(scored[-1][0], fitted_z))
        edge_peak = best_index == 0 or best_index == len(scored) - 1
        logger.debug(
            "Autofocus static result samples=%d best_z=%.6f best_score=%.6f edge=%s",
            len(scored),
            best_z,
            best_score,
            edge_peak,
        )
        return _FocusSweepResult(
            best_z=float(best_z),
            best_score=float(best_score),
            sample_count=len(scored),
            edge_peak=edge_peak,
        )

    @staticmethod
    def _static_focus_candidates(
        center_z: float,
        *,
        min_z: float,
        max_z: float,
        step_mm: float,
        max_points: int,
    ) -> list[float]:
        return static_focus_candidates(
            center_z,
            min_z=min_z,
            max_z=max_z,
            step_mm=step_mm,
            max_points=max_points,
        )

    def _approach_z_from_below_locked(
        self,
        target_z: float,
        *,
        min_z: float,
        fine_step_mm: float | None = None,
    ) -> None:
        status = self._query_current_status_with_required_coordinates(axes=("Z",))
        position = self._position_for_configured_mode(status)
        if status is None or position is None or len(position) < 3:
            raise StageControllerError("Unable to read Z position for autofocus.")
        current_z = float(position[2])
        target = float(target_z)
        fine_step = (
            self._objective_autofocus_fine_step_mm
            if fine_step_mm is None
            else float(fine_step_mm)
        )
        backlash = min(
            max(abs(fine_step) * 4.0, 0.002),
            self.AUTOFOCUS_BACKLASH_MM,
        )
        approach_z = max(float(min_z), target - backlash)
        if abs(approach_z - current_z) >= 1e-5:
            self._send_relative_move(
                MoveVector(z=approach_z - current_z),
                allow_relative=True,
                as_jog=True,
            )
            current_z = approach_z
        if abs(target - current_z) >= 1e-5:
            self._send_relative_move(
                MoveVector(z=target - current_z),
                allow_relative=True,
                as_jog=True,
            )

    def _frame_samples(
        self,
        *,
        start_counter: int,
        started_at: float,
        ended_at: float,
    ) -> list[tuple[float, np.ndarray]]:
        with self._frame_condition:
            return [
                (timestamp, frame.copy())
                for counter, timestamp, frame in self._frame_history
                if counter > start_counter
                and started_at <= timestamp <= ended_at
            ]

    @staticmethod
    def _parabolic_focus_peak(
        scored: list[tuple[float, float]],
        best_index: int,
    ) -> float | None:
        return parabolic_focus_peak(scored, best_index)

    def _run_home(self, command: str, axis_key: str) -> None:
        self.movement_started.emit()
        try:
            with self._serial_session():
                self._perform_home_command(command)
            self.movement_finished.emit(True, "Homing complete.")
            self.homing_action_finished.emit(True, "Homing complete.", axis_key)
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
            self.homing_action_finished.emit(False, str(exc), axis_key)
        finally:
            with self._task_lock:
                self._active_thread = None

    def _run_startup_sync(self, auto_home_a: bool) -> None:
        success = False
        try:
            serial_connection = self._require_open_serial()

            self.status_message.emit("Loading controller startup state...")
            with self._serial_session_lock:
                axis_feedrates = dict(self._axis_max_feedrates)
                if axis_feedrates:
                    logger.info(
                        "Using cached controller axis max feedrates for unchanged controller session."
                    )
                else:
                    try:
                        axis_feedrates = self._query_axis_max_feedrates_locked(
                            serial_connection
                        )
                    except StageControllerError as exc:
                        axis_feedrates = {}
                        logger.warning(
                            "Unable to read axis max feedrates from controller: %s",
                            exc,
                        )
                if axis_feedrates:
                    self.apply_axis_max_feedrates(axis_feedrates)
                    self.axis_max_feedrates_changed.emit(dict(axis_feedrates))
                self._ensure_axis_limits(
                    serial_connection, required_axes=self.CONTROLLER_LIMIT_AXES
                )
                self._refresh_coordinate_system_state(
                    serial_connection, apply_preference=True
                )
                status = self._query_status(serial_connection)
            if status is None:
                raise StageControllerError("Unable to read startup controller status.")

            self.status_message.emit(
                "Axis limits loaded from controller. B uses app soft limit ±45 deg."
            )

            self._emit_coordinate_system_status(status)
            effective_homed = status.homed_axes
            if effective_homed is None and self._homed_axes:
                effective_homed = set(self._homed_axes)
            if effective_homed:
                ordered = ", ".join(sorted(effective_homed))
                self.status_message.emit(f"Homed axes: {ordered}.")
            else:
                self.status_message.emit("Controller did not report any homed axes.")

            self._controller_state_stale = False
            self._refresh_axis_a_ready_from_state()

            if auto_home_a and (effective_homed is None or "A" not in effective_homed):
                self.movement_started.emit()
                self.homing_action_started.emit("A")
                try:
                    self.status_message.emit("A axis not homed. Homing needles on startup.")
                    with self._serial_session():
                        self._perform_home_command("$HA")
                    self.movement_finished.emit(True, "Startup A homing complete.")
                    self.homing_action_finished.emit(
                        True, "Startup A homing complete.", "A"
                    )
                except StageControllerError as exc:
                    self.movement_finished.emit(False, str(exc))
                    self.homing_action_finished.emit(False, str(exc), "A")
                    raise
            with self._serial_session_lock:
                self._ensure_controller_session_marker()
            if self._last_stage_position is not None:
                self.stage_position_changed.emit(tuple(self._last_stage_position))
            success = True
        except StageControllerError as exc:
            self.status_message.emit(str(exc))
        finally:
            with self._task_lock:
                if success:
                    self._controller_reboot_recovery_pending = False
                    self._controller_reboot_ready_notified = False
                self._active_thread = None

    def _ensure_calibration(
        self,
        target_pixels: np.ndarray | None = None,
    ) -> tuple[bool, int | None]:
        if self._pixels_to_mm is not None:
            if not self._objective_calibration_verified.get(
                self._active_objective_name,
                False,
            ):
                return self._verify_active_objective_calibration(
                    target_pixels=target_pixels,
                )
            return (False, None)
        self.status_message.emit(
            f"Starting {self._active_objective_name} click calibration sequence..."
        )
        before_frame, _ = self._get_frame_snapshot(timeout=3.0)
        if before_frame is None:
            raise StageControllerError("Camera frames are unavailable for calibration.")

        start_status = self._query_current_status_with_required_coordinates(
            axes=("X", "Y"),
        )
        origin = self._position_for_configured_mode(start_status)
        if start_status is None or origin is None:
            raise StageControllerError("Unable to read position for calibration.")
        self._require_homed_axes(start_status, {"X", "Y"})

        observations: list[tuple[np.ndarray, np.ndarray]] = []
        try:
            observations.extend(
                self._calibrate_axis_series(
                    before_frame, origin, axis="X"
                )
            )
            latest_frame, _ = self._get_frame_snapshot(timeout=2.0)
            reference_for_y = latest_frame if latest_frame is not None else before_frame
            observations.extend(
                self._calibrate_axis_series(
                    reference_for_y, origin, axis="Y"
                )
            )
            calibration_matrix = self._calibration_matrix_from_observations(observations)
            if not np.isfinite(calibration_matrix).all():
                raise StageControllerError("Calibration produced invalid values.")
            determinant = float(np.linalg.det(calibration_matrix))
            if abs(determinant) < 1e-9:
                raise StageControllerError("Calibration matrix is singular.")
            self._pixels_to_mm = np.linalg.inv(calibration_matrix)
            mm_per_pixel_x, mm_per_pixel_y = self._calibration_magnitudes()
            self.calibration_changed.emit(mm_per_pixel_x, mm_per_pixel_y)
            self.objective_calibration_updated.emit(
                self._active_objective_name,
                self._pixels_to_mm.tolist(),
            )
            self._objective_matrices[self._active_objective_name] = self._pixels_to_mm
            self._objective_calibration_verified[self._active_objective_name] = True
            target_handled, before_counter = self._move_from_calibration_to_target(
                origin,
                target_pixels,
            )
        except Exception:
            self._return_to_origin(origin)
            raise

        self.status_message.emit(
            f"{self._active_objective_name} calibration updated: dX {mm_per_pixel_x:.6f} mm/px, dY {mm_per_pixel_y:.6f} mm/px"
        )
        return (target_handled, before_counter)

    def _verify_active_objective_calibration(
        self,
        target_pixels: np.ndarray | None = None,
    ) -> tuple[bool, int | None]:
        if self._pixels_to_mm is None:
            return (False, None)
        before_frame, frame_counter = self._get_frame_snapshot(timeout=3.0)
        if before_frame is None:
            raise StageControllerError("Camera frames are unavailable for calibration check.")
        status = self._query_current_status_with_required_coordinates(
            axes=("X", "Y"),
        )
        origin = self._position_for_configured_mode(status)
        if status is None or origin is None:
            raise StageControllerError("Unable to read position for calibration check.")
        self._require_homed_axes(status, {"X", "Y"})
        step_mm = self._calibration_verify_step_mm()
        self.status_message.emit(
            f"Checking {self._active_objective_name} click calibration..."
        )
        try:
            self._send_relative_move(MoveVector(x=step_mm))
            after_frame, _frame_counter = self._wait_for_new_frame(
                frame_counter,
                timeout=2.0,
            )
            if after_frame is None:
                raise StageControllerError(
                    "Camera did not update during calibration check."
                )
            measured_pixels = np.asarray(
                self._estimate_shift(before_frame, after_frame),
                dtype=float,
            )
            if np.linalg.norm(measured_pixels) < 1e-6:
                raise StageControllerError("Calibration check saw no image motion.")
            expected_mm = np.asarray([step_mm, 0.0], dtype=float)
            active_error = float(
                np.linalg.norm(self._pixels_to_mm @ measured_pixels - expected_mm)
            )
            tolerance = max(
                self.CALIBRATION_VERIFY_MIN_ERROR_MM,
                abs(step_mm) * self.CALIBRATION_VERIFY_ERROR_RATIO,
            )
            if active_error <= tolerance:
                self._objective_calibration_verified[self._active_objective_name] = True
                target_handled, before_counter = self._move_from_calibration_to_target(
                    origin,
                    target_pixels,
                )
                self.status_message.emit(
                    f"{self._active_objective_name} click calibration verified."
                )
                return (target_handled, before_counter)

            suggestion = self._best_objective_for_measurement(
                measured_pixels,
                expected_mm,
                tolerance=tolerance,
            )
            if suggestion and suggestion != self._active_objective_name:
                message = (
                    f"Selected objective appears to be {suggestion}, not "
                    f"{self._active_objective_name}. Objective switched; click again."
                )
                self.objective_mismatch_detected.emit(suggestion, message)
                raise StageControllerError(message)
            raise StageControllerError(
                "Click calibration does not match the selected objective. "
                "Select the correct objective in the GUI or recalibrate this objective."
            )
        except Exception:
            self._return_to_origin(origin)
            raise

    def _best_objective_for_measurement(
        self,
        measured_pixels: np.ndarray,
        expected_mm: np.ndarray,
        *,
        tolerance: float,
    ) -> str | None:
        best_name: str | None = None
        best_error = math.inf
        for name, matrix in self._objective_matrices.items():
            try:
                error = float(np.linalg.norm(matrix @ measured_pixels - expected_mm))
            except (TypeError, ValueError):
                continue
            if error < best_error:
                best_error = error
                best_name = name
        if best_name is None or best_error > tolerance:
            return None
        return best_name

    def _calibrate_axis_series(
        self,
        reference_frame: np.ndarray,
        _origin: tuple[float, float, float],
        axis: str,
    ) -> list[tuple[np.ndarray, np.ndarray]]:
        if reference_frame is None:
            raise StageControllerError("Reference frame unavailable for calibration.")
        index = 0 if axis == "X" else 1
        observations: list[tuple[np.ndarray, np.ndarray]] = []
        step_mm = float(self.CALIBRATION_PROBE_STEP_MM)
        target_pixels = float(self._objective_calibration_target_pixels)
        reference_status = self._query_current_status_with_required_coordinates(
            axes=("X", "Y"),
        )
        reference_position = self._position_for_configured_mode(reference_status)
        if reference_status is None or reference_position is None:
            raise StageControllerError("Unable to read reference position for calibration.")
        self._require_homed_axes(reference_status, {axis})
        with self._frame_condition:
            frame_counter = self._frame_counter
        for _ in range(self.CALIBRATION_MAX_OBSERVATIONS_PER_AXIS):
            if axis == "X":
                move = MoveVector(x=step_mm)
            else:
                move = MoveVector(y=step_mm)
            self._send_relative_move(move)
            new_frame, frame_counter = self._wait_for_new_frame(frame_counter, timeout=2.0)
            if new_frame is None:
                raise StageControllerError("Camera did not update during calibration.")
            status = self._query_current_status_with_required_coordinates(
                axes=("X", "Y"),
            )
            current = self._position_for_configured_mode(status)
            if status is None or current is None:
                raise StageControllerError("Unable to query position during calibration.")
            self._require_homed_axes(status, {axis})
            mm_vector = np.array(
                [
                    float(current[0] - reference_position[0]),
                    float(current[1] - reference_position[1]),
                ],
                dtype=float,
            )
            shift_x, shift_y, response = self._estimate_shift_with_response(
                reference_frame,
                new_frame,
            )
            shift_pixels = math.hypot(float(shift_x), float(shift_y))
            reliable_shift = (
                response >= self.CALIBRATION_MIN_RESPONSE
                and shift_pixels >= self.CALIBRATION_MIN_OBSERVATION_PIXELS
            )
            if reliable_shift:
                pixel_vector = np.array([shift_x, shift_y], dtype=float)
                observations.append((mm_vector, pixel_vector))
            if abs(current[index] - reference_position[index]) < 1e-6:
                continue
            if (
                len(observations) >= self.CALIBRATION_MIN_OBSERVATIONS
                and shift_pixels >= target_pixels
            ):
                break
            if not reliable_shift:
                if step_mm >= self.CALIBRATION_MAX_UNVERIFIED_STEP_MM - 1e-12:
                    raise StageControllerError(
                        f"Unable to measure reliable image motion for {axis} calibration."
                    )
                step_mm = min(
                    step_mm * 2.0,
                    self.CALIBRATION_MAX_UNVERIFIED_STEP_MM,
                )
                continue
            axis_delta = abs(float(current[index] - reference_position[index]))
            step_mm = self._next_calibration_probe_step_mm(
                axis_delta_mm=axis_delta,
                shift_pixels=shift_pixels,
                observations_count=len(observations),
                target_pixels=target_pixels,
            )

        if not observations:
            raise StageControllerError(
                f"Pixel shift too small to compute {axis} calibration."
            )
        return observations

    def _next_calibration_probe_step_mm(
        self,
        *,
        axis_delta_mm: float,
        shift_pixels: float,
        observations_count: int,
        target_pixels: float,
    ) -> float:
        min_step = float(self.CALIBRATION_PROBE_STEP_MM)
        max_step = float(self.CALIBRATION_MAX_ADAPTIVE_STEP_MM)
        if axis_delta_mm <= 1e-9 or shift_pixels <= 1e-9:
            return min_step
        px_per_mm = shift_pixels / axis_delta_mm
        if px_per_mm <= 1e-9 or not math.isfinite(px_per_mm):
            return min_step
        target_distance_mm = max(axis_delta_mm, float(target_pixels) / px_per_mm)
        remaining_distance = max(0.0, target_distance_mm - axis_delta_mm)
        remaining_observations = max(
            1,
            self.CALIBRATION_MIN_OBSERVATIONS - int(observations_count),
        )
        step = remaining_distance / remaining_observations
        if step <= 0.0:
            step = min_step
        return min(max(step, min_step), max_step)

    def _calibration_verify_step_mm(self) -> float:
        if self._pixels_to_mm is None:
            return self.CALIBRATION_VERIFY_STEP_MM
        try:
            stage_to_pixels = np.linalg.inv(self._pixels_to_mm)
            pixels_for_x_mm = stage_to_pixels @ np.array([1.0, 0.0], dtype=float)
            px_per_mm = float(np.linalg.norm(pixels_for_x_mm))
        except (TypeError, ValueError, np.linalg.LinAlgError):
            px_per_mm = 0.0
        if not math.isfinite(px_per_mm) or px_per_mm <= 1e-9:
            return self.CALIBRATION_VERIFY_STEP_MM
        step = float(self.CALIBRATION_VERIFY_TARGET_PIXELS) / px_per_mm
        return min(
            max(step, float(self.CALIBRATION_PROBE_STEP_MM)),
            float(self.CALIBRATION_VERIFY_STEP_MM),
        )

    def _calibration_matrix_from_observations(
        self,
        observations: list[tuple[np.ndarray, np.ndarray]],
    ) -> np.ndarray:
        if len(observations) < self.CALIBRATION_MIN_OBSERVATIONS:
            raise StageControllerError("Not enough calibration observations.")
        stage_vectors = np.vstack([item[0] for item in observations])
        pixel_vectors = np.vstack([item[1] for item in observations])
        if np.linalg.matrix_rank(stage_vectors) < 2:
            raise StageControllerError("Calibration observations are degenerate.")
        coefficients, _residuals, _rank, _singular = np.linalg.lstsq(
            stage_vectors,
            pixel_vectors,
            rcond=None,
        )
        calibration_matrix = coefficients.T
        if not np.isfinite(calibration_matrix).all():
            raise StageControllerError("Calibration produced invalid values.")
        predicted = stage_vectors @ coefficients
        errors = np.linalg.norm(predicted - pixel_vectors, axis=1)
        if len(errors) >= self.CALIBRATION_MIN_OBSERVATIONS + 2:
            median_error = float(np.median(errors))
            keep = errors <= max(2.0, median_error * 3.0)
            if int(np.count_nonzero(keep)) >= self.CALIBRATION_MIN_OBSERVATIONS:
                coefficients, _residuals, _rank, _singular = np.linalg.lstsq(
                    stage_vectors[keep],
                    pixel_vectors[keep],
                    rcond=None,
                )
                calibration_matrix = coefficients.T
        return calibration_matrix

    def _move_from_calibration_to_target(
        self,
        origin: tuple[float, float, float],
        target_pixels: np.ndarray | None,
    ) -> tuple[bool, int | None]:
        if target_pixels is None:
            self._return_to_origin(origin)
            return (False, None)
        if self._pixels_to_mm is None:
            raise StageControllerError("Calibration failed. Cannot move stage.")
        pixel_vector = np.asarray(target_pixels, dtype=float)
        if pixel_vector.shape != (2,):
            raise StageControllerError("Invalid click target for calibration move.")
        if float(np.linalg.norm(pixel_vector)) < 1e-3:
            self._return_to_origin(origin)
            return (False, None)

        click_delta_mm = -(self._pixels_to_mm @ pixel_vector)
        move_magnitude = float(np.linalg.norm(click_delta_mm))
        if move_magnitude > self.MAX_CLICK_MOVE_MM:
            self._pixels_to_mm = None
            raise StageControllerError(
                "Predicted click move is too large; calibration was reset. Recalibrate and try again."
            )

        status = self._query_current_status_with_required_coordinates(
            axes=("X", "Y"),
        )
        current = self._position_for_configured_mode(status)
        if status is None or current is None:
            raise StageControllerError("Unable to read position after calibration.")
        self._require_homed_axes(status, {"X", "Y"})
        target_x = float(origin[0]) + float(click_delta_mm[0])
        target_y = float(origin[1]) + float(click_delta_mm[1])
        move = MoveVector(
            x=target_x - float(current[0]),
            y=target_y - float(current[1]),
        )
        if move.is_zero(tol=1e-5):
            return (True, None)
        with self._frame_condition:
            before_counter = self._frame_counter
        self.status_message.emit(
            f"Jogging stage dX={move.x:.3f} mm dY={move.y:.3f} mm"
        )
        self._send_relative_move(
            move,
            as_jog=True,
            motion_started_callback=self._emit_click_move_started,
        )
        return (True, before_counter)

    def _emit_click_move_started(
        self,
        move: MoveVector,
        feedrate: Optional[float] = None,
    ) -> None:
        effective_feedrate = (
            self.DEFAULT_FEEDRATE if feedrate is None else max(self.MIN_FEEDRATE, float(feedrate))
        )
        self.click_move_started.emit(float(move.x), float(move.y), effective_feedrate)

    def _return_to_origin(
        self, origin: tuple[float, float, float]
    ) -> None:
        status = self._query_current_status_with_required_coordinates(
            axes=("X", "Y"),
        )
        current = self._position_for_configured_mode(status)
        if status is None or current is None:
            return
        self._require_homed_axes(status, {"X", "Y"})
        delta_x = origin[0] - current[0]
        delta_y = origin[1] - current[1]
        move = MoveVector(x=delta_x, y=delta_y)
        if move.is_zero(tol=1e-5):
            return
        self.status_message.emit("Returning stage to calibration origin…")
        self._send_relative_move(move)

    def _run_manual_axis_move(
        self,
        axis: str,
        distance_mm: float,
        mode: str,
        feedrate: float | None,
        allow_unhomed: bool = False,
    ) -> None:
        self.movement_started.emit()
        try:
            self._check_cancelled()
            with self._serial_session() as serial_connection:
                if mode == "G91" and abs(distance_mm) < 1e-6:
                    self.movement_finished.emit(True, "Manual axis move skipped.")
                    return
                feedrate_text = (
                    self.DEFAULT_FEEDRATE if feedrate is None else max(self.MIN_FEEDRATE, float(feedrate))
                )
                target_value = self._manual_axis_absolute_target(
                    axis,
                    distance_mm,
                    mode,
                )
                mode_label = "relative" if mode == "G91" else "absolute"
                self.status_message.emit(
                    f"Manual axis move ({mode_label}->G90): "
                    f"{axis}{target_value:+.3f} "
                    f"F{self._format_gcode_value(feedrate_text)}."
                )
                self._send_absolute_axis_move(
                    axis,
                    target_value,
                    ignore_needle_safety=self._motion_safety_disabled,
                    feedrate=feedrate,
                    wait_for_completion=False,
                    allow_unhomed=allow_unhomed or mode == "G91",
                    as_jog=True,
                )
            self.movement_finished.emit(
                True,
                "Manual axis move accepted "
                f"({mode_label}->G90 {axis}{target_value:+.3f}).",
            )
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                self._active_thread = None

    def _run_absolute_axis_targets_move(
        self,
        targets: dict[str, float],
        feedrate: float | None,
        allow_unhomed: bool = True,
    ) -> None:
        self.movement_started.emit()
        try:
            self._check_cancelled()
            with self._serial_session() as serial_connection:
                feedrate_text = (
                    self.DEFAULT_FEEDRATE if feedrate is None else max(self.MIN_FEEDRATE, float(feedrate))
                )
                ordered_targets = {
                    axis: float(targets[axis])
                    for axis in self.AXIS_INDEX
                    if axis in targets
                }
                target_text = " ".join(
                    f"{axis}{value:+.3f}" for axis, value in ordered_targets.items()
                )
                self.status_message.emit(
                    "Coordinate move (G90): "
                    f"{target_text} F{self._format_gcode_value(feedrate_text)}."
                )
                self._send_absolute_axis_targets_move(
                    ordered_targets,
                    ignore_needle_safety=self._motion_safety_disabled,
                    feedrate=feedrate,
                    wait_for_completion=True,
                    allow_unhomed=allow_unhomed,
                    as_jog=True,
                )
            self.movement_finished.emit(
                True,
                f"Coordinate move complete (G90 {target_text}).",
            )
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                self._active_thread = None

    def _manual_axis_absolute_target(
        self,
        axis: str,
        value_mm: float,
        mode: str,
    ) -> float:
        axis = axis.upper().strip()
        mode = mode.upper().strip()
        if mode == "G90":
            return float(value_mm)
        if mode != "G91":
            raise StageControllerError(f"Unsupported manual move mode: {mode}")

        current_value = self._current_axis_value_for_manual_move(
            axis,
        )
        if current_value is None:
            raise StageControllerError(
                f"Unable to read {axis} position for relative manual move."
            )
        return float(current_value) + float(value_mm)

    def _current_axis_value_for_manual_move(
        self,
        axis: str,
    ) -> float | None:
        status = self._query_current_status_with_required_coordinates(axes=(axis,))
        current_value = self._axis_value_for_configured_mode(status, axis)
        if current_value is not None:
            return current_value
        index = self.AXIS_INDEX.get(axis.upper().strip())
        if (
            index is not None
            and self._last_stage_position is not None
            and index < len(self._last_stage_position)
        ):
            return float(self._last_stage_position[index])
        return None

    def _run_oscillation(
        self, mode: str, amplitude_mm: float, feedrate: float, turns_per_sweep: float
    ) -> None:
        serial_connection: serial.Serial | None = None
        try:
            serial_connection = self._require_open_serial()
            if mode not in {"X", "Y", "SPIRAL"}:
                raise StageControllerError(
                    f"Oscillation mode {mode} is not supported."
                )
            if not (
                self.OSCILLATION_MIN_AMPLITUDE_MM
                <= amplitude_mm
                <= self.OSCILLATION_MAX_AMPLITUDE_MM
            ):
                raise StageControllerError(
                    f"Oscillation amplitude must be between "
                    f"{self.OSCILLATION_MIN_AMPLITUDE_MM:.3f} and "
                    f"{self.OSCILLATION_MAX_AMPLITUDE_MM:.3f} mm."
                )
            if not (self.OSCILLATION_MIN_FEEDRATE <= feedrate <= self.OSCILLATION_MAX_FEEDRATE):
                raise StageControllerError(
                    f"Oscillation feedrate must be between "
                    f"{self.OSCILLATION_MIN_FEEDRATE:.1f} and "
                    f"{self.OSCILLATION_MAX_FEEDRATE:.1f} mm/min."
                )
            if mode == "SPIRAL" and not (
                self.SPIRAL_MIN_TURNS_PER_SWEEP
                <= turns_per_sweep
                <= self.SPIRAL_MAX_TURNS_PER_SWEEP
            ):
                raise StageControllerError(
                    f"Spiral turns per sweep must be between "
                    f"{self.SPIRAL_MIN_TURNS_PER_SWEEP:.2f} and "
                    f"{self.SPIRAL_MAX_TURNS_PER_SWEEP:.2f}."
                )
            with self._serial_session() as serial_connection:
                self._oscillation_active = True
                self.oscillation_state_changed.emit(True, mode)
                self.status_message.emit(
                    f"Oscillation started in {mode}: amplitude={amplitude_mm:.3f} mm, "
                    f"feedrate={feedrate:.1f} mm/min."
                )
                self._write_current_command_and_wait("G21")
                self._write_current_command_and_wait("G91")
                if mode == "SPIRAL":
                    self._run_spiral_pattern(
                        amplitude_mm=amplitude_mm,
                        feedrate=feedrate,
                        turns_per_sweep=turns_per_sweep,
                    )
                else:
                    self._run_linear_pattern(
                        axis=mode,
                        amplitude_mm=amplitude_mm,
                        feedrate=feedrate,
                    )
                self._write_current_command_and_wait("G90")
                self.status_message.emit("Oscillation stopped.")
        except StageControllerError as exc:
            try:
                if serial_connection is not None and serial_connection.is_open:
                    with self._serial_session_lock:
                        self._cancel_event.clear()
                        self._write_command(serial_connection, "G90")
                        self._wait_for_ok(serial_connection)
            except StageControllerError:
                pass
            with self._task_lock:
                has_queued_needles_action = bool(self._queued_needles_actions)
            if not (
                str(exc) == "Operation cancelled." and has_queued_needles_action
            ):
                self.status_message.emit(str(exc))
        finally:
            self._oscillation_active = False
            self.oscillation_state_changed.emit(False, mode)
            with self._task_lock:
                self._active_thread = None
            self._start_next_queued_needles_action()

    def _calibration_magnitudes(self) -> tuple[float, float]:
        if self._pixels_to_mm is None:
            return (0.0, 0.0)
        column_x = self._pixels_to_mm[:, 0]
        column_y = self._pixels_to_mm[:, 1]
        return (float(np.linalg.norm(column_x)), float(np.linalg.norm(column_y)))

    def _send_relative_move(
        self,
        move: MoveVector,
        *,
        allow_relative: bool = False,
        ignore_needle_safety: bool = False,
        feedrate: Optional[float] = None,
        wait_for_completion: bool = True,
        motion_started_callback: Optional[Callable[[MoveVector, float], None]] = None,
        as_jog: bool = False,
    ) -> None:
        if move.is_zero():
            return
        serial_connection = self._current_serial()
        if not ignore_needle_safety:
            self._move_safety_check()
        if not self._motion_safety_disabled:
            self._ensure_axis_limits(
                serial_connection,
                required_axes=tuple(
                    axis for axis, delta in move.items() if abs(delta) >= 1e-6
                ),
            )
            self._check_relative_move_limits(move, allow_relative=allow_relative)
        move_parts: list[str] = [
            f"{axis}{value:.4f}"
            for axis, value in move.items()
            if abs(value) >= 1e-6
        ]
        if not move_parts:
            return
        effective_feedrate = (
            self.DEFAULT_FEEDRATE if feedrate is None else max(self.MIN_FEEDRATE, float(feedrate))
        )
        move_distance = self._move_distance_for_timeout(move)
        command = (
            "G1 "
            + " ".join(move_parts)
            + f" F{self._format_gcode_value(effective_feedrate)}"
        )
        self._reset_feed_override()
        if as_jog:
            command = (
                "$J=G91 G21 "
                + " ".join(move_parts)
                + f" F{self._format_gcode_value(effective_feedrate)}"
            )
            self._write_current_command_and_wait(command)
            if motion_started_callback is not None:
                motion_started_callback(move, effective_feedrate)
            if wait_for_completion:
                self._wait_for_idle(
                    timeout=self._idle_timeout_for_distance(
                        move_distance, effective_feedrate
                    ),
                )
            return
        self._write_current_command_and_wait("G21")
        self._write_current_command_and_wait("G91")
        self._write_current_command_and_wait(command)
        if motion_started_callback is not None:
            motion_started_callback(move, effective_feedrate)
        self._write_current_command_and_wait("G90")
        if wait_for_completion:
            self._wait_for_idle(
                timeout=self._idle_timeout_for_distance(
                    move_distance, effective_feedrate
                ),
            )

    def _send_absolute_axis_targets_move(
        self,
        targets: dict[str, float],
        *,
        ignore_needle_safety: bool = False,
        feedrate: Optional[float] = None,
        wait_for_completion: bool = True,
        allow_unhomed: bool = False,
        as_jog: bool = False,
    ) -> None:
        ordered_targets: dict[str, float] = {}
        for axis in self.AXIS_INDEX:
            if axis not in targets:
                continue
            value = float(targets[axis])
            if not math.isfinite(value):
                raise StageControllerError(f"Unsupported target for {axis}: {value}")
            ordered_targets[axis] = value
        unsupported = [
            str(axis)
            for axis in targets
            if str(axis).upper().strip() not in self.AXIS_INDEX
        ]
        if unsupported:
            raise StageControllerError(f"Unsupported axis: {', '.join(unsupported)}")
        if not ordered_targets:
            return
        serial_connection = self._current_serial()
        if not ignore_needle_safety:
            self._move_safety_check()
        current_values: dict[str, float] = {}
        if not self._motion_safety_disabled:
            self._ensure_axis_limits(
                serial_connection, required_axes=tuple(ordered_targets)
            )
            status = self._query_status_with_required_coordinates(
                serial_connection,
                axes=tuple(ordered_targets),
            )
            if status is None:
                raise StageControllerError("Unable to read position for absolute move.")
            self._require_homed_axes(
                status,
                set(ordered_targets),
                allow_relative=allow_unhomed,
            )
            for axis, value in ordered_targets.items():
                current_value = self._axis_value_for_configured_mode(status, axis)
                if current_value is not None:
                    current_values[axis] = float(current_value)
                limits = self._axis_limits_for_configured_mode(axis, status)
                if limits and self._axis_software_limit_ready(status, axis):
                    min_value, max_value = limits
                    if value < min_value or value > max_value:
                        raise StageControllerError(
                            f"{axis} target {value:+.3f} exceeds limits ({min_value:.3f}, {max_value:.3f})."
                        )
        effective_feedrate = (
            self.DEFAULT_FEEDRATE if feedrate is None else max(self.MIN_FEEDRATE, float(feedrate))
        )
        move_parts = [
            f"{axis}{value:.4f}"
            for axis, value in ordered_targets.items()
        ]
        if as_jog:
            self._write_current_command_and_wait(
                self._absolute_axis_targets_jog_command(
                    ordered_targets,
                    effective_feedrate,
                ),
            )
            if wait_for_completion:
                move_distance = self._absolute_move_distance_for_timeout(
                    ordered_targets,
                    current_values,
                )
                self._wait_for_idle_at_targets(
                    ordered_targets,
                    timeout=self._idle_timeout_for_distance(
                        move_distance, effective_feedrate
                    ),
                )
            return
        self._write_current_command_and_wait("G21")
        self._write_current_command_and_wait("G90")
        self._reset_feed_override()
        self._write_current_command_and_wait(
            "G1 "
            + " ".join(move_parts)
            + f" F{self._format_gcode_value(effective_feedrate)}",
        )
        if wait_for_completion:
            move_distance = self._absolute_move_distance_for_timeout(
                ordered_targets,
                current_values,
            )
            self._wait_for_idle(
                timeout=self._idle_timeout_for_distance(
                    move_distance, effective_feedrate
                ),
            )

    def _send_absolute_axis_move(
        self,
        axis: str,
        value: float,
        *,
        ignore_needle_safety: bool = False,
        feedrate: Optional[float] = None,
        wait_for_completion: bool = True,
        allow_unhomed: bool = False,
        as_jog: bool = False,
    ) -> None:
        axis = axis.upper().strip()
        self._send_absolute_axis_targets_move(
            {axis: float(value)},
            ignore_needle_safety=ignore_needle_safety,
            feedrate=feedrate,
            wait_for_completion=wait_for_completion,
            allow_unhomed=allow_unhomed,
            as_jog=as_jog,
        )

    def _absolute_axis_targets_jog_command(
        self,
        targets: dict[str, float],
        feedrate: float,
    ) -> str:
        return absolute_axis_targets_jog_command(
            targets,
            feedrate,
            axis_order=self.AXIS_INDEX,
            machine_position_mode=self._position_reporting_mode == "machine",
        )

    @staticmethod
    def _absolute_move_distance_for_timeout(
        targets: dict[str, float],
        current_values: dict[str, float],
    ) -> float:
        return absolute_move_distance_for_timeout(targets, current_values)

    @staticmethod
    def _move_distance_for_timeout(move: MoveVector) -> float:
        return move_distance_for_timeout(move)

    @staticmethod
    def _format_gcode_value(value: float, decimals: int = 3) -> str:
        return format_gcode_value(value, decimals)

    def axis_a_gcode_coordinate_for_lowering(self, lowering_mm: float) -> float:
        """Map a physical A-axis lowering in millimeters to an absolute G-code A coordinate."""

        return self._axis_a_gcode_coordinate_for_lowering(lowering_mm)

    def axis_a_lowering_for_gcode_coordinate(self, a_coordinate_mm: float) -> float:
        """Map an absolute G-code A coordinate to physical calibrated lowering."""

        return self._axis_a_lowering_for_gcode_coordinate(a_coordinate_mm)

    def axis_a_configured_coordinate_for_lowering(
        self,
        lowering_mm: float,
    ) -> float:
        """Map physical A lowering to the current GUI/controller coordinate basis."""

        return self._axis_a_configured_coordinate_for_lowering(lowering_mm)

    def axis_a_lowering_for_configured_coordinate(
        self,
        a_coordinate_mm: float,
    ) -> float:
        """Map the current GUI/controller A coordinate to physical calibrated lowering."""

        return self._axis_a_lowering_for_configured_coordinate(a_coordinate_mm)

    def _normalise_needle_lowering_target(
        self,
        position_mm: Optional[float],
    ) -> Optional[float]:
        return normalise_needle_lowering_target(
            position_mm,
            lowering_for_gcode_coordinate=self._axis_a_lowering_for_gcode_coordinate,
        )

    def calibrated_axis_display_value(
        self,
        axis: str,
        raw_value: float,
    ) -> float:
        """Map a controller coordinate to the calibrated user-facing coordinate."""

        axis = axis.upper().strip()
        if axis == "A":
            return self._axis_a_calibrated_coordinate_for_gcode_coordinate(raw_value)
        if axis == "Z":
            return self._axis_z_display_for_gcode_coordinate(raw_value)
        return float(raw_value)

    def calibrated_axis_raw_value(
        self,
        axis: str,
        display_value: float,
    ) -> float:
        """Map a calibrated user-facing coordinate to a controller coordinate."""

        axis = axis.upper().strip()
        if axis == "A":
            return self._axis_a_gcode_coordinate_for_calibrated_coordinate(display_value)
        if axis == "Z":
            return self._axis_z_gcode_coordinate_for_display(display_value)
        return float(display_value)

    def _axis_calibration_mapper(self) -> StageAxisCalibrationMapper:
        return StageAxisCalibrationMapper(
            axis_a_calibration=self._axis_a_calibration,
            axis_z_calibration=self._axis_z_calibration,
            position_reporting_mode=self._position_reporting_mode,
            active_work_coordinate_system=self._active_work_coordinate_system,
            controller_coordinate_offsets=self._controller_coordinate_offsets,
            axis_index=self.AXIS_INDEX,
        )

    def _axis_a_model_parameters(self) -> tuple[float, float, float, float] | None:
        return self._axis_calibration_mapper().axis_a_model_parameters()

    def _axis_a_model_calibrated_coordinate_for_commanded(
        self,
        commanded_lowering_mm: float,
    ) -> float:
        mapper = self._axis_calibration_mapper()
        return mapper.axis_a_model_calibrated_coordinate_for_commanded(
            commanded_lowering_mm,
        )

    def _axis_a_model_lowering_for_commanded(
        self,
        commanded_lowering_mm: float,
    ) -> float:
        return self._axis_calibration_mapper().axis_a_model_lowering_for_commanded(
            commanded_lowering_mm,
        )

    def _axis_a_calibrated_coordinate_for_gcode_coordinate(
        self, a_coordinate_mm: float
    ) -> float:
        mapper = self._axis_calibration_mapper()
        return mapper.axis_a_calibrated_coordinate_for_gcode_coordinate(
            a_coordinate_mm,
        )

    def _axis_a_lowering_for_gcode_coordinate(self, a_coordinate_mm: float) -> float:
        return self._axis_calibration_mapper().axis_a_lowering_for_gcode_coordinate(
            a_coordinate_mm,
        )

    def _axis_work_offset_for_configured_mode(
        self,
        axis: str,
        status: _Status | None = None,
    ) -> float:
        return self._axis_calibration_mapper().axis_work_offset_for_configured_mode(
            axis,
            status,
        )

    def _axis_a_lowering_for_configured_coordinate(
        self,
        a_coordinate_mm: float,
        status: _Status | None = None,
    ) -> float:
        mapper = self._axis_calibration_mapper()
        return mapper.axis_a_lowering_for_configured_coordinate(
            a_coordinate_mm,
            status,
        )

    def _axis_a_configured_coordinate_for_lowering(
        self,
        lowering_mm: float,
        status: _Status | None = None,
    ) -> float:
        return self._axis_calibration_mapper().axis_a_configured_coordinate_for_lowering(
            lowering_mm,
            status,
        )

    def _axis_a_gcode_coordinate_for_calibrated_coordinate(
        self,
        calibrated_coordinate_mm: float,
    ) -> float:
        mapper = self._axis_calibration_mapper()
        return mapper.axis_a_gcode_coordinate_for_calibrated_coordinate(
            calibrated_coordinate_mm,
        )

    def _axis_a_gcode_coordinate_for_lowering(self, lowering_mm: float) -> float:
        return self._axis_calibration_mapper().axis_a_gcode_coordinate_for_lowering(
            lowering_mm,
        )

    def _axis_a_commanded_lowering_for_calibrated_coordinate(
        self,
        calibrated_coordinate_mm: float,
    ) -> float:
        mapper = self._axis_calibration_mapper()
        return mapper.axis_a_commanded_lowering_for_calibrated_coordinate(
            calibrated_coordinate_mm,
        )

    def _axis_a_gcode_coordinate_for_lowering_step(
        self,
        current_a: float,
        requested_step_mm: float,
    ) -> float:
        return self._axis_calibration_mapper().axis_a_gcode_coordinate_for_lowering_step(
            current_a,
            requested_step_mm,
        )

    @staticmethod
    def _evaluate_polynomial(coefficients: tuple[float, ...], x_value: float) -> float:
        return evaluate_polynomial(coefficients, x_value)

    def _axis_z_coefficients(self) -> tuple[float, ...] | None:
        return self._axis_calibration_mapper().axis_z_coefficients()

    def _axis_z_display_for_gcode_coordinate(self, z_coordinate_mm: float) -> float:
        return self._axis_calibration_mapper().axis_z_display_for_gcode_coordinate(
            z_coordinate_mm,
        )

    def _axis_z_gcode_coordinate_for_display(self, display_mm: float) -> float:
        return self._axis_calibration_mapper().axis_z_gcode_coordinate_for_display(
            display_mm,
        )

    def _idle_timeout_for_distance(self, distance: float, feedrate: float) -> float:
        """Return an idle wait timeout long enough for slow manual G1 moves."""

        return idle_timeout_for_distance(
            distance,
            feedrate,
            min_feedrate=self.MIN_FEEDRATE,
            margin_s=self.MOVE_IDLE_TIMEOUT_MARGIN_S,
            min_timeout_s=self.MOVE_IDLE_TIMEOUT_MIN_S,
            max_timeout_s=self.MOVE_IDLE_TIMEOUT_MAX_S,
        )

    def _write_relative_g1_unchecked(
        self,
        move: MoveVector,
        *,
        feedrate: Optional[float] = None,
    ) -> None:
        """Send a single relative G1 move assuming the controller is already in G91."""

        if move.is_zero():
            return
        move_parts: list[str] = [
            f"{axis}{value:.4f}"
            for axis, value in move.items()
            if abs(value) >= 1e-6
        ]
        if not move_parts:
            return
        effective_feedrate = (
            self.DEFAULT_FEEDRATE if feedrate is None else max(self.MIN_FEEDRATE, float(feedrate))
        )
        self._write_current_command_and_wait(
            "G1 "
            + " ".join(move_parts)
            + f" F{self._format_gcode_value(effective_feedrate)}",
        )

    def _check_relative_move_limits(
        self,
        move: MoveVector,
        *,
        allow_relative: bool = False,
    ) -> None:
        if not self._axis_limits and abs(move.b) < 1e-6:
            return
        moved_limited_axes = tuple(
            axis
            for axis, delta in move.items()
            if abs(delta) >= 1e-6
            and (axis == "B" or axis in self._axis_limits)
        )
        status = self._query_current_status_with_required_coordinates(
            axes=moved_limited_axes,
        )
        positions = self._position_for_configured_mode(status)
        if status is None or not positions:
            return
        self._ensure_b_axis_zero_reference(status)
        for axis, delta in move.items():
            if abs(delta) < 1e-6:
                continue
            idx = self.AXIS_INDEX.get(axis)
            if idx is None or idx >= len(positions):
                continue
            if axis == "B":
                current_b = self._relative_b_position(status)
                limit = self.B_AXIS_SOFT_LIMIT_DEG
                target_b = current_b + delta
                if target_b < -limit or target_b > limit:
                    raise StageControllerError(
                        f"B move {delta:+.3f} exceeds software limit ({-limit:.3f}, {limit:.3f}) relative to B zero."
                    )
                continue
            limits = self._axis_limits_for_configured_mode(axis, status)
            if not limits:
                continue
            if not self._axis_software_limit_ready(status, axis):
                continue
            self._require_homed_axes(
                status, {axis}, allow_relative=allow_relative
            )
            min_value, max_value = limits
            target = positions[idx] + delta
            if target < min_value or target > max_value:
                raise StageControllerError(
                    f"{axis} move {delta:+.3f} exceeds limits ({min_value:.3f}, {max_value:.3f})."
                )

    def _axis_software_limit_ready(
        self, status: _Status | None, axis: str
    ) -> bool:
        axis = axis.upper().strip()
        if axis == "B":
            return self._b_axis_zero_position is not None
        if axis not in self._axis_limits:
            return False
        effective_homed = self._effective_homed_axes(status)
        if effective_homed is None:
            return False
        return axis in effective_homed

    def _move_safety_check(self) -> None:
        """Validate motion safety prerequisites before any move."""

        if self._motion_safety_disabled:
            return
        if not self._needles_known:
            raise AxisStateError("Needle position unknown. Home/raise A before moving.")
        if not self._needles_up:
            raise AxisStateError("Needles are down. Raise A before moving.")

    def _require_position_for_absolute_motion(
        self, status: _Status, *, required_axes: int
    ) -> tuple[float, ...]:
        position = self._position_for_configured_mode(status)
        if position is None or len(position) < required_axes:
            raise StageControllerError(
                "Controller did not report a complete position for absolute motion."
            )
        return tuple(float(value) for value in position[:required_axes])

    def _position_for_configured_mode(self, status: _Status | None) -> tuple[float, ...] | None:
        return self._axis_calibration_mapper().position_for_configured_mode(status)

    def _axis_value_for_configured_mode(
        self, status: _Status | None, axis: str
    ) -> float | None:
        return self._axis_calibration_mapper().axis_value_for_configured_mode(
            status,
            axis,
        )

    def _axis_limits_for_configured_mode(
        self, axis: str, status: _Status | None
    ) -> tuple[float, float] | None:
        limits = self._axis_limits.get(axis)
        return self._axis_calibration_mapper().axis_limits_for_configured_mode(
            axis,
            limits,
            status,
        )

    def _handle_limit_line(self, line: str) -> None:
        soft_limit_match = self.SOFT_LIMIT_AXIS_PATTERN.search(line)
        if soft_limit_match:
            axis = soft_limit_match.group("axis").upper()
            if axis in self.AXIS_INDEX:
                self._update_limit_axes(set(self._limit_axes).union({axis}))
            return
        if line.strip().lower().startswith("alarm"):
            axes = self._infer_limit_axes_from_position(None)
            if axes:
                self._update_limit_axes(axes)

    def _update_limit_axes_from_status(self, status: _Status) -> None:
        axes: set[str] = set()
        if status.pins:
            axes.update(axis for axis in status.pins if axis in self.AXIS_INDEX)
        if status.state.strip().lower() == "alarm" and not axes:
            axes.update(self._infer_limit_axes_from_position(status))
            if not axes:
                axes.update(self._limit_axes)
        self._update_limit_axes(axes)

    def _infer_limit_axes_from_position(self, status: _Status | None) -> set[str]:
        if status is None:
            position = self._last_stage_position
        else:
            position = self._position_for_configured_mode(status)
        if position is None:
            return set()
        axes: set[str] = set()
        for axis, index in self.AXIS_INDEX.items():
            if index >= len(position):
                continue
            limits = self._axis_limits_for_configured_mode(axis, status)
            if not limits:
                continue
            value = float(position[index])
            min_value, max_value = limits
            tolerance = self.LIMIT_HIT_TOLERANCE
            if value <= min_value + tolerance or value >= max_value - tolerance:
                axes.add(axis)
        return axes

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

    def _ensure_b_axis_zero_reference(self, status: _Status) -> None:
        if self._b_axis_zero_position is not None:
            return
        if self._axis_value_for_configured_mode(status, "B") is None:
            return
        self._set_b_axis_zero_reference(status, emit_status=False)

    def _set_b_axis_zero_reference(
        self, status: _Status, *, emit_status: bool = True
    ) -> None:
        b_position = self._axis_value_for_configured_mode(status, "B")
        if b_position is None:
            raise StageControllerError("B axis position unavailable.")
        self._b_axis_zero_position = b_position
        if emit_status:
            self.status_message.emit(
                f"B zero reference set to current position ({self._b_axis_zero_position:.3f})."
            )

    def _relative_b_position(self, status: _Status) -> float:
        b_position = self._axis_value_for_configured_mode(status, "B")
        if b_position is None:
            raise StageControllerError("B axis position unavailable.")
        self._ensure_b_axis_zero_reference(status)
        zero = self._b_axis_zero_position
        if zero is None:
            raise StageControllerError("B zero reference is not initialized.")
        return b_position - zero

    def _resolve_xy_from_center(
        self,
        center_position: tuple[float, ...],
        dx_pixels: float,
        dy_pixels: float,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        if self._pixels_to_mm is None:
            raise StageControllerError("Calibration is unavailable.")
        if len(center_position) < 2:
            raise StageControllerError("X/Y coordinates are unavailable.")
        center_xy = (float(center_position[0]), float(center_position[1]))
        pixel_vector = np.array([dx_pixels, dy_pixels], dtype=float)
        mm_vector = -(self._pixels_to_mm @ pixel_vector)
        target_xy = (
            center_xy[0] + float(mm_vector[0]),
            center_xy[1] + float(mm_vector[1]),
        )
        return center_xy, target_xy

    def _perform_home_command(self, command: str) -> None:
        """Execute a homing command using the current serial session."""

        self.status_message.emit(f"Homing: {command}")
        self._write_current_command_and_wait(command, timeout=30.0)
        self._wait_for_idle(timeout=30.0)
        if command.upper() in ("$H", "$HA"):
            axes = set(self._homed_axes)
            if command.upper() == "$H":
                axes.update({"X", "Y", "Z", "A"})
            else:
                axes.add("A")
            self._update_homing_status(axes)
            self._update_limit_axes(self._limit_axes.difference(axes))
            self._controller_state_stale = False
            self._set_needles_state(True, known=True, zone="raise")
            if self._controller_session_marker is None:
                self._ensure_controller_session_marker()

    def _move_vector_for_axis(self, axis: str, delta: float) -> MoveVector:
        """Create a single-axis move vector."""

        if axis == "X":
            return MoveVector(x=delta)
        if axis == "Y":
            return MoveVector(y=delta)
        if axis == "Z":
            return MoveVector(z=delta)
        if axis == "A":
            return MoveVector(a=delta)
        if axis == "B":
            return MoveVector(b=delta)
        if axis == "C":
            return MoveVector(c=delta)
        raise StageControllerError(f"Unsupported axis: {axis}")

    def _run_linear_pattern(
        self,
        *,
        axis: str,
        amplitude_mm: float,
        feedrate: float,
    ) -> None:
        """Run endless edge-to-edge motion along a single axis."""

        current_offset = 0.0
        target_offset = amplitude_mm
        direction = 1.0
        segment_length = max(
            0.001, amplitude_mm / float(self.LINEAR_SEGMENTS_PER_SWEEP)
        )
        while not self._cancel_event.is_set():
            self._check_cancelled()
            self._apply_pending_oscillation_needles_actions()
            remaining = target_offset - current_offset
            if abs(remaining) < 1e-6:
                direction *= -1.0
                target_offset = amplitude_mm * direction
                continue
            step = float(np.sign(remaining)) * min(abs(remaining), segment_length)
            self._write_relative_g1_unchecked(
                self._move_vector_for_axis(axis, step),
                feedrate=feedrate,
            )
            current_offset += step

    def _run_spiral_pattern(
        self,
        *,
        amplitude_mm: float,
        feedrate: float,
        turns_per_sweep: float,
    ) -> None:
        """Run a smooth forward-winding spiral around the current point."""

        phase = 0.0
        phase_step = (2.0 * np.pi) / float(self.SPIRAL_SEGMENTS_PER_TURN)
        start_angle = np.pi / 2.0
        last_x = 0.0
        last_y = 0.0
        while not self._cancel_event.is_set():
            self._check_cancelled()
            self._apply_pending_oscillation_needles_actions()
            phase += phase_step
            radius = amplitude_mm * 0.5 * (1.0 - float(np.cos(phase)))
            angle = start_angle + (2.0 * turns_per_sweep * phase)
            next_x = radius * float(np.cos(angle))
            next_y = radius * float(np.sin(angle))
            self._write_relative_g1_unchecked(
                MoveVector(x=next_x - last_x, y=next_y - last_y),
                feedrate=feedrate,
            )
            last_x = next_x
            last_y = next_y

    def _require_homed_axes(
        self, status: _Status, axes: set[str], *, allow_relative: bool = False
    ) -> None:
        effective_homed = self._effective_homed_axes(status)
        if effective_homed is None:
            if allow_relative:
                if not self._relative_warning_emitted:
                    self.status_message.emit(
                        "Homing status unavailable; using relative coordinates."
                    )
                    self._relative_warning_emitted = True
                return
            raise AxisStateError("Homing status unavailable; cannot read coordinates.")
        missing = axes.difference(effective_homed)
        if missing:
            if allow_relative:
                if not self._relative_warning_emitted:
                    ordered = ", ".join(sorted(missing))
                    self.status_message.emit(
                        f"Axes not homed: {ordered}. Using relative coordinates."
                    )
                    self._relative_warning_emitted = True
                return
            ordered = ", ".join(sorted(missing))
            raise AxisStateError(f"Axes not homed: {ordered}.")

    def _effective_homed_axes(self, status: _Status | None) -> set[str] | None:
        if status is None:
            return set(self._homed_axes) if self._homed_axes else None
        effective_homed = status.homed_axes
        if effective_homed is None and self._homed_axes:
            effective_homed = set(self._homed_axes)
        return effective_homed

    def _get_frame_snapshot(
        self, timeout: float = 2.0
    ) -> tuple[Optional[np.ndarray], int]:
        with self._frame_condition:
            if self._latest_frame is None:
                if not self._frame_condition.wait(timeout):
                    return (None, self._frame_counter)
            if self._latest_frame is None:
                return (None, self._frame_counter)
            return (self._latest_frame.copy(), self._frame_counter)

    def _wait_for_new_frame(
        self, previous_counter: int, timeout: float = 2.0
    ) -> tuple[Optional[np.ndarray], int]:
        with self._frame_condition:
            deadline = time.monotonic() + timeout
            while self._frame_counter <= previous_counter:
                self._check_cancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return (None, self._frame_counter)
                self._frame_condition.wait(remaining)
            if self._latest_frame is None:
                return (None, self._frame_counter)
            return (self._latest_frame.copy(), self._frame_counter)

    @staticmethod
    def _estimate_shift(frame_a: np.ndarray, frame_b: np.ndarray) -> tuple[float, float]:
        return estimate_shift(frame_a, frame_b)

    @staticmethod
    def _estimate_shift_with_response(
        frame_a: np.ndarray,
        frame_b: np.ndarray,
    ) -> tuple[float, float, float]:
        return estimate_shift_with_response(frame_a, frame_b)

    @staticmethod
    def _focus_metric(frame: np.ndarray) -> float:
        return focus_metric(frame)

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise StageControllerError("Operation cancelled.")

    def _update_homing_status(self, homed_axes: set[str]) -> None:
        if homed_axes == self._homed_axes:
            return
        self._homed_axes = set(homed_axes)
        self.homing_status_changed.emit(set(self._homed_axes))

    def _update_limit_axes(self, limit_axes: set[str]) -> None:
        axes = {
            str(axis).strip().upper()
            for axis in limit_axes
            if str(axis).strip().upper()
        }
        axes = axes.intersection(self.AXIS_INDEX)
        if axes == self._limit_axes:
            return
        self._limit_axes = set(axes)
        self.limit_axes_changed.emit(set(self._limit_axes))

    @staticmethod
    def _qimage_to_gray(image: QImage) -> np.ndarray:
        return qimage_to_gray(image, QImage.Format_RGB888)


__all__ = ["StageController", "MoveVector"]
