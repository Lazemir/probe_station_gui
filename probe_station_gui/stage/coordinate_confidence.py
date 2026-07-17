"""Pure coordinate-confidence transitions for backlash-aware axes."""

from __future__ import annotations

from dataclasses import dataclass

from probe_station_gui.settings.precision_approach import PrecisionApproachProfile


_DISTANCE_COMPARISON_TOLERANCE = 1e-12


@dataclass(frozen=True)
class AxisCoordinateConfidence:
    """Confirmed coordinate and currently loaded mechanical direction."""

    exact: bool = False
    loaded_direction: int | None = None
    confirmed_machine_coordinate: float | None = None
    takeup_travel: float = 0.0

    def after_precision_final(
        self,
        *,
        coordinate: float,
        final_direction: int,
    ) -> "AxisCoordinateConfidence":
        return AxisCoordinateConfidence(
            exact=True,
            loaded_direction=final_direction,
            confirmed_machine_coordinate=coordinate,
            takeup_travel=0.0,
        )

    def after_confirmed_motion(
        self,
        *,
        coordinate: float,
        profile: PrecisionApproachProfile,
    ) -> "AxisCoordinateConfidence":
        previous = self.confirmed_machine_coordinate
        if previous is None:
            return AxisCoordinateConfidence(confirmed_machine_coordinate=coordinate)

        delta = coordinate - previous
        if delta == 0.0:
            return AxisCoordinateConfidence(
                exact=self.exact,
                loaded_direction=self.loaded_direction,
                confirmed_machine_coordinate=coordinate,
                takeup_travel=self.takeup_travel,
            )

        direction = 1 if delta > 0.0 else -1
        travel = abs(delta)
        if self.exact and direction == self.loaded_direction:
            return AxisCoordinateConfidence(
                exact=True,
                loaded_direction=direction,
                confirmed_machine_coordinate=coordinate,
                takeup_travel=0.0,
            )

        if direction == self.loaded_direction:
            takeup = self.takeup_travel + travel
        else:
            takeup = travel
        exact = (
            direction == profile.final_direction
            and takeup + _DISTANCE_COMPARISON_TOLERANCE >= profile.backlash
        )
        return AxisCoordinateConfidence(
            exact=exact,
            loaded_direction=direction,
            confirmed_machine_coordinate=coordinate,
            takeup_travel=0.0 if exact else takeup,
        )

    def invalidate(
        self,
        *,
        reason: str,
        coordinate: float | None = None,
    ) -> "AxisCoordinateConfidence":
        del reason
        return AxisCoordinateConfidence(
            confirmed_machine_coordinate=(
                self.confirmed_machine_coordinate if coordinate is None else coordinate
            )
        )


__all__ = ["AxisCoordinateConfidence"]
