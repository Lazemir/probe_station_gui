from __future__ import annotations

from dataclasses import dataclass
from typing import Any

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
    contact: object | None


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
        contact = context_result["contact"]
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
    contact: object | None,
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
    contact: object | None,
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


__all__ = [
    "ApiRawVoltageSweepContactPlan",
    "ApiRawVoltageSweepRequest",
    "ApiRawVoltageSweepRequestError",
    "api_raw_voltage_sweep_contact_plan",
    "api_raw_voltage_sweep_error_response",
    "api_raw_voltage_sweep_meter_payload",
    "api_raw_voltage_sweep_missing_contact_response",
    "api_raw_voltage_sweep_request_from_payload",
    "api_raw_voltage_sweep_success_response",
]
