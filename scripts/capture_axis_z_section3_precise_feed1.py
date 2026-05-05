"""Capture Z section 3 up/down precisely into one NPZ."""

from __future__ import annotations

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

from scripts.capture_axis_a_calibration import CalibrationError, flush_input
from scripts.capture_axis_z_current_point_up_down import atomic_save_json, atomic_save_npz
from scripts.capture_axis_z_section3_and_random_walk import (
    command_response_resilient,
    move_axis_absolute,
    query_status_resilient,
    read_indicator_once,
    zero_indicator,
)

PORT = "COM3"
BAUD = 115200
API = "http://127.0.0.1:8000"
OUTPUT_DIR = Path("calibrations")

CAL_ORIGIN_Z = 16.4
SECTION3_START_CAL_Z = 16.5
SECTION3_TOP_CAL_Z = 23.4
STEP_MM = 0.0025
SETTLE_S = 2.0
MEASUREMENT_FEEDRATE = 1.0
TRANSITION_FEEDRATE = 10.0
A_UP = 0.0


def cal_to_controller_z(cal_z: float) -> float:
    return cal_z - CAL_ORIGIN_Z


def controller_to_cal_z(controller_z: float) -> float:
    return controller_z + CAL_ORIGIN_Z


def axis(status: dict[str, Any], index: int) -> float:
    pos = status.get("WPos") or status.get("MPos")
    if not isinstance(pos, tuple) or len(pos) <= index:
        raise CalibrationError(f"Bad status frame: {status}")
    return float(pos[index])


def make_targets(start: float, end: float) -> list[float]:
    direction = 1.0 if end >= start else -1.0
    step = STEP_MM * direction
    out: list[float] = []
    value = start
    while (direction > 0 and value <= end + 1e-12) or (
        direction < 0 and value >= end - 1e-12
    ):
        out.append(round(value, 6))
        value += step
    if not out or abs(out[-1] - end) > 1e-9:
        out.append(round(end, 6))
    return out


def save_npz(
    path: Path,
    *,
    gcode: list[float],
    controller_gcode: list[float],
    indicator: list[float],
    direction: list[int],
) -> None:
    atomic_save_npz(
        path,
        gcode=np.asarray(gcode, dtype=float),
        controller_gcode=np.asarray(controller_gcode, dtype=float),
        indicator=np.asarray(indicator, dtype=float),
        direction=np.asarray(direction, dtype=np.int16),
    )


def capture_targets(
    serial_connection: serial.Serial,
    *,
    live_path: Path,
    cal_targets: list[float],
    direction_id: int,
    gcode: list[float],
    controller_gcode: list[float],
    indicator: list[float],
    direction: list[int],
) -> None:
    for index, cal_target in enumerate(cal_targets, start=1):
        target_controller_z = cal_to_controller_z(cal_target)
        status = move_axis_absolute(
            serial_connection,
            "Z",
            target_controller_z,
            feedrate=MEASUREMENT_FEEDRATE,
            timeout_margin_s=90.0,
        )
        actual_controller_z = axis(status, 2)
        time.sleep(SETTLE_S)
        payload = read_indicator_once(API)
        value = math.nan if payload is None else float(payload["reading_mm"])
        controller_gcode.append(actual_controller_z)
        gcode.append(controller_to_cal_z(actual_controller_z))
        indicator.append(value)
        direction.append(direction_id)
        save_npz(
            live_path,
            gcode=gcode,
            controller_gcode=controller_gcode,
            indicator=indicator,
            direction=direction,
        )
        if index % 100 == 0:
            print(
                f"direction={direction_id} points_total={len(gcode)} "
                f"cal_z={gcode[-1]:.4f} indicator={value:.6f}",
                flush=True,
            )


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = (
        "axis_z_spm6335_section3_precise"
        f"_start{SECTION3_START_CAL_Z:.1f}_top{SECTION3_TOP_CAL_Z:.1f}"
        f"_step{STEP_MM:.4f}_settle{SETTLE_S:.1f}"
        f"_feed{MEASUREMENT_FEEDRATE:.0f}_transition{TRANSITION_FEEDRATE:.0f}_{timestamp}"
    ).replace(".", "p")
    final_path = OUTPUT_DIR / f"{prefix}.npz"
    live_path = OUTPUT_DIR / f"{prefix}_live.npz"
    summary_path = OUTPUT_DIR / f"{prefix}_summary.json"

    summary: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "axis": "Z",
        "section": 3,
        "steps_per_mm": 6335,
        "cal_origin_z_mm": CAL_ORIGIN_Z,
        "section3_start_cal_z_mm": SECTION3_START_CAL_Z,
        "section3_top_cal_z_mm": SECTION3_TOP_CAL_Z,
        "step_mm": STEP_MM,
        "settle_s": SETTLE_S,
        "measurement_feedrate_mm_min": MEASUREMENT_FEEDRATE,
        "transition_feedrate_mm_min": TRANSITION_FEEDRATE,
        "api_zero_at_start": True,
        "final_npz": str(final_path),
        "live_npz": str(live_path),
        "complete": False,
    }
    atomic_save_json(summary_path, summary)

    gcode: list[float] = []
    controller_gcode: list[float] = []
    indicator: list[float] = []
    direction: list[int] = []

    with serial.Serial(PORT, BAUD, timeout=0.2, write_timeout=2.0) as serial_connection:
        time.sleep(0.8)
        flush_input(serial_connection, quiet_s=0.3)
        command_response_resilient(serial_connection, "G21", timeout_s=5.0)
        command_response_resilient(serial_connection, "G90", timeout_s=5.0)
        summary["start_status"] = query_status_resilient(serial_connection, timeout_s=2.0)
        atomic_save_json(summary_path, summary)

        move_axis_absolute(
            serial_connection,
            "Z",
            cal_to_controller_z(SECTION3_START_CAL_Z),
            feedrate=TRANSITION_FEEDRATE,
            timeout_margin_s=90.0,
        )
        move_axis_absolute(
            serial_connection,
            "A",
            A_UP,
            feedrate=TRANSITION_FEEDRATE,
            timeout_margin_s=90.0,
        )
        summary["section3_start_status"] = query_status_resilient(
            serial_connection, timeout_s=2.0
        )
        summary["zero_payload"] = zero_indicator(API)
        summary["zeroed_at"] = datetime.now(timezone.utc).isoformat()
        atomic_save_json(summary_path, summary)

        capture_targets(
            serial_connection,
            live_path=live_path,
            cal_targets=make_targets(SECTION3_START_CAL_Z, SECTION3_TOP_CAL_Z),
            direction_id=1,
            gcode=gcode,
            controller_gcode=controller_gcode,
            indicator=indicator,
            direction=direction,
        )
        capture_targets(
            serial_connection,
            live_path=live_path,
            cal_targets=make_targets(SECTION3_TOP_CAL_Z, SECTION3_START_CAL_Z),
            direction_id=-1,
            gcode=gcode,
            controller_gcode=controller_gcode,
            indicator=indicator,
            direction=direction,
        )
        final_status = query_status_resilient(serial_connection, timeout_s=2.0)

    save_npz(
        final_path,
        gcode=gcode,
        controller_gcode=controller_gcode,
        indicator=indicator,
        direction=direction,
    )
    summary.update(
        {
            "complete": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "points": len(gcode),
            "up_points": int(np.count_nonzero(np.asarray(direction) == 1)),
            "down_points": int(np.count_nonzero(np.asarray(direction) == -1)),
            "final_status": final_status,
        }
    )
    atomic_save_json(summary_path, summary)
    try:
        live_path.unlink()
    except FileNotFoundError:
        pass
    print(f"done points={len(gcode)} file={final_path}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
