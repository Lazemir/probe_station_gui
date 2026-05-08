"""Route and data helpers for dial-indicator surface mapping."""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np


INNER_ROUTE_CLEARANCE_MM = 0.10
RADIUS_EPSILON_MM = 1e-6


@dataclass
class SurfaceMapConfig:
    """Configuration for an XY surface map capture."""

    route_mode: str = "outer_circle"
    indicator_api: str = "http://127.0.0.1:8000"
    output_dir: str = "calibrations"
    inner_diameter_mm: float = 2.0
    outer_diameter_mm: float = 56.0
    circle_count: int = 1
    radial_pitch_mm: float = 2.0
    point_spacing_mm: float = 2.0
    grid_step_mm: float = 2.0
    settle_s: float = 0.08
    max_indicator_delta_mm: float = 0.0
    validate_step_mm: float = 0.10


@dataclass
class SurfaceMapRecord:
    """One measured point on a surface map route."""

    index: int
    x_mm: float
    y_mm: float
    radius_mm: float
    theta_rad: float
    indicator_mm: float
    indicator_delta_mm: float
    timestamp_s: float
    sample_count: int
    attempt_count: int
    tracking_bad_count: int


@dataclass
class SurfaceRoutePlan:
    """Movement waypoints plus the points that should be measured."""

    approach: list[tuple[float, float]]
    measurements: list[tuple[float, float]]

    def movement_points(self) -> list[tuple[float, float]]:
        return [*self.approach, *self.measurements]


def safe_radius_limits(config: SurfaceMapConfig) -> tuple[float, float, float, float]:
    inner_guard = max(0.0, float(config.inner_diameter_mm) / 2.0)
    outer_guard = float(config.outer_diameter_mm) / 2.0
    table_radius = outer_guard
    inner_route = inner_guard + INNER_ROUTE_CLEARANCE_MM
    if inner_guard <= 0.0 or outer_guard <= inner_guard:
        raise ValueError(
            "Invalid surface-map diameters: "
            f"inner_guard={inner_guard:.4f}, "
            f"inner_route={inner_route:.4f}, outer_guard={outer_guard:.4f}."
        )
    if inner_route >= outer_guard:
        raise ValueError(
            "Surface-map annulus is too narrow: "
            f"inner_route={inner_route:.4f}, outer_guard={outer_guard:.4f}."
        )
    return table_radius, inner_guard, inner_route, outer_guard


def point_is_safe(
    point: tuple[float, float],
    *,
    inner_guard_mm: float,
    outer_guard_mm: float,
) -> bool:
    radius = math.hypot(float(point[0]), float(point[1]))
    return (
        inner_guard_mm - RADIUS_EPSILON_MM
        <= radius
        <= outer_guard_mm + RADIUS_EPSILON_MM
    )


def iter_segment_samples(
    start: tuple[float, float],
    end: tuple[float, float],
    step_mm: float,
) -> Iterable[tuple[float, float]]:
    distance = math.hypot(float(end[0] - start[0]), float(end[1] - start[1]))
    count = max(1, int(math.ceil(distance / max(0.01, float(step_mm)))))
    for index in range(count + 1):
        t = index / count
        yield (
            float(start[0] + (end[0] - start[0]) * t),
            float(start[1] + (end[1] - start[1]) * t),
        )


def segment_is_safe(
    start: tuple[float, float],
    end: tuple[float, float],
    *,
    inner_guard_mm: float,
    outer_guard_mm: float,
    validate_step_mm: float,
) -> bool:
    for point in iter_segment_samples(start, end, validate_step_mm):
        if not point_is_safe(
            point,
            inner_guard_mm=inner_guard_mm,
            outer_guard_mm=outer_guard_mm,
        ):
            return False
    return True


def _append_unique(
    route: list[tuple[float, float]],
    point: tuple[float, float],
) -> None:
    if route and math.hypot(route[-1][0] - point[0], route[-1][1] - point[1]) < 1e-5:
        return
    route.append((float(point[0]), float(point[1])))


def build_surface_route(
    config: SurfaceMapConfig,
    start_xy: tuple[float, float],
) -> list[tuple[float, float]]:
    """Build a route that keeps the indicator in contact with the table."""

    mode = config.route_mode.strip().lower()
    if mode in {"outer", "circle", "outer_circle"}:
        route = _build_outer_circle_route(config, start_xy)
    elif mode in {"spiral", "map", "spiral_map"}:
        route = _build_spiral_route(config, start_xy)
    elif mode in {"grid", "raster"}:
        route = _build_grid_route(config, start_xy)
    else:
        raise ValueError(f"Unsupported surface-map route mode: {config.route_mode}")
    validate_surface_route(config, route)
    return route


def _start_angle(start_xy: tuple[float, float]) -> float:
    if abs(start_xy[0]) < 1e-9 and abs(start_xy[1]) < 1e-9:
        return 0.0
    return math.atan2(float(start_xy[1]), float(start_xy[0]))


def _angle_delta(start: float, end: float) -> float:
    return math.atan2(math.sin(end - start), math.cos(end - start))


def safe_approach_waypoints(
    config: SurfaceMapConfig,
    start_xy: tuple[float, float],
    target_xy: tuple[float, float],
) -> list[tuple[float, float]]:
    """Return travel-only waypoints from start to target inside the safe annulus."""

    _table_radius, inner_guard, _inner_route, outer_guard = safe_radius_limits(config)
    start = (float(start_xy[0]), float(start_xy[1]))
    target = (float(target_xy[0]), float(target_xy[1]))
    if not point_is_safe(start, inner_guard_mm=inner_guard, outer_guard_mm=outer_guard):
        radius = math.hypot(start[0], start[1])
        raise ValueError(
            "Current XY is outside the safe surface-map annulus: "
            f"r={radius:.4f}, allowed=[{inner_guard:.4f}, {outer_guard:.4f}]."
        )
    if not point_is_safe(target, inner_guard_mm=inner_guard, outer_guard_mm=outer_guard):
        radius = math.hypot(target[0], target[1])
        raise ValueError(
            "Surface-map first target is outside the safe annulus: "
            f"r={radius:.4f}, allowed=[{inner_guard:.4f}, {outer_guard:.4f}]."
        )
    if segment_is_safe(
        start,
        target,
        inner_guard_mm=inner_guard,
        outer_guard_mm=outer_guard,
        validate_step_mm=config.validate_step_mm,
    ):
        return []

    approach_radius = outer_guard
    start_theta = _start_angle(start)
    target_theta = _start_angle(target)
    delta = _angle_delta(start_theta, target_theta)
    spacing = max(0.05, min(float(config.point_spacing_mm), float(config.grid_step_mm)))
    max_safe_angle = 2.0 * math.acos(
        max(-1.0, min(1.0, inner_guard / max(inner_guard + 1e-9, approach_radius)))
    )
    angle_step = max(0.01, min(0.35, spacing / max(0.05, approach_radius), max_safe_angle * 0.8))
    arc_steps = max(1, int(math.ceil(abs(delta) / angle_step)))
    waypoints: list[tuple[float, float]] = []
    start_anchor = (
        approach_radius * math.cos(start_theta),
        approach_radius * math.sin(start_theta),
    )
    if math.hypot(start_anchor[0] - start[0], start_anchor[1] - start[1]) >= 1e-5:
        _append_unique(waypoints, start_anchor)
    for index in range(1, arc_steps + 1):
        theta = start_theta + delta * index / arc_steps
        _append_unique(
            waypoints,
            (
                approach_radius * math.cos(theta),
                approach_radius * math.sin(theta),
            ),
        )
    if waypoints and math.hypot(waypoints[-1][0] - target[0], waypoints[-1][1] - target[1]) < 1e-5:
        waypoints.pop()

    validate_surface_route(config, [*waypoints, target], start_xy=start)
    return waypoints


def build_surface_route_plan(
    config: SurfaceMapConfig,
    start_xy: tuple[float, float],
) -> SurfaceRoutePlan:
    measurements = build_surface_route(config, start_xy)
    approach = safe_approach_waypoints(config, start_xy, measurements[0])
    validate_surface_route(config, [*approach, *measurements], start_xy=start_xy)
    return SurfaceRoutePlan(approach=approach, measurements=measurements)


def _circle_point_count(radius: float, spacing_mm: float, inner_guard_mm: float) -> int:
    spacing_count = int(math.ceil((2.0 * math.pi * radius) / max(0.05, spacing_mm)))
    if radius <= inner_guard_mm:
        raise ValueError(
            f"Circle radius {radius:.4f} must be greater than "
            f"inner guard {inner_guard_mm:.4f}."
        )
    max_safe_angle = 2.0 * math.acos(max(-1.0, min(1.0, inner_guard_mm / radius)))
    safety_count = int(math.ceil((2.0 * math.pi) / max(1e-6, max_safe_angle)))
    return max(12, spacing_count, safety_count)


def _build_outer_circle_route(
    config: SurfaceMapConfig,
    start_xy: tuple[float, float],
) -> list[tuple[float, float]]:
    _table_radius, inner_guard, inner_route, outer_guard = safe_radius_limits(config)
    circle_count = max(1, int(config.circle_count))
    start_circle_radius = inner_route
    end_circle_radius = outer_guard
    if circle_count == 1:
        radii = [end_circle_radius]
    else:
        radii = [
            float(value)
            for value in np.linspace(start_circle_radius, end_circle_radius, circle_count)
        ]
    for radius in radii:
        if radius < inner_guard or radius > outer_guard:
            raise ValueError(
                f"Circle radius {radius:.4f} is outside "
                f"[{inner_guard:.4f}, {outer_guard:.4f}]."
            )
    route: list[tuple[float, float]] = []
    theta0 = _start_angle(start_xy)
    spacing = max(0.05, float(config.point_spacing_mm))
    for radius in radii:
        _append_unique(route, (radius * math.cos(theta0), radius * math.sin(theta0)))
        points_per_circle = _circle_point_count(radius, spacing, inner_guard)
        for index in range(1, points_per_circle + 1):
            theta = theta0 + 2.0 * math.pi * index / points_per_circle
            _append_unique(
                route,
                (radius * math.cos(theta), radius * math.sin(theta)),
            )
    return route


def _append_spiral(
    route: list[tuple[float, float]],
    *,
    theta: float,
    start_radius: float,
    end_radius: float,
    radial_pitch_mm: float,
    point_spacing_mm: float,
    inner_guard_mm: float,
    outer_guard_mm: float,
) -> tuple[float, float]:
    if abs(end_radius - start_radius) < 1e-6:
        return theta, start_radius
    direction = 1.0 if end_radius > start_radius else -1.0
    pitch_per_radian = max(0.01, float(radial_pitch_mm)) / (2.0 * math.pi)
    radius = float(start_radius)
    while (direction > 0.0 and radius < end_radius - 1e-9) or (
        direction < 0.0 and radius > end_radius + 1e-9
    ):
        effective_radius = max(abs(radius), inner_guard_mm)
        dtheta = max(
            0.015,
            min(0.35, float(point_spacing_mm) / math.hypot(effective_radius, pitch_per_radian)),
        )
        theta += dtheta
        radius += direction * pitch_per_radian * dtheta
        radius = min(radius, end_radius) if direction > 0.0 else max(radius, end_radius)
        if inner_guard_mm <= radius <= outer_guard_mm:
            _append_unique(route, (radius * math.cos(theta), radius * math.sin(theta)))
    return theta, radius


def _build_spiral_route(
    config: SurfaceMapConfig,
    start_xy: tuple[float, float],
) -> list[tuple[float, float]]:
    _table_radius, inner_guard, inner_route, outer_guard = safe_radius_limits(config)
    spiral_inner = inner_route
    spiral_outer = outer_guard
    if spiral_inner < inner_guard:
        raise ValueError(
            f"Spiral inner radius {spiral_inner:.4f} is inside "
            f"the safe minimum {inner_guard:.4f}."
        )
    if spiral_outer > outer_guard:
        raise ValueError(
            f"Spiral outer radius {spiral_outer:.4f} exceeds "
            f"the safe maximum {outer_guard:.4f}."
        )
    if spiral_outer <= spiral_inner:
        raise ValueError("Spiral outer radius must be greater than inner radius.")
    theta = _start_angle(start_xy)
    route: list[tuple[float, float]] = []
    _append_unique(route, (spiral_inner * math.cos(theta), spiral_inner * math.sin(theta)))
    _append_spiral(
        route,
        theta=theta,
        start_radius=spiral_inner,
        end_radius=spiral_outer,
        radial_pitch_mm=config.radial_pitch_mm,
        point_spacing_mm=config.point_spacing_mm,
        inner_guard_mm=inner_guard,
        outer_guard_mm=outer_guard,
    )
    return route


def _build_grid_route(
    config: SurfaceMapConfig,
    start_xy: tuple[float, float],
) -> list[tuple[float, float]]:
    _table_radius, inner_guard, inner_route, outer_guard = safe_radius_limits(config)
    effective_start = (float(start_xy[0]), float(start_xy[1]))
    if not point_is_safe(
        effective_start,
        inner_guard_mm=inner_guard,
        outer_guard_mm=outer_guard,
    ):
        effective_start = (outer_guard, 0.0)
    step = max(0.05, float(config.grid_step_mm))
    half_steps = int(math.floor(outer_guard / step))
    coordinates = [index * step for index in range(-half_steps, half_steps + 1)]
    if not coordinates:
        coordinates = [0.0]
    runs: list[list[tuple[float, float]]] = []
    candidate_count = 0
    for y in coordinates:
        current_run: list[tuple[float, float]] = []
        for x in coordinates:
            radius = math.hypot(float(x), float(y))
            if inner_route <= radius <= outer_guard:
                point = (float(x), float(y))
                if current_run and not segment_is_safe(
                    current_run[-1],
                    point,
                    inner_guard_mm=inner_guard,
                    outer_guard_mm=outer_guard,
                    validate_step_mm=config.validate_step_mm,
                ):
                    runs.append(current_run)
                    candidate_count += len(current_run)
                    current_run = []
                current_run.append(point)
        if current_run:
            runs.append(current_run)
            candidate_count += len(current_run)
    if candidate_count > 12000:
        raise ValueError(
            f"Grid route has too many points ({candidate_count}); increase grid step."
        )
    route: list[tuple[float, float]] = []
    current = effective_start
    while runs:
        best_index: int | None = None
        best_reversed = False
        best_distance = math.inf
        for index, run in enumerate(runs):
            for reverse, point in ((False, run[0]), (True, run[-1])):
                if not segment_is_safe(
                    current,
                    point,
                    inner_guard_mm=inner_guard,
                    outer_guard_mm=outer_guard,
                    validate_step_mm=config.validate_step_mm,
                ):
                    continue
                distance = math.hypot(point[0] - current[0], point[1] - current[1])
                if distance < best_distance:
                    best_index = index
                    best_reversed = reverse
                    best_distance = distance
        if best_index is None:
            remaining_points = [point for run in runs for point in run]
            backtrack_points = list(reversed(route[:-1]))
            backtrack_index = 0
            while remaining_points:
                best_point_index: int | None = None
                best_point_distance = math.inf
                for index, point in enumerate(remaining_points):
                    if not segment_is_safe(
                        current,
                        point,
                        inner_guard_mm=inner_guard,
                        outer_guard_mm=outer_guard,
                        validate_step_mm=config.validate_step_mm,
                    ):
                        continue
                    distance = math.hypot(point[0] - current[0], point[1] - current[1])
                    if distance < best_point_distance:
                        best_point_index = index
                        best_point_distance = distance
                if best_point_index is None:
                    while backtrack_index < len(backtrack_points):
                        waypoint = backtrack_points[backtrack_index]
                        backtrack_index += 1
                        if math.hypot(
                            waypoint[0] - current[0],
                            waypoint[1] - current[1],
                        ) < 1e-9:
                            continue
                        if segment_is_safe(
                            current,
                            waypoint,
                            inner_guard_mm=inner_guard,
                            outer_guard_mm=outer_guard,
                            validate_step_mm=config.validate_step_mm,
                        ):
                            current = waypoint
                            route.append(current)
                            break
                    else:
                        raise ValueError("Grid route cannot find a safe next point.")
                    continue
                current = remaining_points.pop(best_point_index)
                route.append(current)
                backtrack_points = list(reversed(route[:-1]))
                backtrack_index = 0
            return route
        run = runs.pop(best_index)
        if best_reversed:
            run = list(reversed(run))
        route.extend(run)
        current = route[-1]
    return route


def validate_surface_route(
    config: SurfaceMapConfig,
    route: list[tuple[float, float]],
    *,
    start_xy: tuple[float, float] | None = None,
) -> None:
    if not route:
        raise ValueError("Surface-map route is empty.")
    _table_radius, inner_guard, _inner_route, outer_guard = safe_radius_limits(config)
    if start_xy is not None and not segment_is_safe(
        start_xy,
        route[0],
        inner_guard_mm=inner_guard,
        outer_guard_mm=outer_guard,
        validate_step_mm=config.validate_step_mm,
    ):
        raise ValueError(
            f"Unsafe approach segment: {start_xy!r} -> {route[0]!r}."
        )
    for index, point in enumerate(route):
        if not point_is_safe(point, inner_guard_mm=inner_guard, outer_guard_mm=outer_guard):
            raise ValueError(f"Unsafe surface-map point {index}: {point!r}.")
        if index > 0 and not segment_is_safe(
            route[index - 1],
            point,
            inner_guard_mm=inner_guard,
            outer_guard_mm=outer_guard,
            validate_step_mm=config.validate_step_mm,
        ):
            raise ValueError(
                f"Unsafe surface-map segment {index - 1}->{index}: "
                f"{route[index - 1]!r} -> {point!r}."
            )


def route_length_mm(route: list[tuple[float, float]]) -> float:
    return float(
        sum(
            math.hypot(current[0] - previous[0], current[1] - previous[1])
            for previous, current in zip(route, route[1:])
        )
    )


def route_metadata(
    config: SurfaceMapConfig,
    route: list[tuple[float, float]],
    *,
    approach_route: list[tuple[float, float]] | None = None,
    start_xy: tuple[float, float] | None = None,
) -> dict[str, Any]:
    table_radius, inner_guard, inner_route, outer_guard = safe_radius_limits(config)
    approach = approach_route or []
    movement_route = [*approach, *route]
    movement_length_points = [*movement_route]
    if start_xy is not None:
        movement_length_points.insert(0, (float(start_xy[0]), float(start_xy[1])))
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "config": asdict(config),
        "table_radius_mm": table_radius,
        "inner_guard_radius_mm": inner_guard,
        "inner_route_radius_mm": inner_route,
        "outer_guard_radius_mm": outer_guard,
        "route_points": len(route),
        "measurement_points": len(route),
        "approach_points": len(approach),
        "movement_points": len(movement_route),
        "route_length_mm": route_length_mm(movement_length_points),
        "measurement_route_length_mm": route_length_mm(route),
    }


def records_summary(records: list[SurfaceMapRecord]) -> dict[str, Any]:
    if not records:
        return {"points": 0}
    z = np.asarray([record.indicator_delta_mm for record in records], dtype=float)
    r = np.asarray([record.radius_mm for record in records], dtype=float)
    return {
        "points": int(len(records)),
        "radius_min_mm": float(np.nanmin(r)),
        "radius_max_mm": float(np.nanmax(r)),
        "indicator_delta_min_mm": float(np.nanmin(z)),
        "indicator_delta_max_mm": float(np.nanmax(z)),
        "indicator_delta_span_mm": float(np.nanmax(z) - np.nanmin(z)),
        "indicator_delta_mean_mm": float(np.nanmean(z)),
        "indicator_delta_std_mm": float(np.nanstd(z)),
        "indicator_delta_max_abs_mm": float(np.nanmax(np.abs(z))),
    }


def save_surface_map(
    base_path: Path,
    *,
    metadata: dict[str, Any],
    records: list[SurfaceMapRecord],
) -> tuple[Path, Path, Path, Path]:
    base_path.parent.mkdir(parents=True, exist_ok=True)
    json_path = base_path.with_suffix(".json")
    csv_path = base_path.with_suffix(".csv")
    npz_path = base_path.with_suffix(".npz")
    summary_path = base_path.with_name(base_path.name + "_summary.json")
    rows = [asdict(record) for record in records]
    payload = {
        "metadata": metadata,
        "summary": records_summary(records),
        "records": rows,
    }
    json_tmp = json_path.with_suffix(".json.tmp")
    json_tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    json_tmp.replace(json_path)

    import csv

    csv_tmp = csv_path.with_suffix(".csv.tmp")
    with csv_tmp.open("w", encoding="utf-8", newline="") as handle:
        fieldnames = list(asdict(records[0]).keys()) if records else list(SurfaceMapRecord.__annotations__)
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    csv_tmp.replace(csv_path)

    summary_tmp = summary_path.with_suffix(".json.tmp")
    summary_tmp.write_text(
        json.dumps(payload["summary"], ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    summary_tmp.replace(summary_path)

    npz_tmp = npz_path.with_suffix(".npz.tmp")
    with npz_tmp.open("wb") as handle:
        np.savez_compressed(
            handle,
            index=np.asarray([record.index for record in records], dtype=np.int32),
            x_mm=np.asarray([record.x_mm for record in records], dtype=float),
            y_mm=np.asarray([record.y_mm for record in records], dtype=float),
            radius_mm=np.asarray([record.radius_mm for record in records], dtype=float),
            theta_rad=np.asarray([record.theta_rad for record in records], dtype=float),
            indicator_mm=np.asarray([record.indicator_mm for record in records], dtype=float),
            indicator_delta_mm=np.asarray(
                [record.indicator_delta_mm for record in records],
                dtype=float,
            ),
            timestamp_s=np.asarray([record.timestamp_s for record in records], dtype=float),
            metadata_json=np.asarray(json.dumps(metadata, ensure_ascii=False, indent=2)),
        )
    npz_tmp.replace(npz_path)
    return json_path, csv_path, npz_path, summary_path


__all__ = [
    "SurfaceMapConfig",
    "SurfaceMapRecord",
    "SurfaceRoutePlan",
    "build_surface_route_plan",
    "build_surface_route",
    "records_summary",
    "route_length_mm",
    "route_metadata",
    "safe_radius_limits",
    "safe_approach_waypoints",
    "save_surface_map",
    "segment_is_safe",
    "validate_surface_route",
]
