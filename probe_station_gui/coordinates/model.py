from __future__ import annotations

from dataclasses import dataclass
import math
from types import MappingProxyType
from typing import Mapping


STAGE_AXES = ("X", "Y", "Z", "A", "B", "C")
VISIBLE_STAGE_AXES = ("X", "Y", "Z", "A", "B")


def normalize_axis_values(values: Mapping[str, float]) -> Mapping[str, float]:
    normalized: dict[str, float] = {}
    for raw_axis, raw_value in values.items():
        axis = str(raw_axis).strip().upper()
        if axis not in STAGE_AXES:
            raise ValueError(f"Unsupported stage axis {raw_axis!r}.")
        value = float(raw_value)
        if not math.isfinite(value):
            raise ValueError(f"{axis} coordinate must be finite.")
        normalized[axis] = value
    return MappingProxyType(normalized)


@dataclass(frozen=True)
class PhysicalMachinePose:
    values: Mapping[str, float]

    def __post_init__(self) -> None:
        object.__setattr__(self, "values", normalize_axis_values(self.values))

    @classmethod
    def from_mapping(cls, values: Mapping[str, float]) -> PhysicalMachinePose:
        return cls(values)

    def require(self, axis: str) -> float:
        normalized = str(axis).strip().upper()
        try:
            return self.values[normalized]
        except KeyError as exc:
            raise ValueError(f"Physical Machine {normalized} is unavailable.") from exc

    def to_dict(self) -> dict[str, float]:
        return dict(self.values)
