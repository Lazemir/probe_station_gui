"""Telegram notification settings and account-link workflow."""

from __future__ import annotations

import asyncio
import os
from typing import Dict

from PySide6.QtCore import QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.notifications.telegram import (
    LinkedTelegramChat,
    TELEGRAM_BOT_TOKEN_ENV,
    TelegramNotificationError,
    can_manage_global_bot_token,
    create_start_payload,
    global_telegram_token_path,
    load_global_bot_token,
    prepare_telegram_link,
    save_global_bot_token,
    send_telegram_message_in_thread,
    telegram_dependency_available,
    wait_for_telegram_link,
)
from probe_station_gui.settings.manager import (
    Settings,
    TELEGRAM_ALERT_TYPES,
    TelegramSettings,
)


class TelegramLinkWorker(QObject):
    """Background worker for Telegram deep-link account binding."""

    link_ready = Signal(str)
    finished = Signal(object, str)

    def __init__(self, bot_token: str, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bot_token = bot_token
        self._cancel_requested = False

    def cancel(self) -> None:
        self._cancel_requested = True

    def run(self) -> None:
        try:
            linked_chat = asyncio.run(self._run_async())
        except Exception as exc:
            self.finished.emit(None, str(exc))
        else:
            self.finished.emit(linked_chat, "Telegram account linked.")

    async def _run_async(self) -> LinkedTelegramChat:
        payload = create_start_payload()
        _bot_username, link = await prepare_telegram_link(self._bot_token, payload)
        if self._cancel_requested:
            raise TelegramNotificationError("Telegram link cancelled.")
        self.link_ready.emit(link)
        return await wait_for_telegram_link(
            self._bot_token,
            payload,
            timeout_s=120.0,
            poll_timeout_s=2.0,
            should_cancel=lambda: self._cancel_requested,
        )


class TelegramSettingsWidget(QWidget):
    """Tab that exposes Telegram notification settings."""

    test_finished = Signal(bool, str)

    def __init__(
        self, telegram_settings: TelegramSettings, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: TelegramLinkWorker | None = None
        self._token_from_env = bool(os.environ.get(TELEGRAM_BOT_TOKEN_ENV, "").strip())
        self._can_manage_global_token = can_manage_global_bot_token()
        global_token = load_global_bot_token()
        token_text = global_token or telegram_settings.bot_token

        root_layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        root_layout.addLayout(form)

        self._enabled_checkbox = QCheckBox("Enable Telegram notifications", self)
        self._enabled_checkbox.setChecked(telegram_settings.enabled)
        form.addRow(QLabel("Notifications", self), self._enabled_checkbox)

        self._token_edit = QLineEdit(self)
        self._token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._token_edit.setText(token_text)
        if self._token_from_env:
            self._token_edit.setPlaceholderText(f"Provided by {TELEGRAM_BOT_TOKEN_ENV}")
            self._token_edit.setEnabled(False)
        elif not self._can_manage_global_token:
            self._token_edit.setPlaceholderText("Managed by administrator")
            self._token_edit.setEnabled(False)
        else:
            self._token_edit.setPlaceholderText("Machine-wide bot token")
        form.addRow(QLabel("Bot token", self), self._token_edit)

        self._token_path_label = QLabel(str(global_telegram_token_path()), self)
        self._token_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self._token_path_label.setWordWrap(True)
        form.addRow(QLabel("Token file", self), self._token_path_label)

        self._linked_label = QLabel(self._linked_text(telegram_settings), self)
        self._linked_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow(QLabel("Linked chat", self), self._linked_label)

        self._alerts_list = QListWidget(self)
        for key, label in TELEGRAM_ALERT_TYPES:
            item = QListWidgetItem(label, self._alerts_list)
            item.setData(Qt.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            checked = telegram_settings.alert_enabled(key)
            item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
        root_layout.addWidget(QLabel("Alerts", self))
        root_layout.addWidget(self._alerts_list, 1)

        button_row = QHBoxLayout()
        self._link_button = QPushButton("Link Telegram", self)
        self._test_button = QPushButton("Send Test", self)
        self._forget_button = QPushButton("Forget", self)
        button_row.addWidget(self._link_button)
        button_row.addWidget(self._test_button)
        button_row.addWidget(self._forget_button)
        button_row.addStretch(1)
        root_layout.addLayout(button_row)

        self._status_label = QLabel("", self)
        self._status_label.setWordWrap(True)
        root_layout.addWidget(self._status_label)
        if not self._token_from_env and not self._can_manage_global_token:
            self._status_label.setText(
                "Machine-wide bot token can be changed only by an administrator."
            )

        self._linked_chat_id = telegram_settings.chat_id
        self._linked_chat_title = telegram_settings.chat_title
        self._bot_username = telegram_settings.bot_username
        self._linked_at_utc = telegram_settings.linked_at_utc

        self._enabled_checkbox.toggled.connect(self._update_enabled_state)
        self._link_button.clicked.connect(self._start_link)
        self._test_button.clicked.connect(self._send_test)
        self._forget_button.clicked.connect(self._forget_link)
        self.test_finished.connect(self._on_test_sent)
        self._update_enabled_state(self._enabled_checkbox.isChecked())

    def to_settings(self, settings: Settings) -> None:
        """Persist the widget state into the provided settings object."""

        telegram = settings.telegram.clone()
        telegram.enabled = self._enabled_checkbox.isChecked()
        telegram.bot_token = ""
        if not self._token_from_env and self._can_manage_global_token:
            try:
                save_global_bot_token(self._token_edit.text().strip())
            except OSError as exc:
                self._status_label.setText(
                    f"Failed to save machine-wide bot token: {exc}"
                )
            else:
                self._status_label.setText("Machine-wide bot token saved.")
        telegram.bot_username = self._bot_username.strip().lstrip("@")
        telegram.chat_id = self._linked_chat_id.strip()
        telegram.chat_title = self._linked_chat_title.strip()
        telegram.linked_at_utc = self._linked_at_utc.strip()
        telegram.alerts = self._alert_settings()
        settings.telegram = telegram

    def _start_link(self) -> None:
        if self._thread is not None:
            return
        if not telegram_dependency_available():
            self._status_label.setText("Install python-telegram-bot to link Telegram.")
            return
        token = self._current_bot_token()
        if not token:
            self._status_label.setText("Enter a Telegram bot token first.")
            return
        self._status_label.setText("Preparing Telegram link.")
        self._set_linking(True)
        thread = QThread(self)
        worker = TelegramLinkWorker(token)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.link_ready.connect(self._open_link)
        worker.finished.connect(self._on_link_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_link_thread)
        self._thread = thread
        self._worker = worker
        thread.start()

    def _open_link(self, link: str) -> None:
        opened = QDesktopServices.openUrl(QUrl(link))
        if opened:
            self._status_label.setText("Telegram opened. Press Start in the bot chat.")
        else:
            self._status_label.setText(f"Open this link: {link}")

    def _on_link_finished(self, linked_chat: object, message: str) -> None:
        self._set_linking(False)
        if isinstance(linked_chat, LinkedTelegramChat):
            self._linked_chat_id = linked_chat.chat_id
            self._linked_chat_title = linked_chat.chat_title
            self._bot_username = linked_chat.bot_username
            self._linked_at_utc = linked_chat.linked_at_utc
            self._linked_label.setText(self._linked_text_from_fields())
            self._enabled_checkbox.setChecked(True)
            self._status_label.setText(message)
            return
        self._status_label.setText(message or "Telegram link failed.")

    def _clear_link_thread(self) -> None:
        self._thread = None
        self._worker = None
        self._set_linking(False)

    def _send_test(self) -> None:
        token = self._current_bot_token()
        chat_id = self._linked_chat_id.strip()
        if not token:
            self._status_label.setText("Enter a Telegram bot token first.")
            return
        if not chat_id:
            self._status_label.setText("Link Telegram before sending a test.")
            return
        self._status_label.setText("Sending Telegram test.")
        started = send_telegram_message_in_thread(
            bot_token=token,
            chat_id=chat_id,
            text="Probe Station GUI Telegram test.",
            done_callback=self.test_finished.emit,
        )
        if not started:
            self._status_label.setText("Telegram test was not started.")

    def _on_test_sent(self, success: bool, message: str) -> None:
        self._status_label.setText(
            message if success else f"Telegram test failed: {message}"
        )

    def _forget_link(self) -> None:
        self._linked_chat_id = ""
        self._linked_chat_title = ""
        self._bot_username = ""
        self._linked_at_utc = ""
        self._linked_label.setText(self._linked_text_from_fields())
        self._status_label.setText("Telegram link removed.")
        self._update_enabled_state(self._enabled_checkbox.isChecked())

    def _alert_settings(self) -> Dict[str, bool]:
        alerts: Dict[str, bool] = {}
        for index in range(self._alerts_list.count()):
            item = self._alerts_list.item(index)
            key = str(item.data(Qt.UserRole) or "")
            if key:
                alerts[key] = item.checkState() == Qt.Checked
        return alerts

    def _current_bot_token(self) -> str:
        env_value = os.environ.get(TELEGRAM_BOT_TOKEN_ENV, "").strip()
        if env_value:
            return env_value
        return self._token_edit.text().strip() or load_global_bot_token()

    def _update_enabled_state(self, enabled: bool) -> None:
        has_chat = bool(self._linked_chat_id.strip())
        self._test_button.setEnabled(has_chat)
        self._forget_button.setEnabled(has_chat)
        self._alerts_list.setEnabled(bool(enabled))

    def _set_linking(self, linking: bool) -> None:
        self._link_button.setEnabled(not linking)
        self._test_button.setEnabled(
            (not linking) and bool(self._linked_chat_id.strip())
        )
        self._forget_button.setEnabled(
            (not linking) and bool(self._linked_chat_id.strip())
        )

    @staticmethod
    def _linked_text(telegram_settings: TelegramSettings) -> str:
        title = telegram_settings.chat_title.strip()
        if title:
            return title
        if telegram_settings.chat_id.strip():
            return f"Chat {telegram_settings.chat_id.strip()}"
        return "Not linked"

    def _linked_text_from_fields(self) -> str:
        title = self._linked_chat_title.strip()
        if title:
            return title
        if self._linked_chat_id.strip():
            return f"Chat {self._linked_chat_id.strip()}"
        return "Not linked"

    def shutdown(self) -> None:
        worker = self._worker
        if worker is not None:
            worker.cancel()
        thread = self._thread
        if thread is not None and thread.isRunning():
            thread.quit()
            thread.wait(3000)


__all__ = ["TelegramSettingsWidget"]
