"""Capture an XY surface map of the circular probe-station table.

The script talks only to the running GUI API for stage moves and to the
localhost dial-indicator API for readings. It does not command Z/A motion.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np


STAGE_API = "http://127.0.0.1:8765"
INDICATOR_API = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[1]
STOP_FLAG = ROOT / "calibrations" / "STOP_table_surface_map.flag"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Capture a no-lift XY surface map on a circular table using the "
            "GUI control API and the dial-indicator API."
        )
    )
    parser.add_argument("--stage-api", default=STAGE_API)
    parser.add_argument("--indicator-api", default=INDICATOR_API)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "calibrations")
    parser.add_argument(
        "--resume-json",
        type=Path,
        help="Resume and append to an existing table_surface_map JSON capture.",
    )
    parser.add_argument("--table-diameter-mm", type=float, default=57.0)
    parser.add_argument(
        "--avoid-diameter-mm",
        type=float,
        default=2.0,
        help="Forbidden center diameter.",
    )
    parser.add_argument(
        "--center-margin-mm",
        type=float,
        default=0.25,
        help="Extra radial clearance around the forbidden center.",
    )
    parser.add_argument(
        "--edge-margin-mm",
        type=float,
        default=0.50,
        help="Radial clearance inside the physical table edge.",
    )
    parser.add_argument(
        "--inner-route-margin-mm",
        type=float,
        default=0.25,
        help="Extra radial offset for the innermost measured spiral.",
    )
    parser.add_argument(
        "--radial-pitch-mm",
        type=float,
        default=2.0,
        help="Approximate spacing between spiral turns.",
    )
    parser.add_argument(
        "--point-spacing-mm",
        type=float,
        default=2.0,
        help="Approximate travel distance between sampled route points.",
    )
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--sample-delay-s", type=float, default=0.03)
    parser.add_argument("--settle-s", type=float, default=0.08)
    parser.add_argument(
        "--move-tolerance-mm",
        type=float,
        default=0.03,
        help="Allowed final XY target error reported by the GUI API.",
    )
    parser.add_argument(
        "--max-indicator-delta-mm",
        type=float,
        default=0.1,
        help="Abort when the reading differs from the start by more than this.",
    )
    parser.add_argument(
        "--validate-step-mm",
        type=float,
        default=0.10,
        help="Sampling step used for route safety validation.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def request_json(
    method: str,
    url: str,
    payload: dict[str, Any] | None = None,
    *,
    timeout: float = 10.0,
) -> dict[str, Any]:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc}") from exc
    return json.loads(raw)


def stage_status(stage_api: str) -> dict[str, Any]:
    return request_json("GET", f"{stage_api.rstrip('/')}/api/v1/stage/status", timeout=5.0)


def indicator_reading(indicator_api: str) -> dict[str, Any]:
    return request_json("GET", f"{indicator_api.rstrip('/')}/reading", timeout=3.0)


def is_stage_idle(status: dict[str, Any]) -> bool:
    return (
        bool(status.get("connected"))
        and not bool(status.get("busy"))
        and status.get("state") == "Idle"
        and status.get("active_coordinate_axis") in (None, "")
        and not bool(status.get("pending_targets"))
    )


def wait_idle(stage_api: str, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_status: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_status = stage_status(stage_api)
        if is_stage_idle(last_status):
            return last_status
        time.sleep(0.05)
    raise TimeoutError(f"stage did not become idle within {timeout_s:.1f}s; last={last_status}")


def display_xy(status: dict[str, Any]) -> tuple[float, float]:
    display = status.get("display_position")
    if not isinstance(display, dict):
        raise RuntimeError(f"Stage status has no display_position: {status}")
    return (float(display["X"]), float(display["Y"]))


def display_axis(status: dict[str, Any], axis: str) -> float:
    display = status.get("display_position")
    if not isinstance(display, dict):
        raise RuntimeError(f"Stage status has no display_position: {status}")
    return float(display[axis.upper()])


def current_feedrate(status: dict[str, Any]) -> float:
    try:
        return max(0.1, float(status.get("current_feedrate_mm_min", 1.0)))
    except (TypeError, ValueError):
        return 1.0


def indicator_ok(payload: dict[str, Any]) -> bool:
    try:
        value = float(payload["reading_mm"])
    except (KeyError, TypeError, ValueError):
        return False
    return math.isfinite(value) and bool(payload.get("camera_ok")) and bool(payload.get("tracking_ok"))


def sample_indicator(
    indicator_api: str,
    *,
    samples: int,
    sample_delay_s: float,
) -> dict[str, Any]:
    values: list[float] = []
    raw: list[dict[str, Any]] = []
    attempts = 0
    max_attempts = max(samples * 4, samples + 8)
    while len(values) < samples and attempts < max_attempts:
        attempts += 1
        payload = indicator_reading(indicator_api)
        raw.append(payload)
        if indicator_ok(payload):
            values.append(float(payload["reading_mm"]))
        time.sleep(sample_delay_s)
    if len(values) < samples:
        raise RuntimeError(
            f"indicator did not provide {samples} valid samples; "
            f"valid={len(values)} attempts={attempts} last={raw[-1] if raw else None}"
        )
    timestamps = {str(item.get("timestamp", "")) for item in raw if item.get("timestamp")}
    return {
        "median_mm": float(statistics.median(values)),
        "mean_mm": float(statistics.fmean(values)),
        "min_mm": float(min(values)),
        "max_mm": float(max(values)),
        "sample_count": len(values),
        "attempt_count": attempts,
        "tracking_bad_count": sum(1 for item in raw if not item.get("tracking_ok")),
        "unique_timestamp_count": len(timestamps),
        "last_payload": raw[-1],
    }


def safe_radius_limits(args: argparse.Namespace) -> tuple[float, float, float, float]:
    table_radius = float(args.table_diameter_mm) / 2.0
    avoid_radius = float(args.avoid_diameter_mm) / 2.0
    inner_guard = avoid_radius + float(args.center_margin_mm)
    inner_route = inner_guard + float(args.inner_route_margin_mm)
    outer_guard = table_radius - float(args.edge_margin_mm)
    if inner_guard <= 0.0 or inner_route <= inner_guard or outer_guard <= inner_route:
        raise RuntimeError(
            "Invalid route radii: "
            f"inner_guard={inner_guard}, inner_route={inner_route}, outer_guard={outer_guard}"
        )
    return table_radius, inner_guard, inner_route, outer_guard


def point_is_safe(
    point: tuple[float, float],
    *,
    inner_guard: float,
    outer_guard: float,
) -> bool:
    radius = math.hypot(point[0], point[1])
    return inner_guard <= radius <= outer_guard


def iter_segment_samples(
    start: tuple[float, float],
    end: tuple[float, float],
    step_mm: float,
) -> Iterable[tuple[float, float]]:
    length = math.hypot(end[0] - start[0], end[1] - start[1])
    count = max(1, int(math.ceil(length / max(0.01, step_mm))))
    for idx in range(count + 1):
        t = idx / count
        yield (start[0] + (end[0] - start[0]) * t, start[1] + (end[1] - start[1]) * t)


def axis_order_move_is_safe(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    order: str,
    inner_guard: float,
    outer_guard: float,
    validate_step_mm: float,
) -> bool:
    if order == "XY":
        corner = (end[0], start[1])
    elif order == "YX":
        corner = (start[0], end[1])
    else:
        raise ValueError(f"Unsupported axis order: {order}")
    for segment_start, segment_end in ((start, corner), (corner, end)):
        for point in iter_segment_samples(segment_start, segment_end, validate_step_mm):
            if not point_is_safe(point, inner_guard=inner_guard, outer_guard=outer_guard):
                return False
    return True


def safe_axis_order(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    inner_guard: float,
    outer_guard: float,
    validate_step_mm: float,
) -> str | None:
    for order in ("XY", "YX"):
        if axis_order_move_is_safe(
            start,
            end,
            order=order,
            inner_guard=inner_guard,
            outer_guard=outer_guard,
            validate_step_mm=validate_step_mm,
        ):
            return order
    return None


def append_spiral(
    points: list[tuple[float, float]],
    *,
    theta: float,
    start_radius: float,
    end_radius: float,
    radial_pitch_mm: float,
    point_spacing_mm: float,
    inner_guard: float,
    outer_guard: float,
) -> tuple[float, float]:
    if abs(end_radius - start_radius) < 1e-6:
        return theta, start_radius
    direction = 1.0 if end_radius > start_radius else -1.0
    pitch_per_radian = float(radial_pitch_mm) / (2.0 * math.pi)
    radius = float(start_radius)
    while (direction > 0.0 and radius < end_radius - 1e-9) or (
        direction < 0.0 and radius > end_radius + 1e-9
    ):
        effective_radius = max(radius, inner_guard)
        dtheta = float(point_spacing_mm) / math.hypot(effective_radius, pitch_per_radian)
        dtheta = min(0.35, max(0.015, dtheta))
        theta += dtheta
        radius += direction * pitch_per_radian * dtheta
        if direction > 0.0:
            radius = min(radius, end_radius)
        else:
            radius = max(radius, end_radius)
        if radius < inner_guard or radius > outer_guard:
            continue
        points.append((radius * math.cos(theta), radius * math.sin(theta)))
    return theta, radius


def build_route(
    start_xy: tuple[float, float],
    *,
    inner_guard: float,
    inner_route: float,
    outer_guard: float,
    radial_pitch_mm: float,
    point_spacing_mm: float,
    validate_step_mm: float,
) -> list[tuple[float, float]]:
    start_radius = math.hypot(*start_xy)
    if not point_is_safe(start_xy, inner_guard=inner_guard, outer_guard=outer_guard):
        raise RuntimeError(
            f"Current XY is outside the safe annulus: X={start_xy[0]:.4f}, "
            f"Y={start_xy[1]:.4f}, r={start_radius:.4f}, "
            f"allowed=[{inner_guard:.4f}, {outer_guard:.4f}]"
        )
    theta = math.atan2(start_xy[1], start_xy[0])
    points: list[tuple[float, float]] = [(float(start_xy[0]), float(start_xy[1]))]
    if start_radius > inner_route + 1e-4:
        theta, _radius = append_spiral(
            points,
            theta=theta,
            start_radius=start_radius,
            end_radius=inner_route,
            radial_pitch_mm=radial_pitch_mm,
            point_spacing_mm=point_spacing_mm,
            inner_guard=inner_guard,
            outer_guard=outer_guard,
        )
    elif start_radius < inner_route - 1e-4:
        theta, _radius = append_spiral(
            points,
            theta=theta,
            start_radius=start_radius,
            end_radius=inner_route,
            radial_pitch_mm=radial_pitch_mm,
            point_spacing_mm=point_spacing_mm,
            inner_guard=inner_guard,
            outer_guard=outer_guard,
        )
    theta, _radius = append_spiral(
        points,
        theta=theta,
        start_radius=inner_route,
        end_radius=outer_guard,
        radial_pitch_mm=radial_pitch_mm,
        point_spacing_mm=point_spacing_mm,
        inner_guard=inner_guard,
        outer_guard=outer_guard,
    )
    cleaned: list[tuple[float, float]] = []
    for point in points:
        if cleaned and math.hypot(point[0] - cleaned[-1][0], point[1] - cleaned[-1][1]) < 1e-4:
            continue
        cleaned.append((float(point[0]), float(point[1])))
    for index, point in enumerate(cleaned):
        if not point_is_safe(point, inner_guard=inner_guard, outer_guard=outer_guard):
            raise RuntimeError(f"Unsafe route point {index}: {point}")
        if index > 0 and safe_axis_order(
            cleaned[index - 1],
            point,
            inner_guard=inner_guard,
            outer_guard=outer_guard,
            validate_step_mm=validate_step_mm,
        ) is None:
            raise RuntimeError(
                f"Unsafe axis-ordered move from point {index - 1} to {index}: "
                f"{cleaned[index - 1]} -> {point}"
            )
    return cleaned


def atomic_save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


def atomic_save_npz(path: Path, records: list[dict[str, Any]], metadata: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as handle:
        np.savez_compressed(
            handle,
            index=np.asarray([row["index"] for row in records], dtype=np.int32),
            x_mm=np.asarray([row["x_mm"] for row in records], dtype=float),
            y_mm=np.asarray([row["y_mm"] for row in records], dtype=float),
            radius_mm=np.asarray([row["radius_mm"] for row in records], dtype=float),
            theta_rad=np.asarray([row["theta_rad"] for row in records], dtype=float),
            indicator_mm=np.asarray([row["indicator_mm"] for row in records], dtype=float),
            indicator_delta_mm=np.asarray(
                [row["indicator_delta_mm"] for row in records],
                dtype=float,
            ),
            timestamp_s=np.asarray([row["timestamp_s"] for row in records], dtype=float),
            metadata_json=np.asarray(json.dumps(metadata, ensure_ascii=False, indent=2)),
        )
    tmp.replace(path)


def atomic_save_csv(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fieldnames = [
        "index",
        "x_mm",
        "y_mm",
        "radius_mm",
        "theta_rad",
        "indicator_mm",
        "indicator_delta_mm",
        "timestamp_s",
        "sample_count",
        "attempt_count",
        "tracking_bad_count",
    ]
    with tmp.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    tmp.replace(path)


def move_axis(
    stage_api: str,
    axis: str,
    target: float,
    *,
    current_value: float,
    feedrate_mm_min: float,
    move_tolerance_mm: float,
) -> dict[str, Any]:
    axis = axis.upper()
    distance = abs(float(target) - float(current_value))
    timeout_s = max(20.0, distance / max(feedrate_mm_min, 0.1) * 60.0 + 20.0)
    result = request_json(
        "POST",
        f"{stage_api.rstrip('/')}/api/v1/stage/move",
        {"coordinates": {axis: float(target)}},
        timeout=10.0,
    )
    if not result.get("accepted", False):
        raise RuntimeError(f"{axis} move rejected: {result}")
    return wait_axis_target(
        stage_api,
        axis,
        float(target),
        timeout_s=timeout_s,
        tolerance_mm=move_tolerance_mm,
    )


def wait_axis_target(
    stage_api: str,
    axis: str,
    target: float,
    *,
    timeout_s: float,
    tolerance_mm: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_status: dict[str, Any] | None = None
    consecutive_idle_hits = 0
    while time.monotonic() < deadline:
        last_status = stage_status(stage_api)
        value = display_axis(last_status, axis)
        if abs(value - target) <= tolerance_mm and is_stage_idle(last_status):
            consecutive_idle_hits += 1
            if consecutive_idle_hits >= 2:
                return last_status
        else:
            consecutive_idle_hits = 0
        time.sleep(0.05)
    raise TimeoutError(
        f"{axis} did not reach {target:.4f} within {timeout_s:.1f}s; "
        f"last={last_status}"
    )


def move_xy(
    stage_api: str,
    target: tuple[float, float],
    *,
    current_xy: tuple[float, float],
    feedrate_mm_min: float,
    inner_guard: float,
    outer_guard: float,
    validate_step_mm: float,
    move_tolerance_mm: float,
) -> dict[str, Any]:
    order = safe_axis_order(
        current_xy,
        target,
        inner_guard=inner_guard,
        outer_guard=outer_guard,
        validate_step_mm=validate_step_mm,
    )
    if order is None:
        raise RuntimeError(
            "No safe axis order for move "
            f"({current_xy[0]:+.4f}, {current_xy[1]:+.4f}) -> "
            f"({target[0]:+.4f}, {target[1]:+.4f})"
        )
    status = stage_status(stage_api)
    current = display_xy(status)
    for axis in order:
        if axis == "X":
            current_value = current[0]
            target_value = target[0]
        else:
            current_value = current[1]
            target_value = target[1]
        if abs(target_value - current_value) < 1e-5:
            continue
        status = move_axis(
            stage_api,
            axis,
            target_value,
            current_value=current_value,
            feedrate_mm_min=feedrate_mm_min,
            move_tolerance_mm=move_tolerance_mm,
        )
        current = display_xy(status)
    return status


def stop_requested() -> bool:
    return STOP_FLAG.exists()


def main() -> int:
    args = parse_args()
    if args.radial_pitch_mm <= 0.0 or args.point_spacing_mm <= 0.0:
        raise RuntimeError("Route pitch and point spacing must be positive.")
    if args.samples <= 0:
        raise RuntimeError("--samples must be positive.")
    if STOP_FLAG.exists():
        STOP_FLAG.unlink()

    resume_payload: dict[str, Any] | None = None
    if args.resume_json is not None:
        resume_payload = json.loads(args.resume_json.read_text(encoding="utf-8"))
        base = args.resume_json.with_suffix("")
    else:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = args.output_dir / f"table_surface_map_spiral_{stamp}"
    live_json = base.with_name(base.name + "_live.json")
    live_npz = base.with_name(base.name + "_live.npz")
    final_json = base.with_suffix(".json")
    final_npz = base.with_suffix(".npz")
    final_csv = base.with_suffix(".csv")

    status = wait_idle(args.stage_api, 5.0)
    homed = set(status.get("homed_axes") or [])
    missing = {"X", "Y"} - homed
    if missing:
        raise RuntimeError(f"XY axes are not homed: {sorted(missing)}")
    records: list[dict[str, Any]]
    if resume_payload is not None:
        old_metadata = resume_payload.get("metadata")
        if not isinstance(old_metadata, dict):
            raise RuntimeError(f"Resume JSON has no metadata object: {args.resume_json}")
        old_records = resume_payload.get("records")
        if not isinstance(old_records, list):
            raise RuntimeError(f"Resume JSON has no records list: {args.resume_json}")
        records = [dict(record) for record in old_records]
        records.sort(key=lambda record: int(record.get("index", -1)))
        start_xy = (
            float(old_metadata["start_xy_mm"]["x"]),
            float(old_metadata["start_xy_mm"]["y"]),
        )
        start_read = dict(old_metadata.get("start_indicator_sample") or {})
        start_indicator = float(old_metadata["start_indicator_mm"])
        table_radius = float(old_metadata["table_radius_mm"])
        inner_guard = float(old_metadata["inner_guard_radius_mm"])
        inner_route = float(old_metadata["inner_route_radius_mm"])
        outer_guard = float(old_metadata["outer_guard_radius_mm"])
        radial_pitch_mm = float(old_metadata["radial_pitch_mm"])
        point_spacing_mm = float(old_metadata["point_spacing_mm"])
    else:
        records = []
        start_xy = display_xy(status)
        start_read = sample_indicator(
            args.indicator_api,
            samples=args.samples,
            sample_delay_s=args.sample_delay_s,
        )
        start_indicator = float(start_read["median_mm"])
        table_radius, inner_guard, inner_route, outer_guard = safe_radius_limits(args)
        radial_pitch_mm = float(args.radial_pitch_mm)
        point_spacing_mm = float(args.point_spacing_mm)
    route = build_route(
        start_xy,
        inner_guard=inner_guard,
        inner_route=inner_route,
        outer_guard=outer_guard,
        radial_pitch_mm=radial_pitch_mm,
        point_spacing_mm=point_spacing_mm,
        validate_step_mm=args.validate_step_mm,
    )
    route_length = 0.0
    route_manhattan = 0.0
    for prev, current in zip(route, route[1:]):
        route_length += math.hypot(current[0] - prev[0], current[1] - prev[1])
        route_manhattan += abs(current[0] - prev[0]) + abs(current[1] - prev[1])
    if resume_payload is not None:
        metadata = dict(resume_payload["metadata"])
        metadata["resumed_at"] = datetime.now(timezone.utc).isoformat()
        metadata["resume_stage_status"] = status
    else:
        metadata = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "start_stage_status": status,
        }
    metadata.update(
        {
            "stage_api": args.stage_api.rstrip("/"),
            "indicator_api": args.indicator_api.rstrip("/"),
            "coordinate_display": status.get("coordinate_display"),
            "table_diameter_mm": metadata.get(
                "table_diameter_mm",
                float(args.table_diameter_mm),
            ),
            "table_radius_mm": table_radius,
            "avoid_diameter_mm": metadata.get(
                "avoid_diameter_mm",
                float(args.avoid_diameter_mm),
            ),
            "inner_guard_radius_mm": inner_guard,
            "inner_route_radius_mm": inner_route,
            "outer_guard_radius_mm": outer_guard,
            "radial_pitch_mm": radial_pitch_mm,
            "point_spacing_mm": point_spacing_mm,
            "settle_s": args.settle_s,
            "samples_per_point": args.samples,
            "sample_delay_s": args.sample_delay_s,
            "max_indicator_delta_mm": args.max_indicator_delta_mm,
            "move_tolerance_mm": args.move_tolerance_mm,
            "stop_flag": str(STOP_FLAG),
            "start_xy_mm": {"x": start_xy[0], "y": start_xy[1]},
            "start_indicator_sample": start_read,
            "start_indicator_mm": start_indicator,
            "route_points": len(route),
            "route_length_mm": route_length,
            "route_manhattan_mm": route_manhattan,
            "current_feedrate_mm_min": current_feedrate(status),
            "dry_run": bool(args.dry_run),
            "complete": False,
            "stop_reason": "running",
        }
    )
    atomic_save_json(live_json, {"metadata": metadata, "records": records})

    print(
        f"route points={len(route)} length={route_length:.1f}mm "
        f"manhattan={route_manhattan:.1f}mm feed={metadata['current_feedrate_mm_min']:.1f}mm/min",
        flush=True,
    )
    print(f"live_json={live_json}", flush=True)
    print(f"stop_flag={STOP_FLAG}", flush=True)
    if args.dry_run:
        metadata["complete"] = True
        metadata["stop_reason"] = "dry_run"
        metadata["route_preview"] = [{"x_mm": x, "y_mm": y} for x, y in route]
        atomic_save_json(final_json, {"metadata": metadata, "records": records})
        print(f"dry_run_json={final_json}", flush=True)
        return 0

    current_xy = display_xy(status)
    start_index = 0
    if records:
        last_index = max(int(record["index"]) for record in records)
        candidate_start = max(0, last_index)
        candidate_end = min(len(route), last_index + 8)
        candidates = range(candidate_start, candidate_end)
        nearest_index = min(
            candidates,
            key=lambda idx: math.hypot(
                current_xy[0] - route[idx][0],
                current_xy[1] - route[idx][1],
            ),
        )
        nearest_distance = math.hypot(
            current_xy[0] - route[nearest_index][0],
            current_xy[1] - route[nearest_index][1],
        )
        resume_tolerance = max(0.20, float(args.move_tolerance_mm) * 6.0)
        if nearest_distance > resume_tolerance:
            raise RuntimeError(
                "Current XY is not near the expected resume route segment: "
                f"current=({current_xy[0]:+.4f}, {current_xy[1]:+.4f}), "
                f"nearest_index={nearest_index}, distance={nearest_distance:.4f}"
            )
        start_index = last_index + 1 if nearest_index <= last_index else nearest_index
        print(
            f"resume from index={start_index} "
            f"current=({current_xy[0]:+.3f}, {current_xy[1]:+.3f})",
            flush=True,
        )

    timestamp_offset_s = (
        max(float(record.get("timestamp_s", 0.0)) for record in records)
        if records
        else 0.0
    )
    t0 = time.monotonic()
    stop_reason = "completed"
    try:
        for index in range(start_index, len(route)):
            target = route[index]
            if stop_requested():
                stop_reason = "user_stop_file"
                break
            status = stage_status(args.stage_api)
            current_xy = display_xy(status)
            target_error_before_move = math.hypot(
                current_xy[0] - target[0],
                current_xy[1] - target[1],
            )
            if target_error_before_move > float(args.move_tolerance_mm):
                feedrate = current_feedrate(status)
                status = move_xy(
                    args.stage_api,
                    target,
                    current_xy=current_xy,
                    feedrate_mm_min=feedrate,
                    inner_guard=inner_guard,
                    outer_guard=outer_guard,
                    validate_step_mm=args.validate_step_mm,
                    move_tolerance_mm=args.move_tolerance_mm,
                )
                current_xy = display_xy(status)
                err = math.hypot(current_xy[0] - target[0], current_xy[1] - target[1])
                if err > float(args.move_tolerance_mm):
                    stop_reason = f"target_error_{err:.4f}_mm"
                    break

            if args.settle_s > 0.0:
                time.sleep(args.settle_s)
            sample = sample_indicator(
                args.indicator_api,
                samples=args.samples,
                sample_delay_s=args.sample_delay_s,
            )
            indicator = float(sample["median_mm"])
            delta = indicator - start_indicator
            record = {
                "index": index,
                "x_mm": float(current_xy[0]),
                "y_mm": float(current_xy[1]),
                "radius_mm": math.hypot(current_xy[0], current_xy[1]),
                "theta_rad": math.atan2(current_xy[1], current_xy[0]),
                "indicator_mm": indicator,
                "indicator_delta_mm": delta,
                "timestamp_s": timestamp_offset_s + time.monotonic() - t0,
                "sample_count": sample["sample_count"],
                "attempt_count": sample["attempt_count"],
                "tracking_bad_count": sample["tracking_bad_count"],
            }
            records.append(record)
            if abs(delta) > float(args.max_indicator_delta_mm):
                stop_reason = f"indicator_delta_guard_{delta:+.4f}_mm"
                break
            if index % 10 == 0 or index == len(route) - 1:
                metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
                metadata["records"] = len(records)
                metadata["last_record"] = record
                metadata["stop_reason"] = "running"
                atomic_save_json(live_json, {"metadata": metadata, "records": records})
                atomic_save_npz(live_npz, records, metadata)
            if index % 25 == 0 or index == len(route) - 1:
                print(
                    f"point={index + 1}/{len(route)} "
                    f"X={current_xy[0]:+.3f} Y={current_xy[1]:+.3f} "
                    f"R={record['radius_mm']:.3f} "
                    f"indicator_delta={delta:+.6f}",
                    flush=True,
                )
    except KeyboardInterrupt:
        stop_reason = "keyboard_interrupt"
    except Exception as exc:
        stop_reason = f"error: {exc}"
        print(stop_reason, flush=True)
    finally:
        metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
        metadata["complete"] = stop_reason == "completed"
        metadata["stop_reason"] = stop_reason
        metadata["records"] = len(records)
        try:
            metadata["final_stage_status"] = stage_status(args.stage_api)
        except Exception as exc:
            metadata["final_stage_status_error"] = str(exc)
        try:
            metadata["final_indicator_reading"] = indicator_reading(args.indicator_api)
        except Exception as exc:
            metadata["final_indicator_error"] = str(exc)
        atomic_save_json(live_json, {"metadata": metadata, "records": records})
        if records:
            atomic_save_npz(live_npz, records, metadata)
            atomic_save_json(final_json, {"metadata": metadata, "records": records})
            atomic_save_npz(final_npz, records, metadata)
            atomic_save_csv(final_csv, records)
        print(f"stop_reason={stop_reason}", flush=True)
        print(f"records={len(records)}", flush=True)
        print(f"final_json={final_json}", flush=True)
        print(f"final_npz={final_npz}", flush=True)
        print(f"final_csv={final_csv}", flush=True)
    return 0 if stop_reason == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
