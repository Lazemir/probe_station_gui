"""Capture Z up/down from the current controller point with a calibrated offset."""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import serial

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.capture_axis_a_calibration import flush_input
from scripts.capture_axis_z_section3_and_random_walk import (
    command_response_resilient,
    current_z,
    move_axis_absolute,
    read_indicator_once,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture Z up/down from the current point. Does not change WCO."
    )
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--output-dir", type=Path, default=Path("calibrations"))
    parser.add_argument("--current-cal-z", type=float, required=True)
    parser.add_argument("--limit-cal-z", type=float, required=True)
    parser.add_argument("--clearance-mm", type=float, default=0.02)
    parser.add_argument("--step-mm", type=float, default=0.0025)
    parser.add_argument("--settle-s", type=float, default=2.0)
    parser.add_argument("--feedrate", type=float, default=10.0)
    return parser.parse_args()


def atomic_save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def atomic_save_npz(path: Path, **arrays: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("wb") as handle:
        np.savez(handle, **arrays)
    tmp.replace(path)


def capture_direction(
    serial_connection: serial.Serial,
    *,
    api_base: str,
    controller_start_z: float,
    cal_start_z: float,
    controller_end_z: float,
    step_mm: float,
    settle_s: float,
    feedrate: float,
    output_path: Path,
    label: str,
) -> dict[str, Any]:
    direction = 1.0 if controller_end_z >= controller_start_z else -1.0
    step = abs(step_mm) * direction
    controller_gcode: list[float] = []
    gcode: list[float] = []
    indicator: list[float] = []
    live_path = output_path.with_name(output_path.stem + "_live.npz")

    target = controller_start_z
    index = 0
    while (direction > 0 and target <= controller_end_z + 1e-12) or (
        direction < 0 and target >= controller_end_z - 1e-12
    ):
        target = min(target, controller_end_z) if direction > 0 else max(target, controller_end_z)
        status = move_axis_absolute(
            serial_connection,
            "Z",
            target,
            feedrate=feedrate,
            timeout_margin_s=60.0,
        )
        pos = status.get("WPos") or status.get("MPos")
        z_now = float(pos[2])
        cal_z = cal_start_z + (z_now - controller_start_z)
        time.sleep(settle_s)
        payload = read_indicator_once(api_base)
        value = math.nan if payload is None else float(payload["reading_mm"])
        controller_gcode.append(z_now)
        gcode.append(cal_z)
        indicator.append(value)
        index += 1
        if index % 50 == 0 or abs(z_now - controller_end_z) <= 1e-9:
            atomic_save_npz(
                live_path,
                gcode=np.asarray(gcode, dtype=float),
                controller_gcode=np.asarray(controller_gcode, dtype=float),
                indicator=np.asarray(indicator, dtype=float),
            )
            print(
                f"{label} progress points={index} controller_Z={z_now:.4f} "
                f"cal_Z={cal_z:.4f} indicator={value:.6f}",
                flush=True,
            )
        if abs(target - controller_end_z) <= 1e-12:
            break
        target += step

    atomic_save_npz(
        output_path,
        gcode=np.asarray(gcode, dtype=float),
        controller_gcode=np.asarray(controller_gcode, dtype=float),
        indicator=np.asarray(indicator, dtype=float),
    )
    atomic_save_npz(
        live_path,
        gcode=np.asarray(gcode, dtype=float),
        controller_gcode=np.asarray(controller_gcode, dtype=float),
        indicator=np.asarray(indicator, dtype=float),
    )
    return {
        "points": index,
        "path": str(output_path),
        "live_path": str(live_path),
        "start_controller_z": float(controller_gcode[0]) if controller_gcode else None,
        "end_controller_z": float(controller_gcode[-1]) if controller_gcode else None,
        "start_cal_z": float(gcode[0]) if gcode else None,
        "end_cal_z": float(gcode[-1]) if gcode else None,
    }


def main() -> int:
    args = parse_args()
    turn_cal_z = args.limit_cal_z - args.clearance_mm
    travel_mm = turn_cal_z - args.current_cal_z
    if travel_mm <= 0:
        raise RuntimeError("Turn point must be above the current calibrated point.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = (
        f"axis_z_spm6335_section3_current{args.current_cal_z:.3f}"
        f"_limit{args.limit_cal_z:.3f}_turn{turn_cal_z:.3f}"
        f"_step{args.step_mm:.4f}_settle{args.settle_s:.1f}"
        f"_feed{args.feedrate:.0f}_{timestamp}"
    ).replace(".", "p")
    up_path = args.output_dir / f"{prefix}_up.npz"
    down_path = args.output_dir / f"{prefix}_down.npz"
    summary_path = args.output_dir / f"{prefix}_summary.json"

    summary: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "axis": "Z",
        "steps_per_mm": 6335,
        "api_zero_at_start": False,
        "coordinate_offsets_changed": False,
        "current_cal_z_mm": args.current_cal_z,
        "limit_cal_z_mm": args.limit_cal_z,
        "clearance_mm": args.clearance_mm,
        "turn_cal_z_mm": turn_cal_z,
        "travel_mm": travel_mm,
        "step_mm": args.step_mm,
        "settle_s": args.settle_s,
        "feedrate_mm_min": args.feedrate,
        "complete": False,
    }
    atomic_save_json(summary_path, summary)

    with serial.Serial(args.port, args.baud, timeout=0.2, write_timeout=2.0) as serial_connection:
        time.sleep(0.8)
        flush_input(serial_connection, quiet_s=0.3)
        command_response_resilient(serial_connection, "G21", timeout_s=5.0)
        command_response_resilient(serial_connection, "G90", timeout_s=5.0)
        controller_start_z = current_z(serial_connection)
        controller_turn_z = controller_start_z + travel_mm
        summary["controller_start_z_mm"] = controller_start_z
        summary["controller_turn_z_mm"] = controller_turn_z
        atomic_save_json(summary_path, summary)
        print(
            f"start controller_Z={controller_start_z:.6f} cal_Z={args.current_cal_z:.6f} "
            f"turn controller_Z={controller_turn_z:.6f} cal_Z={turn_cal_z:.6f}",
            flush=True,
        )

        print("up begin", flush=True)
        summary["up"] = capture_direction(
            serial_connection,
            api_base=args.api,
            controller_start_z=controller_start_z,
            cal_start_z=args.current_cal_z,
            controller_end_z=controller_turn_z,
            step_mm=args.step_mm,
            settle_s=args.settle_s,
            feedrate=args.feedrate,
            output_path=up_path,
            label="up",
        )
        atomic_save_json(summary_path, summary)

        print("down begin", flush=True)
        summary["down"] = capture_direction(
            serial_connection,
            api_base=args.api,
            controller_start_z=controller_start_z,
            cal_start_z=args.current_cal_z,
            controller_end_z=controller_start_z,
            step_mm=args.step_mm,
            settle_s=args.settle_s,
            feedrate=args.feedrate,
            output_path=down_path,
            label="down",
        )

    summary["complete"] = True
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_save_json(summary_path, summary)
    print(f"saved summary {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
