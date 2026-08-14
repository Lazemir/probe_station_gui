"""Direct owner for the api meter visa domain."""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from probe_station_gui.instruments.api_sweep import (
    ApiRawVoltageSweepRequestError,
    api_raw_voltage_sweep_contact_plan,
    api_raw_voltage_sweep_error_response,
    api_raw_voltage_sweep_meter_payload,
    api_raw_voltage_sweep_request_from_payload,
    api_raw_voltage_sweep_success_response,
)
from probe_station_gui.instruments.meters.lcr_session_backend import LCRMeterError
from probe_station_gui.route.api_measurement import (
    api_contact_number_from_payload,
    api_current_contact_error_response,
    api_current_contact_failure_alert,
    api_current_contact_response,
    api_current_contact_settings_from_payload,
)
from probe_station_gui.route.measurement import (
    ROUTE_OPERATION_MEASURE,
    RouteMeasurementRunner,
)
from probe_station_gui.route.meter_config import RouteMeterConfiguration
from probe_station_gui.route.point_execution_adapters import RouteMeasurementEvents
from probe_station_gui.stage.controller import StageControllerError

logger = logging.getLogger("main")


class _MainApiMeterVisaMixin:
    def _api_measure_current_contact(
        self,
        payload: dict[str, Any],
        *,
        seek: bool,
    ) -> dict[str, Any]:
        contact_number = api_contact_number_from_payload(payload)
        if contact_number is None:
            return api_current_contact_error_response(
                "Provide a positive contact_number.",
                status_code=400,
            )
        context_result = self._api_contact_context(contact_number)
        if not context_result.get("accepted", False):
            return context_result
        point = context_result["point"]
        contact = context_result["contact"]

        connect_result = self._api_ensure_measurement_instrument_connected()
        if connect_result is not None:
            connect_result["contact"] = contact
            return connect_result

        try:
            settings = api_current_contact_settings_from_payload(
                payload,
                default_check_sample_count=RouteMeasurementRunner.SHORT_CHECK_SAMPLE_COUNT,
                default_contact_seek_range_mm=RouteMeasurementRunner.AUTO_CONTACT_SEEK_MAX_TOTAL_MM,
                default_contact_seek_step_mm=RouteMeasurementRunner.AUTO_CONTACT_SEEK_STEP_MM,
                default_contact_settle_s=RouteMeasurementRunner.DEFAULT_CONTACT_SETTLE_S,
            )
        except ValueError as exc:
            return api_current_contact_error_response(
                str(exc),
                status_code=400,
                contact=contact,
            )

        needle_feedrate = self._api_needle_feedrate(payload)
        design_frame_snapshot = self._snapshot_active_route_design_frame()
        runner = RouteMeasurementRunner(
            points=[point],
            csv_path=Path(os.devnull),
            stage_controller=self.stage_controller,
            lcr_controller=self.lcr_controller,
            needle_feedrate=needle_feedrate,
            measurement_count=settings.measurement_count,
            initial_measurement_count=settings.check_sample_count,
            start_point_number=int(point.index),
            max_relative_rms=settings.max_relative_rms,
            contact_quality_limits=settings.contact_quality_limits,
            auto_contact_seek_on_bad_contact=seek,
            auto_contact_seek_step_mm=settings.contact_seek_step_mm,
            auto_contact_seek_max_total_mm=settings.contact_seek_range_mm,
            contact_settle_s=settings.contact_settle_s,
            operation_mode=ROUTE_OPERATION_MEASURE,
            events=RouteMeasurementEvents(
                status=self.route_measurement_status.emit,
            ),
            design_frame_snapshot=design_frame_snapshot,
            post_success_contact=self._design_contact_success_callback(
                design_frame_snapshot
            ),
        )
        try:
            result = runner.seek_contact(point) if seek else runner.check_contact(point)
        except (StageControllerError, LCRMeterError, RuntimeError) as exc:
            return api_current_contact_error_response(
                str(exc),
                status_code=409,
                contact=contact,
            )
        except Exception as exc:
            prefix = "Contact seek failed" if seek else "Contact check failed"
            logger.exception("API %s.", prefix.lower())
            return self._api_instrument_exception_response(
                prefix,
                exc,
                contact=contact,
            )
        response = api_current_contact_response(
            result,
            contact=contact,
            settings=settings,
            needle_feedrate=needle_feedrate,
            timestamp_utc=self._api_timestamp_utc(),
            seek=seek,
        )
        if seek:
            alert = api_current_contact_failure_alert(
                payload,
                contact_number=contact_number,
                result=result,
                reply_markup=self._telegram_runtime.route_actions_markup(),
            )
            if alert is not None:
                self._telegram_runtime.send_alert(
                    alert.channel,
                    alert.text,
                    attach_photo=alert.attach_photo,
                    reply_markup=alert.reply_markup,
                )
        return response

    def _api_ensure_measurement_instrument_connected(self) -> dict[str, Any] | None:
        if self.lcr_controller.is_connected():
            return None
        connector = getattr(self.lcr_controller, "connect_now", None)
        if not callable(connector):
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Measurement instrument is not connected.",
            }
        try:
            connector()
        except LCRMeterError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        except Exception as exc:
            logger.exception("API measurement instrument connection failed.")
            return self._api_instrument_exception_response(
                "Measurement instrument connection failed",
                exc,
            )
        if not self.lcr_controller.is_connected():
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Measurement instrument is not connected.",
            }
        return None

    def _api_prepare_route_meter_controller(
        self,
        configuration: RouteMeterConfiguration,
        *,
        prefix: str = "Measurement instrument setup failed",
    ) -> dict[str, Any] | None:
        wait_until_idle = getattr(self.lcr_controller, "wait_until_idle", None)
        if callable(wait_until_idle):
            try:
                ready = bool(wait_until_idle(45.0))
            except Exception as exc:
                logger.exception("API measurement instrument wait failed.")
                return self._api_instrument_exception_response(
                    "Measurement instrument wait failed",
                    exc,
                )
            if not ready:
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": "Measurement instrument task is still running.",
                }
        if not self.lcr_controller.is_connected():
            runtime_config = getattr(
                self.lcr_controller,
                "apply_route_meter_runtime_configuration",
                None,
            )
            if callable(runtime_config):
                runtime_config(configuration)
            connect_result = self._api_ensure_measurement_instrument_connected()
            if connect_result is not None:
                return connect_result
        try:
            self.lcr_controller.apply_route_meter_configuration(configuration)
        except LCRMeterError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        except Exception as exc:
            logger.exception("%s.", prefix)
            return self._api_instrument_exception_response(prefix, exc)
        return None

    @staticmethod
    def _api_instrument_exception_response(
        prefix: str,
        exc: Exception,
        **extra: Any,
    ) -> dict[str, Any]:
        message = str(exc).strip()
        response: dict[str, Any] = {
            "accepted": False,
            "status_code": 409,
            "message": f"{prefix}: {message}" if message else prefix,
            "error_type": type(exc).__name__,
        }
        response.update(extra)
        return response

    def _api_configure_meter(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            configuration = self._api_route_meter_configuration(
                payload,
                voltages_v=None,
            )
        except ValueError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        setup_result = self._api_prepare_route_meter_controller(
            configuration,
            prefix="Measurement instrument setup failed",
        )
        if setup_result is not None:
            return setup_result
        return {
            "accepted": True,
            "message": "Measurement instrument configured.",
            "timestamp_utc": self._api_timestamp_utc(),
            "meter_type": configuration.meter_type,
            "nplc": configuration.nplc_label(),
        }

    def _api_raw_voltage_sweep(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            request = api_raw_voltage_sweep_request_from_payload(payload)
            configuration = self._api_route_meter_configuration(
                api_raw_voltage_sweep_meter_payload(payload),
                voltages_v=request.voltage_values,
            )
        except ApiRawVoltageSweepRequestError as exc:
            return exc.response()
        except (TypeError, ValueError) as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        setup_result = self._api_prepare_route_meter_controller(
            configuration,
            prefix="Measurement instrument setup failed",
        )
        if setup_result is not None:
            return setup_result

        context_result = None
        if request.contact_number is not None:
            context_result = self._api_contact_context(request.contact_number)
        contact_plan_result = api_raw_voltage_sweep_contact_plan(
            request,
            context_result=context_result,
        )
        if isinstance(contact_plan_result, dict):
            return contact_plan_result
        contact_plan = contact_plan_result
        needle_feedrate = self._api_needle_feedrate(payload)
        stage_lease = None
        needles_lowered = False
        started_at = time.monotonic()
        timestamp_utc = self._api_timestamp_utc()
        try:
            if request.move_to_contact or request.lower_needles or request.lift_after:
                stage_lease = self.stage_controller.reserve_external_task(
                    "API raw voltage sweep"
                )
            if stage_lease is not None and request.lift_before_move:
                self.stage_controller.run_external_needles_action(
                    "lift",
                    needle_feedrate,
                )
            if request.move_to_contact and contact_plan.point is not None:
                target_xy = self._api_route_adjusted_stage_xy(contact_plan.point)
                self.stage_controller.run_external_move_to_xy(
                    target_xy[0],
                    target_xy[1],
                )
            if stage_lease is not None and request.lower_needles:
                self.stage_controller.run_external_needles_action(
                    "lower",
                    needle_feedrate,
                )
                needles_lowered = True
                if request.contact_settle_s > 0.0:
                    time.sleep(request.contact_settle_s)
            raw_measurement = self.lcr_controller.read_voltage_sweep_now(
                request.voltage_values
            )
            return api_raw_voltage_sweep_success_response(
                voltage_values=request.voltage_values,
                result=self._api_json_ready(raw_measurement),
                timestamp_utc=timestamp_utc,
                elapsed_s=time.monotonic() - started_at,
                contact=contact_plan.contact,
                meter_type=configuration.meter_type,
                lower_needles=request.lower_needles,
                needles_lowered=needles_lowered,
                lift_after=request.lift_after,
            )
        except (StageControllerError, LCRMeterError) as exc:
            return api_raw_voltage_sweep_error_response(
                str(exc),
                contact=contact_plan.contact,
            )
        except Exception as exc:
            logger.exception("API raw voltage sweep failed.")
            return self._api_instrument_exception_response(
                "Raw voltage sweep failed",
                exc,
                contact=contact_plan.contact,
            )
        finally:
            if stage_lease is not None:
                try:
                    if request.lift_after and needles_lowered:
                        self.stage_controller.run_external_needles_action(
                            "lift",
                            needle_feedrate,
                        )
                except StageControllerError:
                    logger.exception("API raw voltage sweep failed to lift needles.")
                finally:
                    stage_lease.release()

    def _api_visa_list_resources(self) -> dict[str, Any]:
        controller = self._api_visa_controller()
        if controller is self.lcr_controller:
            connect_result = self._api_ensure_measurement_instrument_connected()
            if connect_result is not None:
                return connect_result
        try:
            roles_getter = getattr(controller, "visa_resource_roles", None)
            if not callable(roles_getter):
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": "Measurement instrument does not expose VISA roles.",
                }
            roles = dict(roles_getter())
        except LCRMeterError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        except Exception as exc:
            logger.exception("API VISA resource listing failed.")
            return self._api_instrument_exception_response(
                "VISA resource listing failed",
                exc,
            )
        resources = sorted(
            (dict(item) for item in roles.values()),
            key=lambda item: str(item.get("role") or ""),
        )
        return {
            "accepted": True,
            "meter_type": self._api_visa_meter_type(resources),
            "resources": resources,
        }

    def _api_visa_operation(self, payload: dict[str, Any]) -> dict[str, Any]:
        role = str(payload.get("role", "")).strip()
        operation = str(payload.get("operation", "")).strip().lower()
        if not role:
            return {
                "accepted": False,
                "status_code": 400,
                "message": "VISA role is required.",
            }
        if operation not in {"write", "query", "ask", "read", "read_raw", "clear"}:
            return {
                "accepted": False,
                "status_code": 400,
                "message": f"Unsupported VISA operation: {operation}.",
            }
        controller = self._api_visa_controller()
        if controller is self.lcr_controller:
            connect_result = self._api_ensure_measurement_instrument_connected()
            if connect_result is not None:
                return connect_result
        timeout_ms = self._api_optional_timeout_ms(payload)
        command_value = payload.get("command", payload.get("query"))
        command = None if command_value is None else str(command_value)
        read_termination = (
            str(payload.get("read_termination"))
            if payload.get("read_termination") is not None
            else None
        )
        write_termination = (
            str(payload.get("write_termination"))
            if payload.get("write_termination") is not None
            else None
        )
        try:
            operation_runner = getattr(controller, "visa_operation", None)
            if not callable(operation_runner):
                return {
                    "accepted": False,
                    "status_code": 409,
                    "message": "Measurement instrument does not expose VISA operations.",
                }
            result = operation_runner(
                role,
                operation,
                command=command,
                timeout_ms=timeout_ms,
                read_termination=read_termination,
                write_termination=write_termination,
            )
        except LCRMeterError as exc:
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        except Exception as exc:
            logger.exception("API VISA operation failed.")
            return self._api_instrument_exception_response(
                "VISA operation failed",
                exc,
                role=role,
                operation=operation,
            )
        response: dict[str, Any] = {
            "accepted": True,
            "role": role,
            "operation": operation,
            "timestamp_utc": self._api_timestamp_utc(),
        }
        if isinstance(result, (bytes, bytearray)):
            response["data"] = bytes(result)
            response["size_bytes"] = len(result)
        elif result is not None:
            response["response"] = str(result)
        return response

    def _api_visa_controller(self) -> object:
        if (
            self._route_run_execution.snapshot().active
            and self._api_route_lcr_controller is not None
        ):
            return self._api_route_lcr_controller
        return self.lcr_controller

    @staticmethod
    def _api_visa_meter_type(resources: list[dict[str, object]]) -> str:
        for item in resources:
            meter_type = item.get("meter_type")
            if meter_type:
                return str(meter_type)
        return ""

    @staticmethod
    def _api_optional_timeout_ms(payload: dict[str, Any]) -> int | None:
        value = payload.get("timeout_ms", payload.get("timeout"))
        if value is None:
            return None
        try:
            timeout = int(float(value))
        except (TypeError, ValueError):
            return None
        return timeout if timeout > 0 else None
