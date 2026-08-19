"""Sequential propagation delay measurement (level-sensitive cells).

One task per (arc, direction) — the transparent data->output path and the
opening-edge path of a latch — each driving a single interactive ngspice
session through the whole (slew x load) matrix by alter/tran/meas, the
circuit being parsed once per arc.

The conditioning scenario is the one validated by the hand-run dlhq
calibration campaign: the run starts TRANSPARENT with the data input at
the old value, so the DC operating point is forced by transparency and
needs no .nodeset; the value is thereby stored, data flips while the
cell is opaque, and the measured edge is the one that reopens the gate.
Every window is scheduled from that timeline, and the detection
measurements ignore the whole preamble via td=.

Each point runs two passes: a coarse tran bounded by the qualification
contract (grown on failure), in which the crossing window is detected at
10/90 % — deliberately outside the 20-80 % slew band — then a fine tran
restricted to that window for the actual 50/50 delay and 20/80
transition measurements.

Scope: a single data input, a single output, and a level-sensitive gate
declared with the ``clock`` key, whose declared edge is the CLOSING edge
(matching the official constraint tables' *_falling/*_rising naming).
Edge-triggered flops, set/reset pins and differential outputs are not
handled yet.
"""

import PySpice

from charlib.characterizer.procedures import register, ProcedureFailedException
from charlib.characterizer.procedures.session import Session, fmt, pulse_alter
from charlib.characterizer.procedures.sequential.testbench import (
    single_data_gate_output, latch_circuit, transparent_high)
from charlib.liberty import liberty
from charlib.liberty.library import LookupTable

# First-pass window margin beyond the measured edge: one period of the
# calibration campaign's 100 MHz qualification contract (grown on
# failure up to 1000x the applied ramp, the library-wide ceiling).
T_CONTRACT = 10e-9
K_PTS = 200   # second-pass resolution: fine tran step = window / K_PTS
MARG = 0.2    # second-pass margin, in fractions of the detected window

@register('data_slews', 'clock_slews', 'loads', 'transient_sim_end_time')
def sequential_worst_case(cell, config, settings):
    """Measure sequential transient and propagation delays"""
    for output_transition in ('01', '10'):
        yield (measure_arc_matrix, cell, config, settings, 'transparent', output_transition)
        yield (measure_arc_matrix, cell, config, settings, 'opening', output_transition)

def measure_arc_matrix(cell, config, settings, arc, output_transition):
    """Measure the full delay matrix of one sequential arc, in one session.

    :param cell: A Cell object to test.
    :param config: A CellTestConfig object containing cell-specific test configuration details.
    :param settings: A CharacterizationSettings object containing library-wide configuration
                     details.
    :param arc: 'transparent' (data to output through the open gate) or 'opening' (the gate
                edge propagating a value stored while opaque).
    :param output_transition: The output transition of interest, '01' or '10'.
    """
    data, gate, out = single_data_gate_output(cell)

    out_dir = 'rise' if output_transition == '01' else 'fall'
    v_transparent_high = transparent_high(gate)
    open_dir = 'rise' if v_transparent_high else 'fall'

    slews = config.parameters['data_slews'] if arc == 'transparent' \
       else config.parameters.get('clock_slews', config.parameters['data_slews'])
    loads = config.parameters['loads']
    vdd = settings.primary_power.voltage * settings.units.voltage
    vss = settings.primary_ground.voltage * settings.units.voltage
    v_transp, v_opaque = (vdd, vss) if v_transparent_high else (vss, vdd)
    low = settings.logic_thresholds.low
    high = settings.logic_thresholds.high
    v50 = float(vdd) * 0.5
    t_end_config = config.parameters.get('transient_sim_end_time', 0) * settings.units.time
    t_period = 2e-6 # both sources pulse once and never come back

    # (load, slew) -> (tpd, transition) in seconds
    points = {}

    if not settings.dry_run:
        circuit = latch_circuit(cell, config, settings, data, gate, out,
                                loads[0]*settings.units.capacitance, t_period)

        simulator = PySpice.Simulator.factory(simulator=settings.simulation.backend)
        simulation = simulator.simulation(
            circuit,
            temperature=settings.temperature,
            nominal_temperature=settings.temperature
        )
        simulation.options(trtol=1)

        log_path = None
        if settings.debug:
            debug_path = settings.debug_dir / cell.name / __name__.split('.')[-1]
            debug_path.mkdir(parents=True, exist_ok=True)
            stem = f'{arc}_{out_dir}'
            with open(debug_path / f'{stem}.sp', 'w', encoding='utf-8') as file:
                file.write(str(simulation))
            log_path = debug_path / f'{stem}.commands'

        session = None
        try:
            session = Session(simulator, simulation, settings, log_path=log_path)
            for slew in slews:
                # The lib axis is a low-to-high-threshold transition; a linear
                # PULSE ramp is 0-100 %, hence the division.
                s = float(slew * settings.units.time) / (high - low)
                t0 = 1e-9           # conditioning edge (or the transparent D edge)
                t1 = t0 + s + 1e-9  # data flips while opaque
                t2 = t1 + s + 1e-9  # the measured opening edge
                if arc == 'transparent':
                    (v_from, v_to) = (vss, vdd) if out_dir == 'rise' else (vdd, vss)
                    session.execute(pulse_alter(f'v{data}', v_from, v_to, t0, s,
                                                 1e-6, t_period))
                    session.execute(pulse_alter(f'v{gate.name}', v_transp, v_transp, t0, s,
                                                 1e-6, t_period))
                    in_sig, in_dir = data, out_dir
                    t_skip, t_edge = t0/2, t0
                else:
                    (v_old, v_new) = (vss, vdd) if out_dir == 'rise' else (vdd, vss)
                    session.execute(pulse_alter(f'v{data}', v_old, v_new, t1, s,
                                                 1e-6, t_period))
                    # the gate closes at t0 and reopens with the edge under test at t2
                    session.execute(pulse_alter(f'v{gate.name}', v_transp, v_opaque, t0, s,
                                                 t2 - t0 - s, t_period))
                    in_sig, in_dir = gate.name, open_dir
                    t_skip, t_edge = t2 - 0.5e-9, t2
                v_lo, v_hi = 0.1*float(vdd), 0.9*float(vdd)
                v_in_start = v_lo if in_dir == 'rise' else v_hi
                v_out_end = v_hi if out_dir == 'rise' else v_lo
                t_ceiling = max(1000*s, float(t_end_config))
                for load in loads:
                    session.execute(f'alter c{out} = {fmt(load*settings.units.capacitance)}')
                    # Coarse pass: detect the crossing window at 10/90 %,
                    # ignoring the whole preamble, growing on failure.
                    margin = max(float(t_end_config), T_CONTRACT)
                    while True:
                        t_end = t_edge + s + margin
                        session.execute(f'tran {fmt(t_end/200)} {fmt(t_end)}')
                        t_left = session.measure('l_wind',
                            f'meas tran l_wind when v(v{in_sig})={v_in_start} '
                            f'{in_dir}=1 td={fmt(t_skip)}')
                        t_right = session.measure('r_wind',
                            f'meas tran r_wind when v(v{out})={v_out_end} '
                            f'{out_dir}=1 td={fmt(t_skip)}')
                        session.execute('destroy all')
                        if (t_left is not None and t_right is not None and t_right > t_left) \
                           or margin >= t_ceiling:
                            break
                        margin = min(2*margin, t_ceiling)
                    if t_left is None or t_right is None or t_right <= t_left:
                        continue # reported as a missing point below
                    # Fine pass: the detected window plus margins, delay read
                    # 50 %-to-50 %, transition between the slew thresholds.
                    window = t_right - t_left
                    t_from = max(t_left - MARG*window, 0)
                    t_to = t_right + MARG*window
                    session.execute(f'tran {fmt(window/K_PTS)} {fmt(t_to)} {fmt(t_from)}')
                    tpd = session.measure('m_tpd',
                        f'meas tran m_tpd trig v(v{in_sig}) val={v50} {in_dir}=1 '
                        f'targ v(v{out}) val={v50} {out_dir}=1')
                    (v_a, v_b) = (low, high) if out_dir == 'rise' else (high, low)
                    transition = session.measure('m_slew',
                        f'meas tran m_slew trig v(v{out}) val={v_a*float(vdd)} {out_dir}=1 '
                        f'targ v(v{out}) val={v_b*float(vdd)} {out_dir}=1')
                    session.execute('destroy all')
                    if tpd is not None and transition is not None:
                        points[(load, slew)] = (tpd, transition)
        except ProcedureFailedException:
            raise
        except Exception as e:
            msg = f'Procedure measure_arc_matrix failed for cell {cell.name} ' \
                  f'on arc {arc}/{out_dir}'
            raise ProcedureFailedException(msg) from e
        finally:
            if session:
                session.close()

        missing = {point for point in ((l, s) for l in loads for s in slews)} - set(points)
        if missing:
            raise ProcedureFailedException(
                f'Procedure measure_arc_matrix failed for cell {cell.name} on arc '
                f'{arc}/{out_dir}: no measurement at points {sorted(missing)}')

    # Build the timing group, mirroring the official liberty structure
    result = cell.liberty
    timing_group = liberty.Group('timing')
    if arc == 'transparent':
        timing_group.add_attribute('related_pin', data)
        timing_group.add_attribute('timing_sense', 'positive_unate')
        timing_group.add_attribute('timing_type', 'combinational')
    else:
        timing_group.add_attribute('related_pin', gate.name)
        timing_group.add_attribute('timing_sense', 'non_unate')
        timing_group.add_attribute('timing_type', f'{"rising" if open_dir == "rise" else "falling"}_edge')
    lut_template = f'delay_template_{len(loads)}x{len(slews)}'
    if points:
        for lut_name, index in ((f'cell_{out_dir}', 0), (f'{out_dir}_transition', 1)):
            lut = LookupTable(lut_name, lut_template,
                              total_output_net_capacitance=list(loads),
                              input_net_transition=list(slews))
            for (load, slew), values in points.items():
                value = values[index] @ PySpice.Unit.u_s
                lut[load, slew] = value.convert(settings.units.time.prefixed_unit).value
            timing_group.add_group(lut)
    result.group('pin', out).add_group(timing_group)

    return result
