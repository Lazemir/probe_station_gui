# Task 13 Report

- status: DONE
- changed files:
  - `probe_station_gui/stage/fluidnc_config_io.py`
  - `probe_station_gui/stage/controller.py`
  - `probe_station_gui/stage/homing_startup.py`
  - `probe_station_gui/stage/motion_commands.py`
  - `probe_station_gui/stage/autofocus_flow.py`
  - `probe_station_gui/stage/status_io.py`
  - `tests/stage/test_controller.py`
  - `.superpowers/sdd/task-13-report.md`
- commit hash(es):
  - `bd76758` - implementation commit for the completed pass

## Summary

Removed explicit `serial_connection` parameters from the five scoped FluidNC config I/O helpers and updated the listed stage call sites to use the controller's current serial/session internally. Behavior and user-facing messages were preserved.

Explicit helper parameters removed: `5`

## Tests run

- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_controller.py -k "query_axis_max_feedrates or ensure_axis_limits_refreshes_partial_cache or startup_sync or absolute_xy_move_uses_relative_delta or disabled_motion_safety or multi_axis_absolute_move_uses_single_g90_command or absolute_xy_move_respects_homed_axis_soft_limit or absolute_xy_move_emits_started"`
  - initial red step: `6 failed, 5 passed, 112 deselected`
  - after refactor: `11 passed, 112 deselected`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_controller.py tests\stage\test_fluidnc_session.py tests\stage\test_fluidnc_protocol.py`
  - final: `144 passed`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
  - final: `777 passed, 2 skipped`
- `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
  - final: `All checks passed!`

## Wily

- Attempted:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m wily build probe_station_gui\stage\fluidnc_config_io.py probe_station_gui\stage\controller.py probe_station_gui\stage\homing_startup.py probe_station_gui\stage\motion_commands.py probe_station_gui\stage\autofocus_flow.py probe_station_gui\stage\status_io.py`
    - failed: `Dirty repository, make sure you commit/stash files first`
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m wily report ...`
    - crashed on this machine after startup; CLI pointed to a temp wily log file

## Metrics before/after

- `probe_station_gui/stage/fluidnc_config_io.py`
  - file NLOC: `317 -> 307` (`lizard`)
  - `_ensure_status_report_mask`: `A (2) -> A (2)` (`radon`), `16/4 -> 13/3` (`lizard` NLOC/PARAM)
  - `_query_axis_max_feedrates_locked`: `B (7) -> B (7)` (`radon`), `30/3 -> 29/2` (`lizard`)
  - `_refresh_coordinate_system_state`: `C (15) -> C (15)` (`radon`), `41/3 -> 39/2` (`lizard`)
  - `_read_startup_limits`: `A (4) -> A (4)` (`radon`), `20/3 -> 18/2` (`lizard`)
  - `_ensure_axis_limits`: `C (13) -> C (13)` (`radon`), `31/3 -> 30/2` (`lizard`)
- `probe_station_gui/stage/controller.py`
  - file NLOC: `861 -> 858` (`lizard`)
  - `query_axis_max_feedrates`: `A (3) -> A (3)` (`radon`)
  - `_query_synced_status_for_absolute_motion`: `B (6) -> B (6)` (`radon`)
- `probe_station_gui/stage/homing_startup.py`
  - file NLOC: `163 -> 159` (`lizard`)
  - `_run_startup_sync`: `C (15) -> C (15)` (`radon`)
- `probe_station_gui/stage/motion_commands.py`
  - file NLOC: `860 -> 856` (`lizard`)
  - `_send_relative_move`: `C (15) -> C (15)` (`radon`)
  - `_send_absolute_axis_targets_move`: `C (13) -> C (13)` (`radon`)
- `probe_station_gui/stage/autofocus_flow.py`
  - file NLOC: `590 -> 589` (`lizard`)
  - `_prepare_autofocus_context_locked`: `C (11) -> C (11)` (`radon`)
- `probe_station_gui/stage/status_io.py`
  - file NLOC: `300 -> 299` (`lizard`)
  - `_query_status`: `A (5) -> A (5)` (`radon`)

## Concerns / residual gaps

- Wily could not produce a clean before/after history report in the dirty working tree, so the exact comparison here relies on the successful validation run plus radon/lizard spot checks.

## Fix after review

- changed files:
  - `probe_station_gui/stage/connection_state.py`
  - `probe_station_gui/stage/controller.py`
  - `probe_station_gui/stage/homing_startup.py`
  - `tests/stage/test_controller.py`
  - `.superpowers/sdd/task-13-report.md`
- commit hash(es):
  - `edfd7c0` - fix code and regression coverage for serial pinning
- tests run and exact results:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_controller.py -k "captured_serial"`
    - red step before fix: `2 failed, 123 deselected`
    - after fix: `2 passed, 123 deselected`
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_controller.py tests\stage\test_fluidnc_session.py tests\stage\test_fluidnc_protocol.py`
    - final: `146 passed`
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
    - final: `All checks passed!`
- how the fix preserves serial pinning:
  - `_serial_session` now accepts an already captured serial object and pushes that object onto the existing thread-local session stack.
  - `_run_startup_sync` captures the open serial once, then enters `_serial_session(serial_connection)` for startup config/status I/O, optional startup A homing, and the controller session marker, so `_current_serial()` and `_current_fluidnc_session()` resolve to the same serial even if `self._serial` changes.
  - `_query_synced_status_for_absolute_motion` captures the current serial once and wraps coordinate refresh plus final status reads in `_serial_session(serial_connection)`, preserving the same pin for `$10`, `$G`, `$#`, and `?` in lock-only callers.
  - The five refactored FluidNC config helpers still have no explicit `serial_connection` parameters and no compatibility wrappers were added.
- residual concerns:
  - None from the required focused tests and lint. The pre-existing untracked `.scratch/` directory was left untouched.

## Second fix after re-review

- changed files:
  - `probe_station_gui/stage/controller.py`
  - `probe_station_gui/stage/jog_queue.py`
  - `tests/stage/test_controller.py`
  - `.superpowers/sdd/task-13-report.md`
- commit hash(es):
  - `0d2a5e198a0637b8b03eb727d18181c0cbafdf35` - fix code and regression coverage for manual status serial pinning
- tests run and exact results:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_controller.py -k "captured_serial or status_mask"`
    - red step before fix: `2 failed, 2 passed, 123 deselected`
    - after fix: `4 passed, 123 deselected`
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\stage\test_controller.py tests\stage\test_fluidnc_session.py tests\stage\test_fluidnc_protocol.py`
    - final: `148 passed`
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
    - final: `All checks passed!`
- how this closes the status/jog serial pinning issue:
  - `_poll_status_once` still captures the open serial once and keeps its nonblocking manual lock acquisition, then enters `_serial_session(serial_connection)` before calling `_query_status(...)`. This pins `_ensure_status_report_mask()` and `_read_status_frame(serial_connection)` to the same captured serial.
  - `_refresh_cached_jog_status_if_missing` now uses the same nested session pattern around its lock-only `_query_status(...)` call, so jog status mask writes and `?` reads cannot split across a replacement `self._serial`.
  - The fix does not reintroduce explicit `serial_connection` parameters on the five Task 13 FluidNC config helpers and does not add compatibility wrappers.
- residual concerns:
  - None from the required focused tests and lint. The pre-existing untracked `.scratch/` directory was left untouched.

## Controller verification and final metrics

- task review:
  - approved after the second re-review; no Critical, Important, or Minor findings remained.
- controller-run validation:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests`
    - final: `781 passed, 2 skipped`
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .`
    - final: `All checks passed!`
- Wily command:
  - `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m wily -c %TEMP%\probe_station_gui_wily_cache_task13 diff --detail --revision 00a05de ...stage files...`
- Wily file-level comparison, `00a05de -> 19fb9f0`:
  - `fluidnc_config_io.py`: MI `21.0858 -> 21.2981`, LOC `347 -> 336`, cyclomatic `93 -> 93`
  - `controller.py`: MI `0 -> 0`, LOC `1005 -> 1004`, cyclomatic `176 -> 176`
  - `homing_startup.py`: MI `31.7427 -> 31.7427`, LOC `193 -> 189`, cyclomatic `39 -> 39`
  - `motion_commands.py`: MI `0 -> 0`, LOC `935 -> 931`, cyclomatic `173 -> 173`
  - `autofocus_flow.py`: MI `11.0902 -> 11.1223`, LOC `638 -> 637`, cyclomatic `87 -> 87`
  - `status_io.py`: MI `23.3707 -> 23.3707`, LOC `330 -> 329`, cyclomatic `68 -> 68`
  - `jog_queue.py`: MI `13.3109 -> 13.2756`, LOC `539 -> 540`, cyclomatic `108 -> 108`
- fallback radon/lizard spot checks at final HEAD:
  - `fluidnc_config_io._ensure_status_report_mask`: radon `A (2)`, lizard `13 NLOC / 3 params`
  - `fluidnc_config_io._query_axis_max_feedrates_locked`: radon `B (7)`, lizard `29 NLOC / 2 params`
  - `fluidnc_config_io._refresh_coordinate_system_state`: radon `C (15)`, lizard `39 NLOC / 2 params`
  - `fluidnc_config_io._read_startup_limits`: radon `A (4)`, lizard `18 NLOC / 2 params`
  - `fluidnc_config_io._ensure_axis_limits`: radon `C (13)`, lizard `30 NLOC / 2 params`
  - final touched-stage lizard summary: `3551 NLOC`, average CCN `4.4`, warning count `3`
- verdict:
  - This refactor is justified.
- reason:
  - behavior preserved: yes
  - tests passed: yes
  - metrics improved: yes, narrowly; target helper parameter count dropped by five, touched file LOC mostly decreased, MI improved in two touched files and stayed stable in most others
  - maintainability improvement: config helpers now use the FluidNC session seam instead of repeating serial pass-through in callers, and regression tests pin the hidden serial/session invariant
  - new risk introduced: the first implementation exposed a real serial-pinning risk; both affected lock-only path classes now have focused regression coverage
