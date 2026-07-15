# Task 5 Report: KLayout microscope minimap

## Status

Implemented and verified on `codex/klayout-design-renderer` from base `0b4afd4`.

Commit subject: `feat: render design minimap with KLayout`

Review-fix subject: `fix: align and detach KLayout minimap`

## RED evidence

Before production changes:

```powershell
$env:QT_QPA_PLATFORM='offscreen'
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_microscope_view_minimap.py tests/ui/test_microscope_view_klayout_minimap.py -q
```

Result: `5 failed, 7 passed`.

The failures showed that the file-backed minimap still called
`DesignDocument.visible_polygons()` and never created a KLayout request, so
newest-only resize scheduling, stale-frame rejection, same-path worker reuse,
and bounded stop behavior were all absent. The legacy in-memory renderer
characterization already passed.

## Implemented behavior

- File-backed documents build `KLayoutConfig` from resolved path, top cell,
  visible layers, unrotated cell bounds, displayed bounds, quarter-turn
  rotation, and a monotonic generation. They never enter the polygon renderer.
- One dedicated `KLayoutRenderWorker` is created lazily on the GUI/creator
  thread. A zero-delay single-shot timer coalesces paint/resize activity to the
  newest full-bounds request with `purpose="minimap"` and physical-pixel image
  dimensions for the aspect-fitted design rectangle.
- Acceptance checks request id, config generation, viewport generation,
  purpose, world box, dimensions, desired cache key, and detached `QImage`
  validity before creating the GUI-thread `QPixmap`.
- Same-path layer/top/rotation changes reuse the worker and invalidate the old
  configuration image. Different paths disconnect and call `stop(0.0)` before
  lazy replacement. Document removal and widget close also disconnect and call
  `stop(0.0)`, so the GUI thread never waits for backend teardown.
- A matching-configuration image remains available, scaled to the same
  six-pixel-padded aspect-fit rectangle used by native overlays and click
  inversion, while a replacement size is pending. Images are drawn directly
  in screen space, so source row 0 remains the top row.
- Render failures clear the active request key so the same size/configuration
  can be retried on the next paint.
- The existing route, probe-route, mark, selected-point, FOV, and current
  position overlays remain native painter passes above the background. Minimap
  click/double-click code is unchanged.
- Non-file-backed documents retain the prior asynchronous polygon renderer.

## Verification

Focused minimap tests:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_microscope_view_minimap.py tests/ui/test_microscope_view_klayout_minimap.py -q
```

Result after review fixes: `16 passed in 0.71s`.

Task 4 plus minimap integration slice:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_microscope_view_minimap.py tests/ui/test_microscope_view_klayout_minimap.py tests/ui/test_design_klayout_raster.py tests/ui/test_design_plot_klayout.py tests/ui/test_design_navigator_panel.py tests/app/test_main_design_navigation.py -q
```

Result after review fixes: `50 passed in 3.79s`.

Static checks:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check probe_station_gui/views/microscope_view.py tests/ui/test_microscope_view_minimap.py tests/ui/test_microscope_view_klayout_minimap.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m compileall -q probe_station_gui/views/microscope_view.py tests/ui/test_microscope_view_klayout_minimap.py
git diff --check
```

Result: all exited 0. `git diff --check` reported only the repository's
LF-to-CRLF conversion warning, with no whitespace error.

Fresh full suite:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q
```

Result after review fixes: `1430 passed, 10 subtests passed in 22.37s`.

## Review-fix RED and GREEN evidence

The first review found an aspect-fit mismatch, synchronous removal/close
waits, and a latched failure key. New regressions were run before the fix:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_microscope_view_klayout_minimap.py::test_real_tall_klayout_raster_aligns_overlay_and_click_aspect_fit tests/ui/test_microscope_view_klayout_minimap.py::test_removal_and_close_do_not_wait_for_running_worker tests/ui/test_microscope_view_klayout_minimap.py::test_render_failure_allows_same_size_retry -q
```

RED result: `4 failed`.

- A real 50 by 115 KLayout GDS painted from screen X `751`, while native
  overlay/click mapping placed the left design edge at X `820.826`.
- A blocking worker fake made document removal take `0.515s` and widget close
  take `0.500s`.
- Re-requesting the same size after `failed` retained one submission.

GREEN result after the corrections: `4 passed in 0.60s`. The real render now
matches both mapped design corners, preserves the 50:115 aspect ratio, retains
the six-pixel vertical padding, and round-trips the mapped center through click
inversion.

## Files

- Modified `probe_station_gui/views/microscope_view.py`
- Added `tests/ui/test_microscope_view_klayout_minimap.py`
- Added `.superpowers/sdd/klayout-task-5-report.md`

## Self-review and concerns

- After the review corrections, checked each Task 5 requirement against the
  final diff; no remaining required behavior gap or incidental route/minimap
  input change was found.
- No `main.py`, camera, serial, stage, or hardware-dependent path was run or
  modified.
- The KLayout minimap uses the same reviewed worker/backend covered by Task 3
  and Task 4. Automated coverage now also renders a generated asymmetric tall
  GDS through the real worker and composes the production microscope minimap;
  this task did not add a manual production-design visual comparison.
- A different-file update intentionally detaches with `stop(0.0)`. An already
  running daemon backend may finish after detachment, while disconnected Qt
  delivery and worker stop guards prevent it from updating this widget. The
  same nonblocking detach policy applies to document removal and widget close.
