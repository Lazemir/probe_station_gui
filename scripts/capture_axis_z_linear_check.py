"""Capture a Z-axis linearity check with the localhost dial indicator API."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
import sys
from typing import Any

import serial
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.capture_axis_a_calibration import (
    CalibrationError,
    command_response,
    flush_input,
    read_until_ok,
    sample_indicator,
    wait_idle,
    write_command,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture Z-axis commanded position vs dial-indicator reading."
    )
    parser.add_argument("--port", default="COM3", help="FluidNC serial port.")
    parser.add_argument("--baud", type=int, default=115200, help="FluidNC baud rate.")
    parser.add_argument(
        "--api",
        default="http://127.0.0.1:8000",
        help="Dial indicator API base URL.",
    )
    parser.add_argument("--start-mm", type=float, default=0.0)
    parser.add_argument("--end-mm", type=float, default=10.0)
    parser.add_argument("--step-mm", type=float, default=0.01)
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--sample-delay-s", type=float, default=0.05)
    parser.add_argument("--settle-s", type=float, default=0.05)
    parser.add_argument("--feedrate", type=float, default=60.0)
    parser.add_argument(
        "--stop-indicator-delta-mm",
        type=float,
        default=0.0,
        help=(
            "Stop after a valid point whose indicator delta reaches this value. "
            "Disabled when 0."
        ),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("calibrations") / "axis_z_linear_check.json",
    )
    return parser.parse_args()


def atomic_save(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def save_points_npz(path: Path, points: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[float, float]] = []
    for point in points:
        gcode = point.get("commanded_z_mm")
        indicator = point.get("indicator_delta_mm")
        if gcode is None or indicator is None:
            continue
        rows.append((float(gcode), float(indicator)))
    data = np.asarray(rows, dtype=float)
    if data.size == 0:
        data = np.empty((0, 2), dtype=float)
    np.savez(path, gcode=data[:, 0], indicator=data[:, 1])


def current_axis_from_status(status: dict[str, Any], index: int) -> float:
    position = status.get("WPos") or status.get("MPos")
    if not isinstance(position, tuple) or len(position) <= index:
        raise CalibrationError(
            f"Status frame does not contain axis index {index}: {status}"
        )
    return float(position[index])


def home_z(serial_connection: serial.Serial) -> dict[str, Any]:
    flush_input(serial_connection)
    write_command(serial_connection, "$HZ")
    read_until_ok(serial_connection, timeout_s=45.0, description="$HZ")
    return wait_idle(serial_connection, timeout_s=45.0)


def move_z_absolute(
    serial_connection: serial.Serial,
    target_mm: float,
    *,
    feedrate: float,
) -> dict[str, Any]:
    command_response(serial_connection, f"G1 Z{target_mm:.4f} F{feedrate:.0f}", timeout_s=5.0)
    return wait_idle(serial_connection, timeout_s=20.0)


def main() -> int:
    args = parse_args()
    if args.step_mm <= 0:
        raise CalibrationError("--step-mm must be positive.")
    if args.samples <= 0:
        raise CalibrationError("--samples must be positive.")
    if args.end_mm <= args.start_mm:
        raise CalibrationError("--end-mm must be greater than --start-mm.")

    points: list[dict[str, Any]] = []
    skipped_points: list[dict[str, Any]] = []
    partial_output = args.output.with_suffix(args.output.suffix + ".partial")
    capture: dict[str, Any] = {
        "axis": "Z",
        "kind": "linear_check_only",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "controller": {
            "port": args.port,
            "baud": args.baud,
        },
        "range_mm": {"start": args.start_mm, "end": args.end_mm},
        "commanded_step_mm": args.step_mm,
        "feedrate_mm_min": args.feedrate,
        "samples_per_point": args.samples,
        "sample_delay_s": args.sample_delay_s,
        "settle_s": args.settle_s,
        "statistic": "median",
        "indicator_api": args.api.rstrip("/"),
        "stop_indicator_delta_mm": (
            args.stop_indicator_delta_mm if args.stop_indicator_delta_mm > 0 else None
        ),
        "stop_reason": None,
        "zero_indicator_mm": None,
        "complete": False,
        "points": points,
        "skipped_points": skipped_points,
    }

    print("Starting Z linear check", flush=True)
    print(f"Output: {args.output}", flush=True)

    with serial.Serial(
        args.port,
        args.baud,
        timeout=0.2,
        write_timeout=2.0,
    ) as serial_connection:
        time.sleep(0.8)
        flush_input(serial_connection, quiet_s=0.3)
        startup_lines = command_response(serial_connection, "$Startup/Show", timeout_s=8.0)
        capture["controller"]["startup_axis_lines"] = [
            line
            for line in startup_lines
            if "Axis Z" in line or "Configuration file" in line
        ]
        print(capture["controller"]["startup_axis_lines"], flush=True)

        print("Homing Z before capture.", flush=True)
        status = home_z(serial_connection)
        current_z = current_axis_from_status(status, 2)
        print(f"Z after homing: {current_z:.6f} mm.", flush=True)

        time.sleep(args.settle_s)
        zero_sample = sample_indicator(
            args.api,
            sample_count=args.samples,
            sample_delay_s=args.sample_delay_s,
            max_attempts=0,
        )
        if zero_sample is None:
            skipped_points.append(
                {
                    "commanded_z_mm": current_z,
                    "reason": "indicator samples did not update enough",
                }
            )
            print("Skip 0000: indicator did not update enough", flush=True)
        else:
            zero_indicator = float(zero_sample["median_mm"])
            capture["zero_indicator_mm"] = zero_indicator
            points.append(
                {
                    "commanded_z_mm": current_z,
                    "indicator_mm": zero_indicator,
                    "indicator_delta_mm": 0.0,
                    "sample_count": zero_sample["sample_count"],
                    "attempt_count": zero_sample["attempt_count"],
                    "unique_timestamp_count": zero_sample["unique_timestamp_count"],
                    "stale_timestamp_count": zero_sample["stale_timestamp_count"],
                    "tracking_ok_count": zero_sample["tracking_ok_count"],
                    "tracking_bad_count": zero_sample["tracking_bad_count"],
                }
            )
            print(
                f"Point 0000: Z={current_z:.4f} "
                f"indicator={zero_indicator:.6f} "
                f"tracking_bad={zero_sample['tracking_bad_count']}",
                flush=True,
            )
        atomic_save(partial_output, capture)

        command_response(serial_connection, "G21", timeout_s=5.0)
        command_response(serial_connection, "G90", timeout_s=5.0)

        point_index = 1
        target = args.start_mm + args.step_mm
        try:
            while target <= args.end_mm + 1e-9:
                target = min(target, args.end_mm)
                status = move_z_absolute(
                    serial_connection,
                    target,
                    feedrate=args.feedrate,
                )
                current_z = current_axis_from_status(status, 2)
                time.sleep(args.settle_s)
                sample = sample_indicator(
                    args.api,
                    sample_count=args.samples,
                    sample_delay_s=args.sample_delay_s,
                    max_attempts=0,
                )
                if sample is None:
                    skipped_points.append(
                        {
                            "commanded_z_mm": current_z,
                            "reason": "indicator samples did not update enough",
                        }
                    )
                    capture["updated_at"] = datetime.now(timezone.utc).isoformat()
                    atomic_save(partial_output, capture)
                    if point_index % 10 == 0 or abs(current_z - args.end_mm) <= 1e-9:
                        print(
                            f"Skip  {point_index:04d}: "
                            f"Z={current_z:.4f} indicator did not update enough",
                            flush=True,
                        )
                else:
                    median = float(sample["median_mm"])
                    zero_indicator = capture["zero_indicator_mm"]
                    delta = None if zero_indicator is None else median - float(zero_indicator)
                    points.append(
                        {
                            "commanded_z_mm": current_z,
                            "indicator_mm": median,
                            "indicator_delta_mm": delta,
                            "sample_count": sample["sample_count"],
                            "attempt_count": sample["attempt_count"],
                            "unique_timestamp_count": sample["unique_timestamp_count"],
                            "stale_timestamp_count": sample["stale_timestamp_count"],
                            "tracking_ok_count": sample["tracking_ok_count"],
                            "tracking_bad_count": sample["tracking_bad_count"],
                        }
                    )
                    capture["updated_at"] = datetime.now(timezone.utc).isoformat()
                    atomic_save(partial_output, capture)
                    if (
                        args.stop_indicator_delta_mm > 0
                        and delta is not None
                        and delta >= args.stop_indicator_delta_mm
                    ):
                        capture["stop_reason"] = "indicator_delta_reached"
                        print(
                            f"Stop: indicator delta {delta:.6f} mm reached "
                            f"at Z={current_z:.4f} mm.",
                            flush=True,
                        )
                        break
                    if point_index % 10 == 0 or abs(current_z - args.end_mm) <= 1e-9:
                        print(
                            f"Point {point_index:04d}: "
                            f"Z={current_z:.4f} indicator={median:.6f} "
                            f"delta={delta if delta is not None else float('nan'):.6f} "
                            f"tracking_bad={sample['tracking_bad_count']}",
                            flush=True,
                        )
                point_index += 1
                if abs(current_z - args.end_mm) <= 1e-9:
                    break
                target = round(args.start_mm + point_index * args.step_mm, 10)
        finally:
            print("Returning/homing Z after capture.", flush=True)
            try:
                home_z(serial_connection)
            except Exception as exc:
                print(f"WARNING: failed to home Z after capture: {exc}", flush=True)

    capture["complete"] = True
    capture["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_save(args.output, capture)
    save_points_npz(args.output.with_suffix(".npz"), points)
    try:
        partial_output.unlink()
    except FileNotFoundError:
        pass
    print(
        f"Saved {len(points)} Z check points to {args.output}. "
        f"Skipped {len(skipped_points)}.",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
