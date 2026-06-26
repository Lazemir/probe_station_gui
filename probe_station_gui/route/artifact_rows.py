"""Pure CSV field lists and row builders for route artifacts."""

from __future__ import annotations

from pathlib import Path

from probe_station_gui.route.formatting import csv_bool, csv_float
from probe_station_gui.route.measurement_records import (
    RouteContactHeightRecord,
    RoutePhotoRecord,
)


ROUTE_PHOTO_FOCUS_MAP_FIELDS = (
    "timestamp",
    "route_name",
    "route_position",
    "route_total",
    "structure_number",
    "point_index",
    "point_id",
    "label",
    "design_x",
    "design_y",
    "stage_x",
    "stage_y",
    "photo_path",
    "objective_name",
    "focus_start_z_mm",
    "focus_best_z_mm",
    "focus_delta_um",
    "focus_score",
    "focus_sample_count",
    "focus_edge_peak",
    "autofocus_range_mm",
    "autofocus_fine_step_mm",
    "autofocus_lower_z_mm",
    "autofocus_upper_z_mm",
)


ROUTE_CONTACT_HEIGHT_MAP_FIELDS = (
    "timestamp",
    "route_name",
    "route_position",
    "route_total",
    "structure_number",
    "point_index",
    "point_id",
    "label",
    "design_x",
    "design_y",
    "stage_x",
    "stage_y",
    "measurement_status",
    "resistance_ohm",
    "resistance_rms_ohm",
    "relative_rms",
    "contact_quality",
    "contact_median_ohm",
    "contact_mad_sigma_ohm",
    "contact_p95_abs_step_ohm",
    "contact_span_ohm",
    "contact_compliance_hits",
    "contact_found",
    "contact_depth_below_down_mm",
    "contact_axis_a_lowering_mm",
    "contact_seek_used",
    "contact_seek_found",
    "contact_seek_status",
    "contact_seek_attempts",
    "contact_seek_initial_status",
    "contact_seek_final_status",
    "contact_seek_depth_below_down_mm",
    "contact_seek_axis_a_lowering_mm",
    "contact_seek_step_mm",
    "contact_seek_max_depth_mm",
)


def route_photo_focus_map_path(record: RoutePhotoRecord) -> Path:
    return Path(record.path).expanduser().resolve().parent / "route-photo-focus-map.csv"


def route_contact_height_map_path(csv_path: str | Path) -> Path:
    return (
        Path(csv_path).expanduser().resolve().parent / "route-contact-height-map.csv"
    )


def route_photo_focus_map_row(
    record: RoutePhotoRecord,
    position: int,
    total: int,
    *,
    route_name: str,
) -> dict[str, object]:
    focus = record.focus or {}
    return {
        "timestamp": record.timestamp,
        "route_name": route_name,
        "route_position": int(position),
        "route_total": int(total),
        "structure_number": int(record.structure_number),
        "point_index": int(record.point_index),
        "point_id": record.point_id,
        "label": record.label,
        "design_x": float(record.design_center[0]),
        "design_y": float(record.design_center[1]),
        "stage_x": float(record.stage_xy[0]),
        "stage_y": float(record.stage_xy[1]),
        "photo_path": record.path,
        "objective_name": focus.get("objective_name", ""),
        "focus_start_z_mm": focus.get("focus_start_z_mm", ""),
        "focus_best_z_mm": focus.get("focus_best_z_mm", ""),
        "focus_delta_um": focus.get("focus_delta_um", ""),
        "focus_score": focus.get("focus_score", ""),
        "focus_sample_count": focus.get("focus_sample_count", ""),
        "focus_edge_peak": focus.get("focus_edge_peak", ""),
        "autofocus_range_mm": focus.get("autofocus_range_mm", ""),
        "autofocus_fine_step_mm": focus.get("autofocus_fine_step_mm", ""),
        "autofocus_lower_z_mm": focus.get("autofocus_lower_z_mm", ""),
        "autofocus_upper_z_mm": focus.get("autofocus_upper_z_mm", ""),
    }


def route_contact_height_map_row(
    record: RouteContactHeightRecord,
    position: int,
    total: int,
    *,
    route_name: str,
) -> dict[str, object]:
    contact = record.contact_quality
    seek = record.contact_seek
    return {
        "timestamp": record.timestamp,
        "route_name": route_name,
        "route_position": int(position),
        "route_total": int(total),
        "structure_number": int(record.structure_number),
        "point_index": int(record.point_index),
        "point_id": record.point_id,
        "label": record.label,
        "design_x": float(record.design_center[0]),
        "design_y": float(record.design_center[1]),
        "stage_x": float(record.stage_xy[0]),
        "stage_y": float(record.stage_xy[1]),
        "measurement_status": record.measurement_status,
        "resistance_ohm": csv_float(record.resistance_ohm),
        "resistance_rms_ohm": csv_float(record.resistance_rms_ohm),
        "relative_rms": csv_float(record.relative_rms),
        "contact_quality": "" if contact is None else contact.status,
        "contact_median_ohm": "" if contact is None else csv_float(contact.median_ohm),
        "contact_mad_sigma_ohm": ""
        if contact is None
        else csv_float(contact.mad_sigma_ohm),
        "contact_p95_abs_step_ohm": ""
        if contact is None
        else csv_float(contact.p95_abs_step_ohm),
        "contact_span_ohm": "" if contact is None else csv_float(contact.span_ohm),
        "contact_compliance_hits": "" if contact is None else int(contact.compliance_hits),
        "contact_found": csv_bool(record.contact_found),
        "contact_depth_below_down_mm": csv_float(record.contact_depth_below_down_mm),
        "contact_axis_a_lowering_mm": csv_float(record.contact_axis_a_lowering_mm),
        "contact_seek_used": csv_bool(seek is not None),
        "contact_seek_found": "" if seek is None else csv_bool(seek.found),
        "contact_seek_status": "" if seek is None else seek.status,
        "contact_seek_attempts": "" if seek is None else int(seek.attempts),
        "contact_seek_initial_status": "" if seek is None else seek.initial_status,
        "contact_seek_final_status": "" if seek is None else seek.final_status,
        "contact_seek_depth_below_down_mm": ""
        if seek is None
        else csv_float(seek.depth_below_down_mm),
        "contact_seek_axis_a_lowering_mm": ""
        if seek is None
        else csv_float(seek.axis_a_lowering_mm),
        "contact_seek_step_mm": "" if seek is None else csv_float(seek.step_mm),
        "contact_seek_max_depth_mm": ""
        if seek is None
        else csv_float(seek.max_depth_mm),
    }


__all__ = [
    "ROUTE_CONTACT_HEIGHT_MAP_FIELDS",
    "ROUTE_PHOTO_FOCUS_MAP_FIELDS",
    "route_contact_height_map_path",
    "route_contact_height_map_row",
    "route_photo_focus_map_path",
    "route_photo_focus_map_row",
]
