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

## Review fixes: raster orientation and stale hover

Follow-up review identified two production defects after commit `78e49fb`.

### RED evidence

The fixes were preceded by three focused regressions:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_klayout_raster.py::test_real_klayout_raster_world_top_aligns_real_plot_overlay tests/ui/test_design_plot_klayout.py::test_snap_off_invalidates_inflight_hover_response tests/ui/test_design_plot_klayout.py::test_cursor_leave_invalidates_inflight_hover_response -q
```

Result before production changes: `3 failed`.

- The asymmetric real KLayout frame placed high-world-Y geometry at source
  image centroid Y about 47 px. Its real pyqtgraph overlay marker painted at
  widget Y about 63.5 px, while the raster geometry painted at about 356.4 px.
  This proved that the raster item vertically mirrored the worker image.
- Both stale-hover tests showed that a response submitted before Snap Off or
  cursor leave restored a non-`None` hover result after the overlay had been
  cleared.

### Root causes and fixes

- `QImage` rows are top-down while the pyqtgraph world coordinate system is
  Y-up. Directly drawing into the positive-height world rectangle mapped the
  image's top row to `world_box.bottom`. `KLayoutRasterItem.paint` now performs
  one painter-local Y reflection around `world_box.bottom + world_box.top`.
  The worker image, frame box, and bounding rectangle are unchanged, avoiding
  a double flip.
- Hover response acceptance was keyed by `_latest_hover_request_id`, but clear
  paths retained the prior id. `_set_hover_snap(None)` now invalidates that id,
  covering Snap Off, out-of-bounds/no-position cursor state, missing document,
  and missing worker/config. Document changes and shutdown retain their
  explicit invalidation too.

### GREEN evidence

- The three new regressions: `3 passed in 2.14s`.
- Task 4 UI plus existing navigator/main regression command:

  ```powershell
  C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_klayout_raster.py tests/ui/test_design_plot_klayout.py tests/ui/test_design_navigator_panel.py tests/app/test_main_design_navigation.py -q
  ```

  Result: `34 passed in 3.48s`.

- `git diff --check`, focused `compileall`, and Ruff all exited 0; Ruff reported
  `All checks passed!`.
- Fresh full suite:

  ```powershell
  C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q
  ```

  Result: `1420 passed, 10 subtests passed in 22.30s`.

No hardware path or `main.py` process was started. The orientation regression
uses a generated asymmetric GDS, the real `KLayoutRenderWorker`, and a real
offscreen `pyqtgraph.PlotWidget` with a world-coordinate overlay marker.
