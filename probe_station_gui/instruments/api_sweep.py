from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from probe_station_gui.route.api_measurement import api_contact_number_from_payload
from probe_station_gui.route.payload_parsing import payload_bool, payload_float


@dataclass(frozen=True)
class ApiRawVoltageSweepRequest:
    voltage_values: list[float]
    contact_number: int | None
    move_to_contact: bool
    lower_needles: bool
    lift_after: bool
    lift_before_move: bool
    contact_settle_s: float


@dataclass(frozen=True)
class ApiRawVoltageSweepContactPlan:
    point: object | None
    contact: dict[str, Any] | None


class ApiRawVoltageSweepRequestError(ValueError):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = int(status_code)

    def response(self) -> dict[str, object]:
        return {
            "accepted": False,
            "status_code": self.status_code,
            "message": str(self),
        }


def api_raw_voltage_sweep_meter_payload(payload: dict[str, object]) -> object:
    return payload.get("meter", payload.get("meter_configuration", {}))


def api_raw_voltage_sweep_request_from_payload(
    payload: dict[str, object],
) -> ApiRawVoltageSweepRequest:
    voltages = payload.get("voltages_v")
    if not isinstance(voltages, list) or not voltages:
        raise ApiRawVoltageSweepRequestError(
            400,
            "Provide voltages_v as a non-empty array.",
        )
    try:
        voltage_values = [float(value) for value in voltages]
        contact_number = api_contact_number_from_payload(payload)
        move_to_contact = payload_bool(
            payload,
            "move_to_contact",
            "move",
            default=contact_number is not None,
        )
        lower_needles = payload_bool(
            payload,
            "lower_needles",
            "lower",
            default=contact_number is not None,
        )
        lift_after = payload_bool(payload, "lift_after", default=lower_needles)
        lift_before_move = payload_bool(
            payload,
            "lift_before_move",
            default=move_to_contact,
        )
        contact_settle_s = payload_float(
            payload,
            "contact_settle_s",
            "settle_s",
            default=0.2,
            minimum=0.0,
        )
    except (TypeError, ValueError) as exc:
        raise ApiRawVoltageSweepRequestError(409, str(exc)) from exc
    return ApiRawVoltageSweepRequest(
        voltage_values=voltage_values,
        contact_number=contact_number,
        move_to_contact=move_to_contact,
        lower_needles=lower_needles,
        lift_after=lift_after,
        lift_before_move=lift_before_move,
        contact_settle_s=contact_settle_s,
    )


def api_raw_voltage_sweep_contact_plan(
    request: ApiRawVoltageSweepRequest,
    *,
    context_result: dict[str, object] | None = None,
) -> ApiRawVoltageSweepContactPlan | dict[str, object]:
    point = None
    contact = None
    if request.contact_number is not None:
        if context_result is None:
            raise ValueError("context_result is required when contact_number is set.")
        if not context_result.get("accepted", False):
            return context_result
        point = context_result["point"]
        contact_value = context_result["contact"]
        contact = contact_value if isinstance(contact_value, dict) else None
    if request.move_to_contact and point is None:
        return api_raw_voltage_sweep_missing_contact_response()
    return ApiRawVoltageSweepContactPlan(point=point, contact=contact)


def api_raw_voltage_sweep_missing_contact_response() -> dict[str, object]:
    return {
        "accepted": False,
        "status_code": 400,
        "message": "move_to_contact requires contact_number.",
    }


def api_raw_voltage_sweep_success_response(
    *,
    voltage_values: list[float],
    result: dict[str, object],
    timestamp_utc: str,
    elapsed_s: float,
    contact: dict[str, Any] | None,
    meter_type: str,
    lower_needles: bool,
    needles_lowered: bool,
    lift_after: bool,
) -> dict[str, object]:
    return {
        "accepted": True,
        "message": f"Raw voltage sweep complete: {len(voltage_values)} points.",
        "timestamp_utc": str(timestamp_utc),
        "elapsed_s": float(elapsed_s),
        "contact": contact,
        "meter_type": meter_type,
        "measurement_kind": "voltage_sweep",
        "voltages_v": list(voltage_values),
        "iv_pairs": _api_raw_voltage_sweep_iv_pairs(result),
        "result": result,
        "needles_lowered": bool(lower_needles),
        "lifted_after": bool(lift_after and needles_lowered),
    }


def api_raw_voltage_sweep_error_response(
    message: str,
    *,
    contact: dict[str, Any] | None,
) -> dict[str, object]:
    return {
        "accepted": False,
        "status_code": 409,
        "message": str(message),
        "contact": contact,
    }


def _api_raw_voltage_sweep_iv_pairs(
    result: dict[str, object],
) -> list[dict[str, object | None]]:
    points = result.get("points", [])
    if not isinstance(points, list):
        return []
    return [
        {
            "voltage_v": item.get("measured_voltage_v"),
            "current_a": item.get("current_a"),
        }
        for item in points
        if isinstance(item, dict)
    ]


def api_prepare_route_meter_controller_action(
    *,
    configuration: object,
    prefix: str,
    is_connected: Callable[[], bool],
    wait_until_idle: Callable[[float], object] | None,
    apply_route_meter_runtime_configuration: Callable[[object], None] | None,
    ensure_measurement_instrument_connected: Callable[[], dict[str, object] | None],
    apply_route_meter_configuration: Callable[[object], None],
    instrument_exception_response: Callable[[str, Exception], dict[str, object]],
    log_exception: Callable[[str], None],
    meter_error_types: tuple[type[BaseException], ...] = (),
) -> dict[str, object] | None:
    if callable(wait_until_idle):
        try:
            ready = bool(wait_until_idle(45.0))
        except Exception as exc:
            log_exception("API measurement instrument wait failed.")
            return instrument_exception_response(
                "Measurement instrument wait failed",
                exc,
            )
        if not ready:
            return {
                "accepted": False,
                "status_code": 409,
                "message": "Measurement instrument task is still running.",
            }
    if not is_connected():
        if callable(apply_route_meter_runtime_configuration):
            apply_route_meter_runtime_configuration(configuration)
        connect_result = ensure_measurement_instrument_connected()
        if connect_result is not None:
            return connect_result
    try:
        apply_route_meter_configuration(configuration)
    except Exception as exc:
        if meter_error_types and isinstance(exc, meter_error_types):
            return {
                "accepted": False,
                "status_code": 409,
                "message": str(exc),
            }
        log_exception(f"{prefix}.")
        return instrument_exception_response(prefix, exc)
    return None


def api_configure_meter_action(
    *,
    payload: dict[str, object],
    route_meter_configuration: Callable[[object, list[float] | None], object],
    prepare_route_meter_controller: Callable[..., dict[str, object] | None],
    timestamp_utc: Callable[[], str],
) -> dict[str, object]:
    try:
        configuration = route_meter_configuration(payload, None)
    except ValueError as exc:
        return {
            "accepted": False,
            "status_code": 409,
            "message": str(exc),
        }
    setup_result = prepare_route_meter_controller(
        configuration,
        prefix="Measurement instrument setup failed",
    )
    if setup_result is not None:
        return setup_result
    return {
        "accepted": True,
        "message": "Measurement instrument configured.",
        "timestamp_utc": timestamp_utc(),
        "meter_type": configuration.meter_type,
        "nplc": configuration.nplc_label(),
    }


def api_raw_voltage_sweep_action(
    *,
    payload: dict[str, object],
    route_meter_configuration: Callable[[object, list[float]], object],
    prepare_route_meter_controller: Callable[..., dict[str, object] | None],
    contact_context: Callable[[int], dict[str, object]],
    needle_feedrate: Callable[[dict[str, object]], float | None],
    route_adjusted_stage_xy: Callable[[object], tuple[float, float]],
    begin_stage_task: Callable[[str], None],
    run_needles_action: Callable[[str, float | None], None],
    run_move_to_xy: Callable[[float, float], None],
    finish_stage_task: Callable[[], None],
    read_voltage_sweep_now: Callable[[list[float]], object],
    json_ready: Callable[[object], dict[str, object]],
    timestamp_utc: Callable[[], str],
    monotonic: Callable[[], float],
    sleep: Callable[[float], None],
    instrument_exception_response: Callable[..., dict[str, object]],
    log_exception: Callable[[str], None],
    stage_or_meter_error_types: tuple[type[BaseException], ...] = (),
) -> dict[str, object]:
    try:
        request = api_raw_voltage_sweep_request_from_payload(payload)
        configuration = route_meter_configuration(
            api_raw_voltage_sweep_meter_payload(payload),
            request.voltage_values,
        )
    except ApiRawVoltageSweepRequestError as exc:
        return exc.response()
    except (TypeError, ValueError) as exc:
        return {
            "accepted": False,
            "status_code": 409,
            "message": str(exc),
        }
    setup_result = prepare_route_meter_controller(
        configuration,
        prefix="Measurement instrument setup failed",
    )
    if setup_result is not None:
        return setup_result

    context_result = None
    if request.contact_number is not None:
        context_result = contact_context(request.contact_number)
    contact_plan_result = api_raw_voltage_sweep_contact_plan(
        request,
        context_result=context_result,
    )
    if isinstance(contact_plan_result, dict):
        return contact_plan_result
    contact_plan = contact_plan_result

    feedrate = needle_feedrate(payload)
    active_stage_task = False
    needles_lowered = False
    started_at = monotonic()
    started_timestamp = timestamp_utc()
    try:
        if request.move_to_contact or request.lower_needles or request.lift_after:
            begin_stage_task("API raw voltage sweep")
            active_stage_task = True
        if active_stage_task and request.lift_before_move:
            run_needles_action("lift", feedrate)
        if request.move_to_contact and contact_plan.point is not None:
            target_xy = route_adjusted_stage_xy(contact_plan.point)
            run_move_to_xy(target_xy[0], target_xy[1])
        if active_stage_task and request.lower_needles:
            run_needles_action("lower", feedrate)
            needles_lowered = True
            if request.contact_settle_s > 0.0:
                sleep(request.contact_settle_s)
        raw_measurement = read_voltage_sweep_now(request.voltage_values)
        return api_raw_voltage_sweep_success_response(
            voltage_values=request.voltage_values,
            result=json_ready(raw_measurement),
            timestamp_utc=started_timestamp,
            elapsed_s=monotonic() - started_at,
            contact=contact_plan.contact,
            meter_type=configuration.meter_type,
            lower_needles=request.lower_needles,
            needles_lowered=needles_lowered,
            lift_after=request.lift_after,
        )
    except Exception as exc:
        if stage_or_meter_error_types and isinstance(exc, stage_or_meter_error_types):
            return api_raw_voltage_sweep_error_response(
                str(exc),
                contact=contact_plan.contact,
            )
        log_exception("API raw voltage sweep failed.")
        return instrument_exception_response(
            "Raw voltage sweep failed",
            exc,
            contact=contact_plan.contact,
        )
    finally:
        if active_stage_task:
            if request.lift_after and needles_lowered:
                try:
                    run_needles_action("lift", feedrate)
                except Exception as exc:
                    if stage_or_meter_error_types and isinstance(exc, stage_or_meter_error_types):
                        log_exception("API raw voltage sweep failed to lift needles.")
                    else:
                        raise
            finish_stage_task()


__all__ = [
    "ApiRawVoltageSweepContactPlan",
    "ApiRawVoltageSweepRequest",
    "ApiRawVoltageSweepRequestError",
    "api_configure_meter_action",
    "api_prepare_route_meter_controller_action",
    "api_raw_voltage_sweep_action",
    "api_raw_voltage_sweep_contact_plan",
    "api_raw_voltage_sweep_error_response",
    "api_raw_voltage_sweep_meter_payload",
    "api_raw_voltage_sweep_missing_contact_response",
    "api_raw_voltage_sweep_request_from_payload",
    "api_raw_voltage_sweep_success_response",
]
