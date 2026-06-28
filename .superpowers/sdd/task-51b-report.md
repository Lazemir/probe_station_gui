# Task 51b Report: Split Stage Coordinate App Tests

Date: 2026-06-29
Base commit: `45cbedb`

## Scope

Current behavior:

- Stage position display, coordinate-target moves, coordinate cancel state, manual jog tracking, and homing-state app adapter tests lived inside `tests/app/test_main_coordinate_feedrate.py`.
- The file also retained support imports that were no longer needed after the split.

Structural improvement:

- Moved the stage/coordinate/cancel/home tracking tests into `tests/app/test_main_stage_coordinate_controls.py`.
- Removed 12 stale imports from `tests/app/test_main_coordinate_feedrate.py`.
- Kept `tests/app/main_coordinate_feedrate_support.py` as the shared helper module; no fixture model change in this pass.

Validation check:

- Focused pytest for the old and new app test files.
- Direct `unittest` execution for both split files.
- Full `pytest tests` under coverage.
- Configured ruff over the whole repo and strict F401 over the touched test files.
- Wily diff in disposable UTF-8 temp clone, comparing the final pass against `45cbedb`.

## Metrics

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| Largest file LOC in touched app slice | 5696 | 4941 | yes |
| Largest function LOC | 151 | 151 | neutral |
| Max cyclomatic complexity | 14 | 14 | neutral |
| Average cyclomatic complexity | 1.3 | 1.3 | neutral |
| Maintainability Index | old file 0 | old file 0, new file 17.31 | mixed |
| Functions >50 LOC | unchanged in remaining old-file route/API tests | unchanged | neutral |
| Functions with CC >10 | 1 support helper | 1 support helper | neutral |
| Duplicated blocks | not targeted | not changed | neutral |
| Dead code candidates | vulture suspicious fake/test attributes | suspicious fake/test attributes remain | neutral |
| Magic literals | not targeted | not changed | neutral |
| Lint errors | 12 F401 after move | 0 F401 in touched files; ruff clean repo-wide | yes |
| Type errors | not run; no configured cheap type gate | not run | neutral |
| Tests | `1240 passed, 2 skipped` before pass baseline | `1240 passed, 2 skipped` | yes |
| Coverage | 75% | 75% | stable |
| Public API changed | no | no | yes |

Wily final diff:

- `tests/app/test_main_coordinate_feedrate.py`: cyclomatic `200 -> 158`, LOC `5696 -> 4941`, MI `0 -> 0`.
- `tests/app/test_main_stage_coordinate_controls.py`: added with cyclomatic `44`, LOC `766`, MI `17.31`.
- Wily temp root: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task51b_final_20260629012114`.

Radon/lizard final slice:

- `tests/app/test_main_coordinate_feedrate.py`: `LOC 4941`, `SLOC 4387`, MI `0.00`.
- `tests/app/test_main_stage_coordinate_controls.py`: `LOC 766`, `SLOC 647`, MI `17.31`.
- `tests/app/main_coordinate_feedrate_support.py`: `LOC 947`, `SLOC 797`, MI `5.22`.
- Lizard touched slice: `Total nloc 5796`, average CCN `1.2`, warning count `0`.

Vulture:

- Still reports many suspicious fake methods/attributes and injected callback parameter names. Treat as false-positive-heavy test double surface, not deletion evidence.

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests\app\test_main_coordinate_feedrate.py tests\app\test_main_stage_coordinate_controls.py` -> `162 passed`.
- `.\.venv\Scripts\python.exe tests\app\test_main_stage_coordinate_controls.py` -> `37 tests OK`.
- `.\.venv\Scripts\python.exe tests\app\test_main_coordinate_feedrate.py` -> `125 tests OK`.
- `.\.venv\Scripts\python.exe -m ruff check --select F401 tests\app\test_main_coordinate_feedrate.py tests\app\test_main_stage_coordinate_controls.py` -> clean.
- `.\.venv\Scripts\python.exe -m coverage run -m pytest tests` -> `1240 passed, 2 skipped`.
- `.\.venv\Scripts\python.exe -m coverage report -m --ignore-errors` -> total `75%`; existing PySide/shiboken coverage parse warnings remain.
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .` -> clean.

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes
- tests passed: yes
- metrics improved: yes, largest app test file and old-file cyclomatic decreased
- maintainability improvement: stage/coordinate app behavior is now reviewable without scrolling through route/meter/API tests
- new risk introduced: low; split depends on the shared same-directory support import, covered by direct `unittest` execution
