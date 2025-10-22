"""Stage controller coordinating calibration and click-to-move actions."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
import serial
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QImage


class StageControllerError(RuntimeError):
    """Raised when the stage controller cannot complete an operation."""


@dataclass
class _Status:
    state: str
    position: Optional[tuple[float, float, float]] = None


class StageController(QObject):
    """Translate mouse clicks into stage movements via serial commands."""

    calibration_changed: Signal = Signal(float, float)
    movement_started: Signal = Signal()
    movement_finished: Signal = Signal(bool, str)
    status_message: Signal = Signal(str)

    CALIBRATION_PIXEL_TARGET = 120.0
    CALIBRATION_MIN_VERIFY_PIXELS = 15.0
    CALIBRATION_STEP_MM = 0.2
    CALIBRATION_MAX_STEPS = 25
    DEFAULT_FEEDRATE = 600.0

    STATUS_PATTERN = re.compile(
        r"<(?P<state>[A-Za-z]+)(?:\|[^>]*?MPos:(?P<mpos>-?\d+\.?\d*,-?\d+\.?\d*,-?\d+\.?\d*))?"
    )

    def __init__(self) -> None:
        super().__init__()
        self._serial: Optional[serial.Serial] = None
        self._mm_per_pixel_x: Optional[float] = None
        self._mm_per_pixel_y: Optional[float] = None
        self._axis_sign_x: float = 1.0
        self._axis_sign_y: float = 1.0
        self._latest_frame: Optional[np.ndarray] = None
        self._frame_counter = 0
        self._frame_condition = threading.Condition()
        self._task_lock = threading.Lock()
        self._active_thread: Optional[threading.Thread] = None

    def set_serial(self, serial_connection: Optional[serial.Serial]) -> None:
        """Assign or clear the serial connection used for stage control."""

        with self._task_lock:
            self._serial = serial_connection
            if serial_connection is None or not serial_connection.is_open:
                self._mm_per_pixel_x = None
                self._mm_per_pixel_y = None
                self._axis_sign_x = 1.0
                self._axis_sign_y = 1.0

    def shutdown(self) -> None:
        """Stop any outstanding background task before application exit."""

        with self._task_lock:
            thread = self._active_thread
        if thread and thread.is_alive():
            thread.join(timeout=2.0)

    def on_frame_ready(self, frame: QImage) -> None:
        """Receive camera frames and cache them as grayscale numpy arrays."""

        gray = self._qimage_to_gray(frame)
        with self._frame_condition:
            self._latest_frame = gray
            self._frame_counter += 1
            self._frame_condition.notify_all()

    def request_move(self, dx_pixels: float, dy_pixels: float) -> None:
        """Begin an asynchronous move so the clicked point aligns with the cross."""

        with self._task_lock:
            if self._active_thread and self._active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring the new click.")
                return
            thread = threading.Thread(
                target=self._run_move,
                args=(dx_pixels, dy_pixels),
                daemon=True,
            )
            self._active_thread = thread
            thread.start()

    def _run_move(self, dx_pixels: float, dy_pixels: float) -> None:
        self.movement_started.emit()
        try:
            serial_connection = self._serial
            if serial_connection is None or not serial_connection.is_open:
                raise StageControllerError("Serial connection is not available.")

            self.status_message.emit("Ensuring calibration before movement…")
            self._ensure_calibration(serial_connection)
            if self._mm_per_pixel_x is None or self._mm_per_pixel_y is None:
                raise StageControllerError("Calibration failed. Cannot move stage.")

            if abs(dx_pixels) < 1e-3 and abs(dy_pixels) < 1e-3:
                self.movement_finished.emit(True, "Target already centered.")
                return

            before_frame, before_counter = self._get_frame_snapshot()
            if before_frame is None:
                raise StageControllerError("Camera frame unavailable before movement.")
            move_x_mm = dx_pixels * self._mm_per_pixel_x * self._axis_sign_x
            move_y_mm = dy_pixels * self._mm_per_pixel_y * self._axis_sign_y

            self.status_message.emit(
                f"Jogging stage ΔX={move_x_mm:.3f} mm ΔY={move_y_mm:.3f} mm"
            )
            self._send_relative_move(serial_connection, move_x_mm, move_y_mm)
            after_frame, _ = self._wait_for_new_frame(before_counter, timeout=4.0)
            if after_frame is None:
                self.movement_finished.emit(
                    False,
                    "Movement command sent but camera did not provide an updated frame.",
                )
                return

            shift_x, shift_y = self._estimate_shift(before_frame, after_frame)
            message = self._update_calibration_from_measurement(
                dx_pixels, dy_pixels, shift_x, shift_y
            )

            self.movement_finished.emit(True, message)
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                self._active_thread = None

    def _ensure_calibration(self, serial_connection: serial.Serial) -> None:
        if self._mm_per_pixel_x is not None and self._mm_per_pixel_y is not None:
            return
        self.status_message.emit("Starting calibration sequence…")
        before_frame, _ = self._get_frame_snapshot(timeout=3.0)
        if before_frame is None:
            raise StageControllerError("Camera frames are unavailable for calibration.")

        start_status = self._query_status(serial_connection)
        if start_status is None or start_status.position is None:
            raise StageControllerError("Unable to read machine position for calibration.")

        origin = start_status.position
        try:
            mm_per_pixel_x, sign_x = self._calibrate_axis(
                serial_connection, before_frame, origin, axis="X"
            )
            latest_frame, _ = self._get_frame_snapshot(timeout=2.0)
            reference_for_y = latest_frame if latest_frame is not None else before_frame
            mm_per_pixel_y, sign_y = self._calibrate_axis(
                serial_connection, reference_for_y, origin, axis="Y"
            )
        finally:
            self._return_to_origin(serial_connection, origin)

        self._mm_per_pixel_x = mm_per_pixel_x
        self._mm_per_pixel_y = mm_per_pixel_y
        self._axis_sign_x = sign_x
        self._axis_sign_y = sign_y
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
    ) -> tuple[float, float]:
        if reference_frame is None:
            raise StageControllerError("Reference frame unavailable for calibration.")
        index = 0 if axis == "X" else 1
        total_mm = 0.0
        with self._frame_condition:
            frame_counter = self._frame_counter
        for _ in range(self.CALIBRATION_MAX_STEPS):
            step_x = self.CALIBRATION_STEP_MM if axis == "X" else 0.0
            step_y = self.CALIBRATION_STEP_MM if axis == "Y" else 0.0
            self._send_relative_move(serial_connection, step_x, step_y)
            new_frame, frame_counter = self._wait_for_new_frame(frame_counter, timeout=2.0)
            if new_frame is None:
                raise StageControllerError("Camera did not update during calibration.")
            status = self._query_status(serial_connection)
            if status is None or status.position is None:
                raise StageControllerError("Unable to query position during calibration.")
            current = status.position
            total_mm = current[index] - origin[index]
            shift_x, shift_y = self._estimate_shift(reference_frame, new_frame)
            axis_shift = shift_x if axis == "X" else shift_y
            if abs(axis_shift) >= self.CALIBRATION_PIXEL_TARGET:
                break

        if abs(total_mm) < 1e-6:
            raise StageControllerError("Detected zero movement while calibrating.")
        if axis == "X":
            denominator = shift_x
        else:
            denominator = shift_y
        if abs(denominator) < 1e-6:
            raise StageControllerError("Pixel shift too small to compute calibration.")
        ratio = total_mm / denominator
        sign = 1.0 if ratio >= 0 else -1.0
        return abs(ratio), sign

    def _return_to_origin(
        self, serial_connection: serial.Serial, origin: tuple[float, float, float]
    ) -> None:
        status = self._query_status(serial_connection)
        if status is None or status.position is None:
            return
        current = status.position
        delta_x = origin[0] - current[0]
        delta_y = origin[1] - current[1]
        if abs(delta_x) < 1e-5 and abs(delta_y) < 1e-5:
            return
        self.status_message.emit("Returning stage to calibration origin…")
        self._send_relative_move(serial_connection, delta_x, delta_y)

    def _update_calibration_from_measurement(
        self,
        expected_dx: float,
        expected_dy: float,
        measured_dx: float,
        measured_dy: float,
    ) -> str:
        message = "Move complete."
        if self._mm_per_pixel_x is not None and abs(expected_dx) >= self.CALIBRATION_MIN_VERIFY_PIXELS:
            if abs(measured_dx) > 1e-6:
                effective_dx = measured_dx * self._axis_sign_x
                if expected_dx * effective_dx < 0:
                    self._axis_sign_x *= -1.0
                    effective_dx = measured_dx * self._axis_sign_x
                if abs(effective_dx) > 1e-6:
                    ratio_x = abs(expected_dx) / abs(effective_dx)
                    self._mm_per_pixel_x *= ratio_x
                    self._mm_per_pixel_x = abs(self._mm_per_pixel_x)
                    message += f" Cal X adjusted ×{ratio_x:.3f}."
        if self._mm_per_pixel_y is not None and abs(expected_dy) >= self.CALIBRATION_MIN_VERIFY_PIXELS:
            if abs(measured_dy) > 1e-6:
                effective_dy = measured_dy * self._axis_sign_y
                if expected_dy * effective_dy < 0:
                    self._axis_sign_y *= -1.0
                    effective_dy = measured_dy * self._axis_sign_y
                if abs(effective_dy) > 1e-6:
                    ratio_y = abs(expected_dy) / abs(effective_dy)
                    self._mm_per_pixel_y *= ratio_y
                    self._mm_per_pixel_y = abs(self._mm_per_pixel_y)
                    message += f" Cal Y adjusted ×{ratio_y:.3f}."
        if self._mm_per_pixel_x is not None and self._mm_per_pixel_y is not None:
            self.calibration_changed.emit(self._mm_per_pixel_x, self._mm_per_pixel_y)
        return message

    def _send_relative_move(
        self, serial_connection: serial.Serial, delta_x: float, delta_y: float
    ) -> None:
        if abs(delta_x) < 1e-6 and abs(delta_y) < 1e-6:
            return
        self._write_command(serial_connection, "G21")
        self._wait_for_ok(serial_connection)
        self._write_command(serial_connection, "G91")
        self._wait_for_ok(serial_connection)
        move_parts: list[str] = []
        if abs(delta_x) >= 1e-6:
            move_parts.append(f"X{delta_x:.4f}")
        if abs(delta_y) >= 1e-6:
            move_parts.append(f"Y{delta_y:.4f}")
        move = "G1 " + " ".join(move_parts) + f" F{self.DEFAULT_FEEDRATE:.0f}"
        self._write_command(serial_connection, move)
        self._wait_for_ok(serial_connection)
        self._write_command(serial_connection, "G90")
        self._wait_for_ok(serial_connection)
        self._wait_for_idle(serial_connection)

    def _write_command(self, serial_connection: serial.Serial, command: str) -> None:
        data = (command.strip() + "\n").encode("ascii")
        try:
            serial_connection.write(data)
            serial_connection.flush()
        except serial.SerialException as exc:  # pragma: no cover - hardware interaction
            raise StageControllerError(f"Serial write failed: {exc}") from exc

    def _wait_for_ok(self, serial_connection: serial.Serial, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = serial_connection.readline()
            except serial.SerialException as exc:  # pragma: no cover - hardware interaction
                raise StageControllerError(f"Serial read failed: {exc}") from exc
            line = raw.decode("ascii", errors="ignore").strip()
            if not line:
                continue
            if line.lower() == "ok":
                return
            if line.lower().startswith("error"):
                raise StageControllerError(f"Controller reported: {line}")
        raise StageControllerError("Timeout waiting for controller acknowledgement.")

    def _wait_for_idle(self, serial_connection: serial.Serial, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = self._query_status(serial_connection)
            if status and status.state.lower() == "idle":
                return
            time.sleep(0.1)
        raise StageControllerError("Controller did not return to IDLE state in time.")

    def _query_status(self, serial_connection: serial.Serial, timeout: float = 1.5) -> Optional[_Status]:
        try:
            serial_connection.write(b"?\n")
            serial_connection.flush()
        except serial.SerialException as exc:  # pragma: no cover - hardware interaction
            raise StageControllerError(f"Serial query failed: {exc}") from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = serial_connection.readline()
            except serial.SerialException as exc:  # pragma: no cover - hardware interaction
                raise StageControllerError(f"Serial read failed: {exc}") from exc
            line = raw.decode("ascii", errors="ignore").strip()
            if not line:
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
                    if len(coords) == 3:
                        position = coords
                except ValueError:
                    position = None
            return _Status(state=state, position=position)
        return None

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
        return float(shift_x), float(shift_y)

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
        rotated = cv2.rotate(array, cv2.ROTATE_90_COUNTERCLOCKWISE)
        gray = cv2.cvtColor(rotated, cv2.COLOR_RGB2GRAY)
        return gray


__all__ = ["StageController"]
