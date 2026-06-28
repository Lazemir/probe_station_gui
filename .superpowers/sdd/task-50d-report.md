# Task 50d Report: Settings Section Parser Extraction

Date: 2026-06-28
Branch: `codex/refactor-stage-controller`
Base comparison revision: `379868c`
Wily temp root: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task50d_20260628182254`

## Scope

Current behavior:

- `SettingsManager` loaded and normalized objectives, feedrate groups, click-to-move values, Telegram settings, needle calibration preferences, saved stage positions, and JSON persistence.
- Objective and needle parsing behavior was partly tested through `SettingsManager` private methods.
- Public settings dataclasses and `SettingsManager` public methods had to stay stable.

Structural improvement:

- Moved objective section parsing into `probe_station_gui.settings.objective_config`.
- Moved needle calibration preference parsing and config-to-settings conversion into `probe_station_gui.settings.needle_calibration_config`.
- Moved feedrate config-to-settings normalization into `probe_station_gui.settings.feedrate_config`.
- Removed stale private `SettingsManager` compatibility wrappers for objective parsing, feedrate parsing, saved stage position parsing, and value coercion.
- Updated runtime imports to use `normalize_objective_name` from `settings.objective_config`.
- Moved objective and needle parser characterization tests to settings-module tests.

Validation check:

- Preserve JSON schema and settings output shape.
- Keep public `SettingsManager` methods stable.
- Run focused settings/UI tests, full suite with coverage, ruff, vulture spot-check, lizard, radon, and Wily diff.

## Metrics

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| `settings/manager.py` LOC | 1327 | 1006 | yes |
| `settings/manager.py` SLOC | 1110 | 827 | yes |
| `settings/manager.py` Wily cyclomatic | 174 | 142 | yes |
| `settings/manager.py` radon highest CC | C(14), objective parser | B(10), `_load_controls` | yes |
| `parse_needle_calibration_settings` CC | 24 | 2 | yes |
| `settings/needle_calibration_config.py` MI | 18.2379 | 22.2593 | yes |
| `settings/dialogs/click_calibration_dialog.py` LOC | 216 | 216 | neutral |
| `settings/feedrate_config.py` LOC | 213 | 255 | no, responsibility moved here |
| `settings/objective_config.py` LOC | 192 | 330 | no, responsibility moved here |
| `settings/needle_calibration_config.py` LOC | 627 | 809 | no, responsibility moved here |
| Touched-slice lizard warnings | 0 | 0 | neutral |
| Full test suite | passing | passing | yes |
| Coverage | 75% | 75% | neutral |
| Public API changed | no | no | yes |

Wily detail:

- `probe_station_gui/settings/manager.py`: cyclomatic `174 -> 142`, LOC `1327 -> 1006`, MI `0 -> 0`.
- `probe_station_gui/settings/needle_calibration_config.py`: cyclomatic `52 -> 57`, LOC `627 -> 809`, MI `18.2379 -> 22.2593`.
- `probe_station_gui/settings/objective_config.py`: cyclomatic `43 -> 63`, LOC `192 -> 330`, MI `32.8229 -> 26.2583`.
- `probe_station_gui/settings/feedrate_config.py`: cyclomatic `27 -> 29`, LOC `213 -> 255`, MI `40.4459 -> 39.8885`.
- `parse_needle_calibration_settings`: cyclomatic `24 -> 2`.

The local complexity increase in objective/feedrate/needle modules is accepted for this pass because it removes section-specific parsing responsibility from `SettingsManager`, drops the largest manager file below the target, and lowers the worst touched function from warning-level complexity to a small orchestration function.

## Verification

- Focused tests:
  - `.\.venv\Scripts\python.exe -m pytest tests\settings\test_objective_config.py tests\settings\test_needle_calibration_settings.py tests\settings\test_feedrate_config.py tests\ui\test_key_bindings.py`
  - Result: `46 passed`.
- Full tests with coverage:
  - `.\.venv\Scripts\python.exe -m coverage erase`
  - `.\.venv\Scripts\python.exe -m coverage run -m pytest tests`
  - `.\.venv\Scripts\python.exe -m coverage report -m --ignore-errors`
  - Result: `1240 passed, 2 skipped`, total coverage `75%`.
- Ruff:
  - `.\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - Result: no lint errors.
- Focused vulture:
  - `.\.venv\Scripts\python.exe -m vulture probe_station_gui\settings\manager.py probe_station_gui\settings\objective_config.py probe_station_gui\settings\needle_calibration_config.py probe_station_gui\settings\feedrate_config.py tests\settings tests\ui\test_key_bindings.py --min-confidence 80`
  - Result: no candidates.
- Lizard touched slice:
  - Result: 0 warnings.
- Wily:
  - Disposable UTF-8 temp clone/cache.
  - Result: metrics table above.

Full-project vulture including `main.py` still reports old `main.py` candidates such as `QLocale`, validators/shortcuts, route meter constants, and `quarter_turn_delta`. Those are not part of Task 50d and should be handled in a later `main.py` cleanup pass with UI characterization.

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes, full suite passed;
- tests passed: yes;
- metrics improved: yes, `settings/manager.py` LOC/SLOC/cyclomatic dropped and `parse_needle_calibration_settings` CC dropped from 24 to 2;
- maintainability improvement: section parsing now lives with the section modules instead of behind `SettingsManager` private wrappers;
- new risk introduced: low; the main risk is that duplicated manager defaults were replaced by section defaults, covered by settings tests and full-suite regression.

## Stop Point

The user asked to stop when the current task finishes. Do not start Task 51 or another cleanup pass from this state without an explicit next instruction.
