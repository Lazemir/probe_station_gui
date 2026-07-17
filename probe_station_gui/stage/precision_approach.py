"""Pure backlash-aware planning in calibrated display coordinates."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from probe_station_gui.settings.precision_approach import PrecisionApproachProfile
from probe_station_gui.stage.coordinate_confidence import AxisCoordinateConfidence


_DISTANCE_COMPARISON_TOLERANCE = 1e-12
_DISABLED_PROFILE = PrecisionApproachProfile()
_APPROXIMATE_COORDINATE = AxisCoordinateConfidence()


@dataclass(frozen=True)
class PrecisionMovePlan:
    """One optional preparation segment followed by the complete final target."""

    preparation_target: Mapping[str, float] | None
    final_target: Mapping[str, float]
    prepared_axes: frozenset[str]


class PrecisionApproachPlanner:
    """Plan a final approach without performing coordinate conversion or I/O."""

    def plan(
        self,
        *,
        current: Mapping[str, float],
        target: Mapping[str, float],
        profiles: Mapping[str, PrecisionApproachProfile],
        confidence: Mapping[str, AxisCoordinateConfidence],
        validate_target: Callable[[Mapping[str, float]], None],
    ) -> PrecisionMovePlan:
        final_target = {axis: float(value) for axis, value in target.items()}
        for axis in final_target:
            if axis not in current:
                raise ValueError(f"current coordinate for {axis} is unavailable")

        prepared_axes = frozenset(
            axis
            for axis, target_value in final_target.items()
            if self._needs_preparation(
                current=float(current[axis]),
                target=target_value,
                profile=profiles.get(axis, _DISABLED_PROFILE),
                confidence=confidence.get(axis, _APPROXIMATE_COORDINATE),
            )
        )
        preparation_target: dict[str, float] | None = None
        if prepared_axes:
            preparation_target = {
                axis: (
                    final_target[axis]
                    - profiles.get(axis, _DISABLED_PROFILE).final_direction
                    * profiles.get(axis, _DISABLED_PROFILE).backlash
                    if axis in prepared_axes
                    else float(current[axis])
                )
                for axis in final_target
            }

        validate_target(final_target)
        if preparation_target is not None:
            validate_target(preparation_target)
        return PrecisionMovePlan(
            preparation_target=preparation_target,
            final_target=final_target,
            prepared_axes=prepared_axes,
        )

    @staticmethod
    def _needs_preparation(
        *,
        current: float,
        target: float,
        profile: PrecisionApproachProfile,
        confidence: AxisCoordinateConfidence,
    ) -> bool:
        if not profile.enabled or profile.backlash <= 0.0:
            return False
        directed_travel = (target - current) * profile.final_direction
        if (
            confidence.exact
            and confidence.loaded_direction == profile.final_direction
            and directed_travel >= -_DISTANCE_COMPARISON_TOLERANCE
        ):
            return False
        available_takeup = directed_travel
        if confidence.loaded_direction == profile.final_direction:
            available_takeup += confidence.takeup_travel
        return (
            available_takeup + _DISTANCE_COMPARISON_TOLERANCE
            < profile.backlash
        )


def resolve_relative_targets(
    *,
    current: Mapping[str, float],
    deltas: Mapping[str, float],
) -> dict[str, float]:
    """Resolve a relative display-space request into absolute targets."""

    resolved: dict[str, float] = {}
    for axis, delta in deltas.items():
        if axis not in current:
            raise ValueError(f"current coordinate for {axis} is unavailable")
        resolved[axis] = float(current[axis]) + float(delta)
    return resolved


__all__ = [
    "PrecisionApproachPlanner",
    "PrecisionMovePlan",
    "resolve_relative_targets",
]
