from __future__ import annotations

import os
import threading
import time

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication

import probe_station_gui.dialogs.settings.telegram as telegram_module
from probe_station_gui.dialogs.settings.telegram import (
    TelegramLinkWorker,
    TelegramSettingsWidget,
)
from probe_station_gui.notifications.telegram import (
    LinkedTelegramChat,
    TELEGRAM_BOT_TOKEN_ENV,
)
from probe_station_gui.settings.manager import Settings, TelegramSettings


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture
def local_telegram_io(tmp_path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    saved: list[str] = []
    monkeypatch.delenv(TELEGRAM_BOT_TOKEN_ENV, raising=False)
    monkeypatch.setattr(telegram_module, "can_manage_global_bot_token", lambda: True)
    monkeypatch.setattr(telegram_module, "load_global_bot_token", lambda: "global")
    monkeypatch.setattr(
        telegram_module,
        "global_telegram_token_path",
        lambda: tmp_path / "telegram-bot.json",
    )
    monkeypatch.setattr(
        telegram_module,
        "save_global_bot_token",
        lambda token: saved.append(token) or (tmp_path / "telegram-bot.json"),
    )
    return saved


def _process_until(app: QApplication, predicate, *, timeout_s: float = 2.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert predicate()


def test_telegram_owner_uses_environment_token_without_global_save(
    app: QApplication,
    local_telegram_io: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(TELEGRAM_BOT_TOKEN_ENV, "environment")
    widget = TelegramSettingsWidget(TelegramSettings(bot_token="per-user"))
    widget._token_edit.setText("edited")
    settings = Settings()

    widget.to_settings(settings)

    assert widget.__class__.__module__ == "probe_station_gui.dialogs.settings.telegram"
    assert (
        TelegramLinkWorker.__module__ == "probe_station_gui.dialogs.settings.telegram"
    )
    assert widget._current_bot_token() == "environment"
    assert widget._token_edit.isEnabled() is False
    assert local_telegram_io == []
    assert settings.telegram.bot_token == ""
    widget.deleteLater()


def test_telegram_owner_saves_editable_global_token_and_collects_alerts(
    app: QApplication,
    local_telegram_io: list[str],
) -> None:
    widget = TelegramSettingsWidget(
        TelegramSettings(enabled=True, alerts={"route_attention": False})
    )
    widget._token_edit.setText(" edited-token ")
    assert widget._current_bot_token() == "edited-token"
    widget._token_edit.clear()
    assert widget._current_bot_token() == "global"
    widget._token_edit.setText(" edited-token ")
    first_alert = widget._alerts_list.item(0)
    desired_state = (
        Qt.Unchecked if first_alert.checkState() == Qt.Checked else Qt.Checked
    )
    first_alert.setCheckState(desired_state)
    settings = Settings()

    widget.to_settings(settings)

    assert local_telegram_io == ["edited-token"]
    assert settings.telegram.bot_token == ""
    assert settings.telegram.enabled is True
    assert settings.telegram.alerts[str(first_alert.data(Qt.UserRole))] == (
        first_alert.checkState() == Qt.Checked
    )
    widget.deleteLater()


class _ImmediateLinkWorker(QObject):
    link_ready = Signal(str)
    finished = Signal(object, str)
    instances: list[_ImmediateLinkWorker] = []

    def __init__(self, bot_token: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.bot_token = bot_token
        self.cancelled = False
        self.instances.append(self)

    @Slot()
    def run(self) -> None:
        self.link_ready.emit("https://t.me/probe_bot?start=payload")
        self.finished.emit(
            LinkedTelegramChat(
                chat_id="42",
                chat_title="Probe User",
                bot_username="probe_bot",
                linked_at_utc="2026-08-12T12:00:00Z",
            ),
            "Telegram account linked.",
        )

    def cancel(self) -> None:
        self.cancelled = True


def test_telegram_owner_starts_one_link_thread_and_forgets_link(
    app: QApplication,
    local_telegram_io: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ImmediateLinkWorker.instances.clear()
    opened: list[str] = []
    monkeypatch.setattr(telegram_module, "TelegramLinkWorker", _ImmediateLinkWorker)
    monkeypatch.setattr(telegram_module, "telegram_dependency_available", lambda: True)
    monkeypatch.setattr(
        QDesktopServices,
        "openUrl",
        lambda url: opened.append(url.toString()) or True,
    )
    widget = TelegramSettingsWidget(TelegramSettings())

    widget._start_link()
    first_thread = widget._thread
    widget._start_link()

    assert widget._thread is first_thread
    assert len(_ImmediateLinkWorker.instances) == 1
    _process_until(app, lambda: widget._thread is None)
    assert opened == ["https://t.me/probe_bot?start=payload"]
    assert widget._linked_chat_id == "42"
    assert widget._linked_label.text() == "Probe User"
    assert widget._enabled_checkbox.isChecked() is True

    widget._forget_button.click()

    assert widget._linked_chat_id == ""
    assert widget._linked_label.text() == "Not linked"
    assert widget._test_button.isEnabled() is False
    widget.deleteLater()


class _GuiThreadProbe(QObject):
    def __init__(self) -> None:
        super().__init__()
        self.thread_seen: QThread | None = None

    @Slot(bool, str)
    def record(self, _success: bool, _message: str) -> None:
        self.thread_seen = QThread.currentThread()


def test_telegram_test_send_returns_to_the_gui_thread(
    app: QApplication,
    local_telegram_io: list[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[threading.Thread] = []

    def send_in_thread(*, done_callback, **_kwargs) -> bool:
        thread = threading.Thread(
            target=lambda: done_callback(True, "Telegram test sent."),
            daemon=True,
        )
        spawned.append(thread)
        thread.start()
        return True

    monkeypatch.setattr(
        telegram_module,
        "send_telegram_message_in_thread",
        send_in_thread,
    )
    widget = TelegramSettingsWidget(
        TelegramSettings(chat_id="42", chat_title="Probe User")
    )
    probe = _GuiThreadProbe()
    widget.test_finished.connect(probe.record)

    widget._test_button.click()

    _process_until(app, lambda: probe.thread_seen is not None)
    for thread in spawned:
        thread.join(timeout=1.0)
    assert probe.thread_seen is app.thread()
    assert widget._status_label.text() == "Telegram test sent."
    widget.deleteLater()


class _CancelProbe:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def cancel(self) -> None:
        self._events.append("cancel")


class _ThreadProbe:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def isRunning(self) -> bool:
        return True

    def quit(self) -> None:
        self._events.append("quit")

    def wait(self, timeout: int) -> None:
        self._events.append(f"wait:{timeout}")


def test_telegram_owner_shutdown_preserves_bounded_cancel_sequence(
    app: QApplication,
    local_telegram_io: list[str],
) -> None:
    widget = TelegramSettingsWidget(TelegramSettings())
    events: list[str] = []
    worker = _CancelProbe(events)
    thread = _ThreadProbe(events)
    widget._worker = worker
    widget._thread = thread

    widget.shutdown()

    assert events == ["cancel", "quit", "wait:3000"]
    widget._worker = None
    widget._thread = None
    widget.deleteLater()
