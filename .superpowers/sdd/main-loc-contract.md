# main.py LOC Reduction Contract

Baseline date: 2026-06-28
Baseline commit: `19ccb0b refactor: extract API command dispatch`
Baseline `main.py` metric: `radon raw main.py` reports `LOC 8948`, `SLOC 8400`.

This is the working contract for the remaining architecture refactoring. The goal is to reduce `main.py` size with behavior-preserving refactor passes, not to game total LOC. A pass is valid only when tests pass and at least one maintainability metric improves.

## Rules

- Measure `main.py` with `.\.venv\Scripts\python.exe -m radon raw main.py`.
- Use `LOC`, not physical line count, for this contract.
- Run Wily from a disposable UTF-8 temp clone/cache for before/after metrics.
- After every completed task, record:
  - `main.py` LOC before/after;
  - Wily `main.py` cyclomatic before/after;
  - touched hotspot NLOC/CCN before/after;
  - tests and ruff result;
  - whether the LOC target was met.
- If a task misses its `main.py` LOC target by more than `50` LOC, the next task must either compensate or this contract must be amended with a concrete reason and a new target table.
- Do not count a stage-only or pure-module task as progress toward `main.py` reduction unless `main.py` LOC actually drops.
- Do not make `main.py` harder to understand just to reduce LOC. The target is invalid if complexity or behavior safety gets worse without an explicit compensating reason.

## Target Table

| Task | Scope | Current behavior | Structural improvement | Required `main.py` LOC after task |
| --- | --- | --- | --- | --- |
| 38 | `probe_station_gui.stage.api_moves` | API coordinate move planning is a pure stage helper with one warning-level function. | Split pure validation/planning helpers; no GUI ownership move. | `<= 8948` |
| 39 | Stage position update adapter | `Main._on_stage_position_changed` owns stage status, prediction reconciliation, design position updates, B-axis invalidation, contact calibration update, and idle cleanup. | Extract a focused stage-position update adapter/presenter so `Main` applies a plan and keeps Qt side effects. | `<= 8750` |
| 40 | Coordinate move finish/cancel flow | `Main.on_move_finished`, `_has_cancelable_operation`, and `_cancel_stage_coordinate_action` mix task lifecycle, UI state, and coordinate target cleanup. | Extract coordinate move lifecycle/cancel decisions into a stage UI adapter module; keep controller calls in `Main`. | `<= 8550` |
| 41 | Microscope scan workflow | `Main` owns scan start, run loop, settle, tile capture, mosaic save, manifest write, and completion UI. | Move scan planning/filesystem payload helpers into a microscope scan module; `Main` keeps thread and Qt updates. | `<= 8250` |
| 42 | Contact seek and saved-position workflows | `Main` owns contact seek request/run/finish plus saved needle/surface/sample position helpers. | Extract contact seek presentation/result planning and saved-position payload helpers. | `<= 8000` |
| 43 | Serial/controller connection UI flow | `Main` owns serial connected/disconnected, auto-connect, startup sync, reboot recovery, feedrate preferences, and controller-state persistence glue. | Move connection-state UI orchestration into a main-window adapter module; keep actual serial/stage controller behavior unchanged. | `<= 7750` |
| 44 | Route/UI residue cleanup | Route, design, API, Telegram, and objective adapter remnants remain as many thin `Main` methods after prior extractions. | Delete or consolidate wrappers that no longer provide locality, and move cohesive remaining adapter groups to existing modules. | `<= 7550` |

## Completion Definition

The `main.py` reduction track is complete when all are true:

- `main.py` is `<= 7550 LOC` by `radon raw`.
- Full `pytest tests` passes.
- Configured `ruff check --ignore E402,F401 .` passes.
- Wily shows `main.py` cyclomatic lower than the baseline at `19ccb0b`.
- No public API change is introduced unless explicitly approved.
- Whole-branch review has no unresolved blocking or important findings.

## Non-Goals

- Do not move hardware I/O into new modules just to reduce `main.py`.
- Do not rename external API routes, route-control terms, UI labels, settings keys, or public client methods.
- Do not treat total repository LOC increase as failure if complexity and locality improve.

## Contract Exceptions

- Task 41 finished at `main.py LOC 8371`, missing the `<= 8250` target by `121` LOC. This is accepted as a documented exception because the implementation that reached the target moved thread/stage/camera/Qt/file-save side effects out of `Main`, contradicting the task brief. The accepted version keeps those side effects in `Main` and still improves Wily `main.py` cyclomatic `1591 -> 1587`. Task 42 must compensate by keeping the existing `<= 8000` target, requiring at least `371` LOC reduction from the Task 41 result.
