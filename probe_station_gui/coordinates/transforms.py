from __future__ import annotations

from dataclasses import dataclass
import math


def _finite_float(value: float, name: str) -> float:
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError(f"{name} must be finite.")
    return converted


def _finite_xy(point: tuple[float, float], name: str) -> tuple[float, float]:
    try:
        x, y = point
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain exactly two coordinates.") from exc
    return (_finite_float(x, f"{name} X"), _finite_float(y, f"{name} Y"))


def rotate_xy(point: tuple[float, float], angle_deg: float) -> tuple[float, float]:
    angle_rad = math.radians(float(angle_deg))
    cosine = math.cos(angle_rad)
    sine = math.sin(angle_rad)
    return (
        cosine * float(point[0]) - sine * float(point[1]),
        sine * float(point[0]) + cosine * float(point[1]),
    )


@dataclass(frozen=True)
class BFrameTransform:
    origin_xy_at_reference_b: tuple[float, float]
    reference_b_deg: float
    xy_angle_at_reference_b_deg: float
    b_zero_machine_deg: float
    z_zero_machine_mm: float | None = None
    a_zero_machine_mm: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "origin_xy_at_reference_b",
            _finite_xy(self.origin_xy_at_reference_b, "Frame XY origin"),
        )
        object.__setattr__(
            self,
            "reference_b_deg",
            _finite_float(self.reference_b_deg, "Reference B angle"),
        )
        object.__setattr__(
            self,
            "xy_angle_at_reference_b_deg",
            _finite_float(self.xy_angle_at_reference_b_deg, "Frame XY angle"),
        )
        object.__setattr__(
            self,
            "b_zero_machine_deg",
            _finite_float(self.b_zero_machine_deg, "Frame B origin"),
        )
        if self.z_zero_machine_mm is not None:
            object.__setattr__(
                self,
                "z_zero_machine_mm",
                _finite_float(self.z_zero_machine_mm, "Frame Z origin"),
            )
        if self.a_zero_machine_mm is not None:
            object.__setattr__(
                self,
                "a_zero_machine_mm",
                _finite_float(self.a_zero_machine_mm, "Frame A origin"),
            )

    def machine_origin_xy(
        self,
        machine_b_deg: float,
        pivot_machine_xy: tuple[float, float],
    ) -> tuple[float, float]:
        delta = float(machine_b_deg) - self.reference_b_deg
        relative = (
            self.origin_xy_at_reference_b[0] - pivot_machine_xy[0],
            self.origin_xy_at_reference_b[1] - pivot_machine_xy[1],
        )
        rotated = rotate_xy(relative, delta)
        return (pivot_machine_xy[0] + rotated[0], pivot_machine_xy[1] + rotated[1])

    def frame_xy_to_machine(
        self,
        frame_xy: tuple[float, float],
        *,
        machine_b_deg: float,
        pivot_machine_xy: tuple[float, float],
    ) -> tuple[float, float]:
        angle = self.xy_angle_at_reference_b_deg + machine_b_deg - self.reference_b_deg
        rotated = rotate_xy(frame_xy, angle)
        origin = self.machine_origin_xy(machine_b_deg, pivot_machine_xy)
        return (origin[0] + rotated[0], origin[1] + rotated[1])

    def machine_xy_to_frame(
        self,
        machine_xy: tuple[float, float],
        *,
        machine_b_deg: float,
        pivot_machine_xy: tuple[float, float],
    ) -> tuple[float, float]:
        origin = self.machine_origin_xy(machine_b_deg, pivot_machine_xy)
        angle = self.xy_angle_at_reference_b_deg + machine_b_deg - self.reference_b_deg
        return rotate_xy(
            (machine_xy[0] - origin[0], machine_xy[1] - origin[1]),
            -angle,
        )

    def machine_b_to_frame(self, machine_b_deg: float) -> float:
        return float(machine_b_deg) - self.b_zero_machine_deg

    def machine_z_to_frame(self, machine_z_mm: float) -> float:
        if self.z_zero_machine_mm is None:
            raise ValueError("Frame Z origin is unavailable.")
        return float(machine_z_mm) - self.z_zero_machine_mm

    def machine_a_to_frame(self, machine_a_mm: float) -> float:
        if self.a_zero_machine_mm is None:
            raise ValueError("Frame A origin is unavailable.")
        return float(machine_a_mm) - self.a_zero_machine_mm
