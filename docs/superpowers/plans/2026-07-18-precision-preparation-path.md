# Precision Preparation Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the mixed-quadrant hook by moving every non-prepared target axis directly to its final coordinate during the preparation segment.

**Architecture:** Keep `PrecisionApproachPlanner` responsible for the pure two-segment geometry. Change only the fallback value used in an existing preparation segment, then expose the calculated segments in debug logging at the executor boundary.

**Tech Stack:** Python 3.11+, pytest, existing `PrecisionApproachPlanner` and `StageControllerPrecisionMotionMixin`.

## Global Constraints

- An axis that needs preparation receives `target - direction * backlash`.
- Every other requested axis receives its final target in the preparation segment.
- If no axis needs preparation, send only the final segment.
- Preserve validation of every segment, motion safety, cancellation between segments, route-control interrupt propagation, and coordinate-confidence transitions.
- Do not run hardware-dependent code.
- Use `C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe` for every Python command.

---

### Task 1: Correct preparation geometry and log the actual segments

**Files:**
- Modify: `probe_station_gui/stage/precision_approach.py:47-57`
- Modify: `probe_station_gui/stage/precision_motion.py:1-205`
- Test: `tests/stage/test_precision_approach.py`
- Test: `tests/stage/test_precision_motion.py`

**Interfaces:**
- Consumes: `PrecisionApproachPlanner.plan(...) -> PrecisionMovePlan`.
- Produces: unchanged `PrecisionMovePlan`; only `preparation_target` values change for axes outside `prepared_axes`.

- [ ] **Step 1: Replace the old hold-position assertion with the intended mixed-axis geometry**

Rename `test_planner_holds_other_target_axes_during_mixed_axis_preparation` to
`test_planner_moves_nonprepared_axes_directly_to_final_during_preparation` and
assert the non-prepared axes use their final values:

```python
assert plan.preparation_target == {
    "X": pytest.approx(10.0),
    "Z": pytest.approx(4.98),
    "B": pytest.approx(0.5),
}
```

Add the reported quadrant-II/quadrant-IV regression:

```python
@pytest.mark.parametrize(
    ("target", "prepared_axis", "expected_preparation"),
    [
        ({"X": 0.024, "Y": -0.026}, "Y", {"X": 0.024, "Y": -0.031}),
        ({"X": -0.031, "Y": 0.027}, "X", {"X": -0.036, "Y": 0.027}),
    ],
)
def test_mixed_sign_xy_preparation_keeps_other_axis_at_final_target(
    target, prepared_axis, expected_preparation
) -> None:
    exact = AxisCoordinateConfidence(exact=True, loaded_direction=1)
    plan = PrecisionApproachPlanner().plan(
        current={"X": 0.0, "Y": 0.0},
        target=target,
        profiles={"X": _profile(backlash=0.005), "Y": _profile(backlash=0.005)},
        confidence={"X": exact, "Y": exact},
        validate_target=lambda _values: None,
    )
    assert plan.prepared_axes == frozenset({prepared_axis})
    assert plan.preparation_target == pytest.approx(expected_preparation)
    assert plan.final_target == target
```

- [ ] **Step 2: Run the planner regressions and verify RED**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_precision_approach.py -q
```

Expected: the renamed test reports current X/B values and both mixed-sign cases report `0.0` for the non-prepared axis.

- [ ] **Step 3: Use the final target for non-prepared axes**

In `PrecisionApproachPlanner.plan`, change the preparation mapping to:

```python
preparation_target = {
    axis: (
        final_target[axis]
        - profiles.get(axis, _DISABLED_PROFILE).final_direction
        * profiles.get(axis, _DISABLED_PROFILE).backlash
        if axis in prepared_axes
        else final_target[axis]
    )
    for axis in final_target
}
```

- [ ] **Step 4: Add debug evidence at the executor boundary**

Add `import logging`, `logger = logging.getLogger(__name__)`, and immediately
after `segments` is built log only the actual target mappings:

```python
logger.debug(
    "Precision move plan: prepared=%s preparation=%s final=%s",
    sorted(plan.prepared_axes),
    dict(plan.preparation_target or {}),
    dict(plan.final_target),
)
```

Add a `caplog` assertion in `tests/stage/test_precision_motion.py` around an
existing two-segment executor fixture, checking that the record contains
`Precision move plan`, `preparation`, and `final`.

- [ ] **Step 5: Run focused movement tests and verify GREEN**

Run:

```powershell
C:\Users\Public\code\probe_station_gui\.venv\Scripts\python.exe -m pytest tests/stage/test_precision_approach.py tests/stage/test_precision_motion.py tests/stage/test_controller_click_move.py tests/route/test_measurement.py -q
```

Expected: all selected tests pass; the route interrupt-after-preparation test
continues to prove that the final segment and contact work are skipped.

- [ ] **Step 6: Commit the independently testable path fix**

```powershell
git add probe_station_gui/stage/precision_approach.py probe_station_gui/stage/precision_motion.py tests/stage/test_precision_approach.py tests/stage/test_precision_motion.py
git commit -m "fix: keep precision moves on direct path"
```
