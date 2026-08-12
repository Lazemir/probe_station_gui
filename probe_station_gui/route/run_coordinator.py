"""Private orchestration for one route measurement run."""

from __future__ import annotations

from dataclasses import dataclass
import math
import time

from probe_station_gui.route import measurement_recording
from probe_station_gui.route.formatting import format_route_percent
from probe_station_gui.route.measurement_records import (
    RouteMeasurementPoint,
    RouteMeasurementRecord,
)
from probe_station_gui.route.point_execution import (
    RoutePointCleanupError,
    RoutePointResult,
)


class _RouteMeasurementStopped(RuntimeError):
    pass


@dataclass(frozen=True)
class _RoutePointLoopDecision:
    position_index: int
    stop_message: str | None = None


@dataclass
class _RouteRunProgress:
    position_index: int = 0
    measurements_saved: int = 0
    photos_saved: int = 0
    needs_final_lift: bool = False


@dataclass(frozen=True)
class _RoutePointFlowResult:
    position_index: int
    measurements_saved: int = 0
    photos_saved: int = 0
    stop_message: str | None = None


class _RouteRunCoordinator:
    """Coordinate one blocking route run for a composed runner."""

    def _run_route(self) -> tuple[bool, str]:
        progress = _RouteRunProgress()
        success = False
        message = "Route measurement stopped."
        self._last_run_result = None
        try:
            self._validate_route_run_configuration()
            self._open_route_meter_if_needed()
            startup_stop_message = self._start_route_run()
            if startup_stop_message is not None:
                message = startup_stop_message
                return success, message
            total = len(self._points)
            progress.position_index = self._route_start_index()
            self._progress_started_at = time.monotonic()
            if self._wait_before_first_point:
                initial_decision = self._initial_route_point_loop_decision(
                    position_index=progress.position_index,
                    total=total,
                )
                if initial_decision.stop_message is not None:
                    message = initial_decision.stop_message
                    return success, message
                progress.position_index = initial_decision.position_index
            while progress.position_index < total:
                point_result = self._run_route_point(
                    position_index=progress.position_index,
                    total=total,
                    progress=progress,
                )
                progress.photos_saved += point_result.photos_saved
                progress.measurements_saved += point_result.measurements_saved
                if point_result.stop_message is not None:
                    message = point_result.stop_message
                    break
                progress.position_index = point_result.position_index
            if progress.position_index >= total:
                success = True
                message = self._route_completion_message(progress)
        except _RouteMeasurementStopped as exc:
            message = str(exc) or "Route measurement stopped by user."
        except Exception as exc:
            message = str(exc)
            self._status(f"Route measurement failed: {message}")
        finally:
            message = self._finish_route_run(
                message=message,
                needs_final_lift=progress.needs_final_lift,
            )
            self._last_run_result = self.result_payload(
                success=success,
                message=message,
            )
        return success, message

    def _validate_route_run_configuration(self) -> None:
        if not self._points:
            raise ValueError("Route has no enabled points.")
        if (
            self._photo_enabled
            and self._events.photo is None
            and self._events.point_photo is None
        ):
            raise ValueError("Route photo capture is not configured.")
        if (
            self._photo_focus_enabled
            and self._events.photo_focus is None
            and self._events.point_photo_focus is None
        ):
            raise ValueError("Route autofocus is not configured.")

    def _open_route_meter_if_needed(self) -> None:
        if self._measure_enabled and hasattr(self._lcr_controller, "open"):
            self._status("Route measurement: connecting meter.")
            self._lcr_controller.open()

    def _start_route_run(self) -> str | None:
        self._begin_stage_task()
        if self._run_control.stop_requested():
            return "Route measurement stopped by user."
        initial_needle_action = (
            "raise" if self._photo_enabled or self._photo_focus_enabled else "lift"
        )
        self._status(
            "Route measurement: raising needles."
            if initial_needle_action == "raise"
            else "Route measurement: lifting needles."
        )
        self._stage_controller.run_external_needles_action(
            initial_needle_action,
            self._needle_feedrate,
        )
        if self._run_control.stop_requested():
            return "Route measurement stopped by user."
        return None

    def _route_start_index(self) -> int:
        start_index = self.index_for_point_number(self._start_point_number)
        if start_index is None and self._start_point_number == 1:
            return 0
        if start_index is None:
            raise ValueError(
                "Route start point "
                f"{self._start_point_number} is not enabled or not found."
            )
        return start_index

    def _initial_route_point_loop_decision(
        self,
        *,
        position_index: int,
        total: int,
    ) -> _RoutePointLoopDecision:
        decision = self._wait_before_first_route_point(
            point=self._points[position_index],
            position=position_index + 1,
            total=total,
        )
        if decision == "stop":
            return _RoutePointLoopDecision(
                position_index=position_index,
                stop_message="Route measurement stopped by user.",
            )
        self._begin_stage_task()
        jump_index = self._jump_target_index(decision)
        if jump_index is not None:
            return _RoutePointLoopDecision(position_index=jump_index)
        if decision == "skip":
            return _RoutePointLoopDecision(position_index=position_index + 1)
        return _RoutePointLoopDecision(position_index=position_index)

    def _run_route_point(
        self,
        *,
        position_index: int,
        total: int,
        progress: _RouteRunProgress,
    ) -> _RoutePointFlowResult:
        if self._run_control.stop_requested():
            return _RoutePointFlowResult(
                position_index=position_index,
                stop_message="Route measurement stopped by user.",
            )
        position = position_index + 1
        point = self._points[position_index]
        self._emit_progress(position, total, int(point.index))
        self._status(f"Route measurement: point {position}/{total} {point.label}.")
        try:
            result = self._point_adapters.execution.execute(
                self._point_request(point, position=position, total=total)
            )
        except RoutePointCleanupError as exc:
            progress.needs_final_lift = exc.pending_cleanup
            raise exc.cause from exc
        progress.needs_final_lift = result.pending_cleanup
        return self._finish_point_execution(
            point=point,
            result=result,
            position=position,
            total=total,
            position_index=position_index,
        )

    def _finish_point_execution(
        self,
        *,
        point: RouteMeasurementPoint,
        result: RoutePointResult,
        position: int,
        total: int,
        position_index: int,
    ) -> _RoutePointFlowResult:
        if result.stopped:
            return _RoutePointFlowResult(
                position_index=position_index,
                photos_saved=result.photos_saved,
                stop_message=result.stop_message,
            )
        if result.pending_cleanup:
            raise RuntimeError("Route point needle cleanup failed.")
        if result.interrupted:
            decision = self._interrupted_route_point_loop_decision(
                point=point,
                position=position,
                total=total,
                position_index=position_index,
                clear_stage_cancel=self._measure_enabled,
            )
            return _RoutePointFlowResult(
                position_index=decision.position_index,
                photos_saved=result.photos_saved,
                stop_message=decision.stop_message,
            )
        if not self._measure_enabled:
            return _RoutePointFlowResult(
                position_index=position_index + 1,
                photos_saved=result.photos_saved,
            )
        if result.record is None:
            return _RoutePointFlowResult(
                position_index=position_index,
                photos_saved=result.photos_saved,
            )
        if result.quality_rejected:
            return self._finish_rejected_point_result(
                point=point,
                result=result,
                position=position,
                total=total,
                position_index=position_index,
            )
        return self._finish_accepted_point_result(
            point=point,
            result=result,
            position=position,
            total=total,
            position_index=position_index,
        )

    def _finish_rejected_point_result(
        self,
        *,
        point: RouteMeasurementPoint,
        result: RoutePointResult,
        position: int,
        total: int,
        position_index: int,
    ) -> _RoutePointFlowResult:
        assert result.record is not None
        self._run_control.consume_pause()
        decision = self._wait_after_rejected_result(
            point=point,
            record=result.record,
            position=position,
            total=total,
            emit_result=not result.result_emitted,
        )
        loop_decision, saved_count = self._route_point_confirmation_loop_decision(
            decision,
            point=point,
            position=position,
            total=total,
            position_index=position_index,
            default_advances=False,
        )
        return _RoutePointFlowResult(
            position_index=loop_decision.position_index,
            measurements_saved=saved_count,
            photos_saved=result.photos_saved,
            stop_message=loop_decision.stop_message,
        )

    def _finish_accepted_point_result(
        self,
        *,
        point: RouteMeasurementPoint,
        result: RoutePointResult,
        position: int,
        total: int,
        position_index: int,
    ) -> _RoutePointFlowResult:
        assert result.record is not None
        auto_next = self._route_point_auto_next(
            result.record,
            record_saved=result.record_saved,
        )
        pause_after_point = False
        if self._confirm_each_point:
            auto_next, pause_after_point = self._prepare_saved_route_point_wait(
                point=point,
                auto_next=auto_next,
            )
            confirmation = self._saved_route_point_confirmation_result(
                point=point,
                record=result.record,
                position=position,
                total=total,
                position_index=position_index,
                auto_next=auto_next,
                pause_after_point=pause_after_point,
                save_exhausted_bad_contact=result.save_exhausted_bad_contact,
            )
            if confirmation is not None:
                return _RoutePointFlowResult(
                    position_index=confirmation.position_index,
                    measurements_saved=(
                        result.measurements_saved + confirmation.measurements_saved
                    ),
                    photos_saved=result.photos_saved,
                    stop_message=confirmation.stop_message,
                )
        return _RoutePointFlowResult(
            position_index=position_index + 1,
            measurements_saved=result.measurements_saved,
            photos_saved=result.photos_saved,
        )

    def _route_point_auto_next(
        self,
        record: RouteMeasurementRecord,
        *,
        record_saved: bool,
    ) -> bool:
        return (
            self._confirm_each_point
            and record_saved
            and record.status in {"ok", "short"}
            and self._auto_next_ok_or_short_enabled()
        )

    def _prepare_saved_route_point_wait(
        self,
        *,
        point: RouteMeasurementPoint,
        auto_next: bool,
    ) -> tuple[bool, bool]:
        self._run_control.clear_confirmation()
        with self._route_offset_lock:
            self._last_recorded_point = point
        pause_after_point = self._run_control.consume_pause()
        auto_next = auto_next and not pause_after_point
        if not auto_next:
            self._finish_stage_task()
            self._run_control.set_waiting(True)
        return auto_next, pause_after_point

    def _saved_route_point_confirmation_result(
        self,
        *,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        position_index: int,
        auto_next: bool,
        pause_after_point: bool,
        save_exhausted_bad_contact: bool,
    ) -> _RoutePointFlowResult | None:
        status_detail = measurement_recording.saved_route_point_status_detail(
            record,
            save_exhausted_bad_contact=save_exhausted_bad_contact,
        )
        if auto_next:
            self._status(
                f"Route measurement: point {position}/{total} "
                f"{status_detail}; continuing."
            )
            return None
        action_text = "paused" if pause_after_point else status_detail
        self._status(
            f"Route measurement: point {position}/{total} "
            f"{action_text}; "
            "choose Measure or Skip."
        )
        decision = self._wait_for_valid_confirmation()
        self._run_control.set_waiting(False)
        loop_decision, saved_count = self._route_point_confirmation_loop_decision(
            decision,
            point=point,
            position=position,
            total=total,
            position_index=position_index,
            default_advances=True,
        )
        return _RoutePointFlowResult(
            position_index=loop_decision.position_index,
            measurements_saved=saved_count,
            stop_message=loop_decision.stop_message,
        )

    def _route_completion_message(self, progress: _RouteRunProgress) -> str:
        if self._measure_enabled and self._photo_enabled:
            return (
                "Route measurement complete: "
                f"{progress.photos_saved} photos and "
                f"{progress.measurements_saved} measurements saved. "
                f"CSV: {self.csv_path}."
            )
        if self._photo_enabled:
            return (
                f"Route photo capture complete: {progress.photos_saved} photos saved."
            )
        return (
            "Route measurement complete: "
            f"{progress.measurements_saved} measurements saved to "
            f"{self.csv_path}."
        )

    def _finish_route_run(
        self,
        *,
        message: str,
        needs_final_lift: bool,
    ) -> str:
        if self._stage_task_active and needs_final_lift:
            try:
                self._stage_controller.run_external_needles_action(
                    "lift",
                    self._needle_feedrate,
                )
            except Exception as exc:
                message = f"{message} Needle lift failed: {exc}"
        try:
            self._close_meter_output_context()
        except Exception as exc:
            message = f"{message} Instrument output disable failed: {exc}"
        self._finish_stage_task()
        if self._measure_enabled and hasattr(self._lcr_controller, "close"):
            try:
                self._lcr_controller.close()
            except Exception as exc:
                message = f"{message} Instrument close failed: {exc}"
        self._run_control.set_waiting(False)
        return message

    def _measure_manual_contact_and_advance(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        position_index: int,
    ) -> tuple[_RoutePointLoopDecision, int]:
        manual_record = self._measure_manual_contact_here(
            point=point,
            position=position,
            total=total,
        )
        if manual_record is None:
            if self._run_control.interrupt_requested():
                self._run_control.clear_interrupt()
                return _RoutePointLoopDecision(position_index=position_index), 0
            return (
                _RoutePointLoopDecision(
                    position_index=position_index,
                    stop_message="Route measurement stopped by user.",
                ),
                0,
            )
        next_index = position_index + 1
        if next_index < total:
            self._begin_stage_task()
        return _RoutePointLoopDecision(position_index=next_index), 1

    def _route_point_confirmation_loop_decision(
        self,
        decision: str,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        position_index: int,
        default_advances: bool,
    ) -> tuple[_RoutePointLoopDecision, int]:
        if decision == "stop":
            return (
                _RoutePointLoopDecision(
                    position_index=position_index,
                    stop_message="Route measurement stopped by user.",
                ),
                0,
            )
        if decision == "measure":
            return self._measure_manual_contact_and_advance(
                point=point,
                position=position,
                total=total,
                position_index=position_index,
            )
        self._begin_stage_task()
        jump_index = self._jump_target_index(decision)
        if jump_index is not None:
            return _RoutePointLoopDecision(position_index=jump_index), 0
        if decision == "skip" or (default_advances and decision != "remeasure"):
            return _RoutePointLoopDecision(position_index=position_index + 1), 0
        return _RoutePointLoopDecision(position_index=position_index), 0

    def _interrupted_route_point_loop_decision(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        position_index: int,
        clear_stage_cancel: bool,
    ) -> _RoutePointLoopDecision:
        if clear_stage_cancel:
            self._clear_stage_cancel_after_point_interrupt()
        self._run_control.clear_interrupt()
        decision = self._wait_after_interrupted_point(
            point=point,
            position=position,
            total=total,
        )
        if decision == "stop":
            return _RoutePointLoopDecision(
                position_index=position_index,
                stop_message="Route measurement stopped by user.",
            )
        self._begin_stage_task()
        jump_index = self._jump_target_index(decision)
        if jump_index is not None:
            return _RoutePointLoopDecision(position_index=jump_index)
        if decision == "skip":
            return _RoutePointLoopDecision(position_index=position_index + 1)
        return _RoutePointLoopDecision(position_index=position_index)

    def _wait_before_first_route_point(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> str:
        self._run_control.consume_pause()
        self._run_control.clear_confirmation()
        with self._route_offset_lock:
            self._last_recorded_point = point
        self._finish_stage_task()
        self._emit_progress(position, total, int(point.index))
        self._run_control.set_waiting(True)
        self._status(
            f"Route measurement ready: point {position}/{total} {point.label}."
        )
        decision = self._wait_for_valid_confirmation()
        self._run_control.set_waiting(False)
        return decision

    def _wait_after_interrupted_point(
        self,
        *,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> str:
        self._run_control.consume_pause()
        self._run_control.clear_confirmation()
        with self._route_offset_lock:
            self._last_recorded_point = point
        self._finish_stage_task()
        self._run_control.set_waiting(True)
        self._status(
            f"Route measurement: point {position}/{total} interrupted; "
            "correct position, then Measure or Skip."
        )
        decision = self._wait_for_valid_confirmation()
        self._run_control.set_waiting(False)
        return decision

    def _wait_after_rejected_result(
        self,
        *,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        emit_result: bool = True,
    ) -> str:
        self._run_control.clear_confirmation()
        with self._route_offset_lock:
            self._last_recorded_point = point
        self._finish_stage_task()
        self._run_control.set_waiting(True)
        if emit_result:
            self._point_adapters.events.emit_result(record, position, total, False)
        contact_quality = record.contact_quality
        if measurement_recording.record_has_failed_contact_quality(record):
            assert contact_quality is not None
            self._status(
                f"Route measurement: point {position}/{total} contact check failed "
                f"({contact_quality.status}"
                f"{measurement_recording.contact_quality_failure_suffix(self._legacy_contact.recording_context(), contact_quality)}); "
                "correct contact, then Measure, "
                "Remeasure, or Skip."
            )
        else:
            self._status(
                f"Route measurement: point {position}/{total} relative RMS "
                f"{format_route_percent(record.relative_rms)} exceeds "
                f"{format_route_percent(self._max_relative_rms or math.nan)}; "
                "correct contact, then Measure, Remeasure, or Skip."
            )
        decision = self._wait_for_valid_confirmation()
        self._run_control.set_waiting(False)
        return decision

    def _wait_for_valid_confirmation(self) -> str:
        while True:
            decision = self._run_control.wait_for_confirmation()
            if not decision.startswith("jump:"):
                return decision
            if self._jump_target_index(decision) is not None:
                return decision
            point_number = decision.split(":", 1)[1]
            self._status(
                f"Route measurement: point {point_number} is not enabled or not found."
            )
            self._run_control.set_waiting(True)

    def _jump_target_index(self, decision: str) -> int | None:
        if not decision.startswith("jump:"):
            return None
        try:
            point_number = int(decision.split(":", 1)[1])
        except ValueError:
            return None
        return self.index_for_point_number(point_number)
