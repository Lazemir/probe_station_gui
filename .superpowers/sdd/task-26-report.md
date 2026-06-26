# Task 26 Report: Raw Voltage Sweep Instrument Adapter

## Scope

- Kept `Main` ownership for:
  - `_api_prepare_route_meter_controller(...)`
  - `_api_configure_meter(...)`
  - raw voltage sweep stage/LCR orchestration, timing, cleanup, logging, and `_api_instrument_exception_response(...)`
- Kept `probe_station_gui/instruments/api_sweep.py` limited to:
  - raw-sweep request parsing
  - contact planning from an already-resolved context response
  - meter payload extraction
  - success/error response shaping
  - IV-pair extraction
- Added regression coverage for:
  - non-dict contact passthrough
  - final-lift `StageControllerError` logging + `finish_external_task()`
  - final-lift `LCRMeterError` not being swallowed

## Review Rework

Review-driven corrections applied:

1. Removed the public `api_prepare_route_meter_controller_action`, `api_configure_meter_action`, and `api_raw_voltage_sweep_action` orchestration helpers from `api_sweep.py`.
2. Restored wait/connect/runtime-config/apply flow to `Main._api_prepare_route_meter_controller(...)`.
3. Restored configure-meter flow to `Main._api_configure_meter(...)`.
4. Restored raw-sweep side-effect orchestration to `Main._api_raw_voltage_sweep(...)`.
5. Changed `api_raw_voltage_sweep_contact_plan(...)` to pass `context_result["contact"]` through unchanged.
6. Restored final-lift swallowing/logging to `StageControllerError` only.

## Physical LOC

- `main.py` physical line count before: `11401`
- `main.py` physical line count after: `11356`
- Delta: `-45`
- Required gate: `<= 11251`
- Target: `<= 11181`
- Verdict: required gate missed after ownership-correct rework

## Target Method Metrics

### Baseline from task brief

| Method | Lizard NLOC | Lizard CCN | Radon |
| --- | ---: | ---: | --- |
| `Main._api_raw_voltage_sweep` | 148 | 31 | D (30) |
| `Main._api_prepare_route_meter_controller` | 45 | 9 | B (9) |
| `Main._api_route_meter_configuration` | 18 | 2 | B (2) |
| `Main._api_contact_context` | 14 | 2 | B (2) |
| `Main._api_contact_number` | 8 | 1 | A (1) |

### Final state after rework

| Method | Lizard NLOC | Lizard CCN | Radon |
| --- | ---: | ---: | --- |
| `Main._api_raw_voltage_sweep` | 96 | 23 | D (22) |
| `Main._api_prepare_route_meter_controller` | 45 | 9 | B (9) |
| `Main._api_route_meter_configuration` | 18 | 2 | A (2) |
| `Main._api_contact_context` | 14 | 2 | A (2) |
| `Main._api_contact_number` | 8 | 1 | A (1) |

### New module hotspots

| Function | Lizard NLOC | Lizard CCN | Radon |
| --- | ---: | ---: | --- |
| `api_raw_voltage_sweep_request_from_payload` | 48 | 5 | A (5) |
| `api_raw_voltage_sweep_contact_plan` | 17 | 6 | B (6) |
| `api_raw_voltage_sweep_success_response` | 26 | 2 | A (2) |
| `_api_raw_voltage_sweep_iv_pairs` | 14 | 4 | A (4) |

## Wily Diff From `2f12470`

Command source: fresh disposable clone with a throwaway local commit for `main.py` and `probe_station_gui/instruments/api_sweep.py` only.

| Item | Wily result |
| --- | --- |
| `main.py` cyclomatic complexity | `2229 -> 2221` |
| `main.py` raw.loc | `11401 -> 11356` |
| `main.py` maintainability index | `0 -> 0` |
| `probe_station_gui/instruments/api_sweep.py` cyclomatic complexity | `- -> 26` |
| `probe_station_gui/instruments/api_sweep.py` raw.loc | `- -> 188` |
| `probe_station_gui/instruments/api_sweep.py` maintainability index | `- -> 40.37178904665695` |
| `Main._api_raw_voltage_sweep` CCN | `30 -> 22` |

Notes:

- Wily completed successfully from the disposable clone.
- Wily emitted `No data collected` warnings for older revisions where the target path did not exist yet, but still produced the requested diff.
- Wily `raw.loc` matches the direct `splitlines()` count for `main.py` in this final state (`11401 -> 11356`).

## Lizard / Radon Spot Checks

Commands run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m lizard main.py probe_station_gui\instruments\api_sweep.py -l python -C 10 -L 50 --sort cyclomatic_complexity
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon cc -s main.py probe_station_gui\instruments\api_sweep.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon mi -s main.py probe_station_gui\instruments\api_sweep.py
```

Key outputs:

- `main.py`: radon MI `C (0.00)`
- `probe_station_gui/instruments/api_sweep.py`: radon MI `A (40.37)`
- `Main._api_raw_voltage_sweep`: lizard `96 / 23`, radon `D (22)`
- `Main._api_prepare_route_meter_controller`: lizard `45 / 9`, radon `B (9)`
- `api_raw_voltage_sweep_request_from_payload`: lizard `48 / 5`, radon `A (5)`
- `api_raw_voltage_sweep_contact_plan`: lizard `17 / 6`, radon `B (6)`

## Tests and Ruff

Original Task 26 RED/GREEN evidence:

1. RED:
   - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\instruments\test_api_sweep.py -q`
   - Result: failed with `ModuleNotFoundError: No module named 'probe_station_gui.instruments.api_sweep'`
2. GREEN:
   - same command after initial extraction
   - Result: `12 passed in 0.12s`

Review rework RED/GREEN evidence:

1. RED:
   - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\instruments\test_api_sweep.py tests\app\test_main_coordinate_feedrate.py -q`
   - Result:
     - `test_contact_plan_preserves_non_dict_contact_payload` failed because contact was coerced to `None`
     - `test_api_raw_voltage_sweep_final_lift_lcr_error_is_not_swallowed` failed because `LCRMeterError` was swallowed and only logged
2. GREEN:
   - same command after boundary rework
   - Result: `152 passed, 3 subtests passed in 2.33s`

Final verification:

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - Result: `940 passed, 2 skipped in 11.86s`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - Result: `All checks passed!`

## Behavior Preservation Summary

- `voltages_v` validation, conversion, and error/status shapes are preserved.
- Meter setup still returns before any stage side effects.
- Contact-context rejection still returns unchanged.
- Raw sweep stage side-effect order remains: begin, optional lift-before-move, move, lower, optional settle sleep, read, optional lift-after, finish.
- `lift_after=False` still leaves needles down and reports `lifted_after=False`.
- Final-lift `StageControllerError` is logged and still calls `finish_external_task()`.
- Final-lift `LCRMeterError` is no longer swallowed.
- Contact payload from `context_result["contact"]` is passed through unchanged, including non-dict values.
- Unexpected instrument exceptions still route through `_api_instrument_exception_response(...)`.

## Verdict

- Behavior preserved: `yes`
- Tests passed: `yes`
- Metrics improved: `partially`
- LOC gate passed: `no`
- Maintainability improvement:
  - the accepted extraction is now limited to parsing, contact planning, and response formatting, which matches the task ownership boundary;
  - `api_sweep.py` has direct unit coverage for the extracted policy;
  - `Main._api_raw_voltage_sweep` complexity still drops versus baseline (`30 -> 22` radon / `31 -> 23` lizard CCN), but most orchestration remains in `Main` as required.
- New risk introduced:
  - Low to moderate. The ownership boundary is now correct, but `Main._api_raw_voltage_sweep` remains a large orchestrator and is still a hotspot for future refactor tasks.
