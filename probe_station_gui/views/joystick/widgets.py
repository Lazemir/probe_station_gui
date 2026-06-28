"""Helper widgets used by the joystick window."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QLocale, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QDoubleValidator, QPainter, QPen
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLineEdit, QWidget


class _SpinnerOverlay(QWidget):
    """Lightweight spinner overlay drawn with QPainter."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self._angle = 0
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)

    def set_angle(self, angle: int) -> None:
        self._angle = angle % 360
        self.update()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        size = min(self.width(), self.height())
        if size <= 8:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        rect = QRectF(3, 3, self.width() - 6, self.height() - 6)
        bg_pen = QPen(QColor("#bdbdbd"), 2)
        bg_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(bg_pen)
        painter.drawEllipse(rect)
        pen = QPen(QColor("#1565c0"), 2)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.drawArc(rect, int(self._angle * 16), int(120 * 16))


class _NeedleContactCoordinateEdit(QWidget):
    """Temporary editor for the saved needle contact A coordinate."""

    accepted = Signal(float)
    cancelled = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("NeedleContactCoordinateEdit")
        self._line_edit = QLineEdit(self)
        validator = QDoubleValidator(self._line_edit)
        validator.setLocale(QLocale.c())
        validator.setNotation(QDoubleValidator.StandardNotation)
        self._line_edit.setValidator(validator)
        self._line_edit.setAlignment(Qt.AlignCenter)
        self._line_edit.installEventFilter(self)
        self._line_edit.setContextMenuPolicy(Qt.CustomContextMenu)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._line_edit, 1)

        for widget in (self, self._line_edit):
            font = widget.font()
            font.setBold(False)
            widget.setFont(font)
        self.setStyleSheet(
            "#NeedleContactCoordinateEdit QLineEdit { font-weight: normal; }"
        )
        self.hide()
        self._accepting = False
        self._cancel_on_focus_out = True

    def set_cancel_on_focus_out(self, enabled: bool) -> None:
        self._cancel_on_focus_out = bool(enabled)

    def setText(self, text: str) -> None:  # noqa: N802 - Qt API style
        self._line_edit.setText(text)

    def clear(self) -> None:
        self._line_edit.clear()

    def setModified(self, modified: bool) -> None:  # noqa: N802 - Qt API style
        self._line_edit.setModified(modified)

    def selectAll(self) -> None:  # noqa: N802 - Qt API style
        self._line_edit.selectAll()

    def value(self) -> float | None:
        if not self._line_edit.hasAcceptableInput():
            return None
        return float(self._line_edit.text().strip())

    def line_edit(self) -> QLineEdit:
        return self._line_edit

    def setFocus(self, reason: Qt.FocusReason = Qt.OtherFocusReason) -> None:  # type: ignore[override]
        self._line_edit.setFocus(reason)

    def eventFilter(self, obj, event):  # type: ignore[override]
        if obj is self._line_edit and event.type() == QEvent.KeyPress:
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self._accept_current_text()
                event.accept()
                return True
            if event.key() == Qt.Key_Escape:
                self.cancelled.emit()
                event.accept()
                return True
        if (
            obj is self._line_edit
            and event.type() == QEvent.FocusOut
            and not self._accepting
            and self._cancel_on_focus_out
        ):
            self.cancelled.emit()
        return super().eventFilter(obj, event)

    def _accept_current_text(self) -> None:
        value = self.value()
        if value is None:
            QApplication.beep()
            return
        self._accepting = True
        try:
            self.accepted.emit(value)
        finally:
            self._accepting = False




__all__ = ["_NeedleContactCoordinateEdit", "_SpinnerOverlay"]
