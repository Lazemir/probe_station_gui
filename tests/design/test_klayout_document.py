from dataclasses import replace
from pathlib import Path

import klayout.db as db
import pytest

from probe_station_gui.design.model import DesignDocument


def _write_design(path: Path) -> None:
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell("TOP")
    alternate = layout.create_cell("ALT")
    top.shapes(layout.layer(1, 0)).insert(db.Box(1_000, 2_000, 11_000, 6_000))
    alternate.shapes(layout.layer(2, 3)).insert(
        db.Box(20_000, 10_000, 22_000, 18_000)
    )
    layout.write(str(path))


def test_load_keeps_only_klayout_document_metadata(tmp_path: Path) -> None:
    design_path = tmp_path / "metadata.gds"
    _write_design(design_path)

    document = DesignDocument.load(design_path)

    assert document.file_backed is True
    assert document.library is None
    assert document.top_cell is None
    assert document.top_cell_name == "ALT"
    assert document.cell_names == ("ALT", "TOP")
    assert document.available_layers == frozenset({(1, 0), (2, 3)})
    assert document.layer_keys() == ((1, 0), (2, 3))
    assert document.visible_layers == document.available_layers
    assert document.cell_bounds == {
        "ALT": (20.0, 10.0, 22.0, 18.0),
        "TOP": (1.0, 2.0, 11.0, 6.0),
    }
    assert document.bounds == (20.0, 10.0, 22.0, 18.0)
    assert document.dbu == pytest.approx(1e-6)
    assert document.user_unit == pytest.approx(1e-9)
    assert document.polygons_by_layer == {}
    assert document.plot_paths_by_layer == {}
    assert document.snap_vertices.size == 0
    assert document.snap_segment_starts.size == 0
    assert document.snap_segment_ends.size == 0


def test_fresh_load_has_new_source_identity_preserved_by_document_changes(
    tmp_path: Path,
) -> None:
    design_path = tmp_path / "identity.gds"
    _write_design(design_path)

    first = DesignDocument.load(design_path)
    second = DesignDocument.load(design_path)

    assert first.source_load_id
    assert second.source_load_id
    assert first.source_load_id != second.source_load_id
    assert replace(first, visible_layers=frozenset({(2, 3)})).source_load_id == first.source_load_id
    assert first.with_visible_layers({(2, 3)}).source_load_id == first.source_load_id
    assert first.with_top_cell("TOP").source_load_id == first.source_load_id
    assert first.with_rotation_delta(1).source_load_id == first.source_load_id


def test_file_backed_layer_and_cell_changes_do_not_materialize_polygons(
    tmp_path: Path,
) -> None:
    design_path = tmp_path / "metadata.gds"
    _write_design(design_path)
    document = DesignDocument.load(design_path)

    filtered = document.with_visible_layers({(2, 3)})
    alternate = filtered.with_top_cell("TOP")

    assert filtered.visible_layers == frozenset({(2, 3)})
    assert alternate.top_cell_name == "TOP"
    assert alternate.bounds == (1.0, 2.0, 11.0, 6.0)
    assert filtered.polygons_by_layer == {}
    assert alternate.polygons_by_layer == {}


def test_file_backed_odd_rotation_swaps_bounds_around_the_same_center(
    tmp_path: Path,
) -> None:
    design_path = tmp_path / "metadata.gds"
    _write_design(design_path)
    document = DesignDocument.load(design_path)

    rotated = document.with_rotation_delta(1)

    original_width = document.bounds[2] - document.bounds[0]
    original_height = document.bounds[3] - document.bounds[1]
    rotated_width = rotated.bounds[2] - rotated.bounds[0]
    rotated_height = rotated.bounds[3] - rotated.bounds[1]
    original_center = (
        (document.bounds[0] + document.bounds[2]) * 0.5,
        (document.bounds[1] + document.bounds[3]) * 0.5,
    )
    rotated_center = (
        (rotated.bounds[0] + rotated.bounds[2]) * 0.5,
        (rotated.bounds[1] + rotated.bounds[3]) * 0.5,
    )

    assert rotated.rotation_quarter_turns == 1
    assert rotated_width == pytest.approx(original_height)
    assert rotated_height == pytest.approx(original_width)
    assert rotated_center == pytest.approx(original_center)
    assert rotated.polygons_by_layer == {}
    assert rotated.plot_paths_by_layer == {}
