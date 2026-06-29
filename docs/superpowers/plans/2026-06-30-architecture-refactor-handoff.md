# Architecture Refactor Handoff

Date: 2026-06-30
Branch: `codex/refactor-stage-controller`
Source review: `C:\Users\Lazemir\AppData\Local\Temp\architecture-review-20260625-014631.html`

## Status

The HTML architecture review track is complete for this branch. The branch does not claim that the application is finished forever; it claims that the six review directions were either implemented to the planned depth or converted into explicit follow-up work.

Final verification for the last hardening step:

- full suite: `1240 passed, 2 skipped`;
- ruff gate: `ruff check --ignore E402,F401 .` passed;
- coverage: `75%`;
- `lizard`: installed version `1.23.0`, duplicate detection uses `-Eduplicate`;
- public API changed: yes, accepted removal of legacy compatibility wrappers.

## HTML Review Coverage

| HTML direction | Status | Result | Remaining work |
| --- | --- | --- | --- |
| Deepen the route operation module | Complete for this branch | Route control, session start, confirmation, runtime presentation, Telegram route behavior, API artifacts/current-contact planning, contact lifecycle/readout, and result recording were moved into route modules. Old `route_measurement.py` is replaced by `probe_station_gui/route/measurement.py`; current `radon raw` LOC is `2267`. | Duplicate cleanup in route tests; `route/measurement.py` is still a large coordinator but no longer the primary hidden-state sink. |
| Deepen the stage motion and FluidNC seam | Complete for this branch | Old `probe_station_gui/stage_controller.py` was split into focused stage modules: controller, motion commands/planning, FluidNC session/config/status seams, jog queue, needle state, coordinate targets, position update, and API move planning. Explicit serial parameters were removed from the targeted internal helpers where the controller/session seam already owns serial access. | Existing serial-terminal ordinary-command needles-state invalidation gap appears inherited from `main`; decide separately whether to make every manual terminal command invalidate known-raised state. |
| Split settings into deeper section modules | Complete for this branch | Settings parsing/default-file behavior, feedrate/objective/needle/measurement sections, and settings-dialog widgets were split. Current `settings/manager.py` is `1006` LOC and `dialogs/settings_dialog.py` is `1091` LOC by `radon raw`. | Remaining inline settings-dialog sections are below the current size target; split only when changing those settings. |
| Separate instrument worker from meter adapters | Complete for LCR path | LCR VISA dispatch, route-session helpers, GW Instek session implementation, and worker runtime were split from `instruments/meters/lcr.py`; tests were split by responsibility. Current `lcr.py` is `1398` LOC by `radon raw`. | Keithley driver internals were not refactored; treat that as a separate driver-adapter task if it becomes a change hotspot. |
| Deepen design navigation and route editing | Complete for this branch | Design navigation, objective alignment, design plot pane, enablement policy, and main-window design adapters were split. Current `views/design_navigator_panel.py` is `1849` LOC by `radon raw`. | `views/microscope_view.py` remains a future UI hotspot, but it was not one of the selected HTML completion blockers. |
| Turn route dialog helpers into presentation modules | Complete for this branch | Route dialog profile/default persistence, result presentation, histogram painting, operation-state policy, and profile/meter helpers were split. Current `dialogs/route_measurement_dialog.py` is `1387` LOC by `radon raw`. | Do not rewrite the dialog wholesale; split future UI pieces only when a change touches them. |

## Metrics Summary

Compared with `main` during Task 55:

| Metric | `main` | Current branch | Direction |
| --- | ---: | ---: | --- |
| `main.py` LOC | 13424 | 7545 | better |
| `main.py` Wily cyclomatic | 2661 | 1385 | better |
| Max cyclomatic complexity | 128 | 39 | better |
| Average cyclomatic complexity | 3.47 | 2.87 | better |
| Functions with CC > 10 | 189 | 151 | better |
| Lizard warning-level functions | 69 | 23 | better |
| Lizard duplicate blocks | 383 | 406 | worse |

The duplicate-block regression is the main known metrics regression. Do not hide it in a broad rewrite; handle it as a targeted follow-up.

## Follow-Up Tracks

1. Duplicate cleanup: start with `probe_station_gui/api/server.py` and route-test clone shapes reported by `lizard -Eduplicate`.
2. Dead-code audit: review vulture candidates manually; do not delete protocol imports, public parameters, or test seams just because vulture reports them.
3. Serial safety decision: decide whether ordinary manual terminal commands should invalidate known-raised needles state the same way Ctrl+X/manual disruptive flows do.
4. Hardware smoke: run the GUI against the actual FluidNC/camera/LCR setup before trusting this branch operationally.
5. Optional UI hotspot: `views/microscope_view.py` still mixes rendering, overlay state, minimap, input handling, and zoom behavior.

## Next-Agent Rules

- Keep behavior stable unless the user explicitly asks for a functional change.
- Use Wily from a disposable UTF-8 temp clone/cache, not from this checkout.
- Keep route Pause Request, Pause Ack, and Interrupt semantics safety-critical.
- Do not use total repository LOC as a success metric by itself; require lower complexity, clearer locality, fewer duplicates, or better test protection.
- When using lizard duplicate detection, run `lizard -Eduplicate ...`.
