Status: DONE

Summary of structural change:
- Split `api_coordinate_move_plan(...)` into private pure helpers for rejection payload creation, axis-name normalization, target parsing, target-error priority selection, stage-axis ordering, and final `ApiCoordinateMovePlan` construction.
- Kept the public `api_coordinate_move_plan(...)` function signature and `ApiCoordinateMovePlan` dataclass unchanged.
- Added public characterization coverage for mixed validation failures so the required priority order is pinned through the stable API.

Behavior parity notes:
- Non-dict targets, mode validation, and feedrate validation still occur before target parsing.
- Target parsing still preserves original raw unsupported-axis labels via `str(raw_axis)`.
- Target-error priority remains unsupported axes, invalid coordinate values, unavailable GUI coordinates, then limit errors.
- Empty target rejection still happens after all target-error classes.
- Mode alias handling and feedrate floor behavior continue to use the existing public helpers unchanged.

Tests/commands run and exact results:
- `.\.venv\Scripts\python.exe -m pytest tests\stage\test_api_moves.py`
  - Before refactor after adding characterization: `12 passed in 0.08s`.
  - After refactor: `12 passed in 0.07s`.
- `.\.venv\Scripts\python.exe -m pytest tests\stage\test_api_moves.py tests\app\test_main_coordinate_feedrate.py`
  - `174 passed in 2.15s`.
  - Final rerun: `174 passed in 2.11s`.
- `.\.venv\Scripts\python.exe -m pytest tests`
  - Final controller rerun: `1142 passed, 2 skipped in 24.06s`.
- `.\.venv\Scripts\python.exe -m ruff check probe_station_gui\stage\api_moves.py tests\stage\test_api_moves.py`
  - First run found stale import: `F401 [*] typing.Any imported but unused`.
  - After removing the import: `All checks passed!`.
  - Final rerun: `All checks passed!`.
- `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - Final controller rerun: `All checks passed!`.
- `.\.venv\Scripts\python.exe -m lizard probe_station_gui\stage\api_moves.py`
  - `No thresholds exceeded`.
  - `Warning cnt 0`.
- `.\.venv\Scripts\python.exe -m radon cc probe_station_gui\stage\api_moves.py -s`
  - Completed successfully; spot-check metrics below.
- `.\.venv\Scripts\python.exe -m radon raw probe_station_gui\stage\api_moves.py`
  - Completed successfully; raw metrics below.

Metrics spot checks:
- Lizard `api_coordinate_move_plan`: `57` NLOC / CCN `6`.
- Lizard largest helper: `_parse_coordinate_targets`: `49` NLOC / CCN `7`.
- Lizard file average CCN: `3.5`; warning count `0`.
- Radon `api_coordinate_move_plan`: `B (6)`.
- Radon largest helper: `_parse_coordinate_targets`: `B (7)`.
- Radon raw `api_moves.py`: LOC `317`, SLOC `271`, LLOC `142`.
- Wily final from disposable UTF-8 temp clone/cache, comparing temp Task 38 commit against `19ccb0b`:
  - `probe_station_gui/stage/api_moves.py`: MI `33.0571 -> 31.18`, cyclomatic `39 -> 47`, raw LOC `235 -> 317`.
  - `tests/stage/test_api_moves.py`: MI `33.7657 -> 30.436`, cyclomatic `21 -> 22`, raw LOC `245 -> 324`.
  - `api_coordinate_move_plan`: cyclomatic `20 -> 6`.
  - `main.py`: unchanged by this task; contract target `<= 8948` remains satisfied.

Reviews:
- Spec review: approved with no findings.
- Code-quality review: approved for code scope. One minor scope note: `CONTEXT.md` is modified in the working tree for the roadmap/LOC contract update, but it is not part of Task 38 and should not be included in the Task 38 commit.

Verdict:
This refactor is justified.

- Behavior preserved: yes.
- Tests passed: yes.
- Metrics improved: yes for the targeted hotspot and warning count.
- Maintainability improvement: validation, target parsing, error priority selection, axis ordering, and plan construction now have local named helpers; `api_coordinate_move_plan` dropped from warning-level CC `20` to CC `6`.
- New risk introduced: low. File-level Wily LOC/cyclomatic and MI worsened because the former monolith was split into explicit helpers and characterization coverage was added; this is accepted for this pass because the warning-level hotspot was removed and behavior priority is now pinned by tests.

Files changed:
- `probe_station_gui/stage/api_moves.py`
- `tests/stage/test_api_moves.py`
- `.superpowers/sdd/task-38-brief.md`
- `.superpowers/sdd/task-38-report.md`
