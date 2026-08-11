from __future__ import annotations

from pathlib import Path

import pytest

from probe_station_gui.design.klayout_types import (
    KLayoutConfig,
    SnapFailure,
    SnapResponse,
)
from probe_station_gui.design.model import SnapResult
from probe_station_gui.design.plot_interaction import (
    PlotAction,
    SnapClickIntent,
    SnapHoverIntent,
)
from probe_station_gui.design.selection_geometry import GuideSnapCandidate
from probe_station_gui.design.snap_coordinator import (
    AttachWorker,
    CancelHover,
    CancelPending,
    ClickPublication,
    DetachWorker,
    HoverPublication,
    ReplaceWorker,
    SnapNotice,
    SnapCoordinator,
    SubmitClick,
    SubmitHover,
)


def _config(*, generation: int = 1) -> KLayoutConfig:
    return KLayoutConfig(
        path=Path("layout.gds"),
        top_cell_name="TOP",
        visible_layers=frozenset({(1, 0)}),
        source_bounds=(0.0, 0.0, 100.0, 50.0),
        display_bounds=(0.0, 0.0, 100.0, 50.0),
        rotation_quarter_turns=0,
        generation=generation,
        source_load_id="load-1",
    )


def _click(
    point: tuple[float, float],
    *,
    generation: int = 1,
    action: PlotAction = PlotAction.MOVE,
) -> SnapClickIntent:
    return SnapClickIntent(
        action=action,
        raw_point=point,
        payload=(),
        shift=False,
        control=False,
        generation=generation,
    )


def _response(command: SubmitClick, point: tuple[float, float]) -> SnapResponse:
    request = command.request
    return SnapResponse(
        request_id=request.request_id,
        config_generation=request.config.generation,
        raw_point=request.point,
        result=SnapResult(point, "vertex", 0.1),
        elapsed_ms=1.0,
        shapes_inspected=1,
        purpose="click",
    )


def _hover(
    point: tuple[float, float], *, generation: int = 1, shift: bool = False
) -> SnapHoverIntent:
    return SnapHoverIntent(point, shift, False, generation)


def test_reverse_worker_completion_publishes_one_click_per_checkpoint() -> None:
    coordinator = SnapCoordinator()
    coordinator.configure(_config())
    first = coordinator.submit_click(_click((10.0, 10.0)), radius=2.0)
    second = coordinator.submit_click(_click((20.0, 20.0)), radius=2.0)
    first_command = next(c for c in first.commands if isinstance(c, SubmitClick))
    second_command = next(c for c in second.commands if isinstance(c, SubmitClick))

    out_of_order = coordinator.worker_response(_response(second_command, (21.0, 22.0)))
    in_order = coordinator.worker_response(_response(first_command, (11.0, 12.0)))
    continued = coordinator.continue_ready_clicks()

    assert out_of_order.publications == ()
    assert [item.result.point for item in in_order.publications] == [(11.0, 12.0)]
    assert [item.result.point for item in continued.publications] == [(21.0, 22.0)]


def test_preflight_fallback_waits_behind_worker_head_and_failed_head_releases_it() -> (
    None
):
    coordinator = SnapCoordinator()
    coordinator.configure(_config())
    head = coordinator.submit_click(_click((10.0, 10.0)), radius=1.0)
    head_command = next(c for c in head.commands if isinstance(c, SubmitClick))

    fallback = coordinator.submit_click(_click((1_000.0, 1_000.0)), radius=1.0)
    released = coordinator.worker_failure(
        SnapFailure(
            head_command.request.request_id,
            head_command.request.config.generation,
            "click",
            "failed",
        )
    )

    assert fallback.publications == ()
    clicks = tuple(p for p in released.publications if isinstance(p, ClickPublication))
    assert [p.result.point for p in clicks] == [(1_000.0, 1_000.0)]
    assert any(isinstance(n, SnapNotice) for n in released.notices)


def test_latest_hover_terminal_is_consumed_once_with_modifier_snapshot() -> None:
    coordinator = SnapCoordinator()
    coordinator.configure(_config())
    first = coordinator.submit_hover(_hover((1.0, 2.0)), radius=2.0)
    second = coordinator.submit_hover(_hover((3.0, 4.0), shift=True), radius=2.0)
    first_command = next(c for c in first.commands if isinstance(c, SubmitHover))
    second_command = next(c for c in second.commands if isinstance(c, SubmitHover))

    stale = coordinator.worker_response(
        SnapResponse(
            first_command.request.request_id,
            first_command.request.config.generation,
            first_command.request.point,
            SnapResult((9.0, 9.0), "vertex", 0.1),
            1.0,
            1,
            "hover",
        )
    )
    current = coordinator.worker_response(
        SnapResponse(
            second_command.request.request_id,
            second_command.request.config.generation,
            second_command.request.point,
            SnapResult((5.0, 6.0), "vertex", 0.1),
            1.0,
            1,
            "hover",
        )
    )
    replay = coordinator.worker_response(
        SnapResponse(
            second_command.request.request_id,
            second_command.request.config.generation,
            second_command.request.point,
            SnapResult((7.0, 8.0), "vertex", 0.1),
            1.0,
            1,
            "hover",
        )
    )

    assert stale.publications == ()
    assert current.publications == (
        HoverPublication(
            SnapResult((5.0, 6.0), "vertex", 0.1), True, False, (3.0, 4.0), 1.0, 1
        ),
    )
    assert replay.publications == ()


def test_stale_config_markup_load_and_interaction_generations_are_inert() -> None:
    coordinator = SnapCoordinator()
    coordinator.configure(_config(generation=1))
    coordinator.set_markup_candidates(())
    pending = coordinator.submit_click(_click((10.0, 10.0)), radius=2.0)
    command = next(c for c in pending.commands if isinstance(c, SubmitClick))

    coordinator.set_markup_candidates(())
    markup_stale = coordinator.worker_response(_response(command, (11.0, 12.0)))
    coordinator.configure(_config(generation=2))
    config_stale = coordinator.worker_response(_response(command, (13.0, 14.0)))
    coordinator.reset_document()
    load_stale = coordinator.worker_response(_response(command, (15.0, 16.0)))
    coordinator.set_interaction_generation(2)
    generation_stale = coordinator.submit_click(
        _click((2.0, 2.0), generation=1), radius=2.0
    )

    assert markup_stale.publications == ()
    assert config_stale.publications == ()
    assert load_stale.publications == ()
    assert generation_stale.publications == ()
    assert generation_stale.commands == ()


def test_snap_off_cancels_pending_and_clicks_publish_free_immediately() -> None:
    coordinator = SnapCoordinator()
    coordinator.configure(_config())
    coordinator.submit_hover(_hover((1.0, 2.0)), radius=2.0)
    disabled = coordinator.set_enabled(False)
    click = coordinator.submit_click(_click((7.0, 8.0)), radius=2.0)

    assert any(isinstance(c, CancelPending) for c in disabled.commands)
    assert HoverPublication(None, False, False, None, None) in disabled.publications
    publications = tuple(
        p for p in click.publications if isinstance(p, ClickPublication)
    )
    assert [p.result for p in publications] == [SnapResult((7.0, 8.0), "free", 0.0)]


def test_markup_arbitration_prefers_nearest_screen_candidate() -> None:
    coordinator = SnapCoordinator(
        screen_distance=lambda raw, candidate: abs(candidate[0] - raw[0])
    )
    coordinator.configure(_config())
    markup = SnapResult((1.0, 1.0), "guide_end", 0.2)
    coordinator.set_markup_candidates((markup,))
    transition = coordinator.submit_click(_click((1.1, 1.0)), radius=1.0)
    command = next(c for c in transition.commands if isinstance(c, SubmitClick))

    completed = coordinator.worker_response(_response(command, (20.0, 20.0)))

    publication = next(
        p for p in completed.publications if isinstance(p, ClickPublication)
    )
    assert publication.result.point == markup.point
    assert publication.result.mode == markup.mode
    assert publication.result.distance == pytest.approx(0.1)


def test_local_geometry_and_markup_use_the_same_arbitration_policy() -> None:
    coordinator = SnapCoordinator(
        screen_distance=lambda raw, candidate: abs(candidate[0] - raw[0])
    )
    coordinator.set_markup_candidates((SnapResult((1.0, 1.0), "guide_end", 0.1),))

    result = coordinator.resolve_local(
        (1.1, 1.0),
        radius=1.0,
        geometry_result=SnapResult((10.0, 10.0), "vertex", 0.1),
    )

    assert result.point == (1.0, 1.0)
    assert result.mode == "guide_end"
    assert result.distance == pytest.approx(0.1)


def test_markup_candidate_reports_actual_euclidean_distance() -> None:
    coordinator = SnapCoordinator()
    coordinator.set_markup_candidates(
        (GuideSnapCandidate((0.0, 0.0), "guide_end"),)
    )

    result = coordinator.resolve_local(
        (3.0, 4.0),
        radius=10.0,
        geometry_result=None,
    )

    assert result == SnapResult((0.0, 0.0), "guide_end", 5.0)


def test_worker_attach_reuse_replace_detach_and_lifecycle_failure_are_typed() -> None:
    coordinator = SnapCoordinator()
    attached = coordinator.configure(_config(generation=1))
    reused = coordinator.configure(_config(generation=2))
    replaced = coordinator.configure(
        KLayoutConfig(
            **{
                **_config(generation=3).__dict__,
                "source_load_id": "load-2",
            }
        )
    )
    active_token = coordinator.worker_token
    pending = coordinator.submit_click(
        _click((10.0, 10.0), action=PlotAction.ROUTE_POINT),
        radius=2.0,
    )
    pending_command = next(c for c in pending.commands if isinstance(c, SubmitClick))
    lifecycle = coordinator.worker_lifecycle_failed(active_token, "backend died")
    late = coordinator.worker_response(_response(pending_command, (11.0, 12.0)))
    recovered = coordinator.submit_click(
        _click((20.0, 20.0), action=PlotAction.ROUTE_POINT),
        radius=2.0,
    )
    detached = coordinator.configure(None)

    assert isinstance(attached.commands[0], AttachWorker)
    assert reused.commands == ()
    assert isinstance(replaced.commands[0], ReplaceWorker)
    assert lifecycle.notices == (SnapNotice("lifecycle_failed", "backend died"),)
    assert lifecycle.commands == (DetachWorker(active_token, 0.0),)
    assert HoverPublication(None, False, False, None, None) in lifecycle.publications
    assert late.publications == ()
    assert isinstance(recovered.commands[0], AttachWorker)
    assert isinstance(recovered.commands[1], SubmitClick)
    assert recovered.commands[0].worker_token != active_token
    assert recovered.commands[1].worker_token == recovered.commands[0].worker_token
    assert isinstance(detached.commands[0], DetachWorker)


def test_close_is_bounded_command_and_all_late_terminals_are_inert() -> None:
    coordinator = SnapCoordinator()
    coordinator.configure(_config())
    submitted = coordinator.submit_click(_click((10.0, 10.0)), radius=2.0)
    command = next(c for c in submitted.commands if isinstance(c, SubmitClick))

    closed = coordinator.close()
    late = coordinator.worker_response(_response(command, (11.0, 12.0)))
    reopened_attempt = coordinator.configure(_config(generation=2))

    assert any(isinstance(c, CancelPending) for c in closed.commands)
    assert any(
        isinstance(c, DetachWorker) and c.timeout_s == 0.0 for c in closed.commands
    )
    assert late.publications == ()
    assert reopened_attempt.commands == ()


def test_document_generation_changes_on_reset_and_guards_geometry_completion() -> None:
    coordinator = SnapCoordinator()
    generation = coordinator.document_generation

    coordinator.reset_document()

    assert coordinator.document_generation == generation + 1
    assert not coordinator.geometry_is_current(generation)
    assert coordinator.geometry_is_current(generation + 1)


def test_preflight_hover_cancels_worker_and_publishes_fallback() -> None:
    coordinator = SnapCoordinator()
    coordinator.configure(_config())
    transition = coordinator.submit_hover(_hover((1_000.0, 1_000.0)), radius=1.0)

    assert any(isinstance(c, CancelHover) for c in transition.commands)
    publication = next(
        p for p in transition.publications if isinstance(p, HoverPublication)
    )
    assert publication.result == SnapResult((1_000.0, 1_000.0), "free", 0.0)


def test_tool_switch_keeps_nontransient_fifo_head_and_cancels_transient_tail() -> None:
    coordinator = SnapCoordinator()
    coordinator.configure(_config())
    coordinator.set_interaction_generation(1)
    head = coordinator.submit_click(
        _click((10.0, 10.0), action=PlotAction.CALIBRATION),
        radius=2.0,
    )
    tail = coordinator.submit_click(_click((20.0, 20.0)), radius=2.0)
    head_command = next(c for c in head.commands if isinstance(c, SubmitClick))
    tail_command = next(c for c in tail.commands if isinstance(c, SubmitClick))

    switched = coordinator.set_interaction_generation(2)
    cancelled_failure = coordinator.worker_failure(
        SnapFailure(
            tail_command.request.request_id,
            tail_command.request.config.generation,
            "click",
            "late failure",
        )
    )
    completed = coordinator.worker_response(_response(head_command, (11.0, 12.0)))

    assert switched.publications == ()
    assert cancelled_failure.notices == ()
    assert cancelled_failure.publications == ()
    publications = tuple(
        item for item in completed.publications if isinstance(item, ClickPublication)
    )
    assert [item.intent.action for item in publications] == [PlotAction.CALIBRATION]


def test_preflight_click_cancels_hover_and_late_hover_terminal_is_inert() -> None:
    coordinator = SnapCoordinator()
    coordinator.configure(_config())
    hover = coordinator.submit_hover(_hover((10.0, 10.0)), radius=1.0)
    hover_command = next(c for c in hover.commands if isinstance(c, SubmitHover))

    click = coordinator.submit_click(_click((1_000.0, 1_000.0)), radius=1.0)
    late = coordinator.worker_response(
        SnapResponse(
            hover_command.request.request_id,
            hover_command.request.config.generation,
            hover_command.request.point,
            SnapResult((9.0, 9.0), "vertex", 0.1),
            1.0,
            1,
            "hover",
        )
    )

    assert any(isinstance(command, CancelHover) for command in click.commands)
    assert late.publications == ()


def test_only_matching_hover_failure_clears_hover_and_is_consumed_once() -> None:
    coordinator = SnapCoordinator()
    coordinator.configure(_config())
    first = coordinator.submit_hover(_hover((1.0, 2.0)), radius=2.0)
    second = coordinator.submit_hover(_hover((3.0, 4.0)), radius=2.0)
    first_command = next(c for c in first.commands if isinstance(c, SubmitHover))
    second_command = next(c for c in second.commands if isinstance(c, SubmitHover))

    stale = coordinator.worker_failure(
        SnapFailure(
            first_command.request.request_id,
            first_command.request.config.generation,
            "hover",
            "stale",
        )
    )
    current_failure = SnapFailure(
        second_command.request.request_id,
        second_command.request.config.generation,
        "hover",
        "current",
    )
    current = coordinator.worker_failure(current_failure)
    replay = coordinator.worker_failure(current_failure)

    assert stale.publications == ()
    assert current.publications == (
        HoverPublication(None, False, False, (3.0, 4.0), None, 1),
    )
    assert replay.publications == ()


def test_markup_stale_response_consumes_fifo_head_before_current_tail() -> None:
    coordinator = SnapCoordinator()
    coordinator.configure(_config())
    head = coordinator.submit_click(_click((10.0, 10.0)), radius=2.0)
    head_command = next(c for c in head.commands if isinstance(c, SubmitClick))
    coordinator.set_markup_candidates((SnapResult((1.0, 1.0), "guide_end", 0.1),))
    tail = coordinator.submit_click(_click((20.0, 20.0)), radius=2.0)
    tail_command = next(c for c in tail.commands if isinstance(c, SubmitClick))

    stale = coordinator.worker_response(_response(head_command, (11.0, 12.0)))
    current = coordinator.worker_response(_response(tail_command, (21.0, 22.0)))

    assert stale.publications == ()
    publications = tuple(
        item for item in current.publications if isinstance(item, ClickPublication)
    )
    assert [item.result.point for item in publications] == [(21.0, 22.0)]


def test_markup_stale_failure_is_silent_and_consumes_fifo_head() -> None:
    coordinator = SnapCoordinator()
    coordinator.configure(_config())
    head = coordinator.submit_click(_click((10.0, 10.0)), radius=2.0)
    head_command = next(c for c in head.commands if isinstance(c, SubmitClick))
    coordinator.set_markup_candidates((SnapResult((1.0, 1.0), "guide_end", 0.1),))
    tail = coordinator.submit_click(_click((20.0, 20.0)), radius=2.0)
    tail_command = next(c for c in tail.commands if isinstance(c, SubmitClick))

    stale = coordinator.worker_failure(
        SnapFailure(
            head_command.request.request_id,
            head_command.request.config.generation,
            "click",
            "stale failure",
        )
    )
    current = coordinator.worker_response(_response(tail_command, (21.0, 22.0)))

    assert stale.notices == ()
    assert stale.publications == ()
    publications = tuple(
        item for item in current.publications if isinstance(item, ClickPublication)
    )
    assert [item.result.point for item in publications] == [(21.0, 22.0)]
