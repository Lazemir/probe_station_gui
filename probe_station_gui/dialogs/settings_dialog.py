"""Dialog for configuring application settings."""

from __future__ import annotations

import asyncio
import os
from typing import Dict

from PySide6.QtCore import QLocale, QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLineEdit,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.api.keys import (
    API_PERMISSION_ROUTE_MEASURE,
    API_PERMISSION_ROUTE_READ,
    API_PERMISSION_STAGE_READ,
    API_PERMISSION_STAGE_WRITE,
    ApiKeyRecord,
    ApiKeyStore,
    default_api_user_name,
    masked_api_key,
    normalize_permissions,
)
from probe_station_gui.dialogs.camera_settings_dialog import CameraSettingsWidget
from probe_station_gui.dialogs.settings.axis_settings import AxisSettingsWidget
from probe_station_gui.dialogs.settings.controls import (
    ControlsSettingsWidget,
    KeyBindingListEditor,
    KeyCaptureDialog,
)
from probe_station_gui.dialogs.settings.coordinate_system import (
    CoordinateSystemSettingsWidget,
)
from probe_station_gui.dialogs.settings.feedrates import (
    FeedrateGroupEditor,
    FeedrateSettingsWidget,
)
from probe_station_gui.dialogs.settings.jog import JogSettingsWidget
from probe_station_gui.dialogs.settings.measurement import MeasurementSettingsWidget
from probe_station_gui.dialogs.settings.objectives import ObjectivesSettingsWidget
from probe_station_gui.shared.wheel_guard import (
    GuardedComboBox as QComboBox,
    GuardedDoubleSpinBox as QDoubleSpinBox,
    GuardedSpinBox as QSpinBox,
)
from probe_station_gui.settings.manager import (
    ApiSettings,
    LoggingSettings,
    NeedleCalibrationSettings,
    Settings,
    TELEGRAM_ALERT_TYPES,
    TelegramSettings,
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

__all__ = [
    "ControlsSettingsWidget",
    "CoordinateSystemSettingsWidget",
    "FeedrateGroupEditor",
    "FeedrateSettingsWidget",
    "JogSettingsWidget",
    "KeyBindingListEditor",
    "KeyCaptureDialog",
    "MeasurementSettingsWidget",
    "ObjectivesSettingsWidget",
    "SettingsDialog",
]


class LoggingSettingsWidget(QWidget):
    """Tab that exposes logging configuration."""

    LEVEL_OPTIONS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    def __init__(
        self, logging_settings: LoggingSettings, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._level_combo = QComboBox(self)
        self._level_combo.addItems(self.LEVEL_OPTIONS)
        current_level = logging_settings.level.upper()
        if current_level in self.LEVEL_OPTIONS:
            self._level_combo.setCurrentText(current_level)
        layout.addRow(QLabel("Verbosity", self), self._level_combo)

        self._file_edit = QLineEdit(self)
        self._file_edit.setPlaceholderText("Leave blank for the default log file")
        self._file_edit.setText(logging_settings.file)
        layout.addRow(QLabel("Log file", self), self._file_edit)

    def to_settings(self, logging_settings: LoggingSettings) -> None:
        """Persist the widget state into the provided settings object."""

        logging_settings.level = self._level_combo.currentText()
        logging_settings.file = self._file_edit.text().strip()


class ApiSettingsWidget(QWidget):
    """Tab that exposes the local control API settings."""

    PERMISSION_COLUMNS = {
        3: API_PERMISSION_STAGE_READ,
        4: API_PERMISSION_STAGE_WRITE,
        5: API_PERMISSION_ROUTE_READ,
        6: API_PERMISSION_ROUTE_MEASURE,
    }
    ITEM_KIND_ROLE = Qt.UserRole
    RECORD_ID_ROLE = Qt.UserRole + 1
    USER_NAME_ROLE = Qt.UserRole + 2

    def __init__(
        self,
        api_settings: ApiSettings,
        parent: QWidget | None = None,
        *,
        api_key_store: ApiKeyStore | None = None,
    ) -> None:
        super().__init__(parent)
        self._api_key_store = api_key_store
        self._api_key_records: list[ApiKeyRecord] = []
        self._refreshing_keys = False

        root_layout = QVBoxLayout(self)
        form_layout = QFormLayout()
        form_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        root_layout.addLayout(form_layout)

        self._enabled_checkbox = QCheckBox("Start with app", self)
        self._enabled_checkbox.setChecked(api_settings.enabled)
        form_layout.addRow(QLabel("API server", self), self._enabled_checkbox)

        self._host_edit = QLineEdit(self)
        self._host_edit.setText(api_settings.host)
        self._host_edit.setPlaceholderText("127.0.0.1")
        form_layout.addRow(QLabel("Host", self), self._host_edit)

        self._port_spin = QSpinBox(self)
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(int(api_settings.port))
        form_layout.addRow(QLabel("Port", self), self._port_spin)

        keys_group = QGroupBox("API keys", self)
        keys_layout = QVBoxLayout(keys_group)
        self._keys_tree = QTreeWidget(keys_group)
        self._keys_tree.setColumnCount(9)
        self._keys_tree.setHeaderLabels(
            [
                "User / key",
                "Key",
                "Enabled",
                "Stage read",
                "Stage write",
                "Route read",
                "Route measure",
                "Created",
                "Last used",
            ]
        )
        self._keys_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self._keys_tree.setRootIsDecorated(True)
        header = self._keys_tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        keys_layout.addWidget(self._keys_tree)

        key_buttons = QHBoxLayout()
        self._create_key_button = QPushButton("Create Key", keys_group)
        self._rename_user_button = QPushButton("Rename User", keys_group)
        self._rename_key_button = QPushButton("Rename Key", keys_group)
        self._delete_key_button = QPushButton("Delete Key", keys_group)
        key_buttons.addWidget(self._create_key_button)
        key_buttons.addWidget(self._rename_user_button)
        key_buttons.addWidget(self._rename_key_button)
        key_buttons.addWidget(self._delete_key_button)
        key_buttons.addStretch(1)
        keys_layout.addLayout(key_buttons)

        self._key_status_label = QLabel("", keys_group)
        self._key_status_label.setWordWrap(True)
        keys_layout.addWidget(self._key_status_label)
        root_layout.addWidget(keys_group, 1)

        self._enabled_checkbox.toggled.connect(self._update_enabled_state)
        self._keys_tree.itemChanged.connect(self._on_key_item_changed)
        self._keys_tree.itemSelectionChanged.connect(self._update_key_buttons)
        self._create_key_button.clicked.connect(self._create_api_key)
        self._rename_user_button.clicked.connect(self._rename_selected_user)
        self._rename_key_button.clicked.connect(self._rename_selected_key)
        self._delete_key_button.clicked.connect(self._delete_selected_api_key)
        self._update_enabled_state(self._enabled_checkbox.isChecked())
        self._refresh_api_keys()

    def to_settings(self, settings: Settings) -> None:
        """Persist the widget state into the provided settings object."""

        host = self._host_edit.text().strip() or "127.0.0.1"
        settings.api = ApiSettings(
            enabled=self._enabled_checkbox.isChecked(),
            host=host,
            port=int(self._port_spin.value()),
        )

    def _update_enabled_state(self, enabled: bool) -> None:
        self._host_edit.setEnabled(enabled)
        self._port_spin.setEnabled(enabled)

    def _refresh_api_keys(self) -> None:
        self._refreshing_keys = True
        try:
            self._api_key_records = (
                self._api_key_store.list_keys()
                if self._api_key_store is not None
                else []
            )
            self._keys_tree.clear()
            for user_name, records in self._grouped_key_records():
                self._populate_user_group(user_name, records)
            self._keys_tree.expandAll()
            for column in range(self._keys_tree.columnCount()):
                self._keys_tree.resizeColumnToContents(column)
        finally:
            self._refreshing_keys = False
        key_count = len(self._api_key_records)
        if self._api_key_store is None:
            self._key_status_label.setText("API key storage is unavailable.")
        elif key_count == 0:
            self._key_status_label.setText("No API keys.")
        else:
            user_count = len({record.user_name for record in self._api_key_records})
            self._key_status_label.setText(f"Users: {user_count}, keys: {key_count}.")
        self._update_key_buttons()

    def _grouped_key_records(self) -> list[tuple[str, list[ApiKeyRecord]]]:
        grouped: dict[str, list[ApiKeyRecord]] = {}
        for record in sorted(
            self._api_key_records,
            key=lambda item: (
                item.user_name.casefold(),
                item.key_name.casefold(),
                item.created_at_utc,
            ),
        ):
            grouped.setdefault(record.user_name, []).append(record)
        return list(grouped.items())

    def _populate_user_group(
        self,
        user_name: str,
        records: list[ApiKeyRecord],
    ) -> None:
        key_count = len(records)
        user_item = QTreeWidgetItem(
            [user_name, f"{key_count} key{'s' if key_count != 1 else ''}"]
        )
        user_item.setData(0, self.ITEM_KIND_ROLE, "user")
        user_item.setData(0, self.USER_NAME_ROLE, user_name)
        user_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
        self._keys_tree.addTopLevelItem(user_item)
        for record in records:
            user_item.addChild(self._key_item(record))

    def _key_item(self, record: ApiKeyRecord) -> QTreeWidgetItem:
        permissions = normalize_permissions(record.permissions)
        item = QTreeWidgetItem(
            [
                record.key_name or record.key_prefix,
                masked_api_key(record.key_prefix, record.key_suffix),
                "",
                "",
                "",
                "",
                "",
                record.created_at_utc,
                record.last_used_at_utc or "",
            ]
        )
        item.setData(0, self.ITEM_KIND_ROLE, "key")
        item.setData(0, self.RECORD_ID_ROLE, record.id)
        item.setData(0, self.USER_NAME_ROLE, record.user_name)
        item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
        item.setCheckState(2, Qt.Checked if record.enabled else Qt.Unchecked)
        for column, permission in self.PERMISSION_COLUMNS.items():
            item.setCheckState(
                column,
                Qt.Checked if permissions.get(permission, False) else Qt.Unchecked,
            )
        return item

    def _create_api_key(self) -> None:
        if self._api_key_store is None:
            return
        user_name, ok = QInputDialog.getText(
            self,
            "Create API Key",
            "User name",
            text=default_api_user_name(),
        )
        if not ok:
            return
        user_name = user_name.strip() or default_api_user_name()
        key_name, ok = QInputDialog.getText(
            self,
            "Create API Key",
            "Key name",
        )
        if not ok:
            return
        api_key, _record = self._api_key_store.create_key(
            user_name=user_name,
            key_name=key_name.strip(),
        )
        clipboard = QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(api_key)
        self._refresh_api_keys()
        QMessageBox.information(
            self,
            "API Key Created",
            f"API key copied to clipboard. It will not be shown again.\n\n{api_key}",
        )

    def _rename_selected_user(self) -> None:
        if self._api_key_store is None:
            return
        user_name = self._selected_user_name()
        if not user_name:
            return
        new_name, ok = QInputDialog.getText(
            self,
            "Rename User",
            "User name",
            text=user_name,
        )
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name:
            QMessageBox.warning(self, "Rename User", "Enter a user name.")
            return
        for record in self._api_key_records:
            if record.user_name == user_name:
                self._api_key_store.update_key(record.id, user_name=new_name)
        self._refresh_api_keys()

    def _rename_selected_key(self) -> None:
        if self._api_key_store is None:
            return
        record = self._selected_key_record()
        if record is None:
            return
        new_name, ok = QInputDialog.getText(
            self,
            "Rename Key",
            "Key name",
            text=record.key_name,
        )
        if not ok:
            return
        self._api_key_store.update_key(record.id, key_name=new_name.strip())
        self._refresh_api_keys()

    def _delete_selected_api_key(self) -> None:
        if self._api_key_store is None:
            return
        record_id = self._selected_key_id()
        if not record_id:
            QMessageBox.information(self, "Delete API Key", "Select a key.")
            return
        answer = QMessageBox.question(
            self,
            "Delete API Key",
            "Delete selected API key?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self._api_key_store.delete_key(record_id)
        self._refresh_api_keys()

    def _on_key_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._refreshing_keys or self._api_key_store is None:
            return
        if item.data(0, self.ITEM_KIND_ROLE) != "key":
            return
        record_id = self._record_id_for_item(item)
        if not record_id:
            return
        permissions = {}
        for column, permission in self.PERMISSION_COLUMNS.items():
            permissions[permission] = item.checkState(column) == Qt.Checked
        self._api_key_store.update_key(
            record_id,
            enabled=item.checkState(2) == Qt.Checked,
            permissions=permissions,
        )

    def _update_key_buttons(self) -> None:
        has_store = self._api_key_store is not None
        selected_key = self._selected_key_id() is not None
        selected_user = self._selected_user_name() is not None
        self._create_key_button.setEnabled(has_store)
        self._rename_user_button.setEnabled(has_store and selected_user)
        self._rename_key_button.setEnabled(has_store and selected_key)
        self._delete_key_button.setEnabled(has_store and selected_key)

    def _selected_key_id(self) -> str | None:
        return self._record_id_for_item(self._keys_tree.currentItem())

    def _selected_key_record(self) -> ApiKeyRecord | None:
        record_id = self._selected_key_id()
        if not record_id:
            return None
        for record in self._api_key_records:
            if record.id == record_id:
                return record
        return None

    def _selected_user_name(self) -> str | None:
        item = self._keys_tree.currentItem()
        if item is None:
            return None
        if item.data(0, self.ITEM_KIND_ROLE) == "key":
            item = item.parent()
        if item is None:
            return None
        user_name = item.data(0, self.USER_NAME_ROLE)
        return str(user_name) if user_name else None

    @staticmethod
    def _record_id_for_item(item: QTreeWidgetItem | None) -> str | None:
        if item is None or item.data(0, ApiSettingsWidget.ITEM_KIND_ROLE) != "key":
            return None
        record_id = item.data(0, ApiSettingsWidget.RECORD_ID_ROLE)
        return str(record_id) if record_id else None


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


class NeedleSettingsWidget(QWidget):
    """Tab that exposes needle-only settings."""

    COORDINATE_RANGE_MM = 1000000.0

    def __init__(
        self,
        calibration_settings: NeedleCalibrationSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._contact_zone_spin = QDoubleSpinBox(self)
        self._contact_zone_spin.setDecimals(3)
        self._contact_zone_spin.setRange(0.0, 10.0)
        self._contact_zone_spin.setSingleStep(0.01)
        self._contact_zone_spin.setSuffix(" mm")
        self._contact_zone_spin.setValue(calibration_settings.contact_zone_mm)
        layout.addRow(QLabel("Needle contact zone", self), self._contact_zone_spin)

        self._chip_contact_configured_checkbox = QCheckBox("Configured", self)
        self._chip_contact_configured_checkbox.setChecked(
            calibration_settings.chip_position.configured
        )
        layout.addRow(
            QLabel("Chip contact (machine)", self),
            self._chip_contact_configured_checkbox,
        )

        chip_contact_widget = QWidget(self)
        chip_contact_layout = QHBoxLayout(chip_contact_widget)
        chip_contact_layout.setContentsMargins(0, 0, 0, 0)
        self._chip_contact_x_spin = self._make_coordinate_spin(
            calibration_settings.chip_position.x_mm
        )
        self._chip_contact_y_spin = self._make_coordinate_spin(
            calibration_settings.chip_position.y_mm
        )
        self._chip_contact_z_spin = self._make_coordinate_spin(
            calibration_settings.chip_position.z_mm
        )
        self._chip_contact_reset_button = QPushButton("Reset", self)
        chip_contact_layout.addWidget(QLabel("X", chip_contact_widget))
        chip_contact_layout.addWidget(self._chip_contact_x_spin, 1)
        chip_contact_layout.addWidget(QLabel("Y", chip_contact_widget))
        chip_contact_layout.addWidget(self._chip_contact_y_spin, 1)
        chip_contact_layout.addWidget(QLabel("Z", chip_contact_widget))
        chip_contact_layout.addWidget(self._chip_contact_z_spin, 1)
        chip_contact_layout.addWidget(self._chip_contact_reset_button)
        layout.addRow(QLabel("Contact point", self), chip_contact_widget)

        self._chip_contact_configured_checkbox.toggled.connect(
            self._update_chip_contact_state
        )
        self._chip_contact_reset_button.clicked.connect(self._reset_chip_contact)
        self._update_chip_contact_state()

    def to_settings(self, settings: Settings) -> None:
        """Persist the widget state into the provided settings object."""

        needle_settings = settings.needle_calibration.clone()
        needle_settings.contact_zone_mm = self._contact_zone_spin.value()
        chip_position = needle_settings.chip_position
        chip_position.configured = self._chip_contact_configured_checkbox.isChecked()
        if chip_position.configured:
            chip_position.x_mm = self._chip_contact_x_spin.value()
            chip_position.y_mm = self._chip_contact_y_spin.value()
            chip_position.z_mm = self._chip_contact_z_spin.value()
        else:
            chip_position.x_mm = 0.0
            chip_position.y_mm = 0.0
            chip_position.z_mm = 0.0
        settings.needle_calibration = needle_settings

    def _make_coordinate_spin(self, value: float) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setLocale(QLocale.c())
        spin.setDecimals(4)
        spin.setRange(-self.COORDINATE_RANGE_MM, self.COORDINATE_RANGE_MM)
        spin.setSingleStep(0.001)
        spin.setSuffix(" mm")
        spin.setValue(value)
        return spin

    def _update_chip_contact_state(self) -> None:
        enabled = self._chip_contact_configured_checkbox.isChecked()
        self._chip_contact_x_spin.setEnabled(enabled)
        self._chip_contact_y_spin.setEnabled(enabled)
        self._chip_contact_z_spin.setEnabled(enabled)

    def _reset_chip_contact(self) -> None:
        self._chip_contact_configured_checkbox.setChecked(False)
        self._chip_contact_x_spin.setValue(0.0)
        self._chip_contact_y_spin.setValue(0.0)
        self._chip_contact_z_spin.setValue(0.0)


class SettingsDialog(QDialog):
    """Main settings dialog with tabbed sections."""

    settings_applied = Signal(object)

    def __init__(
        self,
        settings: Settings,
        parent: QWidget | None = None,
        *,
        initial_tab: str | None = None,
        camera_settings_source: object | None = None,
        axis_position_source: object | None = None,
        api_key_store: ApiKeyStore | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self._settings = settings.clone()
        self._applied_once = False
        self._calibration_imports_active = False
        self._camera_tab: CameraSettingsWidget | None = None

        root_layout = QVBoxLayout(self)
        self._tabs = QTabWidget(self)
        root_layout.addWidget(self._tabs)

        self._controls_tab = ControlsSettingsWidget(self._settings, self)
        self._api_tab = ApiSettingsWidget(
            self._settings.api,
            self,
            api_key_store=api_key_store,
        )
        self._telegram_tab = TelegramSettingsWidget(self._settings.telegram, self)
        self._logging_tab = LoggingSettingsWidget(self._settings.logging, self)
        self._jog_tab = JogSettingsWidget(self._settings.jog, self)
        self._measurement_tab = MeasurementSettingsWidget(
            self._settings.needle_calibration, self
        )
        self._needles_tab = NeedleSettingsWidget(
            self._settings.needle_calibration, self
        )
        self._coordinate_system_tab = CoordinateSystemSettingsWidget(
            self._settings.coordinate_system, self
        )
        self._objectives_tab = ObjectivesSettingsWidget(
            self._settings.objectives,
            self,
        )
        self._axes_tab = AxisSettingsWidget(
            self._settings.axis_calibrations,
            self._settings.precision_approach,
            self,
            position_source=axis_position_source,
        )
        if camera_settings_source is not None:
            self._camera_tab = CameraSettingsWidget(camera_settings_source, self)
        self._tabs.addTab(self._controls_tab, "Controls")
        self._tabs.addTab(self._api_tab, "API")
        if self._camera_tab is not None:
            self._tabs.addTab(self._camera_tab, "Camera")
        self._tabs.addTab(self._telegram_tab, "Telegram")
        self._tabs.addTab(self._jog_tab, "Jog")
        self._tabs.addTab(self._coordinate_system_tab, "Coordinates")
        self._tabs.addTab(self._axes_tab, "Axes")
        self._tabs.addTab(self._objectives_tab, "Objectives")
        self._tabs.addTab(self._measurement_tab, "Measurement")
        self._tabs.addTab(self._needles_tab, "Needles")
        self._tabs.addTab(self._logging_tab, "Logging")
        requested_tab = (initial_tab or "").strip().lower()
        if requested_tab in {"axis calibration", "precision approach"}:
            requested_tab = "axes"
        if requested_tab:
            for index in range(self._tabs.count()):
                if self._tabs.tabText(index).lower() == requested_tab:
                    self._tabs.setCurrentIndex(index)
                    break
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._refresh_camera_tab_if_current()

        self._button_box = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Apply | QDialogButtonBox.Cancel,
            self,
        )
        self._button_box.accepted.connect(self.accept)
        self._button_box.rejected.connect(self.reject)
        self._save_button = self._button_box.button(QDialogButtonBox.Save)
        self._apply_button = self._button_box.button(QDialogButtonBox.Apply)
        if self._apply_button is not None:
            self._apply_button.clicked.connect(self._apply_without_closing)
        self._axes_tab.calibration_imports_active_changed.connect(
            self._set_calibration_imports_active
        )
        root_layout.addWidget(self._button_box)

    def _on_tab_changed(self, _index: int) -> None:
        self._refresh_camera_tab_if_current()

    def _refresh_camera_tab_if_current(self) -> None:
        if self._camera_tab is None:
            return
        if self._tabs.currentWidget() is not self._camera_tab:
            return
        if not self._camera_tab.has_loaded():
            self._camera_tab.refresh()

    def accept(self) -> None:  # type: ignore[override]
        if self._calibration_imports_active:
            return
        self._collect_settings()
        self._applied_once = True
        self.settings_applied.emit(self._settings.clone())
        self._telegram_tab.shutdown()
        super().accept()

    def _apply_without_closing(self) -> None:
        if self._calibration_imports_active:
            return
        self._collect_settings()
        self._applied_once = True
        self.settings_applied.emit(self._settings.clone())

    def _set_calibration_imports_active(self, active: bool) -> None:
        self._calibration_imports_active = active
        if self._save_button is not None:
            self._save_button.setEnabled(not active)
        if self._apply_button is not None:
            self._apply_button.setEnabled(not active)

    def _collect_settings(self) -> None:
        self._controls_tab.to_settings(self._settings)
        self._api_tab.to_settings(self._settings)
        self._telegram_tab.to_settings(self._settings)
        self._jog_tab.to_settings(self._settings)
        self._coordinate_system_tab.to_settings(self._settings)
        self._objectives_tab.to_settings(self._settings)
        self._axes_tab.to_settings(self._settings)
        self._measurement_tab.to_settings(self._settings)
        self._needles_tab.to_settings(self._settings)
        self._logging_tab.to_settings(self._settings.logging)
        if self._camera_tab is not None:
            self._camera_tab.apply_pending_settings()

    def result_settings(self) -> Settings:
        """Return a clone of the adjusted settings."""

        return self._settings.clone()

    def was_applied(self) -> bool:
        """Return True when settings were applied at least once."""

        return self._applied_once

    def reject(self) -> None:  # type: ignore[override]
        self._telegram_tab.shutdown()
        super().reject()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._telegram_tab.shutdown()
        super().closeEvent(event)
