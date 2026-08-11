from __future__ import annotations

from dataclasses import replace
import inspect
from pathlib import Path

from probe_station_gui.coordinates.application_runtime import (
    ApplicationCoordinateRuntime,
)
from probe_station_gui.coordinates.coordinator import CoordinateSystemCoordinator
from probe_station_gui.design.model import DesignDocument, MeasurementTarget
from probe_station_gui.design.session import DesignSession
from probe_station_gui.route.model import MeasurementRoute


def _document(tmp_path: Path) -> DesignDocument:
    source = tmp_path / "chip.gds"
    source.write_bytes(b"layout")
    return DesignDocument(
        path=source,
        library=None,
        top_cell=None,
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-3,
        user_unit=1e-6,
        polygons_by_layer={},
        visible_layers=frozenset({(1, 0)}),
        bounds=(0.0, 0.0, 1000.0, 1000.0),
    )


def test_controller_persistence_combines_workspace_route_with_coordinate_frame(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _document(tmp_path)
    coordinate_session = DesignSession(
        document=document,
        active_frame_id="authoritative-frame",
    )
    coordinator = CoordinateSystemCoordinator._adopt_session(
        session=coordinate_session
    )
    runtime = ApplicationCoordinateRuntime(coordinator=coordinator)

    workspace = DesignSession(
        document=replace(document, visible_layers=frozenset({(2, 0)})),
        active_frame_id="stale-workspace-frame",
    )
    workspace.targets = [
        MeasurementTarget("target-1", "Target 1", (10.0, 20.0))
    ]
    workspace.selected_target_index = 0
    workspace.route = MeasurementRoute.default_for_document(document)
    workspace.route.add_point((10.0, 20.0))
    route_path = workspace.route.save(tmp_path / "route.probe-route.json")
    workspace.selected_route_point_index = 0
    with monkeypatch.context() as filesystem:
        filesystem.setattr(
            Path,
            "stat",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("coordinator persistence touched the filesystem")
            ),
        )
        filesystem.setattr(
            Path,
            "resolve",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError(
                    "coordinator persistence resolved a filesystem path"
                )
            ),
        )
        persisted = runtime.controller_persistence_state(
            workspace.snapshot_state()
        )

    assert persisted is not None
    assert persisted["active_frame_id"] == "authoritative-frame"
    assert persisted["visible_layers"] == [[2, 0]]
    assert persisted["route"] == {
        "path": str(route_path),
        "selected_route_point_index": 0,
    }
    assert coordinate_session.route is None


def test_runtime_uses_public_detached_coordinator_projection() -> None:
    source = inspect.getsource(ApplicationCoordinateRuntime)

    assert "._controller_persistence_state" not in source
    assert callable(CoordinateSystemCoordinator().controller_persistence_state)
