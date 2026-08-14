from __future__ import annotations

import csv
import logging
import os
import threading
from pathlib import Path

from probe_station_gui.camera.imaging import utc_timestamp
from probe_station_gui.camera.microscope_artifacts import save_microscope_image
from probe_station_gui.coordinates.coordinator_model import DesignCoordinateLease
from probe_station_gui.design import objective_offsets as offsets
from probe_station_gui.design.model import DesignModelError
from probe_station_gui.instruments.meters.lcr_session_backend import LCRMeterError
from probe_station_gui.notifications.telegram import (
    telegram_route_attention_alert_enabled,
)
from probe_station_gui.route.adjustment_flow import route_contact_move_plan
from probe_station_gui.route.artifact_rows import (
    ROUTE_CONTACT_HEIGHT_MAP_FIELDS,
    route_contact_height_map_path,
    route_contact_height_map_row,
)
from probe_station_gui.route.confirmation_flow import (
    route_confirmation_runtime_plan,
    route_confirmation_submission_plan,
)
from probe_station_gui.route.dialog_adapter import (
    route_measurement_setup_changed,
)
from probe_station_gui.application.route_run_execution import (
    RouteRunKind,
    RouteRunReleaseCause,
    RouteRunReleaseRequest,
)
from probe_station_gui.route.measurement import (
    RouteContactHeightRecord,
    RouteMeasurementPoint,
    RouteMeasurementRecord,
    RouteMeasurementRunner,
    RoutePhotoRecord,
)
from probe_station_gui.route.model import MeasurementRoute
from probe_station_gui.route.operation import route_measurement_points_for_route
from probe_station_gui.route.point_execution import PointPhotoSettings
from probe_station_gui.route.telegram_adapter import (
    capture_route_photo,
    route_photo_focus_payload,
)
from probe_station_gui.stage.controller import StageControllerError

logger = logging.getLogger("main")


class _MainRouteCaptureRunMixin:
    def _route_measurement_points(
        self,
        route: MeasurementRoute,
        *,
        frame_usability_snapshot: DesignCoordinateLease | None = None,
    ) -> list[RouteMeasurementPoint]:
        usability = (
            frame_usability_snapshot
            or self._coordinate_system_coordinator.current_design_lease()
        )
        if not usability.usable:
            raise DesignModelError(
                usability.rejection_reason or "Design coordinate frame is unavailable."
            )
        objective_settings = self.settings_manager.objectives_configuration()
        base_offset, active_offset = offsets.base_and_active_objective_offsets(
            objective_settings
        )
        return route_measurement_points_for_route(
            route,
            stage_from_design=lambda design_xy: (
                self._coordinate_system_coordinator.project_design_to_camera_stage(
                    usability,
                    design_xy,
                )
            ),
            contact_objective_offset=base_offset,
            photo_objective_offset=active_offset,
        )

    def _capture_route_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        settings: PointPhotoSettings,
        focus_result: object | None = None,
    ) -> str:
        route = self._design_session.route
        return capture_route_photo(
            point,
            position,
            total,
            photo_only_mode=settings.photo_only_mode,
            photo_output_dir=settings.output_dir,
            photo_autofocus_enabled=settings.autofocus_enabled,
            photo_autofocus_range_mm=settings.autofocus_range_mm,
            route_name=(route.name if route is not None else "route"),
            focus_result=focus_result,
            active_microscope_scale=self._active_microscope_scale,
            latest_camera_counter=self._latest_camera_counter,
            wait_for_camera_frame=self._wait_for_camera_frame,
            timestamp_utc=utc_timestamp,
            active_objective_metadata=self._active_objective_metadata,
            stage_position_for_image_metadata=self._stage_position_for_image_metadata,
            save_image=save_microscope_image,
            route_photo_focus_payload=route_photo_focus_payload,
        )

    def _route_photo_autofocus(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        settings: PointPhotoSettings,
    ) -> object:
        _ = point
        self.route_measurement_status.emit(
            "Route photo autofocus: "
            f"point {position}/{total}, "
            f"+/-{settings.autofocus_range_mm:.3f} mm."
        )
        return self.stage_controller.run_external_local_autofocus(
            range_mm=settings.autofocus_range_mm,
            parent_token=self._route_optical_session_token(),
        )

    def _route_optical_session_token(self) -> str:
        token = str(getattr(self, "_route_measurement_optical_session_token", "") or "")
        if not token:
            raise RuntimeError("Route optical session is unavailable.")
        return token

    def _record_route_photo(
        self,
        record: RoutePhotoRecord,
        position: int,
        total: int,
    ) -> None:
        self._telegram_runtime.route_photos.record_route_photo(
            record,
            position,
            total,
            route_name=self._current_route_name(),
            send_bot_message=self._telegram_runtime.send_bot_message,
            default_markup=(
                self._telegram_runtime.default_markup(
                    route_waiting=self._route_run_execution.snapshot().waiting
                )
            ),
        )

    def _capture_route_pre_contact_photo(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
    ) -> None:
        route_attention_enabled = telegram_route_attention_alert_enabled(
            self.settings_manager.telegram_configuration()
        )
        self._telegram_runtime.route_photos.capture_pre_contact_photo(
            point,
            position,
            total,
            structure_number=self._api_structure_number_for_measurement_point(point),
            route_attention_enabled=route_attention_enabled,
            latest_camera_counter=self._latest_camera_counter,
            wait_for_camera_frame=self._wait_for_camera_frame,
            qimage_telegram_photo=self._qimage_telegram_photo,
            latest_camera_frame_photo=self._latest_camera_frame_photo,
        )

    def _capture_route_contact_photo(
        self,
        point: RouteMeasurementPoint,
        record: RouteMeasurementRecord,
        position: int,
        total: int,
        saved: bool,
    ) -> None:
        contact_attention = self._route_record_needs_contact_attention(record)
        route_attention_enabled = telegram_route_attention_alert_enabled(
            self.settings_manager.telegram_configuration()
        )
        self._telegram_runtime.route_photos.capture_contact_photo(
            point,
            record,
            position,
            total,
            saved=bool(saved),
            contact_attention=bool(contact_attention),
            route_attention_enabled=route_attention_enabled,
            latest_camera_counter=self._latest_camera_counter,
            wait_for_camera_frame=self._wait_for_camera_frame,
            qimage_telegram_photo=self._qimage_telegram_photo,
            latest_camera_frame_photo=self._latest_camera_frame_photo,
        )

    def _record_route_contact_height(
        self,
        record: RouteContactHeightRecord,
        position: int,
        total: int,
        *,
        csv_path: str | Path,
    ) -> None:
        try:
            path = route_contact_height_map_path(csv_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            exists = path.exists() and path.stat().st_size > 0
            with path.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=ROUTE_CONTACT_HEIGHT_MAP_FIELDS,
                )
                if not exists:
                    writer.writeheader()
                writer.writerow(
                    route_contact_height_map_row(
                        record,
                        position,
                        total,
                        route_name=self._current_route_name(),
                    )
                )
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            logger.warning("Unable to write route contact height map: %s", exc)

    def _current_route_name(self) -> str:
        route = getattr(getattr(self, "_design_session", None), "route", None)
        if route is None:
            return ""
        return route.name

    def _run_route_measurement(self, runner: RouteMeasurementRunner) -> None:
        success = False
        message = "Route measurement failed."
        execution = self._route_run_execution.snapshot()
        external_result_session = (
            execution.runner is runner
            and execution.kind is RouteRunKind.EXTERNAL_RESULT_SESSION
        )
        self._route_measurement_optical_session_token = None
        try:
            if runner.requires_optical_session():
                with self._optical_session_manager.open(
                    "route photography"
                ) as optical_session:
                    self._route_measurement_optical_session_token = (
                        optical_session.token
                    )
                    success, message = runner.run()
            else:
                success, message = runner.run()
        except Exception as exc:
            message = str(exc) or type(exc).__name__
            self.route_measurement_status.emit(f"Route measurement failed: {message}")
        finally:
            self._route_measurement_optical_session_token = None
        csv_path = "" if external_result_session else str(runner.csv_path)
        self.route_measurement_finished.emit(runner, success, message, csv_path)

    def _on_route_measurement_started(
        self,
        message: str,
        total: int,
        start_point: int,
        open_controls: bool,
    ) -> None:
        self._set_route_measurement_resume_point(int(start_point))
        self._set_route_measurement_pending(True)
        if open_controls:
            self._show_route_measurement_dialog_for_api_session()
        total_points = max(0, int(total))
        execution = self._route_run_execution.snapshot()
        self._route_runtime_presenter().route_started(
            message,
            total_points,
            waiting=execution.waiting,
            waiting_reason=execution.waiting_reason,
        )
        self._show_status(message)
        self._update_stage_coordinate_apply_state()

    def _request_stop_route_measurement(self) -> None:
        if self._api_route_control_state_snapshot().active:
            self._api_route_control_action({"action": "stop"})
            return
        runner = self._route_run_execution.snapshot().runner
        if runner is None:
            self._show_status("No route measurement is running.", 3000)
            return
        self._pending_route_measure_point = None
        runner.stop()
        self._show_status("Stopping route measurement.")
        self._update_stage_coordinate_apply_state()
        self._route_runtime_presenter().stop_requested("Stopping route measurement.")

    def _request_route_measurement_point_correction(
        self, pending_point_number: int | None = None
    ) -> None:
        if self._api_route_control_state_snapshot().active:
            self._interrupt_api_route_controlled_operation(
                "API route control interrupt requested."
            )
            return
        runner = self._route_run_execution.snapshot().runner
        if runner is None:
            self._show_status("No route measurement is running.", 3000)
            return
        if pending_point_number is None:
            self._pending_route_measure_point = None
        self._interrupt_route_measurement_runner(
            reason="Route measurement interrupt requested.",
            expected_runner=runner,
        )
        if pending_point_number is None:
            message = "Stopping contact measurement."
        else:
            message = (
                "Stopping contact measurement, then measuring "
                f"point {int(pending_point_number)}."
            )
        self._show_route_runtime_status(message, 5000)

    def _submit_route_measurement_confirmation(self, action: str) -> None:
        execution = self._route_run_execution.snapshot()
        runner = execution.runner
        move_thread = getattr(self, "_route_contact_move_thread", None)
        confirmation_plan = route_confirmation_submission_plan(
            action,
            api_route_control=self._api_route_control_state_snapshot(),
            runner_available=runner is not None,
            contact_move_active=move_thread is not None and move_thread.is_alive(),
            waiting=execution.waiting,
            pending_point_number=getattr(self, "_pending_route_measure_point", None),
        )
        if confirmation_plan.api_action is not None:
            self._api_route_control_action({"action": confirmation_plan.api_action})
            return
        if confirmation_plan.message:
            self._show_status(
                confirmation_plan.message,
                confirmation_plan.timeout_ms,
            )
            return
        if self._route_measurement_dialog is not None:
            runner = self._apply_route_measurement_confirmation_runtime(runner)
            if runner is None:
                return
        confirmation = confirmation_plan.confirmation
        if confirmation is None:
            return
        if not runner.submit_confirmation(confirmation.action):
            self._show_status("Unknown route measurement action.", 3000)
            return
        if confirmation.replaced_pending_point:
            self._pending_route_measure_point = None
        self._route_runtime_presenter().clear_waiting()
        self._show_status(f"Route measurement: {confirmation.status_label}.")

    def _apply_route_measurement_confirmation_runtime(self, runner):
        if self._route_measurement_dialog is None:
            return runner
        configuration = self._route_measurement_dialog.current_configuration()
        execution = self._route_run_execution.snapshot()
        external_session = (
            execution.runner is runner
            and execution.kind is RouteRunKind.EXTERNAL_RESULT_SESSION
        )
        runtime_plan = route_confirmation_runtime_plan(
            configuration,
            external_session=external_session,
            waiting=execution.waiting,
            setup_changed=route_measurement_setup_changed(
                self._route_measurement_runtime_configuration,
                configuration,
            ),
        )
        if runtime_plan.restart_required:
            route_offset_xy = (
                runner.route_offset_xy()
                if hasattr(runner, "route_offset_xy")
                else (0.0, 0.0)
            )
            release = self._route_run_execution.release(
                RouteRunReleaseRequest(
                    cause=RouteRunReleaseCause.TAKEOVER,
                    expected_runner=runner,
                    join_timeout_s=2.0,
                )
            )
            if not release.released:
                message = "Waiting route measurement did not stop."
                self._show_status(message, 8000)
                self._route_runtime_presenter().set_status(message)
                return None
            self._route_measurement_session_active = False
            self._pending_route_measure_point = None
            self._start_route_measurement(
                configuration,
                wait_before_first_point=True,
            )
            restarted = self._route_run_execution.snapshot()
            runner = restarted.runner
            if runner is None:
                return None
            if not hasattr(runner, "set_route_offset_xy"):
                return None
            runner.set_route_offset_xy(route_offset_xy)
            if runner.wait_until_waiting(timeout_s=10.0):
                self._route_run_execution.publish_waiting(
                    True,
                    expected_runner=runner,
                )
            else:
                runner.stop()
                thread = restarted.thread
                if thread is not None and thread.is_alive():
                    thread.join(timeout=2.0)
                message = "Route measurement did not reach waiting state."
                self._show_status(message, 8000)
                self._route_runtime_presenter().set_status(message)
                return None
        self._route_measurement_runtime_configuration = configuration
        self._save_route_measurement_session_metadata(configuration)
        runner.update_runtime_settings(**runtime_plan.runtime_settings)
        if not external_session and runtime_plan.meter_configuration_required:
            try:
                runner.apply_meter_configuration(configuration.meter)
            except LCRMeterError as exc:
                message = f"Route measurement instrument setup failed: {exc}"
                self._show_status(message, 8000)
                self._route_runtime_presenter().set_status(message)
                return None
        return runner

    def _submit_route_measurement_jump(self, point_number: int) -> None:
        self._submit_route_measurement_confirmation(f"jump:{int(point_number)}")

    def _request_route_contact_move(self, point_number: int) -> None:
        move_thread = getattr(self, "_route_contact_move_thread", None)
        execution = self._route_run_execution.snapshot()
        move_plan = route_contact_move_plan(
            contact_move_active=move_thread is not None and move_thread.is_alive(),
            route_active=execution.thread_alive,
            route_waiting=execution.waiting,
            api_route_control=self._api_route_control_state_snapshot(),
        )
        if move_plan.message:
            self._show_status(move_plan.message, move_plan.timeout_ms)
            return
        context_result = self._api_contact_context(int(point_number))
        if not context_result.get("accepted", False):
            message = str(
                context_result.get("message") or "Route contact move rejected."
            )
            self._show_route_runtime_status(message, 6000)
            return
        point = context_result["point"]
        if move_plan.set_resume_point:
            self._set_route_measurement_resume_point(int(point.index))
            runner = execution.runner
            if runner is not None and move_plan.set_adjustment_point:
                self._pending_route_measure_point = int(point.index)
                if hasattr(runner, "set_current_adjustment_point"):
                    runner.set_current_adjustment_point(int(point.index))
        needle_feedrate = self._current_needle_feedrate()
        message = f"Route contact move: point {int(point.index)} {point.label}."
        self._show_route_runtime_status(message, 5000)
        thread = threading.Thread(
            target=self._run_route_contact_move,
            args=(point, needle_feedrate),
            name="RouteContactMove",
            daemon=True,
        )
        self._route_contact_move_thread = thread
        thread.start()
        self._update_stage_coordinate_apply_state()

    def _run_route_contact_move(
        self, point: RouteMeasurementPoint, needle_feedrate: float | None
    ) -> None:
        success = False
        try:
            with self.stage_controller.reserve_external_task("route contact move"):
                self.route_measurement_status.emit(
                    f"Route contact move: point {int(point.index)} {point.label}, "
                    "raising needles."
                )
                self.stage_controller.run_external_needles_action(
                    "raise",
                    needle_feedrate,
                )
                self.route_measurement_status.emit(
                    f"Route contact move: point {int(point.index)} {point.label}, moving."
                )
                target_xy = self._api_route_adjusted_stage_xy(point)
                self.stage_controller.run_external_move_to_xy(
                    target_xy[0],
                    target_xy[1],
                )
                success = True
                message = (
                    f"Route contact move complete: point {int(point.index)} "
                    f"{point.label}."
                )
        except StageControllerError as exc:
            message = f"Route contact move failed: {exc}"
        except Exception as exc:
            logger.exception("Route contact move failed")
            message = f"Route contact move failed: {exc}"
        self.route_contact_move_finished.emit(success, message)

    def _on_route_contact_move_finished(self, success: bool, message: str) -> None:
        thread = getattr(self, "_route_contact_move_thread", None)
        if thread is not None and not thread.is_alive():
            thread.join(timeout=0.1)
        self._route_contact_move_thread = None
        self._update_stage_coordinate_apply_state()
        timeout_ms = 5000 if success else 8000
        self._show_route_runtime_status(message, timeout_ms)
