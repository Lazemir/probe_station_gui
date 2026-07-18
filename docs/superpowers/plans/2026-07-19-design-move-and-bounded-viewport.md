# Design Move and Bounded Viewport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent single-click Move tool and constrain Design Window navigation and KLayout snapping to finite, model-derived work.

**Architecture:** A new Qt-free navigation-bounds module owns content union, aspect fitting, and range clamping. `_DesignPlotPane` applies those results to pyqtgraph before the raster controller sees a range, while the existing Main-to-StageController path remains untouched. The snap pipeline gains a shared query admission plan, hard traversal budgets, and cooperative in-flight cancellation.

**Tech Stack:** Python 3.12, PySide6, pyqtgraph, KLayout Python API, pytest, existing `DesignDocument`/`MeasurementRoute`/`MarkupDocument` models.

## Global Constraints

- Run every Python command through `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe`; do not use bare `python`.
- Do not run hardware-dependent code or launch `main.py` in automation.
- Keep the GUI thread limited to pointer handling, ViewBox changes, and widget updates; KLayout traversal stays on its existing worker thread.
- Do not route in-process movement through the localhost API.
- Do not modify stage safety, precision-approach planning, registration mathematics, or route Pause/Resume/Interrupt semantics.
- Do not modify Select geometry, selection-rectangle composition, raster LOD, or the one-off polygon display artifact discussed during design.
- Do not add a visible `Fit All` command or a menu label containing an ellipsis.
- Opening and Home frame only GDS; internal maximum zoom/pan includes GDS plus every finite route and markup coordinate, including disabled route points and hidden markup.
- A Move click uses the existing `move_requested -> Main._move_to_design_coordinate -> StageController.request_move_to_xy` path.
- Move remains active until Escape or another tool is selected, but is disabled while a design load or route run is active.
- Snap limits are named constants or immutable request values; partial candidate sets must never produce a snap result.

---

## File Map

- Create `probe_station_gui/design/navigation_bounds.py`: pure content union, padding, aspect fitting, and view clamping.
- Create `tests/design/test_navigation_bounds.py`: pure bounds behavior.
- Modify `tests/ui/test_design_plot_klayout.py`: ViewBox limits, initial focus, Home, resize, dynamic content, and existing KLayout UI regressions.
- Modify `probe_station_gui/views/design_plot_pane.py`: apply navigation limits before rendering; add guarded snap fallback and Move pointer state.
- Modify `probe_station_gui/views/design_navigator_panel.py`: add Move toolbar/page, Home shortcut, and tool transitions.
- Modify `probe_station_gui/views/design_navigator_enablement.py`: central Move enablement policy.
- Modify `probe_station_gui/design/klayout_types.py`: immutable snap work budget and response diagnostics.
- Modify `probe_station_gui/design/klayout_geometry.py`: pure snap query admission plan.
- Modify `probe_station_gui/design/klayout_workers.py`: bounded streaming traversal and cooperative cancellation.
- Create `tests/design/test_klayout_snap_backend.py`: traversal budgets, cancellation checkpoints, and extreme-radius regression.
- Create `tests/ui/test_design_plot_move.py`: single click, drag, double click, persistence, and Escape.
- Modify `tests/design/test_klayout_geometry.py`, `tests/design/test_klayout_types.py`, `tests/design/test_klayout_workers.py`, `tests/design/test_click_navigation.py`, `tests/ui/test_design_plot_klayout.py`, `tests/ui/test_design_navigator_panel.py`, and `tests/ui/test_design_plot_selection.py`: adjacent regression coverage.

---

### Task 1: Pure Navigation Bounds

**Files:**
- Create: `probe_station_gui/design/navigation_bounds.py`
- Create: `tests/design/test_navigation_bounds.py`

**Interfaces:**
- Consumes: `DesignDocument.bounds`, `MeasurementRoute.points`, `MeasurementRoute.needle_hits_for_point()`, `MarkupDocument.guides`.
- Produces: `NavigationBounds`, `build_navigation_bounds()`, and `clamp_view_bounds()` for Task 2.

- [ ] **Step 1: Write failing pure bounds tests**

Create `tests/design/test_navigation_bounds.py` with focused model fixtures and these assertions:

```python
from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path

import pytest

from probe_station_gui.design.markup import GuideSegment, MarkupDocument
from probe_station_gui.design.navigation_bounds import (
    build_navigation_bounds,
    clamp_view_bounds,
    content_bounds,
    fit_bounds_to_aspect,
)
from probe_station_gui.route.model import (
    MeasurementRoute,
    NeedleOffset,
    RouteDesignBinding,
    RoutePoint,
)


def _route() -> MeasurementRoute:
    return MeasurementRoute(
        name="Route",
        design=RouteDesignBinding(
            path="chip.gds",
            sha256="hash",
            top_cell_name="TOP",
            bounds=(0.0, 0.0, 100.0, 50.0),
            dbu=0.001,
        ),
        points=[
            RoutePoint("p1", "P1", (20.0, 10.0), enabled=True),
            RoutePoint("p2", "P2", (240.0, -30.0), enabled=False),
        ],
        needle_offsets=[NeedleOffset("N1", "Needle 1", 5.0, -7.0)],
    )


def _hidden_markup(tmp_path: Path) -> MarkupDocument:
    source = tmp_path / "chip.gds"
    source.write_bytes(b"gds")
    return MarkupDocument.empty(source, visible=False).append_guide(
        (-80.0, 12.0),
        (10.0, 300.0),
        guide_id="far-guide",
    )


def test_content_union_includes_disabled_route_hits_and_hidden_markup(
    tmp_path: Path,
) -> None:
    bounds, ignored = content_bounds(
        (0.0, 0.0, 100.0, 50.0),
        _route(),
        _hidden_markup(tmp_path),
    )

    assert bounds == pytest.approx((-80.0, -37.0, 245.0, 300.0))
    assert ignored == 0


def test_nonfinite_content_is_ignored_and_counted(tmp_path: Path) -> None:
    route = _route()
    route.points.append(RoutePoint("bad", "Bad", (math.inf, 4.0)))

    bounds, ignored = content_bounds(
        (0.0, 0.0, 100.0, 50.0),
        route,
        _hidden_markup(tmp_path),
    )

    assert bounds == pytest.approx((-80.0, -37.0, 245.0, 300.0))
    assert ignored == 2  # invalid center and its invalid needle hit


def test_structurally_malformed_route_and_markup_are_ignored(tmp_path: Path) -> None:
    route = _route()
    route.points = [RoutePoint("bad", "Bad", (1.0,))]  # type: ignore[arg-type]
    markup = _hidden_markup(tmp_path)
    markup = replace(
        markup,
        guides=(GuideSegment("bad-guide", (1.0,), (2.0, 3.0)),),
    )

    bounds, ignored = content_bounds(
        (0.0, 0.0, 100.0, 50.0),
        route,
        markup,
    )

    assert bounds == (0.0, 0.0, 100.0, 50.0)
    assert ignored == 3  # invalid center, derived hit, and guide start


def test_gds_only_bounds_receive_proportional_padding() -> None:
    result = build_navigation_bounds(
        (0.0, 0.0, 100.0, 50.0),
        None,
        None,
        viewport_size=(400.0, 200.0),
    )

    assert result.content_bounds == (0.0, 0.0, 100.0, 50.0)
    assert result.frame == pytest.approx((-5.0, -2.5, 105.0, 52.5))


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((400.0, 200.0), (-5.0, -2.5, 105.0, 52.5)),
        ((200.0, 400.0), (-5.0, -85.0, 105.0, 135.0)),
    ],
)
def test_aspect_fit_expands_only_the_short_axis(size, expected) -> None:
    assert fit_bounds_to_aspect((-5.0, -2.5, 105.0, 52.5), size) == pytest.approx(
        expected
    )


def test_clamp_preserves_valid_view_and_translates_invalid_view() -> None:
    frame = (0.0, 0.0, 100.0, 50.0)
    assert clamp_view_bounds((10.0, 5.0, 40.0, 20.0), frame) == (
        10.0,
        5.0,
        40.0,
        20.0,
    )
    assert clamp_view_bounds((90.0, 40.0, 120.0, 55.0), frame) == pytest.approx(
        (70.0, 35.0, 100.0, 50.0)
    )


def test_clamp_reduces_oversized_view_without_changing_aspect() -> None:
    assert clamp_view_bounds((-50.0, -50.0, 150.0, 50.0), (0.0, 0.0, 100.0, 50.0)) == pytest.approx(
        (0.0, 0.0, 100.0, 50.0)
    )


def test_build_navigation_bounds_adds_named_padding(tmp_path: Path) -> None:
    result = build_navigation_bounds(
        (0.0, 0.0, 100.0, 50.0),
        _route(),
        _hidden_markup(tmp_path),
        viewport_size=(800.0, 600.0),
    )

    assert result.content_bounds == pytest.approx((-80.0, -37.0, 245.0, 300.0))
    assert result.frame[0] < result.content_bounds[0]
    assert result.frame[1] < result.content_bounds[1]
    assert result.frame[2] > result.content_bounds[2]
    assert result.frame[3] > result.content_bounds[3]
```

- [ ] **Step 2: Run the pure tests and confirm the missing-module failure**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_navigation_bounds.py -q
```

Expected: collection fails with `ModuleNotFoundError: probe_station_gui.design.navigation_bounds`.

- [ ] **Step 3: Implement the pure navigation module**

Create `probe_station_gui/design/navigation_bounds.py` with these public values and algorithms:

```python
"""Qt-free bounds used to constrain Design Window navigation."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import TypeAlias

from probe_station_gui.design.markup import MarkupDocument
from probe_station_gui.route.model import MeasurementRoute


Point2D: TypeAlias = tuple[float, float]
Box2D: TypeAlias = tuple[float, float, float, float]
CONTENT_PADDING_FRACTION = 0.05
GDS_FOCUS_PADDING_FRACTION = 0.02
VIEW_RANGE_ABS_TOLERANCE = 1e-9


@dataclass(frozen=True)
class NavigationBounds:
    content_bounds: Box2D
    frame: Box2D
    ignored_coordinate_count: int = 0


def content_bounds(
    gds_bounds: Box2D,
    route: MeasurementRoute | None,
    markup: MarkupDocument | None,
) -> tuple[Box2D, int]:
    left, bottom, right, top = _finite_box(gds_bounds)
    ignored = 0

    def include(point: object) -> Point2D | None:
        nonlocal left, bottom, right, top, ignored
        try:
            x_value, y_value = float(point[0]), float(point[1])
        except (IndexError, TypeError, ValueError):
            ignored += 1
            return None
        if not math.isfinite(x_value) or not math.isfinite(y_value):
            ignored += 1
            return None
        left = min(left, x_value)
        bottom = min(bottom, y_value)
        right = max(right, x_value)
        top = max(top, y_value)
        return (x_value, y_value)

    if route is not None:
        for route_point in route.points:
            center = include(route_point.camera_center)
            if center is None:
                ignored += len(route.needle_offsets[:2])
                continue
            for offset in route.needle_offsets[:2]:
                try:
                    hit = offset.apply_to(center)
                except (IndexError, TypeError, ValueError):
                    ignored += 1
                    continue
                include(hit)
    if markup is not None:
        for guide in markup.guides:
            include(guide.start)
            include(guide.end)
    return (left, bottom, right, top), ignored


def pad_bounds(bounds: Box2D, fraction: float) -> Box2D:
    left, bottom, right, top = _finite_box(bounds)
    fraction = float(fraction)
    if not math.isfinite(fraction) or fraction < 0.0:
        raise ValueError("Navigation padding must be finite and nonnegative.")
    width = right - left
    height = top - bottom
    x_margin = width * fraction
    y_margin = height * fraction
    return (
        left - x_margin,
        bottom - y_margin,
        right + x_margin,
        top + y_margin,
    )


def fit_bounds_to_aspect(bounds: Box2D, viewport_size: tuple[float, float]) -> Box2D:
    left, bottom, right, top = _finite_box(bounds)
    viewport_width, viewport_height = map(float, viewport_size)
    if viewport_width <= 0.0 or viewport_height <= 0.0:
        return bounds
    width = right - left
    height = top - bottom
    target_aspect = viewport_width / viewport_height
    center_x = (left + right) * 0.5
    center_y = (bottom + top) * 0.5
    if width / height < target_aspect:
        width = height * target_aspect
    else:
        height = width / target_aspect
    return (
        center_x - width * 0.5,
        center_y - height * 0.5,
        center_x + width * 0.5,
        center_y + height * 0.5,
    )


def navigation_frame(
    content: Box2D,
    viewport_size: tuple[float, float],
) -> Box2D:
    return fit_bounds_to_aspect(
        pad_bounds(content, CONTENT_PADDING_FRACTION),
        viewport_size,
    )


def build_navigation_bounds(
    gds_bounds: Box2D,
    route: MeasurementRoute | None,
    markup: MarkupDocument | None,
    *,
    viewport_size: tuple[float, float],
) -> NavigationBounds:
    content, ignored = content_bounds(gds_bounds, route, markup)
    return NavigationBounds(
        content_bounds=content,
        frame=navigation_frame(content, viewport_size),
        ignored_coordinate_count=ignored,
    )


def clamp_view_bounds(view: Box2D, frame: Box2D) -> Box2D:
    view_left, view_bottom, view_right, view_top = _finite_box(view)
    frame_left, frame_bottom, frame_right, frame_top = _finite_box(frame)
    view_width = view_right - view_left
    view_height = view_top - view_bottom
    frame_width = frame_right - frame_left
    frame_height = frame_top - frame_bottom
    scale = min(1.0, frame_width / view_width, frame_height / view_height)
    width = view_width * scale
    height = view_height * scale
    center_x = min(
        frame_right - width * 0.5,
        max(frame_left + width * 0.5, (view_left + view_right) * 0.5),
    )
    center_y = min(
        frame_top - height * 0.5,
        max(frame_bottom + height * 0.5, (view_bottom + view_top) * 0.5),
    )
    return (
        center_x - width * 0.5,
        center_y - height * 0.5,
        center_x + width * 0.5,
        center_y + height * 0.5,
    )


def _finite_box(bounds: Box2D) -> Box2D:
    values = tuple(float(value) for value in bounds)
    if len(values) != 4 or not all(math.isfinite(value) for value in values):
        raise ValueError("Navigation bounds must contain four finite values.")
    left, bottom, right, top = values
    if right <= left or top <= bottom:
        raise ValueError("Navigation bounds must have positive width and height.")
    return left, bottom, right, top


__all__ = [
    "Box2D",
    "CONTENT_PADDING_FRACTION",
    "GDS_FOCUS_PADDING_FRACTION",
    "NavigationBounds",
    "VIEW_RANGE_ABS_TOLERANCE",
    "build_navigation_bounds",
    "clamp_view_bounds",
    "content_bounds",
    "fit_bounds_to_aspect",
    "navigation_frame",
    "pad_bounds",
]
```

- [ ] **Step 4: Run the pure bounds tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_navigation_bounds.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit the pure bounds deliverable**

```powershell
git add probe_station_gui/design/navigation_bounds.py tests/design/test_navigation_bounds.py
git commit -m "feat: model bounded design navigation"
```

---

### Task 2: Apply Dynamic ViewBox Limits Before Rendering

**Files:**
- Modify: `probe_station_gui/views/design_plot_pane.py:481-507, 645-845, 1039-1071`
- Modify: `probe_station_gui/views/design_navigator_panel.py:2127-2140`
- Modify: `tests/ui/test_design_plot_klayout.py:1-205, 760-790`

**Interfaces:**
- Consumes: `content_bounds()`, `navigation_frame()`, `clamp_view_bounds()`, `GDS_FOCUS_PADDING_FRACTION` from Task 1.
- Produces: `_DesignPlotPane.focus_gds_bounds()` and constrained ViewBox state used by Task 3 snap admission.

- [ ] **Step 1: Write failing ViewBox integration tests**

Extend the existing `tests/ui/test_design_plot_klayout.py`, which already owns the executable `_RasterController`, `_SnapWorker`, `qt_app`, `pane`, and `_document()` helpers. Add these imports and fixtures in that module:

```python
from dataclasses import replace
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QAction, QWheelEvent
from PySide6.QtWidgets import QAbstractButton, QApplication

from probe_station_gui.design.markup import GuideSegment, MarkupDocument
from probe_station_gui.design.navigation_bounds import (
    GDS_FOCUS_PADDING_FRACTION,
    fit_bounds_to_aspect,
    pad_bounds,
)
from probe_station_gui.route.model import MeasurementRoute, NeedleOffset, RoutePoint


@pytest.fixture
def document(tmp_path: Path) -> DesignDocument:
    source = tmp_path / "layout.gds"
    source.write_bytes(b"gds")
    return DesignDocument(
        path=source,
        library=None,
        top_cell=None,
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-6,
        user_unit=1e-9,
        bounds=(0.0, 0.0, 100.0, 50.0),
        polygons_by_layer={},
        visible_layers=frozenset({(1, 0)}),
        file_backed=True,
        available_layers=frozenset({(1, 0)}),
        cell_bounds={"TOP": (0.0, 0.0, 100.0, 50.0)},
    )


@pytest.fixture
def window(monkeypatch, qt_app: QApplication):
    monkeypatch.setattr(plot_module, "KLayoutRasterController", _RasterController)
    monkeypatch.setattr(plot_module, "KLayoutSnapWorker", _SnapWorker)
    widget = DesignLayoutWindow()
    yield widget
    widget._main_view.shutdown()
    widget.deleteLater()


@pytest.fixture
def distant_hidden_markup(document: DesignDocument) -> MarkupDocument:
    return MarkupDocument.empty(document.path, visible=False).append_guide(
        (1_000_000.0, 0.0),
        (1_000_100.0, 100.0),
        guide_id="distant-guide",
    )


@pytest.fixture
def distant_route(document: DesignDocument) -> MeasurementRoute:
    route = MeasurementRoute.default_for_document(document)
    route.points.append(
        RoutePoint("far", "Far", (1_000_000.0, 0.0), enabled=False)
    )
    return route


def _view_box(pane):
    return pane._plot.getViewBox()


def _box(pane):
    x_range, y_range = _view_box(pane).viewRange()
    return (x_range[0], y_range[0], x_range[1], y_range[1])


def test_open_frames_gds_but_internal_limits_include_distant_hidden_markup(
    pane, document, distant_hidden_markup
) -> None:
    pane.set_document(document)
    pane.set_markup(distant_hidden_markup)

    visible = _box(pane)
    expected = fit_bounds_to_aspect(
        pad_bounds(document.bounds, GDS_FOCUS_PADDING_FRACTION),
        pane._viewport_size(),
    )
    assert visible == pytest.approx(expected)
    assert pane._navigation_frame[2] > 1_000_000.0


def test_route_and_markup_updates_expand_limits_without_changing_view(
    pane, document, distant_route, distant_hidden_markup
) -> None:
    pane.set_document(document)
    before = _box(pane)

    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    pane.set_markup(distant_hidden_markup)

    assert _box(pane) == pytest.approx(before)
    assert pane._navigation_frame[2] > 1_000_000.0


def test_deleting_outer_content_shrinks_and_clamps_once(
    pane, document, distant_route
) -> None:
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    _view_box(pane).setRange(
        xRange=(999_900.0, 1_000_100.0),
        yRange=(-100.0, 100.0),
        padding=0.0,
    )

    pane.set_probe_route(None, selected_route_point_index=-1)

    assert pane._navigation_frame[2] < 1_000.0
    assert _box(pane)[2] <= pane._navigation_frame[2]


def test_shrink_preserves_an_already_valid_view(
    pane, document, distant_route
) -> None:
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    _view_box(pane).setRange(
        xRange=(10.0, 40.0),
        yRange=(5.0, 20.0),
        padding=0.0,
    )
    before = _box(pane)

    pane.set_probe_route(None, selected_route_point_index=-1)

    assert _box(pane) == pytest.approx(before)


def test_home_restores_gds_without_changing_content_limits(
    window, document, distant_route
) -> None:
    window.set_document(document)
    window.set_probe_route(distant_route, selected_route_point_index=-1)
    frame = window._main_view._navigation_frame
    window._home_shortcut.activated.emit()

    assert window._main_view._navigation_frame == frame
    expected = fit_bounds_to_aspect(
        pad_bounds(document.bounds, GDS_FOCUS_PADDING_FRACTION),
        window._main_view._viewport_size(),
    )
    assert _box(window._main_view) == pytest.approx(expected)


def test_needle_offset_update_expands_then_shrinks_navigation_frame(
    pane, document
) -> None:
    route = MeasurementRoute.default_for_document(document)
    route.points.append(RoutePoint("local", "Local", (20.0, 20.0)))
    pane.set_document(document)
    pane.set_probe_route(route, selected_route_point_index=-1)
    original = pane._navigation_frame

    route.needle_offsets = [
        NeedleOffset("N1", "Needle 1", 2_000_000.0, 0.0)
    ]
    pane.set_probe_route(route, selected_route_point_index=-1)
    assert pane._navigation_frame[2] > 2_000_000.0

    route.needle_offsets = [NeedleOffset("N1", "Needle 1", 0.0, 0.0)]
    pane.set_probe_route(route, selected_route_point_index=-1)
    assert pane._navigation_frame == pytest.approx(original)


def test_rotated_document_ignores_stale_content_until_models_are_refreshed(
    pane, document, distant_route, distant_hidden_markup
) -> None:
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    pane.set_markup(distant_hidden_markup)
    assert pane._navigation_frame[2] > 1_000_000.0

    rotated = replace(
        document,
        bounds=(0.0, 0.0, 50.0, 100.0),
        rotation_quarter_turns=1,
        source_load_id="rotated-load",
    )
    pane.set_document(rotated)
    assert pane._navigation_frame[2] < 1_000.0

    rotated_route = MeasurementRoute.default_for_document(rotated)
    rotated_route.points.append(RoutePoint("far", "Far", (0.0, 1_000_000.0)))
    rotated_markup = replace(
        distant_hidden_markup,
        guides=(
            GuideSegment(
                "rotated-guide",
                (0.0, 1_000_000.0),
                (-100.0, 1_000_100.0),
            ),
        ),
    )
    pane.set_probe_route(rotated_route, selected_route_point_index=-1)
    pane.set_markup(rotated_markup)
    assert pane._navigation_frame[3] > 1_000_000.0


def test_design_window_exposes_no_fit_all_control_or_action(window) -> None:
    button_texts = {
        button.text().replace("&", "")
        for button in window.findChildren(QAbstractButton)
    }
    action_texts = {
        action.text().replace("&", "")
        for action in window.findChildren(QAction)
    }
    assert all("fit all" not in text.casefold() for text in button_texts)
    assert all("fit all" not in text.casefold() for text in action_texts)


def test_file_backed_renderer_receives_gds_focused_range_on_first_request(
    pane, document
) -> None:
    pane.set_document(document)

    first_range = pane._raster_controller.ranges_seen_on_set_document[0]
    assert first_range == pytest.approx(_box(pane))
    assert abs(first_range[2] - first_range[0]) < 2.0 * (
        document.bounds[2] - document.bounds[0]
    )


@pytest.mark.parametrize(
    ("dx", "dy"),
    [
        (-1_000_000.0, 0.0),
        (1_000_000.0, 0.0),
        (0.0, -1_000_000.0),
        (0.0, 1_000_000.0),
    ],
)
def test_viewbox_cannot_pan_past_any_navigation_edge(
    pane, document, qt_app, dx, dy
) -> None:
    pane.set_document(document)
    frame = pane._navigation_frame
    view_box = _view_box(pane)
    view_box.setRange(
        xRange=(20.0, 80.0),
        yRange=(10.0, 40.0),
        padding=0.0,
    )
    view_box.translateBy(x=dx, y=dy)
    qt_app.processEvents()

    visible = _box(pane)
    assert visible[0] >= frame[0] - 1e-6
    assert visible[1] >= frame[1] - 1e-6
    assert visible[2] <= frame[2] + 1e-6
    assert visible[3] <= frame[3] + 1e-6


def test_wheel_zoom_out_stops_at_navigation_frame(pane, document, qt_app) -> None:
    pane.set_document(document)
    frame = pane._navigation_frame
    viewport = pane._plot.viewport()
    center = viewport.rect().center()
    wheel = QWheelEvent(
        QPointF(center),
        QPointF(viewport.mapToGlobal(center)),
        QPoint(),
        QPoint(0, -12_000),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.ScrollUpdate,
        False,
    )

    QApplication.sendEvent(viewport, wheel)
    qt_app.processEvents()

    assert _box(pane) == pytest.approx(frame)


def test_resize_recomputes_aspect_frame_without_refocusing(
    pane, document, distant_route, qt_app
) -> None:
    pane.resize(800, 600)
    pane.show()
    qt_app.processEvents()
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    _view_box(pane).setRange(
        xRange=(1_000.0, 1_100.0),
        yRange=(-25.0, 25.0),
        padding=0.0,
    )
    before = _box(pane)
    before_center = ((before[0] + before[2]) * 0.5, (before[1] + before[3]) * 0.5)

    pane.resize(900, 300)
    qt_app.processEvents()

    frame = pane._navigation_frame
    after = _box(pane)
    after_center = ((after[0] + after[2]) * 0.5, (after[1] + after[3]) * 0.5)
    viewport_width, viewport_height = pane._viewport_size()
    frame_aspect = (frame[2] - frame[0]) / (frame[3] - frame[1])
    assert frame_aspect == pytest.approx(
        viewport_width / viewport_height,
        rel=0.05,
    )
    assert after_center == pytest.approx(before_center)


def test_resize_refits_cached_content_without_rescanning_models(
    pane, document, distant_route, qt_app, monkeypatch
) -> None:
    real_content_bounds = plot_module.content_bounds
    calls = 0

    def counted_content_bounds(*args, **kwargs):
        nonlocal calls
        calls += 1
        return real_content_bounds(*args, **kwargs)

    monkeypatch.setattr(plot_module, "content_bounds", counted_content_bounds)
    pane.set_document(document)
    pane.set_probe_route(distant_route, selected_route_point_index=-1)
    scans_before_resize = calls

    pane.resize(900, 300)
    qt_app.processEvents()

    assert calls == scans_before_resize
```

Extend the existing `_RasterController.__init__()` with `self._view_box = _args[0]` and `self.ranges_seen_on_set_document = []`. At the start of `set_document()`, append `(x_range[0], y_range[0], x_range[1], y_range[1])` from `self._view_box.viewRange()`. No fixture is copied to another module; every viewport test above runs against the existing `pane` fixture or the complete `window` fixture shown above.

- [ ] **Step 2: Run the ViewBox tests and verify they fail**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_plot_klayout.py -q
```

Expected: failures show missing `_navigation_frame`, `focus_gds_bounds`, and `_home_shortcut`, plus the renderer-order assertion fails.

- [ ] **Step 3: Integrate limits into `_DesignPlotPane`**

In `probe_station_gui/views/design_plot_pane.py`, add `import os`, import the Task 1 helpers, store `self._navigation_content_bounds: Box2D | None`, `self._navigation_frame: Box2D | None`, `self._navigation_ignored_coordinate_count = 0`, `self._last_navigation_invalid_coordinate_count = 0`, and independent route/markup document keys. Add a no-I/O identity helper based on normalized absolute path, top-cell name, quarter-turn rotation, bounds, and `source_load_id`:

```python
DesignContentKey = tuple[str, str, int, Box2D, str | None]


def _design_content_key(document: DesignDocument) -> DesignContentKey:
    return (
        os.path.normcase(os.path.abspath(os.fspath(document.path))),
        str(document.top_cell_name),
        int(document.rotation_quarter_turns) % 4,
        tuple(float(value) for value in document.bounds),
        document.source_load_id,
    )


def _route_matches_document(
    route: MeasurementRoute,
    document: DesignDocument,
) -> bool:
    same_path = os.path.normcase(os.path.abspath(route.design.path)) == os.path.normcase(
        os.path.abspath(os.fspath(document.path))
    )
    return (
        same_path
        and route.design.top_cell_name == str(document.top_cell_name)
        and all(
            math.isclose(route_value, document_value, rel_tol=0.0, abs_tol=1e-9)
            for route_value, document_value in zip(
                route.design.bounds,
                document.bounds,
                strict=True,
            )
        )
        and math.isclose(route.design.dbu, document.dbu, rel_tol=0.0, abs_tol=1e-12)
    )


def _markup_matches_document(
    markup: MarkupDocument,
    document: DesignDocument,
) -> bool:
    return markup.source_path == os.path.normcase(
        os.path.abspath(os.fspath(document.path))
    )


def _viewport_size(self) -> tuple[float, float]:
    if self._plot is None:
        return (1.0, 1.0)
    rect = self._plot.getViewBox().sceneBoundingRect()
    return (max(1.0, float(rect.width())), max(1.0, float(rect.height())))


def _current_view_bounds(self) -> Box2D | None:
    if self._plot is None:
        return None
    try:
        x_range, y_range = self._plot.getViewBox().viewRange()[:2]
        values = (
            float(x_range[0]),
            float(y_range[0]),
            float(x_range[1]),
            float(y_range[1]),
        )
    except (AttributeError, IndexError, TypeError, ValueError):
        return None
    return values if all(math.isfinite(value) for value in values) else None


def _set_view_bounds(self, bounds: Box2D) -> None:
    if self._plot is None:
        return
    left, bottom, right, top = bounds
    self._plot.getViewBox().setRange(
        xRange=(left, right),
        yRange=(bottom, top),
        padding=0.0,
    )


def _clear_navigation_limits(self) -> None:
    self._navigation_content_bounds = None
    self._navigation_frame = None
    self._navigation_ignored_coordinate_count = 0
    self._last_navigation_invalid_coordinate_count = 0
    if self._plot is not None:
        self._plot.getViewBox().setLimits(
            xMin=None,
            xMax=None,
            yMin=None,
            yMax=None,
            maxXRange=None,
            maxYRange=None,
        )


def _update_navigation_limits(
    self,
    *,
    recompute_content: bool = False,
    focus_gds: bool = False,
) -> None:
    if self._plot is None or self._document is None:
        self._clear_navigation_limits()
        return
    if recompute_content or self._navigation_content_bounds is None:
        identity = _design_content_key(self._document)
        route = (
            self._probe_route
            if not self._document_preview_active
            and self._probe_route_document_key == identity
            else None
        )
        markup = (
            self._markup
            if not self._document_preview_active
            and self._markup_document_key == identity
            else None
        )
        (
            self._navigation_content_bounds,
            self._navigation_ignored_coordinate_count,
        ) = content_bounds(self._document.bounds, route, markup)
    cached_content = self._navigation_content_bounds
    if cached_content is None:
        return
    self._navigation_frame = navigation_frame(
        cached_content,
        self._viewport_size(),
    )
    left, bottom, right, top = self._navigation_frame
    view_box = self._plot.getViewBox()
    view_box.setLimits(
        xMin=left,
        xMax=right,
        yMin=bottom,
        yMax=top,
        maxXRange=right - left,
        maxYRange=top - bottom,
    )
    if (
        self._navigation_ignored_coordinate_count
        and self._navigation_ignored_coordinate_count
        != self._last_navigation_invalid_coordinate_count
    ):
        logger.warning(
            "Ignored %d invalid design navigation coordinates.",
            self._navigation_ignored_coordinate_count,
        )
    self._last_navigation_invalid_coordinate_count = (
        self._navigation_ignored_coordinate_count
    )
    if focus_gds:
        self.focus_gds_bounds()
        return
    current = self._current_view_bounds()
    if current is not None:
        clamped = clamp_view_bounds(current, self._navigation_frame)
        if any(
            abs(a - b) > VIEW_RANGE_ABS_TOLERANCE
            for a, b in zip(current, clamped, strict=True)
        ):
            self._set_view_bounds(clamped)


def focus_gds_bounds(self) -> None:
    if self._document is None:
        return
    focused = fit_bounds_to_aspect(
        pad_bounds(self._document.bounds, GDS_FOCUS_PADDING_FRACTION),
        self._viewport_size(),
    )
    if self._navigation_frame is not None:
        focused = clamp_view_bounds(focused, self._navigation_frame)
    self._set_view_bounds(focused)
```

Make these call-site changes in the same file:

```python
# set_document(): before _configure_file_backed_document() can call
# KLayoutRasterController.set_document()/request_exact().
if document is None:
    self._clear_navigation_limits()
elif not same_document:
    self._update_navigation_limits(recompute_content=True, focus_gds=True)
    if document.file_backed:
        self._configure_file_backed_document(document)

# _redraw_document(): remove both unconditional focus_bounds() calls.
# set_probe_route(): after assigning model state.
self._probe_route_document_key = (
    _design_content_key(self._document)
    if self._document is not None
    and route is not None
    and _route_matches_document(route, self._document)
    else None
)
self._update_navigation_limits(recompute_content=True)

# set_markup(): after assigning model state.
self._markup_document_key = (
    _design_content_key(self._document)
    if self._document is not None
    and markup is not None
    and _markup_matches_document(markup, self._document)
    else None
)
self._update_navigation_limits(recompute_content=True)

# resizeEvent(): after super and route redraw scheduling.
self._update_navigation_limits()
```

Preserve preview behavior by applying candidate GDS-only limits in `set_document_preview()` and rebuilding the restored document envelope before restoring its saved range in `finish_document_preview()`. Call `_update_navigation_limits(recompute_content=True)` unconditionally after `finish_document_preview().set_document(document)`, because accepting a preview can leave `document is self._document` while the preview flag changes from true to false. Resize calls leave `recompute_content=False`, making them O(1) in route/markup size.

- [ ] **Step 4: Add the Home shortcut**

In `DesignLayoutWindow.__init__` next to the existing Escape shortcut:

```python
self._home_shortcut = QShortcut(QKeySequence("Home"), self)
self._home_shortcut.setContext(Qt.WindowShortcut)
self._home_shortcut.activated.connect(self._main_view.focus_gds_bounds)
```

Do not add a toolbar or menu action for content-wide fitting.

- [ ] **Step 5: Run bounds and ViewBox tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_navigation_bounds.py tests/ui/test_design_plot_klayout.py -q
```

Expected: all tests pass; the first file-backed render observes the constrained GDS view.

- [ ] **Step 6: Commit ViewBox integration**

```powershell
git add probe_station_gui/views/design_plot_pane.py probe_station_gui/views/design_navigator_panel.py tests/ui/test_design_plot_klayout.py
git commit -m "feat: bound design viewport to content"
```

---

### Task 3: Bound and Cancel KLayout Snap Work

**Files:**
- Modify: `probe_station_gui/design/klayout_types.py:78-107`
- Modify: `probe_station_gui/design/klayout_geometry.py`
- Modify: `probe_station_gui/design/klayout_workers.py:41-47, 246-433, 549-707`
- Modify: `probe_station_gui/views/design_plot_pane.py:889-897, 1798-1849, 1985-2045`
- Modify: `tests/design/test_klayout_types.py`
- Modify: `tests/design/test_klayout_geometry.py`
- Modify: `tests/design/test_klayout_workers.py`
- Create: `tests/design/test_klayout_snap_backend.py`
- Modify: `tests/ui/test_design_plot_klayout.py`

**Interfaces:**
- Consumes: constrained current viewport from Task 2 and existing `KLayoutConfig.display_bounds/source_bounds`.
- Produces: `SnapWorkBudget`, `SnapSearchPlan`, `plan_snap_search()`, bounded `_KLayoutSnapBackend.snap(request, is_cancelled=callback)`, and immediate markup/free fallback for Task 4 Move clicks.

- [ ] **Step 1: Write failing query-admission and request-budget tests**

In `tests/design/test_klayout_geometry.py`, add `import math` and:

```python
from probe_station_gui.design.klayout_geometry import plan_snap_search
from probe_station_gui.design.klayout_types import (
    SNAP_UNAVAILABLE_EXCESSIVE_COVERAGE,
    SNAP_UNAVAILABLE_INVALID,
    SNAP_UNAVAILABLE_OUTSIDE_BOUNDS,
)


def test_snap_search_rejects_nonfinite_and_outside_queries() -> None:
    invalid = plan_snap_search((math.inf, 1.0), 2.0, (0.0, 0.0, 100.0, 100.0))
    outside = plan_snap_search((1_000.0, 1_000.0), 2.0, (0.0, 0.0, 100.0, 100.0))
    assert invalid.skip_reason == SNAP_UNAVAILABLE_INVALID
    assert outside.skip_reason == SNAP_UNAVAILABLE_OUTSIDE_BOUNDS


def test_snap_search_rejects_excessive_gds_coverage() -> None:
    plan = plan_snap_search(
        (50.0, 50.0),
        10_000_000.0,
        (0.0, 0.0, 100.0, 100.0),
    )
    assert plan.skip_reason == SNAP_UNAVAILABLE_EXCESSIVE_COVERAGE
    assert plan.coverage_fraction == pytest.approx(1.0)


def test_snap_search_keeps_normal_local_query_and_clips_edge() -> None:
    plan = plan_snap_search((1.0, 50.0), 4.0, (0.0, 0.0, 100.0, 100.0))
    assert plan.skip_reason is None
    assert plan.search_box == pytest.approx((0.0, 46.0, 5.0, 54.0))


```

In `tests/design/test_klayout_types.py`, import `SnapWorkBudget` and add:

```python
def test_snap_request_owns_immutable_default_budget() -> None:
    request = SnapRequest(1, _config(), (1.0, 2.0), 0.5)
    assert request.budget == SnapWorkBudget(
        max_shapes=4_000,
        max_candidates=40_000,
        max_elapsed_ms=50.0,
    )
```

- [ ] **Step 2: Write failing worker/backend tests**

Update every `def snap` test double in `tests/design/test_klayout_workers.py` -- the harness backend, failure backend, blocking backend, and inline lifecycle backends -- to accept `*, is_cancelled`. Do not change their intended success/failure behavior except where a cancellation assertion is explicit. Then add:

```python
def test_newer_hover_cooperatively_cancels_inflight_hover(qt_app) -> None:
    entered = threading.Event()
    cancelled = threading.Event()
    calls: list[int] = []

    class Backend:
        def ensure_config(self, _config) -> None:
            pass

        def snap(self, request, *, is_cancelled):
            calls.append(request.request_id)
            if request.request_id == 1:
                entered.set()
                assert cancelled.wait(1.0)
                assert is_cancelled()
            return _response(request)

        def close(self) -> None:
            pass

    worker = KLayoutSnapWorker(backend_factory=Backend)
    responses: list[SnapResponse] = []
    worker.snap_ready.connect(responses.append, Qt.ConnectionType.DirectConnection)
    worker.submit_hover(_snap_request(1))
    assert entered.wait(1.0)
    worker.submit_hover(_snap_request(2))
    cancelled.set()
    _process_until(qt_app, lambda: [item.request_id for item in responses] == [2])
    worker.stop()
    assert calls == [1, 2]


def test_click_preempts_hover_without_cancelling_fifo_clicks(qt_app) -> None:
    harness = _SnapHarness(block_ids={1})
    worker = KLayoutSnapWorker(backend_factory=harness.factory)
    responses: list[SnapResponse] = []
    worker.snap_ready.connect(responses.append, Qt.ConnectionType.DirectConnection)

    worker.submit_hover(_snap_request(1))
    assert harness.entered.wait(1.0)
    worker.submit_click(_snap_request(2, purpose="click"))
    worker.submit_click(_snap_request(3, purpose="click"))
    worker.submit_hover(_snap_request(4))
    harness.release.set()
    _process_until(
        qt_app,
        lambda: [response.request_id for response in responses] == [2, 3, 4],
    )
    worker.stop()

    assert [request_id for request_id, _thread_id in harness.snap_calls] == [1, 2, 3, 4]
    assert [response.request_id for response in responses] == [2, 3, 4]
```

Change `_SnapHarness.Backend.snap()` to the exact callback-aware signature, append the request id as it does now, and return `_response(request)` when cancellation is observed. Create `tests/design/test_klayout_snap_backend.py` with this complete deterministic harness and regression set:

```python
from __future__ import annotations

from pathlib import Path
import time

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    SNAP_UNAVAILABLE_CANCELLED,
    SNAP_UNAVAILABLE_CANDIDATE_BUDGET,
    SNAP_UNAVAILABLE_EXCESSIVE_COVERAGE,
    SNAP_UNAVAILABLE_OUTSIDE_BOUNDS,
    SNAP_UNAVAILABLE_SHAPE_BUDGET,
    SNAP_UNAVAILABLE_TIME_BUDGET,
    SnapRequest,
    SnapWorkBudget,
)
from probe_station_gui.design.klayout_workers import (
    _KLayoutSnapBackend,
    _collect_snap_geometry,
)


def _config() -> KLayoutConfig:
    return KLayoutConfig(
        path=Path("layout.gds"),
        top_cell_name="TOP",
        visible_layers=frozenset({(1, 0)}),
        source_bounds=(0.0, 0.0, 100.0, 100.0),
        display_bounds=(0.0, 0.0, 100.0, 100.0),
        rotation_quarter_turns=0,
        generation=1,
    )


def _shape(points, *, closed: bool = True):
    return ((tuple(points), closed),)


def _budget(*, shapes=100, candidates=100, elapsed_ms=1_000.0):
    return SnapWorkBudget(shapes, candidates, elapsed_ms)


def _assert_abort(result, reason: str) -> None:
    assert result.unavailable_reason == reason
    assert result.vertices == ()
    assert result.segments == ()


def _assert_following_normal_request_runs() -> None:
    result = _collect_snap_geometry(
        [_shape([(0.0, 0.0), (1.0, 0.0)], closed=False)],
        _budget(),
        clock=time.perf_counter,
        is_cancelled=lambda: False,
    )
    assert result.unavailable_reason is None
    assert result.vertices == ((0.0, 0.0), (1.0, 0.0))
    assert result.segments == (((0.0, 0.0), (1.0, 0.0)),)


def test_shape_budget_discards_partial_snap() -> None:
    result = _collect_snap_geometry(
        [_shape([(0.0, 0.0)]), _shape([(1.0, 0.0)])],
        _budget(shapes=1),
        clock=time.perf_counter,
        is_cancelled=lambda: False,
    )
    _assert_abort(result, SNAP_UNAVAILABLE_SHAPE_BUDGET)
    _assert_following_normal_request_runs()


def test_candidate_budget_stops_inside_one_huge_contour() -> None:
    result = _collect_snap_geometry(
        [_shape((float(index), 0.0) for index in range(100))],
        _budget(candidates=3),
        clock=time.perf_counter,
        is_cancelled=lambda: False,
    )
    _assert_abort(result, SNAP_UNAVAILABLE_CANDIDATE_BUDGET)
    _assert_following_normal_request_runs()


class _StepClock:
    def __init__(self, step: float) -> None:
        self.value = 0.0
        self.step = step

    def __call__(self) -> float:
        self.value += self.step
        return self.value


def test_elapsed_budget_discards_partial_snap() -> None:
    result = _collect_snap_geometry(
        [_shape([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)])],
        _budget(elapsed_ms=0.5),
        clock=_StepClock(0.0002),
        is_cancelled=lambda: False,
    )
    _assert_abort(result, SNAP_UNAVAILABLE_TIME_BUDGET)
    _assert_following_normal_request_runs()


def test_cancellation_discards_partial_snap() -> None:
    checks = 0

    def is_cancelled() -> bool:
        nonlocal checks
        checks += 1
        return checks >= 3

    result = _collect_snap_geometry(
        [_shape([(0.0, 0.0), (1.0, 0.0), (2.0, 0.0)])],
        _budget(),
        clock=time.perf_counter,
        is_cancelled=is_cancelled,
    )
    _assert_abort(result, SNAP_UNAVAILABLE_CANCELLED)
    _assert_following_normal_request_runs()


def _backend_with_counted_stream():
    calls: list[object] = []
    backend = _KLayoutSnapBackend(clock=time.perf_counter)

    def stream(_config, source_box):
        calls.append(source_box)
        return iter([_shape([(1.0, 1.0), (2.0, 1.0)], closed=False)])

    backend._iter_shape_contours = stream
    return backend, calls


def test_outside_query_does_not_construct_iterator() -> None:
    backend, calls = _backend_with_counted_stream()
    response = backend.snap(
        SnapRequest(1, _config(), (1_000.0, 1_000.0), 2.0),
        is_cancelled=lambda: False,
    )
    assert response.unavailable_reason == SNAP_UNAVAILABLE_OUTSIDE_BOUNDS
    assert response.result.mode == "free"
    assert response.result.segment_start is None
    assert response.result.segment_end is None
    assert calls == []


def test_extreme_radius_is_rejected_before_recursive_traversal_and_normal_request_runs() -> None:
    backend, calls = _backend_with_counted_stream()
    started = time.perf_counter()
    extreme = backend.snap(
        SnapRequest(1, _config(), (50.0, 50.0), 10_000_000.0),
        is_cancelled=lambda: False,
    )
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1
    assert extreme.unavailable_reason == SNAP_UNAVAILABLE_EXCESSIVE_COVERAGE
    assert extreme.result.mode == "free"
    assert calls == []

    normal = backend.snap(
        SnapRequest(2, _config(), (1.0, 1.0), 0.5),
        is_cancelled=lambda: False,
    )
    assert normal.unavailable_reason is None
    assert normal.result.mode == "vertex"
    assert normal.result.point == (1.0, 1.0)
    assert len(calls) == 1
```

- [ ] **Step 3: Run the new snap tests and verify failures**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_klayout_types.py tests/design/test_klayout_geometry.py tests/design/test_klayout_workers.py tests/design/test_klayout_snap_backend.py -q
```

Expected: failures identify missing budget fields, query plan, callback signature, and backend abort diagnostics.

- [ ] **Step 4: Add immutable snap limits and query admission**

In `probe_station_gui/design/klayout_types.py` add `import math` and:

```python
SNAP_UNAVAILABLE_INVALID = "invalid"
SNAP_UNAVAILABLE_OUTSIDE_BOUNDS = "outside_bounds"
SNAP_UNAVAILABLE_EXCESSIVE_COVERAGE = "excessive_coverage"
SNAP_UNAVAILABLE_CANCELLED = "cancelled"
SNAP_UNAVAILABLE_SHAPE_BUDGET = "shape_budget"
SNAP_UNAVAILABLE_CANDIDATE_BUDGET = "candidate_budget"
SNAP_UNAVAILABLE_TIME_BUDGET = "time_budget"


@dataclass(frozen=True)
class SnapWorkBudget:
    max_shapes: int = 4_000
    max_candidates: int = 40_000
    max_elapsed_ms: float = 50.0

    def __post_init__(self) -> None:
        if self.max_shapes <= 0 or self.max_candidates <= 0:
            raise ValueError("Snap count budgets must be positive.")
        if not math.isfinite(self.max_elapsed_ms) or self.max_elapsed_ms <= 0.0:
            raise ValueError("Snap time budget must be finite and positive.")


DEFAULT_SNAP_WORK_BUDGET = SnapWorkBudget()


@dataclass(frozen=True)
class SnapRequest:
    request_id: int
    config: KLayoutConfig
    point: Point2D
    radius: float
    purpose: str = "hover"
    budget: SnapWorkBudget = DEFAULT_SNAP_WORK_BUDGET


@dataclass(frozen=True)
class SnapResponse:
    request_id: int
    config_generation: int
    raw_point: Point2D
    result: SnapResult
    elapsed_ms: float
    shapes_inspected: int
    purpose: str = "hover"
    candidates_generated: int = 0
    unavailable_reason: str | None = None
```

Add `SnapWorkBudget`, `DEFAULT_SNAP_WORK_BUDGET`, and every `SNAP_UNAVAILABLE_*` constant above to `klayout_types.__all__`; existing exports remain.

In `probe_station_gui/design/klayout_geometry.py`, add `from dataclasses import dataclass`, extend the `klayout_types` import with `Box2D`, `SNAP_UNAVAILABLE_EXCESSIVE_COVERAGE`, `SNAP_UNAVAILABLE_INVALID`, and `SNAP_UNAVAILABLE_OUTSIDE_BOUNDS`, then add this complete admission contract:

```python
MAX_FILE_SNAP_GDS_FRACTION = 0.05


@dataclass(frozen=True)
class SnapSearchPlan:
    search_box: Box2D | None
    coverage_fraction: float
    skip_reason: str | None = None

    @property
    def accepted(self) -> bool:
        return self.skip_reason is None and self.search_box is not None


def plan_snap_search(
    point: Point2D,
    radius: float,
    gds_bounds: Box2D,
    *,
    max_coverage_fraction: float = MAX_FILE_SNAP_GDS_FRACTION,
) -> SnapSearchPlan:
    try:
        x_value, y_value = float(point[0]), float(point[1])
        radius_value = float(radius)
        left, bottom, right, top = (float(value) for value in gds_bounds)
    except (IndexError, TypeError, ValueError):
        return SnapSearchPlan(None, 0.0, SNAP_UNAVAILABLE_INVALID)
    values = (x_value, y_value, radius_value, left, bottom, right, top)
    if (
        not all(math.isfinite(value) for value in values)
        or radius_value < 0.0
        or right <= left
        or top <= bottom
    ):
        return SnapSearchPlan(None, 0.0, SNAP_UNAVAILABLE_INVALID)
    max_fraction = float(max_coverage_fraction)
    if not math.isfinite(max_fraction) or not 0.0 < max_fraction <= 1.0:
        raise ValueError("Snap coverage fraction must be in (0, 1].")
    query = (
        x_value - radius_value,
        y_value - radius_value,
        x_value + radius_value,
        y_value + radius_value,
    )
    clipped = (
        max(left, query[0]),
        max(bottom, query[1]),
        min(right, query[2]),
        min(top, query[3]),
    )
    if clipped[2] < clipped[0] or clipped[3] < clipped[1]:
        return SnapSearchPlan(None, 0.0, SNAP_UNAVAILABLE_OUTSIDE_BOUNDS)
    intersection_area = (clipped[2] - clipped[0]) * (clipped[3] - clipped[1])
    coverage = intersection_area / ((right - left) * (top - bottom))
    if coverage > max_fraction:
        return SnapSearchPlan(
            clipped,
            coverage,
            SNAP_UNAVAILABLE_EXCESSIVE_COVERAGE,
        )
    return SnapSearchPlan(clipped, coverage)
```

Add `MAX_FILE_SNAP_GDS_FRACTION`, `SnapSearchPlan`, and `plan_snap_search` to `klayout_geometry.__all__`.

- [ ] **Step 5: Implement bounded streaming traversal and cancellation**

Change the backend protocol and worker call to:

```python
class _SnapBackend(Protocol):
    def ensure_config(self, config: KLayoutConfig) -> None:
        raise NotImplementedError

    def snap(
        self,
        request: SnapRequest,
        *,
        is_cancelled: Callable[[], bool],
    ) -> SnapResponse:
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError
```

Capture cancellation state atomically while dequeuing, then suppress obsolete work both after configuration loading and after snapping:

```python
@dataclass(frozen=True)
class _SnapWork:
    request: SnapRequest
    cancellation_generation: int
    hover_cancellation_generation: int


def _snap_work(self, request: SnapRequest) -> _SnapWork:
    # Called only while self._condition is held by _take_next().
    return _SnapWork(
        request=request,
        cancellation_generation=self._cancellation_generation,
        hover_cancellation_generation=self._hover_cancellation_generation,
    )


def _work_is_obsolete(self, work: _SnapWork) -> bool:
    with self._condition:
        if self._stopping or self._stop_requested.is_set():
            return True
        if self._cancellation_generation != work.cancellation_generation:
            return True
        if (
            work.request.purpose == "hover"
            and self._hover_cancellation_generation
            != work.hover_cancellation_generation
        ):
            return True
        if work.request.config != self._latest_config:
            return True
        if work.request.purpose != "hover":
            return False
        if self._clicks:
            return True
        return (
            self._hover is not None
            and self._hover.request_id != work.request.request_id
        )


# _take_next(), while holding self._condition:
request = self._clicks.popleft()
if request.config == self._latest_config:
    return self._snap_work(request)

# Apply the same wrapping to the selected hover request.


# _run(), around backend.snap()
work = self._take_next()
if work is _STOP_WORKER:
    return
request = work.request
config_changed = active_config != request.config
if config_changed:
    backend.ensure_config(request.config)
    active_config = request.config
if self._work_is_obsolete(work):
    continue
if config_changed:
    self._post_publication("loaded", request.config, request.config)
response = backend.snap(
    request,
    is_cancelled=lambda work=work: self._work_is_obsolete(work),
)
if self._work_is_obsolete(work):
    continue
self._post_publication("snap_ready", response, request.config)
```

Change `_take_next()`'s return type to `_SnapWork | object`; no raw `SnapRequest` leaves that method.

The explicit-cancel method is:

```python
def cancel_hover(self) -> None:
    self._require_creator_thread()
    with self._condition:
        self._hover_cancellation_generation += 1
        self._hover = None
        self._condition.notify_all()


def cancel_pending(self) -> None:
    self._require_creator_thread()
    with self._condition:
        self._cancellation_generation += 1
        self._hover_cancellation_generation += 1
        self._hover = None
        self._clicks.clear()
        self._condition.notify_all()
```

Initialize both cancellation generations to zero in `KLayoutSnapWorker.__init__()`. Add these concrete regressions to `tests/design/test_klayout_workers.py` (the module already provides `_response`, `_snap_request`, `_process_until`, `Qt`, `threading`, and `time`):

```python
def test_cancel_hover_cooperatively_cancels_active_hover_without_dropping_clicks(
    qt_app: QApplication,
) -> None:
    entered = threading.Event()
    observed_cancel = threading.Event()
    calls: list[int] = []

    class Backend:
        def ensure_config(self, _config: KLayoutConfig) -> None:
            pass

        def snap(self, request: SnapRequest, *, is_cancelled) -> SnapResponse:
            calls.append(request.request_id)
            if request.request_id == 1:
                entered.set()
                while not is_cancelled():
                    time.sleep(0.001)
                observed_cancel.set()
            return _response(request)

        def close(self) -> None:
            pass

    worker = KLayoutSnapWorker(backend_factory=Backend)
    responses: list[SnapResponse] = []
    worker.snap_ready.connect(responses.append, Qt.ConnectionType.DirectConnection)
    worker.submit_hover(_snap_request(1))
    assert entered.wait(1.0)
    worker.submit_click(_snap_request(2, purpose="click"))
    worker.cancel_hover()
    assert observed_cancel.wait(1.0)
    _process_until(
        qt_app,
        lambda: [response.request_id for response in responses] == [2],
    )
    worker.stop()

    assert calls == [1, 2]
    assert [response.request_id for response in responses] == [2]


def test_cancel_between_dequeue_and_config_load_never_reaches_snap(
    qt_app: QApplication,
) -> None:
    ensure_entered = threading.Event()
    release_ensure = threading.Event()
    ensure_returned = threading.Event()
    snap_calls: list[int] = []

    class Backend:
        def ensure_config(self, _config: KLayoutConfig) -> None:
            ensure_entered.set()
            assert release_ensure.wait(1.0)
            ensure_returned.set()

        def snap(self, request: SnapRequest, *, is_cancelled) -> SnapResponse:
            snap_calls.append(request.request_id)
            return _response(request)

        def close(self) -> None:
            pass

    worker = KLayoutSnapWorker(backend_factory=Backend)
    responses: list[SnapResponse] = []
    worker.snap_ready.connect(responses.append, Qt.ConnectionType.DirectConnection)
    worker.submit_click(_snap_request(1, purpose="click"))
    assert ensure_entered.wait(1.0)
    worker.cancel_pending()
    release_ensure.set()
    assert ensure_returned.wait(1.0)
    worker.submit_click(_snap_request(2, purpose="click"))
    _process_until(
        qt_app,
        lambda: [response.request_id for response in responses] == [2],
    )
    worker.stop()

    assert snap_calls == [2]
    assert [response.request_id for response in responses] == [2]
```

Retain the existing FIFO click regression unchanged apart from the callback signature. Extend the lifecycle state and parameterized foreign-thread regression with these exact additions:

```python
# _lifecycle_state(), KLayoutSnapWorker branch
return (
    *common,
    worker._hover,
    tuple(worker._clicks),
    worker._cancellation_generation,
    worker._hover_cancellation_generation,
)

# parameter rows
("snap", "cancel_hover"),
("snap", "cancel_pending"),

# invoke() dispatch before the stop fallback
elif method_name == "cancel_hover":
    assert isinstance(worker, KLayoutSnapWorker)
    worker.cancel_hover()
elif method_name == "cancel_pending":
    assert isinstance(worker, KLayoutSnapWorker)
    worker.cancel_pending()
```

This verifies that creator-thread validation runs before either method mutates a queue or epoch.

Change tuple/list contour materialization to this testable bounded collector in `probe_station_gui/design/klayout_workers.py`. Add `Sequence` and `TypeAlias` to the typing imports; import `plan_snap_search` with `Segment2D` and `select_snap`; and import `Box2D`, `SnapWorkBudget`, `SNAP_UNAVAILABLE_CANCELLED`, `SNAP_UNAVAILABLE_SHAPE_BUDGET`, `SNAP_UNAVAILABLE_CANDIDATE_BUDGET`, and `SNAP_UNAVAILABLE_TIME_BUDGET` from `klayout_types`:

```python
ShapeContours: TypeAlias = Iterable[tuple[Iterable[Point2D], bool]]


@dataclass(frozen=True)
class _CollectedSnapGeometry:
    vertices: Sequence[Point2D]
    segments: Sequence[Segment2D]
    shapes_inspected: int
    candidates_generated: int
    unavailable_reason: str | None = None


def _collect_snap_geometry(
    shapes: Iterable[ShapeContours],
    budget: SnapWorkBudget,
    *,
    clock: Callable[[], float],
    is_cancelled: Callable[[], bool],
) -> _CollectedSnapGeometry:
    started = clock()
    vertices: list[Point2D] = []
    segments: list[Segment2D] = []
    shapes_inspected = 0
    candidates_generated = 0

    def abort(reason: str) -> _CollectedSnapGeometry:
        return _CollectedSnapGeometry(
            (),
            (),
            shapes_inspected,
            candidates_generated,
            reason,
        )

    def checkpoint() -> str | None:
        if is_cancelled():
            return SNAP_UNAVAILABLE_CANCELLED
        if (clock() - started) * 1_000.0 > budget.max_elapsed_ms:
            return SNAP_UNAVAILABLE_TIME_BUDGET
        return None

    shape_iterator = iter(shapes)
    while True:
        # Check before next(): advancing this iterator may re-enter native KLayout.
        reason = checkpoint()
        if reason is not None:
            return abort(reason)
        if shapes_inspected >= budget.max_shapes:
            return abort(SNAP_UNAVAILABLE_SHAPE_BUDGET)
        try:
            shape_contours = next(shape_iterator)
        except StopIteration:
            break
        shapes_inspected += 1
        contour_iterator = iter(shape_contours)
        while True:
            reason = checkpoint()
            if reason is not None:
                return abort(reason)
            try:
                contour, closed = next(contour_iterator)
            except StopIteration:
                break
            contour_points: list[Point2D] = []
            point_iterator = iter(contour)
            while True:
                # Point generators may also advance a native polygon iterator.
                reason = checkpoint()
                if reason is not None:
                    return abort(reason)
                try:
                    point = next(point_iterator)
                except StopIteration:
                    break
                added_candidates = 1 + int(bool(contour_points))
                if candidates_generated + added_candidates > budget.max_candidates:
                    return abort(SNAP_UNAVAILABLE_CANDIDATE_BUDGET)
                vertices.append(point)
                candidates_generated += 1
                if contour_points:
                    segments.append((contour_points[-1], point))
                    candidates_generated += 1
                contour_points.append(point)
            if closed and len(contour_points) > 1:
                reason = checkpoint()
                if reason is not None:
                    return abort(reason)
                if candidates_generated + 1 > budget.max_candidates:
                    return abort(SNAP_UNAVAILABLE_CANDIDATE_BUDGET)
                segments.append((contour_points[-1], contour_points[0]))
                candidates_generated += 1
    return _CollectedSnapGeometry(
        tuple(vertices),
        tuple(segments),
        shapes_inspected,
        candidates_generated,
    )
```

Add `_KLayoutSnapBackend._iter_shape_contours(config, source_search_box)` as the only method that touches `begin_shapes_rec_touching`; it yields one lazy `ShapeContours` value per recursive shape and deletes each KLayout iterator in a `finally` block:

```python
def _iter_shape_contours(
    self,
    config: KLayoutConfig,
    source_search_box: Box2D,
) -> Iterable[ShapeContours]:
    search_box = self._db.DBox(*source_search_box)
    for layer_key in sorted(config.visible_layers):
        layer_index = self._layer_indexes.get(layer_key)
        if layer_index is None:
            continue
        iterator = self._top_cell.begin_shapes_rec_touching(
            layer_index,
            search_box,
        )
        try:
            while not iterator.at_end():
                yield _shape_contours(
                    iterator.shape(),
                    iterator.dtrans(),
                    self._db,
                )
                iterator.next()
        finally:
            del iterator
```

Define `_shape_contours()` with tuple-producing lazy iterators. KLayout `DPoint` is accessed through `.x`/`.y` here, so `_collect_snap_geometry()` receives ordinary `Point2D` tuples and never native objects:

```python
def _point_tuples(points: Iterable[Any]) -> Iterable[Point2D]:
    for point in points:
        yield (float(point.x), float(point.y))


def _shape_contours(
    shape: Any,
    transform: Any,
    db: Any,
) -> ShapeContours:
    if shape.is_box():
        box = shape.dbox
        points = (
            db.DPoint(box.left, box.bottom),
            db.DPoint(box.right, box.bottom),
            db.DPoint(box.right, box.top),
            db.DPoint(box.left, box.top),
        )
        yield (_point_tuples(transform * point for point in points), True)
        return
    if shape.is_polygon():
        polygon = transform * shape.dpolygon
    elif shape.is_path():
        polygon = transform * shape.dpath.polygon()
    elif hasattr(shape, "is_edge") and shape.is_edge():
        edge = shape.dedge
        yield (
            _point_tuples((transform * edge.p1, transform * edge.p2)),
            False,
        )
        return
    else:
        return
    yield (_point_tuples(polygon.each_point_hull()), True)
    for hole_index in range(polygon.holes()):
        yield (_point_tuples(polygon.each_point_hole(hole_index)), True)
```

Do not materialize `each_point_hull()` or `each_point_hole()`. In `snap()`, retain the returned shape generator in a local, call `_collect_snap_geometry()` inside `try`, and invoke its `close()` in `finally` so a budget abort releases the native recursive iterator immediately.

In `_KLayoutSnapBackend.snap()`:

1. check `is_cancelled()`, then call `plan_snap_search(request.point, request.radius, request.config.display_bounds)` before constructing a KLayout iterator;
2. return a correlated free `SnapResponse` immediately for a skipped plan;
3. inverse-rotate the accepted clipped box with `inverse_rotate_box()`;
4. pass `_iter_shape_contours()` into `_collect_snap_geometry()`;
5. on any collector abort, return a free response with its named `unavailable_reason` and empty segment endpoints;
6. call `select_snap()` only when the collector returned a complete bounded traversal.

Use a small `_unavailable_response(request, started, shapes, candidates, reason)` helper so every exit reports identical diagnostics.

Make elapsed-time tests deterministic by changing the production constructor to:

```python
def __init__(self, *, clock: Callable[[], float] = time.perf_counter) -> None:
    self._clock = clock
    self._db = None
    self._layout = None
    self._top_cell = None
    self._layer_indexes = {}
    self._path = None
    self._source_load_id = None
```

Use `self._clock()` for both `started` and every elapsed-budget check. Treat the wall-time limit as cooperative: it must stop at the first Python checkpoint after expiry, while the preflight coverage guard provides the hard no-iterator guarantee for the reproduced extreme-radius case.

- [ ] **Step 6: Add pane-side no-submit fallback**

In `_submit_file_backed_hover()` and `_submit_file_backed_click()`, call the same `plan_snap_search()` before submitting work. For a skipped hover, set `_latest_hover_request_id = 0`, clear `_pending_hover_markup`, and call `worker.cancel_hover()` so a prior response cannot replace the fallback. For a skipped click, do not allocate a pending request. Merge the raw free result with `_best_markup_snap(raw_point)` through `_nearest_screen_result()` and either update hover or execute the click synchronously:

```python
def _file_snap_fallback(self, raw_point: Point2D) -> SnapResult:
    free = SnapResult(point=raw_point, mode="free", distance=0.0)
    return self._nearest_screen_result(
        raw_point,
        free,
        self._best_markup_snap(raw_point),
    )
```

Log the query-plan skip reason at debug level. Log a response's `unavailable_reason` at debug level in `_on_file_backed_snap_ready()`. Use `cancel_pending()` for configuration invalidation and snap-off. Escape clears the pane's pending Move correlation and calls `cancel_hover()`; a bounded in-flight click may finish on the worker, but its discarded correlation prevents movement. Neither cancellation method blocks the GUI thread.

- [ ] **Step 7: Add UI fallback regressions**

Extend `tests/ui/test_design_plot_klayout.py` with:

```python
def test_extreme_hover_skips_worker_but_keeps_markup_snap(pane, tmp_path) -> None:
    source = tmp_path / "chip.gds"
    source.write_bytes(b"gds")
    pane.set_document(_document(source))
    pane.set_markup(
        MarkupDocument.empty(source).append_guide(
            (0.0, 0.0), (10.0, 0.0), guide_id="guide"
        )
    )
    pane._snap_distance_threshold = lambda: 10_000_000.0
    worker = pane._snap_worker

    pane._submit_file_backed_hover((5.0, 0.0))

    assert worker.hover_requests == []
    assert worker.cancel_hover_calls == 1
    assert pane._hover_snap.mode in {"guide_center", "guide_intersection"}


def test_extreme_move_click_executes_exact_cursor_once_without_worker(
    pane, tmp_path
) -> None:
    source = tmp_path / "move-chip.gds"
    source.write_bytes(b"gds")
    pane.set_document(_document(source))
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    pane._snap_distance_threshold = lambda: 10_000_000.0

    pane._submit_file_backed_click("move", (25.0, 30.0))

    assert pane._snap_worker.click_requests == []
    assert emitted == [(25.0, 30.0)]
```

Extend that module's `_SnapWorker` fake with these exact additions:

```python
# _SnapWorker.__init__
self.cancel_hover_calls = 0
self.cancel_pending_calls = 0

def cancel_hover(self) -> None:
    self.cancel_hover_calls += 1

def cancel_pending(self) -> None:
    self.cancel_pending_calls += 1
```

The extreme-hover test above already asserts `cancel_hover_calls == 1`. Add `assert worker.cancel_pending_calls == 1` immediately after `pane.set_snap_enabled(False)` in `test_snap_off_invalidates_inflight_hover_response`; retain its late-response assertions so cancellation and UI-correlation invalidation are both covered.

- [ ] **Step 8: Run the complete snap pipeline tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design/test_klayout_types.py tests/design/test_klayout_geometry.py tests/design/test_klayout_workers.py tests/design/test_klayout_snap_backend.py tests/design/test_klayout_real_smoke.py tests/ui/test_design_plot_klayout.py -q
```

Expected: all tests pass; normal vertex/segment/center cases are unchanged and the extreme query performs no recursive traversal.

- [ ] **Step 9: Commit bounded snapping**

```powershell
git add probe_station_gui/design/klayout_types.py probe_station_gui/design/klayout_geometry.py probe_station_gui/design/klayout_workers.py probe_station_gui/views/design_plot_pane.py tests/design/test_klayout_types.py tests/design/test_klayout_geometry.py tests/design/test_klayout_workers.py tests/design/test_klayout_snap_backend.py tests/ui/test_design_plot_klayout.py
git commit -m "fix: bound KLayout geometry snapping"
```

---

### Task 4: Persistent Move Tool

**Files:**
- Modify: `probe_station_gui/views/design_navigator_enablement.py`
- Modify: `probe_station_gui/views/design_navigator_panel.py:262-440, 567-585, 1140-1153, 1289-1388, 1419-1495`
- Modify: `probe_station_gui/views/design_plot_pane.py:108-117, 794-818, 882-897, 1599-1796`
- Create: `tests/ui/test_design_plot_move.py`
- Modify: `tests/design/test_click_navigation.py`
- Modify: `tests/ui/test_design_navigator_panel.py`
- Modify: `tests/ui/test_design_plot_selection.py`

**Interfaces:**
- Consumes: Task 3 synchronous or worker-backed `_submit_file_backed_click("move", raw_point, payload)` and the existing `move_requested` signal.
- Produces: active tool token `move`; no new Main or StageController API.

- [ ] **Step 1: Write failing panel and enablement tests**

Add to `tests/ui/test_design_navigator_panel.py`:

```python
def test_move_tool_is_adjacent_to_select_and_requires_registration(qt_app) -> None:
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel._update_enabled_state()

    assert panel._move_tool_button.isEnabled() is False
    toolbar_layout = panel._tool_toolbar_widget.layout()
    assert toolbar_layout.itemAt(0).widget() is panel._select_tool_button
    assert toolbar_layout.itemAt(1).widget() is panel._move_tool_button

    panel.set_design_registration_active(True)
    assert panel._move_tool_button.isEnabled() is True

    panel._move_tool_button.click()
    assert panel._active_design_tool == "move"
    panel.set_design_registration_active(False)
    assert panel._active_design_tool == "select"
    panel.deleteLater()


def test_route_run_disables_move_tool(qt_app) -> None:
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel._update_enabled_state()
    panel.set_design_registration_active(True)
    panel._move_tool_button.click()
    panel.set_route_measurement_running(True)
    assert panel._move_tool_button.isEnabled() is False
    assert panel._active_design_tool == "select"
    panel.deleteLater()


def test_design_load_disables_move_tool_and_returns_to_select(qt_app) -> None:
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel._update_enabled_state()
    panel.set_design_registration_active(True)
    panel._move_tool_button.click()

    panel.set_design_load_pending(True)

    assert panel._move_tool_button.isEnabled() is False
    assert panel._active_design_tool == "select"
    panel.deleteLater()


def test_move_tool_is_exclusive_and_escape_returns_select(qt_app) -> None:
    panel = DesignNavigatorPanel()
    panel._document = object()
    panel.set_design_registration_active(True)
    tools: list[str] = []
    panel.active_design_tool_changed.connect(tools.append)

    panel._move_tool_button.click()
    assert panel._move_tool_button.isChecked()
    assert not panel._select_tool_button.isChecked()
    assert panel._active_design_tool == "move"

    panel.cancel_active_tool()
    assert panel._select_tool_button.isChecked()
    assert not panel._move_tool_button.isChecked()
    assert panel._active_design_tool == "select"
    assert tools[-2:] == ["move", "select"]
    panel.deleteLater()
```

Append this exact sequence before `panel.deleteLater()` in `test_design_tools_are_exclusive_and_markup_eye_is_independent`; the canvas tests below provide the ordinary-click persistence regression:

```python
panel.set_design_registration_active(True)
panel._markup_visibility_button.setChecked(True)
panel._move_tool_button.click()
assert panel._move_tool_button.isChecked()
assert not panel._guide_tool_button.isChecked()
assert panel._markup_visibility_button.isChecked()
panel._markup_visibility_button.click()
assert panel._move_tool_button.isChecked()
assert tools[-1] == "move"
```

- [ ] **Step 2: Write failing canvas interaction tests**

Create `tests/ui/test_design_plot_move.py` with an actual offscreen `_DesignPlotPane`, a lightweight in-memory document marker, and these event fixtures before the tests:

```python
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    PendingClick,
    SnapResponse,
)
from probe_station_gui.design.model import SnapResult
from probe_station_gui.views.design_plot_pane import _DesignPlotPane


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _ClickEvent:
    def __init__(
        self,
        *,
        double: bool,
        modifiers: Qt.KeyboardModifiers = Qt.NoModifier,
        x_value: float = 10.0,
        y_value: float = 20.0,
    ) -> None:
        self._double = bool(double)
        self._modifiers = modifiers
        self._position = QPointF(x_value, y_value)

    def button(self):
        return Qt.LeftButton

    def double(self) -> bool:
        return self._double

    def modifiers(self):
        return self._modifiers

    def scenePos(self) -> QPointF:
        return QPointF(self._position)


class _SceneEvent:
    def __init__(self, event_type, x_value: float, y_value: float) -> None:
        self._event_type = event_type
        self._position = QPointF(x_value, y_value)

    def type(self):
        return self._event_type

    def button(self):
        return Qt.LeftButton

    def scenePos(self) -> QPointF:
        return QPointF(self._position)


@pytest.fixture
def click_event():
    return lambda *, double, modifiers=Qt.NoModifier, x=10.0, y=20.0: _ClickEvent(
        double=double,
        modifiers=modifiers,
        x_value=x,
        y_value=y,
    )


@pytest.fixture
def move_scene_event():
    return SimpleNamespace(
        press=lambda x, y: _SceneEvent(QEvent.GraphicsSceneMousePress, x, y),
        move=lambda x, y: _SceneEvent(QEvent.GraphicsSceneMouseMove, x, y),
        release=lambda x, y: _SceneEvent(QEvent.GraphicsSceneMouseRelease, x, y),
    )


@pytest.fixture
def pane(qt_app):
    widget = _DesignPlotPane()
    widget.resize(640, 480)
    widget.show()
    qt_app.processEvents()
    widget._document = SimpleNamespace(file_backed=False)
    widget._resolve_snap_result = lambda point: SnapResult(point, "free", 0.0)
    yield widget
    widget.shutdown()
    widget.deleteLater()


def test_move_single_click_emits_once_and_stays_active(pane, click_event) -> None:
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    pane.set_navigation_enabled(True)
    pane.set_active_design_tool("move")
    pane._resolve_snap_result = lambda point: SnapResult(
        (point[0] + 1.0, point[1] + 2.0), "vertex", 1.0
    )

    event = click_event(double=False)
    raw = pane._plot.getViewBox().mapSceneToView(event.scenePos())
    expected = (float(raw.x()) + 1.0, float(raw.y()) + 2.0)
    pane._on_mouse_clicked(event)
    pane._on_mouse_clicked(event)

    assert emitted == [expected, expected]
    assert pane.active_design_tool == "move"


def test_move_double_click_second_event_does_not_emit_again(pane, click_event) -> None:
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    pane.set_navigation_enabled(True)
    pane.set_active_design_tool("move")

    pane._on_mouse_clicked(click_event(double=False))
    pane._on_mouse_clicked(click_event(double=True))

    assert len(emitted) == 1


def test_modified_move_click_does_not_emit(pane, click_event) -> None:
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    pane.set_navigation_enabled(True)
    pane.set_active_design_tool("move")

    pane._on_mouse_clicked(click_event(double=False, modifiers=Qt.ControlModifier))

    assert emitted == []


def test_move_click_outside_plot_does_not_emit(pane, click_event) -> None:
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    pane.set_navigation_enabled(True)
    pane.set_active_design_tool("move")

    pane._on_mouse_clicked(click_event(double=False, x=-10_000.0, y=-10_000.0))

    assert emitted == []


def test_move_drag_suppresses_click_and_leaves_event_for_viewbox(
    pane, move_scene_event, click_event
) -> None:
    pane.set_navigation_enabled(True)
    pane.set_active_design_tool("move")
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))

    assert pane._handle_move_scene_event(move_scene_event.press(10, 10)) is False
    assert pane._handle_move_scene_event(move_scene_event.move(40, 10)) is False
    assert pane._handle_move_scene_event(move_scene_event.release(40, 10)) is False
    pane._on_mouse_clicked(click_event(double=False))

    assert emitted == []


def test_real_move_drag_pans_viewbox_without_move_request(pane, qt_app) -> None:
    pane.set_navigation_enabled(True)
    pane.set_active_design_tool("move")
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    viewport = pane._plot.viewport()
    start = viewport.rect().center()
    before = pane._plot.getViewBox().viewRange()

    QTest.mousePress(viewport, Qt.LeftButton, pos=start)
    QTest.mouseMove(viewport, pos=start + QPoint(50, 0), delay=20)
    QTest.mouseRelease(viewport, Qt.LeftButton, pos=start + QPoint(50, 0))
    qt_app.processEvents()

    after = pane._plot.getViewBox().viewRange()
    assert after != before
    assert emitted == []


def test_cancel_active_interaction_clears_pending_move_press(
    pane, move_scene_event
) -> None:
    pane.set_active_design_tool("move")
    pane._handle_move_scene_event(move_scene_event.press(10, 10))

    pane.cancel_active_interaction()

    assert pane._move_press_scene_pos is None
    assert pane._move_dragging is False
    assert pane._suppress_move_scene_click is False


def test_escape_cancels_pending_file_backed_move(pane) -> None:
    emitted: list[tuple[float, float]] = []
    pane.move_requested.connect(lambda x, y: emitted.append((x, y)))
    pane.set_active_design_tool("move")
    config = KLayoutConfig(
        path=Path("layout.gds"),
        top_cell_name="TOP",
        visible_layers=frozenset({(1, 0)}),
        source_bounds=(0.0, 0.0, 100.0, 50.0),
        display_bounds=(0.0, 0.0, 100.0, 50.0),
        rotation_quarter_turns=0,
        generation=3,
    )
    pane._klayout_config = config
    pane._pending_clicks[7] = PendingClick(
        request_id=7,
        config_generation=3,
        action="move",
        raw_point=(10.0, 20.0),
        markup_generation=pane._markup_generation,
    )
    pane.cancel_active_interaction()
    pane.set_active_design_tool("select")
    pane._on_file_backed_snap_ready(
        SnapResponse(
            request_id=7,
            config_generation=3,
            raw_point=(10.0, 20.0),
            result=SnapResult((10.0, 20.0), "free", 0.0),
            elapsed_ms=1.0,
            shapes_inspected=0,
            purpose="click",
        )
    )

    assert emitted == []
    assert pane._pending_clicks == {}
```

In `tests/design/test_click_navigation.py`, add `_emit_click_selection=lambda _point, _modifiers: None` to the namespace returned by `_navigation_pane()`, then replace the two legacy navigation-click tests with these exact Select regressions; Move owns the only canvas navigation click:

```python
def test_design_select_single_click_does_not_move() -> None:
    pane = _navigation_pane()
    pane._active_design_tool = "select"

    _DesignPlotPane._on_mouse_clicked(pane, _FakeClickEvent(double=False))

    assert pane.move_requested.emissions == []


def test_design_select_double_click_does_not_move() -> None:
    pane = _navigation_pane()
    pane._active_design_tool = "select"

    _DesignPlotPane._on_mouse_clicked(pane, _FakeClickEvent(double=True))

    assert pane.move_requested.emissions == []
```

- [ ] **Step 3: Run Move tests and verify failures**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_navigator_panel.py tests/ui/test_design_plot_move.py tests/design/test_click_navigation.py tests/ui/test_design_plot_selection.py -q
```

Expected: failures show the missing Move tool/button/token and absent pointer-state methods.

- [ ] **Step 4: Add Move enablement and toolbar UI**

In `DesignNavigatorEnablement` add:

```python
@property
def can_move_design(self) -> bool:
    return self.can_edit_design and self.design_registration_active
```

In `DesignNavigatorPanel`:

- create `_move_tool_button` immediately after `_select_tool_button` with tooltip `Move` and a target/crosshair `move` icon;
- add it to the exclusive button group and toolbar after Select;
- add a Move options page containing only `QLabel("Click a target. Drag to pan.")`;
- connect it to `_set_design_tool("move")`;
- include `move` in the accepted tools, button map, and stack-index map;
- enable it with `state.can_move_design`;
- if registration/load/route state makes an active Move unavailable, call `_set_design_tool("select")` once.
- change the registration hint to `"Load a GDS for registered navigation. Use Alignment to capture chip points, then use Move in the design view."` and update its text assertion.

Do not add a new movement signal; the canvas/window signal already exists.

- [ ] **Step 5: Implement deterministic Move pointer handling**

In `_DesignPlotPane.__init__` add:

```python
self._move_press_scene_pos: QPointF | None = None
self._move_dragging = False
self._suppress_move_scene_click = False
```

Accept `move` in `set_active_design_tool()`, use the crosshair cursor, and clear Move state when leaving it. Split the existing Select branch out of `eventFilter()` without changing it, then dispatch Move events to:

```python
def _handle_move_scene_event(self, event) -> bool:
    event_type = event.type()
    if event_type == QEvent.GraphicsSceneMousePress and event.button() == Qt.LeftButton:
        self._move_press_scene_pos = QPointF(event.scenePos())
        self._move_dragging = False
    elif event_type == QEvent.GraphicsSceneMouseMove and self._move_press_scene_pos is not None:
        distance = (event.scenePos() - self._move_press_scene_pos).manhattanLength()
        if distance >= QApplication.startDragDistance():
            self._move_dragging = True
    elif event_type == QEvent.GraphicsSceneMouseRelease and event.button() == Qt.LeftButton:
        if self._move_press_scene_pos is not None:
            self._suppress_move_scene_click = self._move_dragging
        self._move_press_scene_pos = None
        self._move_dragging = False
        if self._suppress_move_scene_click:
            QTimer.singleShot(0, self._clear_move_click_suppression)
    return False  # pyqtgraph must still receive the drag and pan the ViewBox


def _clear_move_click_suppression(self) -> None:
    self._suppress_move_scene_click = False
```

In `_on_mouse_clicked()` make `active_tool == "move"` the only navigation action:

```python
move_click = (
    active_tool == "move"
    and self._navigation_enabled
    and is_left_click
    and modifiers == Qt.NoModifier
)
if active_tool == "move":
    if not move_click or is_double_click or self._suppress_move_scene_click:
        return
    action = "move"
    payload = ()
```

Remove the legacy `_navigation_enabled` double-click branch for Select and other tools. Preserve legacy calibration only for the explicit `legacy` state. Add `move` to `_cancel_pending_tool_snaps()` so Escape or another tool selection discards a late file-backed response before it can emit hardware movement.

- [ ] **Step 6: Run Move and unchanged downstream motion tests**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/ui/test_design_navigator_panel.py tests/ui/test_design_plot_move.py tests/design/test_click_navigation.py tests/ui/test_design_plot_selection.py tests/ui/test_design_plot_klayout.py tests/app/test_main_design_navigation.py tests/stage/test_controller_click_move.py -q
```

Expected: all tests pass; Main and StageController precision tests require no production changes.

- [ ] **Step 7: Commit the Move tool**

```powershell
git add probe_station_gui/views/design_navigator_enablement.py probe_station_gui/views/design_navigator_panel.py probe_station_gui/views/design_plot_pane.py tests/ui/test_design_navigator_panel.py tests/ui/test_design_plot_move.py tests/design/test_click_navigation.py tests/ui/test_design_plot_selection.py
git commit -m "feat: add design click move tool"
```

---

### Task 5: Final Regression and Performance Verification

**Files:**
- Verify only; no production files are expected to change.

**Interfaces:**
- Consumes: all deliverables from Tasks 1-4.
- Produces: evidence that the feature is ready for manual Design Window testing.

- [ ] **Step 1: Run the complete design/UI/stage regression set**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/design tests/ui tests/app/test_main_design_navigation.py tests/ui/test_main_window_auxiliary.py tests/stage/test_controller_click_move.py -q
```

Expected: all tests pass with no hardware access.

- [ ] **Step 2: Run the full repository suite**

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests -q
```

Expected: all tests pass. Existing skips for unavailable optional hardware or platform dependencies are acceptable; new failures are not.

- [ ] **Step 3: Verify diff scope and repository state**

```powershell
git diff --check
git status --short
git log --oneline -5
```

Expected: `git diff --check` is silent, `git status --short` is empty, and the log contains the four task commits:

```text
feat: add design click move tool
fix: bound KLayout geometry snapping
feat: bound design viewport to content
feat: model bounded design navigation
```

- [ ] **Step 4: Hand off manual checks without launching hardware**

Report these exact manual checks for the operator:

1. open the last large GDS and confirm the initial view fits only the GDS;
2. create or restore distant route/markup content and confirm zoom-out stops at the farthest content;
3. press Home and confirm the GDS fills the window again;
4. activate Move, single-click two snapped targets, and confirm Move remains active;
5. drag with Move and confirm the view pans without stage movement;
6. press Escape and confirm Select becomes active;
7. make the distant route/markup entity visible, navigate to it, select it, delete it, and confirm the limits shrink back toward the GDS;
8. zoom to the former extreme scale and confirm hover remains responsive with no multi-second snap log entry.
