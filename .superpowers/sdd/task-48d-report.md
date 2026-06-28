# Task 48d Report: Route Measurement Histogram Paint Cleanup

## Scope

Current behavior:

- `RouteMeasurementHistogram.paintEvent()` computed histogram buckets, axis tick labels, legend placement, and bar drawing in one Qt paint method.
- Flat sample ranges were expanded before drawing so the x-axis had a non-zero span.
- Polarity mode drew two overlaid series; differential mode drew one series.

Structural improvement:

- Added `_HistogramData` to hold the paint-time histogram payload.
- Split data preparation, plot geometry, axis drawing, legend drawing, and bar drawing into private helpers behind the existing `RouteMeasurementHistogram` widget interface.
- Added a characterization test for flat sample range expansion.

Public API changed: no.

## Metrics

Wily ran from disposable UTF-8 temp clone/cache:

- Temp root: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task48d_20260628163608`
- Baseline: `ac3da53`

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| `route_measurement_result_views.py` Wily LOC | 272 | 376 | no |
| `route_measurement_result_views.py` Wily cyclomatic | 46 | 54 | no |
| `route_measurement_result_views.py` MI | 28.76 | 23.99 | no |
| `RouteMeasurementHistogram.paintEvent` CCN | 24 | 4 | yes |
| Largest helper CCN in histogram slice | 24 | 9 | yes |
| Lizard warnings in result view module | 1 | 0 | yes |
| Focused route/result tests | 9 passed | 10 passed | yes |
| Tests | 1215 passed, 2 skipped | 1216 passed, 2 skipped | yes |
| Coverage total | 73% | 73% | flat |
| Ruff errors | 0 | 0 | flat |

Remaining lizard warnings in the Task 48 slice:

- `RouteMeasurementProfileMixin._apply_profile_data`: 68 NLOC / CCN 16.

## Validation

- `.\.venv\Scripts\python.exe -m pytest tests\route\test_route_measurement_result_views.py tests\route\test_measurement_display.py`
  - `10 passed`
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 probe_station_gui\dialogs\route_measurement_result_views.py tests\route\test_route_measurement_result_views.py`
  - clean
- `.\.venv\Scripts\python.exe -m radon cc -s -a probe_station_gui\dialogs\route_measurement_result_views.py`
  - average complexity `A (2.7)`, `paintEvent` `A (4)`, `_histogram_data` `B (9)`
- `.\.venv\Scripts\python.exe -m radon mi -s probe_station_gui\dialogs\route_measurement_result_views.py`
  - `A (23.99)`
- `.\.venv\Scripts\python.exe -m lizard -w probe_station_gui\dialogs\route_measurement_result_views.py`
  - no warnings
- `.\.venv\Scripts\python.exe -m coverage run -m pytest tests`
  - `1216 passed, 2 skipped`
- `.\.venv\Scripts\python.exe -m coverage report -m --ignore-errors`
  - total coverage `73%`
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - clean

## Verdict

This refactor is justified, but only as a hotspot cleanup.

Reason:

- behavior preserved: yes
- tests passed: yes
- metrics improved: yes for max-function complexity and lizard warnings
- maintainability improvement: paint-time data calculation and drawing responsibilities are now local helpers instead of one warning-level paint method
- new risk introduced: medium; Wily aggregate LOC, aggregate cyclomatic, and MI for the file worsened because drawing branches were split into helper methods rather than deleted

Follow-up:

- Do not use this pass as evidence that the whole result-view module improved by aggregate Wily metrics.
- If future work touches this widget again, prefer removing duplicated drawing branches or moving to a tested painter model only if the aggregate Wily result also improves.
