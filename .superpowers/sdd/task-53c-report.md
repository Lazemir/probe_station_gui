# Task 53c Report: Route Readout Quality Test Split

## Scope

Current behavior:
- `tests/route/test_measurement.py` still held readout, batch measurement, recording, contact-quality, and interactive confirmation characterization after the API route-control and contact-seek splits.
- The safety-critical `test_interactive_pause_waits_after_current_saved_point` remained inside the old route test file.

Structural improvement:
- Moved 24 readout / recording / contact-quality / interactive confirmation tests into `tests/route/test_measurement_readout_quality.py`.
- Kept production code and public interfaces unchanged.
- Preserved direct file execution with `unittest.main()` and package-style unittest execution through the existing support-module import fallback.
- Added a local `_events_named` helper in the new split file to keep event assertions readable and avoid a Wily aggregate complexity regression.

Validation check:
- Focused route split tests, direct file execution, module-style unittest execution, full suite, configured ruff, Wily/radon/lizard/vulture metrics, and two reviewer agents.

## Metrics

Wily comparison ran in disposable temp clone:

`C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task53c_final_20260629225659`

Compared temporary Task 53c commit against `35a6262`.

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| Largest file LOC | 2198 | 1178 | Yes |
| Largest function LOC | 83 | 83 | Neutral |
| Max cyclomatic complexity | 9 | 9 | Neutral |
| Average cyclomatic complexity | 2.27 old file | 1.70 touched slice | Yes |
| Maintainability Index | `test_measurement.py` 0 | `test_measurement.py` 9.11; new readout/quality file 14.34 | Yes |
| Functions >50 LOC | unchanged in split route suite | unchanged | Neutral |
| Functions with CC >10 | 0 | 0 | Neutral |
| Duplicated blocks | readout/quality tests mixed into route monolith | readout/quality tests have their own module and shared support imports | Yes |
| Dead code candidates | none in touched route files | none in touched route files | Neutral |
| Magic literals | unchanged characterization literals | unchanged characterization literals | Neutral |
| Lint errors | 0 | 0 | Yes |
| Type errors | not run | not run | N/A |
| Tests | 1240 passed, 2 skipped | 1240 passed, 2 skipped | Yes |
| Coverage | 75% | 75% | Neutral |
| Public API changed | no | no | Yes |

Wily file deltas:

- `tests/route/test_measurement.py`: LOC `2198 -> 1178`, SLOC `1956 -> 1040`, cyclomatic `116 -> 63`, MI `0 -> 9.11`.
- `tests/route/test_measurement_readout_quality.py`: LOC `1055`, SLOC `944`, cyclomatic `50`, MI `14.34`.
- Touched-slice aggregate Wily cyclomatic: `116 -> 113`.

Radon raw after:

- `tests/route/test_measurement.py`: LOC `1178`, SLOC `1040`.
- `tests/route/test_measurement_readout_quality.py`: LOC `1055`, SLOC `944`.
- `tests/route/test_measurement_contact_seek.py`: LOC `1031`, SLOC `921`.
- `tests/route/test_measurement_api_route_control.py`: LOC `546`, SLOC `499`.
- `tests/route/measurement_test_support.py`: LOC `299`, SLOC `242`.

Lizard after:

- Touched route split slice: 0 warnings.
- Total NLOC `3646`, average CCN `1.7`, function count `165`.

Vulture after:

- No `--min-confidence 80` dead-code candidates in the five touched route test files.

Coverage after:

- `tests/route/measurement_test_support.py`: 96%.
- `tests/route/test_measurement.py`: 99%.
- `tests/route/test_measurement_api_route_control.py`: 99%.
- `tests/route/test_measurement_contact_seek.py`: 99%.
- `tests/route/test_measurement_readout_quality.py`: 99%.
- Total project coverage: 75%.

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests\route\test_measurement.py tests\route\test_measurement_readout_quality.py tests\route\test_measurement_contact_seek.py tests\route\test_measurement_api_route_control.py -q`
  - `76 passed`
- `.\.venv\Scripts\python.exe tests\route\test_measurement_readout_quality.py`
  - `24 tests OK`
- `.\.venv\Scripts\python.exe -m unittest tests.route.test_measurement_readout_quality`
  - `24 tests OK`
- `.\.venv\Scripts\python.exe -m coverage run -m pytest tests`
  - `1240 passed, 2 skipped`
- `.\.venv\Scripts\python.exe -m coverage report -m --ignore-errors`
  - total coverage `75%`
  - used `--ignore-errors` because coverage still sees missing PySide/shiboken helper paths such as `pyscript` and `shibokensupport`.
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - passed
- `git diff --check`
  - passed

## Reviews

- Spec reviewer: approved; confirmed exactly the requested 24 tests moved, production code unchanged, imports limited to allowed modules, direct execution preserved, and the safety-critical pause test was not weakened.
- Code-quality reviewer: approved; confirmed the `_events_named` cleanup is local/readable and the safety-critical pause test still verifies request, waiting state, blocked worker, resume, and saved rows.

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes
- tests passed: yes
- metrics improved: yes, largest file LOC dropped below target, `test_measurement.py` MI improved from `0` to `9.11`, and touched-slice Wily cyclomatic improved from `116` to `113`
- maintainability improvement: route runner tests are now split by API route control, contact seek, readout/quality, and remaining route lifecycle responsibilities; no route measurement test file is above 1600 LOC
- new risk introduced: low; this pass intentionally did not touch production code
