"""Capture Z-axis section 3 up/down without changing coordinate offsets."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import serial

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.capture_axis_a_calibration import flush_input
from scripts.capture_axis_z_section3_and_random_walk import (
    capture_z_curve,
    command_response_resilient,
    current_a,
    current_z,
    move_axis_absolute,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture Z section 3 up/down without zeroing API or touching WCO."
    )
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--output-dir", type=Path, default=Path("calibrations"))
    parser.add_argument("--start-z", type=float, default=19.214)
    parser.add_argument("--top-z", type=float, default=23.435)
    parser.add_argument("--step-mm", type=float, default=0.0025)
    parser.add_argument("--settle-s", type=float, default=2.0)
    parser.add_argument("--feedrate", type=float, default=50.0)
    return parser.parse_args()


def atomic_save_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def main() -> int:
    args = parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = (
        f"axis_z_spm6335_section3_up_down_start{args.start_z:.3f}"
        f"_top{args.top_z:.3f}_step{args.step_mm:.4f}"
        f"_settle{args.settle_s:.1f}_feed{args.feedrate:.0f}_{timestamp}"
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
        "valid_for_calibration": True,
        "start_z_mm": args.start_z,
        "top_z_mm": args.top_z,
        "observed_upper_limit_z_mm": 23.455,
        "top_clearance_from_observed_limit_mm": 23.455 - args.top_z,
        "step_mm": args.step_mm,
        "settle_s": args.settle_s,
        "feedrate_mm_min": args.feedrate,
        "complete": False,
    }
    atomic_save_json(summary_path, summary)

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
            args.start_z,
            feedrate=args.feedrate,
            timeout_margin_s=60.0,
        )
        print(f"Z at section3 start: {status.get('WPos', status.get('MPos'))}", flush=True)

        print("section3 up begin", flush=True)
        summary["up"] = capture_z_curve(
            serial_connection,
            api_base=args.api,
            start_z=args.start_z,
            end_z=args.top_z,
            step_mm=args.step_mm,
            settle_s=args.settle_s,
            feedrate=args.feedrate,
            output_path=up_path,
            label="section3_up",
        )
        atomic_save_json(summary_path, summary)

        print("section3 down begin", flush=True)
        summary["down"] = capture_z_curve(
            serial_connection,
            api_base=args.api,
            start_z=args.top_z,
            end_z=args.start_z,
            step_mm=args.step_mm,
            settle_s=args.settle_s,
            feedrate=args.feedrate,
            output_path=down_path,
            label="section3_down",
        )

    summary["complete"] = True
    summary["updated_at"] = datetime.now(timezone.utc).isoformat()
    atomic_save_json(summary_path, summary)
    print(f"saved summary {summary_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
