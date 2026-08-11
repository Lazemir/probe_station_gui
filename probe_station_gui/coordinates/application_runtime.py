"""Application composition for shared non-coordinate Design state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .coordinator import CoordinateSystemCoordinator

if TYPE_CHECKING:
    from probe_station_gui.design.session_state import DesignSessionState


@dataclass(frozen=True)
class ApplicationCoordinateRuntime:
    """Application-only effects around the coordinate workflow facade."""

    coordinator: CoordinateSystemCoordinator

    def controller_persistence_state(
        self,
        workspace_state: DesignSessionState,
    ) -> dict[str, object] | None:
        """Merge detached workspace state into the coordinate-owned projection."""

        return self.coordinator.controller_persistence_state(workspace_state)


def create_application_coordinate_runtime(
    *,
    restore_frame_id: str | None = None,
) -> ApplicationCoordinateRuntime:
    """Create the one shared session at the application composition root."""

    return ApplicationCoordinateRuntime(
        coordinator=CoordinateSystemCoordinator(
            restore_frame_id=restore_frame_id,
        ),
    )


__all__ = ["ApplicationCoordinateRuntime", "create_application_coordinate_runtime"]
