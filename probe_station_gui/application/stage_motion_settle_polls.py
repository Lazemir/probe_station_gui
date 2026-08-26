"""Session-owned delayed status polls after coordinate motion settles."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QTimer, Slot


class _StageMotionSettleStatusPolls(QObject):
    def __init__(
        self,
        session: QObject,
        *,
        delays_ms: tuple[int, ...],
        request_status_refresh: Callable[[], None],
    ) -> None:
        super().__init__(session)
        self._delays_ms = tuple(int(delay_ms) for delay_ms in delays_ms)
        self._request_status_refresh = request_status_refresh
        self._timers: set[QTimer] = set()

    def schedule(self) -> None:
        session = self.parent()
        if session is None:
            return
        for delay_ms in self._delays_ms:
            timer = QTimer(session)
            timer.setSingleShot(True)
            timer.setInterval(delay_ms)
            timer.timeout.connect(self._on_timeout)
            self._timers.add(timer)
            timer.start()

    @Slot()
    def _on_timeout(self) -> None:
        timer = self.sender()
        if not isinstance(timer, QTimer) or timer not in self._timers:
            return
        self._timers.remove(timer)
        try:
            self._request_status_refresh()
        finally:
            timer.deleteLater()

    def clear(self) -> None:
        timers = tuple(self._timers)
        self._timers.clear()
        for timer in timers:
            timer.stop()
            timer.deleteLater()
