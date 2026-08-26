from __future__ import annotations

from dataclasses import dataclass

from probe_station_gui.stage.motion_prediction import (
    coerce_finite_position,
    coerce_finite_xy,
    held_planned_xy,
    matching_planned_xy_start,
    motion_progress,
    planned_xy_matches,
    planned_xy_timing,
    position_with_stage_xy,
)
from probe_station_gui.stage.types import (
    TrackedAbsoluteXYMoveFinished,
    TrackedAbsoluteXYMoveStarted,
)


@dataclass(frozen=True)
class PlannedXYRequestDecision:
    target_stage_xy: tuple[float, float]
    source_label: str
    motion_token: int


@dataclass(frozen=True)
class PlannedXYStartDecision:
    handled: bool
    clear_workflow: bool
    publish_position: tuple[float, ...] | None
    stage_xy: tuple[float, float] | None
    start_timer: bool


@dataclass(frozen=True)
class PlannedXYFinishDecision:
    handled: bool
    clear_workflow: bool
    stage_xy: tuple[float, float] | None
    stop_timer: bool


@dataclass(frozen=True)
class PlannedXYAdvanceDecision:
    publish_stage_xy: tuple[float, float] | None
    stop_timer: bool


@dataclass
class PlannedXYMotionState:
    pending_target_xy: tuple[float, float] | None = None
    pending_source_label: str | None = None
    origin_xy: tuple[float, float] | None = None
    stage_xy: tuple[float, float] | None = None
    target_xy: tuple[float, float] | None = None
    started_at: float | None = None
    ends_at: float | None = None
    waiting_for_fresh_status: bool = False
    stop_status_timestamp: float | None = None
    next_motion_token: int = 1
    motion_token: object | None = None
    accepted_target_xy: tuple[float, float] | None = None

    def request_in_flight(self) -> bool:
        return bool(
            self.pending_target_xy is not None
            or self.started_at is not None
            or self.waiting_for_fresh_status
            or self.motion_token is not None
        )

    def prepare_request(
        self,
        target_stage_xy: object,
        source_label: str,
    ) -> PlannedXYRequestDecision | None:
        if self.request_in_flight():
            return None
        target = coerce_finite_xy(target_stage_xy)
        if target is None:
            return None
        label = str(source_label).strip() or "absolute XY move"
        token = self.next_motion_token
        self.next_motion_token += 1
        self.pending_target_xy = target
        self.pending_source_label = label
        return PlannedXYRequestDecision(target, label, token)

    def accept_request(self, decision: PlannedXYRequestDecision) -> None:
        self.accepted_target_xy = decision.target_stage_xy
        self.motion_token = decision.motion_token

    def owns_motion(self, motion_token: object) -> bool:
        return self.motion_token is not None and motion_token == self.motion_token

    def start(
        self,
        started: TrackedAbsoluteXYMoveStarted,
        *,
        tolerance_mm: float,
        min_feedrate_mm_min: float,
        duration_padding_s: float,
        monotonic_s: float,
    ) -> PlannedXYStartDecision:
        if not self.owns_motion(started.motion_token) or self.pending_target_xy is None:
            return PlannedXYStartDecision(False, False, None, None, False)
        match = matching_planned_xy_start(
            started.target_stage_xy[0],
            started.target_stage_xy[1],
            started.feedrate_mm_min,
            pending_target=self.pending_target_xy,
            tolerance_mm=tolerance_mm,
        )
        if match is None:
            return PlannedXYStartDecision(True, True, None, None, False)
        target, feedrate = match
        origin_position = coerce_finite_position(started.origin_position)
        origin = coerce_finite_xy(origin_position)
        if origin_position is None or origin is None:
            return PlannedXYStartDecision(True, True, None, None, False)
        timing = planned_xy_timing(
            origin,
            target,
            feedrate,
            min_feedrate_mm_min=min_feedrate_mm_min,
            duration_padding_s=duration_padding_s,
            started_at=monotonic_s,
        )
        if timing is None:
            return PlannedXYStartDecision(True, True, None, None, False)
        self.pending_target_xy = None
        self.pending_source_label = None
        self.origin_xy = origin
        self.stage_xy = origin
        self.target_xy = target
        self.started_at, self.ends_at = timing
        self.waiting_for_fresh_status = False
        self.stop_status_timestamp = None
        return PlannedXYStartDecision(
            True,
            False,
            position_with_stage_xy(origin_position, origin),
            origin,
            True,
        )

    def finish(
        self,
        finished: TrackedAbsoluteXYMoveFinished,
        *,
        tolerance_mm: float,
    ) -> PlannedXYFinishDecision:
        if not self.owns_motion(finished.motion_token):
            return PlannedXYFinishDecision(False, False, None, False)
        accepted_target = self.accepted_target_xy
        if accepted_target is None or not planned_xy_matches(
            finished.target_stage_xy,
            accepted_target,
            tolerance_mm=tolerance_mm,
        ):
            return PlannedXYFinishDecision(False, False, None, False)
        if not finished.success:
            return PlannedXYFinishDecision(True, True, None, True)
        self.stage_xy = accepted_target
        self.pending_target_xy = None
        self.pending_source_label = None
        self.origin_xy = None
        self.target_xy = None
        self.started_at = None
        self.ends_at = None
        self.waiting_for_fresh_status = True
        self.stop_status_timestamp = finished.status_timestamp
        self.motion_token = None
        self.accepted_target_xy = None
        return PlannedXYFinishDecision(True, False, accepted_target, True)

    def advance(self, monotonic_s: float) -> PlannedXYAdvanceDecision:
        if (
            self.origin_xy is None
            or self.target_xy is None
            or self.started_at is None
            or self.ends_at is None
        ):
            return PlannedXYAdvanceDecision(None, True)
        progress = motion_progress(self.started_at, self.ends_at, monotonic_s)
        origin_x, origin_y = self.origin_xy
        target_x, target_y = self.target_xy
        self.stage_xy = (
            float(origin_x + (target_x - origin_x) * progress),
            float(origin_y + (target_y - origin_y) * progress),
        )
        if progress >= 1.0:
            self.waiting_for_fresh_status = True
            self.stop_status_timestamp = None
            self.origin_xy = None
            self.target_xy = None
            self.started_at = None
            self.ends_at = None
        return PlannedXYAdvanceDecision(self.stage_xy, progress >= 1.0)

    def resolve_reported_stage_xy(
        self,
        raw_stage_xy: tuple[float, float],
        *,
        status_timestamp: float | None,
    ) -> tuple[float, float]:
        held = held_planned_xy(
            self.stage_xy,
            prediction_active=self.started_at is not None,
            waiting_for_fresh_status=self.waiting_for_fresh_status,
            status_timestamp=status_timestamp,
            stop_status_timestamp=self.stop_status_timestamp,
        )
        if held is not None:
            return held
        self.waiting_for_fresh_status = False
        self.stop_status_timestamp = None
        self.stage_xy = raw_stage_xy
        return raw_stage_xy

    def clear(self, *, clear_wait_state: bool) -> None:
        self.pending_target_xy = None
        self.pending_source_label = None
        self.origin_xy = None
        self.target_xy = None
        self.started_at = None
        self.ends_at = None
        self.motion_token = None
        self.accepted_target_xy = None
        if clear_wait_state:
            self.stage_xy = None
            self.waiting_for_fresh_status = False
            self.stop_status_timestamp = None
