"""Operation-mode vocabulary for route measurement runs."""

from __future__ import annotations


ROUTE_OPERATION_MEASURE = "measure"
ROUTE_OPERATION_PHOTO = "photo"
ROUTE_OPERATION_PHOTO_THEN_MEASURE = "photo_then_measure"
ROUTE_OPERATION_MODES = (
    ROUTE_OPERATION_MEASURE,
    ROUTE_OPERATION_PHOTO,
    ROUTE_OPERATION_PHOTO_THEN_MEASURE,
)


def normalize_route_operation_mode(value: object) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "measurement": ROUTE_OPERATION_MEASURE,
        "measure_only": ROUTE_OPERATION_MEASURE,
        "photo_only": ROUTE_OPERATION_PHOTO,
        "image": ROUTE_OPERATION_PHOTO,
        "capture": ROUTE_OPERATION_PHOTO,
        "photo_measure": ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        "photo+measure": ROUTE_OPERATION_PHOTO_THEN_MEASURE,
        "photo_then_measure": ROUTE_OPERATION_PHOTO_THEN_MEASURE,
    }
    normalized = aliases.get(text, text)
    if normalized in ROUTE_OPERATION_MODES:
        return normalized
    return ROUTE_OPERATION_MEASURE
