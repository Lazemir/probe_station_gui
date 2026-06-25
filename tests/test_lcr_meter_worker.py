from __future__ import annotations

import threading
import unittest

from probe_station_gui.lcr_meter_worker import MeterWorkerCall


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
