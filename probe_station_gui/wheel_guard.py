"""Guard Qt value editors from accidental mouse-wheel changes."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6 import QtWidgets


WHEEL_ALWAYS_ALLOWED_PROPERTY = "_probeStationWheelAlwaysAllowed"


def allow_wheel_value_change(widget: object, allowed: bool = True) -> None:
    """Allow a value editor to react to wheel events even without edit focus."""

    set_property = getattr(widget, "setProperty", None)
    if callable(set_property):
        set_property(WHEEL_ALWAYS_ALLOWED_PROPERTY, bool(allowed))


def _property_enabled(widget: object) -> bool:
    get_property = getattr(widget, "property", None)
    if not callable(get_property):
        return False
    try:
        return bool(get_property(WHEEL_ALWAYS_ALLOWED_PROPERTY))
    except RuntimeError:
        return False


def _has_focus(widget: object) -> bool:
    has_focus = getattr(widget, "hasFocus", None)
    if callable(has_focus):
        try:
            if bool(has_focus()):
                return True
        except RuntimeError:
            return False
    focus_proxy = getattr(widget, "focusProxy", None)
    if callable(focus_proxy):
        try:
            proxy = focus_proxy()
        except RuntimeError:
            proxy = None
        if proxy is not None and proxy is not widget:
            return _has_focus(proxy)
    return False


def _wheel_changes_value_allowed(widget: object) -> bool:
    if _property_enabled(widget):
        return True
    return _has_focus(widget)


def _strong_focus_policy() -> object | None:
    focus_policy = getattr(Qt, "FocusPolicy", None)
    strong_focus = getattr(focus_policy, "StrongFocus", None)
    if strong_focus is not None:
        return strong_focus
    return getattr(Qt, "StrongFocus", None)


class _WheelGuardMixin:
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        set_focus_policy = getattr(self, "setFocusPolicy", None)
        strong_focus = _strong_focus_policy()
        if callable(set_focus_policy) and strong_focus is not None:
            set_focus_policy(strong_focus)

    def wheelEvent(self, event) -> None:  # type: ignore[override]
        if _wheel_changes_value_allowed(self):
            super().wheelEvent(event)
            return
        event.ignore()


class GuardedComboBox(_WheelGuardMixin, QtWidgets.QComboBox):
    """Combo box that ignores wheel events until explicitly focused."""


class GuardedDoubleSpinBox(_WheelGuardMixin, QtWidgets.QDoubleSpinBox):
    """Double spin box that ignores wheel events until explicitly focused."""


_SpinBoxBase = getattr(QtWidgets, "QSpinBox", QtWidgets.QDoubleSpinBox)


class GuardedSpinBox(_WheelGuardMixin, _SpinBoxBase):
    """Spin box that ignores wheel events until explicitly focused."""


_SliderBase = getattr(QtWidgets, "QSlider")


class GuardedSlider(_WheelGuardMixin, _SliderBase):
    """Slider that ignores wheel events until explicitly focused."""
