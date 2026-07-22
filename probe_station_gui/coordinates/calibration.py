"""Calibration configuration fingerprints for coordinate-frame invalidation."""

from __future__ import annotations

import json
from collections.abc import Mapping

from probe_station_gui.settings.axis_calibration_config import CALIBRATION_AXES


def changed_calibration_axes(
    previous: Mapping[str, object],
    current: Mapping[str, object],
) -> set[str]:
    """Return axes whose complete universal calibration configurations changed."""

    return {
        axis
        for axis in CALIBRATION_AXES
        if _fingerprint(previous.get(axis)) != _fingerprint(current.get(axis))
    }


def _fingerprint(value: object) -> str:
    serializer = getattr(value, "to_dict", None)
    raw = serializer() if callable(serializer) else value
    try:
        return json.dumps(raw, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        return repr(raw)


__all__ = ["changed_calibration_axes"]
