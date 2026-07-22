from __future__ import annotations

from pathlib import Path
import threading
import time

import klayout.db as db
from PySide6.QtWidgets import QApplication
import pytest

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    StructureBoundsRequest,
    StructureBoundsResult,
)
from probe_station_gui.design.klayout_workers import (
    KLayoutStructureBoundsWorker,
    _KLayoutStructureBoundsBackend,
)


def _write_hierarchical_design(path: Path) -> None:
    layout = db.Layout()
    layout.dbu = 0.001
    visible = layout.layer(1, 0)
    hidden = layout.layer(2, 0)
    top = layout.create_cell("TOP")
    inactive = layout.create_cell("INACTIVE")
    child = layout.create_cell("CHILD")
    child.shapes(visible).insert(db.Box(0, 0, 2_000, 1_000))
    child.shapes(hidden).insert(db.Box(0, 0, 9_000, 9_000))
    top.insert(db.CellInstArray(child.cell_index(), db.Trans(10_000, 20_000)))
    inactive.shapes(visible).insert(db.Box(100_000, 100_000, 120_000, 120_000))
    layout.write(str(path))


def _config(path: Path, *, turns: int = 0, generation: int = 1) -> KLayoutConfig:
    source_bounds = (10.0, 20.0, 12.0, 21.0)
    display_bounds = source_bounds if turns % 2 == 0 else (10.5, 19.5, 11.5, 21.5)
    return KLayoutConfig(
        path=path,
        top_cell_name="TOP",
        visible_layers=frozenset({(1, 0)}),
        source_bounds=source_bounds,
        display_bounds=display_bounds,
        rotation_quarter_turns=turns,
        generation=generation,
        source_load_id="load-1",
    )


def test_klayout_structure_bounds_follow_hierarchy_visibility_top_and_rotation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hierarchy.gds"
    _write_hierarchical_design(path)
    backend = _KLayoutStructureBoundsBackend()

    plain = backend.query(
        StructureBoundsRequest(request_id=1, generation=1, config=_config(path))
    )
    rotated = backend.query(
        StructureBoundsRequest(
            request_id=2,
            generation=2,
            config=_config(path, turns=1, generation=2),
        )
    )

    assert plain.structure_bounds == ((10.0, 20.0, 12.0, 21.0),)
    assert rotated.structure_bounds[0] == pytest.approx(
        (10.5, 19.5, 11.5, 21.5)
    )
    backend.close()


def test_fixture_polygons_use_same_structure_bounds_result_interface() -> None:
    backend = _KLayoutStructureBoundsBackend()
    result = backend.query(
        StructureBoundsRequest(
            request_id=7,
            generation=3,
            fixture_polygons=(
                ((10.0, 20.0), (12.0, 20.0), (12.0, 21.0), (10.0, 21.0)),
            ),
        )
    )

    assert isinstance(result, StructureBoundsResult)
    assert result.structure_bounds == ((10.0, 20.0, 12.0, 21.0),)
    backend.close()


def test_structure_worker_ignores_stale_generation_result() -> None:
    app = QApplication.instance() or QApplication([])
    entered = threading.Event()
    release = threading.Event()

    class Backend:
        def query(self, request: StructureBoundsRequest) -> StructureBoundsResult:
            if request.request_id == 1:
                entered.set()
                release.wait(2.0)
            return StructureBoundsResult(
                request_id=request.request_id,
                generation=request.generation,
                structure_bounds=((float(request.request_id), 0.0, 2.0, 2.0),),
            )

        def close(self) -> None:
            return None

    worker = KLayoutStructureBoundsWorker(backend_factory=Backend)
    delivered: list[int] = []
    worker.ready.connect(lambda result: delivered.append(result.request_id))
    worker.submit(StructureBoundsRequest(request_id=1, generation=1, fixture_polygons=((),)))
    assert entered.wait(1.0)
    worker.submit(StructureBoundsRequest(request_id=2, generation=2, fixture_polygons=((),)))
    release.set()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and delivered != [2]:
        app.processEvents()
        time.sleep(0.005)

    assert delivered == [2]
    worker.stop()
