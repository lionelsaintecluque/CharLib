"""Setup & hold constraints by pushout and disturbance bisection.

The method calibrated by the hand-run dlhq campaign, as an alternative to
the contour method (which stays available under its own name):

* **Setup**: the reference is the data->output delay through the *open*
  gate, measured twice (coarse then refined). The gate then closes and
  the data edge is bisected toward — and past — the closing edge; a
  probe fails when the propagation is pushed out beyond
  ``criterion x t_ref``, and a failed measurement is a fail verdict too
  (sentinel discipline). The certificate reruns the last verified-PASSING
  edge and reads setup as t(gate@50%) - t(data@50%).

* **Hold**: the value captured at the closing edge must survive the data
  edge that follows it. The verdict is a disturbance statistic — min (or
  max) of the output over the window — which cannot fail to measure, so
  the sentinel problem disappears by construction. The certificate reruns
  the last verified-SAFE edge with its own witness: if the statistic
  contradicts the verdict there, the point is reported missing rather
  than believed.

Both searches straddle the closing edge, so negative constraints are
reachable — the official tables hold them, and a search that cannot go
negative would hide exactly those.

The criteria are parameters, not truths: ``setup_pushout_criterion`` and
``hold_disturbance_depth`` default to the house values ratified against
the official dlhq tables (TT, 1.2 V, 25 C). The output load is
``metastability_constraint_load``; both verdicts compare the cell
against itself into the same load, so the load largely cancels.

Scope: same as the sequential delay engine — a single data input, a
single output, a level-sensitive gate declared with the ``clock`` key
(the declared edge is the CLOSING edge).
"""

import PySpice

from charlib.characterizer.procedures import register, ProcedureFailedException
from charlib.characterizer.procedures.session import Session, fmt, pulse_alter
from charlib.characterizer.procedures.sequential.testbench import (
    single_data_gate_output, latch_circuit)
from charlib.liberty import liberty
from charlib.liberty.library import LookupTable

T0 = 0.5e-9   # start of the data reference ramp
ITERS = 14    # bisection steps: <1 ps everywhere on the official axes

@register('data_slews', 'clock_slews', 'metastability_constraint_load',
          'setup_pushout_criterion', 'hold_disturbance_depth')
def setup_hold_pushout(cell, config, settings):
    """Find setup & hold constraints by pushout / disturbance bisection"""
    for data_transition in ('01', '10'):
        yield (measure_constraint_matrix, cell, config, settings, 'setup', data_transition)
        yield (measure_constraint_matrix, cell, config, settings, 'hold', data_transition)

def measure_constraint_matrix(cell, config, settings, kind, data_transition):
    """Measure one constraint kind for one data direction over the whole
    (data slew x gate slew) matrix, in one ngspice session.

    :param cell: A Cell object to test.
    :param config: A CellTestConfig object containing cell-specific test configuration details.
    :param settings: A CharacterizationSettings object containing library-wide configuration
                     details.
    :param kind: 'setup' or 'hold'.
    :param data_transition: The data transition of interest, '01' or '10'.
    """
    data, gate, out = single_data_gate_output(cell)

    d_dir = 'rise' if data_transition == '01' else 'fall'
    closing_dir = 'fall' if gate.inversion else 'rise'

    d_slews = config.parameters['data_slews']
    g_slews = config.parameters.get('clock_slews', d_slews)
    vdd = settings.primary_power.voltage * settings.units.voltage
    vss = settings.primary_ground.voltage * settings.units.voltage
    v_transp, v_opaque = (vdd, vss) if gate.inversion else (vss, vdd)
    low = settings.logic_thresholds.low
    high = settings.logic_thresholds.high
    v50 = float(vdd) * 0.5
    load = config.parameters.get('metastability_constraint_load', 0.1) \
         * settings.units.capacitance
    criterion = config.parameters.get('setup_pushout_criterion', 1.1)
    depth = config.parameters.get('hold_disturbance_depth', 0.1)
    t_period = 2e-6

    (v_1, v_2) = (vss, vdd) if d_dir == 'rise' else (vdd, vss)
    # hold: the captured rail is v_1; a disturbance is the output leaving
    # it by depth x VDD, a statistic that always returns a value
    stat = 'max' if d_dir == 'rise' else 'min'
    threshold = depth*float(vdd) if d_dir == 'rise' else (1 - depth)*float(vdd)
    disturbed = (lambda v: v > threshold) if d_dir == 'rise' else (lambda v: v < threshold)

    # (d_slew, g_slew) -> constraint [s], Liberty sign (negative reachable)
    points = {}

    if not settings.dry_run:
        circuit = latch_circuit(cell, config, settings, data, gate, out, load, t_period)
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
            stem = f'{kind}_{d_dir}'
            with open(debug_path / f'{stem}.sp', 'w', encoding='utf-8') as file:
                file.write(str(simulation))
            log_path = debug_path / f'{stem}.commands'

        session = None
        try:
            session = Session(simulator, simulation, settings, log_path=log_path)
            session.execute(f'alter c{out} = {fmt(load)}')
            for d_slew in d_slews:
                s_d = float(d_slew * settings.units.time) / (high - low)

                # Open-gate two-pass data->output reference. The run starts
                # transparent with data at the old value, so the DC point is
                # forced by transparency. t_ref does not depend on the gate
                # slew: measured once per data slew.
                session.execute(pulse_alter(f'v{gate.name}', v_transp, v_transp,
                                            T0, s_d, 1e-6, t_period))
                session.execute(pulse_alter(f'v{data}', v_1, v_2, T0, s_d, 1e-6, t_period))
                ref_meas = (f'meas tran t_ref trig v(v{data}) val={v50} {d_dir}=1 '
                            f'targ v(v{out}) val={v50} {d_dir}=1')
                t_win = T0 + s_d + 2e-9
                while True:
                    session.execute(f'tran {fmt(max(s_d/4, 5e-12))} {fmt(t_win)}')
                    t_ref = session.measure('t_ref', ref_meas)
                    session.execute('destroy all')
                    if t_ref is not None or t_win > 1000*s_d + T0:
                        break
                    t_win = 2*t_win
                if t_ref is None:
                    continue # every point of this data slew reported missing below
                t_step = t_ref/100
                session.execute(f'tran {fmt(t_step)} {fmt(T0 + s_d + 2.5*t_ref)}')
                t_ref = session.measure('t_ref', ref_meas)
                session.execute('destroy all')
                if t_ref is None:
                    continue
                # close the gate only after the reference has provably settled
                t_cs = T0 + s_d + 2*t_ref + 0.5e-9

                for g_slew in g_slews:
                    s_c = float(g_slew * settings.units.time) / (high - low)
                    t_c50 = t_cs + s_c/2
                    session.execute(pulse_alter(f'v{gate.name}', v_transp, v_opaque,
                                                t_cs, s_c, 1e-6, t_period))
                    b_lo = 0.0 if kind == 'setup' else T0
                    b_hi = t_cs + s_c + 4*t_ref + 1e-9
                    if kind == 'setup':
                        # bisect the data edge toward (and past) the closing
                        # edge; a failed probe measurement is a fail verdict
                        b_prev, b_next, b_td = b_lo, b_hi, T0
                        for _ in range(ITERS):
                            session.execute(pulse_alter(f'v{data}', v_1, v_2,
                                                        b_td, s_d, 1e-6, t_period))
                            session.execute(f'tran {fmt(t_step)} {fmt(b_td + s_d + 3*t_ref)}')
                            m_push = session.measure('m_push',
                                f'meas tran m_push trig v(v{data}) val={v50} {d_dir}=1 '
                                f'targ v(v{out}) val={v50} {d_dir}=1')
                            session.execute('destroy all')
                            if m_push is None or m_push > criterion*t_ref:
                                b_next, b_td = b_td, (b_td + b_prev)/2
                            else:
                                b_prev, b_td = b_td, (b_td + b_next)/2
                        # certificate: rerun the last verified-PASSING edge and
                        # read the constraint 50%-to-50%, the Liberty sign
                        session.execute(pulse_alter(f'v{data}', v_1, v_2,
                                                    b_prev, s_d, 1e-6, t_period))
                        session.execute(f'tran {fmt(t_step)} {fmt(t_c50 + s_c/2 + 3*t_ref)}')
                        value = session.measure('m_setup',
                            f'meas tran m_setup trig v(v{data}) val={v50} {d_dir}=1 '
                            f'targ v(v{gate.name}) val={v50} {closing_dir}=1')
                        session.execute('destroy all')
                    else:
                        # bisect the late data edge back toward the closing edge
                        b_prev, b_next = b_lo, b_hi
                        b_td = (b_prev + b_next)/2
                        for _ in range(ITERS):
                            session.execute(pulse_alter(f'v{data}', v_1, v_2,
                                                        b_td, s_d, 1e-6, t_period))
                            session.execute(f'tran {fmt(t_step)} {fmt(b_td + s_d + 6*t_ref)}')
                            m_dist = session.measure('m_dist', f'meas tran m_dist {stat} v(v{out})')
                            session.execute('destroy all')
                            if m_dist is None or disturbed(m_dist):
                                b_prev, b_td = b_td, (b_td + b_next)/2
                            else:
                                b_next, b_td = b_td, (b_td + b_prev)/2
                        # certificate: the last verified-SAFE edge,
                        # unconditionally, with its witness — that alarm should
                        # never fire, and its silence is the proof
                        session.execute(pulse_alter(f'v{data}', v_1, v_2,
                                                    b_next, s_d, 1e-6, t_period))
                        session.execute(f'tran {fmt(t_step)} {fmt(b_next + s_d + 6*t_ref)}')
                        value = session.measure('m_hold',
                            f'meas tran m_hold trig v(v{gate.name}) val={v50} {closing_dir}=1 '
                            f'targ v(v{data}) val={v50} {d_dir}=1')
                        witness = session.measure('m_dist', f'meas tran m_dist {stat} v(v{out})')
                        session.execute('destroy all')
                        if witness is None or disturbed(witness):
                            value = None
                    if value is not None:
                        points[(d_slew, g_slew)] = value
        except ProcedureFailedException:
            raise
        except Exception as e:
            msg = f'Procedure measure_constraint_matrix failed for cell {cell.name} ' \
                  f'on {kind}/{d_dir}'
            raise ProcedureFailedException(msg) from e
        finally:
            if session:
                session.close()

        missing = {point for point in ((d, g) for d in d_slews for g in g_slews)} - set(points)
        if missing:
            raise ProcedureFailedException(
                f'Procedure measure_constraint_matrix failed for cell {cell.name} on '
                f'{kind}/{d_dir}: no verdict at points {sorted(missing)}')

    # Build the timing group on the constrained pin, mirroring the official
    # liberty structure
    result = cell.liberty
    timing_group = liberty.Group('timing')
    timing_group.add_attribute('related_pin', gate.name)
    closing_word = 'falling' if closing_dir == 'fall' else 'rising'
    timing_group.add_attribute('timing_type', f'{kind}_{closing_word}')
    lut_template = f'constraint_template_{len(d_slews)}x{len(g_slews)}'
    if points:
        lut = LookupTable(f'{d_dir}_constraint', lut_template,
                          constrained_pin_transition=list(d_slews),
                          related_pin_transition=list(g_slews))
        for (d_slew, g_slew), value in points.items():
            quantity = value @ PySpice.Unit.u_s
            lut[d_slew, g_slew] = quantity.convert(settings.units.time.prefixed_unit).value
        timing_group.add_group(lut)
    result.group('pin', data).add_group(timing_group)

    return result
