# Task 51c Report: Split Route-Control App Tests

Date: 2026-06-29
Base commit: `40a1f00`

## Scope

Current behavior:

- API Route Control, pause/resume/interrupt, route-control shift saving, Telegram route actions, and related route-control dialog button tests lived inside `tests/app/test_main_coordinate_feedrate.py` with meter/contact/session-start tests.
- These tests protect safety-critical Pause/Resume/Interrupt semantics and were hard to review in the mixed app-test module.

Structural improvement:

- Moved the route-control/API Route Control tests into `tests/app/test_main_route_control.py`.
- Kept adjacent contact/meter/session-start tests in `tests/app/test_main_coordinate_feedrate.py`.
- Removed 4 stale imports from the old file after the split.
- Kept direct `unittest.main()` execution on the new file.

Validation check:

- Focused pytest for the old app file, new route-control file, and existing stage/coordinate split file.
- Direct `unittest` execution for old and new split files.
- Full `pytest tests` under coverage.
- Configured ruff over the whole repo and strict F401 over the touched app files.
- Wily diff in disposable UTF-8 temp clone, comparing the final pass against `40a1f00`.

## Metrics

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| Largest file LOC in touched app slice | 4941 | 3711 | yes |
| Largest function LOC | 151 | 151 | neutral |
| Max cyclomatic complexity | 14 | 14 | neutral |
| Average cyclomatic complexity | 1.3 | 1.3 | neutral |
| Maintainability Index | old file 0 | old file 0, new file 9.38 | mixed |
| Functions >50 LOC | unchanged in remaining tests | unchanged | neutral |
| Functions with CC >10 | 1 support helper | 1 support helper | neutral |
| Duplicated blocks | not targeted | not changed | neutral |
| Dead code candidates | vulture suspicious fake/test attributes | suspicious fake/test attributes remain | neutral |
| Magic literals | not targeted | not changed | neutral |
| Lint errors | 4 F401 after move | 0 F401 in touched files; ruff clean repo-wide | yes |
| Type errors | not run; no configured cheap type gate | not run | neutral |
| Tests | `1240 passed, 2 skipped` before pass baseline | `1240 passed, 2 skipped` | yes |
| Coverage | 75% | 75% | stable |
| Public API changed | no | no | yes |

Wily final diff:

- `tests/app/test_main_coordinate_feedrate.py`: cyclomatic `158 -> 99`, LOC `4941 -> 3711`, MI `0 -> 0`.
- `tests/app/test_main_route_control.py`: added with cyclomatic `61`, LOC `1249`, MI `9.38`.
- Wily temp root: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task51c_20260629203357`.

Radon/lizard final slice:

- `tests/app/test_main_coordinate_feedrate.py`: `LOC 3711`, `SLOC 3315`, MI `0.00`.
- `tests/app/test_main_route_control.py`: `LOC 1249`, `SLOC 1088`, MI `9.38`.
- `tests/app/test_main_stage_coordinate_controls.py`: `LOC 766`, `SLOC 647`, MI `17.31`.
- `tests/app/main_coordinate_feedrate_support.py`: `LOC 947`, `SLOC 797`, MI `5.22`.
- Lizard touched slice: `Total nloc 5812`, average CCN `1.2`, warning count `0`.

Vulture:

- Still reports many suspicious fake methods/attributes and injected callback parameter names. Treat as false-positive-heavy test double surface, not deletion evidence.

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests\app\test_main_coordinate_feedrate.py tests\app\test_main_route_control.py tests\app\test_main_stage_coordinate_controls.py` -> `162 passed`.
- `.\.venv\Scripts\python.exe tests\app\test_main_route_control.py` -> `39 tests OK`.
- `.\.venv\Scripts\python.exe tests\app\test_main_coordinate_feedrate.py` -> `86 tests OK`.
- `.\.venv\Scripts\python.exe -m ruff check --select F401 tests\app\test_main_coordinate_feedrate.py tests\app\test_main_route_control.py tests\app\test_main_stage_coordinate_controls.py` -> clean.
- `.\.venv\Scripts\python.exe -m coverage run -m pytest tests` -> `1240 passed, 2 skipped`.
- `.\.venv\Scripts\python.exe -m coverage report -m --ignore-errors` -> total `75%`; existing PySide/shiboken coverage parse warnings remain.
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .` -> clean.

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes
- tests passed: yes
- metrics improved: yes, largest app test file and old-file cyclomatic decreased
- maintainability improvement: safety-critical API Route Control tests now have a dedicated review surface
- new risk introduced: low; direct execution and focused/full pytest cover the split import path
