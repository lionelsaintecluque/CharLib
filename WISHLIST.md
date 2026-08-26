# Wishlist

Ordered roughly by leverage. Items graduate to DECISIONS when a
choice is made, and to code when it is ratified.

## Measurement coverage

- **internal_power**: the single blocker between PARTIAL and FULL on
  every functional cell of the PDK sweep. Rule: the DB key grammar
  for power/leakage (including `when` states) gets specified in the
  interchange format FIRST; no writer emits power before that.
- **Conditional (`when`) timing arcs**: vendor libs carry per-state
  arcs on complex gates (a21o: 12/46 tables covered today).
- **Latch + reset cells** (dlhr/dlhrq/dllr/dllrq): extend the latch
  procedures with async conditioning; recovery/removal latch-variant.
- **Multi-input latches** (SR taps): generalize the single-data-pin
  scope; add the hold cliff-width probe.
- **Tristate outputs** (einvn/ebufn families, 6 cells): Hi-Z arcs,
  three_state timing, max_capacitance semantics.
- **Clock-gating cells** (lgcp/slgcp): representable timing model to
  define.
- **Scan flip-flops** (multi-data): mux-D conditioning.
- **Falling-edge clocks**: lift the rising-edge-only restriction of
  the FF procedures.
- **min_pulse_width on level set/reset pins**: extend the mpw
  generator beyond edge-declared pins.

## Infrastructure

- **Measurement DB import/export** (decided, see DECISIONS): per-cell
  DB files as drop-in replacement for procedures; incremental dump as
  crash checkpoint; standalone db -> liberty command. Enables split
  workflows: another tool measures some cells (or the most demanding
  arcs), CharLib assembles and writes the library.
- **Resume**: on restart, skip measurements whose keys already exist
  in the dumped DB.
- **Warn on dropped/unused yml keys**: the silent whitelist filter
  (see KNOWN-BUGS) should log what it rejects, and the run should
  report declared-but-never-read keys.
- **Conditioning correct by construction**: anchor service edges by
  their completion time (settled at latest at clock edge minus margin,
  start deduced from the ramp), and check the precondition up front —
  if even the fastest axis slew does not fit between reset release and
  the clock edge, raise an explicit bench-configuration error, never a
  cell disqualification.
- **Inter-measurement sequencing**: measure mpw(reset) before the
  procedures that condition with it — the last banner falls when the
  reset pulse pair is read from measurements instead of assumed.
- **Upstream cherry-picks to evaluate**: threshold percentages
  (66bbfca), bare function expressions (61496d8), max_capacitance
  (4c3bcb3).

## Calibration debt

- The anomalies in KNOWN-BUGS (clamped corner, 1 fF transitions,
  stacked leakage, buf/nor2b input caps, non-unate worst case) each
  deserve a calibration campaign of their own.
