from __future__ import annotations

import json
import logging

from probe_station_gui.settings.manager import Settings, SettingsManager
from probe_station_gui.settings.sections import ExposurePolicySettings


def manager_from_raw(raw: object) -> Settings:
    manager = SettingsManager.__new__(SettingsManager)
    manager._logger = logging.getLogger(__name__)
    return manager._settings_from_raw(raw)


def test_missing_exposure_policy_migrates_to_software_auto() -> None:
    settings = manager_from_raw({})

    assert settings.exposure_policy.to_dict() == {
        "auto_enabled": True,
        "engine": "software",
    }


def test_invalid_engine_normalizes_to_software() -> None:
    settings = manager_from_raw(
        {"camera": {"exposure": {"auto_enabled": False, "engine": "invalid"}}}
    )

    assert settings.exposure_policy.engine == "software"
    assert settings.exposure_policy.auto_enabled is False


def test_engine_choice_normalizes_case_and_whitespace() -> None:
    settings = manager_from_raw(
        {"camera": {"exposure": {"engine": " Camera "}}}
    )

    assert settings.exposure_policy.engine == "camera"


def test_exposure_policy_settings_clone_and_application_serialization_are_independent() -> None:
    settings = ExposurePolicySettings(auto_enabled=False, engine="camera")
    clone = settings.clone()

    clone.auto_enabled = True

    assert settings.auto_enabled is False
    assert clone.to_dict() == {"auto_enabled": True, "engine": "camera"}
    assert Settings(exposure_policy=settings).to_dict()["camera"]["exposure"] == {
        "auto_enabled": False,
        "engine": "camera",
    }


def test_settings_manager_returns_independent_exposure_policy_configuration() -> None:
    manager = SettingsManager.__new__(SettingsManager)
    manager._settings = Settings()

    configuration = manager.exposure_policy_configuration()
    configuration.auto_enabled = False

    assert manager.settings.exposure_policy.auto_enabled is True


def test_settings_manager_persists_exposure_policy_configuration(tmp_path) -> None:
    manager = SettingsManager.__new__(SettingsManager)
    manager._settings = Settings()
    manager._config_dir = tmp_path
    manager._config_path = tmp_path / "settings.json"
    manager._logger = logging.getLogger(__name__)
    manager.apply = lambda: None

    manager.set_exposure_policy_configuration(
        ExposurePolicySettings(auto_enabled=False, engine="camera")
    )

    persisted = json.loads(manager._config_path.read_text(encoding="utf-8"))
    assert persisted["camera"]["exposure"] == {
        "auto_enabled": False,
        "engine": "camera",
    }
