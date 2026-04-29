"""Capture FluidNC A-axis calibration with a localhost dial indicator API."""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

import serial


AXIS_LIMIT_PATTERN = re.compile(
    r"^\[MSG:INFO: Axis (?P<axis>[A-Za-z]) "
    r"\((?P<min>-?\d+(?:\.\d+)?),(?P<max>-?\d+(?:\.\d+)?)\)\]"
)
STATUS_PATTERN = re.compile(r"^<(?P<body>[^>]*)>")


class CalibrationError(RuntimeError):
    """Raised when the calibration capture cannot continue safely."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture A-axis commanded position vs dial-indicator reading."
    )
    parser.add_argument("--port", default="COM3", help="FluidNC serial port.")
    parser.add_argument("--baud", type=int, default=115200, help="FluidNC baud rate.")
    parser.add_argument(
        "--api",
        default="http://127.0.0.1:8000",
        help="Dial indicator API base URL.",
    )
    parser.add_argument(
        "--step-mm",
        type=float,
        default=0.02,
        help="Commanded A-axis step size while lowering.",
    )
    parser.add_argument(
        "--samples",
        type=int,
        default=20,
        help="Valid indicator samples to average at each point.",
    )
    parser.add_argument(
        "--sample-delay-s",
        type=float,
        default=0.05,
        help="Delay between indicator samples.",
    )
    parser.add_argument(
        "--max-sample-attempts",
        type=int,
        default=0,
        help=(
            "Maximum reading attempts per point. "
            "Default is max(samples * 5, samples + 20)."
        ),
    )
    parser.add_argument(
        "--settle-s",
        type=float,
        default=0.05,
        help="Delay after each move before sampling.",
    )
    parser.add_argument(
        "--feedrate",
        type=float,
        default=60.0,
        help="A-axis feedrate in mm/min for calibration moves.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("calibrations") / "axis_a_calibration.json",
        help="Output JSON path.",
    )
    parser.add_argument(
        "--leave-down",
        action="store_true",
        help="Do not raise/home A at the end of the capture.",
    )
    return parser.parse_args()


def save_calibration(path: Path, calibration: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(calibration, handle, indent=2, ensure_ascii=False)


def read_indicator(api_base: str, timeout_s: float = 2.0) -> dict[str, Any]:
    with urlopen(f"{api_base.rstrip('/')}/reading", timeout=timeout_s) as response:
        payload = json.loads(response.read().decode("utf-8"))
    try:
        reading = float(payload["reading_mm"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CalibrationError(f"Indicator response has no numeric reading_mm: {payload}") from exc
    if not math.isfinite(reading):
        raise CalibrationError(f"Indicator reading is not finite: {payload}")
    if not bool(payload.get("camera_ok", False)):
        raise CalibrationError(f"Indicator camera is not OK: {payload}")
    return payload


def sample_indicator(
    api_base: str,
    *,
    sample_count: int,
    sample_delay_s: float,
    max_attempts: int = 0,
) -> dict[str, Any] | None:
    readings: list[float] = []
    raw_payloads: list[dict[str, Any]] = []
    timestamps: set[str] = set()
    stale_count = 0
    attempts = 0
    effective_max_attempts = (
        int(max_attempts)
        if int(max_attempts) > 0
        else max(sample_count * 5, sample_count + 20)
    )
    while len(readings) < sample_count and attempts < effective_max_attempts:
        attempts += 1
        try:
            payload = read_indicator(api_base)
        except (CalibrationError, TimeoutError, URLError):
            if attempts >= effective_max_attempts:
                raise
            time.sleep(sample_delay_s)
            continue
        value = float(payload["reading_mm"])
        timestamp = str(payload.get("timestamp", ""))
        if timestamp and timestamp in timestamps:
            stale_count += 1
        elif timestamp:
            timestamps.add(timestamp)
        readings.append(value)
        raw_payloads.append(payload)
        time.sleep(sample_delay_s)
    if len(readings) < sample_count:
        return None
    median_mm = statistics.median(readings)
    tracking_ok_count = sum(1 for payload in raw_payloads if payload.get("tracking_ok"))
    return {
        "median_mm": median_mm,
        "sample_count": len(readings),
        "attempt_count": attempts,
        "unique_timestamp_count": len(timestamps),
        "stale_timestamp_count": stale_count,
        "tracking_ok_count": tracking_ok_count,
        "tracking_bad_count": len(raw_payloads) - tracking_ok_count,
        "last_payload": raw_payloads[-1],
    }


def flush_input(serial_connection: serial.Serial, quiet_s: float = 0.2) -> list[str]:
    lines: list[str] = []
    quiet_until = time.monotonic() + quiet_s
    while time.monotonic() < quiet_until:
        raw = serial_connection.readline()
        if not raw:
            continue
        line = raw.decode("ascii", errors="ignore").strip()
        if line:
            lines.append(line)
            quiet_until = time.monotonic() + quiet_s
    return lines


def write_command(serial_connection: serial.Serial, command: str) -> None:
    serial_connection.write((command.strip() + "\n").encode("ascii"))
    serial_connection.flush()


def read_until_ok(
    serial_connection: serial.Serial,
    *,
    timeout_s: float,
    description: str,
) -> list[str]:
    deadline = time.monotonic() + timeout_s
    lines: list[str] = []
    while time.monotonic() < deadline:
        line = serial_connection.readline().decode("ascii", errors="ignore").strip()
        if not line:
            continue
        lower = line.lower()
        if lower == "ok":
            return lines
        if lower.startswith("alarm") or lower.startswith("error") or line.startswith("[MSG:ERR:"):
            raise CalibrationError(f"Controller reported during {description}: {line}")
        lines.append(line)
    raise CalibrationError(f"Timeout waiting for controller response: {description}.")


def command_response(
    serial_connection: serial.Serial,
    command: str,
    *,
    timeout_s: float = 5.0,
) -> list[str]:
    flush_input(serial_connection)
    write_command(serial_connection, command)
    return read_until_ok(serial_connection, timeout_s=timeout_s, description=command)


def query_status(serial_connection: serial.Serial, *, timeout_s: float = 2.0) -> dict[str, Any]:
    write_command(serial_connection, "?")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        line = serial_connection.readline().decode("ascii", errors="ignore").strip()
        if not line:
            continue
        if line.lower().startswith("alarm"):
            raise CalibrationError(f"Controller is in alarm: {line}")
        match = STATUS_PATTERN.search(line)
        if not match:
            continue
        parts = match.group("body").split("|")
        status: dict[str, Any] = {"state": parts[0]}
        for part in parts[1:]:
            if ":" not in part:
                continue
            key, value = part.split(":", 1)
            if key in {"MPos", "WPos", "WCO"}:
                try:
                    status[key] = tuple(float(item) for item in value.split(","))
                except ValueError:
                    pass
        return status
    raise CalibrationError("Timeout waiting for a status frame.")


def wait_idle(serial_connection: serial.Serial, *, timeout_s: float = 20.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = query_status(serial_connection)
        state = str(status.get("state", "")).lower()
        if state == "idle":
            return status
        if state == "alarm":
            raise CalibrationError("Controller entered ALARM state.")
        time.sleep(0.1)
    raise CalibrationError("Controller did not return to IDLE in time.")


def parse_axis_limits(lines: list[str], axis: str) -> tuple[float, float]:
    axis = axis.upper()
    for line in lines:
        match = AXIS_LIMIT_PATTERN.match(line)
        if not match or match.group("axis").upper() != axis:
            continue
        return (float(match.group("min")), float(match.group("max")))
    raise CalibrationError(f"Could not find {axis}-axis limits in startup output.")


def parse_axis_steps_per_mm(lines: list[str], axis: str) -> float:
    axis = axis.upper()
    in_axis = False
    axis_headers = {"X:", "Y:", "Z:", "A:", "B:", "C:"}
    for line in lines:
        stripped = line.strip()
        upper = stripped.upper()
        if upper in axis_headers:
            in_axis = upper == f"{axis}:"
            continue
        if in_axis and stripped.lower().startswith("steps_per_mm:"):
            raw = stripped.split(":", 1)[1].strip()
            return float(raw)
    raise CalibrationError(f"Could not find {axis}.steps_per_mm in config dump.")


def parse_axis_limits_from_config(lines: list[str], axis: str) -> tuple[float, float]:
    axis = axis.upper()
    in_axis = False
    max_travel_mm: float | None = None
    positive_direction = False
    axis_headers = {"X:", "Y:", "Z:", "A:", "B:", "C:"}
    for line in lines:
        stripped = line.strip()
        upper = stripped.upper()
        if upper in axis_headers:
            if in_axis:
                break
            in_axis = upper == f"{axis}:"
            continue
        if not in_axis:
            continue
        lower = stripped.lower()
        if lower.startswith("max_travel_mm:"):
            max_travel_mm = float(stripped.split(":", 1)[1].strip())
        elif lower.startswith("positive_direction:"):
            raw = stripped.split(":", 1)[1].strip().lower()
            positive_direction = raw in {"true", "yes", "1", "on"}
    if max_travel_mm is None or max_travel_mm <= 0:
        raise CalibrationError(f"Could not find {axis}.max_travel_mm in config dump.")
    if positive_direction:
        return (-float(max_travel_mm), 0.0)
    return (0.0, float(max_travel_mm))


def current_a_from_status(status: dict[str, Any]) -> float:
    position = status.get("WPos") or status.get("MPos")
    if not isinstance(position, tuple) or len(position) < 4:
        raise CalibrationError(f"Status frame does not contain A position: {status}")
    return float(position[3])


def home_a(serial_connection: serial.Serial) -> dict[str, Any]:
    flush_input(serial_connection)
    write_command(serial_connection, "$HA")
    read_until_ok(serial_connection, timeout_s=30.0, description="$HA")
    return wait_idle(serial_connection, timeout_s=30.0)


def move_a_relative(
    serial_connection: serial.Serial,
    delta_mm: float,
    *,
    feedrate: float,
) -> dict[str, Any]:
    if abs(delta_mm) < 1e-9:
        return wait_idle(serial_connection)
    for command in ("G21", "G91"):
        command_response(serial_connection, command)
    command_response(serial_connection, f"G1 A{delta_mm:.4f} F{feedrate:.0f}")
    command_response(serial_connection, "G90")
    return wait_idle(serial_connection)


def main() -> int:
    args = parse_args()
    if args.step_mm <= 0:
        raise CalibrationError("--step-mm must be positive.")
    if args.samples <= 0:
        raise CalibrationError("--samples must be positive.")

    health_payload = None
    with urlopen(f"{args.api.rstrip('/')}/health", timeout=2.0) as response:
        health_payload = json.loads(response.read().decode("utf-8"))
    print(f"Indicator API health: {health_payload}")

    points: list[dict[str, Any]] = []
    skipped_points: list[dict[str, Any]] = []
    zero_indicator: float | None = None
    partial_output = args.output.with_suffix(args.output.suffix + ".partial")
    with serial.Serial(
        args.port,
        args.baud,
        timeout=0.2,
        write_timeout=2.0,
    ) as serial_connection:
        print(f"Opened {args.port} at {args.baud} baud.")
        time.sleep(1.0)
        startup_lines = command_response(serial_connection, "$Startup/Show", timeout_s=5.0)
        config_lines = command_response(serial_connection, "$Config/Dump", timeout_s=15.0)
        steps_per_mm = parse_axis_steps_per_mm(config_lines, "A")
        try:
            a_min, a_max = parse_axis_limits(startup_lines, "A")
        except CalibrationError:
            a_min, a_max = parse_axis_limits_from_config(config_lines, "A")
        print(f"A limits from controller: {a_min:.4f} to {a_max:.4f} mm.")
        print(f"A steps_per_mm from live config: {steps_per_mm:.6f}.")

        print("Homing A before capture.")
        status = home_a(serial_connection)
        current_a = current_a_from_status(status)
        print(f"A after homing: {current_a:.6f} mm.")

        calibration = {
            "configured": False,
            "version": 1,
            "axis": "A",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "controller": {
                "port": args.port,
                "baud": args.baud,
                "axis_limits_mm": {"min": a_min, "max": a_max},
                "steps_per_mm": steps_per_mm,
            },
            "steps_per_mm": steps_per_mm,
            "commanded_step_mm": args.step_mm,
            "feedrate_mm_min": args.feedrate,
            "samples_per_point": args.samples,
            "sample_delay_s": args.sample_delay_s,
            "statistic": "median",
            "settle_s": args.settle_s,
            "indicator_api": args.api.rstrip("/"),
            "zero_indicator_mm": None,
            "complete": False,
            "points": points,
            "skipped_points": skipped_points,
        }

        time.sleep(args.settle_s)
        zero_sample = sample_indicator(
            args.api,
            sample_count=args.samples,
            sample_delay_s=args.sample_delay_s,
            max_attempts=args.max_sample_attempts,
        )
        if zero_sample is None:
            skipped_points.append(
                {
                    "commanded_a_mm": current_a,
                    "reason": "indicator samples did not update enough",
                }
            )
            print(f"Skip  0000: A={current_a:.4f} indicator did not update enough")
        else:
            zero_indicator = float(zero_sample["median_mm"])
            calibration["zero_indicator_mm"] = zero_indicator
            points.append(
                {
                    "commanded_a_mm": current_a,
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
                "Point 0000: "
                f"A={current_a:.4f} indicator={zero_indicator:.6f} "
                f"tracking_bad={zero_sample['tracking_bad_count']}"
            )
        save_calibration(partial_output, calibration)

        point_index = 1
        try:
            while current_a - args.step_mm > a_min - 1e-9:
                target = max(a_min, current_a - args.step_mm)
                delta = target - current_a
                status = move_a_relative(
                    serial_connection,
                    delta,
                    feedrate=args.feedrate,
                )
                current_a = current_a_from_status(status)
                time.sleep(args.settle_s)
                sample = sample_indicator(
                    args.api,
                    sample_count=args.samples,
                    sample_delay_s=args.sample_delay_s,
                    max_attempts=args.max_sample_attempts,
                )
                if sample is None:
                    skipped_points.append(
                        {
                            "commanded_a_mm": current_a,
                            "reason": "indicator samples did not update enough",
                        }
                    )
                    calibration["skipped_points"] = skipped_points
                    calibration["updated_at"] = datetime.now(timezone.utc).isoformat()
                    save_calibration(partial_output, calibration)
                    if point_index % 10 == 0 or abs(current_a - a_min) <= 1e-6:
                        print(
                            f"Skip  {point_index:04d}: "
                            f"A={current_a:.4f} indicator did not update enough"
                        )
                    point_index += 1
                    if abs(current_a - a_min) <= 1e-6:
                        break
                    continue
                median = float(sample["median_mm"])
                indicator_delta = (
                    None if zero_indicator is None else median - zero_indicator
                )
                points.append(
                    {
                        "commanded_a_mm": current_a,
                        "indicator_mm": median,
                        "indicator_delta_mm": indicator_delta,
                        "sample_count": sample["sample_count"],
                        "attempt_count": sample["attempt_count"],
                        "unique_timestamp_count": sample["unique_timestamp_count"],
                        "stale_timestamp_count": sample["stale_timestamp_count"],
                        "tracking_ok_count": sample["tracking_ok_count"],
                        "tracking_bad_count": sample["tracking_bad_count"],
                    }
                )
                calibration["points"] = points
                calibration["updated_at"] = datetime.now(timezone.utc).isoformat()
                save_calibration(partial_output, calibration)
                if point_index % 10 == 0 or abs(current_a - a_min) <= 1e-6:
                    print(
                        f"Point {point_index:04d}: "
                        f"A={current_a:.4f} indicator={median:.6f} "
                        f"delta={indicator_delta if indicator_delta is not None else float('nan'):.6f} "
                        f"tracking_bad={sample['tracking_bad_count']}"
                    )
                point_index += 1
                if abs(current_a - a_min) <= 1e-6:
                    break
        finally:
            if not args.leave_down:
                print("Raising A after capture.")
                try:
                    home_a(serial_connection)
                except Exception as exc:
                    print(f"WARNING: failed to raise/home A after capture: {exc}")

    calibration["configured"] = len(points) >= 2
    calibration["complete"] = True
    calibration["updated_at"] = datetime.now(timezone.utc).isoformat()
    save_calibration(args.output, calibration)
    try:
        partial_output.unlink()
    except FileNotFoundError:
        pass
    print(f"Saved {len(points)} calibration points to {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
