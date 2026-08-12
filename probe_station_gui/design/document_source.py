"""Prepared file and fixture sources for design documents."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class DocumentSourceError(RuntimeError):
    """Raised when a design source cannot be normalized."""


@dataclass(frozen=True)
class DocumentSource:
    """Normalized source values used to construct one design document."""

    path: Path
    library: Any
    top_cell: Any
    top_cell_name: str
    cell_names: tuple[str, ...]
    dbu: float
    user_unit: float
    bounds: tuple[float, float, float, float]
    polygons_by_layer: dict[tuple[int, int], tuple[Any, ...]]
    available_layers: frozenset[tuple[int, int]]
    cell_bounds: Mapping[str, tuple[float, float, float, float]]
    file_backed: bool
    source_load_id: str | None


def load_file_source(path: str | Path) -> DocumentSource:
    """Read lightweight metadata from one GDS/OASIS file using KLayout."""

    resolved = Path(path).expanduser().resolve()
    try:
        import klayout.db as db
    except ImportError as exc:
        raise DocumentSourceError(
            "KLayout is not installed. Install it to enable GDS design navigation."
        ) from exc

    layout = db.Layout()
    try:
        layout.read(str(resolved))
    except Exception as exc:  # pragma: no cover - KLayout specific
        raise DocumentSourceError(f"Failed to load design '{resolved}': {exc}") from exc

    cells = tuple(layout.each_cell())
    if not cells:
        raise DocumentSourceError(f"Design '{resolved}' does not contain any cells.")
    top_level = tuple(layout.top_cells())
    top_cell = top_level[0] if top_level else cells[0]
    cell_names = tuple(sorted(str(cell.name) for cell in cells if str(cell.name)))
    if not cell_names:
        raise DocumentSourceError(f"Design '{resolved}' does not expose named cells.")
    cell_bounds = {
        str(cell.name): _klayout_bounds(cell.bbox(), layout.dbu)
        for cell in cells
        if str(cell.name) and not cell.bbox().empty()
    }
    top_cell_name = str(top_cell.name or cell_names[0])
    if top_cell_name not in cell_bounds:
        geometry_cell_name = next(
            (
                str(cell.name)
                for group in (top_level or cells, cells)
                for cell in group
                if str(cell.name) in cell_bounds
            ),
            None,
        )
        if geometry_cell_name is not None:
            top_cell_name = geometry_cell_name
    if top_cell_name not in cell_bounds:
        raise DocumentSourceError(
            f"Top cell '{top_cell_name}' has no polygon geometry."
        )

    available_layers = frozenset(
        (int(info.layer), int(info.datatype)) for info in layout.layer_infos()
    )
    bounds = cell_bounds[top_cell_name]
    user_unit = float(layout.dbu) * 1e-6
    del cells, top_level, top_cell, layout
    return DocumentSource(
        path=resolved,
        library=None,
        top_cell=None,
        top_cell_name=top_cell_name,
        cell_names=cell_names,
        dbu=1e-6,
        user_unit=user_unit,
        bounds=bounds,
        polygons_by_layer={},
        available_layers=available_layers,
        cell_bounds=cell_bounds,
        file_backed=True,
        source_load_id=uuid.uuid4().hex,
    )


def load_fixture_source(
    *,
    path: Path,
    library: Any,
    top_cell_name: str,
) -> DocumentSource:
    """Normalize one legacy in-memory fixture without a GDS dependency."""

    cells = tuple(getattr(library, "cells", ()))
    cell_by_name = {
        str(getattr(cell, "name", "")): cell
        for cell in cells
        if getattr(cell, "name", "")
    }
    if top_cell_name not in cell_by_name:
        raise DocumentSourceError(
            f"Cell '{top_cell_name}' was not found in '{path.name}'."
        )
    top_cell = cell_by_name[top_cell_name]
    polygons_by_layer = _fixture_polygons(top_cell)
    if not polygons_by_layer:
        raise DocumentSourceError(
            f"Top cell '{top_cell_name}' has no polygon geometry."
        )
    dbu = float(getattr(library, "unit", 1e-6))
    precision = float(getattr(library, "precision", dbu))
    return DocumentSource(
        path=path,
        library=library,
        top_cell=top_cell,
        top_cell_name=top_cell_name,
        cell_names=tuple(sorted(cell_by_name)),
        dbu=dbu,
        user_unit=precision if precision > 0 else dbu,
        bounds=polygon_bounds(polygons_by_layer),
        polygons_by_layer=polygons_by_layer,
        available_layers=frozenset(polygons_by_layer),
        cell_bounds={},
        file_backed=False,
        source_load_id=None,
    )


def polygon_bounds(
    polygons_by_layer: Mapping[tuple[int, int], tuple[Any, ...]],
) -> tuple[float, float, float, float]:
    """Return the axis-aligned bounds of normalized polygon arrays."""

    import numpy as np

    mins_x: list[float] = []
    mins_y: list[float] = []
    maxs_x: list[float] = []
    maxs_y: list[float] = []
    for polygons in polygons_by_layer.values():
        for polygon in polygons:
            mins_x.append(float(np.min(polygon[:, 0])))
            mins_y.append(float(np.min(polygon[:, 1])))
            maxs_x.append(float(np.max(polygon[:, 0])))
            maxs_y.append(float(np.max(polygon[:, 1])))
    if not mins_x:
        raise DocumentSourceError("Unable to determine document bounds.")
    return (min(mins_x), min(mins_y), max(maxs_x), max(maxs_y))


def _fixture_polygons(cell: Any) -> dict[tuple[int, int], tuple[Any, ...]]:
    import numpy as np

    polygons_by_layer: dict[tuple[int, int], list[Any]] = {}
    polygon_map: Any = None
    if hasattr(cell, "get_polygons"):
        try:
            polygon_map = cell.get_polygons(apply_repetitions=True, by_spec=True)
        except TypeError:
            try:
                polygon_map = cell.get_polygons(by_spec=True)
            except Exception:
                polygon_map = None
        except Exception:
            polygon_map = None
    if isinstance(polygon_map, dict):
        for spec, polygons in polygon_map.items():
            if not isinstance(spec, tuple) or len(spec) < 2:
                continue
            layer = (int(spec[0]), int(spec[1]))
            for polygon in polygons:
                points = np.asarray(polygon, dtype=float)
                if points.ndim != 2 or points.shape[1] != 2 or len(points) < 2:
                    continue
                polygons_by_layer.setdefault(layer, []).append(points)
    if polygons_by_layer:
        return {
            layer: tuple(polygons)
            for layer, polygons in sorted(polygons_by_layer.items())
        }

    raw_polygons = getattr(cell, "polygons", ())
    for polygon in raw_polygons:
        layer = int(getattr(polygon, "layer", 0))
        datatype = int(getattr(polygon, "datatype", 0))
        points = np.asarray(getattr(polygon, "points", ()), dtype=float)
        if points.ndim != 2 or points.shape[1] != 2 or len(points) < 2:
            continue
        polygons_by_layer.setdefault((layer, datatype), []).append(points)
    return {
        layer: tuple(polygons) for layer, polygons in sorted(polygons_by_layer.items())
    }


def _klayout_bounds(box: Any, dbu: float) -> tuple[float, float, float, float]:
    design_box = box.to_dtype(float(dbu))
    return (
        float(design_box.left),
        float(design_box.bottom),
        float(design_box.right),
        float(design_box.top),
    )


__all__ = [
    "DocumentSource",
    "DocumentSourceError",
    "load_file_source",
    "load_fixture_source",
    "polygon_bounds",
]
