# Task 53b Report: Route Contact Seek Test Split

## Scope

Current behavior:
- `tests/route/test_measurement.py` still held route runner contact placement, auto-contact seek, and runtime remeasure characterization after the API route control split.
- The safety-critical `test_pause_during_auto_contact_seek_waits_for_saved_point` lived inside the remaining route test monolith.

Structural improvement:
- Moved 17 contact placement / auto-contact seek / runtime remeasure tests into `tests/route/test_measurement_contact_seek.py`.
- Kept production code and public interfaces unchanged.
- Preserved direct file execution with `unittest.main()` and package-style unittest execution through the existing support-module import fallback.
- Kept the split file depending only on stdlib, `probe_station_gui.route.measurement`, and `tests/route/measurement_test_support.py`.

Validation check:
- Focused route split tests, direct file execution, module-style unittest execution, full suite, configured ruff, Wily/radon/lizard/vulture metrics, and two reviewer agents.

## Metrics

Wily comparison ran in disposable temp clone:

`C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task53b_final2_20260629224451`

Compared temporary Task 53b commit against `c59c014`.

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| Largest file LOC | 3201 | 2198 | Yes |
| Largest function LOC | 83 | 83 | Neutral |
| Max cyclomatic complexity | 11 | 11 | Neutral |
| Average cyclomatic complexity | 2.24 | 1.70 touched slice | Yes |
| Maintainability Index | `test_measurement.py` 0 | `test_measurement.py` 0; new contact seek file 16.71 | Yes |
| Functions >50 LOC | unchanged in remaining route suite | unchanged | Neutral |
| Functions with CC >10 | 1 | 1 | Neutral |
| Duplicated blocks | contact seek tests mixed into route monolith | contact seek tests have their own module and shared support imports | Yes |
| Dead code candidates | none in touched route files | none in touched route files | Neutral |
| Magic literals | unchanged characterization literals | unchanged characterization literals | Neutral |
| Lint errors | 0 | 0 | Yes |
| Type errors | not run | not run | N/A |
| Tests | 1240 passed, 2 skipped | 1240 passed, 2 skipped | Yes |
| Coverage | 75% | 75% | Neutral |
| Public API changed | no | no | Yes |

Wily file deltas:

- `tests/route/test_measurement.py`: LOC `3201 -> 2198`, SLOC `2857 -> 1956`, cyclomatic `192 -> 116`, MI `0 -> 0`.
- `tests/route/test_measurement_contact_seek.py`: LOC `1031`, SLOC `921`, cyclomatic `41`, MI `16.71`.
- Touched-slice aggregate Wily cyclomatic: `192 -> 157`.

Radon raw after:

- `tests/route/test_measurement.py`: LOC `2198`, SLOC `1956`.
- `tests/route/test_measurement_contact_seek.py`: LOC `1031`, SLOC `921`.
- `tests/route/measurement_test_support.py`: LOC `299`, SLOC `242`.
- `tests/route/test_measurement_api_route_control.py`: LOC `546`, SLOC `499`.

Lizard after:

- Touched route split slice: 0 warnings.
- Total NLOC `3618`, average CCN `1.7`, function count `165`.

Vulture after:

- No `--min-confidence 80` dead-code candidates in the four touched route test files.

Coverage after:

- `tests/route/measurement_test_support.py`: 96%.
- `tests/route/test_measurement.py`: 99%.
- `tests/route/test_measurement_api_route_control.py`: 99%.
- `tests/route/test_measurement_contact_seek.py`: 99%.
- Total project coverage: 75%.

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests\route\test_measurement.py tests\route\test_measurement_contact_seek.py tests\route\test_measurement_api_route_control.py tests\route\test_measurement.py::RouteMeasurementRunnerTest::test_interactive_pause_waits_after_current_saved_point -q`
  - `76 passed`
- `.\.venv\Scripts\python.exe tests\route\test_measurement_contact_seek.py`
  - `17 tests OK`
- `.\.venv\Scripts\python.exe -m unittest tests.route.test_measurement_contact_seek`
  - `17 tests OK`
- `.\.venv\Scripts\python.exe -m coverage run -m pytest tests`
  - `1240 passed, 2 skipped`
- `.\.venv\Scripts\python.exe -m coverage report -m --ignore-errors`
  - total coverage `75%`
  - used `--ignore-errors` because coverage still sees missing PySide/shiboken helper paths such as `pyscript` and `shibokensupport`.
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - passed
- `git diff --check`
  - exit 0; only LF/CRLF warning for `tests/route/test_measurement.py`

## Reviews

- Spec reviewer: approved; confirmed production code unchanged, the 17 moved tests are present, the new split imports only allowed support/production modules, and direct execution is preserved.
- Code-quality reviewer: approved; confirmed the safety-critical pause-during-auto-contact-seek test remains strong and readable. Optional annotation nit on `_calls_named` was fixed before commit.

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes
- tests passed: yes
- metrics improved: yes, largest file LOC dropped by 1003 lines and touched-slice Wily cyclomatic dropped from `192` to `157`
- maintainability improvement: contact seek/placement/remeasure characterization now has its own module with locality around that route lifecycle
- new risk introduced: low; Task 53 still needs one more split because `tests/route/test_measurement.py` remains 2198 LOC
