from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ManualJogPredictionConfig:
    axis_names: tuple[str, ...]
    ignore_idle_after_command_s: float
    reconcile_smooth_threshold_mm: float
    reconcile_smooth_alpha: float
    status_settle_hold_s: float
    default_stop_tail_s: float
    stop_tail_min_s: float
    stop_tail_max_s: float
    stop_tail_learn_alpha: float


@dataclass(frozen=True)
class ManualJogCommandResult:
    handled: bool
    zero_distance: bool
    clear_coordinate_move_tracking: bool
    motion_axes: frozenset[str]
    clear_motion_axes: bool
    publish_position: tuple[float, ...] | None
    stage_source: str
    start_timer: bool
    stop_timer: bool
    schedule_status_refreshes: bool


@dataclass(frozen=True)
class ManualJogStopResult:
    stop_tail_s: float
    start_timer: bool
    stop_timer: bool
    schedule_status_refreshes: bool
    resume_live_poll: bool


@dataclass(frozen=True)
class ManualJogAdvanceResult:
    publish_position: tuple[float, ...] | None
    velocity_xy: tuple[float, float]
    dt: float
    log_tick: bool
    stop_timer: bool


@dataclass(frozen=True)
class ManualJogIdleSampleResult:
    ignore: bool
    age_s: float | None
    state: str


@dataclass(frozen=True)
class ManualJogStopTailLearnResult:
    old_tail_s: float
    residual_s: float
    learned_tail_s: float
    new_tail_s: float


@dataclass
class ManualJogPredictionState:
    config: ManualJogPredictionConfig
    stage_position: tuple[float, ...] | None = None
    stage_xy: tuple[float, float] | None = None
    axis_velocities: dict[str, float] = field(default_factory=dict)
    stop_axis_velocities: dict[str, float] = field(default_factory=dict)
    velocity_xy: tuple[float, float] | None = None
    stop_prediction_until: float | None = None
    stop_tail_position: tuple[float, ...] | None = None
    stop_tail_s: float | None = None
    command_started_at: float | None = None
    last_timestamp: float | None = None
    last_prediction_log_at: float = 0.0
    waiting_for_fresh_status: bool = False
    settle_until: float = 0.0
    stop_status_timestamp: float | None = None

    def __post_init__(self) -> None:
        if self.stop_tail_s is None:
            self.stop_tail_s = float(self.config.default_stop_tail_s)

    def reset_tracking(self) -> None:
        self.stage_position = None
        self.stage_xy = None
        self.axis_velocities.clear()
        self.stop_axis_velocities.clear()
        self.velocity_xy = None
        self.stop_prediction_until = None
        self.stop_tail_position = None
        self.command_started_at = None
        self.last_timestamp = None
        self.last_prediction_log_at = 0.0
        self.waiting_for_fresh_status = False
        self.settle_until = 0.0
        self.stop_status_timestamp = None

    def prediction_active(self, now: float | None = None) -> bool:
        if self.axis_velocities:
            return True
        if self.stop_axis_velocities and self.stop_prediction_until is not None:
            current_time = _monotonic(now)
            return bool(
                current_time < self.stop_prediction_until
                or (
                    self.last_timestamp is not None
                    and self.last_timestamp < self.stop_prediction_until
                )
            )
        return False

    def prediction_available(self, now: float | None = None) -> bool:
        return bool(
            self.prediction_active(now)
            or self.stop_axis_velocities
            or (self.waiting_for_fresh_status and self.stage_position is not None)
        )

    def prediction_velocities(self, now: float | None = None) -> dict[str, float]:
        if self.axis_velocities:
            return dict(self.axis_velocities)
        if self.prediction_active(now):
            return dict(self.stop_axis_velocities)
        return {}

    def clear_stop_prediction(self) -> None:
        self.stop_axis_velocities.clear()
        self.stop_prediction_until = None
        self.stop_tail_position = None

    def clear_waiting_status(self) -> None:
        self.waiting_for_fresh_status = False
        self.settle_until = 0.0
        self.stop_status_timestamp = None

    def predicted_stage_xy(self, now: float | None = None) -> tuple[float, float] | None:
        if not self.prediction_available(now):
            return None
        stage_xy = _stage_xy_from_position(self.stage_position)
        if stage_xy is not None:
            return stage_xy
        return self.stage_xy

    def resolve_waiting_stage_xy(
        self,
        *,
        now: float | None = None,
        latest_state: str,
        last_status_timestamp: float | None,
    ) -> tuple[float, float] | None:
        if not self.waiting_for_fresh_status:
            return None
        current_time = _monotonic(now)
        state = str(latest_state or "").lower()
        if (
            last_status_timestamp is not None
            and self.stop_status_timestamp is not None
            and last_status_timestamp > self.stop_status_timestamp
            and state == "idle"
        ):
            self.clear_waiting_status()
            self.clear_stop_prediction()
            return None
        if self.stage_xy is not None and current_time < self.settle_until:
            return self.stage_xy
        self.clear_waiting_status()
        self.clear_stop_prediction()
        return None

    def handle_command(
        self,
        commanded_distances: object,
        *,
        feedrate: float,
        now: float,
        coordinate_move_active: bool = False,
        coordinate_move_stage_position: object | None = None,
        latest_stage_position: object | None = None,
        current_design_stage_xy: tuple[float, float] | None = None,
    ) -> ManualJogCommandResult:
        if not isinstance(commanded_distances, tuple):
            return ManualJogCommandResult(
                handled=False,
                zero_distance=False,
                clear_coordinate_move_tracking=False,
                motion_axes=frozenset(),
                clear_motion_axes=False,
                publish_position=None,
                stage_source="",
                start_timer=False,
                stop_timer=False,
                schedule_status_refreshes=False,
            )

        self.clear_waiting_status()
        axis_components = {axis: 0.0 for axis in self.config.axis_names}
        for item in commanded_distances:
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            axis = str(item[0]).upper()
            try:
                distance = float(item[1])
            except (TypeError, ValueError):
                continue
            if axis in axis_components:
                axis_components[axis] = distance

        path_length = math.sqrt(
            sum(component * component for component in axis_components.values())
        )
        if path_length <= 1e-9:
            self.axis_velocities.clear()
            self.clear_stop_prediction()
            self.velocity_xy = None
            return ManualJogCommandResult(
                handled=True,
                zero_distance=True,
                clear_coordinate_move_tracking=False,
                motion_axes=frozenset(),
                clear_motion_axes=True,
                publish_position=None,
                stage_source="",
                start_timer=False,
                stop_timer=True,
                schedule_status_refreshes=True,
            )

        speed_mm_per_s = max(0.0, float(feedrate)) / 60.0
        motion_axes = frozenset(
            axis for axis, distance in axis_components.items() if abs(distance) > 1e-9
        )
        self.axis_velocities = {
            axis: speed_mm_per_s * distance / path_length
            for axis, distance in axis_components.items()
            if abs(distance) > 1e-9
        }
        self.velocity_xy = (
            self.axis_velocities.get("X", 0.0),
            self.axis_velocities.get("Y", 0.0),
        )
        self.clear_stop_prediction()

        stage_source = "tracked"
        if self.stage_position is None and self.stage_xy is None:
            latest = _coerce_position_tuple(latest_stage_position)
            if latest is not None and len(latest) >= 2:
                stage_source = "latest_status"
            elif current_design_stage_xy is not None:
                stage_source = "current_design"
            else:
                stage_source = "unknown"

        self.stage_position = self.seed_position(
            coordinate_move_stage_position=coordinate_move_stage_position,
            latest_stage_position=latest_stage_position,
            current_design_stage_xy=current_design_stage_xy,
        )
        self.stage_xy = _stage_xy_from_position(self.stage_position)
        if self.stage_position is None and current_design_stage_xy is not None:
            self.stage_position = _position_with_stage_xy(
                current_design_stage_xy,
                base_position=latest_stage_position,
            )
            self.stage_xy = tuple(
                float(value) for value in current_design_stage_xy
            )
            stage_source = "current_design"

        current_time = float(now)
        self.command_started_at = current_time
        self.last_timestamp = current_time
        self.last_prediction_log_at = 0.0
        return ManualJogCommandResult(
            handled=True,
            zero_distance=False,
            clear_coordinate_move_tracking=bool(coordinate_move_active),
            motion_axes=motion_axes,
            clear_motion_axes=False,
            publish_position=self.stage_position,
            stage_source=stage_source,
            start_timer=True,
            stop_timer=False,
            schedule_status_refreshes=False,
        )

    def handle_stop(
        self,
        *,
        now: float,
        last_status_timestamp: float | None,
    ) -> ManualJogStopResult:
        current_time = float(now)
        stop_velocities = dict(self.axis_velocities)
        if stop_velocities and self.stage_position is not None:
            self.stop_axis_velocities = stop_velocities
            self.stop_prediction_until = current_time + float(self.stop_tail_s or 0.0)
            self.stop_tail_position = None
            if self.last_timestamp is None:
                self.last_timestamp = current_time
            start_timer = True
            stop_timer = False
        else:
            self.clear_stop_prediction()
            start_timer = False
            stop_timer = True

        self.axis_velocities.clear()
        self.velocity_xy = None
        self.last_prediction_log_at = 0.0
        self.waiting_for_fresh_status = self.stage_xy is not None
        self.settle_until = current_time + self.config.status_settle_hold_s
        self.stop_status_timestamp = last_status_timestamp
        return ManualJogStopResult(
            stop_tail_s=float(self.stop_tail_s or 0.0) if stop_velocities else 0.0,
            start_timer=start_timer,
            stop_timer=stop_timer,
            schedule_status_refreshes=True,
            resume_live_poll=True,
        )

    def advance(
        self,
        *,
        now: float,
        coordinate_move_stage_position: object | None = None,
        latest_stage_position: object | None = None,
        current_design_stage_xy: tuple[float, float] | None = None,
    ) -> ManualJogAdvanceResult:
        velocities = self.prediction_velocities(now)
        if not velocities:
            return ManualJogAdvanceResult(
                publish_position=None,
                velocity_xy=(0.0, 0.0),
                dt=0.0,
                log_tick=False,
                stop_timer=False,
            )

        current_time = float(now)
        if self.last_timestamp is None:
            self.last_timestamp = current_time
            return ManualJogAdvanceResult(
                publish_position=None,
                velocity_xy=(velocities.get("X", 0.0), velocities.get("Y", 0.0)),
                dt=0.0,
                log_tick=False,
                stop_timer=False,
            )

        prediction_now = current_time
        if not self.axis_velocities and self.stop_prediction_until is not None:
            prediction_now = min(current_time, self.stop_prediction_until)
        dt = max(0.0, prediction_now - self.last_timestamp)
        self.last_timestamp = prediction_now
        if dt <= 0.0:
            should_stop_timer = bool(
                self.stop_prediction_until is not None
                and current_time >= self.stop_prediction_until
            )
            if should_stop_timer:
                self.stop_tail_position = self.stage_position
            return ManualJogAdvanceResult(
                publish_position=None,
                velocity_xy=(velocities.get("X", 0.0), velocities.get("Y", 0.0)),
                dt=0.0,
                log_tick=False,
                stop_timer=should_stop_timer,
            )

        if self.stage_position is None:
            self.stage_position = self.seed_position(
                coordinate_move_stage_position=coordinate_move_stage_position,
                latest_stage_position=latest_stage_position,
                current_design_stage_xy=current_design_stage_xy,
            )
            if self.stage_position is None:
                return ManualJogAdvanceResult(
                    publish_position=None,
                    velocity_xy=(velocities.get("X", 0.0), velocities.get("Y", 0.0)),
                    dt=dt,
                    log_tick=False,
                    stop_timer=False,
                )

        values = [float(value) for value in self.stage_position]
        for axis, velocity in velocities.items():
            try:
                axis_index = self.config.axis_names.index(axis)
            except ValueError:
                continue
            if axis_index >= len(values):
                continue
            values[axis_index] = float(values[axis_index] + velocity * dt)
        self.stage_position = tuple(values)
        self.stage_xy = _stage_xy_from_position(self.stage_position)
        log_tick = current_time - self.last_prediction_log_at >= 0.15
        if log_tick:
            self.last_prediction_log_at = current_time
        should_stop_timer = bool(
            self.stop_prediction_until is not None
            and current_time >= self.stop_prediction_until
            and not self.axis_velocities
        )
        if should_stop_timer:
            self.stop_tail_position = self.stage_position
        return ManualJogAdvanceResult(
            publish_position=self.stage_position,
            velocity_xy=(velocities.get("X", 0.0), velocities.get("Y", 0.0)),
            dt=dt,
            log_tick=log_tick,
            stop_timer=should_stop_timer,
        )

    def seed_position(
        self,
        *,
        coordinate_move_stage_position: object | None = None,
        latest_stage_position: object | None = None,
        current_design_stage_xy: tuple[float, float] | None = None,
    ) -> tuple[float, ...] | None:
        coordinate_move_position = _coerce_position_tuple(coordinate_move_stage_position)
        if coordinate_move_position is not None:
            return coordinate_move_position
        tracked_position = _coerce_position_tuple(self.stage_position)
        if tracked_position is not None:
            return tracked_position
        if self.stage_xy is not None:
            return _position_with_stage_xy(
                self.stage_xy,
                base_position=latest_stage_position,
            )
        latest_position = _coerce_position_tuple(latest_stage_position)
        if latest_position is not None and len(latest_position) >= 2:
            return latest_position
        if current_design_stage_xy is not None:
            return _position_with_stage_xy(
                current_design_stage_xy,
                base_position=latest_stage_position,
            )
        return None

    def learn_stop_tail(
        self,
        predicted_position: object | None,
        actual_position: object | None,
    ) -> ManualJogStopTailLearnResult | None:
        if not self.stop_axis_velocities:
            return None
        if not isinstance(predicted_position, (tuple, list)) or not isinstance(
            actual_position, (tuple, list)
        ):
            return None
        speed_sq = 0.0
        projected_error = 0.0
        for axis, velocity in self.stop_axis_velocities.items():
            try:
                axis_index = self.config.axis_names.index(axis)
            except ValueError:
                continue
            if axis_index >= len(predicted_position) or axis_index >= len(actual_position):
                continue
            try:
                predicted_value = float(predicted_position[axis_index])
                actual_value = float(actual_position[axis_index])
            except (TypeError, ValueError):
                continue
            speed_sq += velocity * velocity
            projected_error += (actual_value - predicted_value) * velocity
        if speed_sq <= 1e-9:
            return None
        residual_s = projected_error / speed_sq
        old_tail_s = float(self.stop_tail_s or 0.0)
        learned_tail_s = min(
            self.config.stop_tail_max_s,
            max(
                self.config.stop_tail_min_s,
                old_tail_s + residual_s,
            ),
        )
        new_tail_s = old_tail_s + (
            learned_tail_s - old_tail_s
        ) * self.config.stop_tail_learn_alpha
        self.stop_tail_s = new_tail_s
        return ManualJogStopTailLearnResult(
            old_tail_s=old_tail_s,
            residual_s=residual_s,
            learned_tail_s=learned_tail_s,
            new_tail_s=new_tail_s,
        )

    def ignore_idle_status_sample(
        self,
        *,
        actual_stage_xy: tuple[float, float],
        now: float,
        latest_state: str,
        last_jog_write_timestamp: float | None,
    ) -> ManualJogIdleSampleResult:
        _ = actual_stage_xy
        state = str(latest_state or "").lower()
        if not self.prediction_active(now):
            return ManualJogIdleSampleResult(ignore=False, age_s=None, state=state)
        if self.waiting_for_fresh_status:
            return ManualJogIdleSampleResult(ignore=False, age_s=None, state=state)
        if state != "idle":
            return ManualJogIdleSampleResult(ignore=False, age_s=None, state=state)
        timestamps = [
            timestamp
            for timestamp in (last_jog_write_timestamp, self.command_started_at)
            if timestamp is not None
        ]
        if not timestamps:
            return ManualJogIdleSampleResult(ignore=False, age_s=None, state=state)
        age = float(now) - max(timestamps)
        if age > self.config.ignore_idle_after_command_s:
            return ManualJogIdleSampleResult(ignore=False, age_s=age, state=state)
        return ManualJogIdleSampleResult(ignore=True, age_s=age, state=state)

    def smooth_actual_stage_xy(
        self,
        predicted_stage_xy: tuple[float, float],
        actual_stage_xy: tuple[float, float],
        *,
        latest_state: str,
    ) -> tuple[float, float]:
        delta_x = float(actual_stage_xy[0] - predicted_stage_xy[0])
        delta_y = float(actual_stage_xy[1] - predicted_stage_xy[1])
        delta_norm = math.hypot(delta_x, delta_y)
        state = str(latest_state or "").lower()
        if (
            delta_norm <= self.config.reconcile_smooth_threshold_mm
            or state not in {"jog", "run"}
        ):
            return actual_stage_xy
        alpha = self.config.reconcile_smooth_alpha
        return (
            float(predicted_stage_xy[0] + delta_x * alpha),
            float(predicted_stage_xy[1] + delta_y * alpha),
        )


def _coerce_position_tuple(position: object | None) -> tuple[float, ...] | None:
    if not isinstance(position, (tuple, list)):
        return None
    try:
        return tuple(float(value) for value in position)
    except (TypeError, ValueError):
        return None


def _stage_xy_from_position(position: object | None) -> tuple[float, float] | None:
    if not isinstance(position, (tuple, list)) or len(position) < 2:
        return None
    try:
        return (float(position[0]), float(position[1]))
    except (TypeError, ValueError):
        return None


def _position_with_stage_xy(
    stage_xy: tuple[float, float],
    *,
    base_position: object | None = None,
) -> tuple[float, ...]:
    position = _coerce_position_tuple(base_position)
    if position is None or len(position) < 2:
        return (float(stage_xy[0]), float(stage_xy[1]))
    values = list(position)
    values[0] = float(stage_xy[0])
    values[1] = float(stage_xy[1])
    return tuple(values)


def _monotonic(now: float | None) -> float:
    if now is None:
        return time.monotonic()
    return float(now)
