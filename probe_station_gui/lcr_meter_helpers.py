"""Pure helper functions for LCR meter controller integrations."""

from __future__ import annotations

import inspect


def callable_accepts_keyword(function: object, name: str) -> bool:
    try:
        signature = inspect.signature(function)
    except (TypeError, ValueError):
        return False
    for parameter in signature.parameters.values():
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            return True
        if (
            parameter.name == name
            and parameter.kind
            in {
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            }
        ):
            return True
    return False


def prepare_route_measurement_batch(
    preparer: object,
    count: int,
    *,
    source_list_count: int | None,
) -> None:
    if not callable(preparer):
        return
    if (
        source_list_count is not None
        and callable_accepts_keyword(preparer, "source_list_count")
    ):
        preparer(count, source_list_count=source_list_count)
        return
    preparer(count)


def read_route_measurement_batch(
    batch_reader: object,
    count: int,
    *,
    after_measurement: object | None,
) -> list[object]:
    if not callable(batch_reader):
        return []
    kwargs: dict[str, object] = {}
    if callable_accepts_keyword(batch_reader, "trigger"):
        kwargs["trigger"] = True
    if (
        after_measurement is not None
        and callable_accepts_keyword(batch_reader, "after_measurement")
    ):
        kwargs["after_measurement"] = after_measurement
    return list(batch_reader(count, **kwargs))


def voltage_sweep_point_to_dict(point: object) -> dict[str, object]:
    as_dict = getattr(point, "as_dict", None)
    if callable(as_dict):
        return dict(as_dict())
    if isinstance(point, dict):
        return dict(point)
    values: dict[str, object] = {}
    for name in (
        "source_voltage_v",
        "measured_voltage_v",
        "current_a",
        "resistance_ohm",
        "compliance_hit",
    ):
        if hasattr(point, name):
            values[name] = getattr(point, name)
    if values:
        return values
    return {"value": point}


def normalize_visa_role(role: object) -> str:
    return str(role or "").strip().lower().replace("-", "_")


def session_visa_resource_roles(
    session: object | None,
    *,
    meter_type: str,
) -> dict[str, dict[str, object]]:
    if session is None:
        return {}
    roles_getter = getattr(session, "visa_resource_roles", None)
    if not callable(roles_getter):
        return {}
    roles: dict[str, dict[str, object]] = {}
    for role, metadata in dict(roles_getter()).items():
        item = dict(metadata) if isinstance(metadata, dict) else {}
        item["role"] = str(item.get("role") or role)
        item["meter_type"] = meter_type
        item["available"] = True
        roles[str(role)] = item
    return roles
