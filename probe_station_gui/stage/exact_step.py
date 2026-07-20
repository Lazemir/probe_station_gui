"""Accumulate exact Step targets before issuing one stage move."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Sequence


DEFAULT_AXIS_NAMES = ("X", "Y", "Z", "A", "B", "C")


@dataclass
class ExactStepAccumulator:
    axis_names: Sequence[str] = DEFAULT_AXIS_NAMES
    targets: dict[str, float] = field(default_factory=dict)

    @property
    def has_targets(self) -> bool:
        return bool(self.targets)

    def add(self, axis_name: str, delta: float, *, baseline: float) -> float:
        return self.add_relative(axis_name, delta, baseline=baseline)

    def add_relative(self, axis_name: str, delta: float, *, baseline: float) -> float:
        axis = self._validated_axis(axis_name)
        base = self.targets.get(axis, self._validated_value(baseline))
        target = self._validated_value(float(base) + float(delta))
        self.targets[axis] = target
        return target

    def set_absolute(self, axis_name: str, target: float) -> float:
        axis = self._validated_axis(axis_name)
        value = self._validated_value(target)
        self.targets[axis] = value
        return value

    def drain(self) -> dict[str, float]:
        snapshot = dict(self.targets)
        self.targets.clear()
        return snapshot

    def clear(self) -> None:
        self.targets.clear()

    def _validated_axis(self, axis_name: str) -> str:
        axis = str(axis_name).strip().upper()
        if axis not in self.axis_names:
            raise ValueError(f"Unsupported stage axis: {axis_name!r}")
        return axis

    @staticmethod
    def _validated_value(value: float) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("Exact Step target must be finite.")
        return result


__all__ = ["ExactStepAccumulator"]
