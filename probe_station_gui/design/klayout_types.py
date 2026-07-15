"""Immutable values shared by the KLayout render and snap pipeline."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .model import LayerKey, SnapResult


Point2D = tuple[float, float]
Box2D = tuple[float, float, float, float]


@dataclass(frozen=True)
class KLayoutConfig:
    """Document state that determines KLayout render and snap results."""

    path: Path
    top_cell_name: str
    visible_layers: frozenset[LayerKey]
    source_bounds: Box2D
    display_bounds: Box2D
    rotation_quarter_turns: int
    generation: int

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "rotation_quarter_turns",
            int(self.rotation_quarter_turns) % 4,
        )


@dataclass(frozen=True)
class RenderRequest:
    """One generation-tagged raster render request."""

    request_id: int
    config: KLayoutConfig
    world_box: Box2D
    pixel_width: int
    pixel_height: int
    viewport_generation: int
    density: float
    purpose: str = "viewport"


@dataclass(frozen=True)
class RenderFrame:
    """Detached rendered image and its exact design-space placement."""

    request_id: int
    config_generation: int
    viewport_generation: int
    world_box: Box2D
    pixel_width: int
    pixel_height: int
    density: float
    image: object
    elapsed_ms: float
    purpose: str = "viewport"


@dataclass(frozen=True)
class SnapRequest:
    """One local geometry query around a raw design-space point."""

    request_id: int
    config: KLayoutConfig
    point: Point2D
    radius: float
    purpose: str = "hover"


@dataclass(frozen=True)
class SnapResponse:
    """A correlated local snap result and query diagnostics."""

    request_id: int
    config_generation: int
    raw_point: Point2D
    result: SnapResult
    elapsed_ms: float
    shapes_inspected: int
    purpose: str = "hover"


@dataclass(frozen=True)
class PendingClick:
    """A click action waiting for its exact snap response."""

    request_id: int
    config_generation: int
    action: str
    raw_point: Point2D
    payload: tuple[object, ...] = ()


def forward_rotate_point(point: Point2D, config: KLayoutConfig) -> Point2D:
    """Rotate a source-space point into displayed document coordinates."""

    return _rotate_point(point, config.source_bounds, config.rotation_quarter_turns)


def inverse_rotate_point(point: Point2D, config: KLayoutConfig) -> Point2D:
    """Rotate a displayed point back into unrotated source coordinates."""

    return _rotate_point(point, config.source_bounds, -config.rotation_quarter_turns)


def inverse_rotate_box(box: Box2D, config: KLayoutConfig) -> Box2D:
    """Return the source-space axis-aligned box covering a displayed box."""

    left, bottom, right, top = box
    corners = tuple(
        inverse_rotate_point(point, config)
        for point in (
            (float(left), float(bottom)),
            (float(left), float(top)),
            (float(right), float(bottom)),
            (float(right), float(top)),
        )
    )
    return (
        min(point[0] for point in corners),
        min(point[1] for point in corners),
        max(point[0] for point in corners),
        max(point[1] for point in corners),
    )


def _rotate_point(point: Point2D, bounds: Box2D, quarter_turns: int) -> Point2D:
    left, bottom, right, top = bounds
    center_x = (float(left) + float(right)) * 0.5
    center_y = (float(bottom) + float(top)) * 0.5
    delta_x = float(point[0]) - center_x
    delta_y = float(point[1]) - center_y
    turns = int(quarter_turns) % 4
    if turns == 1:
        return (center_x - delta_y, center_y + delta_x)
    if turns == 2:
        return (center_x - delta_x, center_y - delta_y)
    if turns == 3:
        return (center_x + delta_y, center_y - delta_x)
    return (float(point[0]), float(point[1]))


__all__ = [
    "Box2D",
    "KLayoutConfig",
    "PendingClick",
    "Point2D",
    "RenderFrame",
    "RenderRequest",
    "SnapRequest",
    "SnapResponse",
    "forward_rotate_point",
    "inverse_rotate_box",
    "inverse_rotate_point",
]
