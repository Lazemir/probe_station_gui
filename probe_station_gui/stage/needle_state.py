"""Pure state helpers for A-axis needle position classification."""

from __future__ import annotations


def axis_a_ready_from_state(
    *,
    needles_up: bool,
    needles_known: bool,
    controller_state_stale: bool,
    serial_is_open: bool,
) -> bool:
    return (
        bool(needles_up)
        and bool(needles_known)
        and not bool(controller_state_stale)
        and bool(serial_is_open)
    )


def normalized_needles_zone(
    raised: bool,
    *,
    known: bool,
    zone: str | None,
) -> str | None:
    if not known:
        return None
    zone_key = str(zone).strip().lower() if zone is not None else ""
    if zone_key in {"raise", "lift", "lower"}:
        return zone_key
    return "raise" if raised else "lower"


def needle_contact_boundary_lowering(
    *,
    down_lowering_mm: float | None,
    contact_zone_mm: float,
) -> float | None:
    if down_lowering_mm is None:
        return None
    contact_zone = max(0.0, float(contact_zone_mm))
    if contact_zone <= 1e-9:
        return None
    return max(0.0, float(down_lowering_mm) - contact_zone)


def needle_zone_for_lowering(
    current_lowering: float,
    *,
    raise_lowering_mm: float | None,
    down_lowering_mm: float | None,
    contact_zone_mm: float,
    tolerance: float,
) -> str | None:
    raise_lowering = 0.0 if raise_lowering_mm is None else float(raise_lowering_mm)
    if float(current_lowering) <= raise_lowering + float(tolerance):
        return "raise"
    boundary_lowering = needle_contact_boundary_lowering(
        down_lowering_mm=down_lowering_mm,
        contact_zone_mm=contact_zone_mm,
    )
    if boundary_lowering is not None:
        if float(current_lowering) <= boundary_lowering + float(tolerance):
            return "lift"
    if down_lowering_mm is not None:
        if float(current_lowering) >= float(down_lowering_mm) - float(tolerance):
            return "lower"
    return None
