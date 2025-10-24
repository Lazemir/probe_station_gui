"""Qt compatibility helpers for platform differences."""

from __future__ import annotations

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
