# Task 37 Report: API Command Dispatch Module

Status: DONE

## Summary

- Added `probe_station_gui.api.command_dispatch` to own API command normalization, unsupported responses, supported command routing, API-thread versus GUI-thread routing policy, and bridge-facing API request handling.
- Kept `Main` method names stable as thin adapters:
  - `_submit_api_command_request_from_api_thread`
  - `_api_command_action_payload`
  - `_dispatch_api_command_request`
  - `_handle_api_request`
- Left route, stage, instrument, VISA, and measurement behavior in `main.py`; only dispatch policy moved.

## Behavior Parity Notes

- `api_route_control_status` and `api_route_control_action` still route through `_submit_api_command_request_on_gui_thread`.
- Probe-route commands that require an open route-control window still submit `probe_route_window_guard` on the GUI thread before direct API-thread dispatch.
- If the GUI-thread route-window guard rejects, that response is returned without direct dispatch.
- Accepted route-window guards still dispatch directly with `apply_route_control_guard=False`.
- Bridge-facing actions remain `move_to_coordinates`, `status`, `command`, and `probe_route_window_guard`.
- Unsupported command/action response payloads are unchanged.

## Tests Run

- `.\.venv\Scripts\python.exe -m pytest tests\api\test_command_dispatch.py`
  - Expected RED before implementation: failed during collection with `ModuleNotFoundError: No module named 'probe_station_gui.api.command_dispatch'`.
- `.\.venv\Scripts\python.exe -m pytest tests\api\test_command_dispatch.py tests\app\test_main_coordinate_feedrate.py`
  - Initial worker result: `170 passed in 2.19s`.
  - Final controller result after review fixes: `190 passed in 2.09s`.
- `.\.venv\Scripts\python.exe -m pytest tests\api tests\app\test_main_coordinate_feedrate.py`
  - Result: `218 passed in 3.54s`.
- `.\.venv\Scripts\python.exe -m ruff check main.py probe_station_gui\api\command_dispatch.py tests\api\test_command_dispatch.py`
  - Result: failed on pre-existing `main.py` `E402`/`F401` import-layout and unused-import findings.
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 main.py probe_station_gui\api\command_dispatch.py tests\api\test_command_dispatch.py`
  - Result: `All checks passed!`
- `.\.venv\Scripts\python.exe -m pytest tests`
  - Initial worker result: `1121 passed, 2 skipped in 11.27s`.
  - Final controller result after review fixes: `1141 passed, 2 skipped in 11.22s`.
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - Result: `All checks passed!`

## Metrics Spot Checks

- `.\.venv\Scripts\python.exe -m lizard main.py probe_station_gui\api\command_dispatch.py`
  - Result: exited `1` because unrelated existing functions still exceed lizard warning thresholds.
  - Relevant Task 37 `main.py` methods:
    - `_submit_api_command_request_from_api_thread`: `13` NLOC / CCN `1`.
    - `_dispatch_api_command_request`: `11` NLOC / CCN `1`.
    - `_handle_api_request`: `10` NLOC / CCN `1`.
  - New module functions:
    - `dispatch_api_command_request`: `40` NLOC / CCN `5`.
    - `submit_api_command_request_from_api_thread`: `17` NLOC / CCN `4`.
    - `handle_api_request`: `35` NLOC / CCN `8`.
  - File spot check: `main.py` reported `8400` NLOC in this run.
- Wily final from disposable UTF-8 temp clone/cache, comparing temp Task 37 commit against `299825c`:
  - `main.py`: MI `0 -> 0`, cyclomatic `1765 -> 1733`, raw LOC `8999 -> 8948`.
  - `probe_station_gui/api/command_dispatch.py`: new MI `35.3768`, cyclomatic `29`, raw LOC `218`.
  - `tests/api/test_command_dispatch.py`: new MI `31.3819`, cyclomatic `17`, raw LOC `315`.
  - Function detail:
    - `Main._submit_api_command_request_from_api_thread`: CC `4 -> 1`.
    - `Main._api_command_action_payload`: CC `2 -> 1`.
    - `Main._dispatch_api_command_request`: CC `23 -> 1`.
    - `Main._handle_api_request`: CC `8 -> 1`.

## Reviews

- Spec review approved with no behavior/scope findings.
- Code-quality review found two issues:
  - dispatch-table tests only covered two actions;
  - two callback aliases used `Callable[..., ApiResponse]`.
- Follow-up fixes:
  - Added parameterized tests for every supported no-payload and payload command action, plus explicit `api_route_control_status` GUI-thread bridge coverage.
  - Replaced broad callback aliases with precise `Protocol` interfaces for direct command dispatch and coordinate move handling.
- Code-quality re-review found no remaining issues.

## Verdict

This refactor is justified.

- Behavior preserved: yes.
- Tests passed: yes.
- Metrics improved: yes.
- Maintainability improvement: API command parsing/routing and GUI-thread guard sequencing now live in a focused API dispatch module; the `Main` dispatch hotspot dropped from CC `23` to `1`, and `main.py` Wily cyclomatic dropped by `32`.
- New risk introduced: low. The supported-action test lists intentionally duplicate the dispatch table, so future action additions must update both implementation and test lists.

## Files Changed

- `main.py`
- `probe_station_gui/api/command_dispatch.py`
- `tests/api/test_command_dispatch.py`
- `.superpowers/sdd/task-37-brief.md`
- `.superpowers/sdd/task-37-report.md`
