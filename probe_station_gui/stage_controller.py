"""Stage controller coordinating calibration and click-to-move actions."""

from __future__ import annotations

import logging
import math
from collections import deque
from queue import Empty, PriorityQueue
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np
import serial
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage
try:
    from scipy import optimize as scipy_optimize
except ImportError:  # pragma: no cover - optional dependency for autofocus
    scipy_optimize = None


logger = logging.getLogger(__name__)


class StageControllerError(RuntimeError):
    """Raised when the stage controller cannot complete an operation."""


class AxisStateError(StageControllerError):
    """Raised when axis state prevents the requested operation."""


@dataclass
class MoveVector:
    """Represents a movement across the available motion axes."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    a: float = 0.0
    b: float = 0.0
    c: float = 0.0

    def is_zero(self, tol: float = 1e-6) -> bool:
        """Return True when all components are effectively zero."""

        return all(
            abs(component) < tol
            for component in (self.x, self.y, self.z, self.a, self.b, self.c)
        )

    def items(self) -> tuple[tuple[str, float], ...]:
        """Expose the vector components in G-code axis order."""

        return (
            ("X", self.x),
            ("Y", self.y),
            ("Z", self.z),
            ("A", self.a),
            ("B", self.b),
            ("C", self.c),
        )


@dataclass
class _Status:
    state: str
    position: Optional[tuple[float, ...]] = None
    display_position: Optional[tuple[float, ...]] = None
    work_position: Optional[tuple[float, ...]] = None
    work_offset: Optional[tuple[float, ...]] = None
    coordinate_system: Optional[str] = None
    homed_axes: Optional[set[str]] = None


@dataclass(order=True)
class _QueuedSerialWrite:
    priority: int
    sequence: int
    kind: str = field(compare=False)
    payload: bytes = field(compare=False)
    description: str = field(compare=False)
    generation: int = field(compare=False, default=0)


class StageController(QObject):
    """Translate mouse clicks into stage movements via serial commands."""

    calibration_changed: Signal = Signal(float, float)
    movement_started: Signal = Signal()
    movement_finished: Signal = Signal(bool, str)
    stage_position_changed: Signal = Signal(object)
    autofocus_finished: Signal = Signal(bool, str)
    homing_status_changed: Signal = Signal(object)
    axis_a_ready_changed: Signal = Signal(bool)
    homing_action_started: Signal = Signal(str)
    homing_action_finished: Signal = Signal(bool, str, str)
    needles_state_changed: Signal = Signal(bool, bool)
    needles_action_started: Signal = Signal(str)
    needles_action_finished: Signal = Signal(bool, str, str)
    needle_height_changed: Signal = Signal(float)
    oscillation_state_changed: Signal = Signal(bool, str)
    status_message: Signal = Signal(str)

    CALIBRATION_PIXEL_TARGET = 120.0
    CALIBRATION_MIN_VERIFY_PIXELS = 15.0
    CALIBRATION_STEP_MM = 0.2
    CALIBRATION_MAX_STEPS = 25
    DEFAULT_FEEDRATE = 600.0
    MOVE_IDLE_TIMEOUT_MARGIN_S = 5.0
    MOVE_IDLE_TIMEOUT_MIN_S = 10.0
    MOVE_IDLE_TIMEOUT_MAX_S = 3600.0
    AUTOFOCUS_RANGE_MM = 0.0
    AUTOFOCUS_INITIAL_STEP_MM = 0.5
    AUTOFOCUS_FINE_STEP_MM = 0.02
    AUTOFOCUS_REFINEMENT_RANGE_MM = 2.0
    AUTOFOCUS_SAMPLES = 2
    AUTOFOCUS_ACCEPT_RATIO = 0.98
    AUTOFOCUS_MAXFUN = 35
    AUTOFOCUS_ANNEALING_MAXITER = 12
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
    SERIAL_PRIORITY_JOG_COMMAND = 10
    SERIAL_PRIORITY_SOFT_RESET = 20
    SERIAL_PRIORITY_TERMINAL = 30
    SERIAL_JOG_COMMAND_SETTLE_S = 0.03

    STATUS_PATTERN = re.compile(r"^<(?P<body>[^>]*)>")
    STATUS_FIELD_PATTERN = re.compile(r"(?P<key>[A-Za-z]+):(?P<value>.+)")
    AXIS_RANGE_PATTERN = re.compile(
        r"^\[MSG:INFO: Axis (?P<axis>[A-Za-z]) \((?P<min>-?\d+\.?\d*),(?P<max>-?\d+\.?\d*)\)\]"
    )
    HOMED_PATTERN = re.compile(r"\|H:([A-Za-z]+)")
    HOMED_MSG_PATTERN = re.compile(r"^\[MSG:Homed:(?P<axes>[A-Za-z]+)\]")
    MODAL_STATE_PATTERN = re.compile(r"^\[GC:(?P<modal>[^\]]+)\]$")
    COORDINATE_OFFSET_PATTERN = re.compile(
        r"^\[(?P<system>G5(?:4|5|6|7|8|9(?:\.[123])?)):(?P<coords>[^\]]+)\]$"
    )
    AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}
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
    DEFAULT_WORK_COORDINATE_SYSTEM = "G54"

    def __init__(self) -> None:
        super().__init__()
        self._serial: Optional[serial.Serial] = None
        self._pixels_to_mm: Optional[np.ndarray] = None
        self._last_stage_position: Optional[tuple[float, ...]] = None
        self._last_machine_position: Optional[tuple[float, ...]] = None
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_counter = 0
        self._frame_condition = threading.Condition()
        self._task_lock = threading.Lock()
        self._active_thread: Optional[threading.Thread] = None
        self._status_refresh_thread: Optional[threading.Thread] = None
        self._cancel_event = threading.Event()
        self._axis_limits: dict[str, tuple[float, float]] = {}
        self._homed_axes: set[str] = set()
        self._relative_warning_emitted = False
        self._axis_a_ready = False
        self._needles_up = False
        self._needles_known = False
        self._needle_down_offset: Optional[float] = None
        self._oscillation_active = False
        self._motion_safety_disabled = False
        self._queued_needles_actions: deque[tuple[str, float | None]] = deque()
        self._oscillation_needles_actions: deque[tuple[str, float | None]] = deque()
        self._b_axis_zero_position: Optional[float] = None
        self._serial_session_lock = threading.RLock()
        self._queued_write_sequence = 0
        self._queued_jog_generation = 0
        self._last_stage_state: Optional[str] = None
        self._last_status_timestamp: Optional[float] = None
        self._last_jog_write_timestamp: Optional[float] = None
        self._last_a_position_read_failure: Optional[str] = None
        self._controller_state_stale = False
        self._position_reporting_mode = "work"
        self._current_status_report_mask: Optional[int] = None
        self._coordinate_startup_mode = "controller"
        self._preferred_work_coordinate_system = self.DEFAULT_WORK_COORDINATE_SYSTEM
        self._active_work_coordinate_system: Optional[str] = None
        self._controller_coordinate_offsets: dict[str, tuple[float, ...]] = {}
        self._async_write_queue: PriorityQueue[_QueuedSerialWrite] = PriorityQueue()
        self._async_write_shutdown = threading.Event()
        self._async_write_thread = threading.Thread(
            target=self._run_async_write_worker,
            daemon=True,
        )
        self._async_write_thread.start()

    def set_serial(self, serial_connection: Optional[serial.Serial]) -> None:
        """Assign or clear the serial connection used for stage control."""

        with self._task_lock:
            self._serial = serial_connection
            self._queued_jog_generation += 1
            self._clear_pending_async_writes()
            if serial_connection is None or not serial_connection.is_open:
                self._pixels_to_mm = None
                self._last_stage_state = None
                self._last_status_timestamp = None
                self._last_jog_write_timestamp = None
                self._current_status_report_mask = None
                self._last_machine_position = None
                self._active_work_coordinate_system = None
                self._controller_coordinate_offsets.clear()
                self._axis_limits.clear()
                self._b_axis_zero_position = None
                self._controller_state_stale = bool(
                    self._last_stage_position is not None
                    or self._homed_axes
                    or self._needles_known
                )
                self._refresh_axis_a_ready_from_state()
            else:
                self._axis_limits.clear()
                self._b_axis_zero_position = None
                self._controller_state_stale = True
                if bool(
                    getattr(serial_connection, "probe_station_reboot_detected", False)
                ):
                    logger.warning(
                        "Controller reboot detected on serial connect; clearing cached homing and position state."
                    )
                    self._last_stage_position = None
                    self._last_machine_position = None
                    self._last_stage_state = None
                    self._current_status_report_mask = None
                    self._active_work_coordinate_system = None
                    self._controller_coordinate_offsets.clear()
                    self._update_homing_status(set())
                    self._set_needles_state(False, known=False)
                else:
                    self._refresh_axis_a_ready_from_state()

    def export_cached_controller_state(self) -> dict[str, object] | None:
        """Return controller state suitable for persistence across app restarts."""

        if (
            self._last_stage_position is None
            and not self._homed_axes
            and not self._needles_known
        ):
            return None
        return {
            "last_stage_position": (
                list(self._last_stage_position)
                if self._last_stage_position is not None
                else None
            ),
            "last_machine_position": (
                list(self._last_machine_position)
                if self._last_machine_position is not None
                else None
            ),
            "last_stage_state": self._last_stage_state,
            "active_work_coordinate_system": self._active_work_coordinate_system,
            "controller_coordinate_offsets": {
                system: list(values)
                for system, values in self._controller_coordinate_offsets.items()
            },
            "homed_axes": sorted(self._homed_axes),
            "needles_up": bool(self._needles_up),
            "needles_known": bool(self._needles_known),
        }

    def import_cached_controller_state(self, data: dict[str, object]) -> None:
        """Restore controller state persisted from a previous application run."""

        position_raw = data.get("last_stage_position")
        position = None
        if isinstance(position_raw, (list, tuple)):
            try:
                coords = tuple(float(value) for value in position_raw)
                if coords:
                    position = coords
            except (TypeError, ValueError):
                position = None
        state_raw = data.get("last_stage_state")
        if isinstance(state_raw, str) and state_raw.strip():
            self._last_stage_state = state_raw.strip()
        self._last_stage_position = position
        machine_position_raw = data.get("last_machine_position")
        machine_position = None
        if isinstance(machine_position_raw, (list, tuple)):
            try:
                coords = tuple(float(value) for value in machine_position_raw)
                if coords:
                    machine_position = coords
            except (TypeError, ValueError):
                machine_position = None
        self._last_machine_position = machine_position
        coordinate_system_raw = data.get("active_work_coordinate_system")
        if (
            isinstance(coordinate_system_raw, str)
            and coordinate_system_raw.strip().upper() in self.WORK_COORDINATE_SYSTEMS
        ):
            self._active_work_coordinate_system = coordinate_system_raw.strip().upper()
        offsets_raw = data.get("controller_coordinate_offsets")
        self._controller_coordinate_offsets = self._parse_controller_coordinate_offsets(
            offsets_raw
        )
        self._controller_state_stale = True
        homed_raw = data.get("homed_axes")
        homed_axes: set[str] = set()
        if isinstance(homed_raw, (list, tuple)):
            for value in homed_raw:
                if isinstance(value, str) and value.strip():
                    homed_axes.add(value.strip().upper())
        self._update_homing_status(homed_axes)
        self._set_needles_state(
            bool(data.get("needles_up", False)),
            known=bool(data.get("needles_known", False)),
        )
        self._refresh_axis_a_ready_from_state()
        if position is not None:
            self.stage_position_changed.emit(tuple(position))

    def clear_cached_controller_state(self) -> None:
        """Forget locally cached controller state."""

        self._last_stage_position = None
        self._last_machine_position = None
        self._last_stage_state = None
        self._active_work_coordinate_system = None
        self._controller_coordinate_offsets.clear()
        self._update_homing_status(set())
        self._set_needles_state(False, known=False)

    def request_startup_sync(self, *, auto_home_a: bool = True) -> None:
        """Load controller state after connect and optionally home A."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Skipping startup sync.")
                return
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

        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            raise StageControllerError("Serial connection is not available.")
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
        with self._frame_condition:
            self._latest_frame = gray
            self._frame_counter += 1
            self._frame_condition.notify_all()

    def _poll_status_once(self) -> None:
        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            return
        try:
            if not self._serial_session_lock.acquire(blocking=False):
                return
            try:
                self._query_status(serial_connection)
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

    def request_move(self, dx_pixels: float, dy_pixels: float) -> None:
        """Begin an asynchronous move so the clicked point aligns with the cross."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring the new click.")
                return
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_move,
                args=(dx_pixels, dy_pixels),
                daemon=True,
            )
            self._active_thread = thread
            thread.start()

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
        self, axis: str, distance_mm: float, mode: str, feedrate: float | None = None
    ) -> None:
        """Move an arbitrary axis from the manual jog controls."""

        axis = axis.upper().strip()
        if axis not in self.AXIS_INDEX:
            self.status_message.emit(f"Unsupported axis: {axis}")
            return
        mode = mode.upper().strip()
        if mode not in {"G90", "G91"}:
            self.status_message.emit(f"Unsupported manual move mode: {mode}")
            return
        try:
            effective_feedrate = (
                None if feedrate is None else max(1.0, float(feedrate))
            )
        except (TypeError, ValueError):
            self.status_message.emit(f"Unsupported manual feedrate: {feedrate}")
            return
        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring manual axis move.")
                return
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_manual_axis_move,
                args=(axis, float(distance_mm), mode, effective_feedrate),
                daemon=True,
            )
            self._active_thread = thread
            thread.start()

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

    def request_home_axis(self, axis: str) -> None:
        """Home a specific axis via a background task."""

        axis = axis.upper().strip()
        if not axis:
            return
        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring home request.")
                return
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_home, args=(f"$H{axis}", axis), daemon=True
            )
            self._active_thread = thread
            self.homing_action_started.emit(axis)
            thread.start()

    def request_home_all(self) -> None:
        """Home all axes via a background task."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring home request.")
                return
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_home, args=("$H", "ALL"), daemon=True
            )
            self._active_thread = thread
            self.homing_action_started.emit("ALL")
            thread.start()

    def request_needles_raise(self) -> None:
        """Raise the needles by homing the A axis."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                if self._oscillation_active:
                    self._queue_oscillation_needles_action_locked("raise")
                    return
                self.status_message.emit("Stage is busy. Ignoring needle raise request.")
                return
            self._start_needles_action_locked("raise")

    def request_needles_lower(self) -> None:
        """Lower the needles to the calibrated down position."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                if self._oscillation_active:
                    self._queue_oscillation_needles_action_locked("lower")
                    return
                self.status_message.emit("Stage is busy. Ignoring needle lower request.")
                return
            self._start_needles_action_locked("lower")

    def apply_needle_calibration(
        self,
        *,
        down_position_mm: Optional[float],
    ) -> None:
        """Apply the persisted needle calibration settings."""

        self._needle_down_offset = down_position_mm

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

    def request_needles_adjust(self, step_mm: float) -> None:
        """Adjust the A axis for needle calibration without the XY safety gate."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                if self._oscillation_active:
                    self._queue_oscillation_needles_action_locked(
                        "adjust", float(step_mm)
                    )
                    return
                self.status_message.emit("Stage is busy. Ignoring needle adjustment.")
                return
            self._start_needles_action_locked("adjust", float(step_mm))

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
        with self._serial_session_lock:
            a_position = self._read_current_a_position(serial_connection)
        if a_position is None:
            return None
        self.needle_height_changed.emit(a_position)
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
            except serial.SerialException as exc:  # pragma: no cover - hardware interaction
                raise StageControllerError(f"Serial read failed: {exc}") from exc
            if waiting <= 0:
                return b""
            if max_bytes is not None:
                waiting = min(waiting, max(1, int(max_bytes)))
            logger.debug("SERIAL TRACE terminal_in_waiting bytes=%s", waiting)
            try:
                data = serial_connection.read(waiting)
            except serial.SerialException as exc:  # pragma: no cover - hardware interaction
                raise StageControllerError(f"Serial read failed: {exc}") from exc
            if data:
                logger.debug("SERIAL TRACE terminal_read bytes=%r", data[:200])
            return data
        finally:
            self._serial_session_lock.release()

    def invalidate_needles_state(self, reason: str = "") -> None:
        """Mark needles state unknown after manual A-axis changes."""

        if reason:
            self.status_message.emit(reason)
        self._set_needles_state(False, known=False)

    def cancel_active_task(self, reason: str = "Operation cancelled.") -> None:
        """Signal any active task to stop as soon as possible."""

        self._cancel_event.set()
        self.status_message.emit(reason)
        self._update_homing_status(set())
        self._set_needles_state(False, known=False)

    def is_busy(self) -> bool:
        """Return True when a background movement task is currently running."""

        with self._task_lock:
            return bool(self._active_thread and self._active_thread.is_alive())

    def current_stage_position(self) -> tuple[float, ...]:
        """Return the latest controller position in the active GUI coordinate space."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                raise StageControllerError("Stage is busy. Wait for the current operation to finish.")
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
            with self._serial_session_lock:
                status = self._query_synced_status_for_absolute_motion(
                    serial_connection
                )
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
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
            with self._serial_session_lock:
                status = self._query_status(serial_connection)
                if status is None or status.display_position is None:
                    raise StageControllerError("Unable to read stage position.")
                self._ensure_calibration(serial_connection)
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
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
            with self._serial_session_lock:
                status = self._query_status(serial_connection)
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
        self._queued_jog_generation += 1
        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_JOG_COMMAND,
                sequence=self._next_queued_write_sequence(),
                kind="jog_command",
                payload=(stripped + "\n").encode("ascii"),
                description=stripped,
                generation=self._queued_jog_generation,
            )
        )

    def queue_jog_stop(self) -> None:
        """Queue a jog stop command without blocking the UI thread."""

        # Invalidate any queued-but-not-yet-written jog command so a late $J
        # cannot arrive after the stop and keep motion alive.
        self._queued_jog_generation += 1
        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_JOG_STOP,
                sequence=self._next_queued_write_sequence(),
                kind="jog_stop",
                payload=b"\x85",
                description="0x85",
                generation=self._queued_jog_generation,
            )
        )

    def queue_soft_reset(self, *, source: str = "unknown") -> None:
        """Queue a FluidNC soft reset without blocking the UI thread."""

        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_SOFT_RESET,
                sequence=self._next_queued_write_sequence(),
                kind="soft_reset",
                payload=b"\x18",
                description=f"CTRL-X source={source}",
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
        self.status_message.emit(reason)

    def _run_move(self, dx_pixels: float, dy_pixels: float) -> None:
        self.movement_started.emit()
        try:
            self._check_cancelled()
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
            self._move_safety_check()
            before_counter = self._prepare_click_move_without_status_locked(
                serial_connection,
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
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
            self._move_safety_check()
            message = self._move_to_xy_locked(
                serial_connection,
                target_x_mm,
                target_y_mm,
            )
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
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
            self._move_safety_check()
            message = self._move_to_xyz_locked(
                serial_connection,
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
        serial_connection: serial.Serial,
        target_x_mm: float,
        target_y_mm: float,
    ) -> str:
        with self._serial_session_lock:
            status = self._query_synced_status_for_absolute_motion(serial_connection)
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
            self._send_relative_move(serial_connection, move)
            self._wait_for_idle(serial_connection)
            self._query_status(serial_connection)
            return f"Arrived at X={target_x_mm:.3f} mm, Y={target_y_mm:.3f} mm."

    def _move_to_xyz_locked(
        self,
        serial_connection: serial.Serial,
        target_x_mm: float,
        target_y_mm: float,
        target_z_mm: float,
        *,
        transit_z_mm: float | None,
        label: str,
    ) -> str:
        with self._serial_session_lock:
            status = self._query_synced_status_for_absolute_motion(serial_connection)
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
                    serial_connection,
                    MoveVector(z=transit_z - current_z),
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
                self._send_relative_move(serial_connection, xy_move)
                current_x = float(target_position[0])
                current_y = float(target_position[1])
                moved = True

            delta_z = float(target_position[2]) - current_z
            if abs(delta_z) >= 1e-5:
                self.status_message.emit(
                    f"Moving Z to {label} focus height {target_z_mm:.3f} mm"
                )
                self._send_relative_move(
                    serial_connection,
                    MoveVector(z=delta_z),
                )
                moved = True

            self._wait_for_idle(serial_connection)
            self._query_status(serial_connection)

            if not moved:
                return f"{label.capitalize()} already reached."
            return (
                f"Arrived at {label}: X={target_x_mm:.3f} mm, "
                f"Y={target_y_mm:.3f} mm, Z={target_z_mm:.3f} mm."
            )

    def _query_synced_status_for_absolute_motion(
        self, serial_connection: serial.Serial
    ) -> Optional[_Status]:
        """Refresh coordinate-system state before absolute position reads and moves."""

        if self._position_reporting_mode != "machine" or self._controller_state_stale:
            self._refresh_coordinate_system_state(
                serial_connection,
                apply_preference=True,
            )
        return self._query_status(serial_connection)

    def _prepare_click_move_without_status_locked(
        self,
        serial_connection: serial.Serial,
        dx_pixels: float,
        dy_pixels: float,
    ) -> int | None:
        with self._serial_session_lock:
            self._ensure_calibration(serial_connection)
            self._check_cancelled()
            if self._pixels_to_mm is None:
                raise StageControllerError("Calibration failed. Cannot move stage.")

            if abs(dx_pixels) < 1e-3 and abs(dy_pixels) < 1e-3:
                return None

            _, before_counter = self._get_frame_snapshot()
            pixel_vector = np.array([dx_pixels, dy_pixels], dtype=float)
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
            self._send_relative_move(serial_connection, move)
            return before_counter

    def _run_rotate_b(self, delta_deg: float) -> None:
        self.movement_started.emit()
        try:
            self._check_cancelled()
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
            if abs(delta_deg) < 1e-3:
                self.movement_finished.emit(True, "Chip is already aligned.")
                return
            self.status_message.emit(f"Chip alignment: rotating B by {delta_deg:+.3f} deg.")
            self._send_relative_move(
                serial_connection,
                MoveVector(b=delta_deg),
                allow_relative=True,
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
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
            if scipy_optimize is None:
                raise StageControllerError(
                    "SciPy is required for autofocus optimization. Please install it."
                )
            self._relative_warning_emitted = False
            if not self._needles_up:
                self.status_message.emit("Autofocus: homing A axis.")
                self._write_command(serial_connection, "$HA")
                self._wait_for_ok(serial_connection, timeout=30.0)
                self._wait_for_idle(serial_connection, timeout=30.0)
                self._set_needles_state(True, known=True)
            self._move_safety_check()

            self._ensure_axis_limits(serial_connection)
            local_range = 1.0
            fine_step = float(self.AUTOFOCUS_FINE_STEP_MM)
            if fine_step <= 0:
                raise StageControllerError("Autofocus parameters are invalid.")
            with self._frame_condition:
                frame_counter = self._frame_counter

            status = self._query_status(serial_connection)
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
            lower_limit = max(min_z - start_z, -local_range)
            upper_limit = min(max_z - start_z, local_range)
            if upper_limit <= lower_limit:
                raise StageControllerError("Z axis range near current position is empty.")

            current_offset = 0.0

            def move_to_offset(target_offset: float, timeout: float = 3.0) -> np.ndarray:
                nonlocal current_offset, frame_counter
                self._check_cancelled()
                target_offset = max(lower_limit, min(upper_limit, target_offset))
                delta = target_offset - current_offset
                if abs(delta) < 1e-6:
                    frame, frame_counter = self._get_frame_snapshot(timeout=timeout)
                else:
                    self._send_relative_move(
                        serial_connection, MoveVector(z=delta), allow_relative=True
                    )
                    frame, frame_counter = self._wait_for_new_frame(
                        frame_counter, timeout=timeout
                    )
                if frame is None:
                    raise StageControllerError(
                        "Camera did not update during autofocus movement."
                    )
                current_offset = target_offset
                return frame

            def focus_at(offset: float) -> float:
                offset = max(lower_limit, min(upper_limit, offset))
                samples = max(1, int(self.AUTOFOCUS_SAMPLES))
                total = 0.0
                for _ in range(samples):
                    frame = move_to_offset(offset, timeout=2.5)
                    total += self._focus_metric(frame)
                return total / samples

            self.status_message.emit(
                f"Autofocus: local search within ±{local_range:.3f} mm."
            )
            start_score = focus_at(0.0)

            self.status_message.emit("Autofocus: local refinement.")
            result = scipy_optimize.minimize_scalar(
                lambda x: -focus_at(x),
                bounds=(lower_limit, upper_limit),
                method="bounded",
                options={"xatol": fine_step, "maxiter": self.AUTOFOCUS_MAXFUN},
            )
            best_offset = float(result.x)
            best_score = -float(result.fun)

            if best_score < start_score * self.AUTOFOCUS_ACCEPT_RATIO:
                move_to_offset(0.0, timeout=3.0)
                message = (
                    "Autofocus complete. Best focus was worse than start; kept current position."
                )
                self.autofocus_finished.emit(True, message)
                return

            move_to_offset(best_offset, timeout=3.0)
            message = (
                "Autofocus complete. "
                f"Best score {best_score:.2f} at offset {best_offset:+.3f} mm."
            )
            self.autofocus_finished.emit(True, message)
        except StageControllerError as exc:
            self.autofocus_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                self._active_thread = None

    def _run_home(self, command: str, axis_key: str) -> None:
        self.movement_started.emit()
        try:
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
            self._perform_home_command(serial_connection, command)
            self.movement_finished.emit(True, "Homing complete.")
            self.homing_action_finished.emit(True, "Homing complete.", axis_key)
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
            self.homing_action_finished.emit(False, str(exc), axis_key)
        finally:
            with self._task_lock:
                self._active_thread = None

    def _run_startup_sync(self, auto_home_a: bool) -> None:
        try:
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")

            self.status_message.emit("Loading controller startup state...")
            with self._serial_session_lock:
                self._ensure_axis_limits(serial_connection)
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

            if not auto_home_a:
                return

            if effective_homed is None or "A" not in effective_homed:
                self.movement_started.emit()
                self.homing_action_started.emit("A")
                try:
                    self.status_message.emit("A axis not homed. Homing needles on startup.")
                    with self._serial_session_lock:
                        self._perform_home_command(serial_connection, "$HA")
                    self.movement_finished.emit(True, "Startup A homing complete.")
                    self.homing_action_finished.emit(
                        True, "Startup A homing complete.", "A"
                    )
                except StageControllerError as exc:
                    self.movement_finished.emit(False, str(exc))
                    self.homing_action_finished.emit(False, str(exc), "A")
                    raise
        except StageControllerError as exc:
            self.status_message.emit(str(exc))
        finally:
            with self._task_lock:
                self._active_thread = None

    def _ensure_calibration(self, serial_connection: serial.Serial) -> None:
        if self._pixels_to_mm is not None:
            return
        self.status_message.emit("Starting calibration sequence…")
        before_frame, _ = self._get_frame_snapshot(timeout=3.0)
        if before_frame is None:
            raise StageControllerError("Camera frames are unavailable for calibration.")

        start_status = self._query_status(serial_connection)
        origin = self._position_for_configured_mode(start_status)
        if start_status is None or origin is None:
            raise StageControllerError("Unable to read position for calibration.")
        self._require_homed_axes(start_status, {"X", "Y"})

        try:
            mm_x, shift_x_vec = self._calibrate_axis(
                serial_connection, before_frame, origin, axis="X"
            )
            latest_frame, _ = self._get_frame_snapshot(timeout=2.0)
            reference_for_y = latest_frame if latest_frame is not None else before_frame
            mm_y, shift_y_vec = self._calibrate_axis(
                serial_connection, reference_for_y, origin, axis="Y"
            )
        finally:
            self._return_to_origin(serial_connection, origin)

        calibration_matrix = np.column_stack(
            (shift_x_vec / mm_x, shift_y_vec / mm_y)
        )
        if not np.isfinite(calibration_matrix).all():
            raise StageControllerError("Calibration produced invalid values.")
        determinant = float(np.linalg.det(calibration_matrix))
        if abs(determinant) < 1e-9:
            raise StageControllerError("Calibration matrix is singular.")
        self._pixels_to_mm = np.linalg.inv(calibration_matrix)
        mm_per_pixel_x, mm_per_pixel_y = self._calibration_magnitudes()
        self.calibration_changed.emit(mm_per_pixel_x, mm_per_pixel_y)
        self.status_message.emit(
            f"Calibration updated: ΔX {mm_per_pixel_x:.6f} mm/px, ΔY {mm_per_pixel_y:.6f} mm/px"
        )

    def _calibrate_axis(
        self,
        serial_connection: serial.Serial,
        reference_frame: np.ndarray,
        origin: tuple[float, float, float],
        axis: str,
    ) -> tuple[float, np.ndarray]:
        if reference_frame is None:
            raise StageControllerError("Reference frame unavailable for calibration.")
        index = 0 if axis == "X" else 1
        total_mm = 0.0
        with self._frame_condition:
            frame_counter = self._frame_counter
        for _ in range(self.CALIBRATION_MAX_STEPS):
            if axis == "X":
                move = MoveVector(x=self.CALIBRATION_STEP_MM)
            else:
                move = MoveVector(y=self.CALIBRATION_STEP_MM)
            self._send_relative_move(serial_connection, move)
            new_frame, frame_counter = self._wait_for_new_frame(frame_counter, timeout=2.0)
            if new_frame is None:
                raise StageControllerError("Camera did not update during calibration.")
            status = self._query_status(serial_connection)
            current = self._position_for_configured_mode(status)
            if status is None or current is None:
                raise StageControllerError("Unable to query position during calibration.")
            self._require_homed_axes(status, {axis})
            total_mm = current[index] - origin[index]
            shift_x, shift_y = self._estimate_shift(reference_frame, new_frame)
            axis_shift = shift_x if axis == "X" else shift_y
            if abs(axis_shift) >= self.CALIBRATION_PIXEL_TARGET:
                break

        if abs(total_mm) < 1e-6:
            raise StageControllerError("Detected zero movement while calibrating.")
        shift_vector = np.array([shift_x, shift_y], dtype=float)
        if np.linalg.norm(shift_vector) < 1e-6:
            raise StageControllerError("Pixel shift too small to compute calibration.")
        return total_mm, shift_vector

    def _return_to_origin(
        self, serial_connection: serial.Serial, origin: tuple[float, float, float]
    ) -> None:
        status = self._query_status(serial_connection)
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
        self._send_relative_move(serial_connection, move)

    def _run_needles_action(self, action: str) -> None:
        self.needles_action_started.emit(action)
        try:
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
            if action == "raise":
                status = self._query_status(serial_connection)
                effective_homed = status.homed_axes if status else None
                if effective_homed is None and self._homed_axes:
                    effective_homed = set(self._homed_axes)
                if (
                    status is not None
                    and self._position_for_configured_mode(status) is not None
                    and effective_homed is not None
                    and "A" in effective_homed
                ):
                    current_a = self._axis_value_for_configured_mode(status, "A")
                    if current_a is None:
                        raise StageControllerError("A axis position unavailable.")
                    if abs(current_a) >= 1e-6:
                        self.status_message.emit("Needles: raising to A zero.")
                        self._send_relative_move(
                            serial_connection,
                            MoveVector(a=-current_a),
                            ignore_needle_safety=True,
                        )
                    self._update_needles_from_a_position(0.0)
                    self.needles_action_finished.emit(True, "Needles raised.", action)
                    return
                self.status_message.emit("Needles: raising (home A).")
                self._write_command(serial_connection, "$HA")
                self._wait_for_ok(serial_connection, timeout=30.0)
                self._wait_for_idle(serial_connection, timeout=30.0)
                self._update_needles_from_a_position(0.0)
                self.needles_action_finished.emit(True, "Needles raised.", action)
                return
            if action == "lower":
                if self._needle_down_offset is None:
                    raise StageControllerError(
                        "Needle down calibration missing; cannot lower."
                    )
                status = self._query_status(serial_connection)
                current_a = self._axis_value_for_configured_mode(status, "A")
                if status is None or current_a is None:
                    raise StageControllerError("Unable to read A position for needles.")
                self._require_homed_axes(status, {"A"})
                delta = float(self._needle_down_offset) - current_a
                if abs(delta) < 1e-6:
                    self._update_needles_from_a_position(float(self._needle_down_offset))
                    self.needles_action_finished.emit(True, "Needles already lowered.", action)
                    return
                self._send_relative_move(
                    serial_connection,
                    MoveVector(a=delta),
                    ignore_needle_safety=True,
                )
                self._update_needles_from_a_position(float(self._needle_down_offset))
                self.needles_action_finished.emit(True, "Needles lowered.", action)
                return
            raise StageControllerError(f"Unknown needle action: {action}.")
        except StageControllerError as exc:
            self.needles_action_finished.emit(False, str(exc), action)
        finally:
            with self._task_lock:
                self._active_thread = None
            self._start_next_queued_needles_action()

    def _run_manual_axis_move(
        self, axis: str, distance_mm: float, mode: str, feedrate: float | None
    ) -> None:
        self.movement_started.emit()
        try:
            self._check_cancelled()
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
            if abs(distance_mm) < 1e-6:
                self.movement_finished.emit(True, "Manual axis move skipped.")
                return
            move = self._move_vector_for_axis(axis, distance_mm)
            feedrate_text = (
                self.DEFAULT_FEEDRATE if feedrate is None else max(1.0, float(feedrate))
            )
            self.status_message.emit(
                f"Manual axis move ({mode}): {axis}{distance_mm:+.3f} F{feedrate_text:.0f}."
            )
            if mode == "G91":
                with self._serial_session_lock:
                    self._send_relative_move(
                        serial_connection,
                        move,
                        allow_relative=True,
                        ignore_needle_safety=self._motion_safety_disabled,
                        feedrate=feedrate,
                        wait_for_completion=False,
                    )
            else:
                with self._serial_session_lock:
                    self._send_absolute_axis_move(
                        serial_connection,
                        axis,
                        distance_mm,
                        ignore_needle_safety=self._motion_safety_disabled,
                        feedrate=feedrate,
                        wait_for_completion=False,
                    )
            self.movement_finished.emit(
                True,
                f"Manual axis move accepted ({mode} {axis}{distance_mm:+.3f}).",
            )
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                self._active_thread = None

    def _run_needles_adjust(self, step_mm: float) -> None:
        action = "adjust"
        try:
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
            if abs(step_mm) < 1e-6:
                self.needles_action_finished.emit(True, "Needle position unchanged.", action)
                return
            status = self._query_status(serial_connection)
            if status is None or self._axis_value_for_configured_mode(status, "A") is None:
                raise StageControllerError("Unable to read A position for needles.")
            self._require_homed_axes(status, {"A"})
            self._send_relative_move(
                serial_connection,
                MoveVector(a=step_mm),
                ignore_needle_safety=True,
            )
            current_a = self._read_current_a_position(serial_connection)
            if current_a is None:
                raise StageControllerError("Unable to confirm A position after move.")
            self._update_needles_from_a_position(current_a)
            direction = "lowered" if step_mm < 0 else "raised"
            self.needles_action_finished.emit(
                True,
                f"Needles {direction} by {abs(step_mm):.3f} mm.",
                action,
            )
        except StageControllerError as exc:
            self.needles_action_finished.emit(False, str(exc), action)
        finally:
            with self._task_lock:
                self._active_thread = None
            self._start_next_queued_needles_action()

    def _run_oscillation(
        self, mode: str, amplitude_mm: float, feedrate: float, turns_per_sweep: float
    ) -> None:
        try:
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
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
            self._oscillation_active = True
            self.oscillation_state_changed.emit(True, mode)
            self.status_message.emit(
                f"Oscillation started in {mode}: amplitude={amplitude_mm:.3f} mm, "
                f"feedrate={feedrate:.1f} mm/min."
            )
            self._write_command(serial_connection, "G21")
            self._wait_for_ok(serial_connection)
            self._write_command(serial_connection, "G91")
            self._wait_for_ok(serial_connection)
            if mode == "SPIRAL":
                self._run_spiral_pattern(
                    serial_connection,
                    amplitude_mm=amplitude_mm,
                    feedrate=feedrate,
                    turns_per_sweep=turns_per_sweep,
                )
            else:
                self._run_linear_pattern(
                    serial_connection,
                    axis=mode,
                    amplitude_mm=amplitude_mm,
                    feedrate=feedrate,
                )
            self._write_command(serial_connection, "G90")
            self._wait_for_ok(serial_connection)
            self.status_message.emit("Oscillation stopped.")
        except StageControllerError as exc:
            try:
                if serial_connection is not None and serial_connection.is_open:
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

    def _update_calibration_from_measurement(
        self,
        expected_pixels: np.ndarray,
        measured_pixels: np.ndarray,
        mm_vector: np.ndarray,
    ) -> str:
        message = "Move complete."
        if self._pixels_to_mm is None:
            return message
        if np.linalg.norm(expected_pixels) < self.CALIBRATION_MIN_VERIFY_PIXELS:
            return message
        if np.linalg.norm(measured_pixels) < 1e-6:
            return message

        predicted_mm = self._pixels_to_mm @ measured_pixels
        error = mm_vector - predicted_mm
        denom = float(measured_pixels @ measured_pixels)
        if abs(denom) < 1e-6:
            return message
        correction = np.outer(error, measured_pixels) / denom
        updated_matrix = self._pixels_to_mm + correction
        if not np.isfinite(updated_matrix).all():
            return message
        self._pixels_to_mm = updated_matrix
        mm_per_pixel_x, mm_per_pixel_y = self._calibration_magnitudes()
        self.calibration_changed.emit(mm_per_pixel_x, mm_per_pixel_y)
        message += " Calibration refined."
        return message

    def _calibration_magnitudes(self) -> tuple[float, float]:
        if self._pixels_to_mm is None:
            return (0.0, 0.0)
        column_x = self._pixels_to_mm[:, 0]
        column_y = self._pixels_to_mm[:, 1]
        return (float(np.linalg.norm(column_x)), float(np.linalg.norm(column_y)))

    def _send_relative_move(
        self,
        serial_connection: serial.Serial,
        move: MoveVector,
        *,
        allow_relative: bool = False,
        ignore_needle_safety: bool = False,
        feedrate: Optional[float] = None,
        wait_for_completion: bool = True,
    ) -> None:
        if move.is_zero():
            return
        if not ignore_needle_safety:
            self._move_safety_check()
        if not self._motion_safety_disabled:
            self._ensure_axis_limits(serial_connection)
            self._check_relative_move_limits(
                serial_connection, move, allow_relative=allow_relative
            )
        self._write_command(serial_connection, "G21")
        self._wait_for_ok(serial_connection)
        self._write_command(serial_connection, "G91")
        self._wait_for_ok(serial_connection)
        move_parts: list[str] = [
            f"{axis}{value:.4f}"
            for axis, value in move.items()
            if abs(value) >= 1e-6
        ]
        if not move_parts:
            return
        effective_feedrate = (
            self.DEFAULT_FEEDRATE if feedrate is None else max(1.0, float(feedrate))
        )
        move_distance = self._move_distance_for_timeout(move)
        command = "G1 " + " ".join(move_parts) + f" F{effective_feedrate:.0f}"
        self._write_command(serial_connection, command)
        self._wait_for_ok(serial_connection)
        self._write_command(serial_connection, "G90")
        self._wait_for_ok(serial_connection)
        if wait_for_completion:
            self._wait_for_idle(
                serial_connection,
                timeout=self._idle_timeout_for_distance(
                    move_distance, effective_feedrate
                ),
            )

    def _send_absolute_axis_move(
        self,
        serial_connection: serial.Serial,
        axis: str,
        value: float,
        *,
        ignore_needle_safety: bool = False,
        feedrate: Optional[float] = None,
        wait_for_completion: bool = True,
    ) -> None:
        axis = axis.upper().strip()
        if axis not in self.AXIS_INDEX:
            raise StageControllerError(f"Unsupported axis: {axis}")
        if not ignore_needle_safety:
            self._move_safety_check()
        current_value: float | None = None
        if not self._motion_safety_disabled:
            self._ensure_axis_limits(serial_connection)
            status = self._query_status(serial_connection)
            if status is None:
                raise StageControllerError("Unable to read position for absolute move.")
            self._require_homed_axes(status, {axis})
            current_value = self._axis_value_for_configured_mode(status, axis)
            limits = self._axis_limits_for_configured_mode(axis, status)
            if limits:
                min_value, max_value = limits
                if value < min_value or value > max_value:
                    raise StageControllerError(
                        f"{axis} target {value:+.3f} exceeds limits ({min_value:.3f}, {max_value:.3f})."
                    )
        effective_feedrate = (
            self.DEFAULT_FEEDRATE if feedrate is None else max(1.0, float(feedrate))
        )
        self._write_command(serial_connection, "G21")
        self._wait_for_ok(serial_connection)
        self._write_command(serial_connection, "G90")
        self._wait_for_ok(serial_connection)
        self._write_command(
            serial_connection,
            f"G1 {axis}{value:.4f} F{effective_feedrate:.0f}",
        )
        self._wait_for_ok(serial_connection)
        if wait_for_completion:
            move_distance = (
                abs(value)
                if current_value is None
                else abs(float(value) - float(current_value))
            )
            self._wait_for_idle(
                serial_connection,
                timeout=self._idle_timeout_for_distance(
                    move_distance, effective_feedrate
                ),
            )

    @staticmethod
    def _move_distance_for_timeout(move: MoveVector) -> float:
        return math.sqrt(sum(value * value for _axis, value in move.items()))

    def _idle_timeout_for_distance(self, distance: float, feedrate: float) -> float:
        """Return an idle wait timeout long enough for slow manual G1 moves."""

        try:
            distance_value = abs(float(distance))
            feedrate_value = max(1.0, float(feedrate))
        except (TypeError, ValueError):
            return self.MOVE_IDLE_TIMEOUT_MIN_S
        travel_time_s = (distance_value / feedrate_value) * 60.0
        timeout = travel_time_s + self.MOVE_IDLE_TIMEOUT_MARGIN_S
        return min(
            self.MOVE_IDLE_TIMEOUT_MAX_S,
            max(self.MOVE_IDLE_TIMEOUT_MIN_S, timeout),
        )

    def _write_relative_g1_unchecked(
        self,
        serial_connection: serial.Serial,
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
            self.DEFAULT_FEEDRATE if feedrate is None else max(1.0, float(feedrate))
        )
        self._write_command(
            serial_connection,
            "G1 " + " ".join(move_parts) + f" F{effective_feedrate:.0f}",
        )
        self._wait_for_ok(serial_connection)

    def _check_relative_move_limits(
        self,
        serial_connection: serial.Serial,
        move: MoveVector,
        *,
        allow_relative: bool = False,
    ) -> None:
        if not self._axis_limits and abs(move.b) < 1e-6:
            return
        status = self._query_status(serial_connection)
        positions = self._position_for_configured_mode(status)
        if status is None or not positions:
            return
        self._ensure_b_axis_zero_reference(status)
        requested_axes = {
            axis for axis, delta in move.items() if abs(delta) >= 1e-6
        }
        if requested_axes:
            self._require_homed_axes(
                status, requested_axes, allow_relative=allow_relative
            )
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
            min_value, max_value = limits
            target = positions[idx] + delta
            if target < min_value or target > max_value:
                raise StageControllerError(
                    f"{axis} move {delta:+.3f} exceeds limits ({min_value:.3f}, {max_value:.3f})."
                )

    def _move_safety_check(self) -> None:
        """Validate motion safety prerequisites before any move."""

        if self._motion_safety_disabled:
            return
        if not self._needles_known:
            raise AxisStateError("Needle position unknown. Home/raise A before moving.")
        if not self._needles_up:
            raise AxisStateError("Needles are down. Raise A before moving.")

    @staticmethod
    def _desired_status_report_mask_for_mode(position_mode: str) -> int:
        # FluidNC RtStatus::Position bit selects MPos; without it reports WPos.
        # Keep buffer reporting enabled in both modes.
        return 3 if position_mode.strip().lower() == "machine" else 2

    @staticmethod
    def _parse_controller_coordinate_offsets(
        raw_offsets: object,
    ) -> dict[str, tuple[float, ...]]:
        offsets: dict[str, tuple[float, ...]] = {}
        if not isinstance(raw_offsets, dict):
            return offsets
        for system_raw, values_raw in raw_offsets.items():
            if not isinstance(system_raw, str):
                continue
            if not isinstance(values_raw, (list, tuple)):
                continue
            system = system_raw.strip().upper()
            try:
                values = tuple(float(value) for value in values_raw)
            except (TypeError, ValueError):
                continue
            if values:
                offsets[system] = values
        return offsets

    def _ensure_status_report_mask(self, serial_connection: serial.Serial, mask: int) -> None:
        if self._current_status_report_mask == mask:
            return
        self._write_command(serial_connection, f"$10={int(mask)}")
        self._wait_for_ok(serial_connection)
        self._current_status_report_mask = int(mask)

    def _read_response_lines(
        self,
        serial_connection: serial.Serial,
        *,
        timeout: float,
        description: str,
    ) -> list[str]:
        deadline = time.monotonic() + timeout
        lines: list[str] = []
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                raw = serial_connection.readline()
            except serial.SerialException as exc:  # pragma: no cover - hardware interaction
                raise StageControllerError(f"Serial read failed: {exc}") from exc
            line = raw.decode("ascii", errors="ignore").strip()
            if not line:
                continue
            logger.debug("SERIAL TRACE stage_readline %s line=%r", description, line)
            homed_msg = self.HOMED_MSG_PATTERN.match(line)
            if homed_msg:
                axes = set(homed_msg.group("axes").upper())
                if self._homed_axes:
                    axes = set(self._homed_axes).union(axes)
                self._update_homing_status(axes)
                continue
            lower = line.lower()
            if lower == "ok":
                return lines
            if lower.startswith("alarm"):
                raise StageControllerError(f"Controller alarm: {line}")
            if lower.startswith("error") or line.startswith("[MSG:ERR:"):
                raise StageControllerError(f"Controller reported: {line}")
            lines.append(line)
        raise StageControllerError(
            f"Timeout waiting for controller response: {description}."
        )

    def _query_active_coordinate_system(
        self, serial_connection: serial.Serial, timeout: float = 2.0
    ) -> str | None:
        self._write_command(serial_connection, "$G")
        lines = self._read_response_lines(
            serial_connection, timeout=timeout, description="$G"
        )
        for line in lines:
            modal_match = self.MODAL_STATE_PATTERN.match(line)
            if not modal_match:
                continue
            tokens = modal_match.group("modal").split()
            for token in tokens:
                candidate = token.strip().upper()
                if candidate in self.WORK_COORDINATE_SYSTEMS:
                    return candidate
        return None

    def _query_work_coordinate_offsets(
        self, serial_connection: serial.Serial, timeout: float = 2.5
    ) -> dict[str, tuple[float, ...]]:
        self._write_command(serial_connection, "$#")
        lines = self._read_response_lines(
            serial_connection, timeout=timeout, description="$#"
        )
        offsets: dict[str, tuple[float, ...]] = {}
        for line in lines:
            match = self.COORDINATE_OFFSET_PATTERN.match(line)
            if not match:
                continue
            coords = self._parse_float_tuple(match.group("coords"))
            if coords is None:
                continue
            offsets[match.group("system").upper()] = coords
        return offsets

    def _refresh_coordinate_system_state(
        self,
        serial_connection: serial.Serial,
        *,
        apply_preference: bool,
    ) -> None:
        desired_mask = self._desired_status_report_mask_for_mode(
            self._position_reporting_mode
        )
        self._ensure_status_report_mask(serial_connection, desired_mask)
        if (
            self._position_reporting_mode != "machine"
            and apply_preference
            and self._coordinate_startup_mode == "fixed"
        ):
            self._write_command(serial_connection, self._preferred_work_coordinate_system)
            self._wait_for_ok(serial_connection)
        detected_system = None if self._position_reporting_mode == "machine" else None
        try:
            if self._position_reporting_mode != "machine":
                detected_system = self._query_active_coordinate_system(serial_connection)
        except StageControllerError as exc:
            logger.warning("Unable to query active coordinate system: %s", exc)
        if (
            detected_system is None
            and self._position_reporting_mode != "machine"
            and self._coordinate_startup_mode == "fixed"
        ):
            detected_system = self._preferred_work_coordinate_system
        elif detected_system is None and self._position_reporting_mode != "machine":
            detected_system = self._preferred_work_coordinate_system
        self._active_work_coordinate_system = (
            None if self._position_reporting_mode == "machine" else detected_system
        )
        if self._position_reporting_mode != "machine":
            try:
                self._controller_coordinate_offsets = self._query_work_coordinate_offsets(
                    serial_connection
                )
            except StageControllerError as exc:
                logger.warning("Unable to load work coordinate offsets: %s", exc)

    def _emit_coordinate_system_status(self, status: _Status | None) -> None:
        if self._position_reporting_mode == "machine":
            self.status_message.emit("Coordinate system: machine coordinates.")
            return
        coordinate_system = self._active_work_coordinate_system
        if not coordinate_system:
            self.status_message.emit("Coordinate system: work coordinates.")
            return
        offset = None if status is None else status.work_offset
        if offset is not None and len(offset) >= 2:
            self.status_message.emit(
                f"Coordinate system: {coordinate_system} (X={offset[0]:.3f}, Y={offset[1]:.3f})."
            )
            return
        self.status_message.emit(f"Coordinate system: {coordinate_system}.")

    def _read_startup_limits(
        self, serial_connection: serial.Serial, timeout: float = 3.5
    ) -> None:
        try:
            serial_connection.reset_input_buffer()
        except AttributeError:
            pass
        self._write_command(serial_connection, "$Startup/Show")
        deadline = time.monotonic() + timeout
        limits: dict[str, tuple[float, float]] | None = None
        lines: list[str] = []
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                raw = serial_connection.readline()
            except serial.SerialException as exc:  # pragma: no cover - hardware interaction
                raise StageControllerError(f"Serial read failed: {exc}") from exc
            line = raw.decode("ascii", errors="ignore").strip()
            if not line:
                continue
            lower = line.lower()
            lines.append(line)
            if lower == "ok":
                break
            if lower.startswith("alarm"):
                raise StageControllerError(f"Controller alarm: {line}")
            if lower.startswith("error") or line.startswith("[MSG:ERR:"):
                raise StageControllerError(f"Controller reported: {line}")
        limits = self._parse_startup_limits(lines)
        if limits:
            limits.pop("B", None)
            self._axis_limits.update(limits)

    def _ensure_axis_limits(self, serial_connection: serial.Serial) -> None:
        if not self._axis_limits:
            self._read_startup_limits(serial_connection)
        if not self._axis_limits:
            raise StageControllerError("Axis limits unavailable from startup message.")

    @staticmethod
    def _parse_startup_limits(lines: list[str]) -> dict[str, tuple[float, float]]:
        limits: dict[str, tuple[float, float]] = {}
        for line in lines:
            match = StageController.AXIS_RANGE_PATTERN.match(line)
            if not match:
                continue
            axis = match.group("axis").upper()
            try:
                min_value = float(match.group("min"))
                max_value = float(match.group("max"))
            except ValueError:
                continue
            limits[axis] = (min_value, max_value)
        return limits

    def _next_queued_write_sequence(self) -> int:
        sequence = self._queued_write_sequence
        self._queued_write_sequence += 1
        return sequence

    def _clear_pending_async_writes(self) -> None:
        while True:
            try:
                self._async_write_queue.get_nowait()
                self._async_write_queue.task_done()
            except Empty:
                break

    def _run_async_write_worker(self) -> None:
        while not self._async_write_shutdown.is_set():
            job = self._async_write_queue.get()
            try:
                if job.kind == "shutdown":
                    return
                if job.kind == "jog_command" and not self._await_current_jog_command(job):
                    continue
                serial_connection = self._serial
                if serial_connection is None or not serial_connection.is_open:
                    continue
                with self._serial_session_lock:
                    self._write_async_job(serial_connection, job)
            except StageControllerError as exc:
                self.status_message.emit(str(exc))
            finally:
                self._async_write_queue.task_done()

    def _await_current_jog_command(self, job: _QueuedSerialWrite) -> bool:
        if job.kind != "jog_command":
            return True

        deadline = time.monotonic() + self.SERIAL_JOG_COMMAND_SETTLE_S
        while time.monotonic() < deadline:
            if self._async_write_shutdown.is_set():
                return False
            if job.generation != self._queued_jog_generation:
                logger.debug(
                    "TIMING jog_command_dropped_superseded command=%s generation=%s current_generation=%s",
                    job.description,
                    job.generation,
                    self._queued_jog_generation,
                )
                return False
            remaining = deadline - time.monotonic()
            time.sleep(min(0.005, remaining))

        if job.generation != self._queued_jog_generation:
            logger.debug(
                "TIMING jog_command_dropped_superseded command=%s generation=%s current_generation=%s",
                job.description,
                job.generation,
                self._queued_jog_generation,
            )
            return False
        return True

    def _write_async_job(
        self, serial_connection: serial.Serial, job: _QueuedSerialWrite
    ) -> None:
        try:
            if job.kind == "jog_command":
                logger.debug("TIMING jog_serial_write_begin command=%s", job.description)
            elif job.kind == "jog_stop":
                logger.debug("TIMING jog_stop_write_begin command=0x85")
            elif job.kind == "soft_reset":
                logger.debug("SERIAL TRACE terminal_write %s", job.description)
            elif job.kind == "terminal":
                logger.debug("SERIAL TRACE terminal_write payload=%r", job.description)
            serial_connection.write(job.payload)
            serial_connection.flush()
            if job.kind == "jog_command":
                self._last_jog_write_timestamp = time.monotonic()
                logger.debug("TIMING jog_serial_write_flushed command=%s", job.description)
            elif job.kind == "jog_stop":
                logger.debug("TIMING jog_stop_write_flushed command=0x85")
        except serial.SerialException as exc:  # pragma: no cover - hardware interaction
            raise StageControllerError(f"Serial write failed: {exc}") from exc

    def _write_command(self, serial_connection: serial.Serial, command: str) -> None:
        self._check_cancelled()
        data = (command.strip() + "\n").encode("ascii")
        try:
            logger.debug("SERIAL TRACE stage_write command=%s", command.strip())
            serial_connection.write(data)
            serial_connection.flush()
            logger.debug("SERIAL TRACE stage_write_flushed command=%s", command.strip())
        except serial.SerialException as exc:  # pragma: no cover - hardware interaction
            raise StageControllerError(f"Serial write failed: {exc}") from exc

    def _wait_for_ok(self, serial_connection: serial.Serial, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                raw = serial_connection.readline()
            except serial.SerialException as exc:  # pragma: no cover - hardware interaction
                raise StageControllerError(f"Serial read failed: {exc}") from exc
            line = raw.decode("ascii", errors="ignore").strip()
            if not line:
                continue
            logger.debug("SERIAL TRACE stage_readline wait_for_ok line=%r", line)
            homed_msg = self.HOMED_MSG_PATTERN.match(line)
            if homed_msg:
                axes = set(homed_msg.group("axes").upper())
                if self._homed_axes:
                    axes = set(self._homed_axes).union(axes)
                self._update_homing_status(axes)
                continue
            if line.lower() == "ok":
                return
            if line.lower().startswith("alarm"):
                raise StageControllerError(f"Controller alarm: {line}")
            if line.startswith("[MSG:ERR:"):
                raise StageControllerError(f"Controller reported: {line}")
            if line.lower().startswith("error"):
                raise StageControllerError(f"Controller reported: {line}")
        raise StageControllerError("Timeout waiting for controller acknowledgement.")

    def _wait_for_idle(self, serial_connection: serial.Serial, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            status = self._query_status(serial_connection)
            logger.debug(
                "SERIAL TRACE wait_for_idle status=%s position=%s",
                None if status is None else status.state,
                None if status is None else status.display_position,
            )
            if status and status.state.lower() == "idle":
                return
            if status and status.state.lower() == "alarm":
                raise StageControllerError("Controller entered ALARM state.")
            time.sleep(0.1)
        raise StageControllerError("Controller did not return to IDLE state in time.")

    def _query_status(self, serial_connection: serial.Serial, timeout: float = 1.5) -> Optional[_Status]:
        desired_mask = self._desired_status_report_mask_for_mode(
            self._position_reporting_mode
        )
        self._ensure_status_report_mask(serial_connection, desired_mask)
        status = self._read_status_frame(serial_connection, timeout=timeout)
        if status is None:
            return None
        if self._position_reporting_mode == "work":
            status.display_position = status.work_position
        else:
            status.display_position = status.position
        self._last_stage_state = status.state
        self._last_status_timestamp = time.monotonic()
        self._controller_state_stale = False
        self._update_cached_positions(status)
        self._update_needles_from_status(status)
        self._ensure_b_axis_zero_reference(status)
        return status

    def _read_status_frame(
        self, serial_connection: serial.Serial, *, timeout: float
    ) -> Optional[_Status]:
        try:
            logger.debug("SERIAL TRACE stage_query_status write=?")
            serial_connection.write(b"?\n")
            serial_connection.flush()
            logger.debug("SERIAL TRACE stage_query_status flushed=?")
        except serial.SerialException as exc:  # pragma: no cover - hardware interaction
            raise StageControllerError(f"Serial query failed: {exc}") from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_cancelled()
            try:
                raw = serial_connection.readline()
            except serial.SerialException as exc:  # pragma: no cover - hardware interaction
                raise StageControllerError(f"Serial read failed: {exc}") from exc
            line = raw.decode("ascii", errors="ignore").strip()
            if not line:
                continue
            logger.debug("SERIAL TRACE stage_readline query_status line=%r", line)
            if line.lower().startswith("alarm"):
                raise StageControllerError(f"Controller alarm: {line}")
            homed_msg = self.HOMED_MSG_PATTERN.match(line)
            if homed_msg:
                axes = set(homed_msg.group("axes").upper())
                if self._homed_axes:
                    axes = set(self._homed_axes).union(axes)
                self._update_homing_status(axes)
                continue
            status = self._parse_status_line(line)
            if status is None:
                continue
            homed_match = self.HOMED_PATTERN.search(line)
            if homed_match:
                status.homed_axes = set(homed_match.group(1).upper())
                self._update_homing_status(status.homed_axes)
            return status
        return None

    def _parse_status_line(self, line: str) -> Optional[_Status]:
        match = self.STATUS_PATTERN.search(line)
        if not match:
            return None
        body = match.group("body")
        parts = body.split("|")
        if not parts:
            return None
        state = parts[0].strip()
        if not state:
            return None

        machine_position: tuple[float, ...] | None = None
        work_position: tuple[float, ...] | None = None
        work_offset: tuple[float, ...] | None = None
        for part in parts[1:]:
            field_match = self.STATUS_FIELD_PATTERN.match(part)
            if not field_match:
                continue
            key = field_match.group("key")
            value = field_match.group("value")
            if key == "MPos" and self._position_reporting_mode == "machine":
                machine_position = self._parse_float_tuple(value)
            elif key == "WPos" and self._position_reporting_mode != "machine":
                work_position = self._parse_float_tuple(value)
            elif key == "WCO":
                work_offset = self._parse_float_tuple(value)

        if machine_position is not None and len(machine_position) < 3:
            return None
        if work_position is not None and len(work_position) < 3:
            return None
        if work_offset is not None and len(work_offset) < 3:
            return None
        if self._position_reporting_mode == "machine" and machine_position is None:
            return None
        if self._position_reporting_mode != "machine" and work_position is None:
            return None

        coordinate_system = self._active_work_coordinate_system
        if self._position_reporting_mode != "machine":
            if (
                work_offset is None
                and coordinate_system
                and coordinate_system in self._controller_coordinate_offsets
            ):
                work_offset = self._controller_coordinate_offsets.get(coordinate_system)
        else:
            coordinate_system = None

        return _Status(
            state=state,
            position=machine_position,
            display_position=(
                machine_position
                if self._position_reporting_mode == "machine"
                else work_position
            ),
            work_position=work_position,
            work_offset=work_offset,
            coordinate_system=coordinate_system,
        )

    @staticmethod
    def _parse_float_tuple(raw: str) -> tuple[float, ...] | None:
        try:
            values = tuple(float(part) for part in raw.split(","))
        except ValueError:
            return None
        return values if values else None

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
        if status is None:
            return None
        return (
            status.position
            if self._position_reporting_mode == "machine"
            else status.work_position
        )

    def _axis_value_for_configured_mode(
        self, status: _Status | None, axis: str
    ) -> float | None:
        position = self._position_for_configured_mode(status)
        if position is None:
            return None
        idx = self.AXIS_INDEX.get(axis.upper())
        if idx is None or idx >= len(position):
            return None
        return float(position[idx])

    def _axis_limits_for_configured_mode(
        self, axis: str, status: _Status | None
    ) -> tuple[float, float] | None:
        limits = self._axis_limits.get(axis)
        if not limits:
            return None
        if self._position_reporting_mode == "machine":
            return limits
        idx = self.AXIS_INDEX.get(axis.upper())
        if idx is None:
            return limits
        work_offset = None if status is None else status.work_offset
        if work_offset is None and self._active_work_coordinate_system:
            work_offset = self._controller_coordinate_offsets.get(
                self._active_work_coordinate_system
            )
        if work_offset is None or idx >= len(work_offset):
            return None
        min_value, max_value = limits
        offset = float(work_offset[idx])
        return (float(min_value) - offset, float(max_value) - offset)

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

    def _update_axis_a_ready(self, ready: bool) -> None:
        if ready == self._axis_a_ready:
            return
        self._axis_a_ready = ready
        self.axis_a_ready_changed.emit(ready)

    def _refresh_axis_a_ready_from_state(self) -> None:
        ready = (
            self._needles_up
            and self._needles_known
            and not self._controller_state_stale
            and self._serial is not None
            and self._serial.is_open
        )
        self._update_axis_a_ready(bool(ready))

    def _set_needles_state(self, raised: bool, *, known: bool) -> None:
        if self._needles_up == raised and self._needles_known == known:
            self._refresh_axis_a_ready_from_state()
            return
        self._needles_up = raised
        self._needles_known = known
        self._refresh_axis_a_ready_from_state()
        self.needles_state_changed.emit(raised, known)

    def _update_needles_from_a_position(self, a_position: float) -> None:
        """Update the coarse needles state using the current A coordinate."""

        self.needle_height_changed.emit(a_position)
        self._set_needles_state(abs(a_position) <= self.A_ZERO_TOLERANCE, known=True)

    def _update_needles_from_status(self, status: _Status) -> None:
        """Update needle state only when A homing is actually known."""

        a_position = self._axis_value_for_configured_mode(status, "A")
        if a_position is None:
            return

        self.needle_height_changed.emit(a_position)

        effective_homed = status.homed_axes
        if effective_homed is None and self._homed_axes:
            effective_homed = set(self._homed_axes)
        if effective_homed is None or "A" not in effective_homed:
            self._set_needles_state(False, known=False)
            return

        self._set_needles_state(abs(a_position) <= self.A_ZERO_TOLERANCE, known=True)

    def _read_current_a_position(
        self, serial_connection: serial.Serial
    ) -> Optional[float]:
        """Read the current A coordinate from the configured controller report mode."""

        status = self._query_status(serial_connection)
        if status is None:
            self._record_a_position_read_failure(
                "status query returned no complete status frame"
            )
            return None
        position = self._position_for_configured_mode(status)
        mode_name = "machine" if self._position_reporting_mode == "machine" else "work"
        if position is None:
            self._record_a_position_read_failure(
                f"status has no {mode_name} position: "
                f"state={status.state!r}, display_position={status.display_position!r}, "
                f"work_position={status.work_position!r}, work_offset={status.work_offset!r}, "
                f"coordinate_system={status.coordinate_system!r}"
            )
            return None
        idx = self.AXIS_INDEX.get("A")
        if idx is None or idx >= len(position):
            self._record_a_position_read_failure(
                f"{mode_name} position does not include A axis: "
                f"axis_index={idx!r}, position={position!r}, state={status.state!r}"
            )
            return None
        a_position = float(position[idx])
        self._last_a_position_read_failure = None
        logger.debug(
            "A position read succeeded: A=%.6f, mode=%s, state=%s, position=%r, "
            "homed_axes=%r, coordinate_system=%r",
            a_position,
            mode_name,
            status.state,
            position,
            status.homed_axes,
            status.coordinate_system,
        )
        return a_position

    def _record_a_position_read_failure(self, reason: str) -> None:
        self._last_a_position_read_failure = reason
        logger.debug("A position read failed: %s", reason)

    def _perform_home_command(
        self, serial_connection: serial.Serial, command: str
    ) -> None:
        """Execute a homing command while the caller owns serial access."""

        self.status_message.emit(f"Homing: {command}")
        self._write_command(serial_connection, command)
        self._wait_for_ok(serial_connection, timeout=30.0)
        self._wait_for_idle(serial_connection, timeout=30.0)
        if command.upper() in ("$H", "$HA"):
            axes = set(self._homed_axes)
            if command.upper() == "$H":
                axes.update({"X", "Y", "Z", "A"})
            else:
                axes.add("A")
            self._update_homing_status(axes)
            self._controller_state_stale = False
            self._set_needles_state(True, known=True)

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
        serial_connection: serial.Serial,
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
            self._apply_pending_oscillation_needles_actions(serial_connection)
            remaining = target_offset - current_offset
            if abs(remaining) < 1e-6:
                direction *= -1.0
                target_offset = amplitude_mm * direction
                continue
            step = float(np.sign(remaining)) * min(abs(remaining), segment_length)
            self._write_relative_g1_unchecked(
                serial_connection,
                self._move_vector_for_axis(axis, step),
                feedrate=feedrate,
            )
            current_offset += step

    def _run_spiral_pattern(
        self,
        serial_connection: serial.Serial,
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
            self._apply_pending_oscillation_needles_actions(serial_connection)
            phase += phase_step
            radius = amplitude_mm * 0.5 * (1.0 - float(np.cos(phase)))
            angle = start_angle + (2.0 * turns_per_sweep * phase)
            next_x = radius * float(np.cos(angle))
            next_y = radius * float(np.sin(angle))
            self._write_relative_g1_unchecked(
                serial_connection,
                MoveVector(x=next_x - last_x, y=next_y - last_y),
                feedrate=feedrate,
            )
            last_x = next_x
            last_y = next_y

    def _apply_pending_oscillation_needles_actions(
        self, serial_connection: serial.Serial
    ) -> None:
        """Apply queued A-axis actions inline while oscillation continues."""

        pending: list[tuple[str, float | None]] = []
        with self._task_lock:
            while self._oscillation_needles_actions:
                pending.append(self._oscillation_needles_actions.popleft())
        for action, step_mm in pending:
            self._execute_oscillation_needles_action(
                serial_connection,
                action,
                step_mm,
            )

    def _execute_oscillation_needles_action(
        self,
        serial_connection: serial.Serial,
        action: str,
        step_mm: float | None,
    ) -> None:
        """Execute an A-axis move inline in G91 during oscillation."""

        try:
            current_a = self._latest_known_a_position()
            if current_a is None:
                raise StageControllerError(
                    "A axis position is unknown; cannot adjust needles during oscillation."
                )
            if action == "raise":
                delta = -current_a
                if abs(delta) < 1e-6:
                    self._update_needles_from_a_position(0.0)
                    self.needles_action_finished.emit(
                        True,
                        "Needles already raised.",
                        action,
                    )
                    return
            elif action == "lower":
                if self._needle_down_offset is None:
                    raise StageControllerError(
                        "Needle down calibration missing; cannot lower."
                    )
                delta = float(self._needle_down_offset) - current_a
                if abs(delta) < 1e-6:
                    self._update_needles_from_a_position(float(self._needle_down_offset))
                    self.needles_action_finished.emit(
                        True,
                        "Needles already lowered.",
                        action,
                    )
                    return
            elif action == "adjust":
                delta = 0.0 if step_mm is None else float(step_mm)
                if abs(delta) < 1e-6:
                    self.needles_action_finished.emit(
                        True,
                        "Needle position unchanged.",
                        action,
                    )
                    return
            else:
                raise StageControllerError(f"Unknown needle action: {action}.")

            self._write_relative_g1_unchecked(
                serial_connection,
                MoveVector(a=delta),
                feedrate=self.DEFAULT_FEEDRATE,
            )
            new_a = current_a + delta
            self._update_cached_axis_position("A", new_a)
            self._update_needles_from_a_position(new_a)
            if action == "adjust":
                direction = "lowered" if delta < 0 else "raised"
                message = f"Needles {direction} by {abs(delta):.3f} mm."
            elif action == "raise":
                message = "Needles raised."
            else:
                message = "Needles lowered."
            self.needles_action_finished.emit(True, message, action)
        except StageControllerError as exc:
            self.needles_action_finished.emit(False, str(exc), action)

    def _ensure_oscillation_limits(
        self, axis: str, center_position: float, amplitude_mm: float
    ) -> None:
        """Validate that the oscillation window stays inside the known soft limits."""

        limits = self._axis_limits.get(axis)
        if not limits:
            return
        min_value, max_value = limits
        lower = center_position - amplitude_mm
        upper = center_position + amplitude_mm
        if lower < min_value or upper > max_value:
            raise StageControllerError(
                f"Oscillation window for {axis} exceeds limits "
                f"({min_value:.3f}, {max_value:.3f})."
            )

    def _require_homed_axes(
        self, status: _Status, axes: set[str], *, allow_relative: bool = False
    ) -> None:
        effective_homed = status.homed_axes
        if effective_homed is None and self._homed_axes:
            effective_homed = set(self._homed_axes)
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

    def _ensure_axis_a_zero(
        self,
        serial_connection: serial.Serial,
        *,
        allow_missing_homing: bool = False,
        allow_relative: bool = False,
    ) -> None:
        status = self._query_status(serial_connection)
        a_position = self._axis_value_for_configured_mode(status, "A")
        if status is None or a_position is None:
            raise AxisStateError("Unable to read A axis position.")
        if not allow_missing_homing:
            self._require_homed_axes(status, {"A"}, allow_relative=allow_relative)
        if abs(a_position) > self.A_ZERO_TOLERANCE:
            raise AxisStateError(f"A axis not at zero (A={a_position:.3f}).")

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
        a = frame_a.astype(np.float32)
        b = frame_b.astype(np.float32)
        window = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
        (shift_x, shift_y), _ = cv2.phaseCorrelate(a, b, window)
        return float(shift_x), float(-shift_y)

    @staticmethod
    def _focus_metric(frame: np.ndarray) -> float:
        lap = cv2.Laplacian(frame, cv2.CV_64F)
        return float(lap.var())

    def _check_cancelled(self) -> None:
        if self._cancel_event.is_set():
            raise StageControllerError("Operation cancelled.")

    def _queue_needles_action_locked(
        self, action: str, step_mm: float | None = None
    ) -> None:
        """Queue a needle command so it runs immediately after oscillation stops."""

        self._queued_needles_actions.append((action, step_mm))
        self._cancel_event.set()
        if action == "raise":
            label = "Needle raise"
        elif action == "lower":
            label = "Needle lower"
        else:
            direction = "lower" if (step_mm or 0.0) < 0 else "raise"
            label = f"Needle {direction} step"
        self.status_message.emit(f"{label} queued with priority. Stopping oscillation.")

    def _queue_oscillation_needles_action_locked(
        self, action: str, step_mm: float | None = None
    ) -> None:
        """Queue an A-axis move to be injected into the running oscillation."""

        self._oscillation_needles_actions.append((action, step_mm))
        self.needles_action_started.emit(action)
        if action == "raise":
            label = "Needle raise"
        elif action == "lower":
            label = "Needle lower"
        else:
            direction = "lower" if (step_mm or 0.0) < 0 else "raise"
            label = f"Needle {direction} step"
        self.status_message.emit(f"{label} queued during oscillation.")

    def _start_needles_action_locked(
        self, action: str, step_mm: float | None = None
    ) -> None:
        """Start a needle action while the caller owns the task lock."""

        self._cancel_event.clear()
        if action == "adjust":
            if step_mm is None:
                step_mm = 0.0
            thread = threading.Thread(
                target=self._run_needles_adjust,
                args=(float(step_mm),),
                daemon=True,
            )
            self._active_thread = thread
            self.needles_action_started.emit("adjust")
            thread.start()
            return
        thread = threading.Thread(
            target=self._run_needles_action,
            args=(action,),
            daemon=True,
        )
        self._active_thread = thread
        thread.start()

    def _start_next_queued_needles_action(self) -> None:
        """Run the next queued needle action after oscillation yields the controller."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                return
            if not self._queued_needles_actions:
                return
            action, step_mm = self._queued_needles_actions.popleft()
            self._start_needles_action_locked(action, step_mm)

    def _latest_known_a_position(self) -> float | None:
        """Return the best available cached A-axis coordinate."""

        if self._position_reporting_mode != "machine":
            if self._last_stage_position is not None and len(self._last_stage_position) > 3:
                return float(self._last_stage_position[3])
            return None
        if self._last_machine_position is not None and len(self._last_machine_position) > 3:
            return float(self._last_machine_position[3])
        return None

    def _update_cached_axis_position(self, axis: str, value: float) -> None:
        """Update cached stage coordinates after an inline single-axis move."""

        index = self.AXIS_INDEX.get(axis.upper())
        if index is None:
            return
        if (
            self._position_reporting_mode == "machine"
            and self._last_machine_position is not None
            and len(self._last_machine_position) > index
        ):
            machine = list(self._last_machine_position)
            machine[index] = float(value)
            self._last_machine_position = tuple(machine)
        if self._last_stage_position is not None and len(self._last_stage_position) > index:
            stage = list(self._last_stage_position)
            stage[index] = float(value)
            coords = tuple(stage)
            self._last_stage_position = coords
            self.stage_position_changed.emit(coords)

    def _update_homing_status(self, homed_axes: set[str]) -> None:
        if homed_axes == self._homed_axes:
            return
        self._homed_axes = set(homed_axes)
        self.homing_status_changed.emit(set(self._homed_axes))

    @staticmethod
    def _qimage_to_gray(image: QImage) -> np.ndarray:
        converted = image.convertToFormat(QImage.Format_RGB888)
        width = converted.width()
        height = converted.height()
        ptr = converted.constBits()
        array = np.frombuffer(
            ptr, np.uint8, count=converted.sizeInBytes()
        ).reshape((height, converted.bytesPerLine()))
        array = array[:, : width * 3].reshape((height, width, 3))
        gray = cv2.cvtColor(array, cv2.COLOR_RGB2GRAY)
        return gray


__all__ = ["StageController", "MoveVector"]
