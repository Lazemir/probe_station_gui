# Task 24 Report: API Route Artifact And Session Result Adapter

## Scope

- Created `probe_station_gui/route/api_artifacts.py`
- Updated `main.py`
- Added `tests/route/test_api_artifacts.py`
- Updated `tests/app/test_main_coordinate_feedrate.py`

## TDD Evidence

### RED

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_api_artifacts.py -q
```

Observed failure:

- `ModuleNotFoundError: No module named 'probe_station_gui.route.api_artifacts'`

This was the expected first failure because the new route extraction module did not exist yet.

### GREEN

Focused route-module tests:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_api_artifacts.py -q
```

Result:

- `18 passed in 0.07s`

Focused route/app characterization:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_api_artifacts.py tests\app\test_main_coordinate_feedrate.py -q
```

Result:

- `147 passed, 3 subtests passed in 1.92s`

Final full verification:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .
```

Result:

- `888 passed, 2 skipped in 11.11s`
- `All checks passed!`

## Behavior Notes

Preserved:

- API route session status/last-status/404 policy
- API route session action pause / interrupt / stop / confirmation semantics
- API external result 404 / 409 / accepted policy
- API seek 404 / 409 / accepted policy
- Artifact public payload omission of binary `data`
- Artifact private response inclusion of binary `data`
- Artifact record bookkeeping: UUID id, created timestamp, size, metadata copy, binary copy
- Route photo capture frame wait / fallback / unavailable error behavior
- Route photo metadata keys and values
- Telegram requested route photo one-shot consumption and default markup transport
- Final API route session status capture and exception logging behavior

Pause Request / Pause Ack / Interrupt behavior was not changed.

## LOC

- `main.py` before: `11740`
- `main.py` after: `11577`
- delta: `-163`

Gate check:

- required: `main.py <= 11590`
- actual: `11577`
- verdict: pass

## Target Method Metrics

### Lizard before (`7ee9c7e`)

```text
_api_route_session_status: 14 NLOC / CCN 4
_api_route_session_action: 44 NLOC / CCN 6
_api_route_session_result: 23 NLOC / CCN 4
_api_route_session_seek: 18 NLOC / CCN 4
_api_route_session_artifact: 15 NLOC / CCN 3
_api_route_artifacts_payload: 7 NLOC / CCN 2
_add_api_route_artifact: 22 NLOC / CCN 1
_capture_api_route_photo_artifact: 44 NLOC / CCN 5
_store_final_api_route_session_status: 10 NLOC / CCN 5
```

### Lizard after

```text
main.py
_api_route_session_status: 2 NLOC / CCN 1
_api_route_session_action: 2 NLOC / CCN 1
_api_route_session_result: 2 NLOC / CCN 1
_api_route_session_seek: 2 NLOC / CCN 1
_api_route_session_artifact: 2 NLOC / CCN 1
_api_route_artifacts_payload: 2 NLOC / CCN 1
_capture_api_route_photo_artifact: 11 NLOC / CCN 4
_store_final_api_route_session_status: 4 NLOC / CCN 2

probe_station_gui/route/api_artifacts.py
ApiRouteArtifactsStore.add: 23 NLOC / CCN 1
ApiRouteArtifactsStore.public_payloads: 6 NLOC / CCN 2
ApiRouteArtifactsStore.artifact_response: 15 NLOC / CCN 3
api_route_session_status_response: 17 NLOC / CCN 4
api_route_session_result_response: 24 NLOC / CCN 4
api_route_session_seek_response: 17 NLOC / CCN 4
api_route_session_action_response: 45 NLOC / CCN 6
final_api_route_session_status: 13 NLOC / CCN 5
api_route_photo_artifact_metadata: 16 NLOC / CCN 1
api_route_photo_content_type: 4 NLOC / CCN 2
```

### Radon CC before (`7ee9c7e`)

```text
Main._api_route_session_action - B (6)
Main._capture_api_route_photo_artifact - A (5)
Main._store_final_api_route_session_status - A (5)
Main._api_route_session_status - A (4)
Main._api_route_session_result - A (4)
Main._api_route_session_seek - A (4)
Main._api_route_session_artifact - A (3)
Main._api_route_artifacts_payload - A (2)
Main._add_api_route_artifact - A (1)
```

### Radon CC after

```text
Main._capture_api_route_photo_artifact - A (4)
Main._store_final_api_route_session_status - A (2)
Main._api_route_session_status - A (1)
Main._api_route_session_action - A (1)
Main._api_route_session_result - A (1)
Main._api_route_session_seek - A (1)
Main._api_route_session_artifact - A (1)
Main._api_route_artifacts_payload - A (1)

api_route_session_action_response - B (6)
final_api_route_session_status - A (5)
api_route_session_status_response - A (4)
api_route_session_result_response - A (4)
api_route_session_seek_response - A (4)
ApiRouteArtifactsStore.artifact_response - A (3)
api_route_photo_content_type - A (2)
ApiRouteArtifactsStore.public_payloads - A (2)
api_route_photo_artifact_metadata - A (1)
ApiRouteArtifactsStore.add - A (1)
```

### Radon MI

```text
before: main.py - C (0.00)
after:  main.py - C (0.00)
after:  probe_station_gui/route/api_artifacts.py - A (34.21)
```

## Wily Diff From `7ee9c7e`

Command set was run with forced UTF-8 in a temporary clean local clone carrying the final patch, because `wily build` refuses a dirty repository.

```text
main.py: cyclomatic 2271 -> 2250, raw.loc 11740 -> 11577, maintainability.mi 0 -> 0
probe_station_gui/route/api_artifacts.py: cyclomatic - -> 40, raw.loc - -> 240, maintainability.mi - -> 34.20971377608331

main.py:Main._capture_api_route_photo_artifact: 5 -> 4
main.py:Main._store_final_api_route_session_status: 5 -> 2
main.py:Main._api_route_session_status: 4 -> 1
main.py:Main._api_route_session_action: 6 -> 1
main.py:Main._api_route_session_result: 4 -> 1
main.py:Main._api_route_session_seek: 4 -> 1
main.py:Main._api_route_session_artifact: 3 -> 1
main.py:Main._api_route_artifacts_payload: 2 -> 1
main.py:Main._api_route_artifacts_store: - -> 1
```

## Self-Review

- Scope stayed inside API route session/artifact response policy and artifact bookkeeping.
- No change to route session start runner construction.
- No change to `_api_measure_current_contact`.
- No change to camera ownership or Telegram transport internals.
- Added route-module tests first, then integration characterization where Main behavior needed pinning.

## Verdict

- behavior preserved: yes
- tests passed: yes
- metrics improved: yes
- LOC gate passed: yes
- maintainability improvement: yes, response/artifact policy is now isolated in `probe_station_gui.route.api_artifacts` and `main.py` route-session wrappers are trivial
- new risk introduced: low; the main residual risk is that `ApiRouteArtifactsStore` is instantiated on demand from legacy attributes, so tests depend on those attributes being kept in sync when future code touches artifact state
