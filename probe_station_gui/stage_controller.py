"""Stage controller coordinating calibration and click-to-move actions."""

from __future__ import annotations

import logging
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

    STATUS_PATTERN = re.compile(
        r"<(?P<state>[A-Za-z]+)(?:\|[^>]*?MPos:(?P<mpos>-?\d+\.?\d*(?:,-?\d+\.?\d*)*))?"
    )
    AXIS_RANGE_PATTERN = re.compile(
        r"^\[MSG:INFO: Axis (?P<axis>[A-Za-z]) \((?P<min>-?\d+\.?\d*),(?P<max>-?\d+\.?\d*)\)\]"
    )
    HOMED_PATTERN = re.compile(r"\|H:([A-Za-z]+)")
    HOMED_MSG_PATTERN = re.compile(r"^\[MSG:Homed:(?P<axes>[A-Za-z]+)\]")
    AXIS_INDEX = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4, "C": 5}

    def __init__(self) -> None:
        super().__init__()
        self._serial: Optional[serial.Serial] = None
        self._pixels_to_mm: Optional[np.ndarray] = None
        self._last_stage_position: Optional[tuple[float, ...]] = None
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
        self._needle_lower_direction_sign = -1.0
        self._oscillation_active = False
        self._b_axis_zero_position: Optional[float] = None
        self._serial_session_lock = threading.RLock()
        self._queued_write_sequence = 0
        self._queued_jog_generation = 0
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
                self._last_stage_position = None
                self._axis_limits.clear()
                self._b_axis_zero_position = None
                self._update_homing_status(set())
                self._set_needles_state(False, known=False)
            else:
                self._axis_limits.clear()
                self._b_axis_zero_position = None
                try:
                    with self._serial_session_lock:
                        self._ensure_axis_limits(serial_connection)
                    self.status_message.emit(
                        "Axis limits loaded from controller. B uses app soft limit ±45 deg."
                    )
                except StageControllerError as exc:
                    self._axis_limits.clear()
                    self.status_message.emit(str(exc))
                self._update_homing_status(set())
                self._set_needles_state(False, known=False)
                threading.Thread(target=self._poll_status_once, daemon=True).start()

    def check_motion_safety(self) -> None:
        """Public motion safety gate; raises StageControllerError when unsafe."""

        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            raise StageControllerError("Serial connection is not available.")
        self._move_safety_check()

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
        """Move to an absolute X/Y machine-space coordinate in the background."""

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
                self.status_message.emit("Stage is busy. Ignoring needle raise request.")
                return
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_needles_action, args=("raise",), daemon=True
            )
            self._active_thread = thread
            thread.start()

    def request_needles_lower(self) -> None:
        """Lower the needles to the calibrated down position."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring needle lower request.")
                return
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_needles_action, args=("lower",), daemon=True
            )
            self._active_thread = thread
            thread.start()

    def apply_needle_calibration(
        self,
        *,
        down_position_mm: Optional[float],
        lower_direction: str,
    ) -> None:
        """Apply the persisted needle calibration settings."""

        self._needle_down_offset = down_position_mm
        self._needle_lower_direction_sign = (
            1.0 if lower_direction.strip().lower() == "positive" else -1.0
        )

    def request_needles_adjust(self, step_mm: float) -> None:
        """Adjust the A axis for needle calibration without the XY safety gate."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring needle adjustment.")
                return
            self._cancel_event.clear()
            thread = threading.Thread(
                target=self._run_needles_adjust, args=(float(step_mm),), daemon=True
            )
            self._active_thread = thread
            self.needles_action_started.emit("adjust")
            thread.start()

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
        """Return the current machine A coordinate when it can be queried safely."""

        serial_connection = self._serial
        if serial_connection is None or not serial_connection.is_open:
            return None
        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                return None
        with self._serial_session_lock:
            a_position = self._read_current_a_position(serial_connection)
        if a_position is None:
            return None
        self.needle_height_changed.emit(a_position)
        return a_position

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
        """Return the latest controller-reported machine position."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                raise StageControllerError("Stage is busy. Wait for the current operation to finish.")
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")
            with self._serial_session_lock:
                status = self._query_status(serial_connection)
        if status is None or status.position is None:
            raise StageControllerError("Unable to read stage position.")
        if status.state.lower() in {"jog", "run"}:
            raise StageControllerError("Wait for the stage to stop before capturing a marker.")
        self._ensure_b_axis_zero_reference(status)
        return tuple(float(value) for value in status.position)

    def latest_stage_position(self) -> tuple[float, ...] | None:
        """Return the most recently observed machine position, if any."""

        if self._last_stage_position is None:
            return None
        return tuple(self._last_stage_position)

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
                if status is None or status.position is None:
                    raise StageControllerError("Unable to read stage position.")
                self._ensure_calibration(serial_connection)
        if self._pixels_to_mm is None:
            raise StageControllerError("Calibration failed. Cannot resolve clicked position.")
        return self._resolve_xy_from_center(
            tuple(float(value) for value in status.position),
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
        """Set the current B machine position as the application zero reference."""

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
        if status is None or status.position is None:
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

    def queue_soft_reset(self) -> None:
        """Queue a FluidNC soft reset without blocking the UI thread."""

        self._async_write_queue.put(
            _QueuedSerialWrite(
                priority=self.SERIAL_PRIORITY_SOFT_RESET,
                sequence=self._next_queued_write_sequence(),
                kind="soft_reset",
                payload=b"\x18",
                description="CTRL-X",
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

            self.status_message.emit("Ensuring calibration before movement…")
            self._ensure_calibration(serial_connection)
            self._check_cancelled()
            if self._pixels_to_mm is None:
                raise StageControllerError("Calibration failed. Cannot move stage.")

            if abs(dx_pixels) < 1e-3 and abs(dy_pixels) < 1e-3:
                self.movement_finished.emit(True, "Target already centered.")
                return

            before_frame, before_counter = self._get_frame_snapshot()
            if before_frame is None:
                raise StageControllerError("Camera frame unavailable before movement.")
            pixel_vector = np.array([dx_pixels, dy_pixels], dtype=float)
            # Moving the stage shifts the image in the opposite direction, so we
            # negate the calibrated conversion when turning pixel error into mm.
            mm_vector = -(self._pixels_to_mm @ pixel_vector)
            move = MoveVector(x=float(mm_vector[0]), y=float(mm_vector[1]))
            move_magnitude = float(np.linalg.norm(mm_vector))
            if move_magnitude > self.MAX_CLICK_MOVE_MM:
                self._pixels_to_mm = None
                raise StageControllerError(
                    "Predicted click move is too large; calibration was reset. Recalibrate and try again."
                )

            self.status_message.emit(
                f"Jogging stage ΔX={move.x:.3f} mm ΔY={move.y:.3f} mm"
            )
            self._send_relative_move(serial_connection, move)
            after_frame, _ = self._wait_for_new_frame(before_counter, timeout=4.0)
            if after_frame is None:
                self.movement_finished.emit(
                    False,
                    "Movement command sent but camera did not provide an updated frame.",
                )
                return

            shift_x, shift_y = self._estimate_shift(before_frame, after_frame)
            message = self._update_calibration_from_measurement(
                pixel_vector,
                np.array([shift_x, shift_y], dtype=float),
                mm_vector,
            )

            self.movement_finished.emit(True, message)
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
            status = self._query_status(serial_connection)
            if status is None or status.position is None:
                raise StageControllerError("Unable to read current stage position.")
            self._require_homed_axes(status, {"X", "Y"})
            current_x = float(status.position[0])
            current_y = float(status.position[1])
            delta_x = float(target_x_mm) - current_x
            delta_y = float(target_y_mm) - current_y
            move = MoveVector(x=delta_x, y=delta_y)
            if move.is_zero(tol=1e-5):
                self.movement_finished.emit(True, "Target already at requested X/Y.")
                return
            self.status_message.emit(
                f"Moving to X={target_x_mm:.3f} mm, Y={target_y_mm:.3f} mm"
            )
            self._send_relative_move(serial_connection, move)
            self._wait_for_idle(serial_connection)
            updated_status = self._query_status(serial_connection)
            if updated_status and updated_status.position is not None:
                self._last_stage_position = tuple(float(v) for v in updated_status.position)
                self.stage_position_changed.emit(tuple(self._last_stage_position))
            self.movement_finished.emit(
                True,
                f"Arrived at X={target_x_mm:.3f} mm, Y={target_y_mm:.3f} mm.",
            )
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                self._active_thread = None

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
            if status is None or status.position is None:
                raise StageControllerError("Unable to read Z position for autofocus.")
            self._require_homed_axes(status, {"Z"}, allow_relative=True)
            start_z = float(status.position[2])
            z_limits = self._axis_limits.get("Z")
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
            self.status_message.emit(f"Homing: {command}")
            self._write_command(serial_connection, command)
            self._wait_for_ok(serial_connection, timeout=30.0)
            self._wait_for_idle(serial_connection, timeout=30.0)
            if command.upper() in ("$H", "$HA"):
                self._set_needles_state(True, known=True)
            self.movement_finished.emit(True, "Homing complete.")
            self.homing_action_finished.emit(True, "Homing complete.", axis_key)
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
            self.homing_action_finished.emit(False, str(exc), axis_key)
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
        if start_status is None or start_status.position is None:
            raise StageControllerError("Unable to read machine position for calibration.")
        self._require_homed_axes(start_status, {"X", "Y"})

        origin = start_status.position
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
            if status is None or status.position is None:
                raise StageControllerError("Unable to query position during calibration.")
            self._require_homed_axes(status, {axis})
            current = status.position
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
        if status is None or status.position is None:
            return
        self._require_homed_axes(status, {"X", "Y"})
        current = status.position
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
                    and status.position is not None
                    and effective_homed is not None
                    and "A" in effective_homed
                ):
                    idx = self.AXIS_INDEX.get("A")
                    if idx is None or idx >= len(status.position):
                        raise StageControllerError("A axis position unavailable.")
                    current_a = float(status.position[idx])
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
                if status is None or status.position is None:
                    raise StageControllerError("Unable to read A position for needles.")
                self._require_homed_axes(status, {"A"})
                idx = self.AXIS_INDEX.get("A")
                if idx is None or idx >= len(status.position):
                    raise StageControllerError("A axis position unavailable.")
                current_a = float(status.position[idx])
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
            if status is None or status.position is None:
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
            direction = "lowered" if step_mm * self._needle_lower_direction_sign > 0 else "raised"
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
            self.status_message.emit(str(exc))
        finally:
            self._oscillation_active = False
            self.oscillation_state_changed.emit(False, mode)
            with self._task_lock:
                self._active_thread = None

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
    ) -> None:
        if move.is_zero():
            return
        if not ignore_needle_safety:
            self._move_safety_check()
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
        move = "G1 " + " ".join(move_parts) + f" F{effective_feedrate:.0f}"
        self._write_command(serial_connection, move)
        self._wait_for_ok(serial_connection)
        self._write_command(serial_connection, "G90")
        self._wait_for_ok(serial_connection)
        self._wait_for_idle(serial_connection)

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
        if status is None or not status.position:
            return
        positions = status.position
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
            limits = self._axis_limits.get(axis)
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

        if not self._needles_known:
            raise AxisStateError("Needle position unknown. Home/raise A before moving.")
        if not self._needles_up:
            raise AxisStateError("Needles are down. Raise A before moving.")

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
                if job.kind == "jog_command" and job.generation != self._queued_jog_generation:
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

    def _write_async_job(
        self, serial_connection: serial.Serial, job: _QueuedSerialWrite
    ) -> None:
        try:
            if job.kind == "jog_command":
                logger.debug("TIMING jog_serial_write_begin command=%s", job.description)
            elif job.kind == "jog_stop":
                logger.debug("TIMING jog_stop_write_begin command=0x85")
            elif job.kind == "soft_reset":
                logger.debug("SERIAL TRACE terminal_write CTRL-X")
            elif job.kind == "terminal":
                logger.debug("SERIAL TRACE terminal_write payload=%r", job.description)
            serial_connection.write(job.payload)
            serial_connection.flush()
            if job.kind == "jog_command":
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
                None if status is None else status.position,
            )
            if status and status.state.lower() == "idle":
                return
            if status and status.state.lower() == "alarm":
                raise StageControllerError("Controller entered ALARM state.")
            time.sleep(0.1)
        raise StageControllerError("Controller did not return to IDLE state in time.")

    def _query_status(self, serial_connection: serial.Serial, timeout: float = 1.5) -> Optional[_Status]:
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
            match = self.STATUS_PATTERN.search(line)
            if not match:
                continue
            state = match.group("state")
            mpos = match.group("mpos")
            position = None
            if mpos:
                try:
                    coords = tuple(float(value) for value in mpos.split(","))
                    if coords:
                        position = coords
                        self._last_stage_position = coords
                        a_idx = self.AXIS_INDEX.get("A")
                        if a_idx is not None and a_idx < len(coords):
                            self._update_needles_from_a_position(float(coords[a_idx]))
                        self.stage_position_changed.emit(tuple(coords))
                except ValueError:
                    position = None
            homed_axes = None
            homed_match = self.HOMED_PATTERN.search(line)
            if homed_match:
                homed_axes = set(homed_match.group(1).upper())
                self._update_homing_status(homed_axes)
            status = _Status(state=state, position=position, homed_axes=homed_axes)
            self._ensure_b_axis_zero_reference(status)
            return status
        return None

    def _ensure_b_axis_zero_reference(self, status: _Status) -> None:
        if self._b_axis_zero_position is not None:
            return
        if status.position is None:
            return
        self._set_b_axis_zero_reference(status, emit_status=False)

    def _set_b_axis_zero_reference(
        self, status: _Status, *, emit_status: bool = True
    ) -> None:
        if status.position is None:
            raise StageControllerError("Unable to read B axis position.")
        idx = self.AXIS_INDEX.get("B")
        if idx is None or idx >= len(status.position):
            raise StageControllerError("B axis position unavailable.")
        self._b_axis_zero_position = float(status.position[idx])
        if emit_status:
            self.status_message.emit(
                f"B zero reference set to current position ({self._b_axis_zero_position:.3f})."
            )

    def _relative_b_position(self, status: _Status) -> float:
        if status.position is None:
            raise StageControllerError("B axis position unavailable.")
        idx = self.AXIS_INDEX.get("B")
        if idx is None or idx >= len(status.position):
            raise StageControllerError("B axis position unavailable.")
        self._ensure_b_axis_zero_reference(status)
        zero = self._b_axis_zero_position
        if zero is None:
            raise StageControllerError("B zero reference is not initialized.")
        return float(status.position[idx]) - zero

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

    def _set_needles_state(self, raised: bool, *, known: bool) -> None:
        if self._needles_up == raised and self._needles_known == known:
            return
        self._needles_up = raised
        self._needles_known = known
        self._update_axis_a_ready(raised and known)
        self.needles_state_changed.emit(raised, known)

    def _update_needles_from_a_position(self, a_position: float) -> None:
        """Update the coarse needles state using the current A coordinate."""

        self.needle_height_changed.emit(a_position)
        self._set_needles_state(abs(a_position) <= self.A_ZERO_TOLERANCE, known=True)

    def _read_current_a_position(
        self, serial_connection: serial.Serial
    ) -> Optional[float]:
        """Read the current machine A coordinate from the controller."""

        status = self._query_status(serial_connection)
        if status is None or status.position is None:
            return None
        idx = self.AXIS_INDEX.get("A")
        if idx is None or idx >= len(status.position):
            return None
        return float(status.position[idx])

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
        if status is None or status.position is None:
            raise AxisStateError("Unable to read A axis position.")
        if not allow_missing_homing:
            self._require_homed_axes(status, {"A"}, allow_relative=allow_relative)
        idx = self.AXIS_INDEX.get("A")
        if idx is None or idx >= len(status.position):
            raise AxisStateError("A axis position unavailable.")
        a_position = float(status.position[idx])
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
