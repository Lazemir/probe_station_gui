from __future__ import annotations

from types import SimpleNamespace

from probe_station_gui.route.control_state import ApiRouteControlState
from probe_station_gui.route.measurement_records import RouteMeasurementPoint
from probe_station_gui.route.operation import (
    ApiRouteControlActionEffect,
    api_route_control_action_plan,
    find_route_contact_point,
    route_measurement_points_for_route,
    route_measurement_start_decision,
)
from probe_station_gui.route.session_start import snapshot_route_design_frame


def _point(index: int, label: str | None = None) -> RouteMeasurementPoint:
    return RouteMeasurementPoint(
        index=index,
        point_id=f"p{index}",
        label=label or f"P{index}",
        design_center=(float(index), float(index + 1)),
        stage_xy=(float(index + 10), float(index + 20)),
        needle_1_design=(float(index), float(index)),
        needle_2_design=(float(index), float(index)),
    )


def test_route_measurement_start_decision_filters_previous_ok_points(tmp_path) -> None:
    previous_csv = tmp_path / "previous.csv"
    previous_csv.write_text(
        "structure_number,status\n"
        "1,ok\n"
        "2,open\n"
        "3,ok\n",
        encoding="utf-8",
    )
    points = [_point(1), _point(2), _point(3)]
    route = SimpleNamespace(points=[object()])

    decision = route_measurement_start_decision(
        route=route,
        registration_valid=True,
        points_factory=lambda _route: list(points),
        current_point=3,
        previous_ok_only=True,
        previous_csv_path=previous_csv,
    )

    assert decision.accepted is True
    assert decision.plan is not None
    assert [point.index for point in decision.plan.points] == [1, 3]
    assert decision.plan.selected_point.index == 3
    assert decision.plan.previous_ok_skipped_count == 1
    assert decision.message == ""


def test_gui_route_plan_retains_design_frame_identity_when_active_link_changes(
    tmp_path,
) -> None:
    snapshot = snapshot_route_design_frame(frame_id="design-a", frame_version=4)
    active_link = {"frame_id": "design-a"}

    decision = route_measurement_start_decision(
        route=SimpleNamespace(points=[object()]),
        registration_valid=True,
        points_factory=lambda _route: [_point(1)],
        current_point=1,
        previous_ok_only=False,
        previous_csv_path=tmp_path / "unused.csv",
        design_frame_snapshot=snapshot,
    )
    active_link["frame_id"] = "design-b"

    assert decision.plan is not None
    assert decision.plan.design_frame_snapshot is snapshot
    assert decision.plan.design_frame_snapshot.frame_id == "design-a"
    assert decision.plan.points[0].stage_xy == (11.0, 21.0)


def test_route_measurement_start_decision_rejects_current_point_filtered_out(
    tmp_path,
) -> None:
    previous_csv = tmp_path / "previous.csv"
    previous_csv.write_text(
        "structure_number,status\n"
        "1,ok\n"
        "2,open\n",
        encoding="utf-8",
    )
    route = SimpleNamespace(points=[object()])

    decision = route_measurement_start_decision(
        route=route,
        registration_valid=True,
        points_factory=lambda _route: [_point(1), _point(2)],
        current_point=2,
        previous_ok_only=True,
        previous_csv_path=previous_csv,
    )

    assert decision.accepted is False
    assert decision.plan is None
    assert decision.dialog_status is True
    assert decision.message == (
        "Contact 2 is not enabled or not included by the current route filter."
    )


def test_route_measurement_points_for_route_resolves_enabled_points_and_offsets() -> None:
    enabled = SimpleNamespace(
        id="p1",
        label="P1",
        enabled=True,
        camera_center=(1.0, 2.0),
    )
    disabled = SimpleNamespace(
        id="p2",
        label="P2",
        enabled=False,
        camera_center=(3.0, 4.0),
    )

    def needle_hits_for_point(route_point):
        if route_point is enabled:
            return [
                ("needle_1", (1.5, 2.5)),
                ("needle_2", (0.5, 1.5)),
            ]
        return []

    route = SimpleNamespace(
        points=[enabled, disabled],
        needle_hits_for_point=needle_hits_for_point,
    )

    points = route_measurement_points_for_route(
        route,
        stage_from_design=lambda point: (point[0] + 10.0, point[1] + 20.0),
        contact_objective_offset=(0.25, -0.5),
        photo_objective_offset=(-1.0, 1.5),
    )

    assert len(points) == 1
    assert points[0] == RouteMeasurementPoint(
        index=1,
        point_id="p1",
        label="P1",
        design_center=(1.0, 2.0),
        stage_xy=(11.25, 21.5),
        needle_1_design=(1.5, 2.5),
        needle_2_design=(0.5, 1.5),
        photo_stage_xy=(10.0, 23.5),
    )


def test_find_route_contact_point_prefers_route_index_then_structure_number() -> None:
    points = [
        _point(1, label="device 7"),
        _point(2, label="device 1"),
    ]

    assert find_route_contact_point(points, 1) is points[0]
    assert find_route_contact_point(points, 7) is points[0]
    assert find_route_contact_point(points, 99) is None


def test_api_route_control_action_plan_preserves_pause_and_ack_semantics() -> None:
    active = ApiRouteControlState(active=True, label="chip 163")

    pause = api_route_control_action_plan(
        {"action": "pause"},
        active,
        updated_utc="t1",
    )
    assert pause.effect == ApiRouteControlActionEffect.PAUSE
    assert pause.state.pause_requested is True
    assert pause.state.paused is False
    assert pause.message == "chip 163: pause requested."

    ack = api_route_control_action_plan(
        {"action": "pause_ack"},
        pause.state,
        updated_utc="t2",
    )
    assert ack.effect == ApiRouteControlActionEffect.PAUSE_ACK
    assert ack.state.pause_requested is False
    assert ack.state.paused is True
    assert ack.message == "chip 163: paused."


def test_api_route_control_action_plan_requires_open_window_for_resume() -> None:
    paused = ApiRouteControlState(active=True, paused=True, label="chip 163")

    plan = api_route_control_action_plan(
        {"action": "skip"},
        paused,
        updated_utc="t3",
    )

    assert plan.effect == ApiRouteControlActionEffect.RESUME
    assert plan.requires_control_window_open is True
    assert plan.state.paused is False
    assert plan.state.pending_action == "skip"
    assert plan.message == "chip 163: skip."


def test_api_route_control_action_plan_rejects_inactive_control_commands() -> None:
    plan = api_route_control_action_plan(
        {"action": "interrupt"},
        ApiRouteControlState(),
        updated_utc="t4",
    )

    assert plan.effect == ApiRouteControlActionEffect.REJECT
    assert plan.status_code == 409
    assert plan.message == "No API route control run is active."
