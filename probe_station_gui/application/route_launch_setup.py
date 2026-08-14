from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication

from probe_station_gui.application.route_run_execution import RouteRunKind
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateAdapterCompletion,
    DesignCoordinateLease,
    FirstContactRequest,
    PhysicalAReadResult,
    ReadPhysicalAIntent,
)
from probe_station_gui.route.dialog_adapter import (
    route_measurement_session_cancel_plan,
    route_measurement_session_start_plan,
)
from probe_station_gui.route.gui_measurement_adapter import (
    GuiRouteEventBindings,
    setup_gui_route_meter,
)
from probe_station_gui.route.measurement import (
    RouteMeasurementPoint,
    RouteMeasurementRunner,
)
from probe_station_gui.route.operation import (
    RouteMeasurementStartPlan,
    route_measurement_start_decision,
)
from probe_station_gui.route.session_start import (
    GuiRouteLaunchState,
    GuiRouteStartPreflight,
    gui_route_camera_frame_preflight,
    gui_route_launch_state,
    gui_route_start_availability,
    gui_route_start_preflight,
    snapshot_route_design_frame,
)
from probe_station_gui.route.telegram_adapter import route_start_telegram_text
from probe_station_gui.views import main_window_coordinate_flow as coordinate_flow

logger = logging.getLogger("main")

if TYPE_CHECKING:
    from probe_station_gui.route.measurement_config import (
        RouteMeasurementRunConfiguration,
    )


@dataclass
class _DesignContactArmDispatch:
    request: FirstContactRequest
    read_intent: ReadPhysicalAIntent | None = None


class _MainRouteLaunchSetupMixin:
    def _start_route_measurement_session(self) -> None:
        execution = self._route_run_execution.snapshot()
        configuration = (
            self._route_measurement_dialog.current_configuration()
            if self._route_measurement_dialog is not None
            else None
        )
        plan = route_measurement_session_start_plan(
            thread_active=execution.thread_alive,
            dialog_configuration=configuration,
            current_point=self._route_measurement_current_point,
        )
        if not plan.accepted:
            self._show_status(plan.status_message, plan.status_timeout_ms)
            return
        self._route_measurement_session_active = plan.session_active
        self._set_route_measurement_resume_point(plan.point_number)
        self._set_route_measurement_pending(plan.pending)
        self._save_route_measurement_session_metadata(configuration)
        self._show_route_dialog_status(plan.status_message, plan.status_timeout_ms)

    def _cancel_route_measurement_session(self) -> None:
        execution = self._route_run_execution.snapshot()
        plan = route_measurement_session_cancel_plan(
            thread_active=execution.thread_alive
        )
        if not plan.accepted:
            self._show_status(plan.status_message, plan.status_timeout_ms)
            return
        self._pending_route_measure_point = None
        self._route_measurement_session_active = plan.session_active
        self._set_route_measurement_resume_point(plan.point_number)
        self._set_route_measurement_pending(plan.pending)
        self._show_route_dialog_status(plan.status_message, plan.status_timeout_ms)

    def _start_route_measurement(
        self,
        configuration: RouteMeasurementRunConfiguration,
        *,
        wait_before_first_point: bool = False,
    ) -> None:
        execution = self._route_run_execution.snapshot()
        availability = gui_route_start_availability(
            route_thread_active=execution.thread_alive,
            serial_connected=self._stage_serial_ready(),
        )
        if not self._apply_gui_route_start_preflight(availability):
            return
        start_plan = self._route_measurement_start_plan(configuration)
        if start_plan is None:
            return
        points = start_plan.points
        selected_point = start_plan.selected_point
        self._prepare_route_measurement_launch(configuration, selected_point)
        launch_state = gui_route_launch_state(
            operation_mode=configuration.operation_mode,
            points=points,
            selected_point=selected_point,
            wait_before_first_point=wait_before_first_point,
            previous_ok_skipped_count=start_plan.previous_ok_skipped_count,
        )
        if not self._route_measurement_photo_preflight(
            launch_state,
            photo_autofocus_enabled=configuration.photo_autofocus_enabled,
            wait_before_first_point=wait_before_first_point,
        ):
            return
        route_lcr_controller = setup_gui_route_meter(
            controller=self.lcr_controller,
            configuration=configuration.meter,
            measure_enabled=launch_state.measure_enabled,
            show_status=self._show_status,
            presenter=self._route_runtime_presenter(),
        )
        if route_lcr_controller is None:
            return
        runner = self._build_route_measurement_runner(
            configuration,
            points=points,
            route_lcr_controller=route_lcr_controller,
            wait_before_first_point=wait_before_first_point,
            design_frame_snapshot=start_plan.design_frame_snapshot,
        )
        self._start_route_measurement_runner(
            runner,
            launch_state,
            configuration=configuration,
            point_count=len(points),
        )

    def _prepare_route_measurement_launch(
        self,
        configuration: RouteMeasurementRunConfiguration,
        selected_point: RouteMeasurementPoint,
    ) -> None:
        self._set_route_measurement_resume_point(int(selected_point.index))
        if not self._route_measurement_session_active:
            self._route_measurement_session_active = True
            self._set_route_measurement_pending(True)
        self._route_measurement_runtime_configuration = configuration
        self._save_route_measurement_session_metadata(configuration)

    def _apply_gui_route_start_preflight(
        self,
        preflight: GuiRouteStartPreflight,
    ) -> bool:
        if preflight.accepted:
            return True
        self._show_status(preflight.message, preflight.timeout_ms)
        if preflight.telegram_failure_text:
            self._telegram_runtime.send_alert(
                "route_failed",
                preflight.telegram_failure_text,
                attach_photo=preflight.attach_failure_photo,
            )
        if preflight.dialog_status:
            self._route_runtime_presenter().set_status(preflight.message)
        return False

    def _route_measurement_photo_preflight(
        self,
        launch_state: GuiRouteLaunchState,
        *,
        photo_autofocus_enabled: bool,
        wait_before_first_point: bool,
    ) -> bool:
        scale = self._active_microscope_scale()
        preflight = gui_route_start_preflight(
            photo_enabled=launch_state.photo_enabled,
            photo_autofocus_enabled=photo_autofocus_enabled,
            wait_before_first_point=wait_before_first_point,
            objective_scale_available=scale is not None,
        )
        if not self._apply_gui_route_start_preflight(preflight):
            return False
        if not preflight.check_camera_frame:
            return True
        frame, _counter = self._wait_for_camera_frame(timeout_s=0.1)
        camera_preflight = gui_route_camera_frame_preflight(
            photo_enabled=launch_state.photo_enabled,
            photo_autofocus_enabled=photo_autofocus_enabled,
            camera_frame_available=frame is not None,
        )
        return self._apply_gui_route_start_preflight(camera_preflight)

    def _build_route_measurement_runner(
        self,
        configuration: RouteMeasurementRunConfiguration,
        *,
        points: list[RouteMeasurementPoint],
        route_lcr_controller: object,
        wait_before_first_point: bool,
        design_frame_snapshot: object | None = None,
    ) -> RouteMeasurementRunner:
        runner: RouteMeasurementRunner | None = None

        def publish_waiting(waiting: bool) -> None:
            if runner is not None:
                self.route_measurement_waiting_changed.emit(runner, waiting)

        callbacks = GuiRouteEventBindings(
            status=self.route_measurement_status.emit,
            progress=self.route_measurement_progress.emit,
            record=self.route_measurement_recorded.emit,
            capture_photo=self._capture_route_photo,
            autofocus=self._route_photo_autofocus,
            photo_record=self._record_route_photo,
            contact_height=self._record_route_contact_height,
            contact_photo=self._capture_route_contact_photo,
            pre_contact_photo=self._capture_route_pre_contact_photo,
            result=self.route_measurement_result.emit,
            waiting=publish_waiting,
        ).events()
        runner = RouteMeasurementRunner(
            points=points,
            csv_path=configuration.csv_path,
            stage_controller=self.stage_controller,
            lcr_controller=route_lcr_controller,
            needle_feedrate=self._current_needle_feedrate(),
            measurement_count=configuration.measurement_count,
            initial_measurement_count=configuration.initial_measurement_count,
            start_point_number=configuration.start_point,
            max_relative_rms=configuration.max_relative_rms,
            contact_quality_limits=configuration.contact_quality_limits,
            confirm_each_point=True,
            auto_next_ok_or_short=True,
            auto_contact_seek_on_bad_contact=True,
            auto_contact_seek_step_mm=configuration.contact_seek_step_mm,
            auto_contact_seek_max_total_mm=configuration.contact_seek_range_mm,
            contact_settle_s=configuration.contact_settle_s,
            nplc_label=configuration.meter.nplc_label(),
            measurement_type=configuration.meter.measurement_type_label(),
            operation_mode=configuration.operation_mode,
            photo_settle_s=configuration.photo_settle_s,
            photo_focus_enabled=configuration.photo_autofocus_enabled,
            photo_focus_range_mm=configuration.photo_autofocus_range_mm,
            photo_output_dir=configuration.photo_output_dir,
            wait_before_first_point=wait_before_first_point,
            events=callbacks,
            design_frame_snapshot=design_frame_snapshot,
            post_success_contact=self._design_contact_success_callback(
                design_frame_snapshot
            ),
        )
        return runner

    def _start_route_measurement_runner(
        self,
        runner: RouteMeasurementRunner,
        launch_state: GuiRouteLaunchState,
        *,
        configuration: RouteMeasurementRunConfiguration,
        point_count: int,
    ) -> None:
        thread = threading.Thread(
            target=self._run_route_measurement,
            args=(runner,),
            name="RouteMeasurement",
            daemon=True,
        )
        self._route_run_execution.activate(
            runner,
            thread,
            kind=RouteRunKind.GUI,
        )
        self._pending_route_measure_point = None
        self._route_measurement_photo_enabled = launch_state.photo_enabled
        self._route_measurement_measure_enabled = launch_state.measure_enabled
        presentation = launch_state.presentation
        self._route_measurement_point_numbers = presentation.point_numbers
        self._telegram_runtime.route_photos.reset_for_route_start()
        self._set_route_measurement_pending(True)
        start_message = presentation.message
        self._route_runtime_presenter().route_runner_started(start_message, point_count)
        self._show_status(start_message)
        self._last_route_measurement_result = None
        if launch_state.send_start_telegram:
            self._telegram_runtime.send_alert(
                "route_started",
                route_start_telegram_text(
                    start_message,
                    str(configuration.csv_path),
                ),
            )
        thread.start()
        self._update_stage_coordinate_apply_state()

    def _route_measurement_start_plan(
        self,
        configuration: RouteMeasurementRunConfiguration,
    ) -> RouteMeasurementStartPlan | None:
        frame_usability = self._coordinate_system_coordinator.current_design_lease()
        if not frame_usability.usable:
            self._show_status(
                str(
                    frame_usability.rejection_reason
                    or "Design coordinate frame is unavailable."
                ),
                6000,
            )
            return None
        route = self._design_session.route
        frame_snapshot = self._snapshot_active_route_design_frame(frame_usability)
        decision = route_measurement_start_decision(
            route=route,
            registration_valid=frame_usability.usable,
            points_factory=lambda selected_route: self._route_measurement_points(
                selected_route,
                frame_usability_snapshot=frame_usability,
            ),
            current_point=configuration.current_point,
            previous_ok_only=configuration.previous_ok_only,
            previous_csv_path=configuration.previous_csv_path,
            structure_number_for_point=self._api_structure_number_for_measurement_point,
            design_frame_snapshot=frame_snapshot,
        )
        if (
            decision.accepted
            and not self._coordinate_system_coordinator.design_lease_is_current(
                frame_usability
            )
        ):
            self._show_status(
                "Design coordinate frame changed before route start.",
                6000,
            )
            return None
        if decision.accepted:
            self._active_route_design_frame_snapshot = (
                decision.plan.design_frame_snapshot
                if decision.plan is not None
                else None
            )
            return decision.plan
        if decision.dialog_status:
            self._show_route_runtime_status(decision.message, decision.timeout_ms)
        else:
            self._show_status(decision.message, decision.timeout_ms)
        return None

    def _snapshot_active_route_design_frame(
        self,
        usability: DesignCoordinateLease | None = None,
    ):
        active = usability or self._coordinate_system_coordinator.current_design_lease()
        if not active.usable:
            return None
        return snapshot_route_design_frame(
            frame_id=active.frame_id,
            frame_version=active.frame_version,
        )

    def _design_contact_success_callback(self, frame_snapshot: object | None):
        frame_id = getattr(frame_snapshot, "frame_id", None)
        frame_version = getattr(frame_snapshot, "frame_version", None)
        if frame_id is None or frame_version is None:
            return None
        read = _MainRouteLaunchSetupMixin._request_design_contact_arm(
            self, FirstContactRequest(str(frame_id), int(frame_version))
        )
        if read is None:
            return None

        def capture(_placement: object):
            try:
                coordinates = self.stage_controller.run_external_current_physical_machine_coordinates(
                    ("A",)
                )
                physical_a = float(coordinates["A"])
                result = PhysicalAReadResult(
                    read.intent_id,
                    succeeded=True,
                    physical_a_mm=physical_a,
                )
            except (KeyError, TypeError, ValueError, RuntimeError) as exc:
                logger.exception(
                    "Unable to read physical A for Design contact reference"
                )
                result = PhysicalAReadResult(
                    read.intent_id,
                    succeeded=False,
                    message=str(exc),
                )

            def finalize() -> None:
                self.design_contact_a_read_finished.emit(result)

            return finalize

        return capture

    def _request_design_contact_arm(
        self,
        request: FirstContactRequest,
    ) -> ReadPhysicalAIntent | None:
        signal = getattr(self, "design_contact_arm_requested", None)
        application = QApplication.instance()
        if (
            signal is None
            or application is None
            or QThread.currentThread() == application.thread()
        ):
            return _MainRouteLaunchSetupMixin._arm_design_contact_on_gui(self, request)
        dispatch = _DesignContactArmDispatch(request)
        signal.emit(dispatch)
        return dispatch.read_intent

    def _on_design_contact_arm_requested(
        self,
        dispatch: object,
    ) -> None:
        if not isinstance(dispatch, _DesignContactArmDispatch):
            return
        dispatch.read_intent = _MainRouteLaunchSetupMixin._arm_design_contact_on_gui(
            self,
            dispatch.request,
        )

    def _arm_design_contact_on_gui(
        self,
        request: FirstContactRequest,
    ) -> ReadPhysicalAIntent | None:
        transition = self._coordinate_system_coordinator.arm_first_contact(request)
        coordinate_flow.apply_coordinate_transition(self, transition)
        return next(
            (
                intent
                for intent in transition.intents
                if isinstance(intent, ReadPhysicalAIntent)
            ),
            None,
        )

    def _on_design_contact_a_read_finished(self, result: object) -> None:
        if not isinstance(result, PhysicalAReadResult):
            return
        completed = self._coordinate_system_coordinator.complete(
            CoordinateAdapterCompletion(result.intent_id, result)
        )
        coordinate_flow.apply_coordinate_transition(self, completed)
