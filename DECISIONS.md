# Decisions

Standing decisions (ratified), and decisions still to take. Method
doctrine lives in the procedure docstrings; this file tracks the
project-level choices.

## Ratified

- **Qualification contract**: every timing window derives from
  `qualification_frequency` / `qualification_duty_cycle` (defaults
  100 MHz / 0.5). A cell that cannot settle inside its contract is
  disqualified, not measured. No naked temporal constant outside the
  reset conditioning pair (banner'd until sequencing feeds it the
  measured mpw).
- **Criteria** (348-point full-grid election against the official
  sg13g2 tables, TT 1.2 V 25 C): house-wide x1.2 pushout criterion
  for setup / hold / recovery / min_pulse_width; hold disturbance
  depth 0.1 kept as certificate witness only (results insensitive).
  Published constraints follow this house convention — deviations
  vs vendor tables (setup avg ~15 % relative) are a convention
  difference, to be stated in the docs, not a bug.
- **Sentinel discipline**: a failed measurement is a fail verdict;
  certificates re-verify the believed bound; saturation on a domain
  bound is a disqualification.
- **Service edges are not measured edges**: conditioning/reference
  data edges carry the fastest axis slew; the single-cycle setup
  reference is the data pin pinned at the rail. Only the probed edge
  carries the axis slew.
- **Honest netlists**: session-driven sources are built as empty DC
  sources; the only waveform a netlist shows is one actually
  simulated (the reset conditioning pulse).
- **Measurement DB interchange** (adopted, to implement): one plain
  Tcl-array file per cell per corner
  (`set DB(<pin>:<related>:<timing_type>:<lut>,<axis1>[,<axis2>]) <SI value>`),
  axes in lib units self-enumerating, provenance in `#` header,
  missing key = not measured, sorted whole-entry writes. CharLib will
  (1) import per-cell DB files declared in the cell yml, (2) dump its
  own measurements incrementally in the same format (doubles as the
  crash checkpoint), (3) offer a standalone db -> liberty export.
  Cell declaration (pins, functions, area, units) stays in the yml:
  the DB only says what was measured.
- **Comparisons between tools happen on values, never on file text.**
  No cross-tool formatting constraints.

## To take

- **Capacitance reduction policy**: the DB carries capacitance per
  direction x slew x companion-state combo; liberty wants one scalar
  per pin. INTERIM: worst case over everything. To ratify against
  the official tables before the DB import ships.
- **Clamped-corner semantics** (see KNOWN-BUGS +37 ps): publish the
  clamped value, or disqualify the corner point?
- **internal_power decode**: measurement method to establish before
  any power table is emitted (see WISHLIST: spec first).
