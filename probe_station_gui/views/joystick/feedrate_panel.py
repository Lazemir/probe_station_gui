"""Feedrate controls for the joystick window."""

from __future__ import annotations

import logging
from typing import List

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QStyle, QStyleOptionSlider, QWidget

from probe_station_gui.shared.wheel_guard import GuardedSlider as QSlider
from probe_station_gui.views.joystick.feedrate_targets import (
    bounded_feedrate_setting,
    clean_axis_feedrate_limits,
    feedrate_from_slider_value,
    feedrate_key,
    feedrate_limit_known_for_target,
    feedrate_max_for_target,
    feedrate_target_for_axis,
    linear_feedrate_min_max,
    slider_value_from_feedrate,
)


logger = logging.getLogger("probe_station_gui.views.joystick_window")


class _FeedrateSlider(QSlider):
    """Slider with visual overlay for temporarily unavailable feedrate ranges."""

    def __init__(self, orientation: Qt.Orientation, parent: QWidget | None = None) -> None:
        super().__init__(orientation, parent)
        self._temporary_bounds: tuple[int, int] | None = None

    def set_temporary_bounds(self, minimum: int | None, maximum: int | None) -> None:
        if minimum is None or maximum is None:
            bounds = None
        else:
            bounds = (int(minimum), int(maximum))
        if bounds == self._temporary_bounds:
            return
        self._temporary_bounds = bounds
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        super().paintEvent(event)
        if self._temporary_bounds is None or self.orientation() != Qt.Horizontal:
            return
        lower, upper = self._temporary_bounds
        slider_min = self.minimum()
        slider_max = self.maximum()
        if slider_max <= slider_min:
            return
        lower = max(slider_min, min(slider_max, lower))
        upper = max(slider_min, min(slider_max, upper))
        if lower <= slider_min and upper >= slider_max:
            return

        option = QStyleOptionSlider()
        self.initStyleOption(option)
        groove = self.style().subControlRect(
            QStyle.CC_Slider,
            option,
            QStyle.SC_SliderGroove,
            self,
        )
        handle = self.style().subControlRect(
            QStyle.CC_Slider,
            option,
            QStyle.SC_SliderHandle,
            self,
        )
        usable_left = groove.left() + handle.width() // 2
        usable_right = groove.right() - handle.width() // 2
        usable_width = max(1, usable_right - usable_left)
        lower_x = usable_left + QStyle.sliderPositionFromValue(
            slider_min,
            slider_max,
            lower,
            usable_width,
            option.upsideDown,
        )
        upper_x = usable_left + QStyle.sliderPositionFromValue(
            slider_min,
            slider_max,
            upper,
            usable_width,
            option.upsideDown,
        )
        if lower_x > upper_x:
            lower_x, upper_x = upper_x, lower_x

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        color = QColor("#9e9e9e")
        color.setAlpha(150)
        painter.setPen(Qt.NoPen)
        painter.setBrush(color)
        overlay_rect = groove.adjusted(0, -2, 0, 2)
        if lower > slider_min:
            painter.drawRect(
                overlay_rect.left(),
                overlay_rect.top(),
                max(0, lower_x - overlay_rect.left()),
                overlay_rect.height(),
            )
        if upper < slider_max:
            painter.drawRect(
                upper_x,
                overlay_rect.top(),
                max(0, overlay_rect.right() - upper_x + 1),
                overlay_rect.height(),
            )




class JoystickFeedrateMixin:
    def _feedrate_key(self, target: str, mode: str | None = None) -> str:
        return feedrate_key(
            target,
            mode or self._control_mode,
            target_labels=self.FEED_TARGET_LABELS,
            default_target=self.FEED_TARGET_XY,
            valid_modes={self.MODE_JOG, self.MODE_STEP},
            default_mode=self.MODE_JOG,
        )

    def _feedrate_value_for_target(self, target: str, mode: str | None = None) -> float:
        mode_key = str(mode or self._control_mode).strip().lower()
        if mode_key not in {self.MODE_JOG, self.MODE_STEP}:
            mode_key = self.MODE_JOG
        key = self._feedrate_key(target, mode_key)
        if key in self._feedrate_values:
            return self._feedrate_values[key]
        if target == self.FEED_TARGET_COMMON:
            return self._common_feedrate_value
        storage = self.FEEDRATE_VALUE_STORAGE.get((target, mode_key))
        if storage is None:
            return self._linear_default
        field_name, _signal_name = storage
        return getattr(self, field_name)

    def _feedrate_target_for_axis(self, axis: str) -> str:
        return feedrate_target_for_axis(
            axis,
            xy_target=self.FEED_TARGET_XY,
            focus_target=self.FEED_TARGET_FOCUS,
            needles_target=self.FEED_TARGET_NEEDLES,
            turntable_target=self.FEED_TARGET_TURNTABLE,
        )

    def _feedrate_max_for_target(self, target: str) -> float:
        return feedrate_max_for_target(
            target,
            axis_limits=self._axis_feedrate_limits,
            common_feedrate_max=getattr(self, "_common_feedrate_max", None),
            linear_feedrate_value=self._linear_feedrate_value,
            linear_default=self._linear_default,
            linear_presets=self._linear_presets,
            min_linear_feedrate=self.MIN_LINEAR_FEEDRATE,
            max_linear_feedrate=self.MAX_LINEAR_FEEDRATE,
            xy_target=self.FEED_TARGET_XY,
            focus_target=self.FEED_TARGET_FOCUS,
            needles_target=self.FEED_TARGET_NEEDLES,
            turntable_target=self.FEED_TARGET_TURNTABLE,
            common_target=self.FEED_TARGET_COMMON,
        )

    def _feedrate_limit_known_for_target(self, target: str) -> bool:
        return feedrate_limit_known_for_target(
            target,
            axis_limits=self._axis_feedrate_limits,
            common_feedrate_max=getattr(self, "_common_feedrate_max", None),
            xy_target=self.FEED_TARGET_XY,
            focus_target=self.FEED_TARGET_FOCUS,
            needles_target=self.FEED_TARGET_NEEDLES,
            turntable_target=self.FEED_TARGET_TURNTABLE,
            common_target=self.FEED_TARGET_COMMON,
        )

    def _bounded_feedrate_setting(self, target: str, value: float) -> float:
        limit_known = self._feedrate_limit_known_for_target(target)
        return bounded_feedrate_setting(
            target,
            value,
            limit_known=limit_known,
            target_max=self._feedrate_max_for_target(target) if limit_known else 0.0,
            min_linear_feedrate=self.MIN_LINEAR_FEEDRATE,
        )

    def _linear_feedrate_min_max(self) -> tuple[float, float]:
        return linear_feedrate_min_max(
            target_max=self._feedrate_max_for_target(self._active_feedrate_target),
            bounds=self._linear_feedrate_bounds,
            min_linear_feedrate=self.MIN_LINEAR_FEEDRATE,
        )

    def _update_linear_feedrate_slider_range(self) -> None:
        minimum = int(round(self.MIN_LINEAR_FEEDRATE * self.LINEAR_FEEDRATE_SCALE))
        _min_value, max_value = self._linear_feedrate_min_max()
        maximum = int(round(max_value * self.LINEAR_FEEDRATE_SCALE))
        self.linear_feedrate_slider.blockSignals(True)
        if self.linear_feedrate_slider.minimum() != minimum or self.linear_feedrate_slider.maximum() != maximum:
            self.linear_feedrate_slider.setRange(minimum, maximum)
        self.linear_feedrate_slider.blockSignals(False)
        if hasattr(self, "linear_feedrate_spin"):
            self.linear_feedrate_spin.blockSignals(True)
            self.linear_feedrate_spin.setRange(self.MIN_LINEAR_FEEDRATE, max_value)
            self.linear_feedrate_spin.blockSignals(False)
        self._update_needle_feedrate_spin_range()
        if isinstance(self.linear_feedrate_slider, _FeedrateSlider):
            if self._linear_feedrate_bounds is None:
                self.linear_feedrate_slider.set_temporary_bounds(None, None)
            else:
                min_value, max_value = self._linear_feedrate_min_max()
                self.linear_feedrate_slider.set_temporary_bounds(
                    int(round(min_value * self.LINEAR_FEEDRATE_SCALE)),
                    int(round(max_value * self.LINEAR_FEEDRATE_SCALE)),
                )

    def _update_needle_feedrate_spin_range(self) -> None:
        if not hasattr(self, "needle_feedrate_spin"):
            return
        needle_max = max(
            self.MIN_LINEAR_FEEDRATE,
            float(self._feedrate_max_for_target(self.FEED_TARGET_NEEDLES)),
        )
        self.needle_feedrate_spin.blockSignals(True)
        self.needle_feedrate_spin.setRange(self.MIN_LINEAR_FEEDRATE, needle_max)
        self.needle_feedrate_spin.blockSignals(False)

    def _slider_value_from_feedrate(self, value: float) -> int:
        min_value, max_value = self._linear_feedrate_min_max()
        return slider_value_from_feedrate(
            value,
            min_value=min_value,
            max_value=max_value,
            scale=self.LINEAR_FEEDRATE_SCALE,
        )

    def _feedrate_from_slider_value(self, slider_value: int) -> float:
        return feedrate_from_slider_value(
            slider_value,
            scale=self.LINEAR_FEEDRATE_SCALE,
        )

    def _set_linear_feedrate(
        self,
        value: float,
        *,
        reissue_if_active: bool,
    ) -> None:
        min_value, max_value = self._linear_feedrate_min_max()
        bounded = min(max_value, max(min_value, float(value)))
        bounded = self._feedrate_from_slider_value(
            self._slider_value_from_feedrate(bounded)
        )
        target = self._active_feedrate_target
        mode = self._control_mode
        key = self._feedrate_key(target, mode)
        previous = self._feedrate_values.get(key, self._linear_feedrate_value)
        changed = abs(bounded - previous) > 1e-9
        self._feedrate_values[key] = bounded
        self._linear_feedrate_value = bounded
        slider_value = self._slider_value_from_feedrate(bounded)
        if self.linear_feedrate_slider.value() != slider_value:
            self.linear_feedrate_slider.blockSignals(True)
            self.linear_feedrate_slider.setValue(slider_value)
            self.linear_feedrate_slider.blockSignals(False)
        if hasattr(self, "linear_feedrate_spin"):
            self.linear_feedrate_spin.blockSignals(True)
            self.linear_feedrate_spin.setValue(bounded)
            self.linear_feedrate_spin.blockSignals(False)
        if hasattr(self, "linear_feedrate_target_label"):
            self.linear_feedrate_target_label.setText(
                self.FEED_TARGET_LABELS.get(target, "Feed")
            )
        if changed:
            self._store_feedrate_value(target, mode, bounded, emit_changed=True)
        if reissue_if_active and self._active_axes:
            self._restart_active_jog_with_current_feedrate()

    def _store_feedrate_value(
        self, target: str, mode: str, value: float, *, emit_changed: bool
    ) -> None:
        mode_key = self.MODE_STEP if mode == self.MODE_STEP else self.MODE_JOG
        if target == self.FEED_TARGET_COMMON:
            mode_key = self.MODE_JOG
        storage = self.FEEDRATE_VALUE_STORAGE.get((target, mode_key))
        if storage is None:
            return
        field_name, signal_name = storage
        setattr(self, field_name, value)

        spin_name = self.FEEDRATE_SPIN_STORAGE.get((target, mode_key))
        spin = getattr(self, spin_name, None) if spin_name is not None else None
        if spin is not None:
            spin.blockSignals(True)
            spin.setValue(value)
            spin.blockSignals(False)
        if emit_changed:
            getattr(self, signal_name).emit(value)

    def _set_active_feedrate_target(self, target: str) -> None:
        if target not in self.FEED_TARGET_LABELS:
            target = self.FEED_TARGET_XY
        self._active_feedrate_target = target
        if hasattr(self, "feedrate_target_combo"):
            index = self.feedrate_target_combo.findData(target)
            if index >= 0 and self.feedrate_target_combo.currentIndex() != index:
                self.feedrate_target_combo.blockSignals(True)
                self.feedrate_target_combo.setCurrentIndex(index)
                self.feedrate_target_combo.blockSignals(False)
        self._update_linear_feedrate_slider_range()
        self._set_linear_feedrate(
            self._feedrate_value_for_target(target),
            reissue_if_active=False,
        )

    def set_common_feedrate_target(
        self,
        feedrate: float,
        max_feedrate: float,
    ) -> None:
        try:
            feedrate_value = max(self.MIN_LINEAR_FEEDRATE, float(feedrate))
        except (TypeError, ValueError):
            feedrate_value = self.MIN_LINEAR_FEEDRATE
        try:
            max_value = max(self.MIN_LINEAR_FEEDRATE, float(max_feedrate))
        except (TypeError, ValueError):
            max_value = feedrate_value
        self._common_feedrate_max = max(max_value, feedrate_value)
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_COMMON, self.MODE_JOG)
        ] = min(self._common_feedrate_max, feedrate_value)
        self._ensure_common_feedrate_combo_item()
        self._set_active_feedrate_target(self.FEED_TARGET_COMMON)

    def clear_common_feedrate_target(self) -> None:
        if self._active_feedrate_target == self.FEED_TARGET_COMMON:
            self._set_active_feedrate_target(self.FEED_TARGET_XY)
        self._common_feedrate_max = None
        self._feedrate_values.pop(
            self._feedrate_key(self.FEED_TARGET_COMMON, self.MODE_JOG),
            None,
        )
        self._remove_common_feedrate_combo_item()
        self._update_linear_feedrate_slider_range()

    def _ensure_common_feedrate_combo_item(self) -> None:
        if not hasattr(self, "feedrate_target_combo"):
            return
        if self.feedrate_target_combo.findData(self.FEED_TARGET_COMMON) >= 0:
            return
        self.feedrate_target_combo.addItem(
            self.FEED_TARGET_LABELS[self.FEED_TARGET_COMMON],
            self.FEED_TARGET_COMMON,
        )

    def _remove_common_feedrate_combo_item(self) -> None:
        if not hasattr(self, "feedrate_target_combo"):
            return
        index = self.feedrate_target_combo.findData(self.FEED_TARGET_COMMON)
        if index >= 0:
            self.feedrate_target_combo.removeItem(index)

    def set_temporary_linear_feedrate_bounds(
        self, min_feedrate: float, max_feedrate: float
    ) -> None:
        min_value = max(self.MIN_LINEAR_FEEDRATE, float(min_feedrate))
        max_value = max(min_value, float(max_feedrate))
        self._linear_feedrate_bounds = (min_value, max_value)
        self._update_linear_feedrate_slider_range()
        self._set_linear_feedrate(
            self._linear_feedrate_value,
            reissue_if_active=False,
        )

    def clear_temporary_linear_feedrate_bounds(self) -> None:
        if self._linear_feedrate_bounds is None:
            return
        self._linear_feedrate_bounds = None
        self._update_linear_feedrate_slider_range()
        self._set_linear_feedrate(
            self._linear_feedrate_value,
            reissue_if_active=False,
        )

    def _on_linear_feedrate_slider_changed(self, slider_value: int) -> None:
        self._set_linear_feedrate(
            self._feedrate_from_slider_value(slider_value),
            reissue_if_active=True,
        )

    def _on_linear_feedrate_spin_changed(self, value: float) -> None:
        self._set_linear_feedrate(float(value), reissue_if_active=True)

    def _on_feedrate_target_changed(self) -> None:
        self._set_active_feedrate_target(self._selected_feedrate_target())

    def _on_jog_mode_changed(self) -> None:
        self._set_control_mode(self._selected_control_mode(), emit_changed=True)

    def _set_control_mode(self, mode: str, *, emit_changed: bool) -> bool:
        mode = str(mode).strip().lower()
        if mode not in {self.MODE_JOG, self.MODE_STEP}:
            mode = self.MODE_JOG
        if mode == self._control_mode:
            return False
        if self._active_axes is not None:
            self.stop_jog()
        previous_target = self._active_feedrate_target
        self._control_mode = mode
        if hasattr(self, "jog_mode_combo"):
            index = self.jog_mode_combo.findData(mode)
            current_index = (
                self.jog_mode_combo.currentIndex()
                if hasattr(self.jog_mode_combo, "currentIndex")
                else None
            )
            if index >= 0 and current_index != index:
                self.jog_mode_combo.blockSignals(True)
                self.jog_mode_combo.setCurrentIndex(index)
                self.jog_mode_combo.blockSignals(False)
        self._update_mode_controls()
        self._set_active_feedrate_target(previous_target)
        if emit_changed:
            self.control_mode_changed.emit(mode)
        return True

    def set_control_mode(self, mode: str, *, emit_changed: bool = True) -> bool:
        """Set jog/step mode from other GUI panels."""

        return self._set_control_mode(mode, emit_changed=emit_changed)

    def _toggle_control_mode(self) -> bool:
        next_mode = (
            self.MODE_STEP if self._control_mode == self.MODE_JOG else self.MODE_JOG
        )
        return self._set_control_mode(next_mode, emit_changed=True)

    def _selected_control_mode(self) -> str:
        data = self.jog_mode_combo.currentData()
        mode = str(data).strip().lower() if data is not None else ""
        if mode in {self.MODE_JOG, self.MODE_STEP}:
            return mode
        return self.MODE_JOG

    def _selected_feedrate_target(self) -> str:
        data = self.feedrate_target_combo.currentData()
        target = str(data).strip().lower() if data is not None else ""
        if target in self.FEED_TARGET_LABELS:
            return target
        return self.FEED_TARGET_XY

    def _update_mode_controls(self) -> None:
        is_step = self._control_mode == self.MODE_STEP
        self.step_distance_label.setVisible(is_step)
        self.step_distance_spin.setVisible(is_step)

    def apply_feedrate_settings(
        self,
        linear_presets: List[float],
        linear_default: float,
        rotary_presets: List[float],
        rotary_default: float,
    ) -> None:
        """Apply only the linear default feedrate used by jog controls."""

        cleaned_linear = sorted(
            {
                max(self.MIN_LINEAR_FEEDRATE, min(self.MAX_LINEAR_FEEDRATE, float(value)))
                for value in linear_presets
                if isinstance(value, (int, float))
            }
        )
        if cleaned_linear:
            self._linear_presets = list(cleaned_linear)
        try:
            candidate = float(linear_default)
        except (TypeError, ValueError):
            candidate = self._linear_presets[0] if self._linear_presets else 10.0
        self._linear_default = self._bounded_feedrate_setting(
            self.FEED_TARGET_XY,
            candidate,
        )
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_XY, self.MODE_JOG)
        ] = self._linear_default
        self._update_linear_feedrate_slider_range()
        if (
            self._active_feedrate_target == self.FEED_TARGET_XY
            and self._control_mode == self.MODE_JOG
        ):
            self._set_linear_feedrate(self._linear_default, reissue_if_active=False)
        logger.info(
            "Joystick feedrate settings updated: linear=%s (default=%s)",
            self._linear_presets,
            self._linear_default,
        )

    def current_linear_feedrate(self) -> float:
        return float(self._linear_feedrate_value)

    def select_coordinate_feedrate_for_axes(self, axes: object) -> float:
        targets: set[str] = set()
        try:
            iterator = iter(axes)  # type: ignore[arg-type]
        except TypeError:
            iterator = iter(())
        for axis in iterator:
            axis_name = str(axis).strip().upper()
            if axis_name not in self.MANUAL_JOG_AXES:
                continue
            targets.add(self._feedrate_target_for_axis(axis_name))
        if not targets:
            return self.current_linear_feedrate()
        if len(targets) == 1:
            self.clear_common_feedrate_target()
            self._set_active_feedrate_target(next(iter(targets)))
            return self.current_linear_feedrate()
        return self.current_linear_feedrate()

    def current_needle_feedrate(self) -> float:
        return float(self._needle_feedrate_value)

    def apply_needle_settings(self, feedrate_mm_min: float) -> None:
        self._set_needle_feedrate(feedrate_mm_min, emit_changed=False)

    def set_axis_feedrate_limits(self, limits: dict[str, float]) -> None:
        self._axis_feedrate_limits = clean_axis_feedrate_limits(
            limits,
            valid_axes=self.MANUAL_JOG_AXES,
        )
        self._update_linear_feedrate_slider_range()
        self._set_needle_feedrate(
            self._needle_feedrate_value,
            emit_changed=False,
        )
        self._needle_step_feedrate_value = self._bounded_feedrate_setting(
            self.FEED_TARGET_NEEDLES,
            self._needle_step_feedrate_value,
        )
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_NEEDLES, self.MODE_STEP)
        ] = self._needle_step_feedrate_value
        self._set_linear_feedrate(
            self._linear_feedrate_value,
            reissue_if_active=False,
        )

    def apply_jog_settings(
        self,
        linear_distance_mm: float,
        rotary_distance_deg: float,
        motion_safety_disabled: bool = False,
        manual_axis: str = "A",
        manual_axis_distance_mm: float = 1.0,
        manual_axis_mode: str = "G91",
        manual_axis_feedrate_mm_min: float = 1.0,
        focus_feedrate_mm_min: float = 1.0,
        turntable_feedrate_mm_min: float = 1.0,
        mode: str = "jog",
        focus_step_feedrate_mm_min: float = 1.0,
        needle_step_feedrate_mm_min: float = 1.0,
        turntable_step_feedrate_mm_min: float = 1.0,
        **_legacy_visibility_options: object,
    ) -> None:
        """Update the jog distance used for linear axes."""

        self._applying_jog_settings = True
        was_motion_safety_disabled = self._motion_safety_disabled
        self._linear_jog_distance_mm = max(0.001, float(linear_distance_mm))
        self._rotary_jog_distance_deg = max(0.001, float(rotary_distance_deg))
        self._set_step_distance(float(manual_axis_distance_mm), emit_changed=False)
        self._manual_axis_feedrate_mm_min = self._bounded_feedrate_setting(
            self.FEED_TARGET_XY,
            float(manual_axis_feedrate_mm_min),
        )
        self._focus_feedrate_value = self._bounded_feedrate_setting(
            self.FEED_TARGET_FOCUS,
            float(focus_feedrate_mm_min),
        )
        self._focus_step_feedrate_value = self._bounded_feedrate_setting(
            self.FEED_TARGET_FOCUS,
            float(focus_step_feedrate_mm_min),
        )
        self._needle_step_feedrate_value = self._bounded_feedrate_setting(
            self.FEED_TARGET_NEEDLES,
            float(needle_step_feedrate_mm_min),
        )
        self._turntable_feedrate_value = self._bounded_feedrate_setting(
            self.FEED_TARGET_TURNTABLE,
            float(turntable_feedrate_mm_min),
        )
        self._turntable_step_feedrate_value = self._bounded_feedrate_setting(
            self.FEED_TARGET_TURNTABLE,
            float(turntable_step_feedrate_mm_min),
        )
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_XY, self.MODE_STEP)
        ] = self._manual_axis_feedrate_mm_min
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_FOCUS, self.MODE_JOG)
        ] = self._focus_feedrate_value
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_FOCUS, self.MODE_STEP)
        ] = self._focus_step_feedrate_value
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_NEEDLES, self.MODE_STEP)
        ] = self._needle_step_feedrate_value
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_TURNTABLE, self.MODE_JOG)
        ] = self._turntable_feedrate_value
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_TURNTABLE, self.MODE_STEP)
        ] = self._turntable_step_feedrate_value
        self._motion_safety_disabled = bool(motion_safety_disabled)
        axis = manual_axis.strip().upper() if isinstance(manual_axis, str) else "A"
        if axis not in self.MANUAL_JOG_AXES:
            axis = "A"
        manual_mode = (
            manual_axis_mode.strip().upper()
            if isinstance(manual_axis_mode, str)
            else self.DEFAULT_MANUAL_AXIS_MODE
        )
        if manual_mode not in self.MANUAL_AXIS_MODES:
            manual_mode = self.DEFAULT_MANUAL_AXIS_MODE
        self._manual_axis_mode = manual_mode
        control_mode = str(mode).strip().lower()
        if control_mode not in {self.MODE_JOG, self.MODE_STEP}:
            control_mode = self.MODE_JOG
        self._control_mode = control_mode
        mode_index = self.jog_mode_combo.findData(control_mode)
        if mode_index >= 0:
            self.jog_mode_combo.blockSignals(True)
            self.jog_mode_combo.setCurrentIndex(mode_index)
            self.jog_mode_combo.blockSignals(False)
        self._update_mode_controls()
        self._set_active_feedrate_target(self.FEED_TARGET_XY)
        self._applying_jog_settings = False
        if (
            was_motion_safety_disabled
            and not self._motion_safety_disabled
            and not self._axis_a_ready
        ):
            self.stop_jog()
            self._pending_jog_axes = None
            self._key_stack.clear()
            self._key_press_times.clear()
            self._clear_pending_key_activations()
            self._sync_physical_key_watchdog()
        self._update_enabled_state()
        logger.debug(
            "Joystick jog settings updated: mode=%s linear_distance_mm=%s safety_disabled=%s manual_axis=%s step_distance_mm=%s manual_mode=%s xy_step_feedrate_mm_min=%s focus_jog_feedrate_mm_min=%s focus_step_feedrate_mm_min=%s needle_step_feedrate_mm_min=%s turntable_jog_feedrate_mm_min=%s turntable_step_feedrate_mm_min=%s",
            self._control_mode,
            self._linear_jog_distance_mm,
            self._motion_safety_disabled,
            axis,
            self._manual_axis_distance_mm,
            self._manual_axis_mode,
            self._manual_axis_feedrate_mm_min,
            self._focus_feedrate_value,
            self._focus_step_feedrate_value,
            self._needle_step_feedrate_value,
            self._turntable_feedrate_value,
            self._turntable_step_feedrate_value,
        )

    def _set_needle_feedrate(
        self,
        value: float,
        *,
        emit_changed: bool,
    ) -> None:
        bounded = min(
            self._feedrate_max_for_target(self.FEED_TARGET_NEEDLES),
            max(self.MIN_LINEAR_FEEDRATE, float(value)),
        )
        changed = abs(bounded - self._needle_feedrate_value) > 1e-9
        self._needle_feedrate_value = bounded
        self._feedrate_values[
            self._feedrate_key(self.FEED_TARGET_NEEDLES, self.MODE_JOG)
        ] = bounded
        if (
            hasattr(self, "needle_feedrate_spin")
            and self.needle_feedrate_spin.value() != bounded
        ):
            self.needle_feedrate_spin.blockSignals(True)
            self.needle_feedrate_spin.setValue(bounded)
            self.needle_feedrate_spin.blockSignals(False)
        if (
            self._active_feedrate_target == self.FEED_TARGET_NEEDLES
            and self._control_mode == self.MODE_JOG
        ):
            self._set_linear_feedrate(bounded, reissue_if_active=False)
        if changed and emit_changed:
            self.needle_feedrate_changed.emit(bounded)

    def _on_needle_feedrate_changed(self, value: float) -> None:
        self._set_active_feedrate_target(self.FEED_TARGET_NEEDLES)
        self._set_needle_feedrate(value, emit_changed=True)



__all__ = ["JoystickFeedrateMixin", "_FeedrateSlider"]
