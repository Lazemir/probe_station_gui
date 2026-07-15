# Task 5 Report: KLayout microscope minimap

## Status

Implemented and verified on `codex/klayout-design-renderer` from base `0b4afd4`.

Commit subject: `feat: render design minimap with KLayout`

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
  dimensions.
- Acceptance checks request id, config generation, viewport generation,
  purpose, world box, dimensions, desired cache key, and detached `QImage`
  validity before creating the GUI-thread `QPixmap`.
- Same-path layer/top/rotation changes reuse the worker and invalidate the old
  configuration image. Different paths disconnect and call `stop(0.0)` before
  lazy replacement. Document removal and widget close call bounded
  `stop(0.5)`.
- A matching-configuration image remains available, scaled to the current
  minimap content rectangle, while a replacement size is pending. Images are
  drawn directly in screen space, so source row 0 remains the top row.
- The existing route, probe-route, mark, selected-point, FOV, and current
  position overlays remain native painter passes above the background. Minimap
  click/double-click code is unchanged.
- Non-file-backed documents retain the prior asynchronous polygon renderer.

## Verification

Focused minimap tests:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_microscope_view_minimap.py tests/ui/test_microscope_view_klayout_minimap.py -q
```

Result: `12 passed in 0.44s`.

Task 4 plus minimap integration slice:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_microscope_view_minimap.py tests/ui/test_microscope_view_klayout_minimap.py tests/ui/test_design_klayout_raster.py tests/ui/test_design_plot_klayout.py tests/ui/test_design_navigator_panel.py tests/app/test_main_design_navigation.py -q
```

Result: `46 passed in 3.63s`.

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

Result: `1426 passed, 10 subtests passed in 21.98s`.

## Files

- Modified `probe_station_gui/views/microscope_view.py`
- Added `tests/ui/test_microscope_view_klayout_minimap.py`
- Added `.superpowers/sdd/klayout-task-5-report.md`

## Self-review and concerns

- Checked each Task 5 requirement against the final diff; no missing required
  behavior or incidental route/minimap input change was found.
- No `main.py`, camera, serial, stage, or hardware-dependent path was run or
  modified.
- The KLayout minimap uses the same reviewed worker/backend covered by Task 3
  and Task 4. This task did not add a manual visual comparison on a production
  design file; automated coverage uses detached test frames plus the existing
  real-worker Task 4 raster tests.
- A different-file update intentionally detaches with `stop(0.0)`. An already
  running daemon backend may finish after detachment, while disconnected Qt
  delivery and worker stop guards prevent it from updating this widget.
