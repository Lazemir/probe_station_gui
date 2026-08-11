from __future__ import annotations

import logging

from probe_station_gui.settings.controls_config import CONTROL_ACTIONS
from probe_station_gui.settings.document import Settings, SettingsDocumentCodec
from probe_station_gui.settings.sections import ExposurePolicySettings


def _codec(tmp_path) -> SettingsDocumentCodec:
    return SettingsDocumentCodec(
        default_log_path=tmp_path / "probe-station-gui.log",
        logger=logging.getLogger(__name__),
    )


def test_settings_clone_and_json_document_are_detached(tmp_path) -> None:
    codec = _codec(tmp_path)
    action_key = CONTROL_ACTIONS[0].key
    settings = codec.decode(
        {
            "controls": {
                action_key: [
                    {
                        "qt_key": 68,
                        "modifiers": 0,
                        "native_scan_code": 32,
                        "text": "d",
                    }
                ]
            },
            "camera": {
                "exposure": {"auto_enabled": False, "engine": "camera"}
            },
        }
    )
    assert isinstance(settings, Settings)

    clone = settings.clone()
    document = settings.to_dict()
    clone.controls[action_key].clear()
    clone.exposure_policy.auto_enabled = True
    document["controls"][action_key][0]["text"] = "changed"
    document["camera"]["exposure"]["engine"] = "software"

    assert settings.controls[action_key][0].text == "d"
    assert settings.exposure_policy == ExposurePolicySettings(
        auto_enabled=False,
        engine="camera",
    )
    assert codec.decode(settings.to_dict()).to_dict() == settings.to_dict()


def test_codec_assembles_defaults_and_preserves_payload_shapes(tmp_path) -> None:
    codec = _codec(tmp_path)

    settings = codec.decode(
        {
            "logging": {"level": "debug", "file": ""},
            "api": {"enabled": False, "host": " 0.0.0.0 ", "port": "9876"},
            "camera": {
                "exposure": {"auto_enabled": False, "engine": " Camera "}
            },
        }
    )

    assert settings.logging.level == "DEBUG"
    assert settings.logging.file == str(tmp_path / "probe-station-gui.log")
    assert settings.api.to_dict() == {
        "enabled": False,
        "host": "0.0.0.0",
        "port": 9876,
    }
    assert settings.exposure_policy.to_dict() == {
        "auto_enabled": False,
        "engine": "camera",
    }
    assert set(settings.controls) == {action.key for action in CONTROL_ACTIONS}
    assert settings.to_dict()["camera"] == {
        "exposure": {"auto_enabled": False, "engine": "camera"}
    }


def test_codec_migrates_legacy_feedrates_and_normalizes_runtime_values(tmp_path) -> None:
    codec = _codec(tmp_path)
    decoded = codec.decode(
        {
            "feedrate_presets": [0.1, 1.0, 3.0],
            "jog": {
                "manual_axis": "b",
                "manual_axis_feedrate_mm_min": "0.1",
            },
            "click_to_move": {"pending_timeout_s": 999},
        }
    )

    assert decoded.feedrates.linear.presets == [1.0, 3.0]
    assert decoded.feedrates.linear.default == 1.0
    assert decoded.jog.manual_axis == "B"
    assert decoded.jog.manual_axis_feedrate_mm_min == 1.0
    assert decoded.click_to_move.pending_timeout_s == 60.0

    decoded.api.port = -1
    decoded.exposure_policy.engine = "invalid"
    decoded.design_last_directory = "  C:/designs  "
    normalized = codec.normalize(decoded)

    assert normalized.api.port == 8765
    assert normalized.exposure_policy.engine == "software"
    assert normalized.design_last_directory == "C:/designs"


def test_codec_preserves_unrecognized_software_coordinate_document(tmp_path) -> None:
    raw_root = {
        "schema_version": 2,
        "future_selection_policy": {"scope": "operator"},
    }

    settings = _codec(tmp_path).decode({"software_coordinates": raw_root})

    assert settings.software_coordinates.degraded
    assert settings.software_coordinates.materialization_blocked
    assert settings.to_dict()["software_coordinates"] == raw_root


def test_control_binding_projection_filters_cyrillic_and_fills_missing_defaults(
    tmp_path,
) -> None:
    codec = _codec(tmp_path)
    positive_key = CONTROL_ACTIONS[0].key
    negative_key = CONTROL_ACTIONS[1].key
    settings = codec.decode(
        {
            "controls": {
                positive_key: [
                    {"qt_key": 68, "modifiers": 0, "text": "d"},
                    {"qt_key": 68, "modifiers": 0, "text": "д"},
                ],
                negative_key: [],
            }
        }
    )

    projected = codec.control_bindings(settings)

    assert [binding.text for binding in projected[positive_key]] == ["d"]
    assert projected[negative_key] == []
    assert set(projected) == {action.key for action in CONTROL_ACTIONS}
