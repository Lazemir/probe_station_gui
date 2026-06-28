# Task 50a Report: Settings Default File Normalization Extraction

Baseline commit: `511905a`
Result commit: pending
Wily temp root: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task50a_final_20260628173715`

## Current Behavior

`SettingsManager._ensure_default_file` copied bundled `default_settings.json`, patched missing sections, migrated legacy feedrate presets, removed persisted Telegram bot tokens, filled nested needle bookmarks, and completed objective profiles before writing the first user `settings.json`.

`SettingsManager._load` also mixed file reading, control binding reconstruction, raw section extraction, logging fallback, and construction of the full `Settings` object.

## Structural Improvement

Added `probe_station_gui/settings/default_file.py` as the module that owns first-run default settings JSON normalization. `SettingsManager` now keeps path/file orchestration, while default section shape and legacy default-file completion live in one focused module.

Also split `_load` orchestration into small helpers:

- `_settings_from_raw`
- `_load_controls`
- `_raw_section`
- `_design_last_directory_from_raw`

This keeps public `SettingsManager` behavior and the settings JSON shape stable.

## Validation

- Focused settings tests: `74 passed`
- Full suite with coverage: `1234 passed, 2 skipped`
- Ruff: `ruff check --ignore E402,F401 .` passed
- Coverage: `74%` total
- Lizard: no warning-level functions in the touched settings slice

## Metrics

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| `settings/manager.py` LOC | 1660 | 1327 | yes |
| `settings/manager.py` Wily cyclomatic | 214 | 174 | yes |
| `settings/manager.py` Maintainability Index | 0 | 0 | flat |
| New `settings/default_file.py` LOC | - | 196 | n/a |
| New `settings/default_file.py` Wily cyclomatic | - | 28 | n/a |
| New `settings/default_file.py` MI | - | 37.85 | n/a |
| `SettingsManager._ensure_default_file` CC | 35 | 4 | yes |
| `SettingsManager._load` CC | 27 | 1 | yes |
| New module max CC | - | 9 | n/a |
| Tests | 1232 passed, 2 skipped | 1234 passed, 2 skipped | yes |
| Coverage | 74% | 74% | flat |
| Public API changed | no | no | yes |

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes;
- tests passed: yes;
- metrics improved: yes for the target monolith and two major hotspots;
- maintainability improvement: first-run default file normalization has a single pure module with focused tests, and `SettingsManager` no longer owns every default-section migration branch inline;
- new risk introduced: low to medium, because settings defaults affect first-run startup; mitigated by focused tests around invalid defaults, legacy feedrate presets, Telegram bot-token stripping, nested needle bookmarks, and objective profile default completion.
