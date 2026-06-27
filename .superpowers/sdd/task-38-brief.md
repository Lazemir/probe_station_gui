# Task 38 Brief: Stage API Coordinate Move Planning Split

Baseline commit: `19ccb0b refactor: extract API command dispatch`

## Current Behavior

- `probe_station_gui.stage.api_moves.api_coordinate_move_plan(...)` validates coordinate move requests from the local API.
- It preserves original unsupported-axis labels in error messages.
- It normalizes coordinate mode aliases to `G90`/`G91`.
- It chooses a feedrate using the current GUI feedrate and `MIN_FEEDRATE_MM_MIN` floor unless an explicit positive finite feedrate is supplied.
- It parses each requested axis, resolves display targets to raw stage targets through the caller-provided resolver, applies caller-provided limit checks, reports unsupported axes/invalid values/unavailable GUI coordinates/limit errors in the same priority order, and orders accepted targets by `axis_names`.
- `Main._api_move_to_coordinates(...)` remains the side-effect adapter that checks stage busy state, starts the coordinate move, and builds the success/error response.

## Structural Improvement

- Split `api_coordinate_move_plan(...)` into smaller pure helpers inside `probe_station_gui.stage.api_moves`.
- Keep the public function and `ApiCoordinateMovePlan` dataclass stable.
- Do not move stage side effects into the stage API planning module; it stays pure parsing/planning/response shaping.
- Prefer named intermediate result objects only if they reduce branching and make validation priority explicit. Do not add an abstraction that merely wraps a dict.

## Baseline Metrics

- `probe_station_gui/stage/api_moves.py` raw LOC: `209`.
- Lizard `api_coordinate_move_plan`: `119` NLOC / CCN `20`.
- Radon `api_coordinate_move_plan`: `C (20)`.
- File lizard average CCN: `5.4`; one lizard warning in this file.

## Target

- `api_coordinate_move_plan` drops below lizard warning thresholds, ideally below CCN `10`.
- No new helper in `api_moves.py` exceeds lizard warning thresholds.
- Response payloads and validation priority remain stable.
- Tests continue to cover each rejection class and successful axis ordering.

## Validation

- Run:
  - `.\.venv\Scripts\python.exe -m pytest tests\stage\test_api_moves.py tests\app\test_main_coordinate_feedrate.py`
  - `.\.venv\Scripts\python.exe -m pytest tests`
  - `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - Wily/radon/lizard comparison for `probe_station_gui/stage/api_moves.py` and `main.py` if affected.

## Risks

- Do not change the priority order of validation failures.
- Do not lose original raw-axis labels in the unsupported-axis message.
- Do not change `G90`/`G91` mode aliases or feedrate floor semantics.
- Do not add hardware I/O or GUI state access to the pure stage API planning module.
