# Task 41 Brief: Microscope Scan Workflow

Date: 2026-06-28
Branch: `codex/refactor-stage-controller`
Base commit: `7d6be8d refactor: split stage move lifecycle`

## Current Behavior

`Main` owns the complete microscope design-scan workflow:

- default scan output path from the loaded design;
- scan start guards: existing scan, serial connection, design loaded, design
  registration, active objective scale, available camera frame;
- stage-bounds projection and scan-plan construction;
- scan thread creation and dialog running/status state;
- scan run loop: output directory creation, external stage task lifecycle,
  needle raise, per-tile XY moves, settle stop polling, camera capture, mosaic
  stitch, mosaic save, manifest write, finish signal;
- tile/mosaic metadata construction and manifest JSON payload construction;
- scan-finished dialog/status cleanup.

Behavior must remain stable. In particular, `Main` must continue to own thread
creation, Qt signal emission, stage-controller side effects, camera frame waits,
status messages, dialog updates, and external-task cleanup.

## Structural Improvement

Extract scan planning and payload construction into a camera-local module:

- `probe_station_gui.camera.microscope_scan` builds start plans, default output
  paths, tile metadata, mosaic metadata, scan filenames, manifest payloads, and
  manifest writes.
- `Main` becomes the adapter that gathers live GUI/hardware state, calls the
  module, and applies Qt/stage/camera/file-save side effects.

This should improve locality: future scan metadata/manifest changes should not
require reading the central window adapter, while scan runtime side effects stay
where the hardware/Qt ownership already lives.

## Validation Check

Focused checks:

- new/updated `tests/camera/test_microscope_scan.py`
- `tests/camera/test_imaging.py`
- `tests/ui/test_main_window_auxiliary.py`
- focused app tests if existing fixtures cover scan start/finish

Full checks:

- `.\.venv\Scripts\python.exe -m pytest tests`
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
- `git diff --check`

Metrics checks:

- `.\.venv\Scripts\python.exe -m radon raw main.py`
- `.\.venv\Scripts\python.exe -m radon cc main.py -s`
- `.\.venv\Scripts\python.exe -m lizard main.py`
- Wily diff from a disposable UTF-8 temp clone/cache, comparing Task 40 commit
  to the Task 41 result.

## Baseline Before Edit

- `main.py` LOC: `8446`
- `main.py` SLOC: `7898`
- Wily baseline commit for this task: `7d6be8d`
- `Main._start_microscope_scan`: `66 NLOC / CCN 12`, radon `C (12)`
- `Main._run_microscope_scan`: `77 NLOC / CCN 7`, radon `B (7)`
- `Main._capture_microscope_scan_tile`: `53 NLOC / CCN 3`
- `Main._save_microscope_scan_mosaic`: `36 NLOC / CCN 2`
- `Main._write_microscope_scan_manifest`: `38 NLOC / CCN 2`
- Task target: `main.py <= 8250 LOC`

## Acceptance

- Behavior preserved: focused scan/camera tests and full suite pass.
- Public API changed: no.
- `main.py <= 8250 LOC` or any miss is explained with hotspot metric
  compensation.
- `Main` scan metadata/manifest helpers are removed or become thin adapters.
- New module tests directly characterize scan metadata/manifest payload behavior.
