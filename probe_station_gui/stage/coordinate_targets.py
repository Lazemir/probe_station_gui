from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

from probe_station_gui.stage.api_moves import normalize_api_coordinate_input_mode
from probe_station_gui.stage.motion_prediction import interpolate_position


RawTargetFromDisplayValue = Callable[[str, float], float | None]
AxisTargetLimitError = Callable[[str, float], str | None]
AxisDisplayLimits = Callable[[str], tuple[float, float] | None]


@dataclass(frozen=True)
class CoordinateTargetConfig:
    axis_names: tuple[str, ...]
    min_feedrate_mm_min: float
    duration_padding_s: float
    min_idle_accept_s: float
    target_tolerance_mm: float


@dataclass(frozen=True)
class CoordinateTargetStatus:
    message: str
    timeout_ms: int


@dataclass(frozen=True)
class CoordinateTargetCommonFeedratePlan:
    clear_common_target: bool
    feedrate_mm_min: float | None = None
    max_feedrate_mm_min: float | None = None


@dataclass(frozen=True)
class CoordinateTargetStartPlan:
    ordered_axes: tuple[str, ...]
    raw_targets: dict[str, float]
    display_targets: dict[str, float]
    origin_position: tuple[float, ...]
    target_position: tuple[float, ...]
    active_axis: str
    active_axes: frozenset[str]
    feedrate_mm_min: float
    duration_s: float
    started_at: float
    ends_at: float
    status: CoordinateTargetStatus
    publish_position: tuple[float, ...]
    invalidate_design_registration: bool
    common_feedrate: CoordinateTargetCommonFeedratePlan
    remove_pending_axes: tuple[str, ...]


@dataclass(frozen=True)
class CoordinateTargetStartDecision:
    accepted: bool
    plan: CoordinateTargetStartPlan | None = None
    status: CoordinateTargetStatus | None = None


@dataclass(frozen=True)
class CoordinateTargetPredictionDecision:
    clear_tracking: bool
    publish_position: tuple[float, ...] | None = None


@dataclass(frozen=True)
class CoordinateTargetFeedrateReissuePlan:
    raw_targets: dict[str, float]
    requested_feedrate_mm_min: float
    origin_position: tuple[float, ...]
    duration_s: float
    started_at: float
    ends_at: float
    set_reissue_cancel_pending: bool


@dataclass(frozen=True)
class CoordinateTargetFeedrateDecision:
    request: CoordinateTargetFeedrateReissuePlan | None = None
    clear_stale_tracking: bool = False
    clear_stage_motion_axes: bool = False
    log_debug_message: str | None = None


@dataclass(frozen=True)
class CoordinateTargetFinishDecision:
    finish: bool
    stage_position: tuple[float, ...] | None = None


@dataclass
class CoordinateTargetMoveState:
    config: CoordinateTargetConfig
    active_axis: str | None = None
    active_axes: set[str] = field(default_factory=set)
    origin_position: tuple[float, ...] | None = None
    stage_position: tuple[float, ...] | None = None
    target_position: tuple[float, ...] | None = None
    display_targets: dict[str, float] = field(default_factory=dict)
    display_basis: object | None = None
    started_at: float | None = None
    ends_at: float | None = None
    programmed_feedrate: float | None = None
    effective_feedrate: float | None = None
    seen_active_state: bool = False
    reissue_cancel_pending: bool = False

    def has_active_move(self) -> bool:
        return bool(self.active_axes_with_fallback())

    def active_axes_with_fallback(self) -> set[str]:
        axes = set(self.active_axes)
        if not axes and self.active_axis is not None:
            axes.add(self.active_axis)
        return axes

    def apply_start_plan(self, plan: CoordinateTargetStartPlan) -> None:
        self.active_axis = plan.active_axis
        self.active_axes = set(plan.active_axes)
        self.origin_position = tuple(float(value) for value in plan.origin_position)
        self.stage_position = tuple(float(value) for value in plan.publish_position)
        self.target_position = tuple(float(value) for value in plan.target_position)
        self.display_targets = {
            axis: float(value) for axis, value in plan.display_targets.items()
        }
        self.display_basis = None
        self.started_at = float(plan.started_at)
        self.ends_at = float(plan.ends_at)
        self.programmed_feedrate = float(plan.feedrate_mm_min)
        self.effective_feedrate = float(plan.feedrate_mm_min)
        self.seen_active_state = False
        self.reissue_cancel_pending = False

    def advance_prediction(
        self,
        *,
        monotonic_s: float,
    ) -> CoordinateTargetPredictionDecision:
        if (
            self.origin_position is None
            or self.target_position is None
            or self.started_at is None
            or self.ends_at is None
        ):
            return CoordinateTargetPredictionDecision(clear_tracking=True)
        self.stage_position = interpolate_position(
            self.origin_position,
            self.target_position,
            self.started_at,
            self.ends_at,
            float(monotonic_s),
        )
        return CoordinateTargetPredictionDecision(
            clear_tracking=False,
            publish_position=self.stage_position,
        )

    def plan_feedrate_reissue(
        self,
        *,
        controller_busy: bool,
        latest_stage_state: str,
        requested_feedrate_mm_min: object,
        monotonic_s: float,
    ) -> CoordinateTargetFeedrateDecision:
        active_axes = self.active_axes_with_fallback()
        if (
            not active_axes
            or self.programmed_feedrate is None
            or self.target_position is None
        ):
            return CoordinateTargetFeedrateDecision()
        state = str(latest_stage_state or "").strip().lower()
        if not controller_busy and state in {"", "idle"}:
            return CoordinateTargetFeedrateDecision(
                clear_stale_tracking=True,
                clear_stage_motion_axes=True,
                log_debug_message=(
                    "Ignoring feedrate change for stale coordinate move tracking."
                ),
            )
        try:
            requested_feedrate = max(
                float(self.config.min_feedrate_mm_min),
                float(requested_feedrate_mm_min),
            )
        except (TypeError, ValueError):
            return CoordinateTargetFeedrateDecision()
        raw_targets = raw_targets_from_position(
            self.config.axis_names,
            target_position=self.target_position,
            active_axes=active_axes,
        )
        if not raw_targets:
            return CoordinateTargetFeedrateDecision()
        current_position = self.stage_position or self.origin_position
        if current_position is None:
            return CoordinateTargetFeedrateDecision()
        duration_s = coordinate_move_duration_s(
            self.config,
            origin_position=current_position,
            target_position=self.target_position,
            active_axes=active_axes,
            feedrate_mm_min=requested_feedrate,
        )
        started_at = float(monotonic_s)
        return CoordinateTargetFeedrateDecision(
            request=CoordinateTargetFeedrateReissuePlan(
                raw_targets=raw_targets,
                requested_feedrate_mm_min=requested_feedrate,
                origin_position=tuple(float(value) for value in current_position),
                duration_s=duration_s,
                started_at=started_at,
                ends_at=started_at + max(duration_s, 0.05),
                set_reissue_cancel_pending=bool(controller_busy),
            )
        )

    def apply_feedrate_reissue_success(
        self,
        plan: CoordinateTargetFeedrateReissuePlan,
    ) -> None:
        self.origin_position = tuple(float(value) for value in plan.origin_position)
        self.programmed_feedrate = float(plan.requested_feedrate_mm_min)
        self.effective_feedrate = float(plan.requested_feedrate_mm_min)
        self.started_at = float(plan.started_at)
        self.ends_at = float(plan.ends_at)
        self.reissue_cancel_pending = bool(plan.set_reissue_cancel_pending)

    def clear_tracking(self) -> None:
        self.active_axis = None
        self.active_axes.clear()
        self.origin_position = None
        self.stage_position = None
        self.target_position = None
        self.display_targets.clear()
        self.display_basis = None
        self.started_at = None
        self.ends_at = None
        self.programmed_feedrate = None
        self.effective_feedrate = None
        self.seen_active_state = False
        self.reissue_cancel_pending = False

    def finish_if_idle_decision(
        self,
        *,
        latest_stage_state: str,
        position: object | None,
        monotonic_s: float,
    ) -> CoordinateTargetFinishDecision:
        if not self.has_active_move():
            return CoordinateTargetFinishDecision(finish=False)
        if str(latest_stage_state or "").strip().lower() != "idle":
            return CoordinateTargetFinishDecision(finish=False)
        if not coordinate_position_is_at_target(
            self.config,
            target_position=self.target_position,
            active_axes=self.active_axes_with_fallback(),
            position=position,
        ):
            return CoordinateTargetFinishDecision(finish=False)
        if (
            self.started_at is not None
            and float(monotonic_s) - self.started_at
            < float(self.config.min_idle_accept_s)
        ):
            return CoordinateTargetFinishDecision(finish=False)
        stage_position = _coerce_position_tuple(position)
        return CoordinateTargetFinishDecision(
            finish=True,
            stage_position=stage_position,
        )


def coordinate_target_common_feedrate_plan(
    axes: Sequence[str],
    feedrate_mm_min: float,
    *,
    axis_max_feedrates: Mapping[str, object] | None,
    min_feedrate_mm_min: float,
) -> CoordinateTargetCommonFeedratePlan:
    axis_set = {str(axis).strip().upper() for axis in axes}
    if len(axis_set) <= 1 or axis_set <= {"X", "Y"}:
        return CoordinateTargetCommonFeedratePlan(clear_common_target=True)
    axis_limits: list[float] = []
    limits = axis_max_feedrates or {}
    for axis in axes:
        try:
            value = float(limits.get(axis, 0.0))
        except (AttributeError, TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0.0:
            axis_limits.append(value)
    max_feedrate = (
        max(axis_limits)
        if axis_limits
        else max(float(min_feedrate_mm_min), float(feedrate_mm_min))
    )
    return CoordinateTargetCommonFeedratePlan(
        clear_common_target=False,
        feedrate_mm_min=float(feedrate_mm_min),
        max_feedrate_mm_min=float(max_feedrate),
    )


def plan_coordinate_target_start(
    config: CoordinateTargetConfig,
    *,
    targets: Mapping[str, tuple[float, float]],
    feedrate_mm_min: float,
    source_label: str,
    seed_position: object | None,
    latest_stage_position: object | None,
    axis_target_limit_error: AxisTargetLimitError,
    axis_max_feedrates: Mapping[str, object] | None,
    monotonic_s: float,
) -> CoordinateTargetStartDecision:
    ordered_axes = tuple(
        axis for axis in config.axis_names if axis in targets
    )
    if not ordered_axes:
        return CoordinateTargetStartDecision(accepted=False)
    feedrate = max(float(config.min_feedrate_mm_min), float(feedrate_mm_min))
    origin_position = seed_position
    if origin_position is None:
        origin_position = latest_stage_position
    origin_tuple = _coerce_position_tuple(origin_position)
    if origin_tuple is None:
        return CoordinateTargetStartDecision(
            accepted=False,
            status=CoordinateTargetStatus(
                "Stage coordinates are unavailable.",
                3000,
            ),
        )
    raw_targets = {
        axis: float(targets[axis][0])
        for axis in ordered_axes
    }
    display_targets = {
        axis: float(targets[axis][1])
        for axis in ordered_axes
    }
    for axis, display_target in display_targets.items():
        limit_error = axis_target_limit_error(axis, display_target)
        if limit_error is not None:
            return CoordinateTargetStartDecision(
                accepted=False,
                status=CoordinateTargetStatus(limit_error, 4000),
            )
    target_position = position_with_axis_values(
        config.axis_names,
        raw_targets,
        base_position=origin_tuple,
    )
    if target_position is None:
        return CoordinateTargetStartDecision(
            accepted=False,
            status=CoordinateTargetStatus(
                "Stage coordinates are unavailable.",
                3000,
            ),
        )
    active_axes = frozenset(ordered_axes)
    duration_s = coordinate_move_duration_s(
        config,
        origin_position=origin_tuple,
        target_position=target_position,
        active_axes=active_axes,
        feedrate_mm_min=feedrate,
    )
    started_at = float(monotonic_s)
    status = CoordinateTargetStatus(
        "Moving "
        + ", ".join(
            f"{axis}={display_targets[axis]:.3f}" for axis in ordered_axes
        )
        + f" at F{feedrate:.1f} from {source_label}.",
        3000,
    )
    return CoordinateTargetStartDecision(
        accepted=True,
        plan=CoordinateTargetStartPlan(
            ordered_axes=ordered_axes,
            raw_targets=raw_targets,
            display_targets=display_targets,
            origin_position=origin_tuple,
            target_position=target_position,
            active_axis=ordered_axes[0],
            active_axes=active_axes,
            feedrate_mm_min=feedrate,
            duration_s=duration_s,
            started_at=started_at,
            ends_at=started_at + max(duration_s, 0.05),
            status=status,
            publish_position=origin_tuple,
            invalidate_design_registration=False,
            common_feedrate=coordinate_target_common_feedrate_plan(
                ordered_axes,
                feedrate,
                axis_max_feedrates=axis_max_feedrates,
                min_feedrate_mm_min=config.min_feedrate_mm_min,
            ),
            remove_pending_axes=ordered_axes,
        ),
    )


def coordinate_move_duration_s(
    config: CoordinateTargetConfig,
    *,
    origin_position: Sequence[float],
    target_position: Sequence[float],
    active_axes: Sequence[str],
    feedrate_mm_min: float,
) -> float:
    squared = 0.0
    for axis in active_axes:
        try:
            axis_index = config.axis_names.index(axis)
        except ValueError:
            continue
        if axis_index >= len(origin_position) or axis_index >= len(target_position):
            continue
        delta = float(target_position[axis_index]) - float(origin_position[axis_index])
        squared += delta * delta
    distance = math.sqrt(squared)
    speed_mm_per_s = max(
        float(config.min_feedrate_mm_min),
        float(feedrate_mm_min),
    ) / 60.0
    return (distance / speed_mm_per_s) + float(config.duration_padding_s)


def coordinate_position_is_at_target(
    config: CoordinateTargetConfig,
    *,
    target_position: Sequence[float] | None,
    active_axes: Sequence[str],
    position: object | None,
) -> bool:
    if target_position is None or not isinstance(position, (tuple, list)):
        return False
    if not active_axes:
        return False
    for axis in active_axes:
        try:
            axis_index = config.axis_names.index(axis)
        except ValueError:
            return False
        if axis_index >= len(position) or axis_index >= len(target_position):
            return False
        if (
            abs(float(position[axis_index]) - float(target_position[axis_index]))
            > float(config.target_tolerance_mm)
        ):
            return False
    return True


def raw_targets_from_position(
    axis_names: Sequence[str],
    *,
    target_position: Sequence[float],
    active_axes: Sequence[str],
) -> dict[str, float]:
    raw_targets: dict[str, float] = {}
    active_axis_set = {str(axis).strip().upper() for axis in active_axes}
    for axis in axis_names:
        if axis not in active_axis_set:
            continue
        try:
            axis_index = axis_names.index(axis)
        except ValueError:
            continue
        if axis_index >= len(target_position):
            continue
        raw_targets[axis] = float(target_position[axis_index])
    return raw_targets


def position_with_axis_values(
    axis_names: Sequence[str],
    raw_targets: Mapping[str, float],
    *,
    base_position: object | None,
) -> tuple[float, ...] | None:
    if not isinstance(base_position, (tuple, list)):
        return None
    try:
        values = [float(value) for value in base_position]
    except (TypeError, ValueError):
        return None
    for axis_name, raw_value in raw_targets.items():
        axis = str(axis_name).strip().upper()
        try:
            axis_index = axis_names.index(axis)
        except ValueError:
            return None
        if len(values) <= axis_index:
            return None
        values[axis_index] = float(raw_value)
    return tuple(values)


def resolve_stage_axis_target(
    axis_names: Sequence[str],
    *,
    raw_target_from_display_value: RawTargetFromDisplayValue,
    display_values: Mapping[str, float],
    axis_name: str,
    input_value: float,
    input_mode: str,
) -> tuple[float | None, float]:
    axis = str(axis_name).strip().upper()
    if axis not in axis_names:
        return None, float(input_value)
    mode = normalize_api_coordinate_input_mode(input_mode) or "G90"
    if mode == "G91":
        current_display = display_values.get(axis)
        if current_display is None:
            return None, float(input_value)
        display_target = float(current_display) + float(input_value)
    else:
        display_target = float(input_value)
    raw_target = raw_target_from_display_value(axis, display_target)
    return raw_target, display_target


def stage_axis_target_limit_error(
    axis_name: str,
    display_target: float,
    *,
    homed_axes: set[str],
    axis_display_limits: AxisDisplayLimits,
) -> str | None:
    axis = str(axis_name).strip().upper()
    if axis not in homed_axes:
        return None
    limits = axis_display_limits(axis)
    if limits is None:
        return None
    min_value, max_value = limits
    target = float(display_target)
    if min_value <= target <= max_value:
        return None
    return (
        f"{axis} target {target:+.3f} exceeds software limits "
        f"({min_value:.3f}..{max_value:.3f})."
    )


def _coerce_position_tuple(position: object | None) -> tuple[float, ...] | None:
    if not isinstance(position, (tuple, list)):
        return None
    try:
        return tuple(float(value) for value in position)
    except (TypeError, ValueError):
        return None


__all__ = [
    "CoordinateTargetCommonFeedratePlan",
    "CoordinateTargetConfig",
    "CoordinateTargetFeedrateDecision",
    "CoordinateTargetFeedrateReissuePlan",
    "CoordinateTargetFinishDecision",
    "CoordinateTargetMoveState",
    "CoordinateTargetPredictionDecision",
    "CoordinateTargetStartDecision",
    "CoordinateTargetStartPlan",
    "CoordinateTargetStatus",
    "coordinate_move_duration_s",
    "coordinate_position_is_at_target",
    "coordinate_target_common_feedrate_plan",
    "plan_coordinate_target_start",
    "position_with_axis_values",
    "raw_targets_from_position",
    "resolve_stage_axis_target",
    "stage_axis_target_limit_error",
]
