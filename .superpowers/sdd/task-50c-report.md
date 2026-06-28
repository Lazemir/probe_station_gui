# Task 50c Report: Settings Dialog Section Module Extraction

Baseline commit: `5fb2737`
Result commit: pending
Wily temp root: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task50c_final2_20260628180303`

## Current Behavior

`SettingsDialog` still owned the objectives tab and coordinate-system tab inline. The objectives tab edited active objective selection, per-objective offsets, autofocus parameters, and click-calibration status. The coordinate tab edited position mode, startup mode, preferred WCS, and the enabled/tooltip state for preferred WCS.

Both tabs used the same dialog collection contract as the other settings tabs: mutate the cloned aggregate `Settings` instance through `to_settings(self._settings)`.

## Structural Improvement

Added focused settings dialog modules:

- `probe_station_gui/dialogs/settings/objectives.py`
- `probe_station_gui/dialogs/settings/coordinate_system.py`

`SettingsDialog` now imports these widgets and keeps tab order plus settings collection only. The moved objective widget now imports objective models from `settings.objective_config`, and the coordinate widget imports coordinate models from `settings.sections`.

The coordinate mode tooltip/enabled logic is behind `coordinate_system_hint_state(...)`, and objective profile lookup/status handling no longer needs branch-heavy inline checks.

## Validation

- Focused objective/coordinate/import tests: `6 passed`
- Full suite with coverage: `1240 passed, 2 skipped`
- Ruff: `ruff check --ignore E402,F401 .` passed
- Coverage: `75%` total
- Lizard: no warning-level functions in the touched settings-dialog slice
- Vulture touched-slice candidates: two existing `SettingsDialog` methods (`result_settings`, `was_applied`); no new objective/coordinate candidates

## Metrics

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| `settings_dialog.py` LOC | 1317 | 1091 | yes |
| `settings_dialog.py` SLOC | 1149 | 955 | yes |
| `settings_dialog.py` Wily cyclomatic | 218 | 179 | yes |
| `settings_dialog.py` Maintainability Index | 0 | 0 | flat |
| New `dialogs/settings/coordinate_system.py` LOC | - | 108 | n/a |
| New `dialogs/settings/coordinate_system.py` Wily cyclomatic | - | 20 | n/a |
| New `dialogs/settings/coordinate_system.py` MI | - | 43.94 | n/a |
| New `dialogs/settings/objectives.py` LOC | - | 164 | n/a |
| New `dialogs/settings/objectives.py` Wily cyclomatic | - | 16 | n/a |
| New `dialogs/settings/objectives.py` MI | - | 41.79 | n/a |
| Touched production slice LOC | 1317 | 1363 | no |
| Touched production slice Wily cyclomatic | 218 | 215 | yes |
| `settings_dialog.py` target `< 1100 LOC` | no | yes | yes |
| Lizard warning-level functions | 0 | 0 | flat |
| Dead code candidates | 2 existing | 2 existing | flat |
| Tests | 1237 passed, 2 skipped | 1240 passed, 2 skipped | yes |
| Coverage | 75% | 75% | flat |
| Public API changed | no | no | yes |

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes;
- tests passed: yes;
- metrics improved: yes for target file size, target-file cyclomatic complexity, aggregate touched-slice cyclomatic complexity, and test coverage around the moved behavior;
- maintainability improvement: objective and coordinate settings now have focused modules and focused tests, while `SettingsDialog` is no longer the place-to-change for those tab internals;
- new risk introduced: low, because this is settings UI plumbing; mitigated by tests for objective profile edit/save, save-before-profile-switching, preserved click-calibration matrix state, coordinate tooltip/enabled states, and public import compatibility.

Tradeoff:

The touched production LOC grows by 46 lines because two section modules now carry their own imports and testable helper logic. This is acceptable here because `settings_dialog.py` is below the `< 1100 LOC` target, aggregate touched-slice cyclomatic complexity improves, and no warning-level functions were introduced.
