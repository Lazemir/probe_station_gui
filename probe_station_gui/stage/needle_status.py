"""Needle and A-axis status tracking for stage control."""

from __future__ import annotations

import logging
from typing import Optional

from probe_station_gui.stage.needle_state import (
    axis_a_ready_from_state,
    needle_zone_for_lowering,
    normalized_needles_zone,
)
from probe_station_gui.stage.types import _Status


logger = logging.getLogger(__name__)


class StageControllerNeedleStatusMixin:
    """Internal needle state and A-position status helpers."""

    def _update_axis_a_ready(self, ready: bool) -> None:
        if ready == self._axis_a_ready:
            return
        self._axis_a_ready = ready
        self.axis_a_ready_changed.emit(ready)

    def _refresh_axis_a_ready_from_state(self) -> None:
        ready = axis_a_ready_from_state(
            needles_up=self._needles_up,
            needles_known=self._needles_known,
            controller_state_stale=self._controller_state_stale,
            serial_is_open=self._serial is not None and self._serial.is_open,
        )
        self._update_axis_a_ready(bool(ready))

    def _set_needles_state(
        self,
        raised: bool,
        *,
        known: bool,
        zone: str | None = None,
    ) -> None:
        normalized_zone = normalized_needles_zone(
            raised,
            known=known,
            zone=zone,
        )

        old_raised = self._needles_up
        old_known = self._needles_known
        old_zone = self._needles_zone
        self._needles_up = bool(raised)
        self._needles_known = bool(known)
        self._needles_zone = normalized_zone
        self._refresh_axis_a_ready_from_state()
        if old_raised != self._needles_up or old_known != self._needles_known:
            self.needles_state_changed.emit(self._needles_up, self._needles_known)
        if old_zone != self._needles_zone:
            self.needles_zone_changed.emit(self._needles_zone or "unknown")

    def _needle_zone_for_a_position(
        self,
        a_position: float,
        status: _Status | None = None,
    ) -> str | None:
        current_lowering = self._axis_a_lowering_for_configured_coordinate(
            float(a_position),
            status,
        )
        return needle_zone_for_lowering(
            current_lowering,
            raise_lowering_mm=self._needle_raise_lowering_mm,
            down_lowering_mm=self._needle_down_lowering_mm,
            contact_zone_mm=self._needle_contact_zone_mm,
            tolerance=self.A_ZERO_TOLERANCE,
        )

    def _update_needles_from_a_position(self, a_position: float) -> None:
        """Update the coarse needles state using the current A coordinate."""

        self.needle_height_changed.emit(
            self._axis_a_lowering_for_configured_coordinate(a_position)
        )
        zone = self._needle_zone_for_a_position(a_position)
        self._set_needles_state(
            zone == "raise",
            known=zone is not None,
            zone=zone,
        )

    def _update_needles_from_status(self, status: _Status) -> None:
        """Update needle state only when A homing is actually known."""

        a_position = self._axis_value_for_configured_mode(status, "A")
        if a_position is None:
            return

        self.needle_height_changed.emit(
            self._axis_a_lowering_for_configured_coordinate(a_position, status)
        )

        effective_homed = status.homed_axes
        if effective_homed is None and self._homed_axes:
            effective_homed = set(self._homed_axes)
        if effective_homed is None or "A" not in effective_homed:
            self._set_needles_state(False, known=False)
            return

        zone = self._needle_zone_for_a_position(a_position, status)
        self._set_needles_state(
            zone == "raise",
            known=zone is not None,
            zone=zone,
        )

    def _read_current_a_position(self) -> Optional[float]:
        """Read the current A coordinate from the configured controller report mode."""

        status = self._query_current_status_with_required_coordinates(
            axes=("A",),
        )
        if status is None:
            self._record_a_position_read_failure(
                "status query returned no complete status frame"
            )
            return None
        position = self._position_for_configured_mode(status)
        mode_name = "machine" if self._position_reporting_mode == "machine" else "work"
        if position is None:
            self._record_a_position_read_failure(
                f"status has no {mode_name} position: "
                f"state={status.state!r}, display_position={status.display_position!r}, "
                f"work_position={status.work_position!r}, work_offset={status.work_offset!r}, "
                f"coordinate_system={status.coordinate_system!r}"
            )
            return None
        idx = self.AXIS_INDEX.get("A")
        if idx is None or idx >= len(position):
            self._record_a_position_read_failure(
                f"{mode_name} position does not include A axis: "
                f"axis_index={idx!r}, position={position!r}, state={status.state!r}"
            )
            return None
        a_position = float(position[idx])
        self._last_a_position_read_failure = None
        logger.debug(
            "A position read succeeded: A=%.6f, mode=%s, state=%s, position=%r, "
            "homed_axes=%r, coordinate_system=%r",
            a_position,
            mode_name,
            status.state,
            position,
            status.homed_axes,
            status.coordinate_system,
        )
        return a_position

    def _record_a_position_read_failure(self, reason: str) -> None:
        self._last_a_position_read_failure = reason
        logger.debug("A position read failed: %s", reason)
