"""Control bindings, held-key chords, and physical-key reconciliation."""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from typing import Dict, Optional, Tuple

from PySide6.QtCore import QTimer

from probe_station_gui.settings.controls_config import CONTROL_ACTIONS, KeyBinding
from probe_station_gui.shared.qt_compat import (
    derive_native_scan_code_from_qt_key,
    keyboard_modifiers_to_int,
    native_scan_code_to_int,
)


logger = logging.getLogger(__name__)


class JoystickKeyboardInputMixin:
    """Own mappings, held-key chord state, and physical reconciliation."""

    def _is_control_mode_toggle_mapping(self, mapping: tuple[str, int] | None) -> bool:
        return bool(mapping and mapping[0] == self.ACTION_TOGGLE_JOG_STEP)

    def _is_control_mode_toggle_event(self, event) -> bool:
        _identifier, mapping = self._mapping_from_event(event)
        return self._is_control_mode_toggle_mapping(mapping)

    def _handle_control_mode_toggle_press(self, event) -> bool:
        identifier, mapping = self._mapping_from_event(event)
        if not identifier or not self._is_control_mode_toggle_mapping(mapping):
            return False
        if event.isAutoRepeat():
            event.ignore()
            return True
        self._toggle_control_mode()
        event.accept()
        logger.debug(
            "Toggled joystick control mode from key: key=%s scan=%s text=%s modifiers=%s mode=%s",
            event.key(),
            self._event_scan_code(event),
            event.text(),
            keyboard_modifiers_to_int(event.modifiers()),
            self._control_mode,
        )
        return True

    def _handle_control_mode_toggle_release(self, event) -> bool:
        identifier, mapping = self._mapping_from_event(event)
        if not identifier or not self._is_control_mode_toggle_mapping(mapping):
            return False
        event.accept()
        return True

    def _handle_key_press_event(self, event) -> bool:
        if self._handle_control_mode_toggle_press(event):
            return True
        if not self._key_stack:
            if not self._move_safety_check():
                event.ignore()
                return True
        if event.isAutoRepeat():
            event.ignore()
            logger.debug(
                "Ignored auto-repeat key press: key=%s scan=%s text=%s modifiers=%s",
                event.key(),
                self._event_scan_code(event),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
            )
            return True
        identifier, mapping = self._mapping_from_event(event)
        if identifier and mapping:
            logger.debug(
                "TIMING keypress_received key=%s scan=%s text=%s modifiers=%s mapping=%s",
                event.key(),
                self._event_scan_code(event),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
                mapping,
            )
            if self._control_mode == self.MODE_STEP:
                axis, direction = mapping
                self._manual_axis_step(axis, direction, mode="G91")
                event.accept()
                return True
            self._register_pressed_mapping(identifier, mapping)
            event.accept()
            logger.debug(
                "Processed key press: key=%s scan=%s text=%s modifiers=%s -> %s",
                event.key(),
                self._event_scan_code(event),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
                mapping,
            )
            return True
        logger.debug(
            "No mapping for key press: key=%s scan=%s text=%s modifiers=%s",
            event.key(),
            self._event_scan_code(event),
            event.text(),
            keyboard_modifiers_to_int(event.modifiers()),
        )
        return False

    def _handle_key_release_event(self, event) -> bool:
        if self._handle_control_mode_toggle_release(event):
            return True
        if event.isAutoRepeat():
            event.ignore()
            logger.debug(
                "Ignored auto-repeat key release: key=%s scan=%s text=%s modifiers=%s",
                event.key(),
                self._event_scan_code(event),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
            )
            return True
        identifier, mapping = self._mapping_from_event(event)
        if identifier and mapping:
            if self._cancel_pending_key_activation(identifier):
                self._key_press_times.pop(identifier, None)
                event.accept()
                logger.debug(
                    "Cancelled pending key activation: key=%s scan=%s text=%s modifiers=%s mapping=%s",
                    event.key(),
                    self._event_scan_code(event),
                    event.text(),
                    keyboard_modifiers_to_int(event.modifiers()),
                    mapping,
                )
                return True
            if identifier in self._key_stack:
                self._release_key_identifier(identifier)
            event.accept()
            logger.debug(
                "TIMING keyrelease_received key=%s scan=%s text=%s modifiers=%s mapping=%s remaining=%s",
                event.key(),
                self._event_scan_code(event),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
                mapping,
                self._key_stack,
            )
            logger.debug(
                "Processed key release: key=%s scan=%s text=%s modifiers=%s -> %s",
                event.key(),
                self._event_scan_code(event),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
                mapping,
            )
            return True
        removed = self._remove_stale_key(event)
        if removed:
            self._promote_pending_keys_if_needed()
            self._schedule_active_jog_update()
            event.accept()
            logger.debug(
                "Recovered key release: key=%s scan=%s text=%s modifiers=%s",
                event.key(),
                self._event_scan_code(event),
                event.text(),
                keyboard_modifiers_to_int(event.modifiers()),
            )
            return True
        logger.debug(
            "No mapping for key release: key=%s scan=%s text=%s modifiers=%s",
            event.key(),
            self._event_scan_code(event),
            event.text(),
            keyboard_modifiers_to_int(event.modifiers()),
        )
        return False

    def _release_key_identifier(self, identifier: Tuple[str, object]) -> bool:
        if identifier not in self._key_stack:
            return False
        self._key_stack.remove(identifier)
        self._key_press_times.pop(identifier, None)
        self._promote_pending_keys_if_needed()
        self._sync_physical_key_watchdog()
        self._schedule_active_jog_update()
        return True

    def _remove_stale_key(self, event) -> bool:
        if not self._key_stack:
            return False
        key = event.key()
        scan_code = self._event_scan_code(event)
        removed = False
        for identifier in list(self._key_stack):
            kind, value = identifier
            if kind == "scan":
                if isinstance(value, tuple) and value[0] == scan_code and scan_code:
                    self._key_stack.remove(identifier)
                    self._key_press_times.pop(identifier, None)
                    removed = True
            elif kind == "key":
                if isinstance(value, tuple) and value[0] == key:
                    self._key_stack.remove(identifier)
                    self._key_press_times.pop(identifier, None)
                    removed = True
        if removed:
            self._sync_physical_key_watchdog()
        return removed

    def _register_pressed_mapping(
        self, identifier: Tuple[str, object], mapping: tuple[str, int]
    ) -> None:
        if identifier in self._key_stack:
            return
        if identifier in self._pending_key_activations:
            return
        self._key_press_times[identifier] = time.monotonic()

        axis, _direction = mapping
        active_axes = {active_axis for active_axis, _ in (self._active_axes or ())}
        pressed_axes = {
            axis_name
            for pending_identifier in self._key_stack
            if (resolved := self._mapping_from_identifier(pending_identifier))
            is not None
            for axis_name, _ in (resolved,)
        }
        current_axes = active_axes.union(pressed_axes)

        if not current_axes or axis in current_axes or axis in self.LINEAR_AXES:
            self._key_stack.append(identifier)
            self._sync_physical_key_watchdog()
            self._schedule_active_jog_update()
            return

        self._schedule_pending_key_activation(
            identifier,
            mapping,
            activation_delay_ms=self._secondary_axis_activation_delay_ms(),
        )

    def _schedule_pending_key_activation(
        self,
        identifier: Tuple[str, object],
        mapping: tuple[str, int],
        *,
        activation_delay_ms: int,
    ) -> None:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(int(max(0, activation_delay_ms)))
        timer.timeout.connect(
            lambda ident=identifier, resolved_mapping=mapping: (
                self._activate_pending_key(ident, resolved_mapping)
            )
        )
        self._pending_key_activations[identifier] = timer
        timer.start()
        self._sync_physical_key_watchdog()
        logger.debug(
            "Deferred secondary axis activation for %s by %s ms mapping=%s",
            identifier,
            int(max(0, activation_delay_ms)),
            mapping,
        )

    def _activate_pending_key(
        self, identifier: Tuple[str, object], mapping: tuple[str, int]
    ) -> None:
        timer = self._pending_key_activations.pop(identifier, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        if identifier in self._key_stack:
            return
        self._key_stack.append(identifier)
        self._sync_physical_key_watchdog()
        self._schedule_active_jog_update()
        logger.debug(
            "Activated deferred secondary axis for %s mapping=%s", identifier, mapping
        )

    def _cancel_pending_key_activation(self, identifier: Tuple[str, object]) -> bool:
        timer = self._pending_key_activations.pop(identifier, None)
        if timer is None:
            return False
        timer.stop()
        timer.deleteLater()
        return True

    def _clear_pending_key_activations(self) -> None:
        for identifier in list(self._pending_key_activations.keys()):
            self._cancel_pending_key_activation(identifier)

    def _promote_pending_keys_if_needed(self) -> None:
        if self._key_stack:
            return
        if not self._pending_key_activations:
            return
        for identifier in list(self._pending_key_activations.keys()):
            mapping = self._mapping_from_identifier(identifier)
            if mapping is None:
                self._cancel_pending_key_activation(identifier)
                continue
            self._activate_pending_key(identifier, mapping)

    def _secondary_axis_activation_delay_ms(self) -> int:
        linear_press_times = [
            self._key_press_times.get(identifier)
            for identifier in self._key_stack
            if (resolved := self._mapping_from_identifier(identifier)) is not None
            and resolved[0] in self.LINEAR_AXES
        ]
        linear_press_times = [
            float(value)
            for value in linear_press_times
            if isinstance(value, (int, float))
        ]
        if not linear_press_times:
            return self.KEYBOARD_JOG_SECONDARY_AXIS_ACTIVATION_MS
        elapsed_ms = (time.monotonic() - max(linear_press_times)) * 1000.0
        if elapsed_ms <= self.KEYBOARD_JOG_DIAGONAL_CHORD_WINDOW_MS:
            logger.debug(
                "Immediate diagonal chord accepted: elapsed_ms=%.1f threshold_ms=%s",
                elapsed_ms,
                self.KEYBOARD_JOG_DIAGONAL_CHORD_WINDOW_MS,
            )
            return 0
        return self.KEYBOARD_JOG_SECONDARY_AXIS_ACTIVATION_MS

    def _mapping_from_event(
        self, event
    ) -> tuple[Optional[Tuple[str, object]], Optional[tuple[str, int]]]:
        key = event.key()
        scan_code = self._event_scan_code(event)
        modifiers = keyboard_modifiers_to_int(event.modifiers())

        if scan_code:
            mapping = self._key_bindings.get(("scan", scan_code, modifiers))
            if mapping:
                return ("scan", (scan_code, modifiers)), mapping
            for identifier, mapping in self._key_bindings.items():
                if identifier[0] == "scan" and identifier[1] == scan_code:
                    return ("scan", (identifier[1], identifier[2])), mapping

        mapping = self._key_bindings.get(("key", key, modifiers))
        if mapping:
            return ("key", (key, modifiers)), mapping

        for identifier, mapping in self._key_bindings.items():
            if identifier[0] == "key" and identifier[1] == key:
                return ("key", (identifier[1], identifier[2])), mapping

        return (None, None)

    def _mapping_from_identifier(
        self, identifier: Tuple[str, object]
    ) -> Optional[tuple[str, int]]:
        kind, value = identifier
        if kind == "scan":
            scan_code, modifiers = value  # type: ignore[misc]
            return self._key_bindings.get(("scan", scan_code, modifiers))
        if kind == "key":
            key, modifiers = value  # type: ignore[misc]
            return self._key_bindings.get(("key", key, modifiers))
        return None

    def apply_control_bindings(self, bindings: Dict[str, list[KeyBinding]]) -> None:
        """Update the joystick key map based on the provided settings."""

        mapping: Dict[tuple, tuple[str, int]] = {}
        for action in CONTROL_ACTIONS:
            action_mapping = (
                (action.axis, action.direction)
                if action.axis
                else (self.ACTION_TOGGLE_JOG_STEP, 0)
            )
            for binding in bindings.get(action.key, []):
                scan_code = int(binding.native_scan_code or 0)
                if not scan_code:
                    scan_code = derive_native_scan_code_from_qt_key(binding.qt_key)
                if scan_code:
                    mapping[("scan", scan_code, binding.modifiers)] = action_mapping
                mapping[("key", binding.qt_key, binding.modifiers)] = action_mapping
        self._key_bindings = mapping
        self._key_stack = [
            identifier
            for identifier in self._key_stack
            if self._mapping_from_identifier(identifier) is not None
        ]
        self._key_press_times = {
            identifier: timestamp
            for identifier, timestamp in self._key_press_times.items()
            if self._mapping_from_identifier(identifier) is not None
        }
        self._sync_physical_key_watchdog()
        logger.info(
            "Joystick key bindings updated: %d entries", len(self._key_bindings)
        )

    def _sync_physical_key_watchdog(self) -> None:
        if self._key_stack or self._pending_key_activations:
            if not self._physical_key_watchdog_timer.isActive():
                self._physical_key_watchdog_timer.start()
            return
        if self._physical_key_watchdog_timer.isActive():
            self._physical_key_watchdog_timer.stop()

    def _drop_released_physical_keys(self) -> None:
        if not self._key_stack and not self._pending_key_activations:
            self._sync_physical_key_watchdog()
            return

        removed: list[Tuple[str, object]] = []
        for identifier in list(self._key_stack):
            is_down = self._physical_key_is_down(identifier)
            if is_down is False:
                self._key_stack.remove(identifier)
                self._key_press_times.pop(identifier, None)
                removed.append(identifier)
        for identifier in list(self._pending_key_activations.keys()):
            is_down = self._physical_key_is_down(identifier)
            if is_down is False:
                self._cancel_pending_key_activation(identifier)
                self._key_press_times.pop(identifier, None)
                removed.append(identifier)

        if not removed:
            self._sync_physical_key_watchdog()
            return

        logger.warning(
            "Recovered lost keyboard release for jog: removed=%s remaining=%s",
            removed,
            self._key_stack,
        )
        self._promote_pending_keys_if_needed()
        self._sync_physical_key_watchdog()
        self._schedule_active_jog_update()

    @staticmethod
    def _physical_key_is_down(identifier: Tuple[str, object]) -> Optional[bool]:
        if not sys.platform.startswith("win"):
            return None
        try:
            kind, value = identifier
            if not isinstance(value, tuple) or not value:
                return None
            vk_code = 0
            if kind == "scan":
                scan_code = int(value[0] or 0)
                if not scan_code:
                    return None
                vk_code = int(ctypes.windll.user32.MapVirtualKeyW(scan_code, 3))
            elif kind == "key":
                vk_code = int(value[0] or 0)
            if not (0 < vk_code <= 0xFF):
                return None
            return bool(ctypes.windll.user32.GetAsyncKeyState(vk_code) & 0x8000)
        except Exception:
            logger.debug(
                "Unable to read physical key state for %s",
                identifier,
                exc_info=True,
            )
            return None

    @staticmethod
    def _event_scan_code(event) -> int:
        native_scan = getattr(event, "nativeScanCode", None)
        if native_scan is None:
            return 0
        if callable(native_scan):
            return native_scan_code_to_int(native_scan())
        return native_scan_code_to_int(native_scan)
