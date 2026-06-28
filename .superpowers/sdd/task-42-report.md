# Task 42 Report: Contact Seek, Needle Calibration, and Sample Handling

Base commit: `01639e4 refactor: extract microscope scan planning`

## Changed

- Added `probe_station_gui.route.contact_seek` request/status/detail helpers for manual contact seek.
- Added `probe_station_gui.stage.calibration_positions` for needle target and chip/stone saved-position planning.
- Added `probe_station_gui.stage.sample_handling` for sample focus cache, load/unload messages, autofocus prompt, and load/unload stage sequences.
- Added `probe_station_gui.views.main_window_needle_calibration` for main-window needle calibration workflow glue.
- Kept `Main` public/private slot names as wrappers and restored `Main.CONTACT_SEEK_*` / `Main.SAMPLE_*` aliases so owner-level overrides still affect behavior.
- Added characterization tests for contact seek status/guards, saved needle/surface plans, sample handling, workflow wrappers, and override compatibility.

## Metrics

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| `main.py` LOC | 8371 | 7961 | yes |
| `main.py` SLOC | 7823 | 7413 | yes |
| Wily `main.py` cyclomatic | 1587 | 1502 | yes |
| Wily `main.py` MI | 0 | 0 | unchanged |
| `Main._request_contact_seek` CC | 8 | 1 | yes |
| `Main._run_contact_seek` CC | 10 | 1 | yes |
| `Main._on_contact_seek_finished` CC | 6 | 1 | yes |
| `Main._save_current_needle_height` CC | 11 | 1 | yes |
| `Main._move_to_surface_position` CC | 6 | 1 | yes |
| `Main._on_sample_handling_finished` CC | 8 | 1 | yes |
| Lizard warnings in touched scope | 2 | 2 | unchanged |
| Coverage | 71% | 71% | unchanged |
| Public API changed | no | no | yes |

New module metrics:

- `probe_station_gui/route/contact_seek.py`: LOC 186, MI A 36.76.
- `probe_station_gui/stage/calibration_positions.py`: LOC 200, MI A 32.83.
- `probe_station_gui/stage/sample_handling.py`: LOC 259, MI A 33.79.
- `probe_station_gui/views/main_window_needle_calibration.py`: LOC 473, MI A 21.92.

## Verification

- `.\.venv\Scripts\python.exe -m ruff check . --ignore E402,F401`: passed.
- `.\.venv\Scripts\python.exe -m pytest tests`: `1186 passed, 2 skipped`.
- `coverage run -m pytest tests` plus `coverage report -m --ignore-errors`: `1186 passed, 2 skipped`, total `71%`.
- `lizard main.py probe_station_gui\route\contact_seek.py probe_station_gui\stage\calibration_positions.py probe_station_gui\stage\sample_handling.py probe_station_gui\views\main_window_needle_calibration.py`: exit 1 only for existing warnings `_api_raw_voltage_sweep` and `closeEvent`; no new module warnings.
- Wily disposable UTF-8 temp clone/cache:
  - `main.py` LOC `8371 -> 7961`
  - `main.py` SLOC `7823 -> 7413`
  - `main.py` cyclomatic `1587 -> 1502`
  - `main.py` MI `0 -> 0`

## Review

- Spec compliance subagent: no Blocking, Important, or Minor findings.
- Code quality subagent: one Minor about class-attribute overrides for `CONTACT_SEEK_*` and `SAMPLE_*`.
- Re-review after fix: prior Minor resolved; no new Blocking or Important issues.

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes
- tests passed: yes
- metrics improved: yes
- maintainability improvement: `Main` no longer owns contact seek, saved calibration position, and sample load/unload branches directly; the old hotspots are wrapper-level in `Main` and covered by module-local tests.
- new risk introduced: moderate owner-protocol coupling in `main_window_needle_calibration`; acceptable for this pass because public slot names stayed stable and the next `main.py` task can continue removing similar adapter clusters.
