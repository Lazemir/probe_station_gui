"""JSON-ready payload helpers for route measurement status surfaces."""

from __future__ import annotations

import math
from typing import Any


def route_measurement_record_payload(record: object) -> dict[str, Any]:
    return {
        "timestamp": getattr(record, "timestamp"),
        "structure_number": int(getattr(record, "structure_number")),
        "nplc": getattr(record, "nplc"),
        "measurement_type": getattr(record, "measurement_type"),
        "n_measurements": int(getattr(record, "n_measurements")),
        "resistance_ohm": json_ready(getattr(record, "resistance_ohm")),
        "resistance_rms_ohm": json_ready(getattr(record, "resistance_rms_ohm")),
        "relative_rms": json_ready(getattr(record, "relative_rms")),
        "status": getattr(record, "status"),
        "contact_quality": route_contact_quality_payload(
            getattr(record, "contact_quality")
        ),
        "raw_samples": [
            route_measurement_sample_payload(sample)
            for sample in getattr(record, "raw_samples")
        ],
    }


def route_measurement_sample_payload(sample: object) -> dict[str, Any]:
    return {
        field: json_ready(getattr(sample, field))
        for field in (
            "sample_index",
            "differential_resistance_ohm",
            "compliance_hit",
            "negative_source_voltage_v",
            "negative_measured_voltage_v",
            "negative_current_a",
            "negative_resistance_ohm",
            "positive_source_voltage_v",
            "positive_measured_voltage_v",
            "positive_current_a",
            "positive_resistance_ohm",
        )
    }


def route_contact_quality_payload(quality: object | None) -> dict[str, Any] | None:
    if quality is None:
        return None
    return {
        "assessed": bool(getattr(quality, "assessed")),
        "good": getattr(quality, "good"),
        "status": getattr(quality, "status"),
        "median_ohm": json_ready(getattr(quality, "median_ohm")),
        "mad_sigma_ohm": json_ready(getattr(quality, "mad_sigma_ohm")),
        "p95_abs_step_ohm": json_ready(getattr(quality, "p95_abs_step_ohm")),
        "span_ohm": json_ready(getattr(quality, "span_ohm")),
        "compliance_hits": int(getattr(quality, "compliance_hits")),
        "polarity_sign_mismatch_count": int(
            getattr(quality, "polarity_sign_mismatch_count")
        ),
        "reasons": list(getattr(quality, "reasons")),
        "failure_criteria": list(getattr(quality, "failure_criteria")),
    }


def route_contact_seek_payload(seek: object | None) -> dict[str, Any] | None:
    if seek is None:
        return None
    return {
        "found": bool(getattr(seek, "found")),
        "status": getattr(seek, "status"),
        "attempts": int(getattr(seek, "attempts")),
        "initial_status": getattr(seek, "initial_status"),
        "final_status": getattr(seek, "final_status"),
        "depth_below_down_mm": json_ready(getattr(seek, "depth_below_down_mm")),
        "axis_a_lowering_mm": json_ready(getattr(seek, "axis_a_lowering_mm")),
        "step_mm": json_ready(getattr(seek, "step_mm")),
        "max_depth_mm": json_ready(getattr(seek, "max_depth_mm")),
    }


def route_external_result_payload(
    payload: dict[str, Any],
    *,
    timestamp_utc: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": str(payload.get("status", "ok")).strip().lower() or "ok",
        "summary": payload.get("summary") if isinstance(payload.get("summary"), dict) else {},
        "files": payload.get("files") if isinstance(payload.get("files"), list) else [],
        "message": str(payload.get("message", "")).strip(),
        "timestamp_utc": str(timestamp_utc or ""),
    }
    request_id = payload.get("external_measurement_request_id")
    if request_id is None:
        request_id = payload.get("request_id")
    if request_id is not None:
        result["external_measurement_request_id"] = request_id
    return result


def route_artifact_record(
    *,
    artifact_id: str,
    data: bytes,
    filename: str,
    content_type: str,
    kind: str,
    metadata: dict[str, object],
    created_at_utc: str,
) -> dict[str, object]:
    return {
        "artifact_id": str(artifact_id),
        "filename": str(filename),
        "content_type": str(content_type),
        "kind": str(kind),
        "metadata": dict(metadata),
        "created_at_utc": str(created_at_utc),
        "size_bytes": len(data),
        "data": bytes(data),
    }


def route_artifact_public_payload(artifact: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in artifact.items() if key != "data"}


def json_ready(value: object) -> object:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def focus_result_to_dict(result: object | None) -> dict[str, object] | None:
    if result is None:
        return None
    if isinstance(result, dict):
        return dict(result)
    to_dict = getattr(result, "to_dict", None)
    if callable(to_dict):
        data = to_dict()
        return dict(data) if isinstance(data, dict) else None
    data: dict[str, object] = {}
    for source_name, target_name in (
        ("objective_name", "objective_name"),
        ("mode", "mode"),
        ("start_z_mm", "focus_start_z_mm"),
        ("best_z_mm", "focus_best_z_mm"),
        ("delta_um", "focus_delta_um"),
        ("best_score", "focus_score"),
        ("sample_count", "focus_sample_count"),
        ("edge_peak", "focus_edge_peak"),
        ("range_mm", "autofocus_range_mm"),
        ("fine_step_mm", "autofocus_fine_step_mm"),
        ("lower_z_mm", "autofocus_lower_z_mm"),
        ("upper_z_mm", "autofocus_upper_z_mm"),
    ):
        if hasattr(result, source_name):
            data[target_name] = getattr(result, source_name)
    return data or None
