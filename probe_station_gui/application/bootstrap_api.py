"""Direct owner for the bootstrap api domain."""

from __future__ import annotations

import logging
import time
from typing import Any, Mapping

from PySide6.QtGui import QImage

from probe_station_gui import StageController
from probe_station_gui.api.command_dispatch import (
    GUI_STAGE_WORKER_ACTIONS,
    ApiBridgeRequestHandlers,
    ApiCommandDispatchHandlers,
)
from probe_station_gui.api.command_dispatch import (
    api_command_action_payload as command_dispatch_action_payload,
)
from probe_station_gui.api.command_dispatch import (
    dispatch_api_command_request as command_dispatch_request,
)
from probe_station_gui.api.command_dispatch import (
    handle_api_request as command_dispatch_handle_api_request,
)
from probe_station_gui.api.command_dispatch import (
    submit_api_command_request_from_api_thread as command_dispatch_from_api_thread,
)
from probe_station_gui.api.request_bridge import DeferredApiResponse
from probe_station_gui.api.server import ProbeStationApiServer
from probe_station_gui.camera.api_control import encode_camera_frame_png
from probe_station_gui.camera.auto_exposure import AutoExposureFrame
from probe_station_gui.camera.exposure_policy import ExposurePolicyController
from probe_station_gui.camera.exposure_policy_qt import ExposurePolicyQtAdapter
from probe_station_gui.camera.microscope_scan_runtime_adapters import (
    MicroscopeScanCameraAdapter,
)
from probe_station_gui.camera.optical_calibration_adapters import (
    OpticalCalibrationCameraAdapter,
    OpticalCalibrationEventAdapter,
    OpticalCalibrationRequestAdapter,
    OpticalCalibrationSessionAdapter,
    OpticalCalibrationStageAdapter,
    OpticalCalibrationStoreAdapter,
)
from probe_station_gui.camera.optical_calibration_runtime import (
    OpticalCalibrationRuntime,
)
from probe_station_gui.camera.optical_session import OpticalSessionManager
from probe_station_gui.notifications import telegram_commands
from probe_station_gui.notifications.telegram_runtime import TelegramCommandSnapshot
from probe_station_gui.route.api_window_guard import probe_route_api_requires_window

logger = logging.getLogger("main")


class _MainBootstrapApiMixin:
    def _start_camera_thread(self) -> None:
        if not self.thread.isRunning():
            self.thread.start()

    def _start_api_server(self) -> None:
        if self._api_server is None:
            return
        _started, message = self._api_server.start()
        if message:
            self._show_status(message, 5000)

    def _compose_camera_exposure_policy(self) -> None:
        self._exposure_policy_controller = ExposurePolicyController(
            initial_policy=self.settings_manager.exposure_policy_configuration(),
            software_once=self._camera_auto_exposure_controller.run,
            settings_read=self._camera_api_broker.read_settings,
            settings_write=self._camera_api_broker.write_settings_trusted,
            frame_read=self._read_camera_auto_exposure_frame,
            persist=self.settings_manager.set_exposure_policy_configuration,
        )
        self._optical_session_manager = OpticalSessionManager(
            self._exposure_policy_controller
        )
        self._exposure_policy_adapter = ExposurePolicyQtAdapter(
            self._exposure_policy_controller,
            settings_write=self._camera_api_broker.write_settings_trusted,
        )
        self._exposure_policy_adapter.command_finished.connect(
            self._on_exposure_policy_command_finished
        )
        self._camera_api_broker.set_manual_exposure_write(
            lambda command: self._exposure_policy_controller.run_manual_exposure_write(
                command
            )
        )

    def _create_stage_controller(self) -> StageController:
        controller = StageController()
        controller.set_optical_session_manager(self._optical_session_manager)
        return controller

    def _compose_optical_calibration_runtime(self) -> None:
        camera = MicroscopeScanCameraAdapter(
            grabber=self.grabber,
            stop_event=self._microscope_scan_stop_requested,
            latest_frame_counter=self._latest_camera_counter,
            wait_for_frame=self._wait_for_camera_frame,
            latest_raw_frame_counter=self._latest_raw_camera_counter,
            wait_for_raw_frame=self._wait_for_raw_camera_frame,
            correct_lens=self._correct_camera_frame_for_active_objective,
            settings_timeout_s=self.MICROSCOPE_SCAN_CAMERA_SETTINGS_TIMEOUT_S,
        )
        self._optical_calibration_request_adapter = OpticalCalibrationRequestAdapter(
            objective_metadata=self._active_objective_metadata,
            objective_scale=self._active_microscope_scale,
        )
        self._optical_calibration_runtime = OpticalCalibrationRuntime(
            stage=OpticalCalibrationStageAdapter(
                reserve_task=self.stage_controller.reserve_external_task,
                read_position=self.stage_controller.run_external_current_stage_position,
                raise_action=self.stage_controller.run_external_needles_action,
                move_xy_callback=self.stage_controller.run_external_move_to_xy,
            ),
            camera=OpticalCalibrationCameraAdapter(
                apply_lock=camera.apply_lock,
                restore_lock=camera.restore_lock,
                raw_counter=self._latest_raw_camera_counter,
                wait_raw_callback=self._wait_for_raw_camera_frame,
            ),
            sessions=OpticalCalibrationSessionAdapter(self._optical_session_manager),
            store=OpticalCalibrationStoreAdapter(
                self._flat_field_calibration_store.install
            ),
            events=OpticalCalibrationEventAdapter.from_signal_emitters(
                status=self.status_message_requested.emit,
                flat_progress=self.flat_field_calibration_progress.emit,
                lens_progress=self.lens_distortion_calibration_progress.emit,
                flat_completion=self.flat_field_calibration_finished.emit,
                lens_completion=self.lens_distortion_calibration_finished.emit,
            ),
        )

    def _on_exposure_policy_command_finished(self, result: object) -> None:
        if not isinstance(result, Mapping) or result.get("operation") != "start":
            return
        self._exposure_policy_start_in_flight = False
        if bool(result.get("accepted", False)):
            self._exposure_policy_started = True
            self._exposure_policy_start_retry_after = 0.0
            return
        self._exposure_policy_started = False
        self._exposure_policy_start_retry_after = (
            time.monotonic() + self.EXPOSURE_POLICY_START_RETRY_BACKOFF_S
        )
        message = str(result.get("message") or "Camera exposure setup failed.")
        logger.warning("Camera exposure policy startup failed: %s", message)
        self._show_status(f"Camera exposure setup failed: {message}", 8000)

    def _configure_api_server_from_settings(self, *, start_if_enabled: bool) -> None:
        api_settings = self.settings_manager.api_configuration()
        signature = (
            bool(api_settings.enabled),
            str(api_settings.host),
            int(api_settings.port),
        )
        if self._api_settings_signature == signature:
            return
        if self._api_server is not None:
            self._api_server.stop()
        self._api_settings_signature = signature
        if not api_settings.enabled:
            self._api_server = None
            if start_if_enabled:
                self._show_status("FastAPI control API is disabled.", 3000)
            return
        self._api_server = ProbeStationApiServer(
            move_callback=self._submit_api_move_request,
            status_callback=self._submit_api_status_request,
            command_callback=self._submit_api_command_request_from_api_thread,
            auth_callback=self._authorize_api_request,
            camera_settings_read_callback=self._camera_api_broker.read_settings,
            camera_settings_write_callback=self._camera_api_broker.write_settings,
            camera_frame_callback=self._api_camera_frame,
            camera_exposure_policy_snapshot_callback=(
                self._exposure_policy_controller.snapshot
            ),
            camera_exposure_policy_set_callback=(
                self._exposure_policy_controller.set_policy
            ),
            camera_exposure_once_callback=self._exposure_policy_controller.run_once,
            host=api_settings.host,
            port=api_settings.port,
        )
        if start_if_enabled:
            self._start_api_server()

    def _authorize_api_request(
        self,
        api_key: str | None,
        permission: str,
    ) -> dict[str, Any]:
        return self._api_key_store.authorize(api_key, permission)

    def _submit_camera_settings_snapshot(
        self,
        request_id: str,
        names: list[str],
    ) -> None:
        self.grabber.request_camera_settings_snapshot(
            "camera",
            names,
            request_id=request_id,
        )

    def _submit_camera_settings_batch(
        self,
        request_id: str,
        settings: list[tuple[str, object]],
    ) -> None:
        self.grabber.request_camera_settings_batch(
            settings,
            request_id=request_id,
            map_key="camera",
        )

    def _write_camera_auto_exposure_settings(
        self,
        settings: list[tuple[str, object]],
    ) -> dict[str, Any]:
        return self._camera_api_broker.write_settings_trusted(settings)

    def _read_camera_auto_exposure_frame(
        self,
        after_counter: int,
        timeout_s: float,
    ) -> AutoExposureFrame:
        import numpy as np

        frame, counter = self._wait_for_raw_camera_frame(
            after_counter=int(after_counter),
            timeout_s=float(timeout_s),
        )
        if frame is None:
            raise RuntimeError("Fresh raw camera frame is unavailable.")
        image = frame.convertToFormat(QImage.Format_RGB888)
        width = int(image.width())
        height = int(image.height())
        stride = int(image.bytesPerLine())
        rows = np.frombuffer(
            image.bits(),
            dtype=np.uint8,
            count=height * stride,
        ).reshape((height, stride))
        rgb = rows[:, : width * 3].reshape((height, width, 3)).copy(order="C")
        return AutoExposureFrame(rgb=rgb, counter=int(counter))

    def _api_camera_frame(
        self,
        space: str,
        after_counter: int | None,
        timeout_s: float,
    ) -> dict[str, Any]:
        if space == "raw":
            frame, counter = self._wait_for_raw_camera_frame(
                after_counter=after_counter,
                timeout_s=timeout_s,
            )
        elif space == "corrected":
            frame, counter = self._wait_for_camera_frame(
                after_counter=after_counter,
                timeout_s=timeout_s,
            )
        else:
            return {
                "accepted": False,
                "status_code": 400,
                "message": f"Unsupported camera frame space: {space!r}.",
            }
        if frame is None:
            return {
                "accepted": False,
                "status_code": 504 if after_counter is not None else 503,
                "message": (
                    "No newer camera frame arrived before timeout."
                    if after_counter is not None
                    else "Camera frame is not available."
                ),
                "counter": int(counter),
                "space": space,
            }
        return encode_camera_frame_png(frame, counter=counter, space=space)

    def _telegram_command_snapshot(self) -> TelegramCommandSnapshot:
        return TelegramCommandSnapshot(
            route_active=telegram_commands.thread_alive(
                getattr(self, "_route_measurement_thread", None)
            ),
            route_waiting=bool(getattr(self, "_route_measurement_waiting", False)),
            runner_available=(
                getattr(self, "_route_measurement_runner", None) is not None
            ),
            photo_enabled=bool(
                getattr(self, "_route_measurement_photo_enabled", False)
            ),
            measure_enabled=bool(
                getattr(self, "_route_measurement_measure_enabled", False)
            ),
        )

    def _telegram_status_snapshot(self) -> telegram_commands.TelegramStatusSnapshot:
        return telegram_commands.TelegramStatusSnapshot(
            latest_status_message=str(
                getattr(self, "_latest_status_message", "") or ""
            ),
            route_thread_active=telegram_commands.thread_alive(
                getattr(self, "_route_measurement_thread", None)
            ),
            route_waiting=bool(getattr(self, "_route_measurement_waiting", False)),
            route_session_active=bool(
                getattr(self, "_route_measurement_session_active", False)
            ),
            route_current_point=getattr(self, "_route_measurement_current_point", None),
            api_route_control_status_text=(
                self._api_route_control_state_snapshot().telegram_status_text()
            ),
            stage_status=self._api_stage_status(),
            microscope_scan_active=telegram_commands.thread_alive(
                getattr(self, "_microscope_scan_thread", None)
            ),
            contact_seek_active=telegram_commands.thread_alive(
                getattr(self, "_contact_seek_thread", None)
            ),
            camera_frame_available=(
                getattr(self, "_latest_camera_frame_for_notifications", None)
                is not None
            ),
            stage_axis_names=self.STAGE_AXIS_NAMES,
        )

    def _submit_api_move_request(self, move_request: dict[str, Any]) -> dict[str, Any]:
        if self._api_bridge is None:
            return {
                "accepted": False,
                "status_code": 503,
                "message": "GUI API bridge is not ready.",
            }
        targets = move_request.get("targets")
        if not isinstance(targets, dict):
            targets = {}
        return self._api_bridge.submit(
            {
                "action": "move_to_coordinates",
                "targets": dict(targets),
                "mode": move_request.get("mode", "G90"),
                "feedrate": move_request.get("feedrate"),
            }
        )

    def _submit_api_status_request(self) -> dict[str, Any]:
        if self._api_bridge is None:
            return {
                "accepted": False,
                "status_code": 503,
                "message": "GUI API bridge is not ready.",
            }
        return self._api_bridge.submit({"action": "status"})

    def _submit_api_command_request(
        self,
        command_request: dict[str, Any],
    ) -> dict[str, Any] | DeferredApiResponse:
        action, payload = self._api_command_action_payload(command_request)
        if action in GUI_STAGE_WORKER_ACTIONS:
            route_guard = self._probe_route_api_window_guard(action, payload)
            if route_guard is not None:
                return route_guard
            return self._api_stage_command_runtime.submit(command_request, action)
        return self._dispatch_api_command_request(
            command_request,
            apply_route_control_guard=True,
        )

    def _submit_api_command_request_from_api_thread(
        self,
        command_request: dict[str, Any],
    ) -> dict[str, Any]:
        return command_dispatch_from_api_thread(
            command_request,
            submit_on_gui_thread=self._submit_api_command_request_on_gui_thread,
            submit_probe_route_window_guard_on_gui_thread=(
                self._submit_probe_route_window_guard_on_gui_thread
            ),
            dispatch_direct=self._dispatch_api_command_request,
            route_window_required=self._probe_route_api_requires_window,
        )

    def _submit_api_command_request_on_gui_thread(
        self,
        command_request: dict[str, Any],
    ) -> dict[str, Any]:
        if self._api_bridge is None:
            return {
                "accepted": False,
                "status_code": 503,
                "message": "GUI API bridge is not ready.",
            }
        return self._api_bridge.submit(
            {
                "action": "command",
                "command": dict(command_request),
            },
            timeout_s=10.0,
        )

    def _submit_probe_route_window_guard_on_gui_thread(
        self,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self._api_bridge is None:
            return {
                "accepted": False,
                "status_code": 503,
                "message": "GUI API bridge is not ready.",
            }
        return self._api_bridge.submit(
            {
                "action": "probe_route_window_guard",
                "guard_action": action,
                "payload": dict(payload),
            },
            timeout_s=10.0,
        )

    @staticmethod
    def _api_command_action_payload(
        command_request: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        return command_dispatch_action_payload(command_request)

    def _api_command_dispatch_handlers(self) -> ApiCommandDispatchHandlers:
        return ApiCommandDispatchHandlers(
            route_control_guard=self._probe_route_api_window_guard,
            list_contacts=self._api_list_contacts,
            move_to_contact=self._api_move_to_contact,
            contact_needles=self._api_contact_needles,
            check_contact=self._api_check_contact,
            stage_local_focus=self._api_stage_local_focus,
            route_contact_focus=self._api_route_contact_focus,
            route_contact_photo=self._api_route_contact_photo,
            contact_seek=self._api_contact_seek,
            api_route_control_status=self._api_route_control_status,
            api_route_control_action=self._api_route_control_action,
            configure_meter=self._api_configure_meter,
            raw_voltage_sweep=self._api_raw_voltage_sweep,
            visa_list_resources=self._api_visa_list_resources,
            visa_operation=self._api_visa_operation,
            start_route_session=self._api_start_route_session,
            route_session_status=self._api_route_session_status,
            route_session_action=self._api_route_session_action,
            route_session_result=self._api_route_session_result,
            route_session_seek=self._api_route_session_seek,
            route_session_artifact=self._api_route_session_artifact,
            lens_distortion_calibration=self._api_lens_distortion_calibration,
            click_to_move_calibration=self._api_click_to_move_calibration,
            microscope_area_scan=self._api_microscope_area_scan,
        )

    def _dispatch_api_command_request(
        self,
        command_request: dict[str, Any],
        *,
        apply_route_control_guard: bool,
    ) -> dict[str, Any]:
        return command_dispatch_request(
            command_request,
            self._api_command_dispatch_handlers(),
            apply_route_control_guard=apply_route_control_guard,
        )

    def _probe_route_api_window_guard(
        self,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not self._probe_route_api_requires_window(action, payload):
            return None
        if self._route_control_window_is_open():
            return None
        message = (
            "Probe route control window is closed. Open the route measurement "
            "window before using probe route API commands."
        )
        self._show_status(message, 8000)
        return {
            "accepted": False,
            "status_code": 409,
            "message": message,
            "route_control_window_open": False,
        }

    def _probe_route_api_requires_window(
        self,
        action: str,
        payload: dict[str, Any],
    ) -> bool:
        return probe_route_api_requires_window(action, payload)

    def _route_control_window_is_open(self) -> bool:
        dialog = getattr(self, "_route_measurement_dialog", None)
        if dialog is None:
            return False
        is_visible = getattr(dialog, "isVisible", None)
        if callable(is_visible):
            try:
                return bool(is_visible())
            except Exception:
                logger.exception("Failed to query route control window visibility.")
                return False
        return True

    def _handle_api_request(
        self,
        request: dict[str, Any],
    ) -> dict[str, Any] | DeferredApiResponse:
        return command_dispatch_handle_api_request(
            request,
            ApiBridgeRequestHandlers(
                move_to_coordinates=self._api_move_to_coordinates,
                stage_status=self._api_stage_status,
                submit_command=self._submit_api_command_request,
                route_control_guard=self._probe_route_api_window_guard,
            ),
        )
