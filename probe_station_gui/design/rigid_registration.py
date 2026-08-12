"""Rigid physical-coordinate fitting for design registration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Iterable

import numpy as np

from probe_station_gui.design.model import DesignModelError, Point2D


@dataclass(frozen=True)
class ResidualMetrics:
    """Residual error statistics in millimetres."""

    count: int = 0
    rms_mm: float = 0.0
    max_mm: float = 0.0


@dataclass(frozen=True)
class ResidualSummary:
    """Residual error statistics for registration check marks."""

    count: int = 0
    rms: float = 0.0
    max_error: float = 0.0


@dataclass(frozen=True)
class DesignRegistration:
    """Rigid physical-mm transform between design and stage coordinates."""

    source_design_marks: tuple[Point2D, ...]
    source_stage_marks: tuple[Point2D, ...]
    check_design_marks: tuple[Point2D, ...] = ()
    check_stage_marks: tuple[Point2D, ...] = ()
    matrix: np.ndarray = field(default_factory=lambda: np.eye(2, dtype=float))
    offset: np.ndarray = field(default_factory=lambda: np.zeros(2, dtype=float))
    design_unit_mm: float = 1.0
    distance_scale_ratio: float = 1.0
    source_residuals: tuple[float, ...] = ()
    source_residual_summary: ResidualSummary = field(default_factory=ResidualSummary)
    residuals: tuple[float, ...] = ()
    residual_summary: ResidualSummary = field(default_factory=ResidualSummary)
    valid: bool = False
    stale_reason: str = ""

    @classmethod
    def empty(cls) -> "DesignRegistration":
        """Return an invalid placeholder registration."""

        return cls(
            source_design_marks=(),
            source_stage_marks=(),
            valid=False,
            stale_reason="Registration not built.",
        )

    @classmethod
    def from_marks(
        cls,
        source_design_marks: Iterable[Point2D],
        source_stage_marks: Iterable[Point2D],
        *,
        check_design_marks: Iterable[Point2D] = (),
        check_stage_marks: Iterable[Point2D] = (),
        design_unit_mm: float = 1.0,
    ) -> "DesignRegistration":
        """Build a rigid physical-mm transform from design and stage mark pairs."""

        design_marks = cls._finite_points(source_design_marks, label="Design source")
        stage_marks = cls._finite_points(source_stage_marks, label="Stage source")
        check_design = cls._finite_points(check_design_marks, label="Design check")
        check_stage = cls._finite_points(check_stage_marks, label="Stage check")
        try:
            fit = fit_rigid_registration(
                design_points=design_marks,
                machine_points=stage_marks,
                design_unit_mm=design_unit_mm,
                check_design_points=check_design,
                check_machine_points=check_stage,
            )
        except ValueError as exc:
            raise DesignModelError(str(exc)) from exc

        source = np.asarray(design_marks, dtype=float) * float(design_unit_mm)
        stage = np.asarray(stage_marks, dtype=float)
        predicted_source = (fit.rotation @ source.T).T + fit.offset_machine_mm
        source_errors = np.linalg.norm(stage - predicted_source, axis=1)
        source_residuals = tuple(float(value) for value in source_errors)
        source_summary = cls._residual_summary(source_residuals)

        residuals: list[float] = []
        for design_point, stage_point in zip(check_design, check_stage):
            design_mm = np.asarray(design_point, dtype=float) * float(design_unit_mm)
            predicted = fit.rotation @ design_mm + fit.offset_machine_mm
            actual = np.asarray(stage_point, dtype=float)
            residuals.append(float(np.linalg.norm(actual - predicted)))
        summary = cls._residual_summary(residuals)

        return cls(
            source_design_marks=design_marks,
            source_stage_marks=stage_marks,
            check_design_marks=check_design,
            check_stage_marks=check_stage,
            matrix=fit.rotation,
            offset=fit.offset_machine_mm,
            design_unit_mm=float(design_unit_mm),
            distance_scale_ratio=fit.distance_scale_ratio,
            source_residuals=source_residuals,
            source_residual_summary=source_summary,
            residuals=tuple(residuals),
            residual_summary=summary,
            valid=True,
        )

    @property
    def scale(self) -> float:
        """Return the measured spacing ratio retained for compatibility."""

        return self.distance_scale_ratio

    @property
    def rotation_deg(self) -> float:
        """Return the fitted proper-rotation angle in degrees."""

        return float(
            math.degrees(math.atan2(float(self.matrix[1, 0]), float(self.matrix[0, 0])))
        )

    @staticmethod
    def _finite_points(
        points: Iterable[Point2D],
        *,
        label: str,
    ) -> tuple[Point2D, ...]:
        normalized: list[Point2D] = []
        for point in points:
            try:
                if len(point) != 2:
                    raise ValueError
                x_value = float(point[0])
                y_value = float(point[1])
            except (TypeError, ValueError, IndexError) as exc:
                raise DesignModelError(
                    f"{label} marks must be finite 2D points."
                ) from exc
            if not math.isfinite(x_value) or not math.isfinite(y_value):
                raise DesignModelError(f"{label} marks must be finite 2D points.")
            normalized.append((x_value, y_value))
        return tuple(normalized)

    @staticmethod
    def _residual_summary(residuals: Iterable[float]) -> ResidualSummary:
        values = tuple(float(value) for value in residuals)
        if not values:
            return ResidualSummary()
        residual_array = np.asarray(values, dtype=float)
        return ResidualSummary(
            count=len(values),
            rms=float(np.sqrt(np.mean(np.square(residual_array)))),
            max_error=float(np.max(residual_array)),
        )

    def design_to_stage(self, point: Point2D) -> Point2D:
        """Transform a design-space point into stage-space coordinates."""

        if not self.valid:
            raise DesignModelError(self.stale_reason or "Registration is not valid.")
        vec = np.asarray(point, dtype=float) * self.design_unit_mm
        result = self.matrix @ vec + self.offset
        return (float(result[0]), float(result[1]))

    def stage_to_design(self, point: Point2D) -> Point2D:
        """Transform a stage-space point back into design coordinates."""

        if not self.valid:
            raise DesignModelError(self.stale_reason or "Registration is not valid.")
        inverse = np.linalg.inv(self.matrix)
        vec = np.asarray(point, dtype=float) - self.offset
        result = (inverse @ vec) / self.design_unit_mm
        return (float(result[0]), float(result[1]))

    def mark_stale(self, reason: str) -> "DesignRegistration":
        """Return a stale copy retaining the previous transform for diagnostics."""

        return replace(self, valid=False, stale_reason=reason)


@dataclass(frozen=True)
class RigidRegistrationFit:
    """A proper rigid transform from design millimetres to machine millimetres."""

    rotation: np.ndarray
    offset_machine_mm: np.ndarray
    rotation_deg: float
    distance_scale_ratio: float
    source_residual: ResidualMetrics
    check_residual: ResidualMetrics

    def __post_init__(self) -> None:
        """Defensively copy immutable transform arrays for this frozen result."""

        object.__setattr__(
            self,
            "rotation",
            _immutable_array(self.rotation, shape=(2, 2), label="Rotation"),
        )
        object.__setattr__(
            self,
            "offset_machine_mm",
            _immutable_array(
                self.offset_machine_mm,
                shape=(2,),
                label="Machine offset",
            ),
        )

    def design_mm_to_machine_xy(self, point: Point2D) -> Point2D:
        """Map a physical design coordinate into machine millimetres."""

        result = self.rotation @ np.asarray(point, dtype=float) + self.offset_machine_mm
        return (float(result[0]), float(result[1]))

    def machine_xy_to_design_mm(self, point: Point2D) -> Point2D:
        """Map a physical machine coordinate back into design millimetres."""

        result = self.rotation.T @ (
            np.asarray(point, dtype=float) - self.offset_machine_mm
        )
        return (float(result[0]), float(result[1]))


def fit_rigid_registration(
    *,
    design_points: Iterable[Point2D],
    machine_points: Iterable[Point2D],
    design_unit_mm: float,
    check_design_points: Iterable[Point2D] = (),
    check_machine_points: Iterable[Point2D] = (),
) -> RigidRegistrationFit:
    """Fit a proper rotation and translation without applying measured scale."""

    design = _point_array(design_points, "Design source")
    machine = _point_array(machine_points, "Machine source")
    check_design = _point_array(check_design_points, "Design check")
    check_machine = _point_array(check_machine_points, "Machine check")
    if len(design) != len(machine):
        raise ValueError("Design and machine source mark counts must match.")
    if len(design) < 2:
        raise ValueError("At least two source mark pairs are required.")
    if len(check_design) != len(check_machine):
        raise ValueError("Design and machine check mark counts must match.")
    if not math.isfinite(design_unit_mm) or design_unit_mm <= 0.0:
        raise ValueError(
            "Design unit conversion must be a positive finite millimetre value."
        )

    design_mm = design * float(design_unit_mm)
    design_center = np.mean(design_mm, axis=0)
    machine_center = np.mean(machine, axis=0)
    design_centered = design_mm - design_center
    machine_centered = machine - machine_center
    design_energy = float(np.sum(np.square(design_centered)))
    if design_energy <= 1e-18:
        raise ValueError("Design source geometry is degenerate.")
    machine_energy = float(np.sum(np.square(machine_centered)))
    if machine_energy <= 1e-18:
        raise ValueError("Machine source geometry is degenerate.")

    covariance = machine_centered.T @ design_centered
    u, singular_values, vt = np.linalg.svd(covariance)
    determinant_sign = 1.0 if float(np.linalg.det(u @ vt)) >= 0.0 else -1.0
    correction = np.diag([1.0, determinant_sign])
    rotation = u @ correction @ vt
    offset = machine_center - rotation @ design_center
    scale_ratio = float(
        np.sum(singular_values * np.asarray([1.0, determinant_sign])) / design_energy
    )
    if not math.isfinite(scale_ratio):
        raise ValueError("Measured source spacing ratio is not finite.")

    predicted_source = (rotation @ design_mm.T).T + offset
    source_residual = _residual_metrics(machine - predicted_source)
    predicted_check = (rotation @ (check_design * float(design_unit_mm)).T).T + offset
    check_residual = _residual_metrics(check_machine - predicted_check)

    return RigidRegistrationFit(
        rotation=rotation,
        offset_machine_mm=offset,
        rotation_deg=float(math.degrees(math.atan2(rotation[1, 0], rotation[0, 0]))),
        distance_scale_ratio=scale_ratio,
        source_residual=source_residual,
        check_residual=check_residual,
    )


def _point_array(points: Iterable[Point2D], label: str) -> np.ndarray:
    values = np.asarray(tuple(points), dtype=float)
    if values.size == 0:
        return np.empty((0, 2), dtype=float)
    if values.ndim != 2 or values.shape[1] != 2 or not np.all(np.isfinite(values)):
        raise ValueError(f"{label} marks must be finite 2D points.")
    return values


def _immutable_array(
    value: np.ndarray,
    *,
    shape: tuple[int, ...],
    label: str,
) -> np.ndarray:
    array = np.array(value, dtype=float, copy=True)
    if array.shape != shape or not np.all(np.isfinite(array)):
        raise ValueError(f"{label} must be a finite array with shape {shape}.")
    array.setflags(write=False)
    return array


def _residual_metrics(vectors: np.ndarray) -> ResidualMetrics:
    if len(vectors) == 0:
        return ResidualMetrics()
    errors = np.linalg.norm(vectors, axis=1)
    return ResidualMetrics(
        count=len(errors),
        rms_mm=float(np.sqrt(np.mean(np.square(errors)))),
        max_mm=float(np.max(errors)),
    )


__all__ = [
    "DesignRegistration",
    "ResidualMetrics",
    "ResidualSummary",
    "RigidRegistrationFit",
    "fit_rigid_registration",
]
