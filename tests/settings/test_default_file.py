import json
from importlib import resources

from probe_station_gui.notifications.telegram_settings import default_telegram_alerts
from probe_station_gui.settings.default_file import normalize_default_settings_data


def test_normalize_default_settings_data_builds_current_shape_from_invalid_input() -> None:
    data = normalize_default_settings_data([], log_path="runtime.log")

    assert data["logging"] == {"level": "INFO", "file": "runtime.log"}
    assert data["api"] == {"enabled": True, "host": "127.0.0.1", "port": 8765}
    assert data["telegram"]["alerts"] == default_telegram_alerts()
    assert data["feedrates"]["linear"]["presets"] == [1.0, 3.0, 10.0, 30.0, 100.0, 300.0]
    assert data["feedrates"]["rotary"]["presets"] == [1.0, 3.0, 10.0, 30.0, 90.0, 360.0]
    assert data["needle_calibration"]["chip_position"] == {
        "x_mm": 0.0,
        "y_mm": 0.0,
        "z_mm": 0.0,
        "configured": False,
    }
    assert data["design_last_directory"] == ""


def test_normalize_default_settings_data_preserves_legacy_defaults_and_adds_gaps() -> None:
    data = normalize_default_settings_data(
        {
            "logging": {"level": "DEBUG", "file": "bundled.log"},
            "telegram": {
                "enabled": True,
                "bot_token": "legacy-secret",
                "alerts": {"route_started": False},
            },
            "feedrate_presets": [0.1, 5, 5],
            "needle_calibration": {
                "meter_type": "gwinstek_lcr_76200",
                "chip_position": [],
            },
            "objectives": {
                "active_name": "X100",
                "objectives": {"X100": {"name": "X100", "magnification": 100.0}},
            },
        },
        log_path="runtime.log",
    )

    assert data["logging"] == {"level": "DEBUG", "file": "runtime.log"}
    assert "bot_token" not in data["telegram"]
    assert data["telegram"]["alerts"]["route_started"] is False
    assert set(data["telegram"]["alerts"]) == set(default_telegram_alerts())
    assert data["feedrates"]["linear"]["presets"] == [1.0, 5.0]
    assert data["feedrates"]["rotary"]["presets"] == [1.0, 5.0]
    assert data["needle_calibration"]["chip_position"] == {
        "x_mm": 0.0,
        "y_mm": 0.0,
        "z_mm": 0.0,
        "configured": False,
    }
    assert data["needle_calibration"]["stone_position"] == {
        "x_mm": 0.0,
        "y_mm": 0.0,
        "z_mm": 0.0,
        "configured": False,
    }
    profile = data["objectives"]["objectives"]["X100"]
    assert profile["magnification"] == 100.0
    assert profile["xy_offset_configured"] is False
    assert profile["autofocus_range_mm"] == 1.0


def test_bundled_defaults_include_camera_exposure_policy() -> None:
    path = resources.files("probe_station_gui").joinpath("default_settings.json")
    data = json.loads(path.read_text(encoding="utf-8-sig"))

    assert data["camera"]["exposure"] == {
        "auto_enabled": True,
        "engine": "software",
    }
