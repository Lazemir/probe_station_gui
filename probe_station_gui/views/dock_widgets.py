"""Custom dock widgets for the probe station GUI."""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QStyle,
    QToolButton,
    QWidget,
)


class CollapsibleDockWidget(QDockWidget):
    """Dock widget with a built-in collapse toggle in the title bar."""

    collapsedChanged = Signal(bool)

    _MAX_SIZE = 16_777_215

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(title, parent)
        self._is_collapsed = False
        self._content: QWidget | None = None

        self.setFeatures(
            QDockWidget.DockWidgetMovable
            | QDockWidget.DockWidgetFloatable
            | QDockWidget.DockWidgetClosable
        )

        self._toggle_button = QToolButton(self)
        self._toggle_button.setAutoRaise(True)
        self._toggle_button.setCheckable(True)
        self._toggle_button.clicked.connect(self.toggle_collapsed)

        self._title_label = QLabel(title, self)
        self._title_label.setObjectName("CollapsibleDockWidgetTitle")

        title_bar = QWidget(self)
        layout = QHBoxLayout(title_bar)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(6)
        layout.addWidget(self._toggle_button)
        layout.addWidget(self._title_label)
        layout.addStretch(1)
        self.setTitleBarWidget(title_bar)

        self._update_toggle_icon()

    def setWidget(self, widget: QWidget | None) -> None:  # type: ignore[override]
        super().setWidget(widget)
        self._content = widget
        self._apply_collapsed_state()

    def setWindowTitle(self, title: str) -> None:  # type: ignore[override]
        super().setWindowTitle(title)
        self._title_label.setText(title)

    def toggle_collapsed(self) -> None:
        self.set_collapsed(not self._is_collapsed)

    def set_collapsed(self, collapsed: bool) -> None:
        if self._is_collapsed == collapsed:
            return
        self._is_collapsed = collapsed
        self._apply_collapsed_state()
        self._update_toggle_icon()
        self.collapsedChanged.emit(collapsed)

    def is_collapsed(self) -> bool:
        return self._is_collapsed

    def _apply_collapsed_state(self) -> None:
        if not self._content:
            return
        if self._is_collapsed:
            self._content.setVisible(False)
            self.setMaximumHeight(self.titleBarWidget().sizeHint().height())
            self.setMinimumHeight(self.titleBarWidget().sizeHint().height())
        else:
            self._content.setVisible(True)
            self.setMaximumHeight(self._MAX_SIZE)
            self.setMinimumHeight(0)

    def _update_toggle_icon(self) -> None:
        icon = (
            self.style().standardIcon(QStyle.SP_ArrowRight)
            if self._is_collapsed
            else self.style().standardIcon(QStyle.SP_ArrowDown)
        )
        self._toggle_button.setIcon(icon)
        self._toggle_button.setChecked(self._is_collapsed)


__all__ = ["CollapsibleDockWidget"]
