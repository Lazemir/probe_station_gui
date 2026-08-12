from __future__ import annotations

from importlib.util import find_spec
import threading

import pytest

from probe_station_gui.route.run_control_mailbox import _RouteRunControlMailbox


def test_run_control_mailbox_owner_exists() -> None:
    assert find_spec("probe_station_gui.route.run_control_mailbox") is not None


def test_stop_wakes_confirmation_waiter() -> None:
    mailbox = _RouteRunControlMailbox()
    started = threading.Event()
    decisions: list[str] = []

    def wait_for_confirmation() -> None:
        started.set()
        decisions.append(mailbox.wait_for_confirmation())

    waiter = threading.Thread(target=wait_for_confirmation)
    waiter.start()
    assert started.wait(timeout=1.0)

    mailbox.request_stop()
    waiter.join(timeout=1.0)

    assert not waiter.is_alive()
    assert decisions == ["stop"]


def test_pause_request_is_consumed_once() -> None:
    mailbox = _RouteRunControlMailbox()

    mailbox.request_pause()

    assert mailbox.consume_pause() is True
    assert mailbox.consume_pause() is False


def test_interrupt_persists_until_explicit_clear() -> None:
    mailbox = _RouteRunControlMailbox()

    mailbox.request_interrupt()

    assert mailbox.interrupt_requested() is True
    assert mailbox.interrupt_requested() is True
    mailbox.clear_interrupt()
    assert mailbox.interrupt_requested() is False


@pytest.mark.parametrize(
    ("submitted", "expected"),
    [
        (" NEXT ", "next"),
        ("MEASURE", "measure"),
        ("remeasure", "remeasure"),
        ("skip", "skip"),
        ("007", "jump:7"),
        (" jump: 8 ", "jump:8"),
    ],
)
def test_confirmation_is_normalized_before_consumption(
    submitted: str,
    expected: str,
) -> None:
    mailbox = _RouteRunControlMailbox()

    assert mailbox.submit_confirmation(submitted) is True

    assert mailbox.wait_for_confirmation() == expected


@pytest.mark.parametrize("action", ["", "previous", "jump:", "jump:1.5"])
def test_invalid_confirmation_is_rejected(action: str) -> None:
    mailbox = _RouteRunControlMailbox()

    assert mailbox.submit_confirmation(action) is False


def test_latest_pending_confirmation_replaces_older_value() -> None:
    mailbox = _RouteRunControlMailbox()

    assert mailbox.submit_confirmation("next") is True
    assert mailbox.submit_confirmation("skip") is True

    assert mailbox.wait_for_confirmation() == "skip"


def test_stop_takes_precedence_over_pending_confirmation() -> None:
    mailbox = _RouteRunControlMailbox()
    assert mailbox.submit_confirmation("next") is True

    mailbox.request_stop()

    assert mailbox.wait_for_confirmation() == "stop"


def test_pending_confirmation_can_be_cleared_before_waiting() -> None:
    mailbox = _RouteRunControlMailbox()
    assert mailbox.submit_confirmation("next") is True

    mailbox.clear_confirmation()
    mailbox.request_stop()

    assert mailbox.wait_for_confirmation() == "stop"


def test_wait_for_stop_times_out_then_observes_stop() -> None:
    mailbox = _RouteRunControlMailbox()

    assert mailbox.wait_for_stop(0.0) is False
    mailbox.request_stop()
    assert mailbox.wait_for_stop(0.0) is True


def test_pause_does_not_stop_point_execution() -> None:
    mailbox = _RouteRunControlMailbox()

    mailbox.request_pause()
    assert mailbox.point_stop_requested() is False

    mailbox.request_interrupt()
    assert mailbox.point_stop_requested() is True


def test_waiting_callback_runs_outside_lock_and_may_reenter() -> None:
    observed: list[tuple[bool, bool]] = []
    mailbox: _RouteRunControlMailbox

    def waiting_changed(waiting: bool) -> None:
        observed.append((waiting, mailbox.is_waiting()))

    mailbox = _RouteRunControlMailbox(waiting_changed=waiting_changed)
    publisher = threading.Thread(target=mailbox.set_waiting, args=(True,), daemon=True)

    publisher.start()
    publisher.join(timeout=1.0)

    assert not publisher.is_alive()
    assert observed == [(True, True)]


def test_wait_until_waiting_times_out_then_observes_publication() -> None:
    mailbox = _RouteRunControlMailbox()

    assert mailbox.wait_until_waiting(0.0) is False
    mailbox.set_waiting(True)
    assert mailbox.wait_until_waiting(0.0) is True
