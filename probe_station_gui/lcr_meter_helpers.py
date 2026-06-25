"""Pure helper functions for LCR meter controller integrations."""

from __future__ import annotations

import inspect
import re


COM_RESOURCE_PATTERN = re.compile(r"^COM(?P<port>\d+)$", re.IGNORECASE)
GPIB_RESOURCE_PATTERN = re.compile(r"^GPIB(?P<board>\d*)::", re.IGNORECASE)


def normalize_resource_name(resource_name: str) -> str:
    """Translate ``COM4``-style names into VISA ASRL resources."""

    candidate = (resource_name or "").strip()
    match = COM_RESOURCE_PATTERN.fullmatch(candidate)
    if match:
        return f"ASRL{int(match.group('port'))}::INSTR"
    return candidate


def gpib_interface_resources_for(
    resources: tuple[str | None, ...],
) -> tuple[str, ...]:
    interfaces: list[str] = []
    seen: set[str] = set()
    for resource in resources:
        normalized = normalize_resource_name(resource or "")
        match = GPIB_RESOURCE_PATTERN.match(normalized)
        if match is None:
            continue
        board = match.group("board")
        interface = f"GPIB{board}::INTFC" if board else "GPIB::INTFC"
        key = interface.upper()
        if key in seen:
            continue
        seen.add(key)
        interfaces.append(interface)
    return tuple(interfaces)


def format_source_level_value(value: float) -> str:
    """Format source levels in the form accepted by the LCR-76200 firmware."""

    numeric = float(value)
    if numeric == 0.0 or abs(numeric) >= 0.1:
        return f"{numeric:.12g}"
    for scale, suffix in ((1e3, "m"), (1e6, "u"), (1e9, "n")):
        scaled = numeric * scale
        if 1.0 <= abs(scaled) < 1000.0:
            return f"{scaled:.12g}{suffix}"
    return f"{numeric:.12g}"


def parse_numeric_response(response: str) -> float:
    value = response.strip().upper()
    for suffix in ("OHM", "MS", "S", "V", "A"):
        if value.endswith(suffix):
            value = value[: -len(suffix)]
            break
    return float(value.strip())


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
