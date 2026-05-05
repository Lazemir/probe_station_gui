# Axis A directional calibration note

Date: 2026-05-04

## Current controller configuration

FluidNC A-axis settings currently loaded:

```yaml
a:
  steps_per_mm: 2600
  max_travel_mm: 5.5
  motor0:
    pulloff_mm: 0.25
```

The allowed working range is `A=0..-5.5`. With `steps_per_mm=2600`,
the measured real displacement over this working range is close enough to
the nominal GCode range: about `5.508 mm` real over `5.5 mm` GCode on the
settled top-to-bottom pass.

## Measured behavior

Axis A has measurable directional hysteresis.

Two settled calibration passes were recorded with `1.0 s` settle time and
`0.01 mm` GCode step:

- Forward / lowering / top-to-bottom:
  `axis_a_spm2600_pulloff0p25_start0p230_to-lowerlimit_step0p01_settle1p0_feed30_oneshot_20260504_223257.npz`
- Reverse / raising / bottom-to-top:
  `axis_a_spm2600_pulloff0p25_reverse_startm5p730_to0p230_step0p01_settle1p0_feed30_oneshot_nozero_20260504_225516.npz`

On the overlapping range the reverse branch differs from the forward branch
by roughly:

```text
mean   +0.0669 mm
median +0.0679 mm
p95     0.0776 mm
max     0.0815 mm
```

A repeat forward pass after a full reverse cycle matched the original forward
branch well on the short tested range:

```text
repeat - base forward:
mean   +0.0027 mm
median +0.0026 mm
p95     0.0040 mm
max     0.0054 mm
```

This means the branch is mostly determined by the final motion direction, not
by long-term drift.

## Random-walk check

Random small reversals were also measured:

`axis_a_spm2600_random_walk_hysteresis_step0p01_settle1p0_nozero_20260504_231921.npz`

Sequence:

```text
+5, -7, +13, -3, -11, +4, -9, +17, -6, +12, -18, +8
```

Findings:

- Upward moves were usually closer to the reverse branch.
- Downward moves were usually closer to the forward branch.
- Immediately after reversing direction, several `0.01 mm` GCode steps can be
  spent taking up backlash/compliance, so the real indicator position does not
  instantly jump to the other branch.

Measured random-walk classification:

```text
up steps:   47 / 59 closer to reverse
down steps: 43 / 54 closer to forward
```

## Intended implementation

The program should support two A-axis calibration curves:

- `forward`: final A movement decreases the GCode coordinate (`A-`, lowering).
- `reverse`: final A movement increases the GCode coordinate (`A+`, raising).

For a requested real A displacement:

1. Determine the intended final approach direction.
2. Use the corresponding calibration curve and invert it to compute the GCode
   target.
3. Track the last A motion direction in software.
4. If the current state has just reversed direction near the target, do not
   assume the stationary point is on the new branch immediately.

For best repeatability, the precise-positioning mode should use a forced final
approach direction:

1. Overshoot the target by a small configured amount.
2. Move back to the target in the chosen final direction.
3. Use the calibration curve for that final direction.

This avoids relying on the transient backlash/compliance region after a
direction reversal.

## Artifacts

Plots:

- `axis_a_spm2600_pulloff0p25_forward_reverse_settle1p0_20260504.png`
- `axis_a_spm2600_repeat_forward_after_reverse_compare_20260504.png`
- `axis_a_spm2600_random_walk_vs_branches_20260504.png`

Configuration files:

- Current A/Z config artifact:
  `config_main_z6335_zmax23_a5p5.yaml`
- Backup before `A pulloff_mm: 0.25`:
  `config_main_backup_before_a_pulloff025_20260504_222406.yaml`
