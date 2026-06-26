# Task 25 Report: API Current Contact Measurement Module

## Scope

- Refactored current-contact API parsing/response policy out of `main.py` into `probe_station_gui/route/api_measurement.py`.
- Kept `Main` ownership for instrument connection, runner construction, check/seek execution, logging, Telegram sending, and timestamps.
- Added dedicated route-module tests and minimal `Main` characterization tests.

## Physical LOC

- `main.py` physical line count before: `11577`
- `main.py` physical line count after: `11401`
- Delta: `-176`
- Required gate: `<= 11427`
- Target: `<= 11327`
- Verdict: required gate passed, target missed

## Target Method Metrics

### Baseline from task brief

| Method | Lizard NLOC | Lizard CCN | Radon |
| --- | ---: | ---: | --- |
| `Main._api_measure_current_contact` | 185 | 18 | C (18) |
| `Main._api_contact_context` | 44 | 9 | B (9) |
| `Main._api_contact_number` | 15 | 6 | B (6) |
| `Main._api_check_contact` | 2 | 1 | A (1) |
| `Main._api_contact_seek` | 2 | 1 | A (1) |

### After task

| Method | Lizard NLOC | Lizard CCN | Radon |
| --- | ---: | ---: | --- |
| `Main._api_measure_current_contact` | 97 | 11 | C (11) |
| `Main._api_contact_context` | 14 | 2 | A (2) |
| `Main._api_contact_number` | 8 | 1 | A (1) |
| `Main._api_check_contact` | 2 | 1 | A (1) |
| `Main._api_contact_seek` | 2 | 1 | A (1) |

### New module hotspots

| Function | Lizard NLOC | Lizard CCN | Radon |
| --- | ---: | ---: | --- |
| `api_contact_context_response` | 48 | 8 | B (8) |
| `api_current_contact_settings_from_payload` | 51 | 1 | A (1) |

## Wily Diff From `3ca2ab8`

Command source: fresh disposable clone after commit, `wily diff ... -r 3ca2ab8`.

| Item | Wily result |
| --- | --- |
| `main.py` cyclomatic complexity | `2250 -> 2229` |
| `main.py` raw.loc | `11577 -> 11401` |
| `main.py` maintainability index | `0 -> 0` |
| `probe_station_gui/route/api_measurement.py` cyclomatic complexity | `- -> 28` |
| `probe_station_gui/route/api_measurement.py` raw.loc | `- -> 300` |
| `probe_station_gui/route/api_measurement.py` maintainability index | `- -> 37.89947022002114` |
| `Main._api_measure_current_contact` CCN | `18 -> 11` |
| `Main._api_contact_context` CCN | `9 -> 2` |
| `Main._api_contact_number` CCN | `6 -> 1` |

Notes:

- Wily completed successfully from the disposable clone.
- Wily emitted `No data collected` warnings for older revisions where the target paths did not exist yet, but still produced the requested diff.
- Wily `raw.loc` matches the physical `splitlines()` count for `main.py` in this task (`11577 -> 11401`).

## Lizard / Radon Spot Checks

Commands run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m lizard main.py probe_station_gui\route\api_measurement.py -l python -C 10 -L 50 --sort cyclomatic_complexity
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon cc -s main.py probe_station_gui\route\api_measurement.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon mi -s main.py probe_station_gui\route\api_measurement.py
```

Key outputs:

- `main.py`: lizard file summary `NLOC 10781`, avg CCN `4.4`
- `probe_station_gui/route/api_measurement.py`: lizard file summary `NLOC 275`, avg CCN `3.2`
- `main.py`: radon MI `C (0.00)`
- `probe_station_gui/route/api_measurement.py`: radon MI `A (37.90)`

## Tests and Ruff

Focused RED/GREEN cycle:

1. RED:
   - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_api_measurement.py -q`
   - Result: failed with `ModuleNotFoundError: No module named 'probe_station_gui.route.api_measurement'`
2. GREEN:
   - Same command after implementation
   - Result: `22 passed in 0.14s`
3. Focused integration:
   - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_api_measurement.py tests\app\test_main_coordinate_feedrate.py -q`
   - Result: `155 passed, 3 subtests passed in 2.52s`

Final verification:

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - Result: `914 passed, 2 skipped in 11.73s`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - Result: `All checks passed!`

## Behavior Preservation Summary

- Contact number alias order and invalid/non-positive handling preserved.
- Contact-context guard order, status codes, and messages preserved.
- Instrument connection failure still appends the resolved contact payload.
- Current-contact settings parsing preserves aliases, defaults, and parser error messages.
- Runner construction remains in `Main` with the existing dependency and callback wiring.
- Check/seek success response payloads remain stable.
- Seek failure Telegram alert condition, channel, text, photo flag, and markup are preserved.

## Verdict

- Behavior preserved: `yes`
- Tests passed: `yes`
- Metrics improved: `yes`
- LOC gate passed: `yes`
- LOC target passed: `no`
- Maintainability improvement:
  - current-contact policy moved into a dedicated route module with direct unit coverage;
  - `Main._api_measure_current_contact` complexity dropped from `18` to `11`;
  - `Main._api_contact_context` dropped from `9` to `2`;
  - `Main._api_contact_number` dropped from `6` to `1`.
- Remaining line-reduction rationale:
  - Task 25 already moved the owned current-contact parsing, contact-context decision logic, measurement-point payload formatting, response shaping, and seek-failure alert policy into `probe_station_gui.route.api_measurement`.
  - The remaining body in `Main` is mostly side-effect ownership that the brief explicitly kept in `Main`: instrument connection, `RouteMeasurementRunner` construction with `stage_controller`/`lcr_controller`/status signal wiring, execution of `check_contact` or `seek_contact`, exception logging, Telegram sending, and timestamp generation.
  - Pushing `main.py` below `11327` from the current `11401` would require moving more of that side-effect code or adjacent route/stage API flow that the brief marked for later tasks, so I did not make a scope-expanding follow-up reduction here.
- New risk introduced:
  - Low. The API contract is now split between `Main` side effects and `probe_station_gui.route.api_measurement` policy helpers, so future edits need to keep both the route tests and the `Main` characterization tests in sync.
