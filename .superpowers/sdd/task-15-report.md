# Task 15 Report: Extract route finish outcome planning

## Status

DONE

## Changed Files

- `main.py`
- `probe_station_gui/route/finish_flow.py`
- `tests/route/test_finish_flow.py`
- `tests/app/test_main_coordinate_feedrate.py`
- `.superpowers/sdd/task-15-report.md`

## Commit Hash(es)

- `f941a4c` - Extract route finish planning.

## Tests Run and Exact Results

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_finish_flow.py`
  - Initial RED result before implementation: `ModuleNotFoundError: No module named 'probe_station_gui.route.finish_flow'`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_finish_flow.py tests\app\test_main_coordinate_feedrate.py -q`
  - `105 passed in 1.83s`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route tests\app\test_main_coordinate_feedrate.py tests\ui\test_design_navigator_panel.py`
  - `312 passed in 4.03s`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - `796 passed, 2 skipped in 10.93s`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - `All checks passed!`

## Wily / Radon / Lizard Metrics Before and After

Wily was tried first with UTF-8 output and cache under `%TEMP%`:

- `$env:PYTHONIOENCODING='utf-8'; $env:WILY_CACHE_DIR=Join-Path $env:TEMP 'probe_station_gui_wily_task15'; C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m wily report main.py raw.loc cyclomatic.complexity maintainability.mi`
- `$env:PYTHONIOENCODING='utf-8'; $env:WILY_CACHE_DIR=Join-Path $env:TEMP 'probe_station_gui_wily_task15'; C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m wily report probe_station_gui\route\finish_flow.py raw.loc cyclomatic.complexity maintainability.mi`

Wily produced repository history/file-level output, but it did not provide useful current-tree method detail for the uncommitted target function, and the new helper file was reported as `Not found` in historical revisions. Exact fallback metrics were therefore taken from radon/lizard.

Before:

- `main.py:Main._on_route_measurement_finished`
  - Radon: `D (26)`
  - Lizard: `NLOC 107`, `CCN 26`, `tokens 522`, `length 107`

After:

- `main.py:Main._on_route_measurement_finished`
  - Radon: `A (5)`
  - Lizard: `NLOC 44`, `CCN 5`, `tokens 214`, `length 45`
- `probe_station_gui/route/finish_flow.py`
  - Radon: helpers are `A`, max `A (5)` for `route_finish_signal_plan`
  - Lizard: `NLOC 152`, max helper `CCN 5`; no warnings

## Verdict

The refactor is justified. The route finish outcome rules now have pure route-module tests, and the `Main._on_route_measurement_finished` hotspot dropped from `D (26)` / lizard `CCN 26` to `A (5)` / lizard `CCN 5`. Main remains the adapter for thread joins, API final status persistence, state reset, CSV counting, UI updates, and Telegram dispatch.

## Concerns / Residual Gaps

- Wily did not provide useful current-tree method-level detail for this pass; radon and lizard were used as exact fallback metrics.
- No behavior concerns remain from this pass.

## Controller verification

- task review:
  - approved; no Critical or Important findings.
  - Minor finding fixed here: report commit hash is now recorded as `f941a4c`.
- controller-run validation:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
    - final: `796 passed, 2 skipped`
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
    - final: `All checks passed!`
- final spot checks:
  - `main.py:Main._on_route_measurement_finished`: radon `A (5)`, lizard `44 NLOC / CCN 5`
  - `probe_station_gui/route/finish_flow.py`: MI `A (39.22)`, lizard max helper CCN `5`
  - final lizard warning count for `main.py` + `finish_flow.py`: `21`; `_on_route_measurement_finished` no longer appears in the warning list.
