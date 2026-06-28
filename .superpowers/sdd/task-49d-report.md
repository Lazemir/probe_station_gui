# Task 49d Report: LCR Worker Runtime Extraction

Baseline commit: `24a0a5e`
Result commit: pending
Wily temp root: `C:\Users\Lazemir\AppData\Local\Temp\probe_station_gui_wily_task49d_final_20260628172652`

## Current Behavior

`LCRMeterController` owned the worker queue, thread startup, synchronous `run`, asynchronous `submit`, idle polling, idle waiting, shutdown signaling, and live-output cleanup coordination inline with instrument connection/read behavior.

## Structural Improvement

Deepened `probe_station_gui/instruments/meters/worker.py` from a dataclass/helper module into `MeterWorkerRuntime`, an internal runtime module that owns:

- queue/thread lifecycle;
- serialized synchronous calls;
- one-at-a-time asynchronous calls;
- idle polling wakeup and timeout;
- idle wait state;
- shutdown signaling and worker-thread reentrancy checks.

`LCRMeterController` remains the public facade and still owns hardware-specific actions, Qt signals, polling policy, and live output context behavior.

## Validation

- Focused worker/LCR tests: `50 passed, 3 subtests passed`
- Full suite with coverage: `1232 passed, 2 skipped`
- Ruff: `ruff check --ignore E402,F401 .` passed
- Coverage: `74%` total
- Lizard: no warning-level functions in the touched LCR/worker slice

## Metrics

| Metric | Before | After | Better? |
| --- | ---: | ---: | --- |
| `lcr.py` LOC | 1495 | 1398 | yes |
| `lcr.py` Wily cyclomatic | 300 | 273 | yes |
| `lcr.py` Maintainability Index | 0 | 0 | flat |
| `worker.py` LOC | 27 | 201 | no |
| `worker.py` Wily cyclomatic | 5 | 47 | no |
| `worker.py` MI | 58.89 | 27.15 | no |
| `LCRMeterController.wait_until_idle` CC | 8 | 1 | yes |
| `LCRMeterController._run_on_meter_worker` CC | 4 | 1 | yes |
| `LCRMeterController._submit_meter_worker_call` CC | 3 | 1 | yes |
| `LCRMeterController.shutdown` CC | 6 | 3 | yes |
| `LCRMeterController._stop_polling_session` CC | 5 | 3 | yes |
| New `MeterWorkerRuntime` max CC | - | 7 | n/a |
| Tests | 1226 passed, 2 skipped | 1232 passed, 2 skipped | yes |
| Coverage | 74% | 74% | flat |
| Public API changed | no | no | yes |

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes;
- tests passed: yes;
- metrics improved: yes for the target monolith and controller hotspots;
- maintainability improvement: serialized worker scheduling now has a single place-to-change and dedicated tests, instead of being interleaved with instrument protocol behavior;
- new risk introduced: medium, because worker scheduling is timing-sensitive; mitigated by focused runtime tests for run/submit/idle polling/reentrancy/blocked idle wait/shutdown and existing controller serialization/polling tests.

Tradeoff:

`worker.py` is larger and has lower MI because it now owns real runtime behavior instead of being a shallow dataclass module. This is acceptable here because the new module is deep, directly tested, has max CC below warning thresholds, and removes worker mechanics from `lcr.py`.
