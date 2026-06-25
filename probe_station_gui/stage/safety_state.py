"""Homing, limit, and motion-safety state for stage control."""

from __future__ import annotations

import logging
from collections.abc import Iterable

from probe_station_gui.stage.errors import AxisStateError
from probe_station_gui.stage.types import _Status


logger = logging.getLogger(__name__)


class StageControllerSafetyStateMixin:
    """Internal homing, limit, and motion-safety helpers."""

    def check_motion_safety(self) -> None:
        """Public motion safety gate; raises StageControllerError when unsafe."""

        self._require_open_serial()
        self._move_safety_check()

    def set_motion_safety_disabled(self, disabled: bool) -> None:
        """Enable or disable the explicit motion safety bypass."""

        self._motion_safety_disabled = bool(disabled)
        if self._motion_safety_disabled:
            logger.warning("Motion safety disabled")
        else:
            logger.info("Motion safety enabled")

    def set_unsafe_motion_enabled(self, enabled: bool) -> None:
        """Backward-compatible alias for persisted pre-split settings."""

        self.set_motion_safety_disabled(enabled)

    def homed_axes(self) -> set[str]:
        """Return a snapshot of axes that the live controller reported as homed."""

        return set(self._homed_axes)

    def axes_are_homed(self, axes: set[str]) -> bool:
        """Return True when every requested axis is known to be homed."""

        return set(axes).issubset(self._homed_axes)

    def mark_axes_unhomed(self, axes: Iterable[str]) -> set[str]:
        """Clear cached homing trust for specific axes.

        Returns the axes that were actually removed from the cached homed set.
        """

        normalized_axes = {
            str(axis).strip().upper()
            for axis in axes
            if str(axis).strip()
        }
        removed_axes = self._homed_axes.intersection(normalized_axes)
        if not removed_axes:
            return set()
        self._update_homing_status(self._homed_axes - removed_axes)
        return set(removed_axes)

    def _axis_software_limit_ready(
        self, status: _Status | None, axis: str
    ) -> bool:
        axis = axis.upper().strip()
        if axis == "B":
            return self._b_axis_zero_position is not None
        if axis not in self._axis_limits:
            return False
        effective_homed = self._effective_homed_axes(status)
        if effective_homed is None:
            return False
        return axis in effective_homed

    def _move_safety_check(self) -> None:
        """Validate motion safety prerequisites before any move."""

        if self._motion_safety_disabled:
            return
        if not self._needles_known:
            raise AxisStateError("Needle position unknown. Home/raise A before moving.")
        if not self._needles_up:
            raise AxisStateError("Needles are down. Raise A before moving.")

    def _handle_limit_line(self, line: str) -> None:
        soft_limit_match = self.SOFT_LIMIT_AXIS_PATTERN.search(line)
        if soft_limit_match:
            axis = soft_limit_match.group("axis").upper()
            if axis in self.AXIS_INDEX:
                self._update_limit_axes(set(self._limit_axes).union({axis}))
            return
        if line.strip().lower().startswith("alarm"):
            axes = self._infer_limit_axes_from_position(None)
            if axes:
                self._update_limit_axes(axes)

    def _update_limit_axes_from_status(self, status: _Status) -> None:
        axes: set[str] = set()
        if status.pins:
            axes.update(axis for axis in status.pins if axis in self.AXIS_INDEX)
        if status.state.strip().lower() == "alarm" and not axes:
            axes.update(self._infer_limit_axes_from_position(status))
            if not axes:
                axes.update(self._limit_axes)
        self._update_limit_axes(axes)

    def _infer_limit_axes_from_position(self, status: _Status | None) -> set[str]:
        if status is None:
            position = self._last_stage_position
        else:
            position = self._position_for_configured_mode(status)
        if position is None:
            return set()
        axes: set[str] = set()
        for axis, index in self.AXIS_INDEX.items():
            if index >= len(position):
                continue
            limits = self._axis_limits_for_configured_mode(axis, status)
            if not limits:
                continue
            value = float(position[index])
            min_value, max_value = limits
            tolerance = self.LIMIT_HIT_TOLERANCE
            if value <= min_value + tolerance or value >= max_value - tolerance:
                axes.add(axis)
        return axes

    def _require_homed_axes(
        self, status: _Status, axes: set[str], *, allow_relative: bool = False
    ) -> None:
        effective_homed = self._effective_homed_axes(status)
        if effective_homed is None:
            if allow_relative:
                if not self._relative_warning_emitted:
                    self.status_message.emit(
                        "Homing status unavailable; using relative coordinates."
                    )
                    self._relative_warning_emitted = True
                return
            raise AxisStateError("Homing status unavailable; cannot read coordinates.")
        missing = axes.difference(effective_homed)
        if missing:
            if allow_relative:
                if not self._relative_warning_emitted:
                    ordered = ", ".join(sorted(missing))
                    self.status_message.emit(
                        f"Axes not homed: {ordered}. Using relative coordinates."
                    )
                    self._relative_warning_emitted = True
                return
            ordered = ", ".join(sorted(missing))
            raise AxisStateError(f"Axes not homed: {ordered}.")

    def _effective_homed_axes(self, status: _Status | None) -> set[str] | None:
        if status is None:
            return set(self._homed_axes) if self._homed_axes else None
        effective_homed = status.homed_axes
        if effective_homed is None and self._homed_axes:
            effective_homed = set(self._homed_axes)
        return effective_homed

    def _update_homing_status(self, homed_axes: set[str]) -> None:
        if homed_axes == self._homed_axes:
            return
        self._homed_axes = set(homed_axes)
        self.homing_status_changed.emit(set(self._homed_axes))

    def _update_limit_axes(self, limit_axes: set[str]) -> None:
        axes = {
            str(axis).strip().upper()
            for axis in limit_axes
            if str(axis).strip().upper()
        }
        axes = axes.intersection(self.AXIS_INDEX)
        if axes == self._limit_axes:
            return
        self._limit_axes = set(axes)
        self.limit_axes_changed.emit(set(self._limit_axes))
