"""Shared route-shift calculations and formatting."""

from __future__ import annotations

from collections.abc import Sequence


def route_shift_from_stage_xy(
    current_stage_xy: Sequence[float],
    reference_stage_xy: Sequence[float],
) -> tuple[tuple[float, float], str]:
    offset_x = float(current_stage_xy[0]) - float(reference_stage_xy[0])
    offset_y = float(current_stage_xy[1]) - float(reference_stage_xy[1])
    offset_xy = (offset_x, offset_y)
    return offset_xy, route_shift_saved_message(offset_xy)


def route_shift_saved_message(offset_xy: Sequence[float]) -> str:
    offset_x = float(offset_xy[0])
    offset_y = float(offset_xy[1])
    return f"Route shift saved: dX={offset_x:+.4f} mm, dY={offset_y:+.4f} mm."


__all__ = [
    "route_shift_from_stage_xy",
    "route_shift_saved_message",
]
