# Known bugs & open anomalies

Observed on the sg13g2 PDK sweep (74 cells, TT 1.2 V 25 C, official
7x7 axes) and on the calibration benches. Ordered by suspected impact.

## Measurement anomalies (unexplained, reproducible)

- **Clamped-corner hold/removal +37 ps** (dfrbp_1 AND dfrbp_2, same
  corner): where the bisection lower bound engages its domain clamp,
  the published value deviates +37 ps from the official table while
  the rest of the grid sits within 5-20 ps. Reproduced on two drives —
  systematic, not noise. Candidate next investigation.
- **Constraint grids come out 3x4** on dfrbp against official 4x4
  axes: one slew row missing. To verify: config axes choice vs a
  still-failing point.
- **Transitions diverge at the 1 fF / slow-slew corner** on most
  combinational families (up to x5.5 on dlygate) while delays stay
  within 2-5 %.
- **Input capacitance low by 17-33 %** on buf (drive >= 8) and nor2b
  families (worst: nor2b cap(A) -33 %).
- **Stacked-transistor leakage low by 25-54 %** (inv -25 %, nor3
  -46 %, nor4 -54 %): deviation grows with stack depth.
- **Non-unate cells (xor2/xnor2)**: worst-case arc selection optimistic
  on one polarity; deviations x2 vs unate cells.

## Liberty rendering

- **Two storage groups instead of one**: a two-state declaration
  (DS0000, DS0001 = !D) emits one ff/latch group per state variable
  where vendor libs write a single group with two variables
  (ff (IQ,IQN)). Cosmetic in general, but WRONG on the second group
  when a dominant is declared: it repeats e.g. preset : "Sb'" for the
  INVERTED variable, which that pin actually clears. Fix: unify into
  one group with two variables (the pairs mechanism is the natural
  vehicle).
- **clear_preset_var1/var2 missing**: the schema cannot declare the
  both-dominants-asserted behavior, so two-dominant cells (sdfbbp)
  render without it. A cell-config key and five lines.

## Infrastructure

- **A run that dies writes nothing**: the library exists only in
  memory until the final write. A worker segfault (BrokenProcessPool)
  aborts everything even with `omit_on_failure`. Mitigation decided:
  incremental measurement dump (see DECISIONS).
- **Unknown cell yml keys are dropped silently** (`cell.py`
  whitelist): a typo in a parameter name dies without a warning.
- **Exit code 2 after a successful library write** observed once
  (all groups present and correct); died in pool teardown.
  Not reproduced after config fix; mechanism not established.
