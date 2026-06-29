# Task 55 Report: Whole-Branch Metrics and Final Review

Date: 2026-06-30
Branch: `codex/refactor-stage-controller`
Baseline: `main`

## Scope

Task 55 compared the current refactor branch against `main`, ran final review, and fixed the review findings that were still structural rather than speculative.

## Main Results

| Metric | `main` | Current branch | Better? |
| --- | ---: | ---: | --- |
| `main.py` LOC | 13424 | 7545 | yes |
| `main.py` Wily cyclomatic | 2661 | 1385 | yes |
| Max cyclomatic complexity | 128 | 39 | yes |
| Average cyclomatic complexity | 3.47 | 2.87 | yes |
| Functions with CC > 10 | 189 | 151 | yes |
| Lizard warning-level functions | 69 | 23 | yes |
| Lizard duplicate blocks | 383 | 406 | no |
| Tests | unknown in this report | 1240 passed, 2 skipped | yes |
| Coverage | not compared | 75% | stable branch gate |
| Public API changed | no | yes, accepted wrapper removal | accepted |

Whole-tree total LOC grew because responsibilities and tests were split across many more files. That is not treated as success by itself. The branch is justified by lower high-end complexity, smaller owner modules, fewer warning-level functions, and better test locality.

## Review Fixes Applied

- Removed shallow `RouteMeasurementRunner` private pass-through methods for route contact readout/seek/status helpers; route contact modules now call the owning implementation module directly.
- Removed the stale `route_external_runtime_settings` alias and updated callers/tests to use `route_common_runtime_settings`.
- Added package markers and a shared `tests/app/import_reset.py` helper so app tests pass under pytest importlib mode and duplicated import-reset code is centralized.
- Kept the accepted public compatibility-wrapper removal as an explicit branch-level API change.

## Verification

- Focused app import-mode suite: `186 passed`.
- Focused route/app wrapper-removal suite: `239 passed`.
- Full suite: `1240 passed, 2 skipped`.
- Coverage run: `1240 passed, 2 skipped`; total coverage `75%`.
- Ruff gate: `ruff check --ignore E402,F401 .` passed.
- Lizard is already `1.23.0`, the current PyPI latest; duplicate detection uses `-Eduplicate`.

## Residual Follow-Ups

- Duplicate blocks worsened by lizard (`383 -> 406`), mainly in API server and route-test shapes. This should be a targeted cleanup, not a broad rewrite.
- Vulture candidates remain suspicious, not automatically dead code.
- Existing serial-terminal ordinary-command needles-state invalidation gap appears inherited from `main` and needs a separate behavior decision.
- Hardware smoke was not run.

## Verdict

This refactor is justified.

Reason:

- behavior preserved: yes for covered behavior; accepted wrapper-removal API break documented;
- tests passed: yes;
- metrics improved: yes on high-end complexity, average complexity, warning-level functions, and owner-module size;
- maintainability improvement: responsibilities are split by route/stage/UI/settings/instrument/test ownership instead of concentrated in giant files;
- new risk introduced: duplicate-block count regressed and should be cleaned up in a follow-up.
