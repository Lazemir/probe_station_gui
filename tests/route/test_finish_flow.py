from probe_station_gui.route.finish_flow import (
    route_finish_outcome_plan,
    route_finish_signal_plan,
    route_finish_telegram_text,
)


def test_signal_plan_ignores_stale_runner() -> None:
    current_runner = object()
    stale_runner = object()

    plan = route_finish_signal_plan(
        (stale_runner, True, "finished", "route.csv"),
        current_runner=current_runner,
    )

    assert plan.ignored is True


def test_successful_measured_route_requests_count_and_reports_csv() -> None:
    plan = route_finish_outcome_plan(
        success=True,
        message="Route measurement complete.",
        csv_path="route.csv",
        measure_enabled=True,
        context_close_requested=False,
        point_numbers=[1, 2],
        current_point=2,
    )

    assert plan.pending is False
    assert plan.resume_point == 1
    assert plan.clear_point_numbers is True
    assert plan.needs_csv_record_count is True
    assert plan.status_text == "Route measurement complete. CSV: route.csv"
    assert plan.telegram is not None
    assert plan.telegram.key == "route_completed"
    assert plan.telegram.document_path == "route.csv"
    assert (
        route_finish_telegram_text(plan.telegram, csv_record_count=2)
        == "Probe route completed:\n"
        "Route measurement complete.\n"
        "Session total: 2 measurements in CSV.\n"
        "CSV: route.csv"
    )


def test_successful_photo_only_route_does_not_request_count_or_csv_suffix() -> None:
    plan = route_finish_outcome_plan(
        success=True,
        message="Route photo capture complete.",
        csv_path="photos.csv",
        measure_enabled=False,
        context_close_requested=False,
        point_numbers=[1],
        current_point=1,
    )

    assert plan.resume_point is None
    assert plan.needs_csv_record_count is False
    assert plan.status_text == "Route photo capture complete."
    assert plan.telegram is not None
    assert (
        route_finish_telegram_text(plan.telegram, csv_record_count=None)
        == "Probe route completed:\nRoute photo capture complete.\nCSV: photos.csv"
    )


def test_failed_route_restores_pending_resume_and_sends_failure() -> None:
    plan = route_finish_outcome_plan(
        success=False,
        message="Needle contact failed.",
        csv_path="partial.csv",
        measure_enabled=True,
        context_close_requested=False,
        point_numbers=[1, 2, 3],
        current_point=3,
    )

    assert plan.pending is True
    assert plan.resume_point == 3
    assert plan.clear_point_numbers is False
    assert plan.needs_csv_record_count is False
    assert plan.status_text == "Needle contact failed."
    assert plan.telegram is not None
    assert plan.telegram.key == "route_failed"
    assert plan.telegram.attach_photo is True
    assert (
        route_finish_telegram_text(plan.telegram, csv_record_count=None)
        == "Probe route stopped or failed:\nNeedle contact failed.\nCSV: partial.csv"
    )


def test_context_close_failure_suppresses_failure_telegram() -> None:
    plan = route_finish_outcome_plan(
        success=False,
        message="Route window closed.",
        csv_path="partial.csv",
        measure_enabled=True,
        context_close_requested=True,
        point_numbers=[1, 2],
        current_point=2,
    )

    assert plan.pending is True
    assert plan.resume_point == 2
    assert plan.telegram is None
