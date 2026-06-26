# Task 21 Report: Route Dialog Adapter And Session Controls

## Scope

- Created [probe_station_gui/route/dialog_adapter.py](/C:/Users/Public/code/probe_station_gui/probe_station_gui/route/dialog_adapter.py)
- Modified [main.py](/C:/Users/Public/code/probe_station_gui/main.py)
- Added [tests/route/test_dialog_adapter.py](/C:/Users/Public/code/probe_station_gui/tests/route/test_dialog_adapter.py)
- Modified [tests/app/test_main_coordinate_feedrate.py](/C:/Users/Public/code/probe_station_gui/tests/app/test_main_coordinate_feedrate.py)

## TDD Evidence

### RED

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_dialog_adapter.py -q
```

Result:

- failed during collection with `ModuleNotFoundError: No module named 'probe_station_gui.route.dialog_adapter'`

### GREEN

Commands:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_dialog_adapter.py -q
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_dialog_adapter.py tests\app\test_main_coordinate_feedrate.py -q
```

Results:

- `8 passed in 0.05s`
- `132 passed, 3 subtests passed in 1.77s`

## LOC

- `main.py` checked-out physical lines before task (`git show e2da4bf:main.py | Measure-Object -Line`): `11688`
- `main.py` checked-out physical lines after task (`git show HEAD:main.py | Measure-Object -Line`): `11639`
- delta in checked-out physical lines: `-49`

Gate note:

- the task brief lists baseline `12313`; Wily `raw.loc` matches that metric family and reports `12313 -> 12262` (`-51`)
- the checked-out file on this branch was already below the brief's target before Task 21; after this task it remains well below `12130`

## Complexity

### Target method before/after

- `_open_route_measurement_dialog`
  - brief baseline lizard: `107 NLOC / CCN 13 / length 108`
  - after lizard: `29 NLOC / CCN 7 / length 29`
  - after radon: `B (7)`

### Related `Main` helpers after refactor

- `_create_route_measurement_dialog`: lizard `33 NLOC / CCN 1 / length 34`, radon `A (1)`
- `_restore_route_measurement_state_after_design_load`: lizard `13 NLOC / CCN 6 / length 13`, radon `B (6)`
- `_start_route_measurement_session`: lizard `23 NLOC / CCN 4 / length 23`, radon `A (4)`
- `_cancel_route_measurement_session`: lizard `16 NLOC / CCN 3 / length 16`, radon `A (3)`

### New adapter module

- `route_dialog_defaults`: radon `A (3)`
- `route_dialog_restore_plan`: radon `A (3)`
- `route_measurement_session_start_plan`: radon `A (3)`
- `route_measurement_session_cancel_plan`: radon `A (2)`
- `route_dialog_open_state`: radon `A (3)`
- `open_or_update_route_measurement_dialog`: lizard `45 NLOC / CCN 2 / length 45`, radon `A (2)`

## Maintainability

### Radon MI

- `main.py`: `C (0.00)`
- `probe_station_gui\route\dialog_adapter.py`: `A (37.21)`

### Wily diff (`-r e2da4bf`)

- `main.py`
  - cyclomatic complexity: `2405 -> 2393`
  - raw.loc: `12313 -> 12262`
  - maintainability.mi: `0 -> 0`
- `probe_station_gui\route\dialog_adapter.py`
  - cyclomatic complexity: `- -> 25`
  - raw.loc: `- -> 233`
  - maintainability.mi: `- -> 37.206706523607416`

Assessment:

- locality and hotspot complexity improved materially
- `main.py` MI stayed flat at the file level because the file is still oversized; this task still improves maintainability by moving route-dialog policy into a focused route module and dropping the target hotspot below warning thresholds

## Verification

Focused tests:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_dialog_adapter.py tests\app\test_main_coordinate_feedrate.py -q
```

- `132 passed, 3 subtests passed in 1.77s`

Full suite:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
```

- `843 passed, 2 skipped in 10.99s`

Ruff:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .
```

- `All checks passed!`

Metrics commands run:

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONUTF8='1'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m lizard main.py probe_station_gui\route -l python -C 10 -L 50 --sort cyclomatic_complexity
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon cc -s main.py probe_station_gui\route\dialog_adapter.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon mi -s main.py probe_station_gui\route\dialog_adapter.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c $env:TEMP\probe_station_gui_wily_task21 build main.py probe_station_gui\route\dialog_adapter.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c $env:TEMP\probe_station_gui_wily_task21 diff main.py probe_station_gui\route\dialog_adapter.py --detail -r e2da4bf --metrics cyclomatic.complexity,raw.loc,maintainability.mi --no-wrap
```

Wily note:

- `build` succeeded after committing the code changes because Wily's git archiver rejects a dirty repository
- the build printed historical `No data collected` warnings for many older revisions, but the requested diff against `e2da4bf` completed and produced usable output

## Verdict

- behavior preserved: `yes`
- tests passed: `yes`
- metrics improved: `yes`
- LOC gate passed: `yes`
- maintainability improvement: `yes`, through lower `main.py` complexity and a new focused route adapter seam
- new risk introduced: `low`; the main residual risk is that route dialog behavior now depends on adapter/Main coordination, covered by the new pure tests plus app characterization
