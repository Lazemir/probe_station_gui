"""Application settings document model and JSON codec."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List

from probe_station_gui.notifications.telegram import (
    load_global_bot_token,
    save_global_bot_token,
)
from probe_station_gui.notifications.telegram_settings import (
    TelegramSettings,
    parse_telegram_alerts,
)
from probe_station_gui.settings.axis_calibration_config import (
    AxisCalibrationSettings,
    default_axis_calibrations,
    parse_axis_calibrations,
)
from probe_station_gui.settings.controls_config import (
    CONTROL_ACTIONS,
    ControlAction,
    KeyBinding,
)
from probe_station_gui.settings.feedrate_config import (
    FeedrateSettings,
    feedrate_group_from_config,
    normalise_feedrate_settings,
    parse_feedrate_groups,
)
from probe_station_gui.settings.jog_config import (
    JogSettings,
    JogSettingsDefaults,
    parse_jog_settings,
)
from probe_station_gui.settings.needle_calibration_config import (
    NeedleCalibrationSettings,
    parse_needle_calibration_preferences,
)
from probe_station_gui.settings.objective_config import (
    ObjectivesSettings,
    parse_objectives_settings,
)
from probe_station_gui.settings.oscillation_config import (
    OscillationSettings,
    OscillationSettingsDefaults,
    parse_oscillation_settings,
)
from probe_station_gui.settings.precision_approach import (
    PrecisionApproachSettings,
    parse_precision_approach_settings,
)
from probe_station_gui.settings.section_parsing import (
    parse_api_settings,
    parse_coordinate_system_settings,
    parse_logging_settings,
)
from probe_station_gui.settings.sections import (
    ApiSettings,
    ClickToMoveSettings,
    CoordinateSystemSettings,
    ExposurePolicySettings,
    LoggingSettings,
    WORK_COORDINATE_SYSTEMS,
)
from probe_station_gui.settings.software_coordinates import (
    SoftwareCoordinateSettings,
    parse_software_coordinate_settings,
)
from probe_station_gui.settings.value_parsing import (
    coerce_bool,
    finite_float,
    normalise_choice,
)


@dataclass
class Settings:
    """Detached model for one complete application settings document."""

    controls: Dict[str, List[KeyBinding]] = field(default_factory=dict)
    logging: LoggingSettings = field(default_factory=LoggingSettings)
    api: ApiSettings = field(default_factory=ApiSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    feedrates: FeedrateSettings = field(default_factory=FeedrateSettings)
    oscillation: OscillationSettings = field(default_factory=OscillationSettings)
    jog: JogSettings = field(default_factory=JogSettings)
    click_to_move: ClickToMoveSettings = field(default_factory=ClickToMoveSettings)
    needle_calibration: NeedleCalibrationSettings = field(
        default_factory=NeedleCalibrationSettings
    )
    axis_calibrations: dict[str, AxisCalibrationSettings] = field(
        default_factory=default_axis_calibrations
    )
    coordinate_system: CoordinateSystemSettings = field(
        default_factory=CoordinateSystemSettings
    )
    software_coordinates: SoftwareCoordinateSettings = field(
        default_factory=SoftwareCoordinateSettings
    )
    objectives: ObjectivesSettings = field(default_factory=ObjectivesSettings)
    precision_approach: PrecisionApproachSettings = field(
        default_factory=PrecisionApproachSettings
    )
    design_last_directory: str = ""
    exposure_policy: ExposurePolicySettings = field(
        default_factory=ExposurePolicySettings
    )

    def clone(self) -> Settings:
        """Return a deep, independently mutable settings document."""

        return Settings(
            controls={key: list(value) for key, value in self.controls.items()},
            logging=self.logging.clone(),
            api=self.api.clone(),
            exposure_policy=self.exposure_policy.clone(),
            telegram=self.telegram.clone(),
            feedrates=self.feedrates.clone(),
            oscillation=self.oscillation.clone(),
            jog=self.jog.clone(),
            click_to_move=self.click_to_move.clone(),
            needle_calibration=self.needle_calibration.clone(),
            axis_calibrations={
                axis: calibration.clone()
                for axis, calibration in self.axis_calibrations.items()
            },
            coordinate_system=self.coordinate_system.clone(),
            software_coordinates=self.software_coordinates.clone(),
            objectives=self.objectives.clone(),
            precision_approach=self.precision_approach.clone(),
            design_last_directory=self.design_last_directory,
        )

    def to_dict(self) -> dict:
        """Encode the complete stable JSON payload."""

        return {
            "controls": {
                key: [binding.to_dict() for binding in bindings]
                for key, bindings in self.controls.items()
            },
            "logging": self.logging.to_dict(),
            "api": self.api.to_dict(),
            "camera": {"exposure": self.exposure_policy.to_dict()},
            "telegram": self.telegram.to_dict(),
            "feedrates": {
                "linear": {
                    "presets": self.feedrates.linear.presets,
                    "default": self.feedrates.linear.default,
                },
                "rotary": {
                    "presets": self.feedrates.rotary.presets,
                    "default": self.feedrates.rotary.default,
                },
            },
            "oscillation": self.oscillation.to_dict(),
            "jog": self.jog.to_dict(),
            "click_to_move": self.click_to_move.to_dict(),
            "needle_calibration": self.needle_calibration.to_dict(),
            "axis_calibrations": {
                axis: calibration.to_dict()
                for axis, calibration in self.axis_calibrations.items()
            },
            "coordinate_system": self.coordinate_system.to_dict(),
            "software_coordinates": self.software_coordinates.to_dict(),
            "objectives": self.objectives.to_dict(),
            "precision_approach": self.precision_approach.to_dict(),
            "design_last_directory": self.design_last_directory,
        }


class SettingsDocumentCodec:
    """Decode, migrate, validate, and normalize application settings."""

    EXPOSURE_POLICY_ENGINES = ("software", "camera")
    MIN_FEEDRATE_MM_MIN = 1.0
    DEFAULT_LINEAR_FEEDRATE_PRESETS = (1.0, 3.0, 10.0, 30.0, 100.0, 300.0)
    DEFAULT_ROTARY_FEEDRATE_PRESETS = (1.0, 3.0, 10.0, 30.0, 90.0, 360.0)
    DEFAULT_FEEDRATE_DEFAULT = 1.0
    DEFAULT_OSCILLATION_MODE = "X"
    DEFAULT_OSCILLATION_AMPLITUDE_MM = 0.5
    DEFAULT_OSCILLATION_FEEDRATE_MM_MIN = 120.0
    DEFAULT_OSCILLATION_TURNS_PER_SWEEP = 3.0
    DEFAULT_JOG_MODE = "jog"
    DEFAULT_LINEAR_JOG_DISTANCE_MM = 25.0
    DEFAULT_ROTARY_JOG_DISTANCE_DEG = 5.0
    DEFAULT_MOTION_SAFETY_DISABLED = False
    DEFAULT_MANUAL_AXIS = "A"
    DEFAULT_MANUAL_AXIS_DISTANCE_MM = 1.0
    DEFAULT_MANUAL_AXIS_MODE = "G91"
    DEFAULT_MANUAL_AXIS_FEEDRATE_MM_MIN = 1.0
    DEFAULT_FOCUS_FEEDRATE_MM_MIN = 1.0
    DEFAULT_FOCUS_STEP_FEEDRATE_MM_MIN = 1.0
    DEFAULT_NEEDLES_STEP_FEEDRATE_MM_MIN = 1.0
    DEFAULT_TURNTABLE_FEEDRATE_MM_MIN = 1.0
    DEFAULT_TURNTABLE_STEP_FEEDRATE_MM_MIN = 1.0
    DEFAULT_CLICK_TO_MOVE_PENDING_TIMEOUT_S = 8.0
    MIN_CLICK_TO_MOVE_PENDING_TIMEOUT_S = 0.5
    MAX_CLICK_TO_MOVE_PENDING_TIMEOUT_S = 60.0
    MANUAL_AXIS_MODES = ("G91", "G90")
    MANUAL_JOG_AXES = ("X", "Y", "Z", "A", "B", "C")
    DEFAULT_POSITION_MODE = "work"
    DEFAULT_COORDINATE_STARTUP_MODE = "controller"
    DEFAULT_COORDINATE_SYSTEM = "G54"
    LINEAR_GROUP = "linear"
    ROTARY_GROUP = "rotary"
    CYRILLIC_PATTERN = re.compile(r"[\u0400-\u04FF]")

    def __init__(
        self,
        *,
        default_log_path: str | Path,
        logger: logging.Logger | None = None,
    ) -> None:
        self._default_log_path = str(default_log_path)
        self._logger = logger or logging.getLogger(__name__)

    def decode(self, raw: object) -> Settings:
        """Build the current settings model from one raw JSON value."""

        controls = self._load_controls(raw)
        software_coordinate_section_present = bool(
            isinstance(raw, dict) and "software_coordinates" in raw
        )
        logging_settings = LoggingSettings(
            **parse_logging_settings(self._raw_section(raw, "logging", default={}))
        )
        if not logging_settings.file:
            logging_settings.file = self._default_log_path
            self._logger.debug(
                "Log file path missing in settings; defaulting to %s",
                self._default_log_path,
            )
        feedrates = self._parse_feedrates(
            self._raw_section(raw, "feedrates"),
            self._raw_section(raw, "feedrate_presets"),
        )
        return Settings(
            controls=controls,
            logging=logging_settings,
            api=self._parse_api(self._raw_section(raw, "api")),
            exposure_policy=self._parse_exposure_policy(
                self._raw_section(raw, "camera")
            ),
            telegram=self._parse_telegram(self._raw_section(raw, "telegram")),
            feedrates=feedrates,
            oscillation=self._parse_oscillation(
                self._raw_section(raw, "oscillation")
            ),
            jog=self._parse_jog(self._raw_section(raw, "jog")),
            click_to_move=self._parse_click_to_move(
                self._raw_section(raw, "click_to_move")
            ),
            needle_calibration=parse_needle_calibration_preferences(
                self._raw_section(raw, "needle_calibration"),
                min_feedrate_mm_min=self.MIN_FEEDRATE_MM_MIN,
            ),
            axis_calibrations=parse_axis_calibrations(
                self._raw_section(raw, "axis_calibrations")
            ),
            coordinate_system=self._parse_coordinate_system(
                self._raw_section(raw, "coordinate_system")
            ),
            software_coordinates=parse_software_coordinate_settings(
                self._raw_section(raw, "software_coordinates"),
                section_present=software_coordinate_section_present,
            ),
            objectives=parse_objectives_settings(self._raw_section(raw, "objectives")),
            precision_approach=parse_precision_approach_settings(
                self._raw_section(raw, "precision_approach")
            ),
            design_last_directory=self._design_last_directory(raw),
        )

    def normalize(self, settings: Settings) -> Settings:
        """Return a detached document with every runtime value validated."""

        clone = settings.clone()
        clone.api = self._parse_api(clone.api.to_dict())
        clone.exposure_policy = self._parse_exposure_policy(
            {"exposure": clone.exposure_policy.to_dict()}
        )
        clone.telegram = self._parse_telegram(clone.telegram.to_dict())
        clone.feedrates = normalise_feedrate_settings(
            clone.feedrates,
            linear_defaults=self.DEFAULT_LINEAR_FEEDRATE_PRESETS,
            rotary_defaults=self.DEFAULT_ROTARY_FEEDRATE_PRESETS,
            default_feedrate=self.DEFAULT_FEEDRATE_DEFAULT,
            min_feedrate=self.MIN_FEEDRATE_MM_MIN,
        )
        clone.oscillation = self._parse_oscillation(clone.oscillation.to_dict())
        clone.jog = self._parse_jog(clone.jog.to_dict())
        clone.click_to_move = self._parse_click_to_move(
            clone.click_to_move.to_dict()
        )
        clone.needle_calibration = parse_needle_calibration_preferences(
            clone.needle_calibration.to_dict(),
            min_feedrate_mm_min=self.MIN_FEEDRATE_MM_MIN,
        )
        clone.axis_calibrations = parse_axis_calibrations(
            {
                axis: calibration.to_dict()
                for axis, calibration in clone.axis_calibrations.items()
            }
        )
        clone.software_coordinates = parse_software_coordinate_settings(
            clone.software_coordinates.to_dict(),
            section_present=True,
        )
        clone.objectives = parse_objectives_settings(clone.objectives.to_dict())
        clone.precision_approach = parse_precision_approach_settings(
            clone.precision_approach.to_dict()
        )
        clone.design_last_directory = clone.design_last_directory.strip()
        return clone

    def control_bindings(self, settings: Settings) -> Dict[str, List[KeyBinding]]:
        """Project bindings with missing application defaults restored."""

        controls = {key: list(value) for key, value in settings.controls.items()}
        for action in CONTROL_ACTIONS:
            controls.setdefault(action.key, self._default_control_bindings(action))
        return controls

    def _load_controls(self, raw: object) -> Dict[str, List[KeyBinding]]:
        controls_raw = self._raw_section(raw, "controls", default={})
        controls: Dict[str, List[KeyBinding]] = {}
        for key, values in controls_raw.items():
            bindings: List[KeyBinding] = []
            if isinstance(values, Iterable):
                for value in values:
                    if isinstance(value, dict):
                        binding = KeyBinding.from_dict(value)
                        if self._should_keep_control_binding(binding):
                            bindings.append(binding)
            controls[key] = bindings
        raw_control_keys = set(controls_raw) if isinstance(controls_raw, dict) else set()
        for action in CONTROL_ACTIONS:
            if action.key not in controls:
                controls[action.key] = (
                    self._default_control_bindings(action)
                    if action.key not in raw_control_keys
                    else []
                )
        return controls

    def _parse_api(self, raw_api: object) -> ApiSettings:
        defaults = ApiSettings()
        return ApiSettings(
            **parse_api_settings(
                raw_api,
                default_enabled=defaults.enabled,
                default_host=defaults.host,
                default_port=defaults.port,
            )
        )

    def _parse_exposure_policy(self, raw_camera: object) -> ExposurePolicySettings:
        settings = ExposurePolicySettings()
        raw_exposure = raw_camera.get("exposure") if isinstance(raw_camera, dict) else None
        if not isinstance(raw_exposure, dict):
            return settings
        settings.auto_enabled = coerce_bool(
            raw_exposure.get("auto_enabled", settings.auto_enabled),
            default=settings.auto_enabled,
        )
        settings.engine = normalise_choice(
            raw_exposure.get("engine"),
            choices=self.EXPOSURE_POLICY_ENGINES,
            default=settings.engine,
        )
        return settings

    def _parse_telegram(self, raw_telegram: object) -> TelegramSettings:
        settings = TelegramSettings()
        if not isinstance(raw_telegram, dict):
            return settings
        settings.enabled = coerce_bool(
            raw_telegram.get("enabled", settings.enabled),
            default=settings.enabled,
        )
        legacy_token = raw_telegram.get("bot_token", "")
        legacy_token = (
            str(legacy_token).strip()
            if isinstance(legacy_token, (str, int))
            else ""
        )
        if legacy_token and not load_global_bot_token():
            try:
                save_global_bot_token(legacy_token)
            except OSError as exc:
                self._logger.warning(
                    "Failed to migrate Telegram bot token to global settings: %s",
                    exc,
                )
                settings.bot_token = legacy_token
        for attr in ("bot_username", "chat_id", "chat_title", "linked_at_utc"):
            raw_value = raw_telegram.get(attr, getattr(settings, attr))
            if isinstance(raw_value, (str, int)):
                setattr(settings, attr, str(raw_value).strip())
        settings.bot_username = settings.bot_username.lstrip("@")
        settings.alerts = parse_telegram_alerts(raw_telegram.get("alerts"))
        return settings

    def _parse_feedrates(
        self,
        raw_feedrates: object,
        legacy_presets: object,
    ) -> FeedrateSettings:
        linear_config, rotary_config = parse_feedrate_groups(
            raw_feedrates,
            legacy_presets,
            linear_group=self.LINEAR_GROUP,
            rotary_group=self.ROTARY_GROUP,
            linear_defaults=self.DEFAULT_LINEAR_FEEDRATE_PRESETS,
            rotary_defaults=self.DEFAULT_ROTARY_FEEDRATE_PRESETS,
            default_feedrate=self.DEFAULT_FEEDRATE_DEFAULT,
            min_feedrate=self.MIN_FEEDRATE_MM_MIN,
        )
        return FeedrateSettings(
            linear=feedrate_group_from_config(linear_config),
            rotary=feedrate_group_from_config(rotary_config),
        )

    def _parse_jog(self, raw_jog: object) -> JogSettings:
        config = parse_jog_settings(
            raw_jog,
            JogSettingsDefaults(
                mode=self.DEFAULT_JOG_MODE,
                linear_distance_mm=self.DEFAULT_LINEAR_JOG_DISTANCE_MM,
                rotary_distance_deg=self.DEFAULT_ROTARY_JOG_DISTANCE_DEG,
                motion_safety_disabled=self.DEFAULT_MOTION_SAFETY_DISABLED,
                manual_axis=self.DEFAULT_MANUAL_AXIS,
                manual_axis_distance_mm=self.DEFAULT_MANUAL_AXIS_DISTANCE_MM,
                manual_axis_mode=self.DEFAULT_MANUAL_AXIS_MODE,
                manual_axis_feedrate_mm_min=self.DEFAULT_MANUAL_AXIS_FEEDRATE_MM_MIN,
                focus_feedrate_mm_min=self.DEFAULT_FOCUS_FEEDRATE_MM_MIN,
                focus_step_feedrate_mm_min=self.DEFAULT_FOCUS_STEP_FEEDRATE_MM_MIN,
                needles_step_feedrate_mm_min=self.DEFAULT_NEEDLES_STEP_FEEDRATE_MM_MIN,
                turntable_feedrate_mm_min=self.DEFAULT_TURNTABLE_FEEDRATE_MM_MIN,
                turntable_step_feedrate_mm_min=(
                    self.DEFAULT_TURNTABLE_STEP_FEEDRATE_MM_MIN
                ),
                min_feedrate_mm_min=self.MIN_FEEDRATE_MM_MIN,
                manual_axes=self.MANUAL_JOG_AXES,
                manual_axis_modes=self.MANUAL_AXIS_MODES,
            ),
        )
        return JogSettings(
            mode=config.mode,
            linear_distance_mm=config.linear_distance_mm,
            rotary_distance_deg=config.rotary_distance_deg,
            motion_safety_disabled=config.motion_safety_disabled,
            manual_axis=config.manual_axis,
            manual_axis_distance_mm=config.manual_axis_distance_mm,
            manual_axis_mode=config.manual_axis_mode,
            manual_axis_feedrate_mm_min=config.manual_axis_feedrate_mm_min,
            focus_feedrate_mm_min=config.focus_feedrate_mm_min,
            focus_step_feedrate_mm_min=config.focus_step_feedrate_mm_min,
            needles_step_feedrate_mm_min=config.needles_step_feedrate_mm_min,
            turntable_feedrate_mm_min=config.turntable_feedrate_mm_min,
            turntable_step_feedrate_mm_min=config.turntable_step_feedrate_mm_min,
        )

    def _parse_click_to_move(self, raw_click_to_move: object) -> ClickToMoveSettings:
        timeout_s = self.DEFAULT_CLICK_TO_MOVE_PENDING_TIMEOUT_S
        if isinstance(raw_click_to_move, dict):
            timeout_s = finite_float(
                raw_click_to_move.get("pending_timeout_s", timeout_s),
                default=self.DEFAULT_CLICK_TO_MOVE_PENDING_TIMEOUT_S,
            )
        timeout_s = max(self.MIN_CLICK_TO_MOVE_PENDING_TIMEOUT_S, timeout_s)
        timeout_s = min(self.MAX_CLICK_TO_MOVE_PENDING_TIMEOUT_S, timeout_s)
        return ClickToMoveSettings(pending_timeout_s=float(timeout_s))

    def _parse_oscillation(self, raw_oscillation: object) -> OscillationSettings:
        config = parse_oscillation_settings(
            raw_oscillation,
            OscillationSettingsDefaults(
                mode=self.DEFAULT_OSCILLATION_MODE,
                amplitude_mm=self.DEFAULT_OSCILLATION_AMPLITUDE_MM,
                feedrate_mm_min=self.DEFAULT_OSCILLATION_FEEDRATE_MM_MIN,
                turns_per_sweep=self.DEFAULT_OSCILLATION_TURNS_PER_SWEEP,
            ),
        )
        return OscillationSettings(
            mode=config.mode,
            amplitude_mm=config.amplitude_mm,
            feedrate_mm_min=config.feedrate_mm_min,
            turns_per_sweep=config.turns_per_sweep,
        )

    def _parse_coordinate_system(self, raw_coordinate_system: object) -> CoordinateSystemSettings:
        return CoordinateSystemSettings(
            **parse_coordinate_system_settings(
                raw_coordinate_system,
                default_position_mode=self.DEFAULT_POSITION_MODE,
                default_startup_mode=self.DEFAULT_COORDINATE_STARTUP_MODE,
                default_coordinate_system=self.DEFAULT_COORDINATE_SYSTEM,
                work_coordinate_systems=WORK_COORDINATE_SYSTEMS,
            )
        )

    @classmethod
    def _should_keep_control_binding(cls, binding: KeyBinding) -> bool:
        text = (binding.text or "").strip()
        return not text or cls.CYRILLIC_PATTERN.search(text) is None

    @staticmethod
    def _default_control_bindings(action: ControlAction) -> List[KeyBinding]:
        if action.default_qt_key <= 0:
            return []
        return [
            KeyBinding(
                qt_key=int(action.default_qt_key),
                modifiers=int(action.default_modifiers),
                text=str(action.default_text),
            )
        ]

    @staticmethod
    def _raw_section(raw: object, key: str, *, default: object = None) -> object:
        if isinstance(raw, dict):
            return raw.get(key, default)
        return default

    @staticmethod
    def _design_last_directory(raw: object) -> str:
        if not isinstance(raw, dict):
            return ""
        value = raw.get("design_last_directory", "")
        return value.strip() if isinstance(value, str) else ""


__all__ = ["Settings", "SettingsDocumentCodec"]
