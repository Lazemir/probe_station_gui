# Task 37 Brief: API Command Dispatch Module

Baseline commit: `299825c refactor: split main window construction`

## Current Behavior

- `Main._submit_api_command_request_from_api_thread(...)` decides whether an API command must be routed back through the GUI thread before execution.
- `api_route_control_status` and `api_route_control_action` always use the GUI-thread bridge.
- Probe-route commands such as `move_to_contact`, `contact_needles`, `route_contact_focus`, `route_contact_photo`, `contact_seek`, route-session actions, and route artifacts must pass the route-control-window guard before direct execution from the API thread.
- `Main._dispatch_api_command_request(...)` normalizes `action`/`payload`, applies the optional route-control-window guard, dispatches every supported command action to the existing `Main` handler, and returns the existing unsupported-action payload.
- `Main._handle_api_request(...)` keeps the bridge-facing actions stable: `move_to_coordinates`, `status`, `command`, and `probe_route_window_guard`.

## Structural Improvement

- Add a deep `probe_station_gui.api.command_dispatch` module that owns API command parsing, GUI-thread routing decisions, supported-command routing, and unsupported-command responses.
- Keep `Main` as the adapter that supplies bound handler callables, the Qt bridge submit functions, and the status/UI guard side effects.
- Keep public method names on `Main` stable as thin wrappers so existing tests and local callers do not break.
- Do not move route measurement, stage, instrument, or VISA behavior in this pass; only move API command dispatch policy.

## Baseline Metrics

- `main.py` raw LOC: `8999` (Wily from Task 36)
- Wily `main.py` cyclomatic: `1765`
- Wily `main.py` MI: `0`
- Lizard hotspots:
  - `Main._dispatch_api_command_request`: `56` NLOC / CCN `23`
  - `Main._submit_api_command_request_from_api_thread`: `19` NLOC / CCN `4`
  - `Main._handle_api_request`: `39` NLOC / CCN `8`

## Target

- `Main._dispatch_api_command_request` becomes a thin delegate and drops below lizard warning thresholds.
- API command routing policy is covered by module-level tests that do not instantiate Qt widgets.
- Existing app/API tests covering route-control-window guards and GUI-thread bridge behavior still pass.
- `main.py` Wily cyclomatic decreases; raw LOC may decrease modestly, but success is primarily the removed dispatch hotspot and improved locality.

## Validation

- Add/adjust focused tests for:
  - action/payload normalization for missing or non-dict payloads;
  - supported command dispatch to the correct handler with/without payload;
  - unsupported command response parity;
  - GUI-thread command routing for API Route Control actions;
  - route-control-window guard-before-direct-dispatch behavior.
- Run:
  - `.\.venv\Scripts\python.exe -m pytest tests\api tests\app\test_main_coordinate_feedrate.py`
  - `.\.venv\Scripts\python.exe -m pytest tests`
  - `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - Wily/radon/lizard before/after spot checks for `main.py` and the new API module.

## Risks

- Do not accidentally broaden or shrink the route-window-required action set.
- Do not route in-process GUI behavior through localhost; this pass only preserves the existing local API bridge semantics for external API requests.
- Avoid a giant callback object with unclear ownership. Handler mapping should be explicit and local to the command dispatch module interface.
