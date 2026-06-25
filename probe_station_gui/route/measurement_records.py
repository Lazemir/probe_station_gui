"""Route measurement value records."""

from __future__ import annotations

import math
from dataclasses import dataclass

from probe_station_gui.route.contact_quality import (
    RouteContactQuality,
    RouteMeasurementSample,
)

Point2D = tuple[float, float]


@dataclass(frozen=True)
class RouteMeasurementPoint:
    """One route point resolved into stage coordinates before a run starts."""

    index: int
    point_id: str
    label: str
    design_center: Point2D
    stage_xy: Point2D
    needle_1_design: Point2D
    needle_2_design: Point2D
    photo_stage_xy: Point2D | None = None


@dataclass(frozen=True)
class RouteContactSeekResult:
    """Needle depth selected by automatic contact seek for one route point."""

    found: bool
    status: str
    attempts: int
    initial_status: str
    final_status: str
    depth_below_down_mm: float
    axis_a_lowering_mm: float = math.nan
    step_mm: float = math.nan
    max_depth_mm: float = math.nan


@dataclass(frozen=True)
class RouteMeasurementRecord:
    """One completed measurement row written to CSV."""

    timestamp: str
    structure_number: int
    nplc: str
    measurement_type: str
    n_measurements: int
    resistance_ohm: float
    resistance_rms_ohm: float
    relative_rms: float
    status: str
    contact_quality: RouteContactQuality | None = None
    raw_samples: tuple[RouteMeasurementSample, ...] = ()


@dataclass(frozen=True)
class RouteContactPlacementResult:
    """Result of preparing one route contact for external measurements."""

    success: bool
    message: str
    point: RouteMeasurementPoint
    record: RouteMeasurementRecord
    contact_seek: RouteContactSeekResult | None = None


@dataclass(frozen=True)
class RouteExternalContactPreparation:
    """Prepared route contact plus optional pre-contact photo/focus details."""

    placement: RouteContactPlacementResult
    photo_path: str | None = None
    focus: dict[str, object] | None = None


@dataclass(frozen=True)
class RouteContactHeightRecord:
    """One contact-height map row written next to route measurements."""

    timestamp: str
    structure_number: int
    point_index: int
    point_id: str
    label: str
    design_center: Point2D
    stage_xy: Point2D
    measurement_status: str
    resistance_ohm: float
    resistance_rms_ohm: float
    relative_rms: float
    contact_quality: RouteContactQuality | None = None
    contact_found: bool = False
    contact_depth_below_down_mm: float = math.nan
    contact_axis_a_lowering_mm: float = math.nan
    contact_seek: RouteContactSeekResult | None = None


@dataclass(frozen=True)
class RoutePhotoRecord:
    """One completed route microscope image."""

    timestamp: str
    path: str
    structure_number: int
    point_index: int
    point_id: str
    label: str
    design_center: Point2D
    stage_xy: Point2D
    focus: dict[str, object] | None = None


__all__ = [
    "Point2D",
    "RouteContactHeightRecord",
    "RouteContactPlacementResult",
    "RouteContactSeekResult",
    "RouteExternalContactPreparation",
    "RouteMeasurementPoint",
    "RouteMeasurementRecord",
    "RoutePhotoRecord",
]
