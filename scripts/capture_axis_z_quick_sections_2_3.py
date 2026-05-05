"""Capture a quick combined Z section-2/section-3 pass into one NPZ."""

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

from scripts.capture_axis_a_calibration import CalibrationError, flush_input, query_status
from scripts.capture_axis_z_current_point_up_down import atomic_save_json, atomic_save_npz
from scripts.capture_axis_z_section3_and_random_walk import (
    command_response_resilient,
    move_axis_absolute,
    query_status_resilient,
    read_indicator_once,
    wait_idle_resilient,
    zero_indicator,
)

PORT = "COM3"
BAUD = 115200
API = "http://127.0.0.1:8000"
OUTPUT_DIR = Path("calibrations")

CAL_ORIGIN_Z = 16.4
SECTION2_START_CAL_Z = 11.0
SECTION2_TOP_CAL_Z = 20.0
SECTION3_START_CAL_Z = 16.5
SECTION3_TOP_CAL_Z = 23.4
STEP_MM = 0.1
SETTLE_S = 2.0
MEASUREMENT_FEEDRATE = 100.0
TRANSITION_FEEDRATE = 10.0
A_DOWN = -5.5
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


def targets(start: float, end: float, step: float) -> list[float]:
    direction = 1.0 if end >= start else -1.0
    step *= direction
    out: list[float] = []
    value = start
    while (direction > 0 and value <= end + 1e-9) or (
        direction < 0 and value >= end - 1e-9
    ):
        out.append(round(value, 6))
        value += step
    if abs(out[-1] - end) > 1e-6:
        out.append(round(end, 6))
    return out


def append_point(
    *,
    api: str,
    gcode: list[float],
    controller_gcode: list[float],
    indicator: list[float],
    section: list[int],
    direction: list[int],
    phase: list[int],
    cal_z: float,
    controller_z: float,
    section_id: int,
    direction_id: int,
    phase_id: int,
) -> None:
    time.sleep(SETTLE_S)
    payload = read_indicator_once(api)
    value = math.nan if payload is None else float(payload["reading_mm"])
    gcode.append(cal_z)
    controller_gcode.append(controller_z)
    indicator.append(value)
    section.append(section_id)
    direction.append(direction_id)
    phase.append(phase_id)


def save_live(
    path: Path,
    *,
    gcode: list[float],
    controller_gcode: list[float],
    indicator: list[float],
    section: list[int],
    direction: list[int],
    phase: list[int],
) -> None:
    atomic_save_npz(
        path,
        gcode=np.asarray(gcode, dtype=float),
        controller_gcode=np.asarray(controller_gcode, dtype=float),
        indicator=np.asarray(indicator, dtype=float),
        section=np.asarray(section, dtype=np.int16),
        direction=np.asarray(direction, dtype=np.int16),
        phase=np.asarray(phase, dtype=np.int16),
    )


def capture_targets(
    serial_connection: serial.Serial,
    *,
    live_path: Path,
    api: str,
    cal_targets: list[float],
    section_id: int,
    direction_id: int,
    phase_id: int,
    gcode: list[float],
    controller_gcode: list[float],
    indicator: list[float],
    section: list[int],
    direction: list[int],
    phase: list[int],
) -> None:
    for cal_z in cal_targets:
        controller_z = cal_to_controller_z(cal_z)
        status = move_axis_absolute(
            serial_connection,
            "Z",
            controller_z,
            feedrate=MEASUREMENT_FEEDRATE,
            timeout_margin_s=90.0,
        )
        actual_controller_z = axis(status, 2)
        append_point(
            api=api,
            gcode=gcode,
            controller_gcode=controller_gcode,
            indicator=indicator,
            section=section,
            direction=direction,
            phase=phase,
            cal_z=controller_to_cal_z(actual_controller_z),
            controller_z=actual_controller_z,
            section_id=section_id,
            direction_id=direction_id,
            phase_id=phase_id,
        )
        save_live(
            live_path,
            gcode=gcode,
            controller_gcode=controller_gcode,
            indicator=indicator,
            section=section,
            direction=direction,
            phase=phase,
        )


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = (
        "axis_z_spm6335_quick_sections2_3"
        f"_sec2top{SECTION2_TOP_CAL_Z:.1f}"
        f"_sec3start{SECTION3_START_CAL_Z:.1f}"
        f"_sec3top{SECTION3_TOP_CAL_Z:.1f}"
        f"_step{STEP_MM:.1f}_settle{SETTLE_S:.1f}"
        f"_feed{MEASUREMENT_FEEDRATE:.0f}_transition{TRANSITION_FEEDRATE:.0f}_{timestamp}"
    ).replace(".", "p")
    final_path = OUTPUT_DIR / f"{prefix}.npz"
    live_path = OUTPUT_DIR / f"{prefix}_live.npz"
    summary_path = OUTPUT_DIR / f"{prefix}_summary.json"

    gcode: list[float] = []
    controller_gcode: list[float] = []
    indicator: list[float] = []
    section: list[int] = []
    direction: list[int] = []
    phase: list[int] = []

    summary: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "axis": "Z",
        "steps_per_mm": 6335,
        "cal_origin_z_mm": CAL_ORIGIN_Z,
        "section2_top_cal_z_mm": SECTION2_TOP_CAL_Z,
        "section3_start_cal_z_mm": SECTION3_START_CAL_Z,
        "section3_top_cal_z_mm": SECTION3_TOP_CAL_Z,
        "step_mm": STEP_MM,
        "settle_s": SETTLE_S,
        "measurement_feedrate_mm_min": MEASUREMENT_FEEDRATE,
        "transition_feedrate_mm_min": TRANSITION_FEEDRATE,
        "final_npz": str(final_path),
        "complete": False,
    }
    atomic_save_json(summary_path, summary)

    with serial.Serial(PORT, BAUD, timeout=0.2, write_timeout=2.0) as serial_connection:
        time.sleep(0.8)
        flush_input(serial_connection, quiet_s=0.3)
        command_response_resilient(serial_connection, "G21", timeout_s=5.0)
        command_response_resilient(serial_connection, "G90", timeout_s=5.0)
        start_status = query_status_resilient(serial_connection, timeout_s=2.0)
        summary["start_status"] = start_status
        move_axis_absolute(
            serial_connection,
            "Z",
            cal_to_controller_z(SECTION2_START_CAL_Z),
            feedrate=TRANSITION_FEEDRATE,
            timeout_margin_s=90.0,
        )
        move_axis_absolute(
            serial_connection,
            "A",
            A_DOWN,
            feedrate=TRANSITION_FEEDRATE,
            timeout_margin_s=90.0,
        )
        section2_start_status = query_status_resilient(serial_connection, timeout_s=2.0)
        start_controller_z = axis(section2_start_status, 2)
        start_cal_z = controller_to_cal_z(start_controller_z)
        summary["section2_start_status"] = section2_start_status
        summary["section2_start_cal_z_mm"] = start_cal_z
        atomic_save_json(summary_path, summary)

        capture_targets(
            serial_connection,
            live_path=live_path,
            api=API,
            cal_targets=targets(start_cal_z, SECTION2_TOP_CAL_Z, STEP_MM),
            section_id=2,
            direction_id=1,
            phase_id=1,
            gcode=gcode,
            controller_gcode=controller_gcode,
            indicator=indicator,
            section=section,
            direction=direction,
            phase=phase,
        )
        capture_targets(
            serial_connection,
            live_path=live_path,
            api=API,
            cal_targets=targets(SECTION2_TOP_CAL_Z, start_cal_z, STEP_MM),
            section_id=2,
            direction_id=-1,
            phase_id=2,
            gcode=gcode,
            controller_gcode=controller_gcode,
            indicator=indicator,
            section=section,
            direction=direction,
            phase=phase,
        )

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
        zero_payload = zero_indicator(API)
        summary["zero_payload"] = zero_payload
        summary["zeroed_at"] = datetime.now(timezone.utc).isoformat()
        atomic_save_json(summary_path, summary)

        capture_targets(
            serial_connection,
            live_path=live_path,
            api=API,
            cal_targets=targets(SECTION3_START_CAL_Z, SECTION3_TOP_CAL_Z, STEP_MM),
            section_id=3,
            direction_id=1,
            phase_id=3,
            gcode=gcode,
            controller_gcode=controller_gcode,
            indicator=indicator,
            section=section,
            direction=direction,
            phase=phase,
        )
        capture_targets(
            serial_connection,
            live_path=live_path,
            api=API,
            cal_targets=targets(SECTION3_TOP_CAL_Z, SECTION3_START_CAL_Z, STEP_MM),
            section_id=3,
            direction_id=-1,
            phase_id=4,
            gcode=gcode,
            controller_gcode=controller_gcode,
            indicator=indicator,
            section=section,
            direction=direction,
            phase=phase,
        )
        final_status = query_status_resilient(serial_connection, timeout_s=2.0)

    save_live(
        final_path,
        gcode=gcode,
        controller_gcode=controller_gcode,
        indicator=indicator,
        section=section,
        direction=direction,
        phase=phase,
    )
    summary.update(
        {
            "complete": True,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "points": len(gcode),
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
