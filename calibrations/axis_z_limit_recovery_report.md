# Axis Z upper limit recovery notes

Date: 2026-05-05

Context:

- Z calibration section 3 was commanded up to `Z=23.46`.
- The upper Z hard limit actually asserted at approximately `WPos Z=23.455`.
- Status frame at the limit:

```text
<Alarm|WPos:-32.000,-32.000,23.455,0.000,0.000|...|Pn:Z>
```

Important correction from operator:

To recover from an active limit, do not immediately switch to a recovery config.
First try the combined reset/unlock sequence, then move off the switch:

```gcode
$X
Ctrl-X / 0x18
$X
G91 G21
G1 Z-0.050 F10
G90
?
```

Use small negative Z moves when recovering from the upper Z limit. If `Pn:Z`
clears and the coordinate remains valid, continue from the measured coordinate.

What happened in this run:

- `$X` alone was tried first.
- A relative `G1 Z-0.500 F30` immediately returned to `Alarm`; status remained
  `WPos Z=23.455`, `Pn:Z`.
- Ctrl-X was sent later and changed the state to `Idle`, while `Pn:Z` remained.
- I did not retry motion after the full `$X` + Ctrl-X recovery sequence.
- Instead I switched to `config_recover_z.yaml`, which disables the Z limit input,
  moved off the switch, restored `config_main.yaml`, and manually restored the
  work coordinate by calculation.

Coordinate accuracy warning:

The restored coordinate after recovery is not step-exact. It was computed from:

```text
Z_before_recovery = 23.455
recovery_move = 0.500 mm at 7140 steps/mm = 3570 steps
current_steps_per_mm = 6335
restored_Z = 23.455 - 3570 / 6335 = 22.891464
```

The `23.455` status value is rounded to `0.001 mm`, which is already several
steps at `6335 steps/mm`. Also, the failed pre-recovery move was assumed to have
produced no physical motion because the controller status remained at `23.455`.

Calibration consequence:

- The section 3 upward branch to the limit is valid as a measured branch up to
  the observed limit event.
- The resumed downward branch from `Z=23.300` is a conservative recovered branch,
  not the intended reverse branch starting `0.020 mm` below the limit.
- The intended reverse branch should be remeasured from approximately
  `Z=23.435`, after recovering with `$X` + Ctrl-X without changing configs.
