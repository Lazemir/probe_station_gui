"""Autofocus workflow for the stage controller."""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from probe_station_gui.stage.autofocus_math import (
    autofocus_sweep_feedrate_mm_min,
    frame_rate_from_timestamps,
    parabolic_focus_peak,
    static_focus_candidates,
)
from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.motion_prediction import interpolate_position
from probe_station_gui.stage.types import (
    AutofocusResult,
    MoveVector,
    _AutofocusContext,
    _FocusSweepResult,
)

if TYPE_CHECKING:
    import numpy as np


logger = logging.getLogger(__name__)


class StageControllerAutofocusMixin:
    """Run Z autofocus tasks while preserving the controller's public API."""

    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    def request_autofocus(self) -> None:
        """Begin an asynchronous autofocus sweep along the Z axis."""

        with self._task_lock:
            active_thread = getattr(self, "_active_thread", None)
            if active_thread and active_thread.is_alive():
                self.status_message.emit("Stage is busy. Ignoring autofocus request.")
                return
            self._cancel_event.clear()
            thread = threading.Thread(target=self._run_autofocus, daemon=True)
            setattr(self, "_active_thread", thread)
            thread.start()

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

    def _run_autofocus(self) -> None:
        self.movement_started.emit()
        try:
            with self._serial_session():
                self._run_autofocus_locked()
        except StageControllerError as exc:
            self.autofocus_finished.emit(False, str(exc))
        finally:
            with self._task_lock:
                setattr(self, "_active_thread", None)

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
        setattr(
            self,
            "_queued_jog_generation",
            int(getattr(self, "_queued_jog_generation")) + 1,
        )
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
