"""Named interface types for microscope interaction."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from PySide6.QtCore import QPointF, QRect, QSize


Point = tuple[float, float]
ClickCoordinates = tuple[float, float, float, float]


class CoordinatePresenter(Protocol):
    def __call__(
        self,
        *,
        center_xy: Point | None,
        cursor_xy: Point | None,
    ) -> None: ...


@dataclass(frozen=True)
class ClickMoveBindings:
    request_move: Callable[[float, float], bool]
    stage_connected: Callable[[], bool]
    motion_blocked: Callable[[], bool]
    mark_motion_axes: Callable[[set[str]], None]
    show_status: Callable[[str, int], None]
    repaint: Callable[[], None]
    preview_hover: Callable[[float, float], tuple[Point, Point] | None]
    present_coordinates: CoordinatePresenter
    manual_alignment_active: Callable[[], bool]
    capture_manual_alignment: Callable[[float, float], None]
    pending_state_changed: Callable[[bool], None]


@dataclass(frozen=True)
class ClickMoveConfig:
    pending_timeout_s: Callable[[], float]
    pending_retry_ms: int = 150
    default_pending_timeout_s: float = 8.0
    min_pending_timeout_s: float = 0.5
    max_pending_timeout_s: float = 60.0
    target_blink_ms: int = 250
    target_motion_update_ms: int = 50
    target_animation_padding_s: float = 0.03
    target_animation_min_s: float = 0.05
    min_feedrate_mm_min: float = 1.0


@dataclass(frozen=True)
class PendingClickMove:
    dx: float
    dy: float
    rel_x: float
    rel_y: float
    deadline_s: float


@dataclass(frozen=True)
class ImageGeometry:
    display_rect: QRect
    image_size: QSize


@dataclass(frozen=True)
class PointerDispatch:
    click: ClickCoordinates | None = None
    hover: ClickCoordinates | None = None
    hover_left: bool = False
    accepted: bool = False


__all__ = [
    "ClickCoordinates",
    "ClickMoveBindings",
    "ClickMoveConfig",
    "CoordinatePresenter",
    "ImageGeometry",
    "PendingClickMove",
    "Point",
    "PointerDispatch",
]
