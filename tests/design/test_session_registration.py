from __future__ import annotations

from pathlib import Path

from probe_station_gui.design import session_registration
from probe_station_gui.design.model import DesignDocument
from probe_station_gui.design.session import DesignSession
from probe_station_gui.design.session_registration import (
    apply_prepared_alignment,
    build_registration,
    invalidate_registration,
    prepare_source_alignment,
    rebuild_registration,
)


def _document(tmp_path: Path) -> DesignDocument:
    return DesignDocument(
        path=tmp_path / "registration.gds",
        library=object(),
        top_cell=object(),
        top_cell_name="TOP",
        cell_names=("TOP",),
        dbu=1e-6,
        user_unit=1e-9,
        bounds=(0.0, 0.0, 1000.0, 1000.0),
        polygons_by_layer={},
        visible_layers=frozenset(),
    )


def test_registration_owner_fits_invalidates_and_prepares_alignment(
    tmp_path: Path,
) -> None:
    session = DesignSession(document=_document(tmp_path))
    session.source_design_marks = ((0.0, 0.0), (1000.0, 0.0))
    session.source_stage_marks = ((10.0, 10.0), (10.0, 11.0))

    preparation = prepare_source_alignment(session)
    apply_prepared_alignment(session, preparation)

    assert session.registration is not None
    assert session.registration.valid
    assert preparation.rotation_deg == -90.0

    invalidate_registration(session, "Controller reset.")

    assert session.registration is not None
    assert not session.registration.valid
    assert session.registration.stale_reason == "Controller reset."

    rebuild_registration(session)
    assert session.registration is not None
    assert session.registration.valid


def test_build_registration_returns_detached_status(tmp_path: Path) -> None:
    document = _document(tmp_path)

    registration, status = build_registration(
        document,
        ((0.0, 0.0), (1000.0, 0.0)),
        ((10.0, 10.0), (11.0, 10.0)),
        (),
        (),
    )

    assert registration is not None
    assert registration.valid
    assert status.startswith("Registered. Fit RMS")


def test_registration_owner_has_no_legacy_forwarding_facade() -> None:
    assert not hasattr(session_registration, "block_legacy_registration")
