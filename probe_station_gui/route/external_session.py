"""API Route Control session runner for external route measurements."""

from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from probe_station_gui.route.contact_quality import RouteContactQualityLimits
from probe_station_gui.route.control_state import normalize_route_control_action
from probe_station_gui.route.measurement_defaults import (
    AUTO_CONTACT_SEEK_MAX_TOTAL_MM as DEFAULT_AUTO_CONTACT_SEEK_MAX_TOTAL_MM,
    AUTO_CONTACT_SEEK_STEP_MM as DEFAULT_AUTO_CONTACT_SEEK_STEP_MM,
    DEFAULT_CONTACT_SETTLE_S as DEFAULT_ROUTE_CONTACT_SETTLE_S,
)
from probe_station_gui.route.measurement_payloads import (
    route_contact_seek_payload as _route_contact_seek_payload,
    route_measurement_record_payload as _route_measurement_record_payload,
)
from probe_station_gui.route.measurement_records import (
    Point2D,
    RouteExternalContactPreparation,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
    RoutePhotoRecord,
)
from probe_station_gui.route.model import structure_number_from_labels
from probe_station_gui.route.operation_modes import (
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
)

logger = logging.getLogger("probe_station_gui.route.measurement")


def _structure_number_for_point(point: RouteMeasurementPoint) -> int:
    return structure_number_from_labels(
        point.label,
        point.point_id,
        default=point.index,
    )


class RouteExternalMeasurementSessionRunner:
    """Run route contacts for API-owned external measurements.

    The GUI-visible route state machine remains the owner of movement, contact
    checks, contact seek, pause, interrupt, and Telegram callbacks. The external
    client only owns the final experiment-specific measurement and storage.
    """

    def __init__(
        self,
        *,
        session_id: str,
        points: list[RouteMeasurementPoint],
        stage_controller: Any,
        lcr_controller: Any,
        needle_feedrate: float | None,
        measurement_count: int,
        initial_measurement_count: int,
        start_point_number: int = 1,
        max_relative_rms: float | None = None,
        contact_quality_limits: RouteContactQualityLimits | None = None,
        auto_contact_seek_step_mm: float = DEFAULT_AUTO_CONTACT_SEEK_STEP_MM,
        auto_contact_seek_max_total_mm: float = DEFAULT_AUTO_CONTACT_SEEK_MAX_TOTAL_MM,
        contact_settle_s: float = DEFAULT_ROUTE_CONTACT_SETTLE_S,
        nplc_label: str = "",
        measurement_type: str = "",
        status_callback: Callable[[str], None] | None = None,
        progress_callback: Callable[[int, int, int], None] | None = None,
        photo_callback: Callable[
            [RouteMeasurementPoint, int, int, object | None],
            str | Path,
        ]
        | None = None,
        photo_focus_callback: Callable[[RouteMeasurementPoint, int, int], object | None]
        | None = None,
        photo_record_callback: Callable[[RoutePhotoRecord, int, int], None]
        | None = None,
        contact_photo_callback: Callable[
            [RouteMeasurementPoint, RouteMeasurementRecord, int, int, bool],
            None,
        ]
        | None = None,
        pre_contact_photo_callback: Callable[
            [RouteMeasurementPoint, int, int],
            None,
        ]
        | None = None,
        result_callback: Callable[[RouteMeasurementRecord, int, int, bool], None]
        | None = None,
        waiting_callback: Callable[[bool], None] | None = None,
        photo_enabled: bool = True,
        photo_focus_enabled: bool = True,
        photo_settle_s: float = 0.2,
        wait_before_first_point: bool = False,
    ) -> None:
        self.session_id = str(session_id)
        self._points = list(points)
        self._csv_path = Path(os.devnull)
        self._status_callback = status_callback
        self._progress_callback = progress_callback
        self._result_callback = result_callback
        self._waiting_callback = waiting_callback
        self._photo_enabled = bool(photo_enabled)
        self._photo_focus_enabled = bool(photo_focus_enabled)
        self._wait_before_first_point = bool(wait_before_first_point)
        self._condition = threading.Condition()
        self._stop_requested = False
        self._pause_requested = False
        self._pending_action: str | None = None
        self._pending_external_result: dict[str, Any] | None = None
        self._external_measurement_request_id = 0
        self._state = "idle"
        self._waiting_reason = ""
        self._message = "Route API session idle."
        self._position = 0
        self._total = len(self._points)
        self._current_point: RouteMeasurementPoint | None = None
        self._last_preparation: RouteExternalContactPreparation | None = None
        self._last_external_result: dict[str, Any] | None = None
        self._history: list[dict[str, Any]] = []
        from probe_station_gui.route.measurement import RouteMeasurementRunner

        self._contact_runner = RouteMeasurementRunner(
            points=self._points,
            csv_path=self._csv_path,
            stage_controller=stage_controller,
            lcr_controller=lcr_controller,
            needle_feedrate=needle_feedrate,
            measurement_count=measurement_count,
            initial_measurement_count=initial_measurement_count,
            start_point_number=start_point_number,
            max_relative_rms=max_relative_rms,
            contact_quality_limits=contact_quality_limits,
            confirm_each_point=False,
            auto_contact_seek_on_bad_contact=True,
            auto_contact_seek_step_mm=auto_contact_seek_step_mm,
            auto_contact_seek_max_total_mm=auto_contact_seek_max_total_mm,
            contact_settle_s=contact_settle_s,
            nplc_label=nplc_label,
            measurement_type=measurement_type,
            status_callback=status_callback,
            progress_callback=progress_callback,
            photo_callback=photo_callback,
            photo_focus_callback=photo_focus_callback,
            photo_record_callback=photo_record_callback,
            contact_photo_callback=contact_photo_callback,
            pre_contact_photo_callback=pre_contact_photo_callback,
            result_callback=result_callback,
            waiting_callback=waiting_callback,
            operation_mode=ROUTE_OPERATION_PHOTO_THEN_MEASURE
            if self._photo_enabled
            else ROUTE_OPERATION_MEASURE,
            photo_settle_s=photo_settle_s,
            photo_focus_enabled=self._photo_focus_enabled,
        )

    @property
    def csv_path(self) -> Path:
        return self._csv_path

    def route_offset_xy(self) -> Point2D:
        return self._contact_runner.route_offset_xy()

    def set_route_offset_xy(self, offset_xy: Point2D) -> None:
        self._contact_runner.set_route_offset_xy(offset_xy)

    def stop(self) -> None:
        with self._condition:
            self._stop_requested = True
            self._condition.notify_all()
        self._contact_runner.stop()

    def request_pause_after_current_point(self) -> None:
        with self._condition:
            self._pause_requested = True
            self._message = (
                "Route API session pause requested; will pause after current contact."
            )
            self._condition.notify_all()

    def request_current_point_correction(self) -> None:
        self._contact_runner.request_current_point_correction()
        with self._condition:
            if self._state.startswith("waiting"):
                self._pending_action = "interrupt"
            self._condition.notify_all()

    def submit_confirmation(self, action: str) -> bool:
        normalized = self._normalize_action(action)
        if normalized is None:
            return False
        with self._condition:
            self._pending_action = normalized
            self._condition.notify_all()
        return True

    def submit_external_result(self, result: dict[str, Any]) -> bool:
        with self._condition:
            if self._state != "waiting_external_measurement":
                return False
            request_id = result.get("external_measurement_request_id")
            if request_id is None:
                request_id = result.get("request_id")
            if request_id is not None and str(request_id) != str(
                self._external_measurement_request_id
            ):
                return False
            self._pending_external_result = dict(result)
            self._condition.notify_all()
        return True

    def request_contact_seek(self) -> bool:
        with self._condition:
            if not self._state.startswith("waiting"):
                return False
            self._pending_action = "seek"
            self._condition.notify_all()
        return True

    def set_current_adjustment_point(self, point_number: int) -> tuple[bool, str]:
        return self._contact_runner.set_current_adjustment_point(point_number)

    def save_current_position_adjustment(
        self,
        current_stage_xy: Point2D,
    ) -> tuple[bool, str]:
        return self._contact_runner.save_current_position_adjustment(
            current_stage_xy
        )

    def update_runtime_settings(
        self,
        *,
        measurement_count: int,
        initial_measurement_count: int,
        max_relative_rms: float | None,
        auto_contact_seek_step_mm: float,
        auto_contact_seek_max_total_mm: float,
        contact_settle_s: float,
        contact_quality_limits: RouteContactQualityLimits | None = None,
        photo_settle_s: float | None = None,
    ) -> None:
        self._contact_runner.update_runtime_settings(
            measurement_count=measurement_count,
            initial_measurement_count=initial_measurement_count,
            max_relative_rms=max_relative_rms,
            auto_contact_seek_step_mm=auto_contact_seek_step_mm,
            auto_contact_seek_max_total_mm=auto_contact_seek_max_total_mm,
            contact_settle_s=contact_settle_s,
            contact_quality_limits=contact_quality_limits,
            photo_settle_s=photo_settle_s,
        )

    def status_payload(self) -> dict[str, Any]:
        with self._condition:
            return {
                "accepted": True,
                "session_id": self.session_id,
                "state": self._state,
                "waiting": self._state.startswith("waiting"),
                "waiting_reason": self._waiting_reason,
                "message": self._message,
                "position": self._position,
                "total": self._total,
                "external_measurement_request_id": self._external_measurement_request_id,
                "contact_quality_limits": (
                    self._contact_runner.contact_quality_limits().as_dict()
                ),
                "current_contact": self._point_payload(self._current_point),
                "last_preparation": self._preparation_payload(
                    self._last_preparation
                ),
                "last_external_result": self._last_external_result,
                "history": list(self._history),
            }

    def wait_until_initial_pause(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        with self._condition:
            while True:
                if (
                    self._state == "waiting_paused"
                    and self._waiting_reason == "paused"
                ):
                    return True
                if self._state in {"complete", "stopped", "failed"}:
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(timeout=min(0.05, remaining))

    def run(self) -> tuple[bool, str]:
        success = False
        message = "Route API session stopped."
        try:
            if not self._points:
                raise ValueError("Route has no enabled points.")
            lcr = self._contact_runner._lcr_controller
            if hasattr(lcr, "open"):
                self._status("Route API session: connecting meter.")
                lcr.open()
            self._set_state("running", message="Route API session starting.")
            index = self._start_index()
            index, stop_message = self._handle_initial_pause(index)
            if stop_message is not None:
                message = stop_message
            while stop_message is None and index < len(self._points):
                if self._stop_requested_now():
                    message = "Route API session stopped by user."
                    break
                index = self._run_route_point(index)
            if index >= len(self._points) and not self._stop_requested_now():
                success = True
                message = "Route API session complete."
        except Exception as exc:
            message = str(exc)
            self._status(f"Route API session failed: {message}")
        finally:
            try:
                lcr = self._contact_runner._lcr_controller
                if hasattr(lcr, "close"):
                    lcr.close()
            except Exception as exc:
                message = f"{message} Instrument close failed: {exc}"
            self._set_waiting(False)
            self._set_state(
                "complete" if success else "stopped",
                waiting_reason="",
                message=message,
            )
        return success, message

    def _handle_initial_pause(self, index: int) -> tuple[int, str | None]:
        if not self._wait_before_first_point or index >= len(self._points):
            return index, None
        point = self._points[index]
        position = index + 1
        self._set_current(position, point)
        self._emit_progress(position, len(self._points), int(point.index))
        decision = self._wait_before_first_point_decision(point, position)
        action = self._decision_action(decision, default="next")
        if action == "stop":
            self.stop()
            return len(self._points), "Route API session stopped by user."
        jump = self._jump_index(action)
        if jump is not None:
            return jump, None
        if action == "skip":
            return index + 1, None
        return index, None

    def _run_route_point(self, index: int) -> int:
        point = self._points[index]
        position = index + 1
        total = len(self._points)
        self._set_current(position, point)
        self._emit_progress(position, total, int(point.index))
        try:
            preparation = self._prepare_point(point, position, total)
        except RuntimeError:
            if not self._point_interrupted():
                raise
            decision = self._wait_for_interrupted_point(point, position)
            jump = self._handle_interrupted_decision(decision, position, total)
            return index if jump is None else jump
        record = preparation.placement.record
        self._store_preparation(preparation)
        self._emit_result(record, position, total, record.status in {"ok", "short"})
        if record.status == "short":
            self._append_history(
                point,
                position,
                "short",
                preparation=preparation,
                external_result=None,
            )
            self._status(
                f"Route API session: point {position}/{total} "
                "short-circuit detected; external measurement skipped."
            )
            self._lift_needles(position, total)
            pause_decision = self._wait_if_pause_requested(point, position)
            jump = self._handle_post_point_decision(pause_decision)
            return self._advance_after_optional_jump(index, jump)
        if record.status != "ok":
            decision = self._wait_for_contact_attention(point, position)
            jump = self._handle_attention_decision(decision, point, position, total)
            return self._advance_after_optional_jump(index, jump)
        decision = self._wait_for_external_result(point, position)
        if "result" in decision:
            jump = self._handle_external_result(
                dict(decision["result"]),
                point,
                position,
                total,
                preparation=preparation,
            )
        else:
            jump = self._handle_attention_decision(decision, point, position, total)
        return self._advance_after_optional_jump(index, jump)

    @staticmethod
    def _advance_after_optional_jump(index: int, jump: int | None) -> int:
        return index + 1 if jump is None else jump

    @staticmethod
    def _decision_action(decision: dict[str, Any], *, default: str = "") -> str:
        return str(decision.get("action") or default)

    @staticmethod
    def _normalize_action(action: str) -> str | None:
        return normalize_route_control_action(action)

    def _start_index(self) -> int:
        start = self._contact_runner._start_point_number
        index = self._contact_runner._index_for_point_number(start)
        return 0 if index is None else index

    def _prepare_point(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> RouteExternalContactPreparation:
        self._set_state(
            "running",
            waiting_reason="",
            message=f"Route API session: preparing point {position}/{total}.",
        )
        return self._contact_runner.prepare_external_contact(
            point,
            position=position,
            total=total,
            photo_enabled=self._photo_enabled,
            photo_focus_enabled=self._photo_focus_enabled,
            lift_on_failure=False,
        )

    def _wait_for_contact_attention(
        self,
        point: RouteMeasurementPoint,
        position: int,
    ) -> dict[str, Any]:
        message = (
            f"Route API session: point {position}/{self._total} "
            f"{point.label} needs contact action."
        )
        self._status(message)
        return self._wait_for_decision(
            state="waiting_contact",
            reason="contact",
            message=message,
            allow_external_result=False,
        )

    def _wait_for_interrupted_point(
        self,
        point: RouteMeasurementPoint,
        position: int,
    ) -> dict[str, Any]:
        message = (
            f"Route API session: point {position}/{self._total} "
            f"{point.label} interrupted."
        )
        self._status(message)
        return self._wait_for_decision(
            state="waiting_interrupted",
            reason="interrupted",
            message=message,
            allow_external_result=False,
        )

    def _wait_for_external_result(
        self,
        point: RouteMeasurementPoint,
        position: int,
    ) -> dict[str, Any]:
        self._begin_external_measurement_request()
        message = (
            f"Route API session: point {position}/{self._total} "
            f"{point.label} waiting for external measurement."
        )
        self._status(message)
        return self._wait_for_decision(
            state="waiting_external_measurement",
            reason="external_measurement",
            message=message,
            allow_external_result=True,
        )

    def _begin_external_measurement_request(self) -> int:
        with self._condition:
            self._external_measurement_request_id += 1
            self._pending_external_result = None
            self._condition.notify_all()
            return self._external_measurement_request_id

    def _wait_before_first_point_decision(
        self,
        point: RouteMeasurementPoint,
        position: int,
    ) -> dict[str, Any]:
        message = (
            f"Route API session: ready at point {position}/{self._total} "
            f"{point.label}."
        )
        self._status(message)
        return self._wait_for_decision(
            state="waiting_paused",
            reason="paused",
            message=message,
            allow_external_result=False,
        )

    def _wait_if_pause_requested(
        self,
        point: RouteMeasurementPoint,
        position: int,
    ) -> dict[str, Any]:
        with self._condition:
            requested = self._pause_requested
            self._pause_requested = False
        if not requested:
            return {"action": "next"}
        message = (
            f"Route API session: paused after point {position}/{self._total} "
            f"{point.label}."
        )
        self._status(message)
        return self._wait_for_decision(
            state="waiting_paused",
            reason="paused",
            message=message,
            allow_external_result=False,
        )

    def _wait_for_decision(
        self,
        *,
        state: str,
        reason: str,
        message: str,
        allow_external_result: bool,
    ) -> dict[str, Any]:
        self._set_state(state, waiting_reason=reason, message=message)
        self._set_waiting(True)
        try:
            with self._condition:
                while True:
                    if self._stop_requested:
                        return {"action": "stop"}
                    if allow_external_result and self._pending_external_result is not None:
                        result = self._pending_external_result
                        self._pending_external_result = None
                        return {"result": result}
                    if self._pending_action is not None:
                        action = self._pending_action
                        self._pending_action = None
                        return {"action": action}
                    self._condition.wait(timeout=0.2)
        finally:
            self._set_waiting(False)

    def _handle_attention_decision(
        self,
        decision: dict[str, Any],
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> int | None:
        if "result" in decision:
            return self._handle_external_result(
                dict(decision["result"]),
                point,
                position,
                total,
                preparation=self._last_preparation,
            )
        action = self._decision_action(decision)
        if action == "stop":
            self.stop()
            return len(self._points)
        if action == "interrupt":
            self._contact_runner.clear_current_point_correction_request()
            self._lift_needles(position, total)
            return position - 1
        if action == "skip":
            self._append_history(
                point,
                position,
                "skipped",
                preparation=self._last_preparation,
                external_result=None,
            )
            self._lift_needles(position, total)
            return None
        if action == "measure":
            followup = self._wait_for_external_result(point, position)
            return self._handle_attention_decision(followup, point, position, total)
        if action == "seek":
            preparation = self._seek_current_contact(point, position, total)
            self._store_preparation(preparation)
            record = preparation.placement.record
            self._emit_result(record, position, total, record.status in {"ok", "short"})
            if record.status == "short":
                self._append_history(
                    point,
                    position,
                    "short",
                    preparation=preparation,
                    external_result=None,
                )
                self._lift_needles(position, total)
                return None
            if record.status == "ok":
                followup = self._wait_for_external_result(point, position)
                return self._handle_attention_decision(followup, point, position, total)
            return self._handle_attention_decision(
                self._wait_for_contact_attention(point, position),
                point,
                position,
                total,
            )
        if action == "remeasure":
            self._lift_needles(position, total)
            return position - 1
        jump = self._jump_index(action)
        if jump is not None:
            self._lift_needles(position, total)
            return jump
        self._lift_needles(position, total)
        return None

    def _handle_external_result(
        self,
        external_result: dict[str, Any],
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        preparation: RouteExternalContactPreparation | None,
    ) -> int | None:
        self._last_external_result = dict(external_result)
        if not self._external_result_succeeded(external_result):
            return self._wait_after_external_result_failed(
                external_result,
                point,
                position,
                total,
            )
        self._append_history(
            point,
            position,
            str(external_result.get("status") or "ok"),
            preparation=preparation,
            external_result=external_result,
        )
        self._lift_needles(position, total)
        pause_decision = self._wait_if_pause_requested(point, position)
        return self._handle_post_point_decision(pause_decision)

    def _wait_after_external_result_failed(
        self,
        external_result: dict[str, Any],
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> int | None:
        message_text = str(external_result.get("message") or "").strip()
        status_text = str(external_result.get("status") or "failed").strip() or "failed"
        detail = f": {message_text}" if message_text else "."
        message = (
            f"Route API session: point {position}/{self._total} "
            f"{point.label} external measurement {status_text}{detail}"
        )
        self._status(message)
        self._begin_external_measurement_request()
        decision = self._wait_for_decision(
            state="waiting_external_measurement",
            reason="external_measurement_failed",
            message=message,
            allow_external_result=True,
        )
        return self._handle_attention_decision(decision, point, position, total)

    @staticmethod
    def _external_result_succeeded(external_result: dict[str, Any]) -> bool:
        status = str(external_result.get("status") or "ok").strip().lower()
        return status in {"", "ok", "success", "complete", "completed"}

    def _handle_interrupted_decision(
        self,
        decision: dict[str, Any],
        position: int,
        total: int,
    ) -> int | None:
        action = self._decision_action(decision)
        if action == "stop":
            self.stop()
            return len(self._points)
        jump = self._jump_index(action)
        if action == "skip":
            jump = position
        self._contact_runner.clear_current_point_correction_request()
        self._lift_needles(position, total)
        return jump

    def _handle_post_point_decision(self, decision: dict[str, Any]) -> int | None:
        action = self._decision_action(decision, default="next")
        if action == "stop":
            self.stop()
            return len(self._points)
        if action == "remeasure":
            return max(0, self._position - 1)
        jump = self._jump_index(action)
        return jump

    def _seek_current_contact(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> RouteExternalContactPreparation:
        placement = self._contact_runner.seek_contact(
            point,
            position=position,
            total=total,
        )
        return RouteExternalContactPreparation(
            placement=placement,
            photo_path=None,
            focus=None,
        )

    def _lift_needles(self, position: int, total: int) -> None:
        try:
            self._contact_runner.lift_needles_after_external_measurement(
                position=position,
                total=total,
            )
        except Exception:
            logger.exception("Route API session failed to lift needles.")
            raise

    def _jump_index(self, action: str) -> int | None:
        if not str(action).startswith("jump:"):
            return None
        try:
            point_number = int(str(action).split(":", 1)[1])
        except ValueError:
            return None
        index = self._contact_runner._index_for_point_number(point_number)
        return index

    def _stop_requested_now(self) -> bool:
        with self._condition:
            return bool(self._stop_requested)

    def _point_interrupted(self) -> bool:
        return bool(self._contact_runner._point_interrupt_requested.is_set())

    def _set_current(self, position: int, point: RouteMeasurementPoint) -> None:
        with self._condition:
            self._position = int(position)
            self._current_point = point
            self._condition.notify_all()

    def _store_preparation(
        self,
        preparation: RouteExternalContactPreparation,
    ) -> None:
        with self._condition:
            self._last_preparation = preparation

    def _append_history(
        self,
        point: RouteMeasurementPoint,
        position: int,
        status: str,
        *,
        preparation: RouteExternalContactPreparation | None,
        external_result: dict[str, Any] | None,
    ) -> None:
        entry = {
            "position": int(position),
            "contact": self._point_payload(point),
            "status": str(status),
            "preparation": self._preparation_payload(preparation),
            "external_result": external_result,
        }
        with self._condition:
            self._history.append(entry)

    def _set_state(
        self,
        state: str,
        *,
        waiting_reason: str | None = None,
        message: str | None = None,
    ) -> None:
        with self._condition:
            self._state = str(state)
            if waiting_reason is not None:
                self._waiting_reason = str(waiting_reason)
            if message is not None:
                self._message = str(message)
            self._condition.notify_all()

    def _set_waiting(self, waiting: bool) -> None:
        if self._waiting_callback is not None:
            self._waiting_callback(bool(waiting))

    def _status(self, message: str) -> None:
        self._set_state(self._state, message=message)
        if self._status_callback is not None:
            self._status_callback(message)

    def _emit_progress(self, position: int, total: int, point_number: int) -> None:
        if self._progress_callback is not None:
            self._progress_callback(position, total, point_number)

    def _emit_result(
        self,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        saved: bool,
    ) -> None:
        if self._result_callback is not None:
            self._result_callback(record, position, total, saved)

    @staticmethod
    def _point_payload(point: RouteMeasurementPoint | None) -> dict[str, Any] | None:
        if point is None:
            return None
        return {
            "contact_number": _structure_number_for_point(point),
            "point_index": int(point.index),
            "point_id": point.point_id,
            "label": point.label,
            "design_center": list(point.design_center),
            "stage_xy": list(point.stage_xy),
        }

    @classmethod
    def _preparation_payload(
        cls,
        preparation: RouteExternalContactPreparation | None,
    ) -> dict[str, Any] | None:
        if preparation is None:
            return None
        placement = preparation.placement
        return {
            "success": bool(placement.success),
            "message": placement.message,
            "contact": cls._point_payload(placement.point),
            "measurement": _route_measurement_record_payload(placement.record),
            "contact_seek": _route_contact_seek_payload(placement.contact_seek),
            "photo_artifact_id": preparation.photo_path,
            "focus": preparation.focus,
        }

