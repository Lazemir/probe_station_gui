"""Pure planning helpers for route measurement finish handling."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RouteFinishSignalPlan:
    ignored: bool = False
    success: bool = False
    message: str = ""
    csv_path: str = ""


@dataclass(frozen=True)
class RouteFinishTelegramPlan:
    key: str
    heading: str
    message: str
    csv_path: str = ""
    include_csv_record_count: bool = False
    document_path: str | None = None
    attach_photo: bool = False


@dataclass(frozen=True)
class RouteFinishOutcomePlan:
    success: bool
    message: str
    csv_path: str
    pending: bool
    status_text: str
    resume_point: int | None = None
    clear_point_numbers: bool = False
    needs_csv_record_count: bool = False
    telegram: RouteFinishTelegramPlan | None = None
    status_timeout_ms: int = 8000


def route_finish_signal_plan(
    args: tuple[object, ...],
    *,
    current_runner: object | None,
) -> RouteFinishSignalPlan:
    if len(args) == 4:
        finished_runner, success, message, csv_path = args
    elif len(args) == 3:
        finished_runner = None
        success, message, csv_path = args
    else:
        return RouteFinishSignalPlan(ignored=True)
    if finished_runner is not None and finished_runner is not current_runner:
        return RouteFinishSignalPlan(ignored=True)
    return RouteFinishSignalPlan(
        success=bool(success),
        message=str(message),
        csv_path=str(csv_path),
    )


def route_finish_outcome_plan(
    *,
    success: bool,
    message: str,
    csv_path: str,
    measure_enabled: bool,
    context_close_requested: bool,
    point_numbers: list[int],
    current_point: int | None,
) -> RouteFinishOutcomePlan:
    if success:
        return _success_finish_outcome_plan(
            message=message,
            csv_path=csv_path,
            measure_enabled=measure_enabled,
            point_numbers=point_numbers,
        )
    return _failure_finish_outcome_plan(
        message=message,
        csv_path=csv_path,
        context_close_requested=context_close_requested,
        current_point=current_point,
    )


def route_finish_telegram_text(
    plan: RouteFinishTelegramPlan,
    *,
    csv_record_count: int | None,
) -> str:
    text = f"{plan.heading}\n{plan.message}"
    if plan.include_csv_record_count and csv_record_count is not None:
        text = (
            f"{text}\n"
            f"Session total: {csv_record_count} measurements in CSV."
        )
    if plan.csv_path:
        text = f"{text}\nCSV: {plan.csv_path}"
    return text


def _success_finish_outcome_plan(
    *,
    message: str,
    csv_path: str,
    measure_enabled: bool,
    point_numbers: list[int],
) -> RouteFinishOutcomePlan:
    needs_csv_record_count = bool(measure_enabled and csv_path)
    return RouteFinishOutcomePlan(
        success=True,
        message=message,
        csv_path=csv_path,
        pending=False,
        resume_point=1 if len(point_numbers) > 1 else None,
        clear_point_numbers=True,
        needs_csv_record_count=needs_csv_record_count,
        status_text=f"{message}{_success_status_suffix(message, csv_path)}",
        telegram=RouteFinishTelegramPlan(
            key="route_completed",
            heading="Probe route completed:",
            message=message,
            csv_path=csv_path,
            include_csv_record_count=needs_csv_record_count,
            document_path=csv_path or None,
        ),
    )


def _failure_finish_outcome_plan(
    *,
    message: str,
    csv_path: str,
    context_close_requested: bool,
    current_point: int | None,
) -> RouteFinishOutcomePlan:
    return RouteFinishOutcomePlan(
        success=False,
        message=message,
        csv_path=csv_path,
        pending=True,
        resume_point=current_point,
        clear_point_numbers=False,
        status_text=message,
        telegram=None
        if context_close_requested
        else RouteFinishTelegramPlan(
            key="route_failed",
            heading="Probe route stopped or failed:",
            message=message,
            csv_path=csv_path,
            attach_photo=True,
        ),
    )


def _success_status_suffix(message: str, csv_path: str) -> str:
    if (
        "CSV:" in message
        or message.startswith("Route photo capture")
        or not csv_path
    ):
        return ""
    return f" CSV: {csv_path}"


__all__ = [
    "RouteFinishOutcomePlan",
    "RouteFinishSignalPlan",
    "RouteFinishTelegramPlan",
    "route_finish_outcome_plan",
    "route_finish_signal_plan",
    "route_finish_telegram_text",
]
