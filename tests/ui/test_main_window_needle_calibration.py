from __future__ import annotations

from types import SimpleNamespace

from probe_station_gui.application.route_run_execution import _RouteRunExecutionSlot
from probe_station_gui.views import main_window_needle_calibration as calibration_ui


class _Signal:
    def __init__(self) -> None:
        self.emitted: list[tuple[object, ...]] = []

    def emit(self, *args: object) -> None:
        self.emitted.append(args)


class _ContactWindow:
    def __init__(self) -> None:
        self.running: list[bool] = []
        self.results: list[str] = []

    def set_contact_seek_running(self, running: bool) -> None:
        self.running.append(bool(running))

    def set_contact_seek_result(self, result: str) -> None:
        self.results.append(str(result))


class _FakeThread:
    def __init__(self, *, target, args=(), daemon=None, name=None) -> None:
        self.target = target
        self.args = args
        self.daemon = daemon
        self.name = name
        self.started = False
        self.join_calls: list[float | None] = []

    def start(self) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return False

    def join(self, timeout: float | None = None) -> None:
        self.join_calls.append(timeout)


class _StageController:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.lowering = 1.25

    def begin_external_task(self, label: str) -> None:
        self.calls.append(("begin", label))

    def finish_external_task(self) -> None:
        self.calls.append(("finish",))

    def run_external_needles_adjust(self, delta_mm: float, feedrate: float) -> None:
        self.calls.append(("adjust", delta_mm, feedrate))

    def latest_axis_a_lowering(self) -> float:
        return self.lowering

    def set_current_axis_work_coordinate(self, axis: str, value: float) -> None:
        self.calls.append(("set_work", axis, value))


class _StopFlag:
    def __init__(self) -> None:
        self.cleared = False

    def clear(self) -> None:
        self.cleared = True

    def is_set(self) -> bool:
        return False


def test_save_needle_calibration_uses_serialized_settings_transaction() -> None:
    calls: list[tuple[object, bool]] = []
    needle_settings = SimpleNamespace(
        raise_position_mm=1.0,
        raise_position_configured=True,
        down_position_mm=2.0,
        down_position_configured=True,
        contact_zone_mm=0.25,
        chip_position=None,
        stone_position=None,
    )
    settings = SimpleNamespace(needle_calibration=needle_settings)
    owner = SimpleNamespace(
        settings_manager=SimpleNamespace(
            replace_and_save=lambda value, *, preserve_exposure_policy: calls.append(
                (value, preserve_exposure_policy)
            )
        ),
        stage_controller=SimpleNamespace(
            apply_needle_calibration=lambda **_kwargs: None
        ),
        joystick_panel=None,
        contact_calibration_window=None,
    )

    calibration_ui.save_needle_calibration_settings(owner, settings)

    assert calls == [(settings, True)]


def test_request_contact_seek_rejects_disconnected_instrument_without_thread() -> None:
    statuses: list[str] = []
    owner = SimpleNamespace(
        _contact_seek_thread=None,
        _route_run_execution=_RouteRunExecutionSlot(),
        lcr_controller=SimpleNamespace(is_connected=lambda: False),
        contact_calibration_window=_ContactWindow(),
        _show_status=lambda message: statuses.append(message),
    )

    calibration_ui.request_contact_seek(owner, thread_factory=_FakeThread)

    assert statuses == ["Connect the measurement instrument before contact seek."]
    assert owner.contact_calibration_window.results == [
        "Measurement instrument is not connected."
    ]
    assert owner._contact_seek_thread is None


def test_request_contact_seek_marks_window_running_and_starts_thread() -> None:
    owner = SimpleNamespace(
        _contact_seek_thread=None,
        _route_run_execution=_RouteRunExecutionSlot(),
        _contact_seek_stop_requested=_StopFlag(),
        _run_contact_seek=lambda: None,
        lcr_controller=SimpleNamespace(is_connected=lambda: True),
        contact_calibration_window=_ContactWindow(),
    )

    calibration_ui.request_contact_seek(owner, thread_factory=_FakeThread)

    assert owner._contact_seek_stop_requested.cleared is True
    assert owner.contact_calibration_window.running == [True]
    assert owner.contact_calibration_window.results == ["Starting."]
    assert owner._contact_seek_thread.started is True
    assert owner._contact_seek_thread.daemon is True


def test_run_contact_seek_steps_until_good_contact_then_saves_calibration() -> None:
    qualities = iter(
        (
            SimpleNamespace(
                good=False, status="bad", median_ohm=1e6, mad_sigma_ohm=0.0
            ),
            SimpleNamespace(
                good=True, status="good", median_ohm=12.5, mad_sigma_ohm=0.2
            ),
            SimpleNamespace(
                good=True,
                status="good",
                median_ohm=12.5,
                mad_sigma_ohm=0.2,
                p95_abs_step_ohm=0.7,
            ),
        )
    )
    owner = SimpleNamespace(
        stage_controller=_StageController(),
        _current_needle_feedrate=lambda: 7.0,
        _contact_seek_measure_quality=lambda _count: next(qualities),
        _contact_seek_stop_requested=_StopFlag(),
        contact_seek_status=_Signal(),
        contact_seek_calibration_found=_Signal(),
        contact_seek_finished=_Signal(),
    )

    calibration_ui.run_contact_seek(owner)

    assert ("adjust", -0.001, 7.0) in owner.stage_controller.calls
    assert ("set_work", "A", 0.0) in owner.stage_controller.calls
    assert owner.contact_seek_calibration_found.emitted[0][0] == 1.25
    assert owner.contact_seek_finished.emitted[0][0] is True
    assert (
        "Contact seek found stable contact" in owner.contact_seek_finished.emitted[0][1]
    )


def test_run_contact_seek_uses_owner_tunable_constants() -> None:
    counts: list[int] = []
    qualities = iter(
        (
            SimpleNamespace(
                good=False, status="bad", median_ohm=1e6, mad_sigma_ohm=0.0
            ),
            SimpleNamespace(
                good=True, status="good", median_ohm=12.5, mad_sigma_ohm=0.2
            ),
            SimpleNamespace(
                good=True,
                status="good",
                median_ohm=12.5,
                mad_sigma_ohm=0.2,
                p95_abs_step_ohm=0.7,
            ),
        )
    )

    def measure_quality(count: int):
        counts.append(count)
        return next(qualities)

    owner = SimpleNamespace(
        CONTACT_SEEK_STEP_MM=-0.002,
        CONTACT_SEEK_MAX_TOTAL_MM=0.002,
        CONTACT_SEEK_QUICK_COUNT=3,
        CONTACT_SEEK_CONFIRM_COUNT=4,
        stage_controller=_StageController(),
        _current_needle_feedrate=lambda: 7.0,
        _contact_seek_measure_quality=measure_quality,
        _contact_seek_stop_requested=_StopFlag(),
        contact_seek_status=_Signal(),
        contact_seek_calibration_found=_Signal(),
        contact_seek_finished=_Signal(),
    )

    calibration_ui.run_contact_seek(owner)

    assert counts == [3, 3, 4]
    assert ("adjust", -0.002, 7.0) in owner.stage_controller.calls
    assert "0.0020 mm down" in owner.contact_seek_status.emitted[2][0]


def test_on_contact_seek_finished_clears_thread_and_sends_failure_alert() -> None:
    alerts: list[tuple[object, ...]] = []
    statuses: list[tuple[str, int]] = []
    thread = _FakeThread(target=lambda: None)
    owner = SimpleNamespace(
        _contact_seek_thread=thread,
        contact_calibration_window=_ContactWindow(),
        _resume_resistance_standby_polling=lambda: statuses.append(("resume", 0)),
        _show_status=lambda message, timeout: statuses.append((message, timeout)),
        _telegram_runtime=SimpleNamespace(
            send_alert=lambda *args, **kwargs: alerts.append((args, kwargs))
        ),
    )

    calibration_ui.on_contact_seek_finished(owner, False, "failed")

    assert thread.join_calls == [0.1]
    assert owner._contact_seek_thread is None
    assert owner.contact_calibration_window.running == [False]
    assert owner.contact_calibration_window.results == ["failed"]
    assert ("failed", 8000) in statuses
    assert alerts[0][0][0] == "contact_seek_failed"


def test_on_sample_handling_finished_prompts_and_requests_autofocus() -> None:
    statuses: list[tuple[str, int]] = []
    owner = SimpleNamespace(
        _sample_handling_thread=object(),
        _show_status=lambda message, timeout: statuses.append((message, timeout)),
        _clear_stage_motion_axes=lambda: statuses.append(("clear", 0)),
        _update_stage_coordinate_apply_state=lambda: statuses.append(("apply", 0)),
        _schedule_cancel_state_refresh=lambda: statuses.append(("refresh", 0)),
        stage_controller=SimpleNamespace(
            is_busy=lambda: False,
            request_autofocus=lambda: statuses.append(("autofocus", 0)),
        ),
    )
    message_box = SimpleNamespace(Yes=1, No=2, question=lambda *_args: 1)

    calibration_ui.on_sample_handling_finished(
        owner,
        True,
        "loaded",
        True,
        1.25,
        message_box=message_box,
    )

    assert owner._sample_handling_thread is None
    assert ("loaded", 7000) in statuses
    assert ("autofocus", 0) in statuses
