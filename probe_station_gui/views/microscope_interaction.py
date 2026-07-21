"""Microscope pointer and click-move interaction state."""

from __future__ import annotations

import math
from time import monotonic, perf_counter

from PySide6.QtCore import QObject, QPointF, QRect, Qt, QTimer
from PySide6.QtGui import QPainter

from probe_station_gui.stage.motion_prediction import interpolate_position
from probe_station_gui.views.microscope_overlay_rendering import (
    AlignmentOverlay,
    MeasurementOverlay,
    TargetOverlay,
    draw_alignment_overlay,
    draw_measurement_overlay,
    draw_target_overlay,
)
from probe_station_gui.views.microscope_interaction_types import (
    ClickCoordinates,
    ClickMoveBindings,
    ClickMoveConfig,
    ImageGeometry,
    PendingClickMove,
    PointerDispatch,
)


class MicroscopeInteraction(QObject):
    def __init__(
        self,
        bindings: ClickMoveBindings,
        config: ClickMoveConfig,
    ) -> None:
        super().__init__()
        self._bindings = bindings
        self._config = config
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._retry_scheduled)
        self._retry_generation = 0
        self._scheduled_retry_generation = 0
        self.pending_move: PendingClickMove | None = None
        self.target_rel: tuple[float, float] | None = None
        self._release_target: ClickCoordinates | None = None
        self._release_cancelled = False
        self.hover: ClickCoordinates | None = None
        self._measure_mode: str | None = None
        self._measure_points: list[tuple[float, float]] = []
        self._measure_hover: tuple[float, float] | None = None
        self._ruler_segments: list[
            tuple[tuple[float, float], tuple[float, float]]
        ] = []
        self._rect_segments: list[
            tuple[tuple[float, float], tuple[float, float]]
        ] = []
        self._alignment_mode = False
        self._alignment_points: list[tuple[float, float]] = []
        self._alignment_instruction = ""
        self._target_pending = False
        self._target_blink_dimmed = False
        self._target_blink_timer = QTimer(self)
        self._target_blink_timer.setInterval(max(1, config.target_blink_ms))
        self._target_blink_timer.timeout.connect(self._advance_target_blink)
        self._target_motion_origin_rel: tuple[float, float] | None = None
        self._target_motion_started_at: float | None = None
        self._target_motion_ends_at: float | None = None
        self._target_motion_timer = QTimer(self)
        self._target_motion_timer.setInterval(max(1, config.target_motion_update_ms))
        self._target_motion_timer.timeout.connect(self._advance_target_motion)

    @property
    def measurement_hover(self) -> tuple[float, float] | None:
        return self._measure_hover

    @property
    def target_motion_duration_s(self) -> float | None:
        if self._target_motion_started_at is None or self._target_motion_ends_at is None:
            return None
        return self._target_motion_ends_at - self._target_motion_started_at

    @property
    def has_pending_move(self) -> bool:
        return self.pending_move is not None

    def set_measure_mode(self, mode: str | None) -> None:
        self._measure_mode = mode
        self._measure_points.clear()
        self._measure_hover = None
        self._ruler_segments.clear()
        self._rect_segments.clear()
        self._bindings.repaint()

    def draw(
        self,
        painter: QPainter,
        display_rect: QRect,
        scale_x: float,
        scale_y: float,
        *,
        canvas_width: int,
        mm_per_pixel_x: float | None,
        mm_per_pixel_y: float | None,
    ) -> None:
        draw_target_overlay(
            painter,
            display_rect,
            TargetOverlay(
                self.target_rel,
                self._target_pending,
                self._target_blink_dimmed,
            ),
        )
        draw_alignment_overlay(
            painter,
            display_rect,
            AlignmentOverlay(
                self._alignment_mode,
                tuple(self._alignment_points),
                self._alignment_instruction,
            ),
            canvas_width=canvas_width,
        )
        draw_measurement_overlay(
            painter,
            display_rect,
            scale_x,
            scale_y,
            MeasurementOverlay(
                self._measure_mode,
                tuple(self._measure_points),
                self._measure_hover,
                tuple(self._ruler_segments),
                tuple(self._rect_segments),
                mm_per_pixel_x,
                mm_per_pixel_y,
            ),
        )

    def set_target_pending_blink_interval(self, interval_ms: int) -> None:
        self._target_blink_timer.setInterval(max(1, int(interval_ms)))

    def set_target_motion_update_interval(self, interval_ms: int) -> None:
        self._target_motion_timer.setInterval(max(1, int(interval_ms)))

    def set_alignment_mode(self, enabled: bool) -> None:
        self._alignment_mode = bool(enabled)
        if not enabled:
            self._alignment_points.clear()
            self._alignment_instruction = ""
        self._bindings.repaint()

    def set_alignment_points(self, points: list[tuple[float, float]]) -> None:
        self._alignment_points = list(points[:2])
        self._bindings.repaint()

    def set_alignment_instruction(self, instruction: str) -> None:
        self._alignment_instruction = str(instruction)
        self._bindings.repaint()

    def clear_alignment_points(self) -> None:
        self._alignment_points.clear()
        self._bindings.repaint()

    def press(self, position: QPointF, geometry: ImageGeometry) -> PointerDispatch:
        click = self._map_position(position, geometry)
        if click is None:
            return PointerDispatch()
        if self._measure_mode is not None:
            self._record_measurement_point(click)
            return PointerDispatch(accepted=True)
        if self._alignment_mode:
            return PointerDispatch(click=click, accepted=True)
        self._arm_release(click)
        return PointerDispatch(accepted=True)

    def release(self, position: QPointF, geometry: ImageGeometry) -> PointerDispatch:
        if self._release_cancelled:
            self._release_cancelled = False
            return PointerDispatch(accepted=True)
        if self._release_target is None:
            return PointerDispatch()
        click = self._map_position(position, geometry)
        if click is None:
            self._cancel_release(suppress_until_release=False)
            return PointerDispatch(accepted=True)
        self._release_target = None
        self._stop_target_motion()
        self._set_target_pending(False)
        self.target_rel = (click[2], click[3])
        self._bindings.repaint()
        return PointerDispatch(click=click, accepted=True)

    def move(
        self,
        position: QPointF,
        geometry: ImageGeometry,
        *,
        left_button: bool,
        modifiers: Qt.KeyboardModifier,
    ) -> PointerDispatch:
        click = self._map_position(position, geometry)
        if click is None:
            if left_button:
                self._cancel_release(suppress_until_release=True)
            self.hover = None
            self._clear_hover_presentation()
            self._clear_measurement_hover()
            return PointerDispatch(hover_left=True)
        if self._measure_mode is not None:
            self._update_measurement_hover(click, modifiers)
            return PointerDispatch()
        if left_button and self._release_target is not None:
            self._release_target = click
            self.target_rel = (click[2], click[3])
            self._bindings.repaint()
        self.hover = click
        self._present_hover(click)
        return PointerDispatch(hover=click)

    def leave(self) -> PointerDispatch:
        if self._release_target is not None:
            self._cancel_release(suppress_until_release=True)
        self.hover = None
        self._clear_hover_presentation()
        return PointerDispatch(hover_left=True)

    def _present_hover(self, click: ClickCoordinates) -> None:
        preview = self._bindings.preview_hover(click[0], click[1])
        center_xy, cursor_xy = preview if preview is not None else (None, None)
        self._bindings.present_coordinates(
            center_xy=center_xy,
            cursor_xy=cursor_xy,
        )

    def _clear_hover_presentation(self) -> None:
        self._bindings.present_coordinates(center_xy=None, cursor_xy=None)

    def try_start_move(
        self,
        dx: float,
        dy: float,
        rel_x: float,
        rel_y: float,
    ) -> None:
        self.target_rel = (rel_x, rel_y)
        self._stop_target_motion()
        self._set_target_pending(False)
        self._bindings.repaint()
        if self._request_if_ready(dx, dy):
            self.cancel_pending(clear_target=False)
            return
        timeout_s = self._pending_timeout_s()
        was_pending = self.pending_move is not None
        self.pending_move = PendingClickMove(
            dx=float(dx),
            dy=float(dy),
            rel_x=float(rel_x),
            rel_y=float(rel_y),
            deadline_s=monotonic() + timeout_s,
        )
        self._set_target_pending(True)
        self._schedule_retry()
        self._notify_pending_transition(was_pending)
        self._bindings.show_status(
            (
                "Stage is busy; click-to-move will start when it is ready."
                if self._bindings.stage_connected()
                else "Stage is not connected; click-to-move will wait for it."
            ),
            3000,
        )

    def handle_click(self, dx: float, dy: float, rel_x: float, rel_y: float) -> None:
        if self._bindings.manual_alignment_active():
            self._bindings.capture_manual_alignment(dx, dy)
            return
        self.try_start_move(dx, dy, rel_x, rel_y)

    def retry_pending(self) -> None:
        pending = self.pending_move
        if pending is None:
            return
        if monotonic() >= pending.deadline_s:
            self.cancel_pending(clear_target=True)
            self._bindings.show_status(
                "Click-to-move timed out waiting for the stage.", 5000
            )
            return
        if self._request_if_ready(pending.dx, pending.dy):
            self.cancel_pending(clear_target=False)
            return
        self._schedule_retry()

    def cancel_pending(self, *, clear_target: bool) -> None:
        was_pending = self.pending_move is not None
        self.pending_move = None
        self._retry_generation += 1
        self._retry_timer.stop()
        if clear_target:
            self.clear_target()
        else:
            self._set_target_pending(False)
        self._bindings.repaint()
        self._notify_pending_transition(was_pending)

    def try_start_api_move(self, dx: float, dy: float) -> bool:
        return self._request_if_ready(dx, dy)

    def start_target_motion(
        self,
        move_x_mm: float,
        move_y_mm: float,
        feedrate_mm_min: float,
    ) -> None:
        try:
            distance_mm = math.hypot(float(move_x_mm), float(move_y_mm))
            feedrate = max(
                self._config.min_feedrate_mm_min,
                float(feedrate_mm_min),
            )
        except (TypeError, ValueError):
            return
        if distance_mm <= 1e-9:
            self.finish_target_motion_to_center()
            return
        duration_s = distance_mm / feedrate * 60.0
        duration_s += self._config.target_animation_padding_s
        self._animate_target_to_center(
            max(duration_s, self._config.target_animation_min_s)
        )

    def finish_move(self, *, success: bool) -> None:
        if self.pending_move is not None:
            return
        if success:
            self.finish_target_motion_to_center()
        self.clear_target()

    def finish_target_motion_to_center(self) -> None:
        if self.target_rel is not None:
            self.target_rel = (0.5, 0.5)
        self._stop_target_motion()
        self._bindings.repaint()

    def clear_target(self) -> None:
        self._release_target = None
        self._release_cancelled = False
        self.target_rel = None
        self._stop_target_motion()
        self._set_target_pending(False)
        self._bindings.repaint()

    def shutdown(self) -> None:
        was_pending = self.pending_move is not None
        self.pending_move = None
        self._retry_generation += 1
        self._retry_timer.stop()
        self._target_blink_timer.stop()
        self._target_motion_timer.stop()
        self._notify_pending_transition(was_pending)

    def _notify_pending_transition(self, was_pending: bool) -> None:
        is_pending = self.pending_move is not None
        if is_pending != was_pending:
            self._bindings.pending_state_changed(is_pending)

    def _map_position(
        self,
        position: QPointF,
        geometry: ImageGeometry,
    ) -> ClickCoordinates | None:
        display_rect = geometry.display_rect
        image_size = geometry.image_size
        if not display_rect.contains(position.toPoint()):
            return None
        if min(display_rect.width(), display_rect.height()) <= 0:
            return None
        if min(image_size.width(), image_size.height()) <= 0:
            return None
        scale_x = display_rect.width() / image_size.width()
        scale_y = display_rect.height() / image_size.height()
        image_x = (position.x() - display_rect.left()) / scale_x
        image_y = (position.y() - display_rect.top()) / scale_y
        dx = image_x - image_size.width() / 2.0
        dy = image_size.height() / 2.0 - image_y
        rel_x = (position.x() - display_rect.left()) / display_rect.width()
        rel_y = (position.y() - display_rect.top()) / display_rect.height()
        return dx, dy, _clamp_unit(rel_x), _clamp_unit(rel_y)

    def _record_measurement_point(self, click: ClickCoordinates) -> None:
        point = (click[0], click[1])
        if not self._measure_points:
            self._measure_points = [point]
        else:
            segment = (self._measure_points[0], point)
            if self._measure_mode == "ruler":
                self._ruler_segments.append(segment)
            else:
                self._rect_segments.append(segment)
            self._measure_points.clear()
        self._measure_hover = None
        self._bindings.repaint()

    def _update_measurement_hover(
        self,
        click: ClickCoordinates,
        modifiers: Qt.KeyboardModifier,
    ) -> None:
        dx, dy = click[0], click[1]
        if (
            self._measure_mode == "ruler"
            and modifiers & Qt.ControlModifier
            and len(self._measure_points) == 1
        ):
            anchor_dx, anchor_dy = self._measure_points[0]
            if abs(dx - anchor_dx) >= abs(dy - anchor_dy):
                dy = anchor_dy
            else:
                dx = anchor_dx
        self._measure_hover = (dx, dy)
        self._bindings.repaint()

    def _clear_measurement_hover(self) -> None:
        if self._measure_mode is None or self._measure_hover is None:
            return
        self._measure_hover = None
        self._bindings.repaint()

    def _arm_release(self, click: ClickCoordinates) -> None:
        self._release_target = click
        self._release_cancelled = False
        self._stop_target_motion()
        self._set_target_pending(False)
        self.target_rel = (click[2], click[3])
        self._bindings.repaint()

    def _cancel_release(self, *, suppress_until_release: bool) -> None:
        if self._release_target is None and not self._release_cancelled:
            return
        self._release_target = None
        self.clear_target()
        self._release_cancelled = bool(suppress_until_release)

    def _set_target_pending(self, pending: bool) -> None:
        pending = bool(pending and self.target_rel is not None)
        if self._target_pending == pending:
            return
        self._target_pending = pending
        if pending:
            self._target_blink_dimmed = False
            self._target_blink_timer.start()
        else:
            self._target_blink_timer.stop()
            self._target_blink_dimmed = False
        self._bindings.repaint()

    def _animate_target_to_center(self, duration_s: float) -> None:
        if self.target_rel is None:
            return
        self._set_target_pending(False)
        origin = self.target_rel
        if duration_s <= 0.0 or _points_close(origin, (0.5, 0.5)):
            self.finish_target_motion_to_center()
            return
        started_at = perf_counter()
        self._target_motion_origin_rel = origin
        self._target_motion_started_at = started_at
        self._target_motion_ends_at = started_at + max(1e-3, float(duration_s))
        self._target_motion_timer.start()
        self._advance_target_motion()

    def _advance_target_motion(self) -> None:
        origin = self._target_motion_origin_rel
        started_at = self._target_motion_started_at
        ends_at = self._target_motion_ends_at
        if origin is None or started_at is None or ends_at is None:
            self._stop_target_motion()
            return
        now = perf_counter()
        position = interpolate_position(origin, (0.5, 0.5), started_at, ends_at, now)
        self.target_rel = (float(position[0]), float(position[1]))
        if now >= ends_at:
            self.target_rel = (0.5, 0.5)
            self._stop_target_motion()
        self._bindings.repaint()

    def _stop_target_motion(self) -> None:
        self._target_motion_timer.stop()
        self._target_motion_origin_rel = None
        self._target_motion_started_at = None
        self._target_motion_ends_at = None

    def _advance_target_blink(self) -> None:
        if not self._target_pending or self.target_rel is None:
            self._set_target_pending(False)
            return
        self._target_blink_dimmed = not self._target_blink_dimmed
        self._bindings.repaint()

    def _request_if_ready(self, dx: float, dy: float) -> bool:
        if not self._bindings.stage_connected() or self._bindings.motion_blocked():
            return False
        if not self._bindings.request_move(float(dx), float(dy)):
            return False
        self._bindings.mark_motion_axes({"X", "Y"})
        return True

    def _pending_timeout_s(self) -> float:
        try:
            timeout_s = float(self._config.pending_timeout_s())
        except (TypeError, ValueError):
            timeout_s = self._config.default_pending_timeout_s
        if not math.isfinite(timeout_s):
            timeout_s = self._config.default_pending_timeout_s
        return min(
            self._config.max_pending_timeout_s,
            max(self._config.min_pending_timeout_s, timeout_s),
        )

    def _schedule_retry(self) -> None:
        self._retry_generation += 1
        generation = self._retry_generation
        self._retry_timer.stop()
        self._retry_timer.setInterval(max(1, int(self._config.pending_retry_ms)))
        self._scheduled_retry_generation = generation
        self._retry_timer.start()

    def _retry_scheduled(self) -> None:
        self._retry_if_current(self._scheduled_retry_generation)

    def _retry_if_current(self, generation: int) -> None:
        if generation != self._retry_generation:
            return
        self.retry_pending()


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _points_close(
    first: tuple[float, float],
    second: tuple[float, float],
) -> bool:
    return abs(first[0] - second[0]) < 1e-6 and abs(first[1] - second[1]) < 1e-6


__all__ = [
    "ClickMoveBindings",
    "ClickMoveConfig",
    "ImageGeometry",
    "MicroscopeInteraction",
    "PendingClickMove",
    "PointerDispatch",
]
