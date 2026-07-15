from __future__ import annotations

from collections import Counter
from collections.abc import Callable
import os
from pathlib import Path
import threading
import time

import klayout.db as db
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication
import pytest

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    RenderFrame,
    RenderRequest,
    SnapRequest,
    SnapResponse,
    forward_rotate_point,
)
from probe_station_gui.design.klayout_workers import (
    KLayoutRenderWorker,
    KLayoutSnapWorker,
    _shape_contours,
)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _process_until(
    app: QApplication,
    predicate: Callable[[], bool],
    timeout_s: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for queued Qt delivery")


def _write_hierarchical_design(path: Path) -> None:
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    child = layout.create_cell("CHILD")
    top.shapes(layout.layer(1, 0)).insert(db.Box(0, 0, 10_000, 4_000))
    child.shapes(layout.layer(2, 0)).insert(db.Box(0, 0, 2_000, 2_000))
    top.insert(db.CellInstArray(child.cell_index(), db.Trans(20_000, 10_000)))
    top.shapes(layout.layer(3, 0)).insert(db.Text("label", db.Trans(15_000, 8_000)))
    top.shapes(layout.layer(4, 0)).insert(
        db.Polygon(
            [
                db.Point(12_000, 0),
                db.Point(16_000, 0),
                db.Point(14_000, 3_000),
            ]
        )
    )
    top.shapes(layout.layer(5, 0)).insert(
        db.Path(
            [db.Point(12_000, 5_000), db.Point(16_000, 5_000)],
            1_000,
        )
    )
    layout.write(str(path))


def _config(
    path: Path,
    *,
    turns: int = 0,
    layers: set[tuple[int, int]] | None = None,
    generation: int | None = None,
    source_load_id: str | None = None,
) -> KLayoutConfig:
    source_bounds = (0.0, 0.0, 22.0, 12.0)
    display_bounds = source_bounds if turns % 2 == 0 else (5.0, -5.0, 17.0, 17.0)
    return KLayoutConfig(
        path=path,
        top_cell_name="TOP",
        visible_layers=frozenset({(1, 0), (2, 0)} if layers is None else layers),
        source_bounds=source_bounds,
        display_bounds=display_bounds,
        rotation_quarter_turns=turns,
        generation=turns + 1 if generation is None else generation,
        source_load_id=source_load_id,
    )


def _write_reload_design(path: Path, *, box_left_um: float) -> None:
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    left = round(box_left_um * 1000.0)
    top.shapes(layout.layer(1, 0)).insert(
        db.Box(left, 40_000, left + 10_000, 50_000)
    )
    top.shapes(layout.layer(9, 0)).insert(db.Box(0, 0, 1_000, 1_000))
    top.shapes(layout.layer(9, 0)).insert(db.Box(99_000, 99_000, 100_000, 100_000))
    layout.write(str(path))


def _reload_config(path: Path, *, generation: int, source_load_id: str) -> KLayoutConfig:
    return KLayoutConfig(
        path=path,
        top_cell_name="TOP",
        visible_layers=frozenset({(1, 0)}),
        source_bounds=(0.0, 0.0, 100.0, 100.0),
        display_bounds=(0.0, 0.0, 100.0, 100.0),
        rotation_quarter_turns=0,
        generation=generation,
        source_load_id=source_load_id,
    )


def test_real_render_worker_produces_detached_nonempty_hierarchical_frame(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    design_path = tmp_path / "hierarchical.gds"
    _write_hierarchical_design(design_path)
    config = _config(design_path, turns=1)
    request = RenderRequest(
        request_id=1,
        config=config,
        world_box=config.display_bounds,
        pixel_width=320,
        pixel_height=240,
        viewport_generation=7,
        density=10.0,
    )
    worker = KLayoutRenderWorker()
    ready = threading.Event()
    frames: list[RenderFrame] = []
    failures: list[str] = []
    worker.frame_ready.connect(
        lambda frame: (frames.append(frame), ready.set()),
        Qt.ConnectionType.DirectConnection,
    )
    worker.failed.connect(
        lambda message: (failures.append(message), ready.set()),
        Qt.ConnectionType.DirectConnection,
    )

    worker.submit(request)
    _process_until(qt_app, ready.is_set)
    ready.clear()
    top_only_config = _config(
        design_path,
        turns=1,
        layers={(1, 0)},
        generation=3,
    )
    worker.submit(
        RenderRequest(
            request_id=2,
            config=top_only_config,
            world_box=top_only_config.display_bounds,
            pixel_width=320,
            pixel_height=240,
            viewport_generation=8,
            density=10.0,
        )
    )
    _process_until(qt_app, ready.is_set)
    worker.stop()

    assert failures == []
    assert len(frames) == 2
    frame = frames[0]
    assert frame.request_id == request.request_id
    assert frame.world_box == request.world_box
    assert isinstance(frame.image, QImage)
    assert frame.image.isNull() is False
    assert (frame.image.width(), frame.image.height()) == (320, 240)
    sampled_colors = {
        frame.image.pixel(x, y)
        for x in range(0, frame.image.width(), 8)
        for y in range(0, frame.image.height(), 8)
    }
    assert len(sampled_colors) > 1
    colors = Counter(
        frame.image.pixel(x, y)
        for x in range(frame.image.width())
        for y in range(frame.image.height())
    )
    background = colors.most_common(1)[0][0]
    half_width = frame.image.width() // 2
    half_height = frame.image.height() // 2
    top_left_ink = sum(
        frame.image.pixel(x, y) != background
        for x in range(half_width)
        for y in range(half_height)
    )
    bottom_right_ink = sum(
        frame.image.pixel(x, y) != background
        for x in range(half_width, frame.image.width())
        for y in range(half_height, frame.image.height())
    )
    assert bottom_right_ink > top_left_ink * 5
    child_instance_pixels = sum(
        frame.image.pixel(x, y) != frames[1].image.pixel(x, y)
        for x in range(frame.image.width())
        for y in range(frame.image.height())
    )
    assert child_instance_pixels > 80


def test_real_snap_worker_queries_hierarchical_geometry_and_rotates_results(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    design_path = tmp_path / "hierarchical.gds"
    _write_hierarchical_design(design_path)
    config = _config(design_path, turns=1, layers={(2, 0)})
    displayed_vertex = forward_rotate_point((20.0, 10.0), config)
    request = SnapRequest(
        request_id=11,
        config=config,
        point=displayed_vertex,
        radius=0.25,
        purpose="click",
    )
    worker = KLayoutSnapWorker()
    ready = threading.Event()
    responses: list[SnapResponse] = []
    failures: list[str] = []
    worker.snap_ready.connect(
        lambda response: (responses.append(response), ready.set()),
        Qt.ConnectionType.DirectConnection,
    )
    worker.failed.connect(
        lambda message: (failures.append(message), ready.set()),
        Qt.ConnectionType.DirectConnection,
    )

    worker.submit_click(request)
    _process_until(qt_app, ready.is_set)
    worker.stop()

    assert failures == []
    assert len(responses) == 1
    response = responses[0]
    assert response.request_id == request.request_id
    assert response.result.mode == "vertex"
    assert response.result.point == pytest.approx(displayed_vertex)
    assert response.shapes_inspected == 1


def test_real_snap_worker_ignores_text_origins(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    design_path = tmp_path / "hierarchical.gds"
    _write_hierarchical_design(design_path)
    config = _config(design_path, layers={(3, 0)})
    worker = KLayoutSnapWorker()
    ready = threading.Event()
    responses: list[SnapResponse] = []
    worker.snap_ready.connect(
        lambda response: (responses.append(response), ready.set()),
        Qt.ConnectionType.DirectConnection,
    )

    worker.submit_hover(
        SnapRequest(
            request_id=12,
            config=config,
            point=(15.0, 8.0),
            radius=0.1,
        )
    )
    _process_until(qt_app, ready.is_set)
    worker.stop()

    assert responses[0].result.mode == "free"
    assert responses[0].result.point == pytest.approx((15.0, 8.0))


def test_real_workers_reload_render_snap_and_minimap_content_replaced_at_same_path(
    tmp_path: Path,
    qt_app: QApplication,
) -> None:
    design_path = tmp_path / "reload.gds"
    replacement_path = tmp_path / "replacement.gds"
    _write_reload_design(design_path, box_left_um=10.0)
    first_config = _reload_config(
        design_path,
        generation=1,
        source_load_id="first-load",
    )
    render_worker = KLayoutRenderWorker()
    snap_worker = KLayoutSnapWorker()
    frames: list[RenderFrame] = []
    responses: list[SnapResponse] = []
    failures: list[object] = []
    render_worker.frame_ready.connect(frames.append)
    render_worker.failed.connect(failures.append)
    snap_worker.snap_ready.connect(responses.append)
    snap_worker.failed.connect(failures.append)

    render_worker.submit(
        RenderRequest(
            1,
            first_config,
            first_config.display_bounds,
            240,
            240,
            1,
            2.4,
            "minimap",
        )
    )
    snap_worker.submit_click(
        SnapRequest(1, first_config, (10.0, 40.0), 0.25, "click")
    )
    _process_until(qt_app, lambda: len(frames) == 1 and len(responses) == 1)

    _write_reload_design(replacement_path, box_left_um=70.0)
    os.replace(replacement_path, design_path)
    second_config = _reload_config(
        design_path,
        generation=2,
        source_load_id="second-load",
    )
    render_worker.submit(
        RenderRequest(
            2,
            second_config,
            second_config.display_bounds,
            240,
            240,
            2,
            2.4,
            "minimap",
        )
    )
    snap_worker.submit_click(
        SnapRequest(2, second_config, (70.0, 40.0), 0.25, "click")
    )
    _process_until(qt_app, lambda: len(frames) == 2 and len(responses) == 2)
    render_worker.stop()
    snap_worker.stop()

    assert failures == []
    assert frames[0].image != frames[1].image
    assert responses[0].result.point == pytest.approx((10.0, 40.0))
    assert responses[1].result.point == pytest.approx((70.0, 40.0))


@pytest.mark.parametrize(
    ("layer", "point"),
    [
        ((4, 0), (12.0, 0.0)),
        ((5, 0), (12.0, 4.5)),
    ],
    ids=["polygon", "path"],
)
def test_real_snap_worker_handles_polygon_and_path(
    tmp_path: Path,
    layer: tuple[int, int],
    point: tuple[float, float],
    qt_app: QApplication,
) -> None:
    design_path = tmp_path / "shape-types.gds"
    _write_hierarchical_design(design_path)
    config = _config(design_path, layers={layer})
    worker = KLayoutSnapWorker()
    ready = threading.Event()
    responses: list[SnapResponse] = []
    failures: list[str] = []
    worker.snap_ready.connect(
        lambda response: (responses.append(response), ready.set()),
        Qt.ConnectionType.DirectConnection,
    )
    worker.failed.connect(
        lambda message: (failures.append(message), ready.set()),
        Qt.ConnectionType.DirectConnection,
    )

    worker.submit_hover(
        SnapRequest(
            request_id=13,
            config=config,
            point=point,
            radius=0.2,
        )
    )
    _process_until(qt_app, ready.is_set)
    worker.stop()

    assert failures == []
    assert len(responses) == 1
    assert responses[0].result.mode == "vertex"
    assert responses[0].result.point == pytest.approx(point)
    assert responses[0].shapes_inspected == 1


def test_shape_contours_handles_real_klayout_edge() -> None:
    layout = db.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("TOP")
    shape = cell.shapes(layout.layer(6, 0)).insert(
        db.Edge(1_000, 2_000, 4_000, 6_000)
    )

    contours = list(_shape_contours(shape, db.DTrans(), db))

    assert len(contours) == 1
    points, closed = contours[0]
    assert closed is False
    assert [(point.x, point.y) for point in points] == pytest.approx(
        [(1.0, 2.0), (4.0, 6.0)]
    )
