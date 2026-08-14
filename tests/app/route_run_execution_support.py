from __future__ import annotations

from probe_station_gui.application.route_run_execution import (
    RouteRunKind,
    _RouteRunExecutionSlot,
)


class _IdleRouteThread:
    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        _ = timeout


def install_route_run_execution(owner: object) -> _RouteRunExecutionSlot:
    slot = _RouteRunExecutionSlot()
    owner._route_run_execution = slot
    return slot


def activate_route_run(
    owner: object,
    runner: object | None,
    thread: object | None = None,
    *,
    kind: RouteRunKind = RouteRunKind.GUI,
    waiting: bool = False,
) -> _RouteRunExecutionSlot:
    slot = install_route_run_execution(owner)
    if runner is None:
        return slot
    slot.activate(
        runner,
        thread if thread is not None else _IdleRouteThread(),
        kind=kind,
    )
    if waiting:
        slot.publish_waiting(True, expected_runner=runner)
    return slot


def publish_route_waiting(owner: object, waiting: bool) -> None:
    snapshot = owner._route_run_execution.snapshot()
    if snapshot.runner is not None:
        owner._route_run_execution.publish_waiting(
            waiting,
            expected_runner=snapshot.runner,
        )
