# Remove Microscope Scan Approach Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the duplicate microscope-scan tile approach and rely exclusively on the stage precision-approach planner.

**Architecture:** The scan runner sends each tile center once to `StageController.run_external_move_to_xy`. The GUI no longer creates a scan-specific approach value, and both API layers reject obsolete approach fields before starting work.

**Tech Stack:** Python 3.11, PySide6, FastAPI, pytest

## Global Constraints

- Do not alter the shared stage precision-approach implementation.
- Do not silently accept obsolete approach API fields.
- Preserve overlap, settling, exposure, flat-field, and scan artifact behavior.

---

### Task 1: Remove The Duplicate Tile Approach

**Files:**
- Modify: `probe_station_gui/dialogs/microscope_scan_dialog.py`
- Modify: `probe_station_gui/api/server.py`
- Modify: `main.py`
- Test: `tests/ui/test_microscope_scan_dialog.py`
- Test: `tests/api/test_server.py`
- Test: `tests/app/test_main_microscope_scan.py`

**Interfaces:**
- Consumes: `StageController.run_external_move_to_xy(x_mm: float, y_mm: float)`
- Produces: `_move_to_microscope_scan_tile(tile)` with one coordinate move

- [ ] **Step 1: Write failing GUI, API, and runner tests**

```python
assert not hasattr(dialog, "_approach_spin")
assert not hasattr(dialog.current_configuration(), "tile_approach_mm")
assert client.post("/api/v1/camera/area-scan", json={"approach_mm": 0.01}).status_code == 400
assert client.post("/api/v1/camera/area-scan", json={"tile_approach_mm": 0.01}).status_code == 400
Main._move_to_microscope_scan_tile(window, tile)
assert moves == [(1.25, -2.5)]
```

- [ ] **Step 2: Run the focused tests and verify failure**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_microscope_scan_dialog.py tests/api/test_server.py tests/app/test_main_microscope_scan.py -q`

Expected: failures show the existing approach widget, accepted API fields, and required `tile_approach_mm` argument.

- [ ] **Step 3: Remove the GUI and runner behavior**

```python
@dataclass(frozen=True)
class MicroscopeScanConfiguration:
    output_dir: str
    overlap_fraction: float
    settle_s: float

def _move_to_microscope_scan_tile(self, tile: MicroscopeScanTile) -> None:
    self.stage_controller.run_external_move_to_xy(
        float(tile.stage_xy[0]),
        float(tile.stage_xy[1]),
    )
```

Delete the approach spin box, constants, parser, configuration field, and scan-loop argument.

- [ ] **Step 4: Reject obsolete API fields at both boundaries**

```python
obsolete = sorted({"tile_approach_mm", "approach_mm"}.intersection(payload))
if obsolete:
    return {
        "accepted": False,
        "status_code": 400,
        "message": f"{', '.join(obsolete)} is no longer supported for area scans.",
    }
```

Mirror the validation in the FastAPI route with `HTTPException(status_code=400, ...)` before command dispatch.

- [ ] **Step 5: Run focused and full verification**

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_microscope_scan_dialog.py tests/api/test_server.py tests/app/test_main_microscope_scan.py tests/stage/test_precision_motion.py -q`

Run: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q`

Expected: all tests pass and `rg "tile_approach_mm|approach_mm" main.py probe_station_gui tests` finds only rejection tests/messages.

- [ ] **Step 6: Commit**

```bash
git add main.py probe_station_gui/api/server.py probe_station_gui/dialogs/microscope_scan_dialog.py tests/api/test_server.py tests/app/test_main_microscope_scan.py tests/ui/test_microscope_scan_dialog.py
git commit -m "Remove duplicate microscope scan approach"
```
