# Task 26 Report: Raw Voltage Sweep Instrument Adapter

## Scope

- Moved raw voltage sweep request parsing, meter-setup orchestration, contact planning, stage/LCR sequencing, and response shaping into `probe_station_gui/instruments/api_sweep.py`.
- Kept `Main` ownership for instrument/controller methods, timestamps, `time.monotonic`, `time.sleep`, logging, and `_api_instrument_exception_response(...)`.
- Added direct instrument-module tests plus minimal `Main` characterization for early-return boundaries and stage side-effect order.

## Physical LOC

- `main.py` physical line count before: `11401`
- `main.py` physical line count after: `11232`
- Delta: `-169`
- Required gate: `<= 11251`
- Target: `<= 11181`
- Verdict: required gate passed, target missed

## Target Method Metrics

### Baseline from task brief

| Method | Lizard NLOC | Lizard CCN | Radon |
| --- | ---: | ---: | --- |
| `Main._api_raw_voltage_sweep` | 148 | 31 | D (30) |
| `Main._api_prepare_route_meter_controller` | 45 | 9 | B (9) |
| `Main._api_route_meter_configuration` | 18 | 2 | B (2) |
| `Main._api_contact_context` | 14 | 2 | B (2) |
| `Main._api_contact_number` | 8 | 1 | A (1) |

### After task

| Method | Lizard NLOC | Lizard CCN | Radon |
| --- | ---: | ---: | --- |
| `Main._api_raw_voltage_sweep` | 21 | 1 | A (1) |
| `Main._api_prepare_route_meter_controller` | 18 | 1 | A (1) |
| `Main._api_route_meter_configuration` | 18 | 2 | A (2) |
| `Main._api_contact_context` | 14 | 2 | A (2) |
| `Main._api_contact_number` | 8 | 1 | A (1) |

### New module hotspots

| Function | Lizard NLOC | Lizard CCN | Radon |
| --- | ---: | ---: | --- |
| `api_raw_voltage_sweep_action` | 105 | 26 | D (25) |
| `api_prepare_route_meter_controller_action` | 46 | 10 | B (10) |
| `api_raw_voltage_sweep_request_from_payload` | 48 | 5 | A (5) |
| `api_raw_voltage_sweep_contact_plan` | 18 | 7 | B (7) |
| `api_configure_meter_action` | 28 | 3 | A (3) |

## Wily Diff From `2f12470`

Command source: fresh disposable clone with a throwaway local commit for the task files only.

| Item | Wily result |
| --- | --- |
| `main.py` cyclomatic complexity | `2229 -> 2190` |
| `main.py` raw.loc | `11401 -> 11232` |
| `main.py` maintainability index | `0 -> 0` |
| `probe_station_gui/instruments/api_sweep.py` cyclomatic complexity | `- -> 65` |
| `probe_station_gui/instruments/api_sweep.py` raw.loc | `- -> 379` |
| `probe_station_gui/instruments/api_sweep.py` maintainability index | `- -> 25.14616921972019` |
| `Main._api_prepare_route_meter_controller` CCN | `9 -> 1` |
| `Main._api_configure_meter` CCN | `3 -> 1` |
| `Main._api_raw_voltage_sweep` CCN | `30 -> 1` |

Notes:

- Wily completed successfully from the disposable clone.
- Wily emitted `No data collected` warnings for older revisions where the target paths did not exist yet, but still produced the requested diff.
- Wily `raw.loc` matches the direct `splitlines()` count for `main.py` in this final state (`11401 -> 11232`).

## Lizard / Radon Spot Checks

Commands run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m lizard main.py probe_station_gui\instruments\api_sweep.py -l python -C 10 -L 50 --sort cyclomatic_complexity
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon cc -s main.py probe_station_gui\instruments\api_sweep.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon mi -s main.py probe_station_gui\instruments\api_sweep.py
```

Key outputs:

- `main.py`: radon MI `C (0.00)`
- `probe_station_gui/instruments/api_sweep.py`: radon MI `A (25.15)`
- `Main._api_prepare_route_meter_controller`: lizard `18 / 1`, radon `A (1)`
- `Main._api_raw_voltage_sweep`: lizard `21 / 1`, radon `A (1)`
- `api_raw_voltage_sweep_action`: lizard `105 / 26`, radon `D (25)`

## Tests and Ruff

TDD RED/GREEN evidence:

1. RED:
   - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\instruments\test_api_sweep.py -q`
   - Result: failed with `ModuleNotFoundError: No module named 'probe_station_gui.instruments.api_sweep'`
2. GREEN:
   - same command after initial module extraction
   - Result: `12 passed in 0.12s`
3. RED:
   - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\instruments\test_api_sweep.py -q`
   - Result: failed with `ImportError: cannot import name 'api_configure_meter_action'`
4. GREEN:
   - same command after orchestration extraction
   - Result: `15 passed in 0.13s`
5. Focused integration:
   - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\instruments\test_api_sweep.py tests\app\test_main_coordinate_feedrate.py -q`
   - Result: `152 passed, 3 subtests passed in 2.23s`

Final verification:

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - Result: `940 passed, 2 skipped in 11.24s`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - Result: `All checks passed!`

## Behavior Preservation Summary

- `voltages_v` validation, conversion, and error/status shapes are preserved.
- Meter setup still returns before any stage side effects.
- Contact-context rejection still returns unchanged.
- Raw sweep stage side-effect order remains: begin, optional lift-before-move, move, lower, optional settle sleep, read, optional lift-after, finish.
- `lift_after=False` still leaves needles down and reports `lifted_after=False`.
- Unexpected instrument exceptions still route through `_api_instrument_exception_response(...)`.

## Verdict

- Behavior preserved: `yes`
- Tests passed: `yes`
- Metrics improved: `yes`
- LOC gate passed: `yes`
- LOC target passed: `no`
- Maintainability improvement:
  - raw-sweep instrument policy now lives in a dedicated module with direct unit coverage;
  - `Main._api_prepare_route_meter_controller` dropped from `B (9)` to `A (1)`;
  - `Main._api_raw_voltage_sweep` dropped from `D (30)` to `A (1)`;
  - `main.py` raw LOC dropped by `169`, which clears the required task gate.
- New risk introduced:
  - Moderate. `api_raw_voltage_sweep_action` is now the main hotspot at `D (25)` / `105` NLOC, so future work in this area should likely split request/setup/execution/finalization into another layer once the refactor series reaches this module.
