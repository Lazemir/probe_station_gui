from __future__ import annotations

import threading
import unittest

from probe_station_gui.lcr_meter_worker import (
    MeterWorkerCall,
    meter_worker_poll_timeout,
)


class MeterWorkerCallTests(unittest.TestCase):
    def test_worker_call_defaults_to_pending_state(self) -> None:
        call = MeterWorkerCall(func=lambda: 42)

        self.assertIsNone(call.done)
        self.assertIsNone(call.result)
        self.assertIsNone(call.error)
        self.assertEqual(call.func(), 42)

    def test_worker_call_can_hold_completion_state(self) -> None:
        done = threading.Event()
        error = RuntimeError("boom")

        call = MeterWorkerCall(
            func=lambda: None,
            done=done,
            result=12,
            error=error,
        )

        self.assertIs(call.done, done)
        self.assertEqual(call.result, 12)
        self.assertIs(call.error, error)


class MeterWorkerPollTimeoutTests(unittest.TestCase):
    def test_poll_timeout_is_disabled_without_live_polling_session_or_stop_clear(
        self,
    ) -> None:
        base_args = {
            "live_polling_enabled": True,
            "stop_polling": False,
            "has_session": True,
            "poll_interval_ms": 250,
        }

        for override in (
            {"live_polling_enabled": False},
            {"stop_polling": True},
            {"has_session": False},
        ):
            with self.subTest(override=override):
                self.assertIsNone(
                    meter_worker_poll_timeout(**(base_args | override))
                )

    def test_poll_timeout_uses_poll_interval_with_minimum(self) -> None:
        self.assertEqual(
            meter_worker_poll_timeout(
                live_polling_enabled=True,
                stop_polling=False,
                has_session=True,
                poll_interval_ms=250,
            ),
            0.25,
        )
        self.assertEqual(
            meter_worker_poll_timeout(
                live_polling_enabled=True,
                stop_polling=False,
                has_session=True,
                poll_interval_ms=20,
            ),
            0.05,
        )
