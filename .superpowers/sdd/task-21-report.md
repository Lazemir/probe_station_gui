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

- initial GREEN after first adapter pass: `8 passed in 0.05s`
- fix-pass focused GREEN: `137 passed, 3 subtests passed in 1.64s`

## LOC

- authoritative baseline (`git show e2da4bf:main.py | python -c "import sys; print(len(sys.stdin.read().splitlines()))"`): `12313`
- authoritative current (`main.py | python -c "import sys; print(len(sys.stdin.read().splitlines()))"`): `12159`
- authoritative delta: `-154`

Gate note:

- target `<= 12130`: `not met`
- fallback minimum `<= 12163`: `met`
- LOC gate verdict for acceptance: `passed via fallback minimum`

## Complexity

### Target method before/after

- `_open_route_measurement_dialog`
  - brief baseline lizard: `107 NLOC / CCN 13 / length 108`
  - after lizard: `29 NLOC / CCN 7 / length 29`
  - after radon: `B (7)`

### Related `Main` helpers after refactor

- `_restore_route_measurement_state_after_design_load`: lizard `13 NLOC / CCN 6 / length 13`, radon `B (6)`
- `_start_route_measurement_session`: lizard `23 NLOC / CCN 4 / length 23`, radon `A (4)`
- `_cancel_route_measurement_session`: lizard `16 NLOC / CCN 3 / length 16`, radon `A (3)`
- removed from `Main` in fix pass: `_create_route_measurement_dialog`, `_route_dialog_handlers`, `_request_route_measurement_for_point`, `_restart_waiting_route_measurement_start`

### New adapter module

- `route_dialog_handlers`: radon `A (1)`
- `route_dialog_defaults`: radon `A (3)`
- `route_dialog_restore_plan`: radon `A (3)`
- `route_measurement_session_start_plan`: radon `A (3)`
- `route_measurement_session_cancel_plan`: radon `A (2)`
- `route_dialog_open_state`: radon `A (3)`
- `open_or_update_route_measurement_dialog`: lizard `46 NLOC / CCN 2 / length 46`, radon `A (2)`
- `request_route_measurement_for_point`: radon `A (4)`
- `route_measurement_point_request_handler_for_owner`: radon `A (2)`
- `restart_waiting_route_measurement`: lizard/radon `CCN 10`, at threshold but not above warning gate

## Maintainability

### Radon MI

- `main.py`: `C (0.00)`
- `probe_station_gui\route\dialog_adapter.py`: `A (25.62)`

### Wily diff (`-r e2da4bf`)

- `main.py`
  - cyclomatic complexity: `2405 -> 2371`
  - raw.loc: `12313 -> 12159`
  - maintainability.mi: `0 -> 0`
- `probe_station_gui\route\dialog_adapter.py`
  - cyclomatic complexity: `- -> 51`
  - raw.loc: `- -> 454`
  - maintainability.mi: `- -> 25.621876907437407`

Assessment:

- locality and hotspot complexity improved materially
- `main.py` MI stayed flat at the file level because the file remains oversized, but file-level Wily cyclomatic improved by `34` and the target hotspot dropped from warning range
- `dialog_adapter.py` grew more than the first pass because it now owns dialog wiring, point-request control, and waiting-restart orchestration that were previously in `Main`

## Verification

Focused tests:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_dialog_adapter.py tests\app\test_main_coordinate_feedrate.py -q
```

- `137 passed, 3 subtests passed in 1.64s`

Full suite:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
```

- `843 passed, 2 skipped in 10.99s`
- final full suite: `848 passed, 2 skipped in 11.31s`

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

- final `build` succeeded on a clean commit because Wily's git archiver rejects a dirty repository
- the final cache saw only the baseline commit and current commit, which is enough for the requested `diff -r e2da4bf`

## Verdict

- behavior preserved: `yes`
- tests passed: `yes`
- metrics improved: `yes`
- LOC gate passed: `yes`, via authoritative `12313 -> 12159` and fallback minimum `<= 12163`
- maintainability improvement: `yes`, through lower `main.py` LOC/cyclomatic totals and a deeper route adapter seam
- new risk introduced: `low`; the main residual risk is that route dialog behavior now depends on adapter/Main coordination, covered by the new pure tests plus app characterization
