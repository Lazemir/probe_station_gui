# Task 41 Report: Microscope Scan Workflow

Date: 2026-06-28
Branch: `codex/refactor-stage-controller`
Base commit: `7d6be8d refactor: split stage move lifecycle`

## Summary

Extracted microscope scan planning, status text, metadata save-plan construction, and manifest payload/write helpers into `probe_station_gui.camera.microscope_scan`.

`Main` still owns the runtime side effects required by the brief:

- thread creation;
- Qt signal emission;
- stage-controller external-task lifecycle and moves;
- camera frame waits;
- dialog/status updates;
- image file-save calls;
- external-task cleanup.

## Behavior Preserved

No intended public API or GUI behavior change.

Start guard order is now directly characterized:

- disconnected stage rejects before reading active scale or camera;
- missing design rejects before reading active scale or camera.

## Metrics

Wily was run from a disposable UTF-8 temp clone/cache with a temporary commit:

- Base: `7d6be8d`
- Temp commit: `2217cdf`
- Temp root: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task41_d8ca281d700342028a307fa940483c98`

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| `main.py` LOC | 8446 | 8371 | yes |
| `main.py` SLOC | 7898 | 7823 | yes |
| `main.py` Wily cyclomatic | 1591 | 1587 | yes |
| `main.py` Maintainability Index | 0 | 0 | no change |
| `Main._start_microscope_scan` CC | 12 | 10 | yes |
| `Main._capture_microscope_scan_tile` CC | 3 | 2 | yes |
| `Main._default_microscope_scan_output_dir` CC | 2 | 1 | yes |
| `Main._save_microscope_scan_mosaic` CC | 2 | 1 | yes |
| `Main._write_microscope_scan_manifest` | present | removed | yes |
| Lizard warning count in touched scope | 2 | 2 | no change |
| New module MI | n/a | 36.93 | yes |
| New module max CC | n/a | 6 | yes |

Additional spot checks:

- `radon raw main.py`: `LOC 8371`, `SLOC 7823`
- `radon raw probe_station_gui/camera/microscope_scan.py`: `LOC 309`, `SLOC 265`
- `radon mi`: `main.py C (0.00)`, `microscope_scan.py A (36.93)`
- `lizard`: remaining warnings are unrelated existing warnings, `_api_raw_voltage_sweep` and `closeEvent`.

## LOC Gate

Task target was `main.py <= 8250 LOC`.

Final result is `8371 LOC`, so this is a documented LOC-gate exception. The earlier attempt to meet the target by moving scan orchestration to a main-window adapter was rejected in review because the brief said `Main` must own thread, Qt, stage, camera, status, dialog, cleanup, and file-save side effects. The accepted implementation keeps that ownership and only extracts planning/payload helpers.

Task 42 must compensate: it starts from `8371 LOC` and still targets `main.py <= 8000 LOC`.

## Verification

- Focused tests: `18 passed`
  - `tests/camera/test_microscope_scan.py`
  - `tests/camera/test_imaging.py`
  - `tests/ui/test_main_window_auxiliary.py`
  - `tests/app/test_main_microscope_scan.py`
- Full tests: `1166 passed, 2 skipped`
- Coverage run: `1166 passed, 2 skipped`
- Coverage report: `71%` total with `--ignore-errors`; initial plain report hit stale source entries for `pyscript` and PySide `shibokensupport`.
- New module coverage: `probe_station_gui/camera/microscope_scan.py` at `79%`
- New tests coverage:
  - `tests/app/test_main_microscope_scan.py` at `100%`
  - `tests/camera/test_microscope_scan.py` at `100%`
- Ruff: `All checks passed!`
- `git diff --check`: clean

## Reviews

Initial spec review rejected an over-extracted adapter version:

- moving scan runtime side effects out of `Main` violated the brief;
- camera-local helpers were performing image saves instead of returning save plans.

Strict re-review:

- Spec: no Important findings.
- Code quality: no Important findings. One minor unused `json` import was removed.

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes;
- tests passed: yes;
- metrics improved: yes;
- maintainability improvement: metadata, manifest, status formatting, and scan-plan construction now have a focused camera-local module and direct tests;
- new risk introduced: low to medium, mainly the documented LOC target miss and another module to keep aligned with `Main`'s scan orchestration.
