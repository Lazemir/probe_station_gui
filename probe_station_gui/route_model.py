"""Serializable design-bound probe routes."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .design_model import DesignDocument, DesignModelError, MeasurementTarget, Point2D


ROUTE_FORMAT = "probe_station_probe_route"
ROUTE_VERSION = 1


class RouteModelError(DesignModelError):
    """Raised when a probe route cannot be loaded or matched to a design."""


@dataclass(frozen=True)
class RouteDesignBinding:
    """Identity of the design a route was created against."""

    path: str
    sha256: str
    top_cell_name: str
    bounds: tuple[float, float, float, float]
    dbu: float

    @classmethod
    def from_document(cls, document: DesignDocument) -> "RouteDesignBinding":
        path = Path(document.path).expanduser().resolve()
        return cls(
            path=str(path),
            sha256=_file_sha256(path),
            top_cell_name=str(document.top_cell_name),
            bounds=tuple(float(value) for value in document.bounds),
            dbu=float(document.dbu),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RouteDesignBinding":
        try:
            bounds = value["bounds"]
            if len(bounds) != 4:
                raise ValueError
            return cls(
                path=str(value.get("path", "")),
                sha256=str(value["sha256"]),
                top_cell_name=str(value["top_cell_name"]),
                bounds=tuple(float(item) for item in bounds),
                dbu=float(value.get("dbu", 0.0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RouteModelError("Route file has an invalid design binding.") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "top_cell_name": self.top_cell_name,
            "bounds": list(self.bounds),
            "dbu": self.dbu,
        }

    def validate_document(self, document: DesignDocument) -> None:
        """Raise if the route does not belong to the currently loaded design."""

        if str(document.top_cell_name) != self.top_cell_name:
            raise RouteModelError(
                "Route was created for top cell "
                f"'{self.top_cell_name}', but current top cell is "
                f"'{document.top_cell_name}'."
            )
        actual_hash = _file_sha256(Path(document.path).expanduser().resolve())
        if actual_hash != self.sha256:
            raise RouteModelError(
                "Route design fingerprint does not match the loaded design file."
            )
        if not _bounds_match(self.bounds, document.bounds):
            raise RouteModelError("Route design bounds do not match the loaded design.")


@dataclass(frozen=True)
class NeedleOffset:
    """Design-space offset from camera crosshair center to one needle contact."""

    id: str
    label: str
    dx: float = 0.0
    dy: float = 0.0
    source: str = "manual"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "NeedleOffset":
        try:
            return cls(
                id=str(value["id"]),
                label=str(value.get("label", value["id"])),
                dx=float(value.get("dx", 0.0)),
                dy=float(value.get("dy", 0.0)),
                source=str(value.get("source", "manual")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RouteModelError("Route file has an invalid needle offset.") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "dx": self.dx,
            "dy": self.dy,
            "source": self.source,
        }

    def apply_to(self, center: Point2D) -> Point2D:
        return (float(center[0]) + self.dx, float(center[1]) + self.dy)


@dataclass(frozen=True)
class RoutePoint:
    """One ordered route position in design coordinates."""

    id: str
    label: str
    camera_center: Point2D
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RoutePoint":
        try:
            center = _coerce_point(value["camera_center"], "camera_center")
            point_id = str(value["id"]).strip()
            if not point_id:
                raise ValueError
            return cls(
                id=point_id,
                label=str(value.get("label", point_id)).strip() or point_id,
                camera_center=center,
                enabled=bool(value.get("enabled", True)),
                metadata=(
                    dict(value.get("metadata", {}))
                    if isinstance(value.get("metadata", {}), dict)
                    else {}
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RouteModelError("Route file has an invalid route point.") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "camera_center": [self.camera_center[0], self.camera_center[1]],
            "enabled": self.enabled,
            "metadata": dict(self.metadata),
        }

    def to_measurement_target(self) -> MeasurementTarget:
        metadata = dict(self.metadata)
        metadata["route_point_id"] = self.id
        return MeasurementTarget(
            id=self.id,
            label=self.label,
            design_center=self.camera_center,
            metadata=metadata,
            group="route",
        )


@dataclass
class MeasurementRoute:
    """A mutable route bound to one design document."""

    name: str
    design: RouteDesignBinding
    needle_offsets: list[NeedleOffset] = field(default_factory=list)
    points: list[RoutePoint] = field(default_factory=list)
    created_at_utc: str = field(default_factory=lambda: _utc_timestamp())
    updated_at_utc: str = field(default_factory=lambda: _utc_timestamp())
    path: Path | None = None

    @classmethod
    def default_for_document(
        cls, document: DesignDocument, *, name: str | None = None
    ) -> "MeasurementRoute":
        route_name = name or f"{Path(document.path).stem} route"
        return cls(
            name=route_name,
            design=RouteDesignBinding.from_document(document),
            needle_offsets=[
                NeedleOffset(id="needle_1", label="Needle 1"),
                NeedleOffset(id="needle_2", label="Needle 2"),
            ],
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MeasurementRoute":
        if value.get("format") != ROUTE_FORMAT:
            raise RouteModelError("File is not a probe route.")
        try:
            version = int(value.get("version", 0))
        except (TypeError, ValueError) as exc:
            raise RouteModelError("Route file has an invalid version.") from exc
        if version != ROUTE_VERSION:
            raise RouteModelError("Unsupported probe route version.")
        design = RouteDesignBinding.from_dict(value.get("design", {}))
        offsets = [
            NeedleOffset.from_dict(item)
            for item in value.get("needle_offsets", [])
            if isinstance(item, dict)
        ]
        if len(offsets) < 2:
            offsets = [
                NeedleOffset(id="needle_1", label="Needle 1"),
                NeedleOffset(id="needle_2", label="Needle 2"),
            ]
        points = [
            RoutePoint.from_dict(item)
            for item in value.get("points", [])
            if isinstance(item, dict)
        ]
        return cls(
            name=str(value.get("name", "Probe route")).strip() or "Probe route",
            design=design,
            needle_offsets=offsets[:2],
            points=points,
            created_at_utc=str(value.get("created_at_utc", "")) or _utc_timestamp(),
            updated_at_utc=str(value.get("updated_at_utc", "")) or _utc_timestamp(),
        )

    @classmethod
    def load(cls, path: str | Path) -> "MeasurementRoute":
        resolved = Path(path).expanduser().resolve()
        try:
            data = json.loads(resolved.read_text(encoding="utf-8-sig"))
        except OSError as exc:
            raise RouteModelError(f"Unable to read route '{resolved}': {exc}") from exc
        except json.JSONDecodeError as exc:
            raise RouteModelError(f"Route '{resolved.name}' is not valid JSON.") from exc
        if not isinstance(data, dict):
            raise RouteModelError("Route file must contain a JSON object.")
        route = cls.from_dict(data)
        route.path = resolved
        return route

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": ROUTE_FORMAT,
            "version": ROUTE_VERSION,
            "name": self.name,
            "created_at_utc": self.created_at_utc,
            "updated_at_utc": self.updated_at_utc,
            "design": self.design.to_dict(),
            "needle_offsets": [offset.to_dict() for offset in self.needle_offsets[:2]],
            "points": [point.to_dict() for point in self.points],
        }

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path).expanduser().resolve() if path is not None else self.path
        if target is None:
            raise RouteModelError("Choose a route file before saving.")
        self.updated_at_utc = _utc_timestamp()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        self.path = target
        return target

    def validate_for_document(self, document: DesignDocument) -> None:
        self.design.validate_document(document)

    def add_point(
        self,
        camera_center: Point2D,
        *,
        label: str | None = None,
    ) -> RoutePoint:
        index = len(self.points) + 1
        point_id = _next_point_id(self.points)
        route_point = RoutePoint(
            id=point_id,
            label=label or f"P{index:03d}",
            camera_center=(float(camera_center[0]), float(camera_center[1])),
        )
        self.points.append(route_point)
        self.updated_at_utc = _utc_timestamp()
        return route_point

    def add_linear_points(
        self,
        start: Point2D,
        step: Point2D,
        count: int,
        *,
        clear_existing: bool = False,
    ) -> list[RoutePoint]:
        count_value = int(count)
        if count_value <= 0:
            raise RouteModelError("Linear route point count must be positive.")
        added: list[RoutePoint] = []
        start_x, start_y = float(start[0]), float(start[1])
        step_x, step_y = float(step[0]), float(step[1])
        if count_value > 1 and abs(step_x) <= 1e-12 and abs(step_y) <= 1e-12:
            raise RouteModelError("Linear route step must be non-zero.")
        if clear_existing:
            self.points.clear()
        for index in range(count_value):
            point = self.add_point(
                (
                    start_x + step_x * float(index),
                    start_y + step_y * float(index),
                )
            )
            added.append(point)
        return added

    def add_grid_points(
        self,
        origin: Point2D,
        step_x: Point2D,
        count_x: int,
        step_y: Point2D,
        count_y: int,
        *,
        serpentine: bool = False,
        clear_existing: bool = False,
    ) -> list[RoutePoint]:
        x_count = int(count_x)
        y_count = int(count_y)
        if x_count <= 0 or y_count <= 0:
            raise RouteModelError("Grid route point counts must be positive.")
        origin_x, origin_y = float(origin[0]), float(origin[1])
        step_x_dx, step_x_dy = float(step_x[0]), float(step_x[1])
        step_y_dx, step_y_dy = float(step_y[0]), float(step_y[1])
        if x_count > 1 and abs(step_x_dx) <= 1e-12 and abs(step_x_dy) <= 1e-12:
            raise RouteModelError("Grid X step must be non-zero.")
        if y_count > 1 and abs(step_y_dx) <= 1e-12 and abs(step_y_dy) <= 1e-12:
            raise RouteModelError("Grid Y step must be non-zero.")
        if clear_existing:
            self.points.clear()
        added: list[RoutePoint] = []
        for y_index in range(y_count):
            x_indices: range
            if serpentine and y_index % 2 == 1:
                x_indices = range(x_count - 1, -1, -1)
            else:
                x_indices = range(x_count)
            for x_index in x_indices:
                point = self.add_point(
                    (
                        origin_x
                        + step_x_dx * float(x_index)
                        + step_y_dx * float(y_index),
                        origin_y
                        + step_x_dy * float(x_index)
                        + step_y_dy * float(y_index),
                    )
                )
                added.append(point)
        return added

    def remove_point_at(self, index: int) -> RoutePoint | None:
        if not 0 <= index < len(self.points):
            return None
        point = self.points.pop(index)
        self.updated_at_utc = _utc_timestamp()
        return point

    def clear_points(self) -> None:
        if not self.points:
            return
        self.points.clear()
        self.updated_at_utc = _utc_timestamp()

    def set_needle_offsets(
        self,
        needle_1_dx: float,
        needle_1_dy: float,
        needle_2_dx: float,
        needle_2_dy: float,
        *,
        source: str = "manual",
    ) -> None:
        labels = ["Needle 1", "Needle 2"]
        ids = ["needle_1", "needle_2"]
        values = [
            (float(needle_1_dx), float(needle_1_dy)),
            (float(needle_2_dx), float(needle_2_dy)),
        ]
        self.needle_offsets = [
            NeedleOffset(
                id=ids[index],
                label=labels[index],
                dx=values[index][0],
                dy=values[index][1],
                source=source,
            )
            for index in range(2)
        ]
        self.updated_at_utc = _utc_timestamp()

    def transform_design_coordinates(
        self,
        document: DesignDocument,
        point_transform: Callable[[Point2D], Point2D],
        vector_transform: Callable[[Point2D], Point2D],
    ) -> None:
        """Move route points and needle offsets into a transformed design space."""

        self.design = RouteDesignBinding.from_document(document)
        self.points = [
            replace(point, camera_center=point_transform(point.camera_center))
            for point in self.points
        ]
        transformed_offsets: list[NeedleOffset] = []
        for offset in self.needle_offsets:
            dx, dy = vector_transform((offset.dx, offset.dy))
            transformed_offsets.append(replace(offset, dx=dx, dy=dy))
        self.needle_offsets = transformed_offsets
        self.updated_at_utc = _utc_timestamp()

    def needle_hits_for_point(
        self, route_point: RoutePoint
    ) -> list[tuple[NeedleOffset, Point2D]]:
        return [
            (offset, offset.apply_to(route_point.camera_center))
            for offset in self.needle_offsets[:2]
        ]

    def to_measurement_targets(self) -> list[MeasurementTarget]:
        return [
            point.to_measurement_target()
            for point in self.points
            if point.enabled
        ]


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise RouteModelError(f"Unable to fingerprint design '{path}': {exc}") from exc
    return digest.hexdigest()


def _bounds_match(
    expected: tuple[float, float, float, float],
    actual: tuple[float, float, float, float],
) -> bool:
    return all(abs(float(left) - float(right)) <= 1e-9 for left, right in zip(expected, actual))


def _coerce_point(value: Any, field_name: str) -> Point2D:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise RouteModelError(f"Route point field '{field_name}' must be a 2D point.")
    try:
        return (float(value[0]), float(value[1]))
    except (TypeError, ValueError) as exc:
        raise RouteModelError(f"Route point field '{field_name}' is invalid.") from exc


def structure_number_from_labels(*values: object, default: int) -> int:
    """Return trailing numeric structure id from labels, or the route index."""

    for value in values:
        match = re.search(r"(\d+)\s*$", str(value).strip())
        if match is not None:
            try:
                return int(match.group(1))
            except ValueError:
                pass
    return int(default)


def _next_point_id(points: list[RoutePoint]) -> str:
    existing = {point.id for point in points}
    index = len(points) + 1
    while True:
        candidate = f"p{index:03d}"
        if candidate not in existing:
            return candidate
        index += 1


def _utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = [
    "MeasurementRoute",
    "NeedleOffset",
    "ROUTE_FORMAT",
    "ROUTE_VERSION",
    "RouteDesignBinding",
    "RouteModelError",
    "RoutePoint",
    "structure_number_from_labels",
]
