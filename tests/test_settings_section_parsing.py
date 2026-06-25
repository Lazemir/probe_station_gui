from probe_station_gui.settings_section_parsing import (
    parse_api_settings,
    parse_coordinate_system_settings,
    parse_logging_settings,
)
from probe_station_gui.settings_sections import WORK_COORDINATE_SYSTEMS


def test_parse_logging_settings_uppercases_level_and_keeps_string_file() -> None:
    assert parse_logging_settings({"level": "debug", "file": " app.log "}) == {
        "level": "DEBUG",
        "file": " app.log ",
    }
    assert parse_logging_settings({"file": 123}) == {
        "level": "INFO",
        "file": "",
    }


def test_parse_api_settings_preserves_legacy_bool_and_port_rules() -> None:
    assert parse_api_settings(
        {"enabled": "false", "host": " 127.0.0.1 ", "port": "9000"},
        default_enabled=False,
        default_host="localhost",
        default_port=8000,
    ) == {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 9000,
    }
    assert parse_api_settings(
        {"host": "", "port": 70000},
        default_enabled=True,
        default_host="localhost",
        default_port=8000,
    ) == {
        "enabled": True,
        "host": "localhost",
        "port": 8000,
    }


def test_parse_coordinate_system_settings_normalizes_and_validates_choices() -> None:
    assert parse_coordinate_system_settings(
        {
            "position_mode": " MACHINE ",
            "startup_mode": " fixed ",
            "preferred_system": " g55 ",
        },
        default_position_mode="work",
        default_startup_mode="controller",
        default_coordinate_system="G54",
        work_coordinate_systems=("G54", "G55"),
    ) == {
        "position_mode": "machine",
        "startup_mode": "fixed",
        "preferred_system": "G55",
    }
    assert parse_coordinate_system_settings(
        {
            "position_mode": "bad",
            "startup_mode": "bad",
            "preferred_system": "G99",
        },
        default_position_mode="work",
        default_startup_mode="controller",
        default_coordinate_system="G54",
        work_coordinate_systems=("G54", "G55"),
    ) == {
        "position_mode": "work",
        "startup_mode": "controller",
        "preferred_system": "G54",
    }


def test_work_coordinate_systems_include_fluidnc_standard_slots() -> None:
    assert WORK_COORDINATE_SYSTEMS == (
        "G54",
        "G55",
        "G56",
        "G57",
        "G58",
        "G59",
        "G59.1",
        "G59.2",
        "G59.3",
    )
