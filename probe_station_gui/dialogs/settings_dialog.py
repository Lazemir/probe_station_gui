"""Dialog for configuring application settings."""

from __future__ import annotations

import asyncio
import math
import os
from typing import Dict, List, Sequence, cast

from PySide6.QtCore import QEvent, QLocale, QObject, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QDoubleValidator, QKeyEvent, QKeySequence
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QFrame,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.dialogs.camera_settings_dialog import CameraSettingsWidget
from probe_station_gui.qt_compat import keyboard_modifiers_to_int, native_scan_code_to_int
from probe_station_gui.settings_manager import (
    CONTROL_ACTIONS,
    ApiSettings,
    CoordinateSystemSettings,
    FeedrateGroup,
    FeedrateSettings,
    JogSettings,
    KeyBinding,
    LCR_APERTURE_RATES,
    LCR_LEVEL_MODES,
    LCR_METER_TYPE_GWINSTEK,
    LCR_METER_TYPE_LABELS,
    LCR_METER_TYPES,
    LCR_MEASUREMENT_FUNCTIONS,
    LCR_MONITOR_PARAMETERS,
    LCR_RANGE_MODES,
    LCR_SOURCE_RESISTANCES_OHM,
    LCR_TRIGGER_SOURCES,
    LoggingSettings,
    AxisACalibrationSettings,
    AxisZCalibrationSettings,
    NeedleCalibrationSettings,
    ObjectiveCalibrationSettings,
    ObjectivesSettings,
    Settings,
    TELEGRAM_ALERT_TYPES,
    TelegramSettings,
    WORK_COORDINATE_SYSTEMS,
    ordered_objective_names,
)
from probe_station_gui.telegram_notifications import (
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


class KeyCaptureDialog(QDialog):
    """Modal dialog that captures a single key press."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Capture Key")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Press a key to assign it to the action."))
        layout.addWidget(
            QLabel(
                "Press Escape to cancel. Modifier keys such as Shift or Ctrl can be held while pressing the key."
            )
        )
        self._binding: KeyBinding | None = None

    def event(self, event) -> bool:  # type: ignore[override]
        if event.type() == QEvent.ShortcutOverride:
            event.accept()
            return True

        if event.type() == QEvent.KeyPress:
            key_event = cast(QKeyEvent, event)
            key = key_event.key()
            if key in (Qt.Key_Escape, Qt.Key_Cancel):
                self.reject()
                return True
            if key in (
                Qt.Key_Shift,
                Qt.Key_Control,
                Qt.Key_Meta,
                Qt.Key_Alt,
                Qt.Key_AltGr,
                Qt.Key_Super_L,
                Qt.Key_Super_R,
            ):
                return True
            if key == Qt.Key_unknown:
                return True
            self._binding = KeyBinding(
                qt_key=int(key),
                modifiers=keyboard_modifiers_to_int(key_event.modifiers()),
                native_scan_code=native_scan_code_to_int(key_event.nativeScanCode()),
                text=key_event.text(),
            )
            self.accept()
            return True

        return super().event(event)

    def reject(self) -> None:  # type: ignore[override]
        self._binding = None
        super().reject()

    def binding(self) -> KeyBinding | None:
        """Return the captured binding if one was recorded."""

        return self._binding


class KeyBindingListEditor(QWidget):
    """Widget that manages a list of key bindings for a single action."""

    bindings_changed = Signal()

    def __init__(self, bindings: List[KeyBinding], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bindings: List[KeyBinding] = list(bindings)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._list = QListWidget(self)
        layout.addWidget(self._list)

        button_row = QHBoxLayout()
        self._add_button = QPushButton("Add", self)
        self._remove_button = QPushButton("Remove", self)
        button_row.addWidget(self._add_button)
        button_row.addWidget(self._remove_button)
        button_row.addStretch(1)
        layout.addLayout(button_row)

        self._add_button.clicked.connect(self._add_binding)
        self._remove_button.clicked.connect(self._remove_selected)
        self._list.itemSelectionChanged.connect(self._update_buttons)

        self._refresh()

    def bindings(self) -> List[KeyBinding]:
        """Return the list of configured bindings."""

        return list(self._bindings)

    def _refresh(self) -> None:
        self._list.clear()
        for binding in self._bindings:
            self._list.addItem(QListWidgetItem(self._binding_text(binding)))
        self._update_buttons()

    def _update_buttons(self) -> None:
        self._remove_button.setEnabled(bool(self._list.selectedItems()))

    def _add_binding(self) -> None:
        dialog = KeyCaptureDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        binding = dialog.binding()
        if not binding:
            return
        if binding in self._bindings:
            return
        self._bindings.append(binding)
        self._refresh()
        self.bindings_changed.emit()

    def _remove_selected(self) -> None:
        selected = self._list.selectedIndexes()
        if not selected:
            return
        index = selected[0].row()
        if 0 <= index < len(self._bindings):
            del self._bindings[index]
            self._refresh()
            self.bindings_changed.emit()

    def _binding_text(self, binding: KeyBinding) -> str:
        if binding.modifiers:
            sequence = QKeySequence(binding.qt_key | binding.modifiers)
        else:
            sequence = QKeySequence(binding.qt_key)
        sequence_text = sequence.toString(QKeySequence.NativeText)
        if sequence_text:
            if binding.native_scan_code:
                return f"{sequence_text} [physical]"
            return sequence_text
        if binding.text:
            if binding.native_scan_code:
                return f"{binding.text} [physical]"
            return binding.text
        if binding.native_scan_code:
            return f"Scan {binding.native_scan_code} [physical]"
        return f"Key {binding.qt_key}"


class ControlsSettingsWidget(QWidget):
    """Tab that exposes control bindings similar to game key bindings."""

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self._pending_timeout_spin = QDoubleSpinBox(self)
        self._pending_timeout_spin.setLocale(QLocale.c())
        self._pending_timeout_spin.setDecimals(1)
        self._pending_timeout_spin.setRange(0.5, 60.0)
        self._pending_timeout_spin.setSingleStep(0.5)
        self._pending_timeout_spin.setSuffix(" s")
        self._pending_timeout_spin.setValue(
            float(settings.click_to_move.pending_timeout_s)
        )
        layout.addRow(QLabel("Click wait timeout", self), self._pending_timeout_spin)

        self._editors: Dict[str, KeyBindingListEditor] = {}
        for action in CONTROL_ACTIONS:
            bindings = settings.controls.get(action.key, [])
            editor = KeyBindingListEditor(bindings, self)
            self._editors[action.key] = editor
            layout.addRow(QLabel(action.label, self), editor)

    def to_settings(self, settings: Settings) -> None:
        """Write the user changes back into the provided settings container."""

        controls: Dict[str, List[KeyBinding]] = {}
        for key, editor in self._editors.items():
            controls[key] = editor.bindings()
        settings.controls = controls
        settings.click_to_move.pending_timeout_s = float(
            self._pending_timeout_spin.value()
        )


class LoggingSettingsWidget(QWidget):
    """Tab that exposes logging configuration."""

    LEVEL_OPTIONS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    def __init__(self, logging_settings: LoggingSettings, parent: QWidget | None = None) -> None:
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

    def __init__(self, api_settings: ApiSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._enabled_checkbox = QCheckBox("Start with app", self)
        self._enabled_checkbox.setChecked(api_settings.enabled)
        layout.addRow(QLabel("API server", self), self._enabled_checkbox)

        self._host_edit = QLineEdit(self)
        self._host_edit.setText(api_settings.host)
        self._host_edit.setPlaceholderText("127.0.0.1")
        layout.addRow(QLabel("Host", self), self._host_edit)

        self._port_spin = QSpinBox(self)
        self._port_spin.setRange(1, 65535)
        self._port_spin.setValue(int(api_settings.port))
        layout.addRow(QLabel("Port", self), self._port_spin)

        self._enabled_checkbox.toggled.connect(self._update_enabled_state)
        self._update_enabled_state(self._enabled_checkbox.isChecked())

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

    def __init__(self, telegram_settings: TelegramSettings, parent: QWidget | None = None) -> None:
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
            self._token_edit.setPlaceholderText(
                f"Provided by {TELEGRAM_BOT_TOKEN_ENV}"
            )
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
        self._status_label.setText(message if success else f"Telegram test failed: {message}")

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
        self._test_button.setEnabled((not linking) and bool(self._linked_chat_id.strip()))
        self._forget_button.setEnabled((not linking) and bool(self._linked_chat_id.strip()))

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


class FeedrateGroupEditor(QWidget):
    """Editor for a single feedrate group including presets and default selection."""

    def __init__(
        self,
        title: str,
        units: str,
        group: FeedrateGroup,
        fallback_presets: Sequence[float],
        fallback_default: float,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._fallback_presets = [float(value) for value in fallback_presets]
        self._fallback_presets.sort()
        self._fallback_default = float(fallback_default)
        self._presets: List[float] = sorted(group.presets) if group.presets else list(self._fallback_presets)
        if not self._presets:
            self._presets = list(self._fallback_presets)
        self._default_value: float = group.default
        if not self._presets:
            self._default_value = self._fallback_default

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        title_label = QLabel(title, self)
        title_label.setStyleSheet("font-weight: 600;")
        layout.addWidget(title_label)

        units_label = QLabel(f"Preset feed rates for {units} (positive values):", self)
        units_label.setWordWrap(True)
        layout.addWidget(units_label)

        self._list = QListWidget(self)
        self._list.setSelectionMode(QListWidget.SingleSelection)
        layout.addWidget(self._list)

        input_row = QHBoxLayout()
        self._value_edit = QLineEdit(self)
        self._value_edit.setPlaceholderText("Enter feed rate (e.g. 1)")
        validator = QDoubleValidator(1.0, 1000000.0, 6, self)
        validator.setNotation(QDoubleValidator.StandardNotation)
        self._value_edit.setValidator(validator)
        input_row.addWidget(self._value_edit)

        self._add_button = QPushButton("Add", self)
        input_row.addWidget(self._add_button)
        layout.addLayout(input_row)

        action_row = QHBoxLayout()
        self._remove_button = QPushButton("Remove Selected", self)
        action_row.addWidget(self._remove_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        default_row = QHBoxLayout()
        default_row.addWidget(QLabel("Default preset:", self))
        self._default_combo = QComboBox(self)
        default_row.addWidget(self._default_combo)
        default_row.addStretch(1)
        layout.addLayout(default_row)

        self._add_button.clicked.connect(self._add_value)
        self._remove_button.clicked.connect(self._remove_selected)
        self._list.itemSelectionChanged.connect(self._update_buttons)
        self._default_combo.currentIndexChanged.connect(self._on_default_changed)

        self._refresh_list()
        self._update_buttons()

    def group(self) -> FeedrateGroup:
        """Return the configured feedrate group."""

        presets = list(self._presets)
        if not presets:
            presets = list(self._fallback_presets)
        default_value = self._default_value
        if default_value <= 0:
            default_value = presets[0] if presets else self._fallback_default
        return FeedrateGroup(presets=presets, default=default_value)

    def _refresh_list(self) -> None:
        self._presets.sort()
        self._list.clear()
        for value in self._presets:
            self._list.addItem(self._format_value(value))
        if self._default_value <= 0:
            self._default_value = (
                self._presets[0] if self._presets else self._fallback_default
            )
        self._refresh_default_options()

    def _refresh_default_options(self) -> None:
        values = list(self._presets) if self._presets else list(self._fallback_presets)
        if not values:
            values = [self._fallback_default]
        if self._default_value > 0 and not any(
            math.isclose(self._default_value, value, rel_tol=1e-9, abs_tol=1e-9)
            for value in values
        ):
            values.append(self._default_value)
            values.sort()
        texts = [self._format_value(value) for value in values]
        desired_text = self._format_value(self._default_value)

        self._default_choices = values
        self._default_combo.blockSignals(True)
        self._default_combo.clear()
        self._default_combo.addItems(texts)
        if desired_text in texts:
            self._default_combo.setCurrentText(desired_text)
        else:
            self._default_combo.setCurrentIndex(0)
            self._default_value = values[0]
        self._default_combo.blockSignals(False)

    def _update_buttons(self) -> None:
        self._remove_button.setEnabled(bool(self._list.selectedItems()))

    def _add_value(self) -> None:
        text = self._value_edit.text().strip()
        if not text:
            return
        try:
            value = float(text)
        except ValueError:
            return
        if value < 1.0:
            return
        if any(math.isclose(value, existing, rel_tol=1e-9, abs_tol=1e-9) for existing in self._presets):
            return
        insert_index = len(self._presets)
        for index, existing in enumerate(self._presets):
            if value < existing:
                insert_index = index
                break
        self._presets.insert(insert_index, value)
        self._value_edit.clear()
        self._refresh_list()

    def _remove_selected(self) -> None:
        selected_indexes = self._list.selectedIndexes()
        if not selected_indexes:
            return
        for index in sorted((idx.row() for idx in selected_indexes), reverse=True):
            if 0 <= index < len(self._presets):
                del self._presets[index]
        self._refresh_list()

    def _on_default_changed(self) -> None:
        index = self._default_combo.currentIndex()
        if 0 <= index < len(self._default_choices):
            self._default_value = self._default_choices[index]

    @staticmethod
    def _format_value(value: float) -> str:
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text or "0"


class FeedrateSettingsWidget(QWidget):
    """Tab that lets users manage linear feed rates."""

    DEFAULT_PRESETS = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0)
    DEFAULT_VALUE = 1.0

    def __init__(self, feedrates: FeedrateSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._linear_editor = FeedrateGroupEditor(
            "Linear feed rates",
            "mm/min",
            feedrates.linear,
            self.DEFAULT_PRESETS,
            self.DEFAULT_VALUE,
            self,
        )
        layout.addWidget(self._linear_editor)

        layout.addStretch(1)

    def to_settings(self, settings: Settings) -> None:
        """Write the configured presets back to the settings container."""

        settings.feedrates = FeedrateSettings(
            linear=self._linear_editor.group(),
            rotary=settings.feedrates.rotary,
        )


class JogSettingsWidget(QWidget):
    """Tab that exposes joystick jog distances."""

    def __init__(self, jog_settings: JogSettings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._linear_distance_spin = QDoubleSpinBox(self)
        self._linear_distance_spin.setLocale(QLocale.c())
        self._linear_distance_spin.setDecimals(3)
        self._linear_distance_spin.setRange(0.001, 1000.0)
        self._linear_distance_spin.setSingleStep(1.0)
        self._linear_distance_spin.setSuffix(" mm")
        self._linear_distance_spin.setValue(jog_settings.linear_distance_mm)
        layout.addRow(QLabel("Linear jog distance", self), self._linear_distance_spin)

        self._motion_safety_checkbox = QCheckBox("Disable motion safety", self)
        self._motion_safety_checkbox.setChecked(jog_settings.motion_safety_disabled)
        self._motion_safety_checkbox.setToolTip(
            "Allows joystick movement when needle state is unknown/down and skips axis limit checks."
        )
        layout.addRow(self._motion_safety_checkbox)

    def to_settings(self, settings: Settings) -> None:
        """Persist the widget state into the provided settings object."""

        jog = settings.jog.clone()
        jog.linear_distance_mm = self._linear_distance_spin.value()
        jog.motion_safety_disabled = self._motion_safety_checkbox.isChecked()
        settings.jog = jog


class MeasurementSettingsWidget(QWidget):
    """Tab that exposes measurement-instrument settings."""

    def __init__(
        self,
        calibration_settings: NeedleCalibrationSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._meter_type_combo = QComboBox(self)
        for meter_type in LCR_METER_TYPES:
            self._meter_type_combo.addItem(
                LCR_METER_TYPE_LABELS.get(meter_type, meter_type),
                meter_type,
            )
        meter_type_index = self._meter_type_combo.findData(
            calibration_settings.meter_type
        )
        if meter_type_index >= 0:
            self._meter_type_combo.setCurrentIndex(meter_type_index)
        layout.addRow(QLabel("Instrument type", self), self._meter_type_combo)

        self._visa_resource_edit = QLineEdit(self)
        self._visa_resource_edit.setPlaceholderText("COM4 or ASRL4::INSTR")
        self._visa_resource_edit.setText(calibration_settings.visa_resource)
        layout.addRow(QLabel("GW Instek resource", self), self._visa_resource_edit)

        self._keithley_source_resource_edit = QLineEdit(self)
        self._keithley_source_resource_edit.setPlaceholderText("GPIB2::1::INSTR")
        self._keithley_source_resource_edit.setText(
            calibration_settings.keithley_source_resource
        )
        layout.addRow(
            QLabel("Keithley 2400 resource", self),
            self._keithley_source_resource_edit,
        )

        self._keithley_voltmeter_resource_edit = QLineEdit(self)
        self._keithley_voltmeter_resource_edit.setPlaceholderText("GPIB2::2::INSTR")
        self._keithley_voltmeter_resource_edit.setText(
            calibration_settings.keithley_voltmeter_resource
        )
        layout.addRow(
            QLabel("Keithley 2182A resource", self),
            self._keithley_voltmeter_resource_edit,
        )

        self._function_combo = QComboBox(self)
        self._function_combo.addItems(LCR_MEASUREMENT_FUNCTIONS)
        self._function_combo.setCurrentText(calibration_settings.measurement_function)
        layout.addRow(QLabel("Measurement function", self), self._function_combo)

        self._range_mode_combo = QComboBox(self)
        for mode in LCR_RANGE_MODES:
            label = "Fixed range (HOLD)" if mode == "HOLD" else "Auto range"
            self._range_mode_combo.addItem(label, mode)
        range_index = self._range_mode_combo.findData(calibration_settings.range_mode)
        if range_index >= 0:
            self._range_mode_combo.setCurrentIndex(range_index)
        layout.addRow(QLabel("Range mode", self), self._range_mode_combo)

        self._impedance_range_spin = QSpinBox(self)
        self._impedance_range_spin.setRange(0, 8)
        self._impedance_range_spin.setSingleStep(1)
        self._impedance_range_spin.setValue(calibration_settings.impedance_range)
        layout.addRow(QLabel("Impedance range", self), self._impedance_range_spin)

        self._dcr_range_spin = QSpinBox(self)
        self._dcr_range_spin.setRange(0, 8)
        self._dcr_range_spin.setSingleStep(1)
        self._dcr_range_spin.setValue(calibration_settings.dcr_range)
        layout.addRow(QLabel("DCR range", self), self._dcr_range_spin)

        self._frequency_spin = QDoubleSpinBox(self)
        self._frequency_spin.setDecimals(3)
        self._frequency_spin.setRange(10.0, 300_000.0)
        self._frequency_spin.setSingleStep(100.0)
        self._frequency_spin.setSuffix(" Hz")
        self._frequency_spin.setValue(calibration_settings.frequency_hz)
        layout.addRow(QLabel("AC frequency", self), self._frequency_spin)

        self._level_mode_combo = QComboBox(self)
        self._level_mode_combo.addItem("Voltage", "VOLTAGE")
        self._level_mode_combo.addItem("Current", "CURRENT")
        level_mode_index = self._level_mode_combo.findData(
            calibration_settings.level_mode
        )
        if level_mode_index >= 0:
            self._level_mode_combo.setCurrentIndex(level_mode_index)
        layout.addRow(QLabel("AC level mode", self), self._level_mode_combo)

        self._voltage_level_spin = QDoubleSpinBox(self)
        self._voltage_level_spin.setDecimals(4)
        self._voltage_level_spin.setRange(0.01, 2.0)
        self._voltage_level_spin.setSingleStep(0.01)
        self._voltage_level_spin.setSuffix(" V")
        self._voltage_level_spin.setValue(calibration_settings.voltage_level_v)
        layout.addRow(QLabel("AC voltage level", self), self._voltage_level_spin)

        self._current_level_spin = QDoubleSpinBox(self)
        self._current_level_spin.setDecimals(6)
        self._current_level_spin.setRange(0.0001, 0.02)
        self._current_level_spin.setSingleStep(0.0001)
        self._current_level_spin.setSuffix(" A")
        self._current_level_spin.setValue(calibration_settings.current_level_a)
        layout.addRow(QLabel("AC current level", self), self._current_level_spin)

        self._source_resistance_combo = QComboBox(self)
        for resistance_ohm in LCR_SOURCE_RESISTANCES_OHM:
            self._source_resistance_combo.addItem(f"{resistance_ohm} ohm", resistance_ohm)
        source_index = self._source_resistance_combo.findData(
            calibration_settings.source_resistance_ohm
        )
        if source_index >= 0:
            self._source_resistance_combo.setCurrentIndex(source_index)
        layout.addRow(QLabel("Source resistance", self), self._source_resistance_combo)

        self._aperture_combo = QComboBox(self)
        self._aperture_combo.addItems(LCR_APERTURE_RATES)
        self._aperture_combo.setCurrentText(calibration_settings.aperture_rate)
        layout.addRow(QLabel("Measurement speed", self), self._aperture_combo)

        self._averages_spin = QSpinBox(self)
        self._averages_spin.setRange(1, 256)
        self._averages_spin.setSingleStep(1)
        self._averages_spin.setValue(calibration_settings.aperture_averages)
        layout.addRow(QLabel("Averaging factor", self), self._averages_spin)

        self._trigger_source_combo = QComboBox(self)
        self._trigger_source_combo.addItems(LCR_TRIGGER_SOURCES)
        self._trigger_source_combo.setCurrentText(calibration_settings.trigger_source)
        layout.addRow(QLabel("Trigger source", self), self._trigger_source_combo)

        self._trigger_delay_spin = QDoubleSpinBox(self)
        self._trigger_delay_spin.setDecimals(3)
        self._trigger_delay_spin.setRange(0.0, 60.0)
        self._trigger_delay_spin.setSingleStep(0.01)
        self._trigger_delay_spin.setSuffix(" s")
        self._trigger_delay_spin.setValue(calibration_settings.trigger_delay_s)
        layout.addRow(QLabel("Trigger delay", self), self._trigger_delay_spin)

        self._bias_checkbox = QCheckBox("Enable DC bias", self)
        self._bias_checkbox.setChecked(calibration_settings.bias_enabled)
        layout.addRow(self._bias_checkbox)

        self._bias_level_spin = QDoubleSpinBox(self)
        self._bias_level_spin.setDecimals(3)
        self._bias_level_spin.setRange(-2.5, 2.5)
        self._bias_level_spin.setSingleStep(0.01)
        self._bias_level_spin.setSuffix(" V")
        self._bias_level_spin.setValue(calibration_settings.bias_level_v)
        layout.addRow(QLabel("DC bias level", self), self._bias_level_spin)

        self._monitor1_combo = QComboBox(self)
        self._monitor1_combo.addItems(LCR_MONITOR_PARAMETERS)
        self._monitor1_combo.setCurrentText(calibration_settings.monitor1)
        layout.addRow(QLabel("Monitor 1", self), self._monitor1_combo)

        self._monitor2_combo = QComboBox(self)
        self._monitor2_combo.addItems(LCR_MONITOR_PARAMETERS)
        self._monitor2_combo.setCurrentText(calibration_settings.monitor2)
        layout.addRow(QLabel("Monitor 2", self), self._monitor2_combo)

        self._alc_checkbox = QCheckBox("Enable ALC", self)
        self._alc_checkbox.setChecked(calibration_settings.alc_enabled)
        layout.addRow(self._alc_checkbox)

        self._short_threshold_spin = QDoubleSpinBox(self)
        self._short_threshold_spin.setDecimals(3)
        self._short_threshold_spin.setRange(0.0, 1_000_000.0)
        self._short_threshold_spin.setSingleStep(0.5)
        self._short_threshold_spin.setSuffix(" ohm")
        self._short_threshold_spin.setValue(calibration_settings.short_threshold_ohm)
        layout.addRow(QLabel("Short threshold", self), self._short_threshold_spin)

        self._poll_interval_spin = QDoubleSpinBox(self)
        self._poll_interval_spin.setDecimals(0)
        self._poll_interval_spin.setRange(50, 10_000)
        self._poll_interval_spin.setSingleStep(50)
        self._poll_interval_spin.setSuffix(" ms")
        self._poll_interval_spin.setValue(calibration_settings.poll_interval_ms)
        layout.addRow(QLabel("Polling interval", self), self._poll_interval_spin)

        self._function_combo.currentTextChanged.connect(
            lambda _text: self._update_lcr_control_state()
        )
        self._meter_type_combo.currentIndexChanged.connect(
            lambda _index: self._update_lcr_control_state()
        )
        self._range_mode_combo.currentIndexChanged.connect(
            lambda _index: self._update_lcr_control_state()
        )
        self._level_mode_combo.currentIndexChanged.connect(
            lambda _index: self._update_lcr_control_state()
        )
        self._bias_checkbox.toggled.connect(
            lambda _checked: self._update_lcr_control_state()
        )
        self._update_lcr_control_state()

    def _update_lcr_control_state(self) -> None:
        meter_type = str(self._meter_type_combo.currentData() or LCR_METER_TYPE_GWINSTEK)
        gwinstek_meter = meter_type == LCR_METER_TYPE_GWINSTEK
        function = self._function_combo.currentText()
        range_mode = str(self._range_mode_combo.currentData() or "HOLD")
        level_mode = str(self._level_mode_combo.currentData() or "VOLTAGE")
        fixed_range = range_mode == "HOLD"
        dcr_mode = function == "DCR"
        self._visa_resource_edit.setEnabled(gwinstek_meter)
        self._keithley_source_resource_edit.setEnabled(not gwinstek_meter)
        self._keithley_voltmeter_resource_edit.setEnabled(not gwinstek_meter)
        self._function_combo.setEnabled(gwinstek_meter)
        self._range_mode_combo.setEnabled(gwinstek_meter)
        self._aperture_combo.setEnabled(gwinstek_meter)
        self._averages_spin.setEnabled(gwinstek_meter)
        self._trigger_source_combo.setEnabled(gwinstek_meter)
        self._trigger_delay_spin.setEnabled(gwinstek_meter)
        self._dcr_range_spin.setEnabled(gwinstek_meter and fixed_range and dcr_mode)
        self._impedance_range_spin.setEnabled(
            gwinstek_meter and fixed_range and not dcr_mode
        )
        self._frequency_spin.setEnabled(gwinstek_meter and not dcr_mode)
        self._level_mode_combo.setEnabled(gwinstek_meter and not dcr_mode)
        self._voltage_level_spin.setEnabled(
            gwinstek_meter and not dcr_mode and level_mode == "VOLTAGE"
        )
        self._current_level_spin.setEnabled(
            gwinstek_meter and not dcr_mode and level_mode == "CURRENT"
        )
        self._source_resistance_combo.setEnabled(gwinstek_meter and not dcr_mode)
        self._bias_checkbox.setEnabled(gwinstek_meter and not dcr_mode)
        self._bias_level_spin.setEnabled(
            gwinstek_meter and not dcr_mode and self._bias_checkbox.isChecked()
        )
        self._monitor1_combo.setEnabled(gwinstek_meter and not dcr_mode)
        self._monitor2_combo.setEnabled(gwinstek_meter and not dcr_mode)
        self._alc_checkbox.setEnabled(gwinstek_meter and not dcr_mode)

    def to_settings(self, settings: Settings) -> None:
        """Persist the widget state into the provided settings object."""

        needle_settings = settings.needle_calibration.clone()
        needle_settings.meter_type = str(
            self._meter_type_combo.currentData() or LCR_METER_TYPE_GWINSTEK
        )
        needle_settings.visa_resource = self._visa_resource_edit.text().strip()
        needle_settings.keithley_source_resource = (
            self._keithley_source_resource_edit.text().strip()
        )
        needle_settings.keithley_voltmeter_resource = (
            self._keithley_voltmeter_resource_edit.text().strip()
        )
        needle_settings.measurement_function = self._function_combo.currentText()
        needle_settings.range_mode = str(
            self._range_mode_combo.currentData() or "HOLD"
        )
        needle_settings.auto_range_enabled = (
            str(self._range_mode_combo.currentData() or "") == "AUTO"
        )
        needle_settings.impedance_range = int(self._impedance_range_spin.value())
        needle_settings.dcr_range = int(self._dcr_range_spin.value())
        needle_settings.frequency_hz = self._frequency_spin.value()
        needle_settings.level_mode = str(
            self._level_mode_combo.currentData() or "VOLTAGE"
        )
        needle_settings.voltage_level_v = self._voltage_level_spin.value()
        needle_settings.current_level_a = self._current_level_spin.value()
        needle_settings.source_resistance_ohm = int(
            self._source_resistance_combo.currentData() or 30
        )
        needle_settings.aperture_rate = self._aperture_combo.currentText()
        needle_settings.aperture_averages = int(self._averages_spin.value())
        needle_settings.trigger_source = self._trigger_source_combo.currentText()
        needle_settings.trigger_delay_s = self._trigger_delay_spin.value()
        needle_settings.bias_enabled = self._bias_checkbox.isChecked()
        needle_settings.bias_level_v = self._bias_level_spin.value()
        needle_settings.monitor1 = self._monitor1_combo.currentText()
        needle_settings.monitor2 = self._monitor2_combo.currentText()
        needle_settings.alc_enabled = self._alc_checkbox.isChecked()
        needle_settings.short_threshold_ohm = self._short_threshold_spin.value()
        needle_settings.poll_interval_ms = int(self._poll_interval_spin.value())
        settings.needle_calibration = needle_settings


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


class ObjectivesSettingsWidget(QWidget):
    """Tab that exposes objective selection, offsets, and autofocus parameters."""

    def __init__(
        self,
        objectives: ObjectivesSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._objectives = objectives.clone()
        self._active_editor_name = self._objectives.active_name

        layout = QFormLayout(self)
        layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._active_combo = QComboBox(self)
        self._profile_combo = QComboBox(self)
        for name in ordered_objective_names(self._objectives.objectives):
            self._active_combo.addItem(name, name)
            self._profile_combo.addItem(name, name)
        active_index = self._active_combo.findData(self._objectives.active_name)
        if active_index >= 0:
            self._active_combo.setCurrentIndex(active_index)
        profile_index = self._profile_combo.findData(self._active_editor_name)
        if profile_index >= 0:
            self._profile_combo.setCurrentIndex(profile_index)

        self._apply_offsets_checkbox = QCheckBox(
            "Apply saved offset when objective changes",
            self,
        )
        self._apply_offsets_checkbox.setChecked(
            self._objectives.apply_offsets_on_change
        )
        layout.addRow(QLabel("Active objective", self), self._active_combo)
        layout.addRow(self._apply_offsets_checkbox)

        separator = QFrame(self)
        separator.setFrameShape(QFrame.HLine)
        separator.setFrameShadow(QFrame.Sunken)
        layout.addRow(separator)
        layout.addRow(QLabel("Edit objective", self), self._profile_combo)

        self._xy_configured_checkbox = QCheckBox("Use X/Y offset", self)
        self._z_configured_checkbox = QCheckBox("Use Z offset", self)
        self._magnification_spin = self._positive_spin(" x", decimals=2)
        self._x_offset_spin = self._offset_spin(" mm")
        self._y_offset_spin = self._offset_spin(" mm")
        self._z_offset_spin = self._offset_spin(" mm")
        self._autofocus_range_spin = self._positive_spin(" mm", decimals=4)
        self._autofocus_fine_spin = self._positive_spin(" mm", decimals=4)
        self._xy_calibration_status = QLineEdit(self)
        self._xy_calibration_status.setReadOnly(True)

        layout.addRow(QLabel("Magnification", self), self._magnification_spin)
        layout.addRow(self._xy_configured_checkbox)
        layout.addRow(QLabel("X correction", self), self._x_offset_spin)
        layout.addRow(QLabel("Y correction", self), self._y_offset_spin)
        layout.addRow(self._z_configured_checkbox)
        layout.addRow(QLabel("Z correction", self), self._z_offset_spin)
        layout.addRow(QLabel("AF range", self), self._autofocus_range_spin)
        layout.addRow(QLabel("AF fine step", self), self._autofocus_fine_spin)
        layout.addRow(QLabel("Click calibration", self), self._xy_calibration_status)

        self._profile_combo.currentIndexChanged.connect(
            lambda _index: self._on_profile_changed()
        )
        self._load_profile(self._active_editor_name)

    def to_settings(self, settings: Settings) -> None:
        """Persist the widget state into the provided settings object."""

        self._save_active_profile_edits()
        settings.objectives = ObjectivesSettings(
            active_name=str(self._active_combo.currentData() or "X5"),
            apply_offsets_on_change=self._apply_offsets_checkbox.isChecked(),
            objectives={
                key: value.clone()
                for key, value in self._objectives.objectives.items()
            },
        )

    def _on_profile_changed(self) -> None:
        self._save_active_profile_edits()
        self._active_editor_name = str(self._profile_combo.currentData() or "X5")
        self._load_profile(self._active_editor_name)

    def _load_profile(self, name: str) -> None:
        profile = self._objectives.objectives.get(name)
        if profile is None:
            profile = ObjectiveCalibrationSettings(name=name)
            self._objectives.objectives[name] = profile
        self._xy_configured_checkbox.setChecked(profile.xy_offset_configured)
        self._z_configured_checkbox.setChecked(profile.z_offset_configured)
        self._magnification_spin.setValue(profile.magnification)
        self._x_offset_spin.setValue(profile.xy_offset_x_mm)
        self._y_offset_spin.setValue(profile.xy_offset_y_mm)
        self._z_offset_spin.setValue(profile.z_offset_mm)
        self._autofocus_range_spin.setValue(profile.autofocus_range_mm)
        self._autofocus_fine_spin.setValue(profile.autofocus_fine_step_mm)
        status = "Configured" if profile.xy_calibration_configured else "Not configured"
        self._xy_calibration_status.setText(status)

    def _save_active_profile_edits(self) -> None:
        name = self._active_editor_name
        profile = self._objectives.objectives.get(name)
        if profile is None:
            profile = ObjectiveCalibrationSettings(name=name)
        updated = profile.clone()
        updated.name = name
        updated.magnification = self._magnification_spin.value()
        updated.xy_offset_configured = self._xy_configured_checkbox.isChecked()
        updated.z_offset_configured = self._z_configured_checkbox.isChecked()
        updated.xy_offset_x_mm = self._x_offset_spin.value()
        updated.xy_offset_y_mm = self._y_offset_spin.value()
        updated.z_offset_mm = self._z_offset_spin.value()
        updated.autofocus_range_mm = self._autofocus_range_spin.value()
        updated.autofocus_fine_step_mm = self._autofocus_fine_spin.value()
        self._objectives.objectives[name] = updated

    def _offset_spin(self, suffix: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setLocale(QLocale.c())
        spin.setDecimals(4)
        spin.setRange(-100.0, 100.0)
        spin.setSingleStep(0.01)
        spin.setSuffix(suffix)
        return spin

    def _positive_spin(self, suffix: str, *, decimals: int) -> QDoubleSpinBox:
        spin = QDoubleSpinBox(self)
        spin.setLocale(QLocale.c())
        spin.setDecimals(decimals)
        spin.setRange(0.0001, 10000.0)
        spin.setSingleStep(0.01)
        spin.setSuffix(suffix)
        return spin


class CoordinateSystemSettingsWidget(QWidget):
    """Tab that exposes WCS startup mode."""

    def __init__(
        self,
        coordinate_settings: CoordinateSystemSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        root_layout = QVBoxLayout(self)
        root_layout.setContentsMargins(0, 0, 0, 0)

        mode_layout = QFormLayout()
        mode_layout.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self._position_mode_combo = QComboBox(self)
        self._position_mode_combo.addItem("Relative WCS coordinates", "work")
        self._position_mode_combo.addItem("Absolute machine coordinates", "machine")
        position_index = self._position_mode_combo.findData(
            coordinate_settings.position_mode
        )
        if position_index >= 0:
            self._position_mode_combo.setCurrentIndex(position_index)
        mode_layout.addRow(QLabel("Position mode", self), self._position_mode_combo)

        self._startup_mode_combo = QComboBox(self)
        self._startup_mode_combo.addItem(
            "Follow controller active system", "controller"
        )
        self._startup_mode_combo.addItem(
            "Force selected system on connect", "fixed"
        )
        mode_index = self._startup_mode_combo.findData(
            coordinate_settings.startup_mode
        )
        if mode_index >= 0:
            self._startup_mode_combo.setCurrentIndex(mode_index)
        mode_layout.addRow(QLabel("Coordinate mode", self), self._startup_mode_combo)

        self._preferred_system_combo = QComboBox(self)
        for system in WORK_COORDINATE_SYSTEMS:
            self._preferred_system_combo.addItem(system, system)
        preferred_index = self._preferred_system_combo.findData(
            coordinate_settings.preferred_system
        )
        if preferred_index >= 0:
            self._preferred_system_combo.setCurrentIndex(preferred_index)
        mode_layout.addRow(QLabel("Preferred WCS", self), self._preferred_system_combo)

        mode_widget = QWidget(self)
        mode_widget.setLayout(mode_layout)
        root_layout.addWidget(mode_widget)
        separator = QFrame(self)
        separator.setFrameShape(QFrame.HLine)
        separator.setFrameShadow(QFrame.Sunken)
        root_layout.addWidget(separator)
        root_layout.addStretch(1)
        self._position_mode_combo.currentIndexChanged.connect(
            self._update_mode_hint_state
        )
        self._startup_mode_combo.currentIndexChanged.connect(
            self._update_mode_hint_state
        )
        self._update_mode_hint_state()

    def to_settings(self, settings: Settings) -> None:
        """Persist the widget state into the provided settings object."""

        startup_mode = str(self._startup_mode_combo.currentData() or "controller")
        preferred_system = str(self._preferred_system_combo.currentData() or "G54")
        settings.coordinate_system = CoordinateSystemSettings(
            position_mode=str(self._position_mode_combo.currentData() or "work"),
            startup_mode=startup_mode,
            preferred_system=preferred_system,
        )

    def _update_mode_hint_state(self) -> None:
        fixed_mode = str(self._startup_mode_combo.currentData() or "") == "fixed"
        machine_mode = str(self._position_mode_combo.currentData() or "") == "machine"
        self._preferred_system_combo.setEnabled(not machine_mode)
        if machine_mode:
            self._preferred_system_combo.setToolTip(
                "Unused in absolute machine-coordinate mode."
            )
            return
        if fixed_mode:
            self._preferred_system_combo.setToolTip(
                "This WCS will be sent to the controller on connect."
            )
            return
        self._preferred_system_combo.setToolTip(
            "Controller-selected WCS will be used."
        )


class AxisCalibrationSettingsWidget(QWidget):
    """Tab that controls optional nonlinear axis coordinate calibration."""

    def __init__(
        self,
        axis_a_calibration: AxisACalibrationSettings,
        axis_z_calibration: AxisZCalibrationSettings,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._axis_a_calibration = axis_a_calibration.clone()
        self._axis_z_calibration = axis_z_calibration.clone()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._axis_a_enabled_checkbox = QCheckBox(
            "Use calibrated A-axis coordinate curve",
            self,
        )
        self._axis_a_enabled_checkbox.setChecked(axis_a_calibration.configured)
        self._axis_a_enabled_checkbox.setToolTip(
            "When disabled, A coordinates are sent and displayed as raw GCode."
        )
        layout.addWidget(self._axis_a_enabled_checkbox)
        layout.addLayout(
            self._read_only_field_layout("A curve source", axis_a_calibration.source)
        )
        layout.addLayout(
            self._read_only_field_layout(
                "A fit error",
                (
                    f"RMSE {axis_a_calibration.fit_rmse_mm:.6f} mm, "
                    f"max {axis_a_calibration.fit_max_abs_error_mm:.6f} mm"
                ),
            )
        )

        separator = QFrame(self)
        separator.setFrameShape(QFrame.HLine)
        separator.setFrameShadow(QFrame.Sunken)
        layout.addWidget(separator)

        self._axis_z_enabled_checkbox = QCheckBox(
            "Use calibrated Z-axis coordinate curve",
            self,
        )
        self._axis_z_enabled_checkbox.setChecked(axis_z_calibration.configured)
        self._axis_z_enabled_checkbox.setToolTip(
            "When disabled, Z coordinates are sent and displayed as raw GCode."
        )
        layout.addWidget(self._axis_z_enabled_checkbox)
        layout.addLayout(
            self._read_only_field_layout("Z curve source", axis_z_calibration.source)
        )
        layout.addLayout(
            self._read_only_field_layout(
                "Z fit error",
                (
                    f"RMSE {axis_z_calibration.fit_rmse_mm:.6f} mm, "
                    f"max {axis_z_calibration.fit_max_abs_error_mm:.6f} mm"
                ),
            )
        )

        layout.addStretch(1)

    def to_settings(self, settings: Settings) -> None:
        """Persist enabled/disabled state while preserving curve parameters."""

        axis_a = self._axis_a_calibration.clone()
        axis_a.configured = self._axis_a_enabled_checkbox.isChecked()
        axis_z = self._axis_z_calibration.clone()
        axis_z.configured = self._axis_z_enabled_checkbox.isChecked()
        settings.axis_a_calibration = axis_a
        settings.axis_z_calibration = axis_z

    def _read_only_field_layout(self, label: str, text: str) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel(label, self))
        field = QLineEdit(self)
        field.setReadOnly(True)
        field.setText(text)
        field.setCursorPosition(0)
        row.addWidget(field, 1)
        return row


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
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setModal(True)
        self.setWindowFlag(Qt.WindowContextHelpButtonHint, False)
        self._settings = settings.clone()
        self._applied_once = False
        self._camera_tab: CameraSettingsWidget | None = None

        root_layout = QVBoxLayout(self)
        self._tabs = QTabWidget(self)
        root_layout.addWidget(self._tabs)

        self._controls_tab = ControlsSettingsWidget(self._settings, self)
        self._api_tab = ApiSettingsWidget(self._settings.api, self)
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
        self._axis_calibration_tab = AxisCalibrationSettingsWidget(
            self._settings.axis_a_calibration,
            self._settings.axis_z_calibration,
            self,
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
        self._tabs.addTab(self._objectives_tab, "Objectives")
        self._tabs.addTab(self._axis_calibration_tab, "Axis Calibration")
        self._tabs.addTab(self._measurement_tab, "Measurement")
        self._tabs.addTab(self._needles_tab, "Needles")
        self._tabs.addTab(self._logging_tab, "Logging")
        if initial_tab:
            for index in range(self._tabs.count()):
                if self._tabs.tabText(index).lower() == initial_tab.lower():
                    self._tabs.setCurrentIndex(index)
                    break
        self._tabs.currentChanged.connect(self._on_tab_changed)
        self._refresh_camera_tab_if_current()

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Apply | QDialogButtonBox.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        apply_button = buttons.button(QDialogButtonBox.Apply)
        if apply_button is not None:
            apply_button.clicked.connect(self._apply_without_closing)
        root_layout.addWidget(buttons)

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
        self._collect_settings()
        self._applied_once = True
        self.settings_applied.emit(self._settings.clone())
        self._telegram_tab.shutdown()
        super().accept()

    def _apply_without_closing(self) -> None:
        self._collect_settings()
        self._applied_once = True
        self.settings_applied.emit(self._settings.clone())

    def _collect_settings(self) -> None:
        self._controls_tab.to_settings(self._settings)
        self._api_tab.to_settings(self._settings)
        self._telegram_tab.to_settings(self._settings)
        self._jog_tab.to_settings(self._settings)
        self._coordinate_system_tab.to_settings(self._settings)
        self._objectives_tab.to_settings(self._settings)
        self._axis_calibration_tab.to_settings(self._settings)
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

