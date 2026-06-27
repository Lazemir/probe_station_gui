status: DONE_WITH_CONCERNS

# Task 33 Report: Design Document Load And Navigation Module

## Commits created

- `2e60567` - Extract design navigation adapter.
- `9acddd2` - Restore Z homing status ownership.
- Report committed separately after implementation commit.

## Changed files

- `main.py`
- `probe_station_gui/design/navigation_adapter.py`
- `tests/design/test_navigation_adapter.py`
- `tests/app/test_main_design_navigation.py`
- `tests/app/test_main_planned_move_prediction.py`
- `.superpowers/sdd/task-33-report.md`

## Behavior summary

- Added `probe_station_gui/design/navigation_adapter.py` for design load/restore/navigation/route decision policy and presentation payload planning.
- `Main` remains the side-effect adapter for Qt signals, widgets, status bar, persistence, stage moves, processEvents, and route-measurement restore side effects.
- Moved persisted restore parsing, visible layer parsing, persisted design view application, design load result planning, route edit decisions, target/route point selection, move planning, and panel/minimap payload selection out of `Main`.
- Preserved manual design load threading and window/status behavior.
- Preserved persisted restore one-shot clearing, stale/missing file rejection, X/Y/Z homing invalidation, load success/error cache clearing, route restore, and planned move prediction seeding.

## TDD red/green evidence

- RED: `python -m pytest tests\design\test_navigation_adapter.py tests\app\test_main_design_navigation.py -q`
  - Failed with `ModuleNotFoundError: No module named 'probe_station_gui.design.navigation_adapter'`.
- GREEN focused subset after implementation:
  - `tests\design\test_navigation_adapter.py tests\app\test_main_design_navigation.py tests\app\test_main_planned_move_prediction.py -q`
  - `24 passed in 0.79s`.
- Final required focused command:
  - `41 passed in 1.11s`.

## Verification summaries

- Follow-up focused:
  - Command: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_navigation_adapter.py tests\app\test_main_design_navigation.py tests\app\test_main_planned_move_prediction.py -q`
  - Result: `26 passed in 0.78s`.
- Follow-up full pytest:
  - Command: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - Result: `1063 passed, 2 skipped in 10.84s`.
- Follow-up ruff:
  - Command: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - Result: `All checks passed!`.
- Focused:
  - Command: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\design\test_navigation_adapter.py tests\design\test_workflow.py tests\design\test_click_navigation.py tests\app\test_main_design_navigation.py tests\app\test_main_planned_move_prediction.py -q`
  - Result: `41 passed in 1.11s`.
- Full pytest:
  - Command: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - Result: `1061 passed, 2 skipped in 11.02s`.
- Ruff:
  - Command: `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - Result: `All checks passed!`.

## Metrics

### LOC

- `main.py` physical LOC: `10285 -> 10035` (`-250`).
- Required 400-line target (`<= 9885`): not met.
- Fallback minimum 250-line reduction: met exactly.
- New adapter physical LOC: `796`.

### Wily

Wily was run in a disposable temp clone/cache with UTF-8 forced. The first config-file attempt crashed; retrying with CLI operators succeeded.

- `main.py` cyclomatic complexity: `1939 -> 1882`.
- `main.py` raw LOC: `10285 -> 10035`.
- `main.py` MI: `0 -> 0`.
- `probe_station_gui/design/navigation_adapter.py`: cyclomatic `118`, raw LOC `796`, MI `8.451020567318979`.
- `probe_station_gui/design/session.py`: unchanged in the diff output.

### Lizard/radon target methods

| Target | Before | After |
| --- | ---: | ---: |
| `_on_design_document_loaded` | lizard `71 / 21`, radon `D (21)` | lizard `21 / 5`, radon `A (5)` |
| `_maybe_restore_persisted_design` | lizard `52 / 12`, radon `C (12)` | lizard `32 / 10`, radon `B (10)` |
| `_refresh_design_panel` | lizard `57 / 7`, radon `B (7)` | lizard `30 / 4`, radon `A (4)` |
| `_rotate_design_document` | lizard `46 / 9`, radon `B (9)` | lizard `34 / 8`, radon `B (8)` |
| `_add_route_array_points` | lizard `49 / 7`, radon `B (7)` | lizard `14 / 2`, radon `A (2)` |
| `_update_design_position` | lizard `31 / 5`, radon `A (5)` | lizard `34 / 4`, radon `A (4)` |
| `_move_to_design_coordinate` | lizard `31 / 4`, radon `A (4)` | lizard `36 / 6`, radon `B (6)` |
| `_start_design_document_load` | lizard `26 / 6`, radon `B (6)` | lizard `18 / 6`, radon `B (6)` |
| `_document_with_persisted_design_view` | lizard `20 / 7`, radon `B (7)` | removed from `Main`; adapter function lizard `17 / 7`, radon `B (7)` |
| `_parse_persisted_visible_layers` | lizard `12 / 6`, radon `B (6)` | removed from `Main`; adapter function lizard `12 / 6`, radon `B (6)` |

Lizard `main.py` NLOC after: `9451`. Lizard warnings after: 8 total; the Task 33 `_on_design_document_loaded` warning was removed.

## Explicit verdict

- behavior preserved: yes, with focused app characterization plus full pytest evidence.
- tests passed: yes.
- metrics improved: yes; Wily file cyclomatic improved `1939 -> 1882`, several target methods dropped below warning range, and one major warning method was removed from warning range.
- LOC gate passed: partial. The requested `<= 9885` target was not met; fallback `-250` physical LOC and `_on_design_document_loaded < 10` were met.
- maintainability improvement: yes for locality and target complexity; Wily MI stayed `0` for `main.py`, adapter MI is nonzero but low.
- new risk introduced: `_move_to_design_coordinate` grew from radon `A (4)` to `B (6)` as `Main` now applies a richer move plan. The behavior is covered by planned-move and app design-navigation tests, but this method should be revisited in Task 34/35 style cleanups if more design move policy moves out.
