"""Resume Z section 3 after top-limit recovery.

The upper branch is already captured. This script assumes the current FluidNC
work coordinate has been restored after recovery and captures a safe down branch
from below the observed Z limit, then starts the long random walk.
"""

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
    run_random_walk,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Capture resumed Z section 3 down branch and random walk."
    )
    parser.add_argument("--port", default="COM3")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--output-dir", type=Path, default=Path("calibrations"))
    parser.add_argument("--down-start-z", type=float, default=23.30)
    parser.add_argument("--down-end-z", type=float, default=19.214)
    parser.add_argument("--step-mm", type=float, default=0.0025)
    parser.add_argument("--settle-s", type=float, default=2.0)
    parser.add_argument("--measurement-feed", type=float, default=50.0)
    parser.add_argument("--slow-feed", type=float, default=1.0)
    parser.add_argument("--random-step-mm", type=float, default=0.001)
    parser.add_argument("--random-min-segment-mm", type=float, default=0.1)
    parser.add_argument("--random-max-segment-mm", type=float, default=1.0)
    parser.add_argument("--random-segments", type=int, default=500)
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
        f"axis_z_spm6335_section3_resume_down_start{args.down_start_z:.3f}"
        f"_end{args.down_end_z:.3f}_step0p0025_settle{args.settle_s:.1f}"
        f"_feed{args.measurement_feed:.0f}_{timestamp}"
    ).replace(".", "p")
    down_path = args.output_dir / f"{prefix}.npz"
    walk_prefix = (
        f"axis_z_spm6335_section3_random_walk_after_resume_start{args.down_end_z:.3f}"
        f"_top{args.down_start_z:.3f}_step0p001_settle{args.settle_s:.1f}"
        f"_segments{args.random_segments}_{timestamp}"
    ).replace(".", "p")
    walk_path = args.output_dir / f"{walk_prefix}.npz"
    summary_path = args.output_dir / f"{prefix}_summary.json"
    summary: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "axis": "Z",
        "steps_per_mm": 6335,
        "api_zero_at_start": False,
        "note": "Resumed after top hard-limit recovery. Down branch starts from safe Z below observed limit.",
        "down_start_z_mm": args.down_start_z,
        "down_end_z_mm": args.down_end_z,
        "step_mm": args.step_mm,
        "settle_s": args.settle_s,
        "measurement_feed_mm_min": args.measurement_feed,
        "slow_feed_mm_min": args.slow_feed,
        "random_step_mm": args.random_step_mm,
        "random_segments": args.random_segments,
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
            args.down_start_z,
            feedrate=args.slow_feed,
            timeout_margin_s=120.0,
        )
        print(f"Z at safe down start: {status.get('WPos', status.get('MPos'))}", flush=True)

        summary["down"] = capture_z_curve(
            serial_connection,
            api_base=args.api,
            start_z=args.down_start_z,
            end_z=args.down_end_z,
            step_mm=args.step_mm,
            settle_s=args.settle_s,
            feedrate=args.measurement_feed,
            output_path=down_path,
            label="section3_resume_down",
        )
        atomic_save_json(summary_path, summary)

        summary["random_walk"] = run_random_walk(
            serial_connection,
            api_base=args.api,
            start_z=args.down_end_z,
            top_z=args.down_start_z,
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
