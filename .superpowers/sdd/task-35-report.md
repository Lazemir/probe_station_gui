# Task 35 Report: Telegram Bot Command Module

Status: DONE_WITH_CONCERNS

## Commits

- `e8b340e refactor: extract Telegram command policy`
- Follow-up: `fix: address Telegram command review`

## Files Changed

- `probe_station_gui/notifications/telegram_commands.py`
- `probe_station_gui/notifications/telegram.py`
- `probe_station_gui/route/telegram_adapter.py`
- `main.py`
- `tests/notifications/test_telegram_commands.py`
- `tests/notifications/test_telegram.py`
- `tests/app/test_main_telegram_commands.py`
- `tests/route/test_telegram_adapter.py`
- `.superpowers/sdd/task-35-report.md`

## Behavior Summary

- Moved Telegram command parsing, command/callback routing policy, help text, keyboard row policy, route/contact photo response policy, route action response policy, and status text formatting into `notifications.telegram_commands`.
- Kept `Main` responsible for Telegram service lifecycle, Qt handoff, Telegram transport entry points, latest camera photo attachment, route confirmation submission, route photo/contact request side effects, and `telegram_inline_keyboard(rows)` construction.
- Moved reusable Telegram send preparation into the existing `notifications.telegram` transport module while preserving `Main._send_telegram_alert()` and `Main._send_telegram_bot_message()` as the Main-facing transport methods.
- Added `route_telegram_state_from_legacy_owner()` to keep `_route_telegram_adapter()` thin while preserving legacy route Telegram state migration.
- Fixed review issue: invalid route callbacks no longer consult API route-control state before policy validation.
- Reformatted the compressed Main Telegram adapter expressions into readable multi-line calls.
- Did not change public APIs intentionally.

## TDD Evidence

- RED: `pytest tests\notifications\test_telegram_commands.py tests\app\test_main_telegram_commands.py -q` failed on missing `notifications.telegram_commands`.
- GREEN: same new tests passed after implementation: `19 passed`.
- RED: `pytest tests\route\test_telegram_adapter.py::RouteTelegramAdapterTest::test_route_telegram_state_from_legacy_owner_preserves_flags_and_photos -q` failed on missing helper.
- GREEN: same helper test passed after implementation: `1 passed`.
- RED: `pytest tests\app\test_main_telegram_commands.py::test_invalid_telegram_route_callback_does_not_consult_api_route_control -q` failed because API route-control state was consulted.
- GREEN: same regression passed after route-action prevalidation fix.
- RED: new transport-helper tests failed on missing `send_telegram_bot_message_for_settings()` and `send_telegram_alert_for_settings()`.
- GREEN: helper tests passed after moving send preparation into `notifications.telegram`.

## Validation

- Focused: `194 passed, 3 subtests passed in 2.29s`
- Full: `1104 passed, 2 skipped in 11.20s`
- Ruff: `All checks passed!`

## Metrics

Baseline:

- `main.py` raw LOC: 9815
- Wily `main.py` cyclomatic: 1865
- `_telegram_status_text`: radon D(26), lizard 55 NLOC / CCN 21

Final:

- `main.py` raw LOC: 9613, SLOC 9033
- `probe_station_gui/notifications/telegram_commands.py` raw LOC: 304, SLOC 262
- Wily temp clone/cache: `main.py` cyclomatic 1795, raw LOC 9613
- `main.py` MI: C (0.00)
- `telegram_commands.py` MI: A (24.93)
- `Main._telegram_status_text`: radon A(1), lizard 2 NLOC / CCN 1
- Lizard summary: `main.py` NLOC 9033, avg CCN 3.8; `telegram_commands.py` NLOC 262, avg CCN 4.1
- Lizard warning count remains 7, all unrelated to Telegram command/status methods.

## Verdict

Strict `main.py <= 9535` was not met. Fallback criteria were met:

- `main.py` reduced by 202 raw LOC from 9815 to 9613.
- `_telegram_status_text` is no longer in radon/lizard warning range.
- Wily `main.py` cyclomatic improved from 1865 to 1795.
- Focused/full tests and ruff pass.
