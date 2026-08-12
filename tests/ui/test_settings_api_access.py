from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from probe_station_gui.api.keys import (
    API_PERMISSION_CAMERA_READ,
    API_PERMISSION_STAGE_WRITE,
    ApiKeyStore,
    masked_api_key,
)
from probe_station_gui.dialogs.settings.api_access import ApiSettingsWidget
from probe_station_gui.settings.manager import ApiSettings, Settings


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


def _group_names(widget: ApiSettingsWidget) -> list[str]:
    tree = widget._keys_tree
    return [
        tree.topLevelItem(index).text(0) for index in range(tree.topLevelItemCount())
    ]


def test_api_owner_groups_masks_and_renders_permissions(
    app: QApplication,
    tmp_path,
) -> None:
    store = ApiKeyStore(tmp_path / "api-keys.json")
    _secret_beta, beta = store.create_key(user_name="Zed", key_name="beta")
    _secret_alpha, alpha = store.create_key(
        user_name="Zed",
        key_name="Alpha",
        permissions={
            API_PERMISSION_CAMERA_READ: False,
            API_PERMISSION_STAGE_WRITE: True,
        },
    )
    store.create_key(user_name="alice", key_name="bench")

    widget = ApiSettingsWidget(ApiSettings(), api_key_store=store)

    assert widget.__class__.__module__ == (
        "probe_station_gui.dialogs.settings.api_access"
    )
    assert _group_names(widget) == ["alice", "Zed"]
    zed = widget._keys_tree.topLevelItem(1)
    assert [zed.child(index).text(0) for index in range(zed.childCount())] == [
        "Alpha",
        "beta",
    ]
    alpha_item = zed.child(0)
    assert alpha_item.text(1) == masked_api_key(alpha.key_prefix, alpha.key_suffix)
    assert alpha_item.checkState(4) == Qt.Checked
    assert alpha_item.checkState(7) == Qt.Unchecked
    assert zed.child(1).text(1) == masked_api_key(beta.key_prefix, beta.key_suffix)
    widget.deleteLater()


def test_api_owner_refresh_does_not_write_key_state(
    app: QApplication,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ApiKeyStore(tmp_path / "api-keys.json")
    store.create_key(user_name="operator", key_name="bench")
    writes: list[tuple[tuple[object, ...], dict[str, object]]] = []
    original_update = store.update_key

    def record_update(*args: object, **kwargs: object):
        writes.append((args, kwargs))
        return original_update(*args, **kwargs)

    monkeypatch.setattr(store, "update_key", record_update)
    widget = ApiSettingsWidget(ApiSettings(), api_key_store=store)

    widget._refresh_api_keys()

    assert writes == []
    widget.deleteLater()


def test_api_owner_check_changes_are_persisted_immediately(
    app: QApplication,
    tmp_path,
) -> None:
    store = ApiKeyStore(tmp_path / "api-keys.json")
    _secret, created = store.create_key(user_name="operator", key_name="bench")
    widget = ApiSettingsWidget(ApiSettings(), api_key_store=store)
    key_item = widget._keys_tree.topLevelItem(0).child(0)

    key_item.setCheckState(2, Qt.Unchecked)
    key_item.setCheckState(4, Qt.Checked)

    persisted = store.list_keys()[0]
    assert persisted.id == created.id
    assert persisted.enabled is False
    assert persisted.permissions[API_PERMISSION_STAGE_WRITE] is True
    widget.deleteLater()


def test_api_owner_create_rename_and_delete_are_immediate(
    app: QApplication,
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ApiKeyStore(tmp_path / "api-keys.json")
    widget = ApiSettingsWidget(ApiSettings(), api_key_store=store)
    answers: Iterator[tuple[str, bool]] = iter(
        [("operator", True), ("bench", True), ("engineer", True), ("probe", True)]
    )
    monkeypatch.setattr(
        QInputDialog, "getText", lambda *_args, **_kwargs: next(answers)
    )
    monkeypatch.setattr(QMessageBox, "information", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *_args, **_kwargs: QMessageBox.Yes
    )

    widget._create_key_button.click()

    records = store.list_keys()
    assert [(record.user_name, record.key_name) for record in records] == [
        ("operator", "bench")
    ]
    assert QApplication.clipboard().text().startswith("psk_")
    widget._keys_tree.setCurrentItem(widget._keys_tree.topLevelItem(0))
    widget._rename_user_button.click()
    assert store.list_keys()[0].user_name == "engineer"

    key_item = widget._keys_tree.topLevelItem(0).child(0)
    widget._keys_tree.setCurrentItem(key_item)
    widget._rename_key_button.click()
    assert store.list_keys()[0].key_name == "probe"

    key_item = widget._keys_tree.topLevelItem(0).child(0)
    widget._keys_tree.setCurrentItem(key_item)
    widget._delete_key_button.click()
    assert store.list_keys() == []
    widget.deleteLater()


def test_api_owner_collects_server_draft_with_blank_host_fallback(
    app: QApplication,
) -> None:
    widget = ApiSettingsWidget(ApiSettings(enabled=True, host="example", port=8123))
    widget._host_edit.setText("  ")
    widget._port_spin.setValue(9000)
    settings = Settings()

    widget.to_settings(settings)

    assert settings.api == ApiSettings(enabled=True, host="127.0.0.1", port=9000)
    widget.deleteLater()
