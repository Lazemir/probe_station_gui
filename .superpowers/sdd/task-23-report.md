# Task 23 Report: Route Telegram Photo Adapter

## Scope

- Added `probe_station_gui/route/telegram_adapter.py`.
- Reduced `main.py` by moving route Telegram/photo state, route photo capture/record helpers, contact photo capture, attention alert composition, and related formatting into the route module.
- Added `tests/route/test_telegram_adapter.py`.

## Exact LOC Gate

Source-of-truth commands:

```powershell
git show 07a1152:main.py | C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -c "import sys; print(len(sys.stdin.read().splitlines()))"
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -c "from pathlib import Path; print(len(Path('main.py').read_text(encoding='utf-8').splitlines()))"
```

Results:

- `main.py` physical lines before: `11978`
- `main.py` physical lines after: `11740`
- exact delta: `-238`
- required minimum: `main.py <= 11828`
- target: `main.py <= 11758`

Verdict:

- minimum gate: passed
- task target: passed

## TDD Evidence

### RED 1

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_telegram_adapter.py -q
```

Result:

- failed during collection with `ModuleNotFoundError: No module named 'probe_station_gui.route.telegram_adapter'`.

### GREEN 1

Same command after the initial adapter implementation:

- `10 passed in 0.75s`

### RED 2

Continuation test-first step for the deeper adapter extraction:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_telegram_adapter.py -q
```

Result before the second refactor:

- failed during collection with `ImportError: cannot import name 'capture_route_photo' from 'probe_station_gui.route.telegram_adapter'`.

### GREEN 2

Same command after the deeper extraction:

- `13 passed in 0.68s`

Focused integration check:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests\route\test_telegram_adapter.py tests\app\test_main_coordinate_feedrate.py -q
```

- `139 passed, 3 subtests passed in 1.81s`

## Validation

Full test run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests
```

- `867 passed, 2 skipped in 11.23s`

Ruff:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m ruff check --ignore E402,F401 .
```

- `All checks passed!`

## Metrics

Exact current file line counts:

- `main.py`: `11740`
- `probe_station_gui/route/telegram_adapter.py`: `653`

### Lizard

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m lizard main.py probe_station_gui\route\telegram_adapter.py -l python -C 10 -L 50 --sort cyclomatic_complexity
```

Summary:

- `main.py`: `NLOC 11118`, `Avg.NLOC 20.8`, `AvgCCN 4.4`, `function_cnt 516`
- `probe_station_gui/route/telegram_adapter.py`: `NLOC 602`, `Avg.NLOC 17.9`, `AvgCCN 3.2`, `function_cnt 31`

Relevant extracted/current cluster:

- `capture_route_photo`: CCN `6`
- `route_finish_telegram_payload`: CCN `6`
- `combine_telegram_contact_photos`: CCN `9`
- `RouteTelegramPhotoState.record_route_photo`: CCN `5`
- `RouteTelegramPhotoState.capture_pre_contact_photo`: CCN `4`
- `RouteTelegramPhotoState.capture_contact_photo`: CCN `4`
- `RouteTelegramPhotoState.send_route_attention_alert`: CCN `7`
- `Main._capture_route_photo`: CCN `2`
- `Main._record_route_photo`: CCN `2`
- `Main._send_route_attention_alert`: CCN `2`
- `Main._on_route_measurement_finished`: CCN `7`

### Radon CC

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon cc -s main.py probe_station_gui\route\telegram_adapter.py
```

Relevant extracted/current cluster:

- `capture_route_photo`: `B (6)`
- `route_finish_telegram_payload`: `B (6)`
- `combine_telegram_contact_photos`: `B (9)`
- `RouteTelegramPhotoState.send_route_attention_alert`: `B (7)`
- `RouteTelegramPhotoState.record_route_photo`: `A (5)`
- `RouteTelegramPhotoState.capture_pre_contact_photo`: `A (4)`
- `RouteTelegramPhotoState.capture_contact_photo`: `A (4)`
- `Main._capture_route_photo`: `A (2)`
- `Main._record_route_photo`: `A (2)`
- `Main._capture_route_pre_contact_photo`: `A (1)`
- `Main._capture_route_contact_photo`: `A (1)`
- `Main._send_route_attention_alert`: `A (2)`

### Radon MI

Command:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m radon mi -s main.py probe_station_gui\route\telegram_adapter.py
```

Result:

- `main.py - C (0.00)`
- `probe_station_gui/route/telegram_adapter.py - B (13.41)`

## Wily

Required commands:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c $env:TEMP\probe_station_gui_wily_task23 build main.py probe_station_gui\route\telegram_adapter.py
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -X utf8 -m wily -c $env:TEMP\probe_station_gui_wily_task23 diff main.py probe_station_gui\route\telegram_adapter.py --detail -r 07a1152 --metrics cyclomatic.complexity,raw.loc,maintainability.mi --no-wrap
```

Clean-commit results:

- `main.py`: cyclomatic complexity `2331 -> 2271`
- `main.py`: raw LOC `11978 -> 11740`
- `main.py`: maintainability index `0 -> 0`
- `probe_station_gui/route/telegram_adapter.py`: new file, cyclomatic complexity `102`, raw LOC `653`, maintainability index `13.405573580853957`

Relevant per-function Wily changes:

- `Main._capture_route_photo`: `7 -> 2`
- `Main._record_route_photo`: `5 -> 2`
- `Main._send_route_attention_alert`: `7 -> 2`
- `Main._capture_route_pre_contact_photo`: `6 -> 1`
- `Main._capture_route_contact_photo`: `11 -> 1`
- `Main._matching_route_pre_contact_photo`: `4 -> 1`
- `Main._take_pending_telegram_contact_photos`: `3 -> 1`
- `Main._latest_route_contact_failure_telegram_photos`: `3 -> 1`
- `Main._combine_telegram_contact_photos`: `9 -> 1`
- `Main._telegram_contact_photo_payload`: `4 -> 3`
- `Main._capture_api_route_photo_artifact`: `6 -> 5`

Notes:

- this clean-commit Wily build indexed only the clean revisions needed for the final comparison in the temporary cache
- one unrelated function moved slightly the other way: `Main._on_route_measurement_finished` changed `5 -> 7` after inlining the finish-payload send

## Behavior Check

- Telegram bot service setup remained in `Main`: yes
- Telegram transport remained in `Main`: yes
- Camera frame acquisition ownership remained in `Main`: yes, through callbacks
- route Pause Request / Pause Ack / Interrupt semantics changed: no
- route finish still uses `route_finish_telegram_text(...)` through `route_finish_telegram_payload(...)`: yes
- requested route photo behavior preserved: yes
- contact comparison behavior preserved: yes
- route attention de-duplication preserved: yes

## Self-Review

- The new route module now owns the route-specific Telegram/photo state and most route-specific photo/caption behavior.
- `Main` still owns only the camera-frame source, Telegram sending transport, and route-runner callback wiring.
- The compatibility shim in `Main._route_telegram_adapter()` remains to support `Main.__new__(Main)` test fixtures without changing runtime behavior.

## Verdict

- behavior preserved: yes
- tests passed: yes
- metrics improved: yes
- LOC gate passed: yes
- maintainability improvement: the remaining route Telegram/photo wrappers in `Main` are now shallow, and the route-specific logic is concentrated in `probe_station_gui.route.telegram_adapter`
- new risk introduced: low; the adapter now has a wider callback surface, and `Main._route_telegram_adapter()` still bridges legacy attribute-based fixtures
