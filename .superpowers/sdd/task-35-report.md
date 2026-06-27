# Task 35 Report: Telegram Bot Command Module

Status: DONE_WITH_CONCERNS

Commit: final task commit in this branch (`git log -1 --oneline` after commit).

## Files Changed

- `probe_station_gui/notifications/telegram_commands.py`
- `main.py`
- `probe_station_gui/route/telegram_adapter.py`
- `tests/notifications/test_telegram_commands.py`
- `tests/app/test_main_telegram_commands.py`
- `tests/route/test_telegram_adapter.py`
- `.superpowers/sdd/task-35-report.md`

## Behavior Summary

- Moved Telegram command parsing, command/callback routing policy, help text, keyboard row policy, route/contact photo response policy, route action response policy, and status text formatting into `notifications.telegram_commands`.
- Kept `Main` responsible for Telegram service lifecycle, Qt handoff, Telegram transport, latest camera photo attachment, route confirmation submission, route photo/contact request side effects, and `telegram_inline_keyboard(rows)` construction.
- Added `route_telegram_state_from_legacy_owner()` to keep `_route_telegram_adapter()` thin while preserving legacy route Telegram state migration.
- Did not change public APIs intentionally.

## TDD Evidence

- RED: `pytest tests\notifications\test_telegram_commands.py tests\app\test_main_telegram_commands.py -q` failed on missing `notifications.telegram_commands`.
- GREEN: same new tests passed after implementation: `19 passed`.
- RED: `pytest tests\route\test_telegram_adapter.py::RouteTelegramAdapterTest::test_route_telegram_state_from_legacy_owner_preserves_flags_and_photos -q` failed on missing helper.
- GREEN: same helper test passed after implementation: `1 passed`.

## Validation

- Focused: `191 passed, 3 subtests passed in 2.25s`
- Full: `1101 passed, 2 skipped in 11.15s`
- Ruff: `All checks passed!`

## Metrics

Baseline:

- `main.py` raw LOC: 9815
- Wily `main.py` cyclomatic: 1865
- `_telegram_status_text`: radon D(26), lizard 55 NLOC / CCN 21

Final:

- `main.py` raw LOC: 9615, SLOC 9033
- `probe_station_gui/notifications/telegram_commands.py` raw LOC: 292, SLOC 254
- Wily temp clone/cache: `main.py` cyclomatic 1822, raw LOC 9615
- `main.py` MI: C (0.00)
- `telegram_commands.py` MI: A (25.90)
- `Main._telegram_status_text`: radon A(1), lizard 2 NLOC / CCN 1
- Lizard summary: `main.py` NLOC 9033, avg CCN 3.8; `telegram_commands.py` NLOC 254, avg CCN 4.5
- Lizard warning count remains 7, all unrelated to Telegram command/status methods.

## Verdict

Strict `main.py <= 9535` was not met. Fallback criteria were met:

- `main.py` reduced by exactly 200 raw LOC from 9815 to 9615.
- `_telegram_status_text` is no longer in radon/lizard warning range.
- Wily `main.py` cyclomatic improved from 1865 to 1822.
- Focused/full tests and ruff pass.
