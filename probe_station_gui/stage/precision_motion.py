"""StageController integration for backlash-aware target moves."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable

from probe_station_gui.settings.precision_approach import (
    PRECISION_APPROACH_AXES,
    PrecisionApproachProfile,
    PrecisionApproachSettings,
    precision_profile_is_effective,
)
from probe_station_gui.stage.coordinate_confidence import AxisCoordinateConfidence
from probe_station_gui.stage.errors import StageControllerError
from probe_station_gui.stage.precision_approach import PrecisionApproachPlanner


COORDINATE_CONFIDENCE_STATE_VERSION = 1
COORDINATE_CONFIDENCE_RESTORE_TOLERANCE = 7.5e-4
CALIBRATED_TARGET_ROUND_TRIP_TOLERANCE = 1e-9


class StageControllerPrecisionMotionMixin:
    """Plan precise final approaches and track confirmed coordinate confidence."""

    if TYPE_CHECKING:

        def __getattr__(self, name: str) -> Any: ...

    def _initialize_precision_motion(self) -> None:
        disabled = {
            axis: PrecisionApproachProfile() for axis in PRECISION_APPROACH_AXES
        }
        self._precision_approach_settings = PrecisionApproachSettings(disabled)
        self._coordinate_confidence = {
            axis: AxisCoordinateConfidence() for axis in PRECISION_APPROACH_AXES
        }
        self._precision_profile_fingerprints = self._precision_fingerprints()
        self._pending_coordinate_confidence_restore: dict[str, object] | None = None
        self._precision_motion_in_flight = False

    def apply_precision_approach_configuration(
        self,
        settings: PrecisionApproachSettings,
    ) -> None:
        """Apply profiles and invalidate axes whose mechanical model changed."""

        previous = dict(self._precision_profile_fingerprints)
        self._precision_approach_settings = settings.clone()
        current = self._precision_fingerprints()
        self._precision_profile_fingerprints = current
        profile_changed_axes = {
            axis
            for axis in PRECISION_APPROACH_AXES
            if previous.get(axis) != current.get(axis)
        }
        enabled_changed_axes = {
            axis
            for axis in profile_changed_axes
            if precision_profile_is_effective(
                self._precision_approach_settings.profiles[axis]
            )
        }
        if enabled_changed_axes:
            self._invalidate_coordinate_confidence(
                enabled_changed_axes,
                reason="Precision approach configuration changed.",
            )
        disabled_changed_axes = profile_changed_axes - enabled_changed_axes
        if disabled_changed_axes:
            self._emit_coordinate_confidence_changed(disabled_changed_axes)

    def precision_approach_enabled_axes(self) -> frozenset[str]:
        return frozenset(
            axis
            for axis, profile in self._precision_approach_settings.profiles.items()
            if precision_profile_is_effective(profile)
        )

    def coordinate_confidence(self) -> dict[str, AxisCoordinateConfidence]:
        return dict(self._coordinate_confidence)

    def _precision_targets_require_execution(
        self,
        targets: Mapping[str, float],
    ) -> bool:
        return any(
            precision_profile_is_effective(
                self._precision_approach_settings.profiles[axis]
            )
            and not self._coordinate_confidence[axis].exact
            for axis in targets
        )

    def invalidate_coordinate_confidence(
        self,
        reason: str = "Coordinate confidence invalidated.",
        *,
        axes: object | None = None,
    ) -> None:
        normalized = self._normalized_confidence_axes(axes)
        self._invalidate_coordinate_confidence(normalized, reason=reason)

    def _execute_precision_axis_targets_locked(
        self,
        targets: Mapping[str, float],
        *,
        feedrate: float | None,
        allow_unhomed: bool,
        wait_for_completion: bool = True,
        ignore_needle_safety: bool = False,
        before_first_segment: Callable[[], None] | None = None,
    ) -> None:
        ordered_targets = {
            axis: float(targets[axis])
            for axis in self.AXIS_INDEX
            if axis in targets
        }
        if not ordered_targets:
            raise StageControllerError("No coordinate targets provided.")
        enabled_axes = {
            axis
            for axis in ordered_targets
            if precision_profile_is_effective(
                self._precision_approach_settings.profiles[axis]
            )
        }
        if not enabled_axes:
            self._check_cancelled()
            if not ignore_needle_safety:
                self._move_safety_check()
            self._send_absolute_axis_targets_move(
                ordered_targets,
                ignore_needle_safety=True,
                feedrate=feedrate,
                wait_for_completion=wait_for_completion,
                allow_unhomed=allow_unhomed,
                as_jog=True,
                motion_started_callback=before_first_segment,
            )
            return

        axes = tuple(ordered_targets)
        status = self._query_current_status_with_required_coordinates(axes=axes)
        current_raw: dict[str, float] = {}
        for axis in axes:
            value = self._axis_value_for_configured_mode(status, axis)
            if value is None:
                raise StageControllerError(
                    f"Unable to read {axis} position for precision approach."
                )
            current_raw[axis] = float(value)
        current_display = {
            axis: self.calibrated_axis_display_value(axis, value)
            for axis, value in current_raw.items()
        }
        target_display = {
            axis: self.calibrated_axis_display_value(axis, value)
            for axis, value in ordered_targets.items()
        }

        def validate(display_targets: Mapping[str, float]) -> None:
            raw_targets = self._raw_precision_targets(display_targets)
            self._validate_absolute_axis_targets_move(
                raw_targets,
                allow_unhomed=allow_unhomed,
                status=status,
            )

        plan = PrecisionApproachPlanner().plan(
            current=current_display,
            target=target_display,
            profiles=self._precision_approach_settings.profiles,
            confidence=self._coordinate_confidence,
            validate_target=validate,
        )
        segments = []
        if plan.preparation_target is not None:
            segments.append(self._raw_precision_targets(plan.preparation_target))
        segments.append(self._raw_precision_targets(plan.final_target))

        sent_segment = False
        first_segment = True
        self._precision_motion_in_flight = True
        try:
            for segment in segments:
                self._check_cancelled()
                if not ignore_needle_safety:
                    self._move_safety_check()
                sent_segment = True
                self._send_absolute_axis_targets_move(
                    segment,
                    ignore_needle_safety=True,
                    feedrate=feedrate,
                    wait_for_completion=True,
                    allow_unhomed=allow_unhomed,
                    as_jog=True,
                    motion_started_callback=(
                        before_first_segment if first_segment else None
                    ),
                )
                first_segment = False
            final_status = self._query_current_status_with_required_coordinates(axes=axes)
            changed_axes: set[str] = set()
            for axis in enabled_axes:
                raw_value = self._axis_value_for_configured_mode(final_status, axis)
                if raw_value is None:
                    raise StageControllerError(
                        f"Unable to confirm {axis} precision target."
                    )
                display_value = self.calibrated_axis_display_value(axis, raw_value)
                profile = self._precision_approach_settings.profiles[axis]
                self._coordinate_confidence[axis] = self._coordinate_confidence[
                    axis
                ].after_precision_final(
                    coordinate=display_value,
                    final_direction=profile.final_direction,
                )
                changed_axes.add(axis)
            self._emit_coordinate_confidence_changed(changed_axes)
        except StageControllerError:
            if sent_segment:
                self._invalidate_coordinate_confidence(
                    enabled_axes,
                    reason="Precision motion did not complete.",
                )
            raise
        finally:
            self._precision_motion_in_flight = False

    def _raw_precision_targets(
        self,
        display_targets: Mapping[str, float],
    ) -> dict[str, float]:
        raw_targets: dict[str, float] = {}
        for axis, value in display_targets.items():
            raw_value = self.calibrated_axis_raw_value(axis, value)
            round_trip_value = self.calibrated_axis_display_value(axis, raw_value)
            if not math.isclose(
                round_trip_value,
                float(value),
                rel_tol=0.0,
                abs_tol=CALIBRATED_TARGET_ROUND_TRIP_TOLERANCE,
            ):
                raise StageControllerError(
                    f"{axis} precision target cannot be represented by the "
                    "calibrated axis mapping."
                )
            raw_targets[axis] = raw_value
        return raw_targets

    def _normalized_confidence_axes(self, axes: object | None) -> set[str]:
        if axes is None:
            candidates = PRECISION_APPROACH_AXES
        elif isinstance(axes, str):
            candidates = (axes,)
        elif isinstance(axes, (list, tuple, set, frozenset)):
            candidates = axes
        else:
            candidates = ()
        return {
            str(axis).strip().upper()
            for axis in candidates
            if str(axis).strip().upper() in self._coordinate_confidence
            and precision_profile_is_effective(
                self._precision_approach_settings.profiles[
                    str(axis).strip().upper()
                ]
            )
        }

    def _invalidate_coordinate_confidence(
        self,
        axes: object,
        *,
        reason: str,
        emit: bool = True,
    ) -> None:
        normalized = self._normalized_confidence_axes(axes)
        for axis in normalized:
            self._coordinate_confidence[axis] = self._coordinate_confidence[
                axis
            ].invalidate(reason=reason)
        if emit:
            self._emit_coordinate_confidence_changed(normalized)

    def _emit_coordinate_confidence_changed(self, axes: object) -> None:
        normalized = {
            str(axis).strip().upper()
            for axis in axes
            if str(axis).strip().upper() in self._coordinate_confidence
        }
        if normalized:
            self.coordinate_confidence_changed.emit(
                {
                    axis: self._coordinate_confidence[axis]
                    for axis in sorted(normalized)
                }
            )

    def _precision_fingerprints(self) -> dict[str, str]:
        return {
            axis: self._precision_axis_fingerprint(axis)
            for axis in PRECISION_APPROACH_AXES
        }

    def _precision_axis_fingerprint(self, axis: str) -> str:
        calibration: object = None
        if axis == "A":
            calibration = getattr(self, "_axis_a_calibration", None)
        elif axis == "Z":
            calibration = getattr(self, "_axis_z_calibration", None)
        payload = {
            "profile": self._precision_approach_settings.profiles[axis].to_dict(),
            "calibration": calibration,
        }
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def _export_coordinate_confidence_state(self) -> dict[str, object]:
        records: dict[str, object] = {}
        machine_position = self._last_machine_position
        for axis in PRECISION_APPROACH_AXES:
            profile = self._precision_approach_settings.profiles[axis]
            if not precision_profile_is_effective(profile):
                continue
            index = self.AXIS_INDEX[axis]
            last_machine = (
                float(machine_position[index])
                if machine_position is not None and index < len(machine_position)
                else None
            )
            state = self._coordinate_confidence[axis]
            records[axis] = {
                "exact": bool(state.exact and not self._precision_motion_in_flight),
                "loaded_direction": state.loaded_direction,
                "confirmed_coordinate": state.confirmed_machine_coordinate,
                "takeup_travel": state.takeup_travel,
                "last_machine_coordinate": last_machine,
                "fingerprint": self._precision_profile_fingerprints[axis],
            }
        return {
            "version": COORDINATE_CONFIDENCE_STATE_VERSION,
            "axes": records,
        }

    def _import_coordinate_confidence_state(self, data: Mapping[str, object]) -> None:
        raw = data.get("coordinate_confidence")
        if not isinstance(raw, dict) or raw.get("version") != 1:
            self._pending_coordinate_confidence_restore = {}
            return
        axes = raw.get("axes")
        self._pending_coordinate_confidence_restore = (
            dict(axes) if isinstance(axes, dict) else {}
        )

    def _restore_pending_coordinate_confidence(self, status: object) -> None:
        pending = self._pending_coordinate_confidence_restore
        if pending is None:
            return
        self._pending_coordinate_confidence_restore = None
        machine_position = getattr(status, "position", None)
        changed_axes: set[str] = set()
        for axis in PRECISION_APPROACH_AXES:
            profile = self._precision_approach_settings.profiles[axis]
            if not precision_profile_is_effective(profile):
                continue
            changed_axes.add(axis)
            record = pending.get(axis)
            restored = AxisCoordinateConfidence()
            if isinstance(record, dict):
                restored = self._restored_axis_confidence(
                    axis,
                    record,
                    machine_position=machine_position,
                )
            self._coordinate_confidence[axis] = restored
        self._emit_coordinate_confidence_changed(changed_axes)

    def _update_coordinate_confidence_from_status(self, status: object) -> None:
        if self._pending_coordinate_confidence_restore is not None:
            self._restore_pending_coordinate_confidence(status)
            return
        if self._precision_motion_in_flight:
            return
        raw_position = getattr(status, "display_position", None)
        if not isinstance(raw_position, (list, tuple)):
            return
        changed_axes: set[str] = set()
        for axis in PRECISION_APPROACH_AXES:
            profile = self._precision_approach_settings.profiles[axis]
            if not precision_profile_is_effective(profile):
                continue
            index = self.AXIS_INDEX[axis]
            if index >= len(raw_position):
                continue
            display_coordinate = self.calibrated_axis_display_value(
                axis,
                float(raw_position[index]),
            )
            current = self._coordinate_confidence[axis]
            updated = current.after_confirmed_motion(
                coordinate=display_coordinate,
                profile=profile,
            )
            if updated != current:
                self._coordinate_confidence[axis] = updated
                changed_axes.add(axis)
        self._emit_coordinate_confidence_changed(changed_axes)

    def _restored_axis_confidence(
        self,
        axis: str,
        record: Mapping[str, object],
        *,
        machine_position: object,
    ) -> AxisCoordinateConfidence:
        if record.get("fingerprint") != self._precision_profile_fingerprints[axis]:
            return AxisCoordinateConfidence()
        index = self.AXIS_INDEX[axis]
        if not isinstance(machine_position, (list, tuple)) or index >= len(machine_position):
            return AxisCoordinateConfidence()
        try:
            live_machine = float(machine_position[index])
            cached_machine = float(record.get("last_machine_coordinate"))
            coordinate = float(record.get("confirmed_coordinate"))
            takeup = float(record.get("takeup_travel", 0.0))
        except (TypeError, ValueError):
            return AxisCoordinateConfidence()
        if abs(live_machine - cached_machine) > COORDINATE_CONFIDENCE_RESTORE_TOLERANCE:
            return AxisCoordinateConfidence()
        direction = record.get("loaded_direction")
        loaded_direction = int(direction) if direction in (-1, 1) else None
        return AxisCoordinateConfidence(
            exact=bool(record.get("exact", False)),
            loaded_direction=loaded_direction,
            confirmed_machine_coordinate=coordinate,
            takeup_travel=max(0.0, takeup),
        )


__all__ = [
    "COORDINATE_CONFIDENCE_RESTORE_TOLERANCE",
    "COORDINATE_CONFIDENCE_STATE_VERSION",
    "StageControllerPrecisionMotionMixin",
]
