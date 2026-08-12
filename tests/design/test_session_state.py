from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
import subprocess
import sys

import pytest

from probe_station_gui.design import (
    session as session_module,
    session_navigation,
    session_registration,
)
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.design.session import DesignSession
from probe_station_gui.design.session_state import (
    DesignSessionState,
    PreparedDesignSessionRestore,
    apply_prepared_session_restore,
    prepare_persisted_session_restore,
)
from probe_station_gui.route.model import MeasurementRoute


def test_session_domains_have_canonical_identities_without_legacy_wrappers() -> None:
    assert session_module.__all__ == ["DesignSession"]
    assert session_module.DesignSession is DesignSession
    assert DesignSessionState.__module__ == "probe_station_gui.design.session_state"
    assert (
        PreparedDesignSessionRestore.__module__
        == "probe_station_gui.design.session_state"
    )
    assert (
        session_registration.AlignmentPreparation.__module__
        == "probe_station_gui.design.session_registration"
    )
    assert (
        session_registration.DesignFrameLinkProjection.__module__
        == "probe_station_gui.design.session_registration"
    )
    assert session_navigation.__all__ == []
    for accidental_name in (
        "AlignmentPreparation",
        "DesignFrameLinkProjection",
        "DesignSessionState",
        "MeasurementRoute",
    ):
        assert not hasattr(session_module, accidental_name)
    for legacy_wrapper in (
        "export_persisted_state",
        "restore_persisted_state",
        "load_document",
        "rotate_document",
        "link_active_frame",
        "select_route_point",
    ):
        assert not hasattr(DesignSession, legacy_wrapper)


@pytest.mark.parametrize(
    "modules",
    [
        (
            "probe_station_gui.design.session",
            "probe_station_gui.design.session_state",
            "probe_station_gui.design.session_registration",
            "probe_station_gui.design.session_navigation",
        ),
        (
            "probe_station_gui.design.session_navigation",
            "probe_station_gui.design.session_registration",
            "probe_station_gui.design.session_state",
            "probe_station_gui.design.session",
        ),
    ],
)
def test_session_domain_import_orders_remain_qt_and_klayout_free(
    modules: tuple[str, ...],
) -> None:
    script = (
        "import importlib, sys; "
        f"[importlib.import_module(name) for name in {modules!r}]; "
        "forbidden = [name for name in sys.modules "
        "if name == 'PySide6' or name.startswith('PySide6.') "
        "or name == 'klayout' or name.startswith('klayout.')]; "
        "assert not forbidden, forbidden"
    )

    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


def _document(path: Path) -> DesignDocument:
    path.write_bytes(b"design")
    return DesignDocument(
        path=path,
        library=object(),
        top_cell=object(),
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-6,
        user_unit=1e-9,
        bounds=(0.0, 0.0, 100.0, 100.0),
        polygons_by_layer={},
        visible_layers=frozenset(),
    )


def test_prepare_restore_parses_route_and_apply_performs_no_file_io(
    monkeypatch,
    tmp_path: Path,
) -> None:
    document = _document(tmp_path / "design.gds")
    route = MeasurementRoute.default_for_document(document, name="Prepared")
    route.add_point((10.0, 20.0))
    route_path = route.save(tmp_path / "prepared.probe-route.json")
    payload: dict[str, object] = {
        "version": 2,
        "document_path": str(document.path),
        "source_design_marks": [],
        "source_stage_marks": [],
        "check_design_marks": [],
        "check_stage_marks": [],
        "route": {
            "path": str(route_path),
            "selected_route_point_index": 99,
        },
    }

    prepared = prepare_persisted_session_restore(document, payload)

    assert isinstance(prepared, PreparedDesignSessionRestore)
    assert prepared.state.route is not None
    assert prepared.state.selected_route_point_index == 0
    with pytest.raises(FrozenInstanceError):
        prepared.state = prepared.state  # type: ignore[misc]

    def unexpected_io(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("prepared restore apply must not touch the filesystem")

    monkeypatch.setattr(Path, "stat", unexpected_io)
    monkeypatch.setattr(Path, "exists", unexpected_io)
    monkeypatch.setattr(Path, "read_text", unexpected_io)
    target = DesignSession()

    apply_prepared_session_restore(target, prepared)

    assert target.document is document
    assert target.route is not None
    assert target.route.name == "Prepared"
    assert target.selected_route_point_index == 0
