"""Small top-level settings section models."""

from __future__ import annotations

from dataclasses import dataclass


WORK_COORDINATE_SYSTEMS: tuple[str, ...] = (
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


@dataclass
class LoggingSettings:
    """Configuration for application logging."""

    level: str = "INFO"
    file: str = ""

    def clone(self) -> "LoggingSettings":
        """Return a copy of the logging preferences."""

        return LoggingSettings(level=self.level, file=self.file)

    def to_dict(self) -> dict[str, str]:
        """Serialize the logging preferences."""

        return {"level": self.level, "file": self.file}


@dataclass
class ApiSettings:
    """Configuration for the optional local API server."""

    enabled: bool = True
    host: str = "127.0.0.1"
    port: int = 8765

    def clone(self) -> "ApiSettings":
        """Return a copy of the API preferences."""

        return ApiSettings(
            enabled=self.enabled,
            host=self.host,
            port=self.port,
        )

    def to_dict(self) -> dict[str, bool | int | float | str]:
        """Serialize the API preferences."""

        return {
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
        }


@dataclass
class ClickToMoveSettings:
    """Configuration for click-to-move UI behavior."""

    pending_timeout_s: float = 8.0

    def clone(self) -> "ClickToMoveSettings":
        """Return a copy of the click-to-move preferences."""

        return ClickToMoveSettings(pending_timeout_s=self.pending_timeout_s)

    def to_dict(self) -> dict[str, float]:
        """Serialize the click-to-move preferences."""

        return {"pending_timeout_s": self.pending_timeout_s}


@dataclass
class CoordinateSystemSettings:
    """Configuration for work-coordinate system selection."""

    position_mode: str = "work"
    startup_mode: str = "controller"
    preferred_system: str = "G54"

    def clone(self) -> "CoordinateSystemSettings":
        """Return a copy of the coordinate-system preferences."""

        return CoordinateSystemSettings(
            position_mode=self.position_mode,
            startup_mode=self.startup_mode,
            preferred_system=self.preferred_system,
        )

    def to_dict(self) -> dict[str, object]:
        """Serialize coordinate-system preferences."""

        return {
            "position_mode": self.position_mode,
            "startup_mode": self.startup_mode,
            "preferred_system": self.preferred_system,
        }


__all__ = [
    "ApiSettings",
    "ClickToMoveSettings",
    "CoordinateSystemSettings",
    "LoggingSettings",
    "WORK_COORDINATE_SYSTEMS",
]
