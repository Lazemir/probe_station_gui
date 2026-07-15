from __future__ import annotations

from collections import Counter
import os
from pathlib import Path
import time

import klayout.db as db
import pyqtgraph as pg
import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QPointF, QRectF, Signal
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication, QGraphicsRectItem

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    RenderFrame,
    RenderRequest,
)
from probe_station_gui.design.klayout_workers import KLayoutRenderWorker
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.views.design_klayout_raster import (
    ACTIVE_SCHEDULE_MS,
    KLayoutRasterController,
    KLayoutRasterItem,
    PAN_GUARD_FRACTION,
    SETTLE_SCHEDULE_MS,
    SETTLED_MARGIN_FRACTION,
)


@pytest.fixture(scope="module")
def qt_app() -> QApplication:
    return QApplication.instance() or QApplication([])


class _ViewBox(QObject):
    sigRangeChanged = Signal(object, object)

    def __init__(self) -> None:
        super().__init__()
        self.box = (0.0, 0.0, 100.0, 50.0)
        self.logical_size = (200.0, 100.0)

    def viewRange(self) -> list[list[float]]:
        left, bottom, right, top = self.box
        return [[left, right], [bottom, top]]

    def sceneBoundingRect(self) -> QRectF:
        return QRectF(0.0, 0.0, *self.logical_size)

    def change(self, box: tuple[float, float, float, float]) -> None:
        self.box = box
        self.sigRangeChanged.emit(self, self.viewRange())


class _RenderWorker(QObject):
    loaded = Signal(object)
    frame_ready = Signal(object)
    failed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.requests = []
        self.stop_calls: list[float] = []

    def submit(self, request) -> None:
        self.requests.append(request)

    def stop(self, timeout_s: float = 1.0) -> None:
        self.stop_calls.append(timeout_s)


def _document(path: Path, *, turns: int = 0) -> DesignDocument:
    return DesignDocument(
        path=path,
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
        available_layers=frozenset({(1, 0), (2, 0)}),
        cell_bounds={"TOP": (0.0, 0.0, 100.0, 50.0)},
        rotation_quarter_turns=turns,
    )


def _frame(request, *, generation: int | None = None) -> RenderFrame:
    image = QImage(request.pixel_width, request.pixel_height, QImage.Format_ARGB32)
    image.fill(0xFF102030)
    return RenderFrame(
        request_id=request.request_id,
        config_generation=(
            request.config.generation if generation is None else generation
        ),
        viewport_generation=request.viewport_generation,
        world_box=request.world_box,
        pixel_width=request.pixel_width,
        pixel_height=request.pixel_height,
        density=request.density,
        image=image,
        elapsed_ms=1.0,
        purpose=request.purpose,
    )


def _controller(
    view_box: _ViewBox,
    worker: _RenderWorker,
    *,
    dpr: float = 1.0,
) -> tuple[KLayoutRasterController, KLayoutRasterItem]:
    item = KLayoutRasterItem()
    controller = KLayoutRasterController(
        view_box,
        item,
        render_worker_factory=lambda: worker,
        device_pixel_ratio=lambda: dpr,
    )
    return controller, item


def _process_until(
    app: QApplication,
    predicate,
    *,
    timeout_s: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("timed out waiting for queued Qt delivery")


def _write_top_asymmetric_design(path: Path) -> None:
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    top.shapes(layout.layer(1, 0)).insert(db.Box(40_000, 75_000, 60_000, 95_000))
    layout.write(str(path))


def _rgb(value: int) -> tuple[int, int, int]:
    color = QColor.fromRgba(value)
    return (color.red(), color.green(), color.blue())


def _matching_pixel_centroid(
    image: QImage,
    colors: set[tuple[int, int, int]],
    bounds: QRectF,
) -> tuple[float, float]:
    left = max(0, int(bounds.left()))
    top = max(0, int(bounds.top()))
    right = min(image.width(), int(bounds.right()) + 1)
    bottom = min(image.height(), int(bounds.bottom()) + 1)
    points = [
        (x_value, y_value)
        for y_value in range(top, bottom)
        for x_value in range(left, right)
        if _rgb(image.pixel(x_value, y_value)) in colors
    ]
    assert points
    return (
        sum(point[0] for point in points) / len(points),
        sum(point[1] for point in points) / len(points),
    )


def test_viewport_scheduler_uses_reviewed_timing_and_margin_policy() -> None:
    assert ACTIVE_SCHEDULE_MS == 16
    assert SETTLE_SCHEDULE_MS == 120
    assert PAN_GUARD_FRACTION == pytest.approx(0.04)
    assert SETTLED_MARGIN_FRACTION == pytest.approx(0.125)


def test_raster_item_keeps_exact_world_box_below_overlays(qt_app: QApplication) -> None:
    item = KLayoutRasterItem()
    overlay = QGraphicsRectItem()
    image = QImage(20, 10, QImage.Format_ARGB32)
    frame = RenderFrame(1, 1, 1, (-5.0, 3.0, 15.0, 13.0), 20, 10, 1.0, image, 0.0)

    item.set_frame(frame)

    assert item.boundingRect() == QRectF(-5.0, 3.0, 20.0, 10.0)
    assert item.zValue() < overlay.zValue()
    assert item.frame is frame


def test_real_klayout_raster_world_top_aligns_real_plot_overlay(
    qt_app: QApplication,
    tmp_path: Path,
) -> None:
    design_path = tmp_path / "top-asymmetric.gds"
    _write_top_asymmetric_design(design_path)
    config = KLayoutConfig(
        path=design_path,
        top_cell_name="TOP",
        visible_layers=frozenset({(1, 0)}),
        source_bounds=(0.0, 0.0, 100.0, 100.0),
        display_bounds=(0.0, 0.0, 100.0, 100.0),
        rotation_quarter_turns=0,
        generation=1,
    )
    request = RenderRequest(
        request_id=1,
        config=config,
        world_box=config.display_bounds,
        pixel_width=320,
        pixel_height=320,
        viewport_generation=1,
        density=3.2,
        purpose="exact",
    )
    worker = KLayoutRenderWorker()
    frames: list[RenderFrame] = []
    failures: list[str] = []
    worker.frame_ready.connect(frames.append)
    worker.failed.connect(failures.append)
    worker.submit(request)
    _process_until(qt_app, lambda: bool(frames or failures))
    worker.stop()

    assert failures == []
    frame = frames[0]
    colors = Counter(
        _rgb(frame.image.pixel(x_value, y_value))
        for y_value in range(frame.image.height())
        for x_value in range(frame.image.width())
    )
    background = colors.most_common(1)[0][0]
    ink_colors = {
        color
        for color, count in colors.items()
        if color != background and count > 4 and max(color) - min(color) > 32
    }
    assert ink_colors
    source_ink_y = _matching_pixel_centroid(
        frame.image,
        ink_colors,
        QRectF(0.0, 0.0, frame.image.width(), frame.image.height()),
    )[1]
    assert source_ink_y < frame.image.height() * 0.35

    plot = pg.PlotWidget()
    plot.resize(420, 420)
    plot.setBackground("black")
    plot.setMenuEnabled(False)
    plot.hideButtons()
    plot.plotItem.hideAxis("bottom")
    plot.plotItem.hideAxis("left")
    view_box = plot.getViewBox()
    view_box.setAspectLocked(True)
    view_box.setRange(xRange=(0.0, 100.0), yRange=(0.0, 100.0), padding=0.0)
    raster = KLayoutRasterItem()
    raster.set_frame(frame)
    plot.addItem(raster)
    marker = pg.ScatterPlotItem(
        [50.0],
        [85.0],
        pen=pg.mkPen("#ff00ff", width=3),
        brush=None,
        size=15,
        symbol="+",
    )
    plot.addItem(marker)
    plot.show()
    _process_until(qt_app, lambda: plot.isVisible())
    qt_app.processEvents()

    captured = plot.grab().toImage().convertToFormat(QImage.Format_ARGB32)
    scene_bounds = view_box.sceneBoundingRect()
    raster_centroid = _matching_pixel_centroid(captured, ink_colors, scene_bounds)
    marker_centroid = _matching_pixel_centroid(
        captured,
        {(255, 0, 255)},
        scene_bounds,
    )
    expected_marker = view_box.mapViewToScene(QPointF(50.0, 85.0))

    assert marker_centroid == pytest.approx(
        (expected_marker.x(), expected_marker.y()),
        abs=4.0,
    )
    assert raster_centroid[1] == pytest.approx(marker_centroid[1], abs=45.0)
    assert raster_centroid[1] < scene_bounds.center().y()
    plot.close()
    plot.deleteLater()


def test_exact_request_uses_physical_pixels_and_caps_largest_dimension(
    qt_app: QApplication, tmp_path: Path
) -> None:
    view_box = _ViewBox()
    view_box.logical_size = (3000.0, 1000.0)
    worker = _RenderWorker()
    controller, _item = _controller(view_box, worker, dpr=2.0)
    controller.set_document(_document(tmp_path / "layout.gds"))
    worker.requests.clear()

    controller.request_exact()

    request = worker.requests[-1]
    assert request.world_box == view_box.box
    assert request.pixel_width == 4096
    assert request.pixel_height == 1365
    assert request.purpose == "exact"


def test_settled_request_has_twelve_and_a_half_percent_margin_without_oversampling(
    qt_app: QApplication, tmp_path: Path
) -> None:
    view_box = _ViewBox()
    worker = _RenderWorker()
    controller, _item = _controller(view_box, worker, dpr=2.0)
    controller.set_document(_document(tmp_path / "layout.gds"))
    worker.requests.clear()

    controller.request_settled()

    request = worker.requests[-1]
    assert request.world_box == pytest.approx((-12.5, -6.25, 112.5, 56.25))
    assert (request.pixel_width, request.pixel_height) == (500, 250)
    assert request.purpose == "settled"


def test_range_scheduler_coalesces_zoom_and_keeps_previous_frame_during_pan(
    qt_app: QApplication, tmp_path: Path
) -> None:
    view_box = _ViewBox()
    worker = _RenderWorker()
    controller, item = _controller(view_box, worker)
    controller.set_document(_document(tmp_path / "layout.gds"))
    first_request = worker.requests[-1]
    worker.frame_ready.emit(_frame(first_request))
    first_frame = item.frame
    worker.requests.clear()

    view_box.change((5.0, 0.0, 105.0, 50.0))
    view_box.change((10.0, 0.0, 90.0, 40.0))
    view_box.change((20.0, 5.0, 80.0, 35.0))
    qt_app.processEvents()
    from PySide6.QtTest import QTest

    QTest.qWait(25)

    assert len(worker.requests) == 1
    assert worker.requests[0].world_box == (20.0, 5.0, 80.0, 35.0)
    assert worker.requests[0].purpose == "exact"
    assert item.frame is first_frame


def test_pan_guard_reuses_covering_frame_and_settle_requests_margin(
    qt_app: QApplication, tmp_path: Path
) -> None:
    from PySide6.QtTest import QTest

    view_box = _ViewBox()
    worker = _RenderWorker()
    controller, item = _controller(view_box, worker)
    controller.set_document(_document(tmp_path / "layout.gds"))
    controller.request_settled()
    covering = worker.requests[-1]
    worker.frame_ready.emit(_frame(covering))
    worker.requests.clear()

    view_box.change((5.0, 0.0, 105.0, 50.0))
    QTest.qWait(25)
    assert worker.requests == []
    assert item.frame is not None

    QTest.qWait(115)
    assert worker.requests[-1].purpose == "settled"
    assert worker.requests[-1].world_box == pytest.approx((-7.5, -6.25, 117.5, 56.25))


def test_controller_rejects_stale_configuration_and_uncovered_viewport_frames(
    qt_app: QApplication, tmp_path: Path
) -> None:
    view_box = _ViewBox()
    worker = _RenderWorker()
    controller, item = _controller(view_box, worker)
    controller.set_document(_document(tmp_path / "layout.gds"))
    request = worker.requests[-1]

    worker.frame_ready.emit(_frame(request, generation=request.config.generation - 1))
    assert item.frame is None

    stale = _frame(request)
    view_box.change((200.0, 200.0, 300.0, 250.0))
    worker.frame_ready.emit(stale)
    assert item.frame is None


def test_same_path_reuses_worker_path_change_detaches_and_unload_joins_bounded(
    qt_app: QApplication, tmp_path: Path
) -> None:
    view_box = _ViewBox()
    workers: list[_RenderWorker] = []

    def factory() -> _RenderWorker:
        worker = _RenderWorker()
        workers.append(worker)
        return worker

    controller = KLayoutRasterController(
        view_box,
        KLayoutRasterItem(),
        render_worker_factory=factory,
        device_pixel_ratio=lambda: 1.0,
    )
    document = _document(tmp_path / "layout.gds")
    controller.set_document(document)
    first_generation = controller.config.generation
    controller.set_document(document.with_visible_layers({(2, 0)}))

    assert len(workers) == 1
    assert controller.config.generation > first_generation

    controller.set_document(_document(tmp_path / "other.gds"))
    assert len(workers) == 2
    assert workers[0].stop_calls == [0.0]

    controller.set_document(None)
    assert workers[1].stop_calls == [0.5]
    assert controller.config is None
