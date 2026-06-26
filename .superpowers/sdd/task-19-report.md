# Task 19 Report: Thin GUI Route Measurement Start

## Files changed

- `main.py`
- `probe_station_gui/route/session_start.py`
- `tests/app/test_main_coordinate_feedrate.py`
- `tests/route/test_session_start.py`
- `.superpowers/sdd/task-19-report.md`

## Behavior parity notes

- Preserved the existing GUI route start order: active-thread guard, serial guard, route start plan selection, resume/session pending state, session metadata save, photo/autofocus preflight, meter setup, runner creation, UI status updates, Telegram start alert, thread start, and coordinate apply refresh.
- Moved pure GUI route start decisions into `probe_station_gui.route.session_start`: active/serial availability, photo/autofocus preflight, camera-frame failure text, and launch-state presentation.
- Kept `Main` as the side-effect adapter for status/dialog updates, Telegram alerts, meter setup, runner construction, and thread start.
- Did not change Pause Request / Pause Ack / Interrupt code paths or route-control semantics.
- Did not route any in-process GUI path through the localhost API.

## TDD and characterization

- Added route-module tests first for the new pure helpers. Initial red run:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_session_start.py -q`
  - Result: 4 expected assertion failures because `gui_route_start_availability`, `gui_route_start_preflight`, `gui_route_camera_frame_preflight`, and `gui_route_launch_state` did not exist yet.
- Added app characterization coverage for:
  - active route thread rejection status and `4000` timeout;
  - disconnected serial rejection status and `5000` timeout;
  - photo route without objective scale avoiding meter configuration and thread start;
  - immediate photo route without frame status/dialog/Telegram failure behavior;
  - immediate autofocus route without frame status/dialog/Telegram failure behavior;
  - waiting start not checking camera and not sending route-start Telegram;
  - photo-only route using a dummy LCR controller and not configuring the meter;
  - meter setup `LCRMeterError` clearing dialog running state and reporting status.

## Checks run

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_session_start.py -q`
  - Red before implementation: 13 passed, 4 failed as expected.
  - Green after implementation: 17 passed in 0.13s.
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_session_start.py tests\app\test_main_coordinate_feedrate.py -q`
  - 133 passed in 1.69s.
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route tests\app\test_main_coordinate_feedrate.py`
  - 341 passed in 3.73s.
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - 826 passed, 2 skipped in 10.72s.
- Follow-up cleanup after task review:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_session_start.py tests\app\test_main_coordinate_feedrate.py -q`
  - 133 passed in 1.87s.
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - 826 passed, 2 skipped in 11.22s.
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - All checks passed.
- `$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m lizard main.py probe_station_gui\route -l python -C 10 -L 50 --sort cyclomatic_complexity`
  - Exit code 1 due existing threshold warnings; metrics collected.
- `$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon cc -s main.py probe_station_gui\route`
  - Exit code 0; metrics collected.
- `$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $cache=Join-Path $env:TEMP 'probe_station_gui_wily_task19'; C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c $cache build main.py probe_station_gui\route`
  - Exit code 0; build completed with historical "No data collected" warnings for some revisions.
- `$env:PYTHONIOENCODING='utf-8'; $env:PYTHONUTF8='1'; [Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $cache=Join-Path $env:TEMP 'probe_station_gui_wily_task19'; C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c $cache diff main.py probe_station_gui\route --detail -r 1ec14ef --metrics cyclomatic.complexity,raw.loc,maintainability.mi --no-wrap`
  - Exit code 0; metrics collected.

## Review follow-up

- First task review found no blocking behavior issue, but noted that `_build_route_measurement_runner` introduced a new lizard length warning.
- Follow-up cleanup split runner callback wiring into `_route_measurement_runner_callbacks` and reused `_stage_serial_ready()` for the GUI serial guard.
- Follow-up task review found no blocking findings and no `git diff --check` issue.

## Metrics

### Target method

- Lizard before: `Main._start_route_measurement` 175 NLOC, CCN 23, length 190.
- Lizard after: `Main._start_route_measurement` 50 NLOC, CCN 6, length 50.
- Radon before: `Main._start_route_measurement` D (23).
- Radon after: `Main._start_route_measurement` B (6).
- Wily detail: `main.py:Main._start_route_measurement` cyclomatic complexity 23 -> 6.

### New extracted adapters/helpers

- Lizard after:
  - `Main._apply_gui_route_start_preflight`: 16 NLOC, CCN 5.
  - `Main._route_measurement_photo_preflight`: 25 NLOC, CCN 3.
  - `Main._route_measurement_lcr_controller`: 25 NLOC, CCN 5.
  - `Main._build_route_measurement_runner`: 34 NLOC, CCN 1.
  - `Main._route_measurement_runner_callbacks`: 33 NLOC, CCN 1.
  - `Main._start_route_measurement_runner`: 49 NLOC, CCN 4.
  - `gui_route_start_availability`: 18 NLOC, CCN 3.
  - `gui_route_start_preflight`: 23 NLOC, CCN 5.
  - `gui_route_camera_frame_preflight`: 21 NLOC, CCN 5.
  - `gui_route_launch_state`: 19 NLOC, CCN 1.
- Radon after:
  - `Main._apply_gui_route_start_preflight`: A (5).
  - `Main._route_measurement_lcr_controller`: A (5).
  - `Main._start_route_measurement_runner`: A (4).
  - `Main._route_measurement_photo_preflight`: A (3).
  - `Main._build_route_measurement_runner`: A (1).
  - `Main._route_measurement_runner_callbacks`: A (1).
  - `gui_route_start_preflight`: A (5).
  - `gui_route_camera_frame_preflight`: A (5).
  - `gui_route_start_availability`: A (3).
  - `gui_route_launch_state`: A (1).

### Aggregate lizard

- Before:
  - `main.py`: NLOC 11592, AvgCCN 4.7, function count 518.
  - `probe_station_gui\route\session_start.py`: NLOC 394, AvgCCN 2.5, function count 13.
  - Total for `main.py probe_station_gui\route`: NLOC 19742, AvgCCN 4.0, function count 942, warning count 82.
- After:
  - `main.py`: NLOC 11662, AvgCCN 4.6, function count 525.
  - `probe_station_gui\route\session_start.py`: NLOC 500, AvgCCN 2.8, function count 17.
  - Total for `main.py probe_station_gui\route`: NLOC 19918, AvgCCN 4.0, function count 953, warning count 81.

### Wily diff against `1ec14ef`

- `main.py`: Cyclomatic Complexity 2400 -> 2404; Lines of Code 12218 -> 12294; Maintainability Index 0 -> 0.
- `probe_station_gui\route\session_start.py`: Cyclomatic Complexity 40 -> 56; Lines of Code 439 -> 557; Maintainability Index 31.2629 -> 26.1821.
- Function detail:
  - `main.py:Main._start_route_measurement`: 23 -> 6.
  - New `Main` adapters: 5, 5, 4, 3, 2, 1, 1.
  - New route helpers: 5, 5, 3, 1.

## Verdict and residual risks

- Verdict: Accepted. The primary target is substantially thinner and less complex while route start behavior is pinned by focused route/app tests.
- Residual risk: the app characterization uses fakes and does not exercise real Qt signal delivery, camera hardware, serial hardware, or physical meter I/O. Full hardware behavior remains covered by preserving the existing side-effect adapters and avoiding hardware-dependent automation.
- Residual metric note: aggregate route-module LOC and Wily file-level complexity increased because pure decision helpers and tests were added; the targeted GUI method complexity dropped materially, and the lizard warning count for `main.py probe_station_gui\route` improved 82 -> 81 after the follow-up cleanup.
