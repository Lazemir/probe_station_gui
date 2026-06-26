# Probe Station GUI

This context describes the probe-station control language and the maintenance language used when refactoring the application without changing external behaviour.

## Refactoring Workflow

For the current architecture refactoring branch, the user has explicitly requested subagents. Use `subagent-driven-development` as the main execution workflow for independent refactor tasks: dispatch a focused implementer subagent per task, run a task-scoped reviewer subagent after each implementation, and run a broad whole-branch review before finishing. This is an explicit authorization to use subagents for this refactoring work without asking again for each independent task, while still keeping conflicting implementation edits sequential and review-gated.

Use `wily` as the primary metrics comparison tool for refactor passes, especially when comparing between commits. On Windows, run it with UTF-8 output enabled, for example `$env:PYTHONIOENCODING='utf-8'; .\.venv\Scripts\python.exe -m wily ...`, because the default cp1251 console can crash on Wily's emoji output. Keep Wily cache outside the repo, such as under `%TEMP%`, unless the user explicitly asks to persist it. Use `radon`/`lizard` as fallback or spot-check tools when Wily cannot produce the required detail.

## Language

**Probe Station**:
A microscope station with a FluidNC-controlled stage, camera feedback, and electrical probes used to position needles on chip targets.
_Avoid_: CNC, microscope app

**Stage**:
The motion system controlled through FluidNC serial commands. It includes linear axes and the A-axis needle lift.
_Avoid_: CNC controller, machine

**Needles Known Raised**:
A safety state meaning the application knows the needles are raised before stage motion. Unknown needle state is not equivalent to raised.
_Avoid_: safe by default, needles ok

**Route Measurement**:
A guided sequence that moves through route points, captures optional photos, handles contact placement, and records measurement results.
_Avoid_: batch measurement, script run

**API Route Control**:
The external route-control workflow exposed through the local API. It is named after the route-control interface, not after any current client.
_Avoid_: Jupyter control, notebook control

**Pause Request**:
A request for route measurement to pause at the next safe waiting point. It is not the same as being paused.
_Avoid_: paused

**Pause Ack**:
The state where the route measurement loop has reached a safe waiting point and may show Resume controls.
_Avoid_: pause requested

**Interrupt**:
A request to stop the current route contact flow at the first regular opportunity without allowing later contact steps to continue as if nothing happened.
_Avoid_: cancel, emergency stop

**Behaviour Parity**:
The expected result of a refactor pass: external behaviour stays stable while the internal module shape changes.
_Avoid_: no-op refactor

**Refactor Pass**:
A small reviewable structural change with a named current behaviour, structural improvement, validation check, and metrics comparison against the previous pass and main.
_Avoid_: rewrite, cleanup batch

**Metrics Gate**:
The repeatable check used to decide whether a refactor pass improved maintainability without breaking behaviour. Prefer `wily` for commit-to-commit comparison, with `radon` and `lizard` used for fallback or deeper spot checks. Total LOC is a secondary signal, not an acceptance rule: LOC may grow when cyclomatic complexity, Maintainability Index, function length, locality, or test protection improve enough to justify the extra code.
_Avoid_: lint run, test run

**Regression Metric**:
A metric that becomes worse after a pass. It must either be explained as measurement noise, fixed by the pass, or split into an explicit cleanup pass before claiming improvement.
_Avoid_: acceptable churn
