from __future__ import annotations

import os

import pytest


os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QDialog, QWidget

import probe_station_gui.dialogs.settings.telegram as telegram_module
import probe_station_gui.dialogs.settings_dialog as settings_dialog_module
from probe_station_gui.dialogs.settings_dialog import SettingsDialog
from probe_station_gui.settings.manager import Settings


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def local_telegram_files(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    for module in (settings_dialog_module, telegram_module):
        monkeypatch.setattr(
            module,
            "global_telegram_token_path",
            lambda: tmp_path / "telegram-bot.json",
            raising=False,
        )
        monkeypatch.setattr(
            module,
            "load_global_bot_token",
            lambda: "",
            raising=False,
        )
        monkeypatch.setattr(
            module,
            "save_global_bot_token",
            lambda _token: tmp_path / "telegram-bot.json",
            raising=False,
        )
        monkeypatch.setattr(
            module,
            "can_manage_global_bot_token",
            lambda: True,
            raising=False,
        )


class _CollectTab:
    def __init__(self, name: str, calls: list[str]) -> None:
        self._name = name
        self._calls = calls

    def to_settings(self, _settings: object) -> None:
        self._calls.append(self._name)


class _CoordinateCollectTab(_CollectTab):
    def apply_availability(self) -> tuple[bool, str]:
        return True, ""

    def show_validation_message(self, _message: str) -> None:
        raise AssertionError("valid coordinate draft must not show a message")


class _TelegramCollectTab(_CollectTab):
    def __init__(self, calls: list[str]) -> None:
        super().__init__("telegram", calls)
        self.shutdown_calls = 0

    def shutdown(self) -> None:
        self.shutdown_calls += 1


class _CameraApplyProbe:
    def __init__(self, *, started: bool, on_apply=None) -> None:
        self._started = started
        self._on_apply = on_apply
        self.apply_calls = 0

    def apply_pending_settings(self) -> bool:
        self.apply_calls += 1
        if self._on_apply is not None:
            self._on_apply()
        return self._started


def _replace_collect_tabs(
    dialog: SettingsDialog,
    calls: list[str],
) -> _TelegramCollectTab:
    dialog._controls_tab = _CollectTab("controls", calls)
    dialog._api_tab = _CollectTab("api", calls)
    telegram = _TelegramCollectTab(calls)
    dialog._telegram_tab = telegram
    dialog._jog_tab = _CollectTab("jog", calls)
    dialog._coordinate_system_tab = _CoordinateCollectTab("coordinates", calls)
    dialog._objectives_tab = _CollectTab("objectives", calls)
    dialog._axes_tab = _CollectTab("axes", calls)
    dialog._measurement_tab = _CollectTab("measurement", calls)
    dialog._needles_tab = _CollectTab("needles", calls)
    dialog._logging_tab = _CollectTab("logging", calls)
    return telegram


def test_settings_dialog_collects_tabs_and_camera_in_exact_order(
    app: QApplication,
) -> None:
    dialog = SettingsDialog(Settings())
    calls: list[str] = []
    _replace_collect_tabs(dialog, calls)
    camera = _CameraApplyProbe(
        started=True,
        on_apply=lambda: calls.append("camera"),
    )
    dialog._camera_tab = camera

    assert dialog._collect_settings() is True

    assert calls == [
        "controls",
        "api",
        "telegram",
        "jog",
        "coordinates",
        "objectives",
        "axes",
        "measurement",
        "needles",
        "logging",
        "camera",
    ]
    assert dialog._collecting_settings is False
    dialog.deleteLater()


def test_settings_dialog_owns_clones_of_input_and_result(
    app: QApplication,
) -> None:
    original = Settings()
    original.api.host = "original"
    dialog = SettingsDialog(original)
    dialog._api_tab._host_edit.setText("draft")

    dialog._collect_settings()
    result = dialog.result_settings()
    result.api.host = "mutated-result"

    assert original.api.host == "original"
    assert dialog.result_settings().api.host == "draft"
    dialog.reject()
    dialog.deleteLater()


class _LazyCameraTab(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.refresh_calls = 0

    def has_loaded(self) -> bool:
        return self.refresh_calls > 0

    def refresh(self) -> None:
        self.refresh_calls += 1


def test_settings_dialog_refreshes_camera_only_when_its_tab_becomes_current(
    app: QApplication,
) -> None:
    dialog = SettingsDialog(Settings())
    camera = _LazyCameraTab(dialog)
    dialog._camera_tab = camera
    dialog._tabs.addTab(camera, "Camera probe")

    dialog._refresh_camera_tab_if_current()
    assert camera.refresh_calls == 0

    dialog._tabs.setCurrentWidget(camera)

    assert camera.refresh_calls == 1
    dialog.reject()
    dialog.deleteLater()


def test_settings_dialog_defers_synchronous_camera_success_until_after_emission(
    app: QApplication,
) -> None:
    dialog = SettingsDialog(Settings())
    calls: list[str] = []
    telegram = _replace_collect_tabs(dialog, calls)
    dialog._camera_tab = _CameraApplyProbe(
        started=True,
        on_apply=lambda: dialog._on_camera_apply_finished(True),
    )
    emitted_results: list[int] = []
    dialog.settings_applied.connect(
        lambda _settings: emitted_results.append(dialog.result())
    )

    dialog.accept()

    assert emitted_results == [QDialog.Rejected]
    assert dialog.result() == QDialog.Accepted
    assert dialog._camera_apply_busy is False
    assert telegram.shutdown_calls == 1
    dialog.deleteLater()


@pytest.mark.parametrize("success", [False, True])
def test_settings_dialog_emits_before_async_camera_terminal_result(
    app: QApplication,
    success: bool,
) -> None:
    dialog = SettingsDialog(Settings())
    calls: list[str] = []
    telegram = _replace_collect_tabs(dialog, calls)
    dialog._camera_tab = _CameraApplyProbe(started=True)
    emitted: list[Settings] = []
    dialog.settings_applied.connect(emitted.append)

    dialog.accept()

    assert len(emitted) == 1
    assert dialog.result() == QDialog.Rejected
    assert dialog._camera_apply_busy is True
    assert telegram.shutdown_calls == 0

    dialog._on_camera_apply_finished(success)

    assert dialog._camera_apply_busy is False
    assert dialog.result() == (QDialog.Accepted if success else QDialog.Rejected)
    assert telegram.shutdown_calls == (1 if success else 0)
    if not success:
        dialog.reject()
    dialog.deleteLater()


def test_settings_dialog_apply_never_closes_after_camera_success(
    app: QApplication,
) -> None:
    dialog = SettingsDialog(Settings())
    calls: list[str] = []
    telegram = _replace_collect_tabs(dialog, calls)
    dialog._camera_tab = _CameraApplyProbe(started=True)
    emitted: list[Settings] = []
    dialog.settings_applied.connect(emitted.append)

    dialog._apply_without_closing()
    dialog._on_camera_apply_finished(True)

    assert len(emitted) == 1
    assert dialog.result() == QDialog.Rejected
    assert telegram.shutdown_calls == 0
    dialog.reject()
    dialog.deleteLater()


def test_settings_dialog_reject_shuts_down_telegram_owner(
    app: QApplication,
) -> None:
    dialog = SettingsDialog(Settings())
    calls: list[str] = []
    telegram = _replace_collect_tabs(dialog, calls)

    dialog.reject()

    assert telegram.shutdown_calls == 1
    dialog.deleteLater()
