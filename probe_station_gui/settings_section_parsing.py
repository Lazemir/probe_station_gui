"""Pure parsers for top-level settings sections."""

from __future__ import annotations

from collections.abc import Iterable


def parse_logging_settings(
    raw_logging: object,
    *,
    default_level: str = "INFO",
    default_file: str = "",
) -> dict[str, str]:
    level = default_level
    file_value = default_file
    if isinstance(raw_logging, dict):
        level = str(raw_logging.get("level", level))
        file_raw = raw_logging.get("file", file_value)
        if isinstance(file_raw, str):
            file_value = file_raw
    return {"level": level.upper(), "file": file_value}


def parse_api_settings(
    raw_api: object,
    *,
    default_enabled: bool,
    default_host: str,
    default_port: int,
) -> dict[str, object]:
    enabled = bool(default_enabled)
    host = str(default_host)
    port = int(default_port)
    if isinstance(raw_api, dict):
        enabled = bool(raw_api.get("enabled", enabled))
        host_raw = raw_api.get("host", host)
        if isinstance(host_raw, str):
            candidate = host_raw.strip()
            if candidate:
                host = candidate
        try:
            candidate_port = int(raw_api.get("port", port))
        except (TypeError, ValueError):
            candidate_port = port
        if 0 < candidate_port <= 65535:
            port = candidate_port
    return {"enabled": enabled, "host": host, "port": port}


def parse_coordinate_system_settings(
    raw_coordinate_system: object,
    *,
    default_position_mode: str,
    default_startup_mode: str,
    default_coordinate_system: str,
    work_coordinate_systems: Iterable[str],
) -> dict[str, str]:
    startup_mode = default_startup_mode
    preferred_system = default_coordinate_system
    position_mode = default_position_mode
    if isinstance(raw_coordinate_system, dict):
        position_mode_raw = raw_coordinate_system.get(
            "position_mode", position_mode
        )
        if isinstance(position_mode_raw, str):
            position_mode = position_mode_raw.strip().lower()
        mode_raw = raw_coordinate_system.get("startup_mode", startup_mode)
        if isinstance(mode_raw, str):
            startup_mode = mode_raw.strip().lower()
        system_raw = raw_coordinate_system.get(
            "preferred_system", preferred_system
        )
        if isinstance(system_raw, str):
            preferred_system = system_raw.strip().upper()
    if position_mode not in {"work", "machine"}:
        position_mode = default_position_mode
    if startup_mode not in {"controller", "fixed"}:
        startup_mode = default_startup_mode
    if preferred_system not in tuple(work_coordinate_systems):
        preferred_system = default_coordinate_system
    return {
        "position_mode": position_mode,
        "startup_mode": startup_mode,
        "preferred_system": preferred_system,
    }
