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
- **Measurement DB interchange** (adopted, implemented): one plain
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
- **Latches render as liberty latch groups, synchronous dominants are
  declared gated** (adopted, implemented 16/10). The cell yml declares
  a latch by its `enable` role (`enable: GATE`, `not GATE_N`), never
  by `clock` (which renders an ff group) ; the enable pin carries
  `clock : true` as vendor libs do (STA needs it for the setup/hold
  checks against it). A set/reset in the gated form `<gate> & <pin>`
  (`set: LATCH & DP`) is SYNCHRONOUS : it acts while the gate is up,
  and renders `preset : "LATCH & DP"` on a data latch. A gated SR
  latch (no data operand in the state function, `state: DS0000 =
  DP & !DN`) renders as a D-latch transparent while the gate and a
  dominant are up (`enable : "LATCH & (DP | DN)"`, `data_in : "DP &
  !DN"`) : standard liberty, readable by yosys, which refuses a latch
  group without data_in/enable. Exact but for the both-asserted
  state. The set/reset roles stay on
  the dominant pins : the assert arcs' timing_sense derives from them.
  **Hybrid since 17/10** : the gated dominants are ALSO emitted as the
  group's `preset`/`clear` attributes (`preset : "LATCH & DP"`), which
  is where liberty describes the both-asserted state :
  `clear_preset_var1/2` = X/X when that state is declared `illegal:`
  (its exit is a race, the state is lost : Q=X, QN=!Q, !X is X), else
  the state functions evaluated with the dominants asserted (L/L for
  the SR), nothing when not derivable. Read by OpenSTA and by yosys
  (-lib and whitebox) ; yosys ignores the vars and keeps QN=!QP, so the
  gain is documentary for our tools, and the legal states render the
  same logic as before.

- **Usage-illegal states leave the worst-case fold** (adopted,
  implemented 17/10). A state the ARCHITECTURE forbids (the gated SR
  latch with both dominants up under the open enable : its exit is a
  race) is not a truth of the cell, so the characterizer measures it
  all the same and the db keeps it. The decision to leave it out is
  the cell owner's, declared in the cell yml as conjunctions of pin
  literals (`illegal: ["LATCH & DP & DN"]`), and honoured by this
  consumer at fold time only : a conditioned entry whose conditioning,
  with the toggling pin at either level, satisfies a rule leaves the
  fold while a legal sibling exists at the same point. The fold never
  empties an arc. A rule naming a pin the conditioning does not
  determine does not match (LATCH mpw under DP1 stays). Effect on the
  gated SR latch : LATCH 6.1 -> 4.7 fF, DP/DN 3.2 -> 2.5 fF ; nothing
  else moves. The CharLib code never knows a pin name : no cell
  knowledge in the consumer.

- **Capacitance reduction** (ratified 17/10 evening against the IHP
  sg13g2 typ tables, "moyennes de moyennes"): per direction the MEAN
  over slews and companion conditionings, minus the states the yml
  declares illegal ; the spread goes to `rise/fall_capacitance_range
  (min, max)` as the vendor libs do ; the pin scalar `capacitance` is
  the mean of the two directions. The measurement itself is settled
  (vernier : charge integration over the whole transition, Miller
  included, slew-independent to 1 %) ; the spread is physical — nand2 A
  : 2.25 fF with B=0, 3.75 fF with B=1, the output toggles only then.
  Why the mean : the former worst case sat +17 to +32 % above IHP on
  every pin of nand2/nand3/nor2, the mean within -10/+12 % :
    nand2 A rise  worst 3.72 / mean 2.99 / IHP 2.93 ;  A fall 3.75 / 3.07 / 2.84
    nand2 B rise  3.68 / 3.27 / 3.10 ;  nand3 A rise 3.78 / 2.65 / 2.92
    nor2 B fall   3.74 / 2.97 / 2.85
  A worst case on every net adds up along a path ; the vendor does not
  publish it, it publishes the range. Both DB capacitance encodings are
  read (conditioning field per the addendum, and the legacy integer
  combo axis as an opaque coordinate).

## To take
- **Clamped-corner semantics** (see KNOWN-BUGS +37 ps): publish the
  clamped value, or disqualify the corner point?
- **internal_power decode**: measurement method to establish before
  any power table is emitted (see WISHLIST: spec first).
