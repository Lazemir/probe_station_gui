"""Click-to-move and camera calibration workflow for the stage controller."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, cast

import numpy as np
from PySide6.QtGui import QImage

from probe_station_gui.stage.autofocus_math import (
    focus_metric,
    qimage_to_gray,
)
from probe_station_gui.stage.click_move_calibration import (
    _StageControllerClickCalibrationMixin,
)
from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.motion_command_planning import (
    clamped_motion_feedrate,
)
from probe_station_gui.stage.types import MoveVector


class StageControllerClickMoveMixin(_StageControllerClickCalibrationMixin):
    """Run click-to-move and objective calibration without owning serial I/O."""

    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    def on_frame_ready(self, frame: QImage) -> None:
        """Receive camera frames and cache them as grayscale numpy arrays."""

        gray = self._qimage_to_gray(frame)
        timestamp = time.monotonic()
        with self._frame_condition:
            self._latest_frame = gray
            frame_counter = int(getattr(self, "_frame_counter")) + 1
            setattr(self, "_frame_counter", frame_counter)
            self._frame_history.append((frame_counter, timestamp, gray.copy()))
            self._frame_condition.notify_all()

    def request_move(self, dx_pixels: float, dy_pixels: float) -> bool:
        """Begin an asynchronous move so the clicked point aligns with the cross."""

        return self._start_background_task(
            target=self._run_move,
            args=(dx_pixels, dy_pixels),
            busy_message="Stage is busy. Ignoring the new click.",
        )

    def request_move_to_xy(self, target_x_mm: float, target_y_mm: float) -> None:
        """Move to an absolute X/Y coordinate in the configured report mode."""

        self._start_background_task(
            target=self._run_move_to_xy,
            args=(float(target_x_mm), float(target_y_mm)),
            busy_message="Stage is busy. Ignoring absolute move request.",
        )

    def request_token_bound_move_to_xy(
        self,
        token: object,
        target_x_mm: float,
        target_y_mm: float,
        completion,
    ) -> bool:
        """Start an absolute XY move whose completion carries its exact token."""

        return self._start_background_task(
            target=self._run_token_bound_move_to_xy,
            args=(
                token,
                float(target_x_mm),
                float(target_y_mm),
                completion,
            ),
            busy_message="Stage is busy. Focus reference was not started.",
        )

    def request_move_to_xyz(
        self,
        target_x_mm: float,
        target_y_mm: float,
        target_z_mm: float,
        transit_z_mm: float | None = None,
        label: str = "saved position",
    ) -> None:
        """Move to an absolute X/Y/Z point using a safe intermediate Z level."""

        self._start_background_task(
            target=self._run_move_to_xyz,
            args=(
                float(target_x_mm),
                float(target_y_mm),
                float(target_z_mm),
                None if transit_z_mm is None else float(transit_z_mm),
                str(label),
            ),
            busy_message="Stage is busy. Ignoring XYZ move request.",
        )

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

        with self.reserve_external_task("click calibration"):
            return self._resolve_clicked_point_xy_work(dx_pixels, dy_pixels)

    def request_clicked_point_resolution(
        self,
        request_id: object,
        dx_pixels: float,
        dy_pixels: float,
    ) -> bool:
        """Resolve an image point in a stage-owned background task."""

        def record_request() -> None:
            token = self._operation_lifecycle.snapshot().token
            with self._state_lock:
                self._clicked_point_resolution_request_id = request_id
                self._clicked_point_resolution_token = token

        try:
            return self._start_background_task(
                target=self._run_clicked_point_resolution,
                args=(request_id, float(dx_pixels), float(dy_pixels)),
                busy_message="Stage is busy. Alignment point capture not started.",
                before_create=record_request,
            )
        except Exception:
            with self._state_lock:
                if self._clicked_point_resolution_request_id == request_id:
                    self._clicked_point_resolution_request_id = None
                    self._clicked_point_resolution_token = None
            raise

    def _run_clicked_point_resolution(
        self,
        request_id: object,
        dx_pixels: float,
        dy_pixels: float,
    ) -> None:
        try:
            center_xy, clicked_xy = self._resolve_clicked_point_xy_for_active_task(
                dx_pixels,
                dy_pixels,
            )
            self._check_cancelled()
        except Exception as exc:
            self._release_clicked_point_task(request_id)
            self.clicked_point_resolved.emit(
                request_id,
                False,
                None,
                None,
                str(exc) or type(exc).__name__,
            )
            return
        self._release_clicked_point_task(request_id)
        self.clicked_point_resolved.emit(
            request_id,
            True,
            center_xy,
            clicked_xy,
            "",
        )

    def _release_clicked_point_task(self, request_id: object) -> None:
        self._operation_lifecycle.release_current()
        with self._state_lock:
            if self._clicked_point_resolution_request_id == request_id:
                self._clicked_point_resolution_request_id = None
                self._clicked_point_resolution_token = None

    def _resolve_clicked_point_xy_for_active_task(
        self,
        dx_pixels: float,
        dy_pixels: float,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        if not self._operation_lifecycle.snapshot().owned_by_current_thread:
            raise StageControllerError(
                "Current thread does not own the alignment stage task."
            )
        return self._resolve_clicked_point_xy_work(dx_pixels, dy_pixels)

    def _resolve_clicked_point_xy_work(
        self,
        dx_pixels: float,
        dy_pixels: float,
    ) -> tuple[tuple[float, float], tuple[float, float]]:
        self._check_cancelled()
        if self._click_calibration_required():
            with self._open_optical_session("click-to-move calibration"):
                self._check_cancelled()
                status = self._resolve_clicked_point_status_locked()
        else:
            status = self._resolve_clicked_point_status_locked()
        self._check_cancelled()
        if self._pixels_to_mm is None:
            raise StageControllerError(
                "Calibration failed. Cannot resolve clicked position."
            )
        return self._resolve_xy_from_center(
            tuple(float(value) for value in status.display_position),
            dx_pixels,
            dy_pixels,
        )

    def _resolve_clicked_point_status_locked(self) -> object:
        """Read stage position and ensure click calibration for the task owner."""

        with self._serial_session():
            status = self._query_current_status_with_required_coordinates(
                axes=("X", "Y"),
            )
            if status is None or status.display_position is None:
                raise StageControllerError("Unable to read stage position.")
            self._ensure_calibration()
        return status

    def _click_calibration_required(self) -> bool:
        return (
            self._pixels_to_mm is None
            or not self._objective_calibration_verified.get(
                self._active_objective_name,
                False,
            )
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

    def reset_calibration(self, reason: str = "Click calibration reset.") -> None:
        """Clear the click-to-move calibration so it is rebuilt on next use."""

        lease = self._operation_lifecycle.try_reserve_idle("calibration reset")
        if lease is None:
            raise StageControllerError(
                "Stage is busy. Wait for the current operation to finish."
            )
        with lease:
            self._operation_lifecycle.clear_cancellation()
            with self._state_lock:
                self._pixels_to_mm = None
                self._objective_matrices.pop(self._active_objective_name, None)
                self._objective_calibration_verified[self._active_objective_name] = (
                    False
                )
            task_token = self._operation_lifecycle.advance_generation(
                "click_calibration_reset"
            )
        self.objective_calibration_updated.emit(
            self._active_objective_name,
            [],
            task_token,
        )
        self.status_message.emit(reason)

    def _run_move(self, dx_pixels: float, dy_pixels: float) -> None:
        try:
            if self._click_calibration_required():
                with self._open_optical_session("click-to-move calibration"):
                    self.movement_started.emit()
                    success, message = self._execute_click_move(
                        dx_pixels,
                        dy_pixels,
                    )
            else:
                self.movement_started.emit()
                success, message = self._execute_click_move(
                    dx_pixels,
                    dy_pixels,
                )
            self.movement_finished.emit(success, message)
        except StageControllerError as exc:
            self.movement_finished.emit(False, str(exc))

    def _execute_click_move(
        self,
        dx_pixels: float,
        dy_pixels: float,
    ) -> tuple[bool, str]:
        self._check_cancelled()
        with self._serial_session():
            self._move_safety_check()
            before_counter = self._prepare_click_move_without_status_locked(
                dx_pixels,
                dy_pixels,
            )
        if before_counter is None:
            return (True, "Target already centered.")
        after_frame, _ = self._wait_for_new_frame(before_counter, timeout=4.0)
        if after_frame is None:
            return (
                False,
                "Movement command sent but camera did not provide an updated frame.",
            )
        return (True, "Move complete.")

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

    def _run_token_bound_move_to_xy(
        self,
        token: object,
        target_x_mm: float,
        target_y_mm: float,
        completion,
    ) -> None:
        target = (float(target_x_mm), float(target_y_mm))
        self.movement_started.emit()
        try:
            self._check_cancelled()
            with self._serial_session():
                self._move_safety_check()
                message = self._move_to_xy_locked(*target)
            completion(token, target, True, message)
        except StageControllerError as exc:
            completion(token, target, False, str(exc))

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
            self._require_position_for_absolute_motion(
                status,
                required_axes=2,
            )
            target_position = (float(target_x_mm), float(target_y_mm))
            targets = {"X": target_position[0], "Y": target_position[1]}
            if self._status_matches_axis_targets(
                status, targets, tolerance=1e-5
            ) and not self._precision_targets_require_execution(targets):
                return "Target already at requested X/Y."
            effective_feedrate = clamped_motion_feedrate(
                feedrate,
                default_feedrate=self.DEFAULT_FEEDRATE,
                min_feedrate=self.MIN_FEEDRATE,
            )
            self.status_message.emit(
                f"Moving to X={target_x_mm:.3f} mm, Y={target_y_mm:.3f} mm"
            )
            self.absolute_xy_move_started.emit(
                float(target_x_mm),
                float(target_y_mm),
                float(effective_feedrate),
            )
            self._execute_precision_axis_targets_locked(
                targets,
                feedrate=feedrate,
                allow_unhomed=False,
            )
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
            target_position = (
                float(target_x_mm),
                float(target_y_mm),
                float(target_z_mm),
            )
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
                self._send_absolute_axis_targets_move(
                    {"Z": transit_z},
                    as_jog=True,
                )
                current_z = transit_z
                moved = True

            xy_targets = {
                "X": float(target_position[0]),
                "Y": float(target_position[1]),
            }
            if (
                abs(float(target_position[0]) - current_x) >= 1e-5
                or abs(float(target_position[1]) - current_y) >= 1e-5
                or self._precision_targets_require_execution(xy_targets)
            ):
                self.status_message.emit(
                    f"Moving to {label} X={target_x_mm:.3f} mm, Y={target_y_mm:.3f} mm"
                )
                self._execute_precision_axis_targets_locked(
                    xy_targets,
                    feedrate=None,
                    allow_unhomed=False,
                )
                current_x = float(target_position[0])
                current_y = float(target_position[1])
                moved = True

            delta_z = float(target_position[2]) - current_z
            z_targets = {"Z": float(target_position[2])}
            if abs(delta_z) >= 1e-5 or self._precision_targets_require_execution(
                z_targets
            ):
                self.status_message.emit(
                    f"Moving Z to {label} focus height {target_z_mm:.3f} mm"
                )
                self._execute_precision_axis_targets_locked(
                    z_targets,
                    feedrate=None,
                    allow_unhomed=False,
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
            status = self._query_current_status_with_required_coordinates(
                axes=("X", "Y"),
            )
            position = getattr(status, "display_position", None)
            if not isinstance(position, (list, tuple)) or len(position) < 2:
                raise StageControllerError(
                    "Unable to read stage position for click move."
                )
            self._emit_click_move_started(move, self.DEFAULT_FEEDRATE)
            self._execute_precision_axis_targets_locked(
                {
                    "X": float(position[0]) + move.x,
                    "Y": float(position[1]) + move.y,
                },
                feedrate=None,
                allow_unhomed=False,
            )
            return before_counter

    def _emit_click_move_started(
        self,
        move: MoveVector,
        feedrate: float | None = None,
    ) -> None:
        effective_feedrate = (
            self.DEFAULT_FEEDRATE
            if feedrate is None
            else max(self.MIN_FEEDRATE, float(feedrate))
        )
        self.click_move_started.emit(float(move.x), float(move.y), effective_feedrate)

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

    def _get_frame_snapshot(
        self, timeout: float = 2.0
    ) -> tuple[np.ndarray | None, int]:
        with self._frame_condition:
            if self._latest_frame is None:
                if not self._frame_condition.wait(timeout):
                    return (None, self._frame_counter)
            if self._latest_frame is None:
                return (None, self._frame_counter)
            return (self._latest_frame.copy(), self._frame_counter)

    def _wait_for_new_frame(
        self, previous_counter: int, timeout: float = 2.0
    ) -> tuple[np.ndarray | None, int]:
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
    def _focus_metric(frame: np.ndarray) -> float:
        return focus_metric(frame)

    @staticmethod
    def _qimage_to_gray(image: QImage) -> np.ndarray:
        return cast(np.ndarray, qimage_to_gray(image, getattr(QImage, "Format_RGB888")))
