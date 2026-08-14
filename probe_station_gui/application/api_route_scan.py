"""Direct owner for the api route scan domain."""

from __future__ import annotations

import logging
import math
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from probe_station_gui.camera import microscope_scan
from probe_station_gui.application.route_run_execution import (
    RouteRunKind,
    RouteRunReleaseCause,
    RouteRunReleaseRequest,
)
from probe_station_gui.camera.microscope_scan_runtime_adapters import (
    MicroscopeAreaScanRequest,
)
from probe_station_gui.design.contact_navigation import (
    api_contact_context,
    api_route_adjusted_stage_xy,
)
from probe_station_gui.route.api_artifacts import (
    ApiRouteArtifactsStore,
    api_route_photo_artifact_metadata,
    api_route_photo_content_type,
    api_route_session_action_response,
    api_route_session_result_response,
    api_route_session_seek_response,
    api_route_session_status_response,
)
from probe_station_gui.route.measurement import (
    RouteExternalMeasurementSessionRunner,
    RouteMeasurementPoint,
    RouteMeasurementRunner,
)
from probe_station_gui.route.meter_config import (
    RouteMeterConfiguration,
    route_meter_configuration_from_payload,
)
from probe_station_gui.route.model import structure_number_from_labels
from probe_station_gui.route.payload_parsing import payload_float
from probe_station_gui.route.session_start import (
    RouteExternalSessionStartSettings,
    api_route_existing_session_response,
    api_route_session_launch_state,
    api_route_session_start_decision,
)
from probe_station_gui.route.telegram_adapter import (
    route_photo_focus_payload,
    route_requested_photo_caption,
    route_start_telegram_text,
)

logger = logging.getLogger("main")


class _MainApiRouteScanMixin:
    def _api_route_session_thread_preflight(
        self,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, tuple[float, float]]:
        execution = self._route_run_execution.snapshot()
        if not execution.thread_alive:
            if execution.active:
                self._route_run_execution.release(
                    RouteRunReleaseRequest(
                        cause=RouteRunReleaseCause.FINISHED,
                        expected_runner=execution.runner,
                        join_timeout_s=0.1,
                    )
                )
            return None, (0.0, 0.0)
        runner = execution.runner
        if execution.kind is RouteRunKind.EXTERNAL_RESULT_SESSION:
            return (
                api_route_existing_session_response(
                    payload=payload,
                    status=runner.status_payload(),
                ),
                (0.0, 0.0),
            )
        can_take_over_waiting_gui_runner = (
            execution.kind is RouteRunKind.GUI
            and execution.waiting
            and self._last_route_measurement_result is None
        )
        if not can_take_over_waiting_gui_runner:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Route measurement is already active.",
            }, (0.0, 0.0)
        route_offset_xy = runner.route_offset_xy()
        release = self._route_run_execution.release(
            RouteRunReleaseRequest(
                cause=RouteRunReleaseCause.TAKEOVER,
                expected_runner=runner,
                join_timeout_s=2.0,
            )
        )
        if not release.released:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Waiting GUI route measurement did not stop.",
            }, route_offset_xy
        self._route_measurement_session_active = False
        return None, route_offset_xy

    def _build_api_route_session_runner(
        self,
        *,
        session_id: str,
        points: list[RouteMeasurementPoint],
        selected_point: RouteMeasurementPoint,
        start_settings: RouteExternalSessionStartSettings,
        meter_configuration: RouteMeterConfiguration,
        needle_feedrate: float | None,
        design_frame_snapshot: object | None = None,
    ) -> RouteExternalMeasurementSessionRunner:
        runner: RouteExternalMeasurementSessionRunner | None = None

        def publish_waiting(waiting: bool) -> None:
            if runner is not None:
                self.route_measurement_waiting_changed.emit(runner, waiting)

        runner = RouteExternalMeasurementSessionRunner(
            session_id=session_id,
            points=points,
            stage_controller=self.stage_controller,
            lcr_controller=self._api_route_lcr_controller,
            needle_feedrate=needle_feedrate,
            measurement_count=start_settings.measurement_count,
            initial_measurement_count=start_settings.initial_measurement_count,
            start_point_number=int(selected_point.index),
            max_relative_rms=start_settings.max_relative_rms,
            contact_quality_limits=start_settings.contact_quality_limits,
            auto_contact_seek_step_mm=start_settings.contact_seek_step_mm,
            auto_contact_seek_max_total_mm=start_settings.contact_seek_range_mm,
            contact_settle_s=start_settings.contact_settle_s,
            nplc_label=meter_configuration.nplc_label(),
            measurement_type=meter_configuration.measurement_type_label(),
            status_callback=self.route_measurement_status.emit,
            progress_callback=self.route_measurement_progress.emit,
            photo_callback=self._capture_api_route_photo_artifact,
            photo_focus_callback=lambda point, position, total: (
                self._api_route_photo_autofocus(
                    point,
                    position,
                    total,
                    range_mm=start_settings.photo_focus_range_mm,
                )
            ),
            contact_photo_callback=self._capture_route_contact_photo,
            pre_contact_photo_callback=self._capture_route_pre_contact_photo,
            result_callback=self.route_measurement_result.emit,
            waiting_callback=publish_waiting,
            photo_enabled=start_settings.photo_enabled,
            photo_focus_enabled=start_settings.photo_focus_enabled,
            photo_settle_s=start_settings.photo_settle_s,
            wait_before_first_point=True,
            design_frame_snapshot=design_frame_snapshot,
            post_success_contact=self._design_contact_success_callback(
                design_frame_snapshot
            ),
        )
        return runner

    def _cleanup_failed_api_route_session_start(
        self,
        runner: RouteExternalMeasurementSessionRunner,
    ) -> dict[str, Any]:
        status = runner.status_payload()
        self._route_run_execution.release(
            RouteRunReleaseRequest(
                cause=RouteRunReleaseCause.FAILED_START,
                expected_runner=runner,
                join_timeout_s=2.0,
            )
        )
        self._api_route_lcr_controller = None
        self._route_measurement_session_active = False
        return status

    def _api_start_route_session(self, payload: dict[str, Any]) -> dict[str, Any]:
        active_response, route_offset_xy = self._api_route_session_thread_preflight(
            payload
        )
        if active_response is not None:
            return active_response
        if self.serial_connection is None or not self.serial_connection.is_open:
            return {
                "accepted": False,
                "status_code": 503,
                "message": "Serial connection is not available.",
            }
        frame_usability = self._coordinate_system_coordinator.current_design_lease()
        if not frame_usability.usable:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(
                    frame_usability.rejection_reason
                    or "Design coordinate frame is unavailable."
                ),
            }
        start_decision = api_route_session_start_decision(
            route=self._design_session.route,
            registration_valid=frame_usability.usable,
            payload=payload,
            current_point=int(self._route_measurement_current_point or 1),
            points_factory=lambda route: self._route_measurement_points(
                route,
                frame_usability_snapshot=frame_usability,
            ),
            default_contact_seek_range_mm=(
                RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM
            ),
            default_contact_seek_step_mm=RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM,
            default_contact_settle_s=RouteMeasurementRunner.DEFAULT_CONTACT_SETTLE_S,
            design_frame_snapshot=self._snapshot_active_route_design_frame(
                frame_usability
            ),
        )
        if not start_decision.accepted:
            return start_decision.rejection_payload()
        if not self._coordinate_system_coordinator.design_lease_is_current(
            frame_usability
        ):
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Design coordinate frame changed before route start.",
            }
        assert start_decision.plan is not None
        points = start_decision.plan.points
        selected_point = start_decision.plan.selected_point
        start_settings = start_decision.plan.start_settings
        self._active_route_design_frame_snapshot = (
            start_decision.plan.design_frame_snapshot
        )
        try:
            meter_configuration = self._api_route_meter_configuration(
                payload.get("meter", payload.get("meter_configuration", {})),
                voltages_v=None,
            )
        except ValueError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        setup_result = self._api_prepare_route_meter_controller(
            meter_configuration,
            prefix="Route measurement instrument setup failed",
        )
        if setup_result is not None:
            return setup_result
        route_lcr_controller: object = self.lcr_controller
        session_id = uuid.uuid4().hex
        self._api_route_artifacts_store().clear()
        self._api_route_session_id = session_id
        self._api_route_lcr_controller = route_lcr_controller
        self._api_route_last_status = None
        runner = self._build_api_route_session_runner(
            session_id=session_id,
            points=points,
            selected_point=selected_point,
            start_settings=start_settings,
            meter_configuration=meter_configuration,
            needle_feedrate=self._api_needle_feedrate(payload),
            design_frame_snapshot=start_decision.plan.design_frame_snapshot,
        )
        runner.set_route_offset_xy(route_offset_xy)
        launch_state = api_route_session_launch_state(
            session_id=session_id,
            points=points,
            selected_point=selected_point,
            photo_enabled=start_settings.photo_enabled,
        )
        thread = threading.Thread(
            target=self._run_route_measurement,
            args=(runner,),
            name="RouteApiExternalSession",
            daemon=True,
        )
        self._route_run_execution.activate(
            runner,
            thread,
            kind=RouteRunKind.EXTERNAL_RESULT_SESSION,
        )
        self._pending_route_measure_point = None
        self._route_measurement_session_active = True
        self._route_measurement_photo_enabled = launch_state.photo_enabled
        self._route_measurement_measure_enabled = launch_state.measure_enabled
        self._route_measurement_point_numbers = launch_state.point_numbers
        self._telegram_runtime.route_photos.reset_for_route_start()
        self._last_route_measurement_result = None
        thread.start()
        if not runner.wait_until_initial_pause(timeout_s=10.0):
            status = self._cleanup_failed_api_route_session_start(runner)
            return {
                "accepted": False,
                "status_code": 409,
                "message": (
                    status.get("message")
                    or "Route API session did not reach initial pause."
                ),
                "status": status,
            }
        self._route_run_execution.publish_waiting(True, expected_runner=runner)
        self.route_measurement_started.emit(
            launch_state.start_message,
            len(points),
            launch_state.selected_point_number,
            True,
        )
        self._telegram_runtime.send_alert(
            "route_started",
            route_start_telegram_text(
                launch_state.start_message,
                api_session=True,
            ),
        )
        return runner.status_payload()

    def _show_route_measurement_dialog_for_api_session(self) -> bool:
        try:
            self._open_route_measurement_dialog(start_context=False)
        except Exception:
            logger.exception(
                "Failed to open route measurement controls for API session."
            )
            return False
        return self._route_control_window_is_open()

    def _api_route_session_status(self) -> dict[str, Any]:
        execution = self._route_run_execution.snapshot()
        return api_route_session_status_response(
            execution.runner,
            self._api_route_last_status,
            self._api_route_artifacts_payload(),
        )

    def _api_route_session_action(self, payload: dict[str, Any]) -> dict[str, Any]:
        execution = self._route_run_execution.snapshot()
        return api_route_session_action_response(
            payload,
            runner=execution.runner,
            interrupt_runner=lambda runner, *, reason: (
                self._interrupt_route_measurement_runner(
                    reason=reason,
                    expected_runner=runner,
                )
            ),
        )

    def _api_route_session_result(self, payload: dict[str, Any]) -> dict[str, Any]:
        execution = self._route_run_execution.snapshot()
        return api_route_session_result_response(
            payload,
            runner=execution.runner,
            timestamp_utc=self._api_timestamp_utc(),
        )

    def _api_route_session_seek(self) -> dict[str, Any]:
        return api_route_session_seek_response(
            self._route_run_execution.snapshot().runner
        )

    def _api_route_session_artifact(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._api_route_artifacts_store().artifact_response(payload)

    def _api_lens_distortion_calibration(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if bool(payload.get("reset", False)):
            accepted, message = self._reset_lens_distortion_calibration()
            return {
                "accepted": accepted,
                "status_code": 200 if accepted else 409,
                "message": (
                    "Lens distortion calibration reset requested."
                    if accepted
                    else message
                ),
            }
        return self._start_lens_distortion_calibration()

    def _api_click_to_move_calibration(
        self,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        force = bool(payload.get("force", False))
        try:
            dx_px = float(payload.get("dx_px", 0.0))
            dy_px = float(payload.get("dy_px", 0.0))
        except (TypeError, ValueError):
            return {
                "accepted": False,
                "status_code": 400,
                "message": "dx_px and dy_px must be numeric.",
            }
        if not math.isfinite(dx_px) or not math.isfinite(dy_px):
            return {
                "accepted": False,
                "status_code": 400,
                "message": "dx_px and dy_px must be finite.",
            }
        if not self._stage_serial_ready():
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Connect the stage controller before calibration.",
            }
        if self._objective_mutation_busy():
            return {
                "accepted": False,
                "status_code": 409,
                "message": (
                    "Click-to-move calibration cannot start while a scan, "
                    "calibration, or stage task is active."
                ),
            }
        if force:
            reset, message = self._reset_click_calibration()
            if not reset:
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": message,
                }
        if not self._microscope_interaction.try_start_api_move(dx_px, dy_px):
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Click-to-move calibration not started.",
            }
        return {
            "accepted": True,
            "status_code": 202,
            "message": "Click-to-move calibration started.",
        }

    def _api_microscope_area_scan(self, payload: dict[str, Any]) -> dict[str, Any]:
        if "auto_exposure" in payload:
            return {
                "accepted": False,
                "status_code": 400,
                "message": "auto_exposure is no longer supported for area scans.",
            }
        for field in ("tile_approach_mm", "approach_mm"):
            if field in payload:
                return {
                    "accepted": False,
                    "status_code": 400,
                    "message": f"{field} is no longer supported for area scans.",
                }
        if self._microscope_scan_running():
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Microscope scan is already running.",
            }
        if not self._stage_serial_ready():
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Connect the stage controller before scanning.",
            }
        if self.stage_controller.is_busy():
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Stage is busy; microscope area scan not started.",
            }
        scale = self._active_microscope_scale()
        if scale is None:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Calibrate click-to-move for the active objective before scanning.",
            }
        try:
            scan_pattern = self._microscope_area_scan_pattern(payload)
            if scan_pattern == "stitch_debug":
                rows = 3
                columns = 3
                overlap_default = (
                    self.MICROSCOPE_AREA_SCAN_STITCH_DEBUG_OVERLAP_FRACTION
                )
                structure_size_mm = self._microscope_area_scan_float(
                    payload.get(
                        "structure_size_mm",
                        self.MICROSCOPE_AREA_SCAN_STITCH_DEBUG_STRUCTURE_MM,
                    ),
                    "structure_size_mm",
                    minimum=0.0,
                    maximum=100.0,
                )
                placement_fraction = self._microscope_area_scan_float(
                    payload.get(
                        "placement_fraction",
                        self.MICROSCOPE_AREA_SCAN_STITCH_DEBUG_PLACEMENT_FRACTION,
                    ),
                    "placement_fraction",
                    minimum=0.0,
                    maximum=1.0,
                )
            else:
                overlap_default = self.MICROSCOPE_AREA_SCAN_DEFAULT_OVERLAP_FRACTION
                rows = self._microscope_area_scan_count(
                    payload.get("rows", self.MICROSCOPE_AREA_SCAN_DEFAULT_ROWS),
                    "rows",
                )
                columns = self._microscope_area_scan_count(
                    payload.get("columns", self.MICROSCOPE_AREA_SCAN_DEFAULT_COLUMNS),
                    "columns",
                )
                if rows * columns > self.MICROSCOPE_AREA_SCAN_MAX_TILES:
                    raise ValueError(
                        f"Area scan is too large; maximum is {self.MICROSCOPE_AREA_SCAN_MAX_TILES} tiles."
                    )
                structure_size_mm = None
                placement_fraction = None
            overlap_fraction = self._microscope_area_scan_float(
                payload.get(
                    "overlap_fraction",
                    overlap_default,
                ),
                "overlap_fraction",
                minimum=0.0,
                maximum=0.95,
            )
            settle_s = self._microscope_area_scan_float(
                payload.get("settle_s", self.MICROSCOPE_AREA_SCAN_DEFAULT_SETTLE_S),
                "settle_s",
                minimum=0.0,
                maximum=10.0,
            )
            flat_field_options = microscope_scan.flat_field_options_from_payload(
                payload,
                default_enabled=True,
            )
            camera_lock_settings = microscope_scan.camera_lock_settings_from_payload(
                payload,
                default_enabled=True,
            )
        except ValueError as exc:
            return {
                "accepted": False,
                "status_code": 400,
                "message": str(exc),
            }

        output_dir = str(payload.get("output_dir") or "").strip()
        if not output_dir:
            output_dir = self._microscope_area_scan_default_output_dir()
        pixels_to_mm = getattr(scale, "pixels_to_mm", None)
        if scan_pattern == "stitch_debug":
            if pixels_to_mm is None:
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": "Stitch debug scan requires full pixel-to-stage calibration.",
                }
        design_lease = self._coordinate_system_coordinator.current_design_lease()
        try:
            launch_snapshot = self._capture_microscope_scan_launch_snapshot(
                scale=scale,
                document=design_lease.document,
                frame_usability_snapshot=(
                    design_lease if design_lease.usable else None
                ),
            )
        except (AttributeError, IndexError, TypeError, ValueError) as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": f"Microscope scan launch state is invalid: {exc}",
            }
        configuration = SimpleNamespace(
            output_dir=output_dir,
            overlap_fraction=overlap_fraction,
            settle_s=settle_s,
            scan_pattern=scan_pattern,
            refine_scale_from_overlaps=(scan_pattern != "stitch_debug"),
            structure_size_mm=structure_size_mm,
            placement_fraction=placement_fraction,
            flat_field_options=flat_field_options,
            camera_lock_settings=camera_lock_settings,
        )
        planning_request = MicroscopeAreaScanRequest(
            row_count=rows,
            column_count=columns,
            overlap_fraction=overlap_fraction,
            scan_pattern=scan_pattern,
            structure_size_mm=structure_size_mm,
            placement_fraction=placement_fraction,
        )
        self._microscope_scan_stop_requested.clear()
        thread = threading.Thread(
            target=self._run_microscope_scan,
            args=(configuration, planning_request, launch_snapshot),
            name="MicroscopeAreaScan",
            daemon=True,
        )
        self._microscope_scan_thread = thread
        try:
            thread.start()
        except Exception as exc:
            if self._microscope_scan_thread is thread:
                self._microscope_scan_thread = None
            self._update_stage_coordinate_apply_state()
            return {
                "accepted": False,
                "status_code": 500,
                "message": f"Microscope area scan could not start: {exc}",
            }
        self._update_stage_coordinate_apply_state()
        return {
            "accepted": True,
            "status_code": 202,
            "message": "Microscope area scan started.",
            "output_dir": output_dir,
            "rows": rows,
            "columns": columns,
            "pattern": scan_pattern,
        }

    @staticmethod
    def _microscope_area_scan_pattern(payload: dict[str, Any]) -> str:
        raw = payload.get("pattern", payload.get("scan_pattern", "grid"))
        text = str(raw or "grid").strip().lower().replace("-", "_")
        if text in {"grid", "area", "area_scan", "centered_area"}:
            return "grid"
        if text in {"stitch_debug", "stitching_debug", "debug_stitch"}:
            return "stitch_debug"
        raise ValueError("pattern must be 'grid' or 'stitch_debug'.")

    @classmethod
    def _microscope_area_scan_count(cls, value: object, label: str) -> int:
        try:
            count = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be a positive integer.") from exc
        if count <= 0:
            raise ValueError(f"{label} must be a positive integer.")
        if count > cls.MICROSCOPE_AREA_SCAN_MAX_TILES:
            raise ValueError(
                f"{label} is too large; maximum is {cls.MICROSCOPE_AREA_SCAN_MAX_TILES}."
            )
        return count

    @staticmethod
    def _microscope_area_scan_float(
        value: object,
        label: str,
        *,
        minimum: float,
        maximum: float,
    ) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label} must be numeric.") from exc
        if not math.isfinite(number) or number < minimum or number > maximum:
            raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}.")
        return number

    @staticmethod
    def _microscope_area_scan_default_output_dir() -> str:
        root = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return str(
            root / "ProbeStationGUI" / "MicroscopeScans" / f"area_scan_{timestamp}"
        )

    def _api_route_artifacts_payload(self) -> list[dict[str, object]]:
        return self._api_route_artifacts_store().public_payloads()

    def _api_route_artifacts_store(self) -> ApiRouteArtifactsStore:
        return ApiRouteArtifactsStore(
            artifacts=self._api_route_artifacts, lock=self._api_route_artifacts_lock
        )

    def _capture_api_route_photo_artifact(
        self,
        point: RouteMeasurementPoint,
        position: int,
        total: int,
        focus_result: object | None,
    ) -> str:
        before_counter = self._latest_camera_counter()
        frame, _counter = self._wait_for_camera_frame(
            after_counter=before_counter, timeout_s=2.0
        )
        photo = self._qimage_telegram_photo(frame) or self._latest_camera_frame_photo()
        if photo is None:
            raise RuntimeError("Camera frame is unavailable.")
        photo_bytes, photo_name = photo
        artifact_id = self._api_route_artifacts_store().add(
            data=photo_bytes,
            filename=photo_name,
            content_type=api_route_photo_content_type(photo_name),
            kind="route_photo",
            metadata=api_route_photo_artifact_metadata(
                point,
                position=position,
                total=total,
                contact_number=self._api_structure_number_for_measurement_point(point),
                focus_result=route_photo_focus_payload(focus_result),
            ),
            created_at_utc=self._api_timestamp_utc(),
        )
        if self._telegram_runtime.route_photos.consume_route_photo_request():
            self._telegram_runtime.send_bot_message(
                route_requested_photo_caption(
                    position=int(position),
                    total=int(total),
                    structure_number=self._api_structure_number_for_measurement_point(
                        point
                    ),
                    label=point.label,
                ),
                photo=(photo_bytes, photo_name),
                reply_markup=self._telegram_runtime.default_markup(
                    route_waiting=self._route_run_execution.snapshot().waiting
                ),
            )
        return artifact_id

    def _api_route_photo_autofocus(
        self,
        _point: RouteMeasurementPoint,
        position: int,
        total: int,
        *,
        range_mm: float,
    ) -> object:
        self.route_measurement_status.emit(
            f"Route photo autofocus: point {position}/{total}, +/-{range_mm:.3f} mm."
        )
        return self.stage_controller.run_external_local_autofocus(
            range_mm=range_mm,
            parent_token=self._route_optical_session_token(),
        )

    def _api_contact_context(self, contact_number: int) -> dict[str, Any]:
        return api_contact_context(
            int(contact_number),
            serial_available=bool(
                self.serial_connection is not None and self.serial_connection.is_open
            ),
            route=self._design_session.route,
            registration=(
                self._coordinate_system_coordinator.snapshot().registration.registration_projection
            ),
            points_factory=self._route_measurement_points,
            route_offset_xy=getattr(self, "_api_route_offset_xy", (0.0, 0.0)),
            structure_number_for_point=self._api_structure_number_for_measurement_point,
        )

    def _api_route_adjusted_stage_xy(
        self,
        point: RouteMeasurementPoint,
    ) -> tuple[float, float]:
        return api_route_adjusted_stage_xy(
            point,
            route_offset_xy=getattr(self, "_api_route_offset_xy", (0.0, 0.0)),
        )

    def _api_route_meter_configuration(
        self,
        payload: object,
        *,
        voltages_v: list[float] | None,
    ) -> RouteMeterConfiguration:
        settings_manager = getattr(self, "settings_manager", None)
        gwinstek_resource_name = None
        if settings_manager is not None:
            gwinstek_resource_name = (
                settings_manager.needle_calibration_configuration().visa_resource
            )
        return route_meter_configuration_from_payload(
            payload,
            voltages_v=voltages_v,
            current_meter_type=self.lcr_controller.meter_type(),
            default_gwinstek_resource_name=gwinstek_resource_name,
        )

    def _api_needle_feedrate(self, payload: dict[str, Any]) -> float | None:
        for key in ("needle_feedrate_mm_min", "feedrate_mm_min", "feedrate"):
            if key not in payload or payload.get(key) is None:
                continue
            value = payload_float(payload, key, default=math.nan, minimum=0.0)
            return max(self.MIN_FEEDRATE_MM_MIN, value)
        return float(
            self.settings_manager.needle_calibration_configuration().feedrate_mm_min
        )

    @staticmethod
    def _api_structure_number_for_measurement_point(
        point: RouteMeasurementPoint,
    ) -> int:
        return structure_number_from_labels(
            point.label,
            point.point_id,
            default=point.index,
        )

    @staticmethod
    def _api_structure_number_for_route_point(
        route_index: int,
        route_point: object,
    ) -> int:
        return structure_number_from_labels(
            getattr(route_point, "label", ""),
            getattr(route_point, "id", ""),
            default=route_index,
        )

    @staticmethod
    def _api_timestamp_utc() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    @classmethod
    def _api_json_ready(cls, value: object) -> Any:
        if isinstance(value, dict):
            return {str(key): cls._api_json_ready(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [cls._api_json_ready(item) for item in value]
        if isinstance(value, float):
            return value if math.isfinite(value) else None
        return value
