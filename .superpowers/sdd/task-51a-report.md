# Task 51a Report: App Test Support Extraction

Date: 2026-06-29
Branch: `codex/refactor-stage-controller`
Base comparison revision: `a326d36`
Wily temp root: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task51a_20260629010959`

## Scope

Current behavior:

- `tests/app/test_main_coordinate_feedrate.py` contained both 162 `Main` adapter tests and nearly 1k lines of fake widgets, fake stage/controller helpers, route-start helpers, and import restoration logic.
- The file also supported direct `unittest.main()` execution.

Structural improvement:

- Extracted shared app-test support into `tests/app/main_coordinate_feedrate_support.py`.
- Kept the test scenarios and method names in `test_main_coordinate_feedrate.py`.
- Preserved direct test-file execution by importing the support module from the same directory and adding the repo root to `sys.path` inside the support module before importing `main`.
- Removed a shadowed duplicate `_FakeButton` definition during extraction.

Validation check:

- Focused pytest for the original test file.
- Direct `python tests/app/test_main_coordinate_feedrate.py`.
- Full suite through coverage.
- Full ruff.
- Wily diff from a disposable UTF-8 temp clone/cache.
- Radon/lizard/vulture spot checks on the touched test slice.

## Metrics

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| `test_main_coordinate_feedrate.py` LOC | 6585 | 5696 | yes |
| `test_main_coordinate_feedrate.py` SLOC | 5763 | 5025 | yes |
| `test_main_coordinate_feedrate.py` Wily cyclomatic | 389 | 200 | yes |
| `main_coordinate_feedrate_support.py` LOC | 0 | 947 | expected split |
| `main_coordinate_feedrate_support.py` Wily cyclomatic | 0 | 185 | expected split |
| Lizard warnings in touched slice | 0 | 0 | neutral |
| Coverage | 75% | 75% | neutral |
| Public/runtime API changed | no | no | yes |

Wily detail:

- `tests/app/test_main_coordinate_feedrate.py`: cyclomatic `389 -> 200`, LOC `6585 -> 5696`, MI `0 -> 0`.
- `tests/app/main_coordinate_feedrate_support.py`: cyclomatic `185`, LOC `947`, MI `5.2212`.

Radon/lizard detail:

- `tests/app/test_main_coordinate_feedrate.py`: `LOC 5696`, `SLOC 5025`, MI `0.00`.
- `tests/app/main_coordinate_feedrate_support.py`: `LOC 947`, `SLOC 797`, MI `5.22`.
- Lizard touched slice: `0` warnings.

Vulture detail:

- Vulture reports unused variables in fake callback signatures such as `clear_wait_state`, `required`, `minimum`, `names`, and `prefix`.
- These are suspicious but not automatically true dead code. They are keyword-compatible fake callback parameters used to match production call sites. Renaming them would risk breaking behavior when production code calls the fake with keyword arguments.

## Verification

- Focused pytest:
  - `.\.venv\Scripts\python.exe -m pytest tests\app\test_main_coordinate_feedrate.py`
  - Result: `162 passed`.
- Direct unittest-style execution:
  - `.\.venv\Scripts\python.exe tests\app\test_main_coordinate_feedrate.py`
  - Result: `Ran 162 tests ... OK`.
- Full tests with coverage:
  - `.\.venv\Scripts\python.exe -m coverage erase`
  - `.\.venv\Scripts\python.exe -m coverage run -m pytest tests`
  - `.\.venv\Scripts\python.exe -m coverage report -m --ignore-errors`
  - Result: `1240 passed, 2 skipped`, total coverage `75%`.
- Ruff:
  - `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - Result: no lint errors.
- Lizard:
  - `.\.venv\Scripts\python.exe -m lizard tests\app\test_main_coordinate_feedrate.py tests\app\main_coordinate_feedrate_support.py`
  - Result: `0` warnings.
- Wily:
  - Disposable UTF-8 temp clone/cache.
  - Result: metrics table above.

## Review Notes

- Read-only explorer mapped the remaining monolith and recommended the next split: move stage/coordinate test ranges into `tests/app/test_main_stage_coordinate_controls.py`.
- Read-only reviewer found direct file execution would break with a `tests.app...` import. This was fixed with a same-directory support import plus repo-root path setup in the support module.

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes, focused pytest, direct unittest execution, and full suite passed;
- tests passed: yes;
- metrics improved: yes, largest app test file LOC and cyclomatic complexity dropped substantially;
- maintainability improvement: test support is now a reusable app-test module, enabling future feature-group splits without duplicating fakes;
- new risk introduced: low; import path behavior was explicitly regression-checked with direct file execution.

## Next Suggested Pass

Task 51b: move the stage/coordinate groups into `tests/app/test_main_stage_coordinate_controls.py`:

- stage position display and coordinate input mode tests;
- needle-down target/settings save;
- coordinate move/feedrate/API coordinate move/manual jog tests;
- cancel button/cancel action/homing tests;
- coordinate tracking finish-on-idle tests.
