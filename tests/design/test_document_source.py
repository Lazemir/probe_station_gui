from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from probe_station_gui.design.document_source import (
    DocumentSource,
    DocumentSourceError,
    load_fixture_source,
)
from probe_station_gui.design.model import DesignDocument


class _FixtureCell:
    def __init__(self, name: str, polygons_by_spec: object) -> None:
        self.name = name
        self._polygons_by_spec = polygons_by_spec

    def get_polygons(self, **_kwargs: object) -> object:
        return self._polygons_by_spec


class _FixtureLibrary:
    unit = 1e-6
    precision = 1e-9

    def __init__(self, cells: list[_FixtureCell]) -> None:
        self.cells = cells


def test_document_source_owns_its_interface_and_replaced_helpers() -> None:
    from probe_station_gui.design import document_source

    assert DocumentSource.__module__ == "probe_station_gui.design.document_source"
    assert set(document_source.__all__) == {
        "DocumentSource",
        "DocumentSourceError",
        "load_file_source",
        "load_fixture_source",
        "polygon_bounds",
    }
    assert not hasattr(DesignDocument, "_extract_fixture_polygons")
    assert not hasattr(DesignDocument, "_calculate_bounds")
    assert not hasattr(DesignDocument, "_klayout_bounds")


def test_fixture_source_normalizes_cells_layers_units_and_bounds() -> None:
    top = _FixtureCell(
        "TOP",
        {
            (2, 1): [np.asarray([[3.0, 4.0], [7.0, 4.0], [7.0, 9.0]])],
            (1, 0): [np.asarray([[-2.0, 1.0], [1.0, 1.0], [1.0, 2.0]])],
            "invalid": [np.asarray([[0.0, 0.0], [1.0, 1.0]])],
        },
    )
    alternate = _FixtureCell("ALT", {})
    library = _FixtureLibrary([top, alternate])

    source = load_fixture_source(
        path=Path("synthetic.gds"),
        library=library,
        top_cell_name="TOP",
    )

    assert source.path == Path("synthetic.gds")
    assert source.library is library
    assert source.top_cell is top
    assert source.top_cell_name == "TOP"
    assert source.cell_names == ("ALT", "TOP")
    assert tuple(source.polygons_by_layer) == ((1, 0), (2, 1))
    assert source.available_layers == frozenset({(1, 0), (2, 1)})
    assert source.bounds == (-2.0, 1.0, 7.0, 9.0)
    assert source.dbu == pytest.approx(1e-6)
    assert source.user_unit == pytest.approx(1e-9)
    assert source.file_backed is False
    assert source.source_load_id is None


def test_fixture_source_rejects_missing_or_empty_named_cell() -> None:
    library = _FixtureLibrary([_FixtureCell("EMPTY", {})])

    with pytest.raises(DocumentSourceError, match="was not found"):
        load_fixture_source(
            path=Path("synthetic.gds"),
            library=library,
            top_cell_name="MISSING",
        )

    with pytest.raises(DocumentSourceError, match="has no polygon geometry"):
        load_fixture_source(
            path=Path("synthetic.gds"),
            library=library,
            top_cell_name="EMPTY",
        )


def test_design_document_uses_the_canonical_fixture_source_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from probe_station_gui.design import document_source

    calls: list[str] = []
    original = document_source.load_fixture_source

    def observe(**kwargs: object):
        calls.append(str(kwargs["top_cell_name"]))
        return original(**kwargs)

    monkeypatch.setattr(document_source, "load_fixture_source", observe)
    library = _FixtureLibrary(
        [
            _FixtureCell(
                "TOP",
                {(1, 0): [np.asarray([[0.0, 0.0], [1.0, 0.0]])]},
            )
        ]
    )

    document = DesignDocument._from_components(
        path=Path("synthetic.gds"),
        library=library,
        top_cell_name="TOP",
    )

    assert document.top_cell_name == "TOP"
    assert calls == ["TOP"]
