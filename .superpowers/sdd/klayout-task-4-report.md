# Task 4 Report: Raster viewport scheduler and design plot integration

## Status

Implemented and verified on `codex/klayout-design-renderer`.

Commit subject: `feat: render design canvas with KLayout`

## RED evidence

1. Initial Task 4 RED, before any production edit:

   ```powershell
   $env:QT_QPA_PLATFORM='offscreen'
   C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_klayout_raster.py tests/ui/test_design_plot_klayout.py -q
   ```

   Result: exit 1 during collection with the expected
   `ModuleNotFoundError: No module named 'probe_station_gui.views.design_klayout_raster'`.

2. Plot integration RED after the isolated raster module became GREEN:

   ```powershell
   C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_plot_klayout.py -q
   ```

   Result: exit 1, `1 failed, 2 passed, 11 errors`; the production pane did not
   expose `KLayoutRasterController`, and midpoint hint copy still reported
   `Line` instead of `Center`.

3. Close/reopen lifecycle regression RED:

   ```powershell
   C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_plot_klayout.py::test_design_window_close_detaches_workers_and_reopen_restores_document -q
   ```

   Result: exit 1 because a permanently shut down raster controller left the
   reopened window with `_klayout_config is None`.

4. Existing fixture compatibility RED found by the first full-suite run:

   ```powershell
   C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
   ```

   Result: `1 failed, 1416 passed`. The failure was
   `tests/design/test_click_navigation.py::test_design_navigation_double_click_moves`;
   the legacy lightweight fixture intentionally uses a plain object as its
   document and therefore has no `file_backed` attribute.

5. Non-blocking document-source change RED from final checklist review:

   ```powershell
   C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_klayout_raster.py::test_same_path_reuses_worker_path_change_detaches_and_unload_joins_bounded tests/ui/test_design_plot_klayout.py::test_file_configuration_changes_reuse_snap_worker_and_generation -q
   ```

   Result: exit 1 with `2 failed`; both old workers received a 0.5-second join
   timeout during a different-path update instead of a zero-time detach.

## GREEN evidence

- Isolated raster/controller GREEN: `7 passed`.
- Initial two-file UI GREEN: `21 passed`.
- Close/reopen regression GREEN: `1 passed`.
- Different-path detach regression GREEN: `2 passed`.
- Final focused regression command:

  ```powershell
  C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_click_navigation.py tests/ui/test_design_klayout_raster.py tests/ui/test_design_plot_klayout.py tests/ui/test_design_navigator_panel.py tests/app/test_main_design_navigation.py -q
  ```

  Result: `36 passed in 2.11s`.

- Static checks:

  ```powershell
  git diff --check
  C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m compileall -q probe_station_gui/views/design_klayout_raster.py probe_station_gui/views/design_plot_pane.py probe_station_gui/views/design_navigator_panel.py
  C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check probe_station_gui/views/design_klayout_raster.py probe_station_gui/views/design_plot_pane.py probe_station_gui/views/design_navigator_panel.py tests/ui/test_design_klayout_raster.py tests/ui/test_design_plot_klayout.py
  ```

  Result: exit 0; Ruff reported `All checks passed!`. `git diff --check`
  reported only the repository's line-ending conversion warnings, with no
  whitespace error.

- Final fresh full-suite verification:

  ```powershell
  C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q
  ```

  Result: `1417 passed, 10 subtests passed in 20.74s`.

## Implemented behavior

- Added one negative-z `QGraphicsObject` raster carrying a detached `QImage`
  and its exact design-space world box. Existing route, mark, target, FOV,
  current-position, tool, and hover items stay above it.
- Added the 16 ms active and 120 ms settled schedulers, exact zoom requests,
  4% pan coverage guard, 12.5% settled margin, physical-device-pixel sizing,
  and 4096-pixel largest-dimension cap. Scheduler output does not add another
  oversampling multiplier; the reviewed worker backend retains fixed
  oversampling 1.
- Retains and transforms the accepted frame during pan/zoom. Frames are
  accepted only for the current configuration generation and a current or
  sufficiently covering/dense viewport generation.
- Builds `KLayoutConfig` from file path, selected top cell, visible layers,
  unrotated `cell_bounds[top_cell]`, current rotated bounds, quarter turns, and
  a monotonically increasing generation.
- Reuses render and snap workers for same-path layer/top-cell/rotation changes.
  Different-path updates signal and detach old workers without joining the GUI
  thread. Explicit unload/close uses a bounded 0.5-second stop per worker.
- File-backed documents bypass `visible_plot_paths`, global snap geometry
  construction, and synchronous `DesignDocument.snap_point_info`. The legacy
  in-memory/fixture path remains synchronous and behavior-compatible.
- Hover uses replaceable local snap requests. Clicks capture semantic action,
  raw point, payload, request id, and config generation, then execute only for
  a matching response. Route pick, route point, double-click move, and both
  calibration buttons are covered. Snap-off actions execute immediately at the
  raw point.
- Both `segment` and `segment_center` highlight the complete source segment.
  Sidebar copy reports `Center`, `Line`, and `Corner`.
- Unload and close detach workers on the creator thread. Closing and reopening
  the reusable top-level design window restores workers from the retained
  document instead of leaving the canvas permanently shut down.

## Files

- Created `probe_station_gui/views/design_klayout_raster.py`
- Modified `probe_station_gui/views/design_plot_pane.py`
- Modified `probe_station_gui/views/design_navigator_panel.py`
- Created `tests/ui/test_design_klayout_raster.py`
- Created `tests/ui/test_design_plot_klayout.py`
- Created `.superpowers/sdd/klayout-task-4-report.md`

## Concerns and follow-up

- No hardware path and no `main.py` process was started.
- The full suite includes the reviewed synthetic KLayout worker smoke tests,
  but Task 4 did not repeat the later Task 6 real-workstation GDS p95 benchmark
  or a manual visual parity check.
- A different-file update deliberately uses `stop(timeout_s=0.0)`: the daemon
  may finish an already-running backend operation after it has been detached,
  while Task 3's stopping/publication guards suppress late GUI delivery.
  Explicit unload and window close still perform bounded joins as required.
