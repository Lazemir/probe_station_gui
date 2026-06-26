# Task 28 Report: API Move-To-Contact Planning

## Scope

- `main.py`
- `probe_station_gui/design/contact_navigation.py`
- `tests/design/test_contact_navigation.py`
- `tests/app/test_main_coordinate_feedrate.py`

No other production files were changed.

## main.py physical LOC

- Before (`df5be0e`, Python `splitlines()`): `11195`
- After (current working tree, Python `splitlines()`): `11004`
- Delta: `-191`

LOC gate result: `11004 <= 11025` -> passed.

The earlier report version incorrectly mixed the brief baseline with a PowerShell `Measure-Object` count. This report uses Python `splitlines()` only.

## Target method metrics

### `Main._api_move_to_contact`

- Before lizard: `95 NLOC / CCN 14` (brief baseline)
- Before radon: `C (13)` (brief baseline)
- After lizard: `56 NLOC / CCN 11`
- After radon: `B (10)`

### `Main._api_contact_needles`

- Before lizard: `49 NLOC / CCN 9` (baseline lizard run against `df5be0e`)
- Before radon: `B (8)` (Wily diff against `df5be0e`)
- After lizard: `30 NLOC / CCN 5`
- After radon: `A (4)`

### New module spot check

`probe_station_gui/design/contact_navigation.py` stays shallow:

- `api_move_to_contact_request`: radon `A (2)`
- `api_move_to_contact_plan`: radon `A (3)`
- `api_move_to_contact_success_response`: radon `A (3)`
- `api_contact_needles_request`: radon `A (3)`
- `api_contact_needles_plan`: radon `A (3)`
- `api_contact_context`: radon `A (1)`
- `api_route_point_payload`: radon `A (4)`
- `api_needle_feedrate_from_payload`: radon `A (5)`

## Wily diff

Wily was run from a disposable UTF-8 temporary clone with this task's file contents copied in and committed locally for analysis.

Command result:

```text
main.py                                        Cyclomatic 2189 -> 2148
main.py                                        Lines of Code 11195 -> 11004
main.py:Main._api_move_to_contact              Cyclomatic 13 -> 10
main.py:Main._api_contact_needles              Cyclomatic 8 -> 4
probe_station_gui/design/contact_navigation.py Cyclomatic - -> 37
probe_station_gui/design/contact_navigation.py Lines of Code - -> 362
probe_station_gui/design/contact_navigation.py Maintainability Index - -> 34.2980
```

Wily emitted repeated `No data collected` warnings for older revisions during history build, but the history build and final diff both completed successfully.

## Tests and lint

Initial task verification:

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_contact_navigation.py -q`
  - `17 passed in 0.11s`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_contact_navigation.py tests\app\test_main_coordinate_feedrate.py -q`
  - `166 passed, 3 subtests passed in 2.38s`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - `978 passed, 2 skipped in 11.55s`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - `All checks passed!`

Follow-up LOC-gate fix verification:

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_contact_navigation.py -q`
  - `21 passed in 0.15s`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_contact_navigation.py tests\app\test_main_coordinate_feedrate.py -q`
  - `170 passed, 3 subtests passed in 2.30s`

Regression-order fix verification:

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\app\test_main_coordinate_feedrate.py -q`
  - `153 passed, 3 subtests passed in 2.20s`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_contact_navigation.py tests\app\test_main_coordinate_feedrate.py -q`
  - `174 passed, 3 subtests passed in 2.02s`

Reviewer follow-up fix verification:

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_contact_navigation.py tests\app\test_main_coordinate_feedrate.py -q`
  - `176 passed, 3 subtests passed in 2.14s`

No hardware-dependent automation was run.

## TDD RED/GREEN evidence

RED:

- Added `tests/design/test_contact_navigation.py` first.
- Initial run failed during collection with:
  - `ModuleNotFoundError: No module named 'probe_station_gui.design.contact_navigation'`

GREEN:

- Implemented `probe_station_gui/design/contact_navigation.py` with request/plan/response helpers only.
- Re-ran `tests/design/test_contact_navigation.py -q` and got `17 passed`.
- Then thin `Main` adapters and app characterization additions were validated by the focused combined run and full suite.
- For the LOC-gate follow-up, added failing helper-surface tests first, saw import failure for the missing public helper exports, then implemented the helper moves and revalidated with the focused design/app runs above.
- For the regression-order follow-up, added failing app characterization tests for missing-contact, context-rejection, and invalid-action early returns on bare `Main.__new__(Main)` objects without `settings_manager`. The failure showed eager `self._api_needle_feedrate({})` access before validation. The fix made default feedrate resolution lazy and moved plan-time feedrate parsing back behind the old validation order.
- For the reviewer follow-up, added failing mixed-case regressions proving invalid `action="park"` must beat a rejecting contact-context response. The failure showed `api_contact_needles_plan()` still called `contact_context()` before action validation. The fix validates/normalizes action first, then runs contact-context lookup, then resolves feedrate afterward.

## Behavior preservation summary

- `Main` still owns all stage side effects:
  - `begin_external_task`
  - `run_external_needles_action`
  - `run_external_move_to_xy`
  - `finish_external_task`
  - `time.sleep`
  - `logger.exception`
- Parsing, contact lookup planning, route-point payload shaping, adjusted-XY math, action normalization, and response shaping moved to `probe_station_gui.design.contact_navigation`.
- Default needle feedrate resolution is now lazy for move-to-contact and contact-needles planning, so missing contact, invalid action, and contact-context rejections return before touching `settings_manager`, matching the old behavior.
- Invalid needle actions now short-circuit before contact-context lookup in both design planning and `Main._api_contact_needles()`, restoring the brief-required `400` precedence over context rejection.
- Parser exception behavior for `contact_settle_s` and needle feedrate remains uncaught in the planning layer, matching the old `_api_move_to_contact` / `_api_contact_needles` behavior.
- Final-lift handling still swallows only `StageControllerError`, logs it, and still finishes the external task.
- Route Pause Request / Pause Ack / Interrupt semantics were untouched.

## Verdict

- behavior preserved: `yes`
- tests passed: `yes`
- metrics improved: `yes`
- LOC gate passed: `yes`
- maintainability improvement: `The major hotspot is still improved, and the contact-navigation seam now absorbs more non-side-effect logic: request parsing, contact context assembly, adjusted-XY math, route-point payload shaping, and response helpers. Main is reduced to orchestration and retained seams that app characterization already relies on.`
- new risk introduced: `low`. The follow-up keeps the `_api_contact_context`, `_api_route_adjusted_stage_xy`, and `_api_needle_feedrate` seams where existing app characterization stubs them, while moving the underlying pure logic into the design module and covering it directly with design tests.
