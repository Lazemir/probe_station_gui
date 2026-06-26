"""Outbound FluidNC command classification for stage-owned UI adapters."""

from __future__ import annotations

from typing import Literal


FluidNCOutboundCommandKind = Literal[
    "jog_stop",
    "soft_reset",
    "jog_command",
    "manual_command",
    "fallback",
]


def classify_outbound_command(command: str | bytes) -> FluidNCOutboundCommandKind:
    """Return the controller-owned route for a UI-originated outbound command."""

    if isinstance(command, bytes):
        if command == b"\x85":
            return "jog_stop"
        if command == b"\x18":
            return "soft_reset"
        return "fallback"
    if command.startswith("$J="):
        return "jog_command"
    return "manual_command"


__all__ = ["FluidNCOutboundCommandKind", "classify_outbound_command"]
