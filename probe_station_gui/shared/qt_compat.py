"""Qt compatibility helpers for platform differences."""

from __future__ import annotations

import ctypes
import platform
from typing import Union

from PySide6.QtCore import Qt

KeyboardModifierType = Union[int, Qt.KeyboardModifier, Qt.KeyboardModifiers]


def keyboard_modifiers_to_int(modifiers: KeyboardModifierType) -> int:
    """Return a stable integer representation for Qt keyboard modifiers."""

    if isinstance(modifiers, int):
        return modifiers

    value = getattr(modifiers, "value", None)
    if value is not None:
        return int(value)

    to_int = getattr(modifiers, "__int__", None)
    if callable(to_int):
        try:
            return int(to_int())
        except TypeError:
            pass

    try:
        flags = Qt.KeyboardModifiers(modifiers)
    except TypeError:
        return 0

    return getattr(flags, "value", 0)


def native_scan_code_to_int(scan_code: object) -> int:
    """Return a stable integer representation for a native scan code."""

    if isinstance(scan_code, int):
        return scan_code

    value = getattr(scan_code, "value", None)
    if value is not None:
        return int(value)

    to_int = getattr(scan_code, "__int__", None)
    if callable(to_int):
        try:
            return int(to_int())
        except TypeError:
            return 0

    return 0


def derive_native_scan_code_from_qt_key(qt_key: int) -> int:
    """Best-effort scan-code derivation for persisted legacy key bindings."""

    if platform.system() != "Windows":
        return 0

    virtual_key = _windows_virtual_key_from_qt_key(int(qt_key))
    if virtual_key <= 0:
        return 0

    try:
        scan_code = ctypes.windll.user32.MapVirtualKeyW(virtual_key, 0)
    except (AttributeError, OSError):
        return 0

    return int(scan_code)


def _windows_virtual_key_from_qt_key(qt_key: int) -> int:
    if ord("0") <= qt_key <= ord("9"):
        return qt_key
    if ord("A") <= qt_key <= ord("Z"):
        return qt_key

    arrow_map = {
        int(Qt.Key_Left): 0x25,
        int(Qt.Key_Up): 0x26,
        int(Qt.Key_Right): 0x27,
        int(Qt.Key_Down): 0x28,
        int(Qt.Key_Space): 0x20,
        int(Qt.Key_Tab): 0x09,
        int(Qt.Key_Return): 0x0D,
        int(Qt.Key_Enter): 0x0D,
    }
    return arrow_map.get(qt_key, 0)
