"""Default settings file normalization."""

from __future__ import annotations

from probe_station_gui.notifications.telegram_settings import (
    TelegramSettings,
    default_telegram_alerts,
)
from probe_station_gui.settings.axis_calibration_config import (
    AxisACalibrationSettings,
    AxisZCalibrationSettings,
)
from probe_station_gui.settings.feedrate_config import parse_feedrate_list
from probe_station_gui.settings.jog_config import JogSettings
from probe_station_gui.settings.needle_calibration_config import NeedleCalibrationSettings
from probe_station_gui.settings.objective_config import (
    ObjectivesSettings,
    default_objective,
    normalize_objective_name,
)
from probe_station_gui.settings.oscillation_config import OscillationSettings
from probe_station_gui.settings.sections import (
    ApiSettings,
    ClickToMoveSettings,
    CoordinateSystemSettings,
    ExposurePolicySettings,
)


DEFAULT_LINEAR_FEEDRATE_PRESETS: tuple[float, ...] = (
    1.0,
    3.0,
    10.0,
    30.0,
    100.0,
    300.0,
)
DEFAULT_ROTARY_FEEDRATE_PRESETS: tuple[float, ...] = (
    1.0,
    3.0,
    10.0,
    30.0,
    90.0,
    360.0,
)
DEFAULT_FEEDRATE_DEFAULT = 1.0
MIN_FEEDRATE_MM_MIN = 1.0
LINEAR_GROUP = "linear"
ROTARY_GROUP = "rotary"


def normalize_default_settings_data(
    raw_data: object,
    *,
    log_path: str,
) -> dict:
    """Return bundled defaults updated to the current settings JSON shape."""

    data = raw_data if isinstance(raw_data, dict) else {}
    _ensure_logging_section(data, log_path=log_path)
    _ensure_dict_section(data, "api", ApiSettings().to_dict())
    _ensure_camera_exposure_section(data)
    _ensure_telegram_section(data)
    _ensure_feedrates_section(data)
    _ensure_dict_section(data, "jog", JogSettings().to_dict())
    _ensure_dict_section(data, "click_to_move", ClickToMoveSettings().to_dict())
    _ensure_dict_section(data, "oscillation", OscillationSettings().to_dict())
    _ensure_needle_section(data)
    _ensure_dict_section(data, "axis_a_calibration", AxisACalibrationSettings().to_dict())
    _ensure_dict_section(data, "axis_z_calibration", AxisZCalibrationSettings().to_dict())
    _ensure_dict_section(data, "coordinate_system", CoordinateSystemSettings().to_dict())
    _ensure_objectives_section(data)
    if not isinstance(data.get("design_last_directory"), str):
        data["design_last_directory"] = ""
    return data


def _ensure_logging_section(data: dict, *, log_path: str) -> None:
    section = data.get("logging")
    if not isinstance(section, dict):
        data["logging"] = {"level": "INFO", "file": log_path}
        return
    section["file"] = log_path


def _ensure_dict_section(data: dict, key: str, defaults: dict) -> dict:
    section = data.get(key)
    if not isinstance(section, dict):
        section = dict(defaults)
        data[key] = section
        return section
    for default_key, default_value in defaults.items():
        section.setdefault(default_key, default_value)
    return section


def _ensure_camera_exposure_section(data: dict) -> None:
    camera = _ensure_dict_section(data, "camera", {})
    defaults = ExposurePolicySettings().to_dict()
    exposure = camera.get("exposure")
    if not isinstance(exposure, dict):
        camera["exposure"] = defaults
        return
    for key, value in defaults.items():
        exposure.setdefault(key, value)


def _ensure_telegram_section(data: dict) -> None:
    section = data.get("telegram")
    if not isinstance(section, dict):
        data["telegram"] = TelegramSettings().to_dict()
        return
    for key, value in TelegramSettings().to_dict().items():
        section.setdefault(key, value)
    section.pop("bot_token", None)
    alerts = section.get("alerts")
    if not isinstance(alerts, dict):
        section["alerts"] = default_telegram_alerts()
        return
    for key, enabled in default_telegram_alerts().items():
        alerts.setdefault(key, enabled)


def _ensure_feedrates_section(data: dict) -> None:
    section = data.get("feedrates")
    legacy_presets = data.get("feedrate_presets")
    if not isinstance(section, dict):
        legacy_raw_present = bool(legacy_presets)
        linear_presets = parse_feedrate_list(
            legacy_presets,
            fallback=DEFAULT_LINEAR_FEEDRATE_PRESETS,
            min_feedrate=MIN_FEEDRATE_MM_MIN,
        )
        rotary_presets = (
            list(linear_presets)
            if legacy_raw_present
            else list(DEFAULT_ROTARY_FEEDRATE_PRESETS)
        )
        data["feedrates"] = {
            LINEAR_GROUP: {
                "presets": linear_presets,
                "default": DEFAULT_FEEDRATE_DEFAULT,
            },
            ROTARY_GROUP: {
                "presets": rotary_presets,
                "default": DEFAULT_FEEDRATE_DEFAULT,
            },
        }
        return
    section.setdefault(
        LINEAR_GROUP,
        {
            "presets": list(DEFAULT_LINEAR_FEEDRATE_PRESETS),
            "default": DEFAULT_FEEDRATE_DEFAULT,
        },
    )
    section.setdefault(
        ROTARY_GROUP,
        {
            "presets": list(DEFAULT_ROTARY_FEEDRATE_PRESETS),
            "default": DEFAULT_FEEDRATE_DEFAULT,
        },
    )
    data["feedrates"] = section


def _ensure_needle_section(data: dict) -> None:
    section = _ensure_dict_section(
        data,
        "needle_calibration",
        NeedleCalibrationSettings().to_dict(),
    )
    for key in ("chip_position", "stone_position"):
        bookmark = section.get(key)
        if not isinstance(bookmark, dict):
            bookmark = {}
            section[key] = bookmark
        bookmark.setdefault("x_mm", 0.0)
        bookmark.setdefault("y_mm", 0.0)
        bookmark.setdefault("z_mm", 0.0)
        bookmark.setdefault("configured", False)


def _ensure_objectives_section(data: dict) -> None:
    section = data.get("objectives")
    if not isinstance(section, dict):
        data["objectives"] = ObjectivesSettings().to_dict()
        return
    defaults = ObjectivesSettings().to_dict()
    section.setdefault("active_name", defaults["active_name"])
    section.setdefault(
        "apply_offsets_on_change",
        defaults["apply_offsets_on_change"],
    )
    raw_profiles = section.get("objectives")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raw_profiles = dict(defaults["objectives"])
        section["objectives"] = raw_profiles
    default_profiles = defaults["objectives"]
    if not isinstance(default_profiles, dict):
        return
    for name, stored in list(raw_profiles.items()):
        normalized_name = normalize_objective_name(name)
        profile = default_profiles.get(normalized_name)
        if not isinstance(profile, dict):
            profile = default_objective(normalized_name).to_dict()
        if isinstance(stored, dict):
            for key, value in profile.items():
                stored.setdefault(key, value)
