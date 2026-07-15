from __future__ import annotations

from collections import Counter
from pathlib import Path
import threading

import klayout.db as db
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage
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
)


def _write_hierarchical_design(path: Path) -> None:
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    child = layout.create_cell("CHILD")
    top.shapes(layout.layer(1, 0)).insert(db.Box(0, 0, 10_000, 4_000))
    child.shapes(layout.layer(2, 0)).insert(db.Box(0, 0, 2_000, 2_000))
    top.insert(db.CellInstArray(child.cell_index(), db.Trans(20_000, 10_000)))
    top.shapes(layout.layer(3, 0)).insert(db.Text("label", db.Trans(15_000, 8_000)))
    layout.write(str(path))


def _config(path: Path, *, turns: int = 0, layers: set[tuple[int, int]] | None = None) -> KLayoutConfig:
    source_bounds = (0.0, 0.0, 22.0, 12.0)
    display_bounds = source_bounds if turns % 2 == 0 else (5.0, -5.0, 17.0, 17.0)
    return KLayoutConfig(
        path=path,
        top_cell_name="TOP",
        visible_layers=frozenset(layers or {(1, 0), (2, 0)}),
        source_bounds=source_bounds,
        display_bounds=display_bounds,
        rotation_quarter_turns=turns,
        generation=turns + 1,
    )


def test_real_render_worker_produces_detached_nonempty_hierarchical_frame(
    tmp_path: Path,
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
    assert ready.wait(5.0)
    worker.stop()

    assert failures == []
    assert len(frames) == 1
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


def test_real_snap_worker_queries_hierarchical_geometry_and_rotates_results(
    tmp_path: Path,
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
    assert ready.wait(5.0)
    worker.stop()

    assert failures == []
    assert len(responses) == 1
    response = responses[0]
    assert response.request_id == request.request_id
    assert response.result.mode == "vertex"
    assert response.result.point == pytest.approx(displayed_vertex)
    assert response.shapes_inspected == 1


def test_real_snap_worker_ignores_text_origins(tmp_path: Path) -> None:
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
    assert ready.wait(5.0)
    worker.stop()

    assert responses[0].result.mode == "free"
    assert responses[0].result.point == pytest.approx((15.0, 8.0))
