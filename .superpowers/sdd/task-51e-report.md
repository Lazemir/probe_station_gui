# Task 51e Report: Split Meter/Contact App Tests

Date: 2026-06-29
Base commit: `f30ff69`

## Scope

Current behavior:

- Raw meter configuration, raw voltage sweep, move-to-contact, needle action, contact check, and contact seek API tests still lived in `tests/app/test_main_coordinate_feedrate.py`.
- The former mixed app-test file was `2292 LOC` after Task 51d and remained above the Task 51 target.

Structural improvement:

- Moved 29 meter/contact/API action tests into `tests/app/test_main_meter_contact_actions.py`.
- Kept route photo/artifact behavior, sample focus/load/unload, progress/ETA, route contact move, waiting-dialog runtime controls, close/shutdown behavior, interrupt/current route selection, and contact-height CSV tests in `tests/app/test_main_coordinate_feedrate.py`.
- Preserved direct `unittest.main()` execution for both files.
- Removed stale imports from the old file after the split.

Validation check:

- Focused pytest for all app split files in the Task 51 slice.
- Direct `unittest` execution for old and new split files.
- Full `pytest tests` under coverage.
- Configured ruff over the whole repo and strict F401 over the touched app files.
- Wily diff in disposable UTF-8 temp clone, comparing the final pass against `f30ff69`.
- Two read-only reviewer subagents checked spec compliance and split quality.

## Metrics

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| Largest file LOC in app split slice | 2292 | 1445 | yes |
| Former monolith LOC | 2292 | 1002 | yes |
| Aggregate Wily cyclomatic for split pair | 65 | 67 | mixed |
| Largest function LOC | no lizard warnings | no lizard warnings | stable |
| Max cyclomatic complexity | no lizard warnings | no lizard warnings | stable |
| Average cyclomatic complexity | 1.2 | 1.2 | stable |
| Maintainability Index | old file 5.41 | old file 20.60, new file 16.48 | yes |
| Functions >50 LOC | unchanged long test bodies | unchanged long test bodies | neutral |
| Functions with CC >10 | 1 support helper | 1 support helper | neutral |
| Duplicated blocks | not targeted | not changed | neutral |
| Dead code candidates | suspicious fake/test callback names | suspicious fake/test callback names remain | neutral |
| Magic literals | not targeted | not changed | neutral |
| Lint errors | 0 F401 after worker cleanup | 0 F401; ruff clean repo-wide | yes |
| Type errors | not run; no configured cheap type gate | not run | neutral |
| Tests | `1240 passed, 2 skipped` before pass baseline | `1240 passed, 2 skipped` | yes |
| Coverage | 75% | 75% | stable |
| Public API changed | no | no | yes |

Wily final diff:

- `tests/app/test_main_coordinate_feedrate.py`: LOC `2292 -> 1002`, SLOC `2060 -> 899`, cyclomatic `65 -> 31`, MI `5.41 -> 20.60`.
- `tests/app/test_main_meter_contact_actions.py`: added with LOC `1306`, SLOC `1173`, cyclomatic `36`, MI `16.48`.
- The split pair's aggregate Wily cyclomatic is `65 -> 67`; this is accepted as split-class/module overhead because lizard average CCN stayed `1.2`, warning count stayed `0`, and the former monolith's MI/locality improved.
- Wily temp root: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task51e_20260629210226`.

Radon/lizard final slice:

- `tests/app/test_main_coordinate_feedrate.py`: `LOC 1002`, `SLOC 899`, MI `20.60`.
- `tests/app/test_main_meter_contact_actions.py`: `LOC 1306`, `SLOC 1173`, MI `16.48`.
- `tests/app/test_main_route_measurement_session.py`: `LOC 1445`, `SLOC 1277`, MI `13.42`.
- `tests/app/test_main_route_control.py`: `LOC 1249`, `SLOC 1088`, MI `9.38`.
- `tests/app/test_main_stage_coordinate_controls.py`: `LOC 766`, `SLOC 647`, MI `17.31`.
- `tests/app/main_coordinate_feedrate_support.py`: `LOC 947`, `SLOC 797`, MI `5.22`.
- Lizard touched slice: `Total nloc 5846`, average CCN `1.2`, warning count `0`.

Vulture:

- Still reports suspicious test double callback names and local lambda parameters.
- These are false-positive-heavy characterization-test surfaces and were not deletion evidence for this pass.

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests\app\test_main_coordinate_feedrate.py tests\app\test_main_meter_contact_actions.py tests\app\test_main_route_measurement_session.py tests\app\test_main_route_control.py tests\app\test_main_stage_coordinate_controls.py` -> `162 passed`.
- `.\.venv\Scripts\python.exe tests\app\test_main_meter_contact_actions.py` -> `29 tests OK`.
- `.\.venv\Scripts\python.exe tests\app\test_main_coordinate_feedrate.py` -> `25 tests OK`.
- `.\.venv\Scripts\python.exe -m ruff check --select F401 tests\app\test_main_coordinate_feedrate.py tests\app\test_main_meter_contact_actions.py` -> clean.
- `.\.venv\Scripts\python.exe -m coverage run -m pytest tests` -> `1240 passed, 2 skipped`.
- `.\.venv\Scripts\python.exe -m coverage report -m --ignore-errors` -> total `75%`; existing PySide/shiboken coverage parse warnings remain.
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .` -> clean.
- `.\.venv\Scripts\python.exe -m lizard -w tests\app\test_main_coordinate_feedrate.py tests\app\test_main_meter_contact_actions.py tests\app\test_main_route_measurement_session.py tests\app\test_main_route_control.py tests\app\test_main_stage_coordinate_controls.py tests\app\main_coordinate_feedrate_support.py` -> no warnings.
- `git diff --check -- tests/app/test_main_coordinate_feedrate.py tests/app/test_main_meter_contact_actions.py` -> clean except the existing Windows line-ending warning.

Reviewer results:

- Spec reviewer: no blocking findings; exactly the intended 29 tests moved, intended stay-behind groups remain, direct execution preserved.
- Quality reviewer: no blocking findings; no lost/duplicated test names, no stale imports, direct execution and pytest passed.

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes
- tests passed: yes
- metrics improved: yes, the former app test monolith is below target and MI/cyclomatic improved
- maintainability improvement: raw meter/contact API action tests now have a dedicated review surface
- new risk introduced: low; focused/full pytest and two read-only reviews cover the split
