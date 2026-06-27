# Task 34 Report: Objective And Alignment Adapter

Status: DONE_WITH_CONCERNS

Reason: The strict `main.py <= 9715` physical LOC target was not reached. The fallback target is restored: final `main.py` is 9815 physical LOC, exactly 220 lines below the provided 10035 baseline. Both requested hotspot methods remain below CCN 10 and Wily cyclomatic remains improved.

Commits:
- `0cba3e6` Extract objective alignment policy
- `7265fa5` Update task 34 report hash
- `d91bc2b` Restore objective alignment parity
- `d244a04` Update task 34 parity report hash
- `117e673` Clean up objective alignment adapter formatting
- `03197b8` Update task 34 cleanup report hash
- `89cbc81` Restore Task 34 LOC fallback
- `93cf7d2` Update Task 34 fallback report hash
- `0098039` Clarify objective offset helper docstring

Files changed:
- `main.py`
- `probe_station_gui/design/objective_alignment.py`
- `probe_station_gui/design/objective_offsets.py`
- `tests/design/test_objective_alignment.py`
- `tests/app/test_main_objective_alignment.py`
- `.superpowers/sdd/task-34-report.md`

Behavior summary:
- Added `probe_station_gui/design/objective_alignment.py` as directly tested pure decision policy for objective combo synchronization, objective selection, profile add/delete/reset/update, objective-change offset target planning, objective offset reference/save/reset, alignment capture position fallback, manual quick-alignment rotation planning, design-backed alignment capture decisions, and alignment presentation payloads.
- Kept `Main` as the side-effect adapter for Qt dialogs/widgets, settings replacement/saving, stage reads/moves/B rotation, coordinate conversion, design/session mutation, snap toggles, panel refresh, and status display.
- Preserved external status text and movement target behavior covered by app characterization tests.
- Follow-up parity fix restored objective selection final status ordering and old permissive objective calibration matrix persistence.
- Code-quality cleanup rewrapped compressed imports/planner calls, restored the concrete `_objective_offset_reference` annotation, and removed redundant first-point design alignment refresh flags from the pure plan while leaving `Main` as the single refresh source for that branch.
- Fallback recovery replaced the long objective/alignment from-import list with module aliases, centralized plan status emission in `Main`, moved active-objective fallback selection to `objective_alignment`, moved base/active route offset selection to `objective_offsets`, and removed a constant text wrapper. No Qt, hardware, settings ownership, or stage side effects were moved to the pure policy modules.
- Final docstring cleanup corrected `base_and_active_objective_offsets` wording without changing behavior or metrics.

TDD red/green evidence:
- RED: `python -m pytest tests\design\test_objective_alignment.py -q` failed with `ModuleNotFoundError: No module named 'probe_station_gui.design.objective_alignment'`.
- GREEN: `tests\design\test_objective_alignment.py` passed after implementing the pure module.
- Characterization: `tests\app\test_main_objective_alignment.py` passed before refactoring `Main`, then continued passing after adapter rewiring.
- Full-suite regression: one brittle dataclass identity assertion failed in full-suite order; changed it to compare reference fields, then full suite passed.
- Follow-up RED: `python -m pytest tests\app\test_main_objective_alignment.py::test_set_active_objective_reports_selected_after_offset_motion_status tests\design\test_objective_alignment.py::test_update_objective_calibration_preserves_old_permissive_matrix_conversion -q` failed because selected status came before offset status and singular calibration matrices were cleared.
- Follow-up GREEN: the same two regressions passed after deferring selected status until after offset handling and restoring the old permissive 2x2 float conversion policy.
- Fallback recovery: no new tests were added because this cleanup was intended to preserve behavior; existing focused and full regression suites were rerun.

Focused results:
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_objective_alignment.py tests\design\test_objective_offsets.py tests\settings\test_objective_config.py tests\app\test_main_objective_alignment.py tests\app\test_main_coordinate_feedrate.py tests\app\test_main_planned_move_prediction.py -q`
- Initial result: `200 passed, 3 subtests passed in 2.19s`
- Parity follow-up result: `202 passed, 3 subtests passed in 2.23s`
- Code-quality cleanup result: `202 passed, 3 subtests passed in 2.33s`
- Fallback recovery result: `202 passed, 3 subtests passed in 2.30s`
- Final docstring cleanup result: `202 passed, 3 subtests passed in 2.17s`

Full results:
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
- Initial result: `1079 passed, 2 skipped in 11.34s`
- Parity follow-up result: `1081 passed, 2 skipped in 11.09s`
- Code-quality cleanup result: `1081 passed, 2 skipped in 11.21s`
- Fallback recovery result: `1081 passed, 2 skipped in 11.25s`
- Final docstring cleanup result: `1081 passed, 2 skipped in 11.18s`

Ruff results:
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
- Initial result: `All checks passed!`
- Parity follow-up result: `All checks passed!`
- Code-quality cleanup result: `All checks passed!`
- Fallback recovery result: `All checks passed!`
- Final docstring cleanup result: `All checks passed!`

Metrics before:
- Provided baseline: `main.py` physical LOC 10035; Wily cyclomatic 1882; Wily raw LOC 10035; MI 0.
- Provided hotspots: `_apply_objective_change_offset` lizard 55 NLOC / CCN 13, radon C(13); `_capture_manual_alignment_point` 93 NLOC / CCN 13, radon C(13); `_set_objective_offset_reference` 42 / 7; `_save_active_objective_offset` 37 / 7; `_delete_objective_profile` 33 / 7; `_set_active_objective` 30 / 7; `_sync_objective_combo` 23 / 7; `_resolve_alignment_capture_stage_position` 20 / 6.

Metrics after:
- Final radon raw: `main.py` LOC 9815 / SLOC 9234; `objective_alignment.py` LOC 688 / SLOC 627; `objective_offsets.py` LOC 149 / SLOC 104.
- Final Wily from disposable UTF-8 temp snapshot/cache: `main.py` raw LOC 9815, cyclomatic 1865; `objective_alignment.py` raw LOC 688, cyclomatic 95, MI 10.8238; `objective_offsets.py` raw LOC 136 -> 149, cyclomatic 28 -> 29, MI 51.0152 -> 49.7363.
- Prior code-quality cleanup Wily values: `main.py` raw LOC 9865, cyclomatic 1875.
- Prior pre-cleanup values: `main.py` raw LOC 9806, cyclomatic 1873.
- Radon CC final: `_apply_objective_change_offset` A(5); `_capture_manual_alignment_point` B(6); `_set_objective_offset_reference` B(6); `_save_active_objective_offset` A(4); `_delete_objective_profile` A(5); `_set_active_objective` A(5); `_sync_objective_combo` B(8); `_resolve_alignment_capture_stage_position` A(5); new `objective_change_offset_plan` C(14); new `active_objective_configuration` A(3).
- Lizard final: `main.py` NLOC 9234, avg CCN 3.9, warning count 8; `objective_alignment.py` NLOC 627, avg CCN 4.8, warning count 0.
- Lizard hotspot final: `_apply_objective_change_offset` 23 NLOC / CCN 5; `_capture_manual_alignment_point` 45 NLOC / CCN 5; `_set_active_objective` 21 / 5; `_set_objective_offset_reference` 18 / 6; `_save_active_objective_offset` 14 / 4; `_delete_objective_profile` 24 / 5; `_sync_objective_combo` 21 / 8; `_resolve_alignment_capture_stage_position` 15 / 4.

Verdict:
- Justified: yes.
- Behavior preserved: yes, within covered objective/alignment behavior and full regression suite.
- Tests passed: yes.
- Metrics improved: yes. Wily cyclomatic improved from 1882 to 1865; fallback LOC reduction is restored at exactly 220 lines; both named hotspot methods are below CCN 10 and outside the lizard warning range.
- Maintainability improvement: objective/alignment decisions are pure, named, and directly tested outside `Main`; `Main` is reduced to side-effect orchestration for this workflow, with small shared helpers for repeated status/persistence paths. The small `objective_offsets.py` metric regression is accepted because it is one A(1) pure helper that removes base/active offset selection from `Main` without adding side effects.
- New risk introduced: low; the fallback recovery is a behavior-preserving cleanup verified by focused/full/ruff, with no hardware-dependent automation.
