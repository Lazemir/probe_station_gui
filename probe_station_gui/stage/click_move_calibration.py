"""Calibration policy for click-to-move stage control."""

from __future__ import annotations

import logging
import math

import numpy as np

from probe_station_gui.stage.autofocus_math import (
    estimate_shift,
    estimate_shift_with_response,
)
from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.types import MoveVector


logger = logging.getLogger("probe_station_gui.stage.click_move")


class _StageControllerClickCalibrationMixin:
    """Own click-to-move calibration policy for the stage composition root."""

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
                self._calibrate_axis_series(before_frame, origin, axis="X")
            )
            latest_frame, _ = self._get_frame_snapshot(timeout=2.0)
            reference_for_y = latest_frame if latest_frame is not None else before_frame
            observations.extend(
                self._calibrate_axis_series(reference_for_y, origin, axis="Y")
            )
            calibration_matrix = self._calibration_matrix_from_observations(
                observations
            )
            if not np.isfinite(calibration_matrix).all():
                raise StageControllerError("Calibration produced invalid values.")
            determinant = float(np.linalg.det(calibration_matrix))
            if abs(determinant) < 1e-9:
                raise StageControllerError("Calibration matrix is singular.")
            pixels_to_mm = np.linalg.inv(calibration_matrix)
            task_token = self._calibration_signal_token("click_calibration")
            self._check_cancelled()
            if not self.offer_objective_calibration_candidate(
                task_token,
                self._active_objective_name,
                pixels_to_mm,
            ):
                raise StageControllerError(
                    "Click calibration result is no longer owned by this task."
                )
            self._check_cancelled()
            if not self.is_calibration_task_token_current(task_token):
                self.reject_objective_calibration_candidate(task_token)
                raise StageControllerError(
                    "Click calibration result is no longer owned by this task."
                )
            self.objective_calibration_updated.emit(
                self._active_objective_name,
                pixels_to_mm.tolist(),
                task_token,
            )
            if not self.wait_for_objective_calibration_candidate(
                task_token,
                timeout_s=10.0,
            ):
                raise StageControllerError(
                    "Click calibration result was cancelled or not accepted."
                )
            self._check_cancelled()
            if self._pixels_to_mm is None:
                raise StageControllerError("Calibration result was not applied.")
            mm_per_pixel_x, mm_per_pixel_y = self._calibration_magnitudes()
            target_handled, before_counter = self._move_from_calibration_to_target(
                origin,
                target_pixels,
            )
        except Exception:
            task_token = locals().get("task_token")
            if task_token is not None:
                self.reject_objective_calibration_candidate(task_token)
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
            raise StageControllerError(
                "Camera frames are unavailable for calibration check."
            )
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
                task_token = self._calibration_signal_token("click_calibration_check")
                self._check_cancelled()
                if not self.is_calibration_task_token_current(task_token):
                    raise StageControllerError(
                        "Calibration check result is no longer owned by this task."
                    )
                self.objective_mismatch_detected.emit(
                    suggestion,
                    message,
                    task_token,
                )
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
            raise StageControllerError(
                "Unable to read reference position for calibration."
            )
        self._require_homed_axes(reference_status, {axis})
        with self._frame_condition:
            frame_counter = self._frame_counter
        for _ in range(self.CALIBRATION_MAX_OBSERVATIONS_PER_AXIS):
            if axis == "X":
                move = MoveVector(x=step_mm)
            else:
                move = MoveVector(y=step_mm)
            self._send_relative_move(move)
            new_frame, frame_counter = self._wait_for_new_frame(
                frame_counter, timeout=2.0
            )
            if new_frame is None:
                raise StageControllerError("Camera did not update during calibration.")
            status = self._query_current_status_with_required_coordinates(
                axes=("X", "Y"),
            )
            current = self._position_for_configured_mode(status)
            if status is None or current is None:
                raise StageControllerError(
                    "Unable to query position during calibration."
                )
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
            logger.info(
                "Click calibration %s observation: step_mm=%.9f "
                "status_delta=(%.9f, %.9f) shift_px=(%.3f, %.3f) "
                "shift_norm_px=%.3f response=%.4f reliable=%s",
                axis,
                float(step_mm),
                float(mm_vector[0]),
                float(mm_vector[1]),
                float(shift_x),
                float(shift_y),
                float(shift_pixels),
                float(response),
                bool(reliable_shift),
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
        axis_matrix = self._axis_slope_calibration_matrix(
            stage_vectors,
            pixel_vectors,
        )
        if axis_matrix is not None:
            return axis_matrix
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
        logger.info(
            "Click calibration fit: observations=%d matrix=%s residual_mean_px=%.3f "
            "residual_max_px=%.3f",
            len(observations),
            calibration_matrix.tolist(),
            float(np.mean(errors)),
            float(np.max(errors)),
        )
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
                predicted = stage_vectors[keep] @ coefficients
                errors = np.linalg.norm(predicted - pixel_vectors[keep], axis=1)
                logger.info(
                    "Click calibration fit after outlier filter: observations=%d "
                    "matrix=%s residual_mean_px=%.3f residual_max_px=%.3f",
                    int(np.count_nonzero(keep)),
                    calibration_matrix.tolist(),
                    float(np.mean(errors)),
                    float(np.max(errors)),
                )
        return calibration_matrix

    @staticmethod
    def _axis_slope_calibration_matrix(
        stage_vectors: np.ndarray,
        pixel_vectors: np.ndarray,
    ) -> np.ndarray | None:
        calibration_matrix = np.zeros((2, 2), dtype=float)
        residuals: list[np.ndarray] = []
        intercepts: list[np.ndarray] = []
        counts: list[int] = []
        for axis_index in (0, 1):
            other_index = 1 - axis_index
            primary = np.abs(stage_vectors[:, axis_index])
            secondary = np.abs(stage_vectors[:, other_index])
            mask = (primary > 1e-9) & (primary >= secondary)
            count = int(np.count_nonzero(mask))
            if count < 2:
                return None
            axis_deltas = stage_vectors[mask, axis_index]
            if float(np.ptp(axis_deltas)) <= 1e-9:
                return None
            design = np.column_stack(
                [
                    axis_deltas,
                    np.ones(count, dtype=float),
                ]
            )
            coefficients, _residuals, _rank, _singular = np.linalg.lstsq(
                design,
                pixel_vectors[mask],
                rcond=None,
            )
            slope = coefficients[0]
            intercept = coefficients[1]
            calibration_matrix[:, axis_index] = slope
            predicted = design @ coefficients
            residuals.append(np.linalg.norm(predicted - pixel_vectors[mask], axis=1))
            intercepts.append(intercept)
            counts.append(count)
        if not np.isfinite(calibration_matrix).all():
            raise StageControllerError("Calibration produced invalid values.")
        all_residuals = np.concatenate(residuals) if residuals else np.zeros(0)
        logger.info(
            "Click calibration axis-slope fit: counts=%s matrix=%s "
            "intercepts_px=%s residual_mean_px=%.3f residual_max_px=%.3f",
            counts,
            calibration_matrix.tolist(),
            [intercept.tolist() for intercept in intercepts],
            float(np.mean(all_residuals)) if all_residuals.size else 0.0,
            float(np.max(all_residuals)) if all_residuals.size else 0.0,
        )
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
        self.status_message.emit(f"Jogging stage dX={move.x:.3f} mm dY={move.y:.3f} mm")
        self._emit_click_move_started(move, self.DEFAULT_FEEDRATE)
        self._execute_precision_axis_targets_locked(
            {"X": target_x, "Y": target_y},
            feedrate=None,
            allow_unhomed=False,
        )
        return (True, before_counter)

    def _return_to_origin(self, origin: tuple[float, float, float]) -> None:
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

    def _calibration_magnitudes(self) -> tuple[float, float]:
        if self._pixels_to_mm is None:
            return (0.0, 0.0)
        column_x = self._pixels_to_mm[:, 0]
        column_y = self._pixels_to_mm[:, 1]
        return (float(np.linalg.norm(column_x)), float(np.linalg.norm(column_y)))

    @staticmethod
    def _estimate_shift(
        frame_a: np.ndarray, frame_b: np.ndarray
    ) -> tuple[float, float]:
        return estimate_shift(frame_a, frame_b)

    @staticmethod
    def _estimate_shift_with_response(
        frame_a: np.ndarray,
        frame_b: np.ndarray,
    ) -> tuple[float, float, float]:
        return estimate_shift_with_response(frame_a, frame_b)
