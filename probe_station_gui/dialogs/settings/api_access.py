"""Local API server and key-management settings controls."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from probe_station_gui.api.keys import (
    API_PERMISSION_CAMERA_READ,
    API_PERMISSION_CAMERA_WRITE,
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
from probe_station_gui.settings.manager import ApiSettings, Settings
from probe_station_gui.shared.wheel_guard import GuardedSpinBox as QSpinBox


class ApiSettingsWidget(QWidget):
    """Tab that exposes the local control API settings."""

    PERMISSION_COLUMNS = {
        3: API_PERMISSION_STAGE_READ,
        4: API_PERMISSION_STAGE_WRITE,
        5: API_PERMISSION_ROUTE_READ,
        6: API_PERMISSION_ROUTE_MEASURE,
        7: API_PERMISSION_CAMERA_READ,
        8: API_PERMISSION_CAMERA_WRITE,
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
        self._keys_tree.setColumnCount(11)
        self._keys_tree.setHeaderLabels(
            [
                "User / key",
                "Key",
                "Enabled",
                "Stage read",
                "Stage write",
                "Route read",
                "Route measure",
                "Camera read",
                "Camera write",
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


__all__ = ["ApiSettingsWidget"]
