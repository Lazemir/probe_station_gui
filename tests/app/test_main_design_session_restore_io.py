from __future__ import annotations

# ruff: noqa: E402 -- Qt mode and import-reset must precede Main imports.

import os
import threading
import types
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from tests.app.import_reset import restore_real_imports_for_main

restore_real_imports_for_main()

from probe_station_gui.application import design_load
from probe_station_gui.application.design_load import _LoadedDesignDocument
from probe_station_gui.coordinates.coordinator_model import (
    CoordinateSystemSnapshot,
    CoordinateTransition,
)
from probe_station_gui.route.model import MeasurementRoute, RouteDesignBinding
from probe_station_gui.design.session import DesignSession
from probe_station_gui.design.session_state import PreparedDesignSessionRestore
from tests.app.test_main_design_navigation import (
    Main,
    _make_document,
    _make_window,
    main_module,
)


def test_persisted_design_file_and_route_io_run_on_document_load_thread(
    monkeypatch,
    tmp_path: Path,
) -> None:
    window, _stage_controller, _statuses = _make_window()
    document = _make_document(tmp_path)
    route = MeasurementRoute.default_for_document(document, name="Restored route")
    route.add_point((12.0, 34.0))
    route_path = route.save(tmp_path / "restored.probe-route.json")
    design_stat = document.path.stat()
    restore_state: dict[str, object] = {
        "version": 2,
        "document_path": str(document.path),
        "document_size": design_stat.st_size,
        "document_mtime_ns": design_stat.st_mtime_ns,
        "top_cell_name": document.top_cell_name,
        "rotation_quarter_turns": 0,
        "visible_layers": [[1, 0]],
        "source_design_marks": [],
        "source_stage_marks": [],
        "check_design_marks": [],
        "check_stage_marks": [],
        "route": {
            "path": str(route_path),
            "selected_route_point_index": 0,
        },
    }
    window._pending_persisted_design_state = restore_state
    window._pending_persisted_design_position = (1.0, 2.0, 3.0)
    window._coordinate_system_coordinator = types.SimpleNamespace(
        current_design_lease=lambda: types.SimpleNamespace(document=None),
        cancel_registration=lambda _reason: CoordinateTransition(
            CoordinateSystemSnapshot(False, (), None)
        ),
    )
    window._begin_design_markup_load = lambda *_args, **_kwargs: None
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda *_args: None,
    )
    monkeypatch.setattr(design_load.DesignDocument, "load", lambda _path: document)

    emitted = threading.Event()
    publications: list[tuple[object, ...]] = []

    def record_publication(*args: object) -> None:
        publications.append(args)
        emitted.set()

    window.design_document_loaded = types.SimpleNamespace(emit=record_publication)
    observed: dict[str, list[str]] = {"stat": [], "route_load": [], "sha": []}
    original_stat = Path.stat
    original_route_load = MeasurementRoute.load.__func__
    original_validate = RouteDesignBinding.validate_document

    def tracked_stat(path: Path, *args: object, **kwargs: object):
        observed["stat"].append(threading.current_thread().name)
        return original_stat(path, *args, **kwargs)

    def tracked_route_load(
        cls: type[MeasurementRoute], path: str | Path
    ) -> MeasurementRoute:
        observed["route_load"].append(threading.current_thread().name)
        return original_route_load(cls, path)

    def tracked_validate(binding: RouteDesignBinding, loaded_document: object) -> None:
        observed["sha"].append(threading.current_thread().name)
        original_validate(binding, loaded_document)

    monkeypatch.setattr(Path, "stat", tracked_stat)
    monkeypatch.setattr(MeasurementRoute, "load", classmethod(tracked_route_load))
    monkeypatch.setattr(RouteDesignBinding, "validate_document", tracked_validate)

    design_load.design_workspace.maybe_restore_persisted_design(
        window,
        (1.0, 2.0, 3.0),
    )
    assert emitted.wait(5.0)
    generation, payload, error = publications[-1]
    Main._on_design_document_loaded(window, generation, payload, error)

    assert observed["stat"]
    assert observed["route_load"] == ["DesignDocumentLoad"]
    assert observed["sha"] == ["DesignDocumentLoad"]
    assert set(observed["stat"]) == {"DesignDocumentLoad"}


@pytest.mark.parametrize("missing", [False, True])
def test_changed_or_missing_persisted_design_keeps_exact_clear_status(
    monkeypatch,
    tmp_path: Path,
    missing: bool,
) -> None:
    window, _stage_controller, statuses = _make_window()
    document = _make_document(tmp_path)
    design_path = document.path
    saved_stat = design_path.stat()
    if missing:
        design_path.unlink()
        saved_size = saved_stat.st_size
    else:
        saved_size = saved_stat.st_size + 1
    window._pending_persisted_design_state = {
        "document_path": str(design_path),
        "document_size": saved_size,
        "document_mtime_ns": saved_stat.st_mtime_ns,
    }
    window._pending_persisted_design_position = (1.0, 2.0, 3.0)
    window._coordinate_system_coordinator = types.SimpleNamespace(
        current_design_lease=lambda: types.SimpleNamespace(document=None),
        cancel_registration=lambda _reason: CoordinateTransition(
            CoordinateSystemSnapshot(False, (), None)
        ),
    )
    monkeypatch.setattr(
        main_module.coordinate_flow,
        "apply_coordinate_transition",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        design_load.design_workspace,
        "save_controller_state_without_design",
        lambda _owner: statuses.append("saved_without_design"),
    )
    monkeypatch.setattr(
        design_load.DesignDocument,
        "load",
        lambda _path: pytest.fail("changed or missing design must not be parsed"),
    )
    emitted = threading.Event()
    publications: list[tuple[object, ...]] = []

    def record_publication(*args: object) -> None:
        publications.append(args)
        emitted.set()

    window.design_document_loaded = types.SimpleNamespace(emit=record_publication)

    design_load.design_workspace.maybe_restore_persisted_design(
        window,
        (1.0, 2.0, 3.0),
    )
    assert emitted.wait(5.0)
    Main._on_design_document_loaded(window, *publications[-1])

    assert statuses[-2:] == [
        "Cached design file changed or is unavailable. "
        "Cleared cached design selection.",
        "saved_without_design",
    ]


def test_stale_generation_discards_prepared_session_restore(tmp_path: Path) -> None:
    window, _stage_controller, _statuses = _make_window()
    baseline = window._design_session.snapshot_state()
    document = _make_document(tmp_path)
    prepared = DesignSession(document=document)
    prepared.targets = []
    metadata = main_module.DesignFrameMetadata.from_document(document)
    payload = _LoadedDesignDocument(
        document,
        metadata,
        PreparedDesignSessionRestore(prepared.snapshot_state()),
    )
    window._design_load_generation = 2
    started: list[object] = []
    window._begin_design_markup_load = lambda *args, **kwargs: started.append(
        (args, kwargs)
    )

    Main._on_design_document_loaded(window, 1, payload, None)

    assert window._design_session.snapshot_state() == baseline
    assert started == []
