"""Measurement-plan script loading for design-backed navigation."""

from __future__ import annotations

import importlib.util
import types
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .design_model import DesignDocument, DesignModelError, MeasurementTarget


class DesignScriptError(DesignModelError):
    """Raised when a measurement-plan script cannot be loaded or validated."""


@dataclass(frozen=True)
class ScriptContext:
    """Execution context exposed to user measurement-plan scripts."""

    document: DesignDocument

    @property
    def library(self) -> Any:
        return self.document.library

    @property
    def top_cell(self) -> Any:
        return self.document.top_cell

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return self.document.bounds

    @property
    def bounds_um(self) -> tuple[float, float, float, float]:
        return self.document.bounds_um()

    @property
    def visible_layers(self) -> tuple[tuple[int, int], ...]:
        return tuple(sorted(self.document.visible_layers))

    def dbu_to_um(self, value: float) -> float:
        return self.document.dbu_to_um(value)

    def um_to_dbu(self, value: float) -> float:
        return self.document.um_to_dbu(value)


def load_measurement_plan(
    script_path: str | Path,
    context: ScriptContext,
) -> tuple[types.ModuleType, list[MeasurementTarget]]:
    """Load a Python script exporting build_plan(context)."""

    resolved = Path(script_path).expanduser().resolve()
    if not resolved.exists():
        raise DesignScriptError(f"Measurement script '{resolved}' was not found.")
    module_name = f"probe_station_plan_{resolved.stem}_{abs(hash(str(resolved)))}"
    spec = importlib.util.spec_from_file_location(module_name, resolved)
    if spec is None or spec.loader is None:
        raise DesignScriptError(f"Unable to create import spec for '{resolved}'.")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise DesignScriptError(f"Measurement script '{resolved.name}' failed: {exc}") from exc

    build_plan = getattr(module, "build_plan", None)
    if not callable(build_plan):
        raise DesignScriptError(
            f"Measurement script '{resolved.name}' must define build_plan(context)."
        )
    try:
        raw_targets = build_plan(context)
    except Exception as exc:
        raise DesignScriptError(f"build_plan() failed: {exc}") from exc
    if raw_targets is None:
        return module, []
    if isinstance(raw_targets, (str, bytes)):
        raise DesignScriptError("build_plan() must return a sequence of targets, not text.")
    try:
        targets = [MeasurementTarget.from_object(item) for item in raw_targets]
    except TypeError as exc:
        raise DesignScriptError("build_plan() must return an iterable of targets.") from exc
    return module, targets


__all__ = ["DesignScriptError", "ScriptContext", "load_measurement_plan"]
