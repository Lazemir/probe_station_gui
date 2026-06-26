# Task 27 Report: API Stage Move Planning

## Scope

- Refactored API stage move parsing/planning out of `main.py` into `probe_station_gui/stage/api_moves.py`.
- Kept `Main` responsible for busy checks, `_start_coordinate_targets_move(...)`, and owned stage callbacks.
- Added pure stage-module tests and minimal `Main` characterization coverage for the adapter behavior.

## Physical LOC

- Baseline `7e351db` `main.py` physical lines: `11356`
- Final `main.py` physical lines: `11195`
- Delta: `-161`

Verdict:

- Required gate `<= 11206`: `passed`
- Stretch target `<= 11116`: `not met`

## Target Method Metrics

### Baseline (from brief)

- `Main._api_move_to_coordinates`: lizard `140 NLOC / CCN 23`, radon `D (23)`
- `Main._api_axis_value_map`: lizard `10 NLOC / CCN 5`, radon `A (5)`
- `Main._api_move_feedrate`: lizard `10 NLOC / CCN 5`, radon `A (5)`

### Final

- `Main._api_move_to_coordinates`: lizard `18 NLOC / CCN 5`, radon `A (5)`
- `probe_station_gui.stage.api_moves.api_coordinate_move_plan`: lizard `119 NLOC / CCN 20`, radon `C (20)`
- `probe_station_gui.stage.api_moves.api_axis_value_map`: lizard `14 NLOC / CCN 5`, radon `A (5)`
- `probe_station_gui.stage.api_moves.api_move_feedrate`: lizard `15 NLOC / CCN 5`, radon `A (5)`
- `probe_station_gui.stage.api_moves.normalize_api_coordinate_input_mode`: lizard `7 NLOC / CCN 4`, radon `A (4)`

Notes:

- `_api_axis_value_map`, `_api_move_feedrate`, and coordinate mode normalization no longer live in `Main`; their logic now lives in `probe_station_gui.stage.api_moves`.

## Wily Diff vs `7e351db`

Run from a disposable temporary clone with a disposable local commit, not from the main checkout.

- `main.py`: cyclomatic complexity `2221 -> 2189`, raw LOC `11356 -> 11195`, maintainability index `0 -> 0`
- `probe_station_gui/stage/api_moves.py`: cyclomatic complexity `- -> 39`, raw LOC `- -> 235`, maintainability index `- -> 33.057070737974705`
- `main.py:Main._api_move_to_coordinates`: cyclomatic complexity `23 -> 5`

Wily also reported the extracted helper symbols:

- `api_coordinate_move_plan`: `- -> 20`
- `api_axis_value_map`: `- -> 5`
- `api_move_feedrate`: `- -> 5`
- `normalize_api_coordinate_input_mode`: `- -> 4`
- `api_coordinate_move_success_response`: `- -> 2`
- `api_coordinate_move_busy_response`: `- -> 1`
- `api_coordinate_move_start_failed_response`: `- -> 1`

## Lizard / Radon Spot Checks

Commands run:

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONUTF8='1'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m lizard main.py probe_station_gui\stage\api_moves.py -l python -C 10 -L 50 --sort cyclomatic_complexity
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon cc -s main.py probe_station_gui\stage\api_moves.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon mi -s main.py probe_station_gui\stage\api_moves.py
```

Key results:

- `main.py`: lizard file summary `10584 NLOC`, `511` functions
- `probe_station_gui/stage/api_moves.py`: lizard file summary `208 NLOC`, `7` functions
- radon MI: `main.py - C (0.00)`, `probe_station_gui/stage/api_moves.py - A (33.06)`

## TDD Evidence

### RED

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_api_moves.py -q
```

Observed failure:

- `ModuleNotFoundError: No module named 'probe_station_gui.stage.api_moves'`

### GREEN

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_api_moves.py -q
```

Observed result:

- `11 passed in 0.11s`

### Characterization / Focused Integration

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_api_moves.py tests\app\test_main_coordinate_feedrate.py -q
```

Observed result:

- Final run after the LOC-gate cleanup: `154 passed, 3 subtests passed in 2.30s`

## Verification

### Full test suite

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
```

Observed result:

- `955 passed, 2 skipped in 11.37s`

### Ruff

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .
```

Observed result:

- `All checks passed!`

## Behavior Preservation Summary

- Non-dict target rejection preserved.
- Coordinate mode normalization preserved.
- Feedrate validation/default/minimum behavior preserved.
- Unsupported axis / invalid value / unavailable axis / limit-error rejection priority preserved.
- Axis ordering preserved by `STAGE_AXIS_NAMES`.
- Busy rejections still happen in `Main`, before move start side effects.
- Start failure and success payload formatting preserved.

## Final Verdict

- Behavior preserved: `yes`
- Tests passed: `yes`
- Metrics improved: `yes`
- LOC gate passed: `yes`
- Maintainability improvement: `Main._api_move_to_coordinates` dropped from `23` to `5` cyclomatic complexity, with the planning logic isolated in a dedicated tested module
- New risk introduced: `low`; stage move parsing now depends on a shared helper module, so future changes to API move validation should update both the pure module tests and the `Main` adapter characterization tests together
