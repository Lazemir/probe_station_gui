# Task 17 Report

## Files changed
- `C:\Users\Public\code\probe_station_gui\main.py`
- `C:\Users\Public\code\probe_station_gui\probe_station_gui\route\adjustment_flow.py`
- `C:\Users\Public\code\probe_station_gui\probe_station_gui\route\confirmation_flow.py`
- `C:\Users\Public\code\probe_station_gui\tests\app\test_main_coordinate_feedrate.py`
- `C:\Users\Public\code\probe_station_gui\tests\route\test_adjustment_flow.py`
- `C:\Users\Public\code\probe_station_gui\tests\route\test_confirmation_flow.py`

## Behavior-preservation notes
- `Main._submit_route_measurement_confirmation(...)` still routes API Route Control confirmations before GUI-runner handling.
- Guard messages and timeouts are unchanged: `"No route measurement is waiting."`, `"Wait for route contact move to finish."`, and `"Unknown route measurement action."`
- Waiting `next`/`resume`/`continue` confirmation rewriting still resolves to `jump:{point}` when a pending point exists.
- Waiting GUI-runner restart behavior still uses `runner.route_offset_xy()` and only happens for changed setup on waiting non-external runs.
- Runtime configuration persistence, session metadata save, runner runtime-setting updates, meter setup gating, waiting-state clear, and final status text remain in `Main`.
- Confirmation-time `LCRMeterError` behavior is preserved: show `"Route measurement instrument setup failed: ..."` in the status bar and dialog, then do not submit the confirmation.
- Route Pause Request / Pause Ack / Interrupt semantics were not changed.

## Tests and checks run
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route tests\app\test_main_coordinate_feedrate.py`
  - `319 passed in 3.95s`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - `804 passed, 2 skipped in 11.07s`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - `All checks passed!`

## Metrics
- Wily first:
  - Direct `wily build` on the live workspace failed because Wily refuses a dirty repository: `Failed to setup archiver: 'Dirty repository, make sure you commit/stash files first'`.
  - Per brief, I retried with the stable Windows UTF-8 invocation in a temporary side worktree built from `6a38419` plus this task's code changes:
    - `$env:PYTHONIOENCODING='utf-8'`
    - `$env:PYTHONUTF8='1'`
    - `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`
    - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily ...`
  - Wily diff result for `main.py` vs `6a38419`:
    - cyclomatic `2390 -> 2393`
    - LOC `12165 -> 12165`
    - MI `0 -> 0`
    - `_submit_route_measurement_confirmation` cyclomatic `17 -> 11`
    - new `_apply_route_measurement_confirmation_runtime` cyclomatic `9`
- Radon spot checks on the live workspace:
  - `main.py:Main._submit_route_measurement_confirmation`: `C (11)`
  - `probe_station_gui/route/confirmation_flow.py`: `A (50.86)` MI
  - `main.py`: `C (0.00)` MI
- Lizard spot checks on the live workspace:
  - `main.py:Main._submit_route_measurement_confirmation`: `37 NLOC / CCN 11`
  - `main.py + adjustment_flow.py + runtime_settings.py + confirmation_flow.py` warning count: `19`
  - `_submit_route_measurement_confirmation` no longer appears in the warning list.

Metric                        Before            After             Better?
Target function LOC           78                37                yes
Target function CC            17                11                yes
main.py Wily cyclomatic       2390              2393              no
main.py Wily LOC              12165             12165             no
Lizard warning count          20                19                yes
Route confirmation tests      existing partial  direct module + app regression yes
Public API changed            no                no                yes

## Verdict
- The refactor is justified for the target hotspot. `Main._submit_route_measurement_confirmation` is materially shorter and lower-complexity, it dropped out of the lizard warning list, and the confirmation/runtime decision table now has direct route-module coverage in `probe_station_gui.route.confirmation_flow`.
- File-level Wily cyclomatic for `main.py` increased slightly because the extracted side-effect adapter remains on `Main` as a new helper. That tradeoff is local and visible: the target method dropped from `17` to `11`, the helper is `9`, and the combined route-confirmation behavior stayed covered by direct route tests plus the app regression for confirmation-time instrument setup failure.

## Commit hash
- Final hash is recorded in git after the report write; keeping the report content stable avoids a self-reference churn loop.
