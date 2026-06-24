"""Small FluidNC serial protocol predicates shared across UI and controller code."""

from __future__ import annotations

CONTROLLER_REBOOT_TOKENS = (
    "[MSG:RST",
    "FAST_FLASH_BOOT",
    "ESP-ROM",
)
CONTROLLER_REBOOT_LINE_PREFIXES = ("RST:", "LOAD:", "ENTRY ")
CONTROLLER_STARTUP_BANNER_TOKENS = (
    "[VER:",
    "FLUIDNC",
    "GRBL",
)


def _normalized_line(line: str) -> str:
    return str(line or "").strip().upper()


def line_indicates_controller_reboot(line: str) -> bool:
    upper = _normalized_line(line)
    return upper.startswith(CONTROLLER_REBOOT_LINE_PREFIXES) or any(
        token in upper for token in CONTROLLER_REBOOT_TOKENS
    )


def line_indicates_controller_startup(line: str) -> bool:
    upper = _normalized_line(line)
    return line_indicates_controller_reboot(upper) or any(
        token in upper for token in CONTROLLER_STARTUP_BANNER_TOKENS
    )


__all__ = [
    "CONTROLLER_REBOOT_LINE_PREFIXES",
    "CONTROLLER_REBOOT_TOKENS",
    "CONTROLLER_STARTUP_BANNER_TOKENS",
    "line_indicates_controller_reboot",
    "line_indicates_controller_startup",
]
