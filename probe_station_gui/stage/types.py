"""Small value types used by stage control workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Optional


@dataclass(frozen=True)
class StageTaskToken:
    """Identity of one stage operation that may emit calibration callbacks."""

    generation: int
    source: str


@dataclass
class _ObjectiveCalibrationCandidate:
    token: StageTaskToken
    objective_name: str
    pixels_to_mm: object
    decision: threading.Event = field(default_factory=threading.Event)
    accepted: bool = False
    published: bool = False


@dataclass
class MoveVector:
    """Represents a movement across the available motion axes."""

    x: float = 0.0
    y: float = 0.0
    z: float = 0.0
    a: float = 0.0
    b: float = 0.0
    c: float = 0.0

    def is_zero(self, tol: float = 1e-6) -> bool:
        """Return True when all components are effectively zero."""

        return all(
            abs(component) < tol
            for component in (self.x, self.y, self.z, self.a, self.b, self.c)
        )

    def items(self) -> tuple[tuple[str, float], ...]:
        """Expose the vector components in G-code axis order."""

        return (
            ("X", self.x),
            ("Y", self.y),
            ("Z", self.z),
            ("A", self.a),
            ("B", self.b),
            ("C", self.c),
        )


@dataclass
class _Status:
    state: str
    position: Optional[tuple[float, ...]] = None
    synchronized_machine_position: Optional[tuple[float, ...]] = None
    display_position: Optional[tuple[float, ...]] = None
    work_position: Optional[tuple[float, ...]] = None
    work_offset: Optional[tuple[float, ...]] = None
    coordinate_system: Optional[str] = None
    homed_axes: Optional[set[str]] = None
    pins: Optional[set[str]] = None


@dataclass(order=True)
class _QueuedSerialWrite:
    priority: int
    sequence: int
    kind: str = field(compare=False)
    payload: bytes = field(compare=False)
    description: str = field(compare=False)
    generation: int = field(compare=False, default=0)
    clear_epoch: int = field(compare=False, default=0)


@dataclass
class _FocusSweepResult:
    best_z: float
    best_score: float
    sample_count: int
    edge_peak: bool


@dataclass(frozen=True)
class AutofocusResult:
    """Structured autofocus result suitable for logs and height maps."""

    objective_name: str
    mode: str
    start_z_mm: float
    best_z_mm: float
    best_score: float
    sample_count: int
    edge_peak: bool
    range_mm: float
    fine_step_mm: float
    lower_z_mm: float
    upper_z_mm: float

    @property
    def delta_um(self) -> float:
        return (float(self.best_z_mm) - float(self.start_z_mm)) * 1000.0

    def to_dict(self) -> dict[str, object]:
        return {
            "objective_name": self.objective_name,
            "mode": self.mode,
            "focus_start_z_mm": float(self.start_z_mm),
            "focus_best_z_mm": float(self.best_z_mm),
            "focus_delta_um": float(self.delta_um),
            "focus_score": float(self.best_score),
            "focus_sample_count": int(self.sample_count),
            "focus_edge_peak": bool(self.edge_peak),
            "autofocus_range_mm": float(self.range_mm),
            "autofocus_fine_step_mm": float(self.fine_step_mm),
            "autofocus_lower_z_mm": float(self.lower_z_mm),
            "autofocus_upper_z_mm": float(self.upper_z_mm),
        }

    def summary(self) -> str:
        message = (
            f"Autofocus {self.objective_name} {self.mode} complete. "
            f"Best score {self.best_score:.2f} at Z={self.best_z_mm:.4f} mm "
            f"(dZ={self.delta_um:+.1f} um) from {self.sample_count} frames."
        )
        if self.edge_peak:
            message += " Peak was near the search edge."
        return message

    def __str__(self) -> str:
        return self.summary()


@dataclass(frozen=True)
class _AutofocusContext:
    objective_name: str
    start_z: float
    min_z: float
    max_z: float
    lower_z: float
    upper_z: float
    local_range_mm: float
    fine_step_mm: float
