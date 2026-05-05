"""Capture the next Z-axis hysteresis segment and a long random walk.

This script assumes the previous Z section finished with the stage back at the
section start. It moves to one millimetre below the previous section's maximum,
raises A slowly, zeros the dial indicator API, captures Z up/down without
averaging, and then keeps running a live-saved random walk.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

import numpy as np
import serial

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.capture_axis_a_calibration import (
    CalibrationError,
    command_response,
    flush_input,
    query_status,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture Z section 3 and then run a long random-walk check."
    )
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--output-dir", type=Path, default=Path("calibrations"))
    parser.add_argument("--second-summary", type=Path)
    parser.add_argument("--second-max-z", type=float)
    parser.add_argument("--third-start-offset-mm", type=float, default=1.0)
    parser.add_argument("--third-top-z", type=float, default=23.46)
    parser.add_argument("--third-step-mm", type=float, default=0.0025)
    parser.add_argument("--settle-s", type=float, default=2.0)
    parser.add_argument("--measurement-feed", type=float, default=50.0)
    parser.add_argument("--slow-feed", type=float, default=1.0)
    parser.add_argument("--a-up-target", type=float, default=0.0)
    parser.add_argument("--random-step-mm", type=float, default=0.001)
    parser.add_argument("--random-min-segment-mm", type=float, default=0.1)
    parser.add_argument("--random-max-segment-mm", type=float, default=1.0)
    parser.add_argument("--random-segments", type=int, default=500)
    parser.add_argument("--seed", type=int)
    return parser.parse_args()


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def atomic_save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def atomic_save_npz(path: Path, **arrays: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("wb") as handle:
        np.savez(handle, **arrays)
    temporary_path.replace(path)


def read_indicator_once(api_base: str, *, timeout_s: float = 2.0) -> dict[str, Any] | None:
    try:
        with urlopen(f"{api_base.rstrip('/')}/reading", timeout=timeout_s) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, TimeoutError, URLError, json.JSONDecodeError) as exc:
        return {"reading_mm": math.nan, "error": str(exc)}
    try:
        value = float(payload["reading_mm"])
    except (KeyError, TypeError, ValueError):
        value = math.nan
    payload["reading_mm"] = value
    return payload


def zero_indicator(api_base: str) -> dict[str, Any]:
    request = Request(f"{api_base.rstrip('/')}/zero", method="POST")
    with urlopen(request, timeout=5.0) as response:
        return json.loads(response.read().decode("utf-8"))


def current_axis(status: dict[str, Any], axis_index: int) -> float:
    position = status.get("WPos") or status.get("MPos")
    if not isinstance(position, tuple) or len(position) <= axis_index:
        raise CalibrationError(f"Status frame has no axis {axis_index}: {status}")
    return float(position[axis_index])


def query_status_resilient(
    serial_connection: serial.Serial,
    *,
    timeout_s: float = 2.0,
    attempts: int = 5,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for _ in range(max(1, attempts)):
        try:
            return query_status(serial_connection, timeout_s=timeout_s)
        except CalibrationError as exc:
            last_error = exc
            time.sleep(0.2)
    if last_error is not None:
        raise last_error
    raise CalibrationError("Could not query controller status.")


def wait_idle_resilient(
    serial_connection: serial.Serial,
    *,
    timeout_s: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last_status_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            status = query_status(serial_connection, timeout_s=2.0)
        except CalibrationError as exc:
            last_status_error = exc
            time.sleep(0.5)
            continue
        state = str(status.get("state", "")).lower()
        if state == "idle":
            return status
        if state == "alarm":
            raise CalibrationError("Controller entered ALARM state.")
        time.sleep(0.1)
    if last_status_error is not None:
        raise CalibrationError(
            f"Controller did not return to IDLE in time; last status error: {last_status_error}"
        )
    raise CalibrationError("Controller did not return to IDLE in time.")


def command_response_resilient(
    serial_connection: serial.Serial,
    command: str,
    *,
    timeout_s: float = 5.0,
    retries: int = 3,
    tolerate_timeout: bool = False,
) -> list[str]:
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            return command_response(
                serial_connection,
                command,
                timeout_s=timeout_s,
            )
        except CalibrationError as exc:
            last_error = exc
            if "Timeout waiting for controller response" not in str(exc):
                raise
            if attempt + 1 < max(1, retries):
                print(
                    f"serial warning: timeout waiting for {command!r}; retrying",
                    flush=True,
                )
                time.sleep(0.5)
    if tolerate_timeout:
        print(
            f"serial warning: timeout waiting for {command!r}; continuing",
            flush=True,
        )
        return []
    if last_error is not None:
        raise last_error
    raise CalibrationError(f"Could not get controller response for {command}.")


def current_z(serial_connection: serial.Serial) -> float:
    return current_axis(query_status_resilient(serial_connection), 2)


def current_a(serial_connection: serial.Serial) -> float:
    return current_axis(query_status_resilient(serial_connection), 3)


def move_axis_absolute(
    serial_connection: serial.Serial,
    axis: str,
    target_mm: float,
    *,
    feedrate: float,
    timeout_margin_s: float = 60.0,
) -> dict[str, Any]:
    axis = axis.upper()
    axis_index = {"X": 0, "Y": 1, "Z": 2, "A": 3, "B": 4}[axis]
    status = query_status_resilient(serial_connection)
    start_mm = current_axis(status, axis_index)
    command_response_resilient(
        serial_connection,
        f"G1 {axis}{target_mm:.4f} F{feedrate:.4f}",
        timeout_s=5.0,
        retries=2,
        tolerate_timeout=True,
    )
    travel_s = abs(target_mm - start_mm) / max(feedrate, 1e-9) * 60.0
    return wait_idle_resilient(
        serial_connection,
        timeout_s=max(30.0, travel_s + timeout_margin_s),
    )


def load_second_max_z(args: argparse.Namespace) -> float:
    if args.second_max_z is not None:
        return float(args.second_max_z)
    summary_path = args.second_summary
    if summary_path is None:
        matches = sorted(
            args.output_dir.glob(
                "axis_z_spm6335_section2_nozero_up_down_step0p005_settle2p0_feed50_*.json"
            ),
            key=lambda path: path.stat().st_mtime,
        )
        if not matches:
            raise CalibrationError("No section2 summary JSON found.")
        summary_path = matches[-1]
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    for key in ("up_end_z_mm", "up_max_z_mm", "second_max_z_mm"):
        if key in payload:
            return float(payload[key])
    if "up" in payload and isinstance(payload["up"], dict):
        for key in ("end_z_mm", "max_z_mm"):
            if key in payload["up"]:
                return float(payload["up"][key])
    up_path_raw = payload.get("up_npz") or payload.get("up_path")
    if up_path_raw:
        up_path = Path(up_path_raw)
        if not up_path.is_absolute():
            up_path = summary_path.parent / up_path
        data = np.load(up_path)
        return float(np.nanmax(data["gcode"]))
    raise CalibrationError(f"Could not get section2 maximum Z from {summary_path}.")


def save_curve_npz(path: Path, gcode: list[float], indicator: list[float]) -> None:
    atomic_save_npz(
        path,
        gcode=np.asarray(gcode, dtype=float),
        indicator=np.asarray(indicator, dtype=float),
    )


def capture_z_curve(
    serial_connection: serial.Serial,
    *,
    api_base: str,
    start_z: float,
    end_z: float,
    step_mm: float,
    settle_s: float,
    feedrate: float,
    output_path: Path,
    label: str,
) -> dict[str, Any]:
    direction = 1.0 if end_z >= start_z else -1.0
    step = abs(step_mm) * direction
    gcode: list[float] = []
    indicator: list[float] = []
    live_path = output_path.with_name(output_path.stem + "_live.npz")
    point_index = 0
    target = start_z
    while (direction > 0 and target <= end_z + 1e-12) or (
        direction < 0 and target >= end_z - 1e-12
    ):
        target = min(target, end_z) if direction > 0 else max(target, end_z)
        status = move_axis_absolute(
            serial_connection,
            "Z",
            target,
            feedrate=feedrate,
            timeout_margin_s=20.0,
        )
        z_now = current_axis(status, 2)
        time.sleep(settle_s)
        payload = read_indicator_once(api_base)
        value = math.nan if payload is None else float(payload["reading_mm"])
        gcode.append(z_now)
        indicator.append(value)
        point_index += 1
        if point_index % 50 == 0 or abs(z_now - end_z) <= 1e-9:
            save_curve_npz(live_path, gcode, indicator)
            print(
                f"{label} progress points={point_index} "
                f"Z={z_now:.4f} indicator={value:.6f}",
                flush=True,
            )
        if abs(target - end_z) <= 1e-12:
            break
        target += step
    save_curve_npz(output_path, gcode, indicator)
    save_curve_npz(live_path, gcode, indicator)
    return {
        "points": point_index,
        "start_z_mm": float(gcode[0]) if gcode else None,
        "end_z_mm": float(gcode[-1]) if gcode else None,
        "start_indicator_mm": float(indicator[0]) if indicator else None,
        "end_indicator_mm": float(indicator[-1]) if indicator else None,
        "path": str(output_path),
        "live_path": str(live_path),
    }


def random_walk_target(
    current: float,
    *,
    low: float,
    high: float,
    min_segment: float,
    max_segment: float,
) -> float:
    amplitude = random.uniform(min_segment, max_segment)
    direction = random.choice((-1.0, 1.0))
    target = current + direction * amplitude
    if target > high:
        target = high - (target - high)
    if target < low:
        target = low + (low - target)
    return min(max(target, low), high)


def run_random_walk(
    serial_connection: serial.Serial,
    *,
    api_base: str,
    start_z: float,
    top_z: float,
    step_mm: float,
    settle_s: float,
    feedrate: float,
    min_segment: float,
    max_segment: float,
    segment_count: int,
    output_path: Path,
) -> dict[str, Any]:
    live_path = output_path.with_name(output_path.stem + "_live.npz")
    gcode: list[float] = []
    indicator: list[float] = []
    segment_indices: list[int] = []
    segment_targets: list[float] = []

    current = current_z(serial_connection)
    if abs(current - start_z) > 1e-4:
        status = move_axis_absolute(serial_connection, "Z", start_z, feedrate=feedrate)
        current = current_axis(status, 2)

    point_count = 0
    for segment_index in range(segment_count):
        target = random_walk_target(
            current,
            low=start_z,
            high=top_z,
            min_segment=min_segment,
            max_segment=max_segment,
        )
        direction = 1.0 if target >= current else -1.0
        step = abs(step_mm) * direction
        z_target = current
        segment_point_count = 0
        while (direction > 0 and z_target <= target + 1e-12) or (
            direction < 0 and z_target >= target - 1e-12
        ):
            z_target = min(z_target, target) if direction > 0 else max(z_target, target)
            status = move_axis_absolute(
                serial_connection,
                "Z",
                z_target,
                feedrate=feedrate,
                timeout_margin_s=20.0,
            )
            current = current_axis(status, 2)
            time.sleep(settle_s)
            payload = read_indicator_once(api_base)
            value = math.nan if payload is None else float(payload["reading_mm"])
            gcode.append(current)
            indicator.append(value)
            segment_indices.append(segment_index)
            segment_targets.append(target)
            point_count += 1
            segment_point_count += 1
            if point_count % 100 == 0:
                atomic_save_npz(
                    live_path,
                    gcode=np.asarray(gcode, dtype=float),
                    indicator=np.asarray(indicator, dtype=float),
                    segment=np.asarray(segment_indices, dtype=np.int32),
                    segment_target=np.asarray(segment_targets, dtype=float),
                )
                print(
                    f"walk progress segment={segment_index + 1}/{segment_count} "
                    f"points={point_count} Z={current:.4f} indicator={value:.6f}",
                    flush=True,
                )
            if abs(z_target - target) <= 1e-12:
                break
            z_target += step
        atomic_save_npz(
            live_path,
            gcode=np.asarray(gcode, dtype=float),
            indicator=np.asarray(indicator, dtype=float),
            segment=np.asarray(segment_indices, dtype=np.int32),
            segment_target=np.asarray(segment_targets, dtype=float),
        )
        print(
            f"walk segment_done {segment_index + 1}/{segment_count} "
            f"segment_points={segment_point_count} Z={current:.4f}",
            flush=True,
        )

    atomic_save_npz(
        output_path,
        gcode=np.asarray(gcode, dtype=float),
        indicator=np.asarray(indicator, dtype=float),
        segment=np.asarray(segment_indices, dtype=np.int32),
        segment_target=np.asarray(segment_targets, dtype=float),
    )
    return {
        "points": point_count,
        "segments": segment_count,
        "start_z_mm": start_z,
        "top_z_mm": top_z,
        "path": str(output_path),
        "live_path": str(live_path),
    }


def main() -> int:
    args = parse_args()
    if args.seed is not None:
        random.seed(args.seed)
    if args.third_step_mm <= 0 or args.random_step_mm <= 0:
        raise CalibrationError("Step sizes must be positive.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = now_stamp()
    second_max_z = load_second_max_z(args)
    third_start_z = second_max_z - args.third_start_offset_mm
    if third_start_z >= args.third_top_z:
        raise CalibrationError(
            f"Third start {third_start_z:.6f} is not below top {args.third_top_z:.6f}."
        )

    prefix = (
        f"axis_z_spm6335_section3_start{third_start_z:.3f}_top{args.third_top_z:.2f}"
        f"_step0p0025_settle{args.settle_s:.1f}_feed{args.measurement_feed:.0f}"
        f"_{timestamp}"
    ).replace(".", "p")
    up_path = args.output_dir / f"{prefix}_up.npz"
    down_path = args.output_dir / f"{prefix}_down.npz"
    walk_prefix = (
        f"axis_z_spm6335_section3_random_walk_start{third_start_z:.3f}"
        f"_top{args.third_top_z:.2f}_step0p001_settle{args.settle_s:.1f}"
        f"_segments{args.random_segments}_{timestamp}"
    ).replace(".", "p")
    walk_path = args.output_dir / f"{walk_prefix}.npz"
    summary_path = args.output_dir / f"{prefix}_summary.json"

    summary: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "port": args.port,
        "baud": args.baud,
        "api": args.api.rstrip("/"),
        "steps_per_mm": 6335,
        "second_max_z_mm": second_max_z,
        "third_start_z_mm": third_start_z,
        "third_top_z_mm": args.third_top_z,
        "third_step_mm": args.third_step_mm,
        "settle_s": args.settle_s,
        "measurement_feed_mm_min": args.measurement_feed,
        "slow_feed_mm_min": args.slow_feed,
        "a_up_target_mm": args.a_up_target,
        "random_step_mm": args.random_step_mm,
        "random_segments": args.random_segments,
        "complete": False,
    }
    atomic_save_json(summary_path, summary)

    print(
        f"section3 plan second_max_z={second_max_z:.6f} "
        f"third_start_z={third_start_z:.6f} top_z={args.third_top_z:.6f}",
        flush=True,
    )
    with serial.Serial(args.port, args.baud, timeout=0.2, write_timeout=2.0) as serial_connection:
        time.sleep(0.8)
        flush_input(serial_connection, quiet_s=0.3)
        print(
            f"initial Z={current_z(serial_connection):.6f} "
            f"A={current_a(serial_connection):.6f}",
            flush=True,
        )
        command_response_resilient(serial_connection, "G21", timeout_s=5.0)
        command_response_resilient(serial_connection, "G90", timeout_s=5.0)
        status = move_axis_absolute(
            serial_connection,
            "Z",
            third_start_z,
            feedrate=args.slow_feed,
            timeout_margin_s=120.0,
        )
        print(f"Z at third start: {current_axis(status, 2):.6f}", flush=True)
        status = move_axis_absolute(
            serial_connection,
            "A",
            args.a_up_target,
            feedrate=args.slow_feed,
            timeout_margin_s=120.0,
        )
        print(f"A raised to: {current_axis(status, 3):.6f}", flush=True)
        zero_payload = zero_indicator(args.api)
        summary["zero_payload"] = zero_payload
        summary["zeroed_at"] = datetime.now(timezone.utc).isoformat()
        atomic_save_json(summary_path, summary)
        time.sleep(args.settle_s)

        print("section3 up begin", flush=True)
        summary["up"] = capture_z_curve(
            serial_connection,
            api_base=args.api,
            start_z=third_start_z,
            end_z=args.third_top_z,
            step_mm=args.third_step_mm,
            settle_s=args.settle_s,
            feedrate=args.measurement_feed,
            output_path=up_path,
            label="section3_up",
        )
        atomic_save_json(summary_path, summary)

        print("section3 down begin", flush=True)
        summary["down"] = capture_z_curve(
            serial_connection,
            api_base=args.api,
            start_z=args.third_top_z,
            end_z=third_start_z,
            step_mm=args.third_step_mm,
            settle_s=args.settle_s,
            feedrate=args.measurement_feed,
            output_path=down_path,
            label="section3_down",
        )
        atomic_save_json(summary_path, summary)

        print("random walk begin", flush=True)
        summary["random_walk"] = run_random_walk(
            serial_connection,
            api_base=args.api,
            start_z=third_start_z,
            top_z=args.third_top_z,
            step_mm=args.random_step_mm,
            settle_s=args.settle_s,
            feedrate=args.measurement_feed,
            min_segment=args.random_min_segment_mm,
            max_segment=args.random_max_segment_mm,
            segment_count=args.random_segments,
            output_path=walk_path,
        )

    summary["complete"] = True
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_save_json(summary_path, summary)
    print(f"saved summary {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
