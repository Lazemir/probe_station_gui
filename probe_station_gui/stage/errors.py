"""Stage controller error types and serial I/O exception groups."""

from __future__ import annotations

import serial


class StageControllerError(RuntimeError):
    """Raised when the stage controller cannot complete an operation."""


class AxisStateError(StageControllerError):
    """Raised when axis state prevents the requested operation."""


SERIAL_IO_EXCEPTIONS = (
    serial.SerialException,
    OSError,
    AttributeError,
    TypeError,
)
