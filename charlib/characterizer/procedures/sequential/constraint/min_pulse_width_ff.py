"""Minimum pulse widths of an edge-triggered flip-flop: three families.

Port of the dfrbp hand campaign (4_MEASURE_C / 4_MEASURE_R and their
notebook stanzas). (1) CLK high width: the probe pulse judged by its own
capture arc, BOTH captured values measured — a wide conditioning pulse
plus a data flip during the opaque phase gives the capture-of-0 — and
the worst published. (2) CLK low width: the LOW gap between two pulses
shrunk with the INVERTED slope-preserving probe; data flips while CLK is
high, so the gap tests only the master's acquisition. (3) Async low
width: assert against a conditioned captured state, arc = assertion edge
to output.

Everywhere: composite verdict (pushout <= criterion x t_ref AND output
at the new rail at the deadline), conditioning witness before the
reference is trusted, realized 50-50 width measured on the probe node
itself with tstart blinding, and the domain floor of the fork's latch
procedure — probes never leave the 90 %-swing validity domain
(4/3 x slew), the floor is probed first with the full verdict and that
run doubles as the certificate. Saturation at the contract-wide end is
a failure, not a value.
"""

import PySpice

from charlib.characterizer import utils
from charlib.characterizer.cell import Port
from charlib.characterizer.procedures import register, ProcedureFailedException
from charlib.characterizer.procedures.session import Session, fmt, pulse_alter
from charlib.characterizer.procedures.sequential.testbench import flop_pins
from charlib.liberty import liberty
from charlib.liberty.library import LookupTable

ITERS = 14
T_PERIOD = 2e-6

# ============================== CONSTANTES NUES =============================
# Chronologie en fractions du contrat (c_pw = demi-periode active) :
#   0.1 / 0.2   liberation du reset de conditionnement (rampe entre les deux)
#   0.6         position de la sonde haute ou du pulse de conditionnement
#   0.25        bascule de D pendant la phase opaque
#   1.0         ecartement des fronts fixes (pulse de conditionnement, gap)
#   0.5         ouverture de la fenetre sauvee (aveuglement) ; marge du banc
#   0.4 / 0.2   banc reset : position du pulse d'horloge, retrait du blind
#   2 ps        garde de saturation au bout contractuel de la bissection
# Fractions de la campagne dfrbp, memes statuts que les autres bannieres.
# ============================================================================
RST_RELEASE_START = 0.1
RST_RELEASE_END = 0.2
PROBE_AT = 0.6
D_FLIP_OFFSET = 0.25
FIXED_GAP = 1.0
BLIND_OFFSET = 0.5
RST_CLK_AT = 0.4
RST_BLIND_BACK = 0.2
SATURATION_GUARD = 2e-12
COARSE_STEP_FLOOR = 10e-12


@register('data_slews', 'clock_slews', 'metastability_constraint_load',
          'min_pulse_width_pushout_criterion',
          'qualification_frequency', 'qualification_duty_cycle')
def min_pulse_width_constraint_ff(cell, config, settings):
    """Find the flip-flop minimum pulse widths: clock high, clock low, async low"""
    yield (measure_mpw_ff, cell, config, settings, 'clk_high')
    yield (measure_mpw_ff, cell, config, settings, 'clk_low')
    if cell.clear or cell.preset:
        yield (measure_mpw_ff, cell, config, settings, 'async')


def _pwl_alter(source, points):
    flat = ' '.join(f'{fmt(t)} {fmt(v)}' for (t, v) in points)
    return f'alter @{source}[pwl] = [ {flat} ]'


def _high_probe(t_start, ramp, vdd, width):
    """HIGH probe of exact 50-50 width; slope preserved, peak >= 90 % swing."""
    if width < 0.8 * ramp:
        raise ValueError(f'probe peak below 90% of the swing: {width} < {0.8*ramp}')
    shrink = min((1 + width/ramp) / 2, 1)
    v_peak = vdd * shrink
    t_rise = ramp * shrink
    plateau = max(width - ramp, 1e-15)
    return [(t_start, 0), (t_start + t_rise, v_peak),
            (t_start + t_rise + plateau, v_peak),
            (t_start + 2*t_rise + plateau, 0)]


def _low_probe(t_start, ramp, vdd, width):
    """LOW probe (inverted): valley <= 10 % swing, slope preserved."""
    if width < 0.8 * ramp:
        raise ValueError(f'probe valley above 10% of the swing: {width} < {0.8*ramp}')
    shrink = min((1 + width/ramp) / 2, 1)
    v_valley = vdd * (1 - shrink)
    t_fall = ramp * shrink
    plateau = max(width - ramp, 1e-15)
    return [(t_start, vdd), (t_start + t_fall, v_valley),
            (t_start + t_fall + plateau, v_valley),
            (t_start + 2*t_fall + plateau, vdd)]


def measure_mpw_ff(cell, config, settings, family):
    """Measure one mpw family over its slew axis, in one ngspice session."""
    data, clock, reset, out = flop_pins(cell)
    if reset is None:
        raise ProcedureFailedException(
            f'Cell {cell.name}: conditioning needs a set or reset pin')
    outs = cell.outputs

    slews = config.parameters['data_slews'] if family == 'async' \
       else config.parameters.get('clock_slews', config.parameters['data_slews'])
    vdd = settings.primary_power.voltage * settings.units.voltage
    vss = settings.primary_ground.voltage * settings.units.voltage
    vdd_f, vss_f = float(vdd), float(vss)
    high = settings.logic_thresholds.high
    low = settings.logic_thresholds.low
    v50 = vdd_f * 0.5
    v_low_rail = 0.1 * vdd_f
    v_high_rail = 0.9 * vdd_f
    load = config.parameters['metastability_constraint_load'] * settings.units.capacitance
    criterion = config.parameters['min_pulse_width_pushout_criterion']
    c_per = 1 / config.parameters['qualification_frequency']
    c_pw = config.parameters['qualification_duty_cycle'] * c_per
    d_fast = float(min(config.parameters['data_slews']) * settings.units.time) / (high - low)
    v_rst_active = vss_f if reset.inversion else vdd_f
    v_rst_off = vdd_f if reset.inversion else vss_f
    w_ref = c_pw
    w_floor_of = lambda ramp: 0.8 * ramp

    probe_pin = reset.name if family == 'async' else clock.name
    probe_release = ('rise' if reset.inversion else 'fall') if family == 'async' else None
    polarities = ('one',) if family == 'async' else ('one', 'zero')

    # slew -> worst measured width over the family's polarities [s]
    points = {}

    if not settings.dry_run:
        circuit = utils.init_circuit('ff_mpw', cell.netlist, config.models,
                                     settings.named_nodes, settings.units)
        n_ck = {'clk_high': 9, 'clk_low': 7, 'async': 5}[family]
        n_rst = 7 if family == 'async' else 3
        connections = []
        for pin in cell.pins_in_netlist_order():
            match pin.role:
                case Port.Role.POWER:
                    connections.append(settings.primary_power.name)
                case Port.Role.GROUND:
                    connections.append(settings.primary_ground.name)
                case Port.Role.NWELL:
                    connections.append(settings.nwell.name)
                case Port.Role.PWELL:
                    connections.append(settings.pwell.name)
                case _:
                    connections.append(f'v{pin.name}')
                    if pin.name == data:
                        circuit.PieceWiseLinearVoltageSource(pin.name, f'v{pin.name}',
                            circuit.gnd, values=[(k*1e-9, vss) for k in range(3)])
                    elif pin.name == clock.name:
                        circuit.PieceWiseLinearVoltageSource(pin.name, f'v{pin.name}',
                            circuit.gnd, values=[(k*1e-9, vss) for k in range(n_ck)])
                    elif pin.name == reset.name:
                        circuit.PieceWiseLinearVoltageSource(pin.name, f'v{pin.name}',
                            circuit.gnd,
                            values=[(k*1e-9, v_rst_active) for k in range(n_rst)])
                    elif pin.name == out:
                        circuit.C(pin.name, f'v{pin.name}', circuit.gnd, load)
        circuit.X('dut', cell.name, *connections)

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
            with open(debug_path / f'{family}.sp', 'w', encoding='utf-8') as file:
                file.write(str(simulation))
            log_path = debug_path / f'{family}.commands'

        session = None
        try:
            session = Session(simulator, simulation, settings, log_path=log_path)
            for slew in slews:
                ramp = float(slew * settings.units.time) / (high - low)
                w_floor = w_floor_of(ramp)
                widths = []
                for polarity in polarities:
                    one = polarity == 'one'

                    if family == 'clk_high':
                        session.execute(_pwl_alter(f'v{reset.name}',
                            [(0, v_rst_active), (RST_RELEASE_START*c_pw, v_rst_active),
                             (RST_RELEASE_END*c_pw, v_rst_off)]))
                        if one:
                            t_m = PROBE_AT*c_pw
                            t_blind = None
                            pre = [(0, 0), (t_m/5, 0), (2*t_m/5, 0), (3*t_m/5, 0),
                                   (4*t_m/5, 0)]
                            session.execute(_pwl_alter(f'v{data}',
                                [(0, vdd_f), (1e-9, vdd_f), (2e-9, vdd_f)]))
                        else:
                            t_c1 = PROBE_AT*c_pw
                            t_df = t_c1 + ramp + D_FLIP_OFFSET*c_pw
                            t_f1 = t_c1 + ramp + FIXED_GAP*c_pw
                            t_m = t_f1 + ramp + FIXED_GAP*c_pw
                            t_blind = t_f1 + ramp + BLIND_OFFSET*c_pw
                            pre = [(0, 0), (t_c1, 0), (t_c1 + ramp, vdd_f),
                                   (t_f1, vdd_f), (t_f1 + ramp, 0)]
                            session.execute(_pwl_alter(f'v{data}',
                                [(0, vdd_f), (t_df, vdd_f), (t_df + d_fast, 0)]))
                        probe = lambda w: _pwl_alter(f'v{clock.name}',
                            pre + _high_probe(t_m, ramp, vdd_f, w))
                        arc50 = t_m + ramp/2
                        q_dir = 'rise' if one else 'fall'
                        trig = f'trig v(v{clock.name}) val={v50} rise=1'
                        width_meas = (f'meas tran m_mpw trig v(v{clock.name}) val={v50} '
                                      f'rise=1 targ v(v{clock.name}) val={v50} fall=1')
                    elif family == 'clk_low':
                        session.execute(_pwl_alter(f'v{reset.name}',
                            [(0, v_rst_active), (RST_RELEASE_START*c_pw, v_rst_active),
                             (RST_RELEASE_END*c_pw, v_rst_off)]))
                        t_c1 = PROBE_AT*c_pw
                        t_dx = t_c1 + ramp + D_FLIP_OFFSET*c_pw
                        f1 = t_c1 + ramp + FIXED_GAP*c_pw
                        t_m = f1
                        t_blind = t_c1 + ramp + BLIND_OFFSET*c_pw
                        (v_a, v_b) = (vss_f, vdd_f) if one else (vdd_f, vss_f)
                        session.execute(_pwl_alter(f'v{data}',
                            [(0, v_a), (t_dx, v_a), (t_dx + d_fast, v_b)]))
                        head = [(0, 0), (t_c1, 0), (t_c1 + ramp, vdd_f)]
                        probe = lambda w: _pwl_alter(f'v{clock.name}',
                            head + _low_probe(f1, ramp, vdd_f, w))
                        arc50 = f1 + ramp/2
                        q_dir = 'rise' if one else 'fall'
                        trig = f'trig v(v{clock.name}) val={v50} rise=1'
                        width_meas = (f'meas tran m_mpw trig v(v{clock.name}) val={v50} '
                                      f'fall=1 targ v(v{clock.name}) val={v50} rise=1')
                    else:
                        t_c1 = RST_CLK_AT*c_pw
                        t_a = t_c1 + d_fast + c_per + d_fast + BLIND_OFFSET*c_pw
                        t_m = t_a
                        t_blind = t_a - RST_BLIND_BACK*c_pw
                        session.execute(_pwl_alter(f'v{clock.name}',
                            [(0, vss_f), (t_c1, vss_f), (t_c1 + d_fast, vdd_f),
                             (t_c1 + d_fast + c_per, vdd_f),
                             (t_c1 + 2*d_fast + c_per, vss_f)]))
                        session.execute(_pwl_alter(f'v{data}',
                            [(0, vdd_f), (1e-9, vdd_f), (2e-9, vdd_f)]))
                        head = [(0, v_rst_active), (RST_RELEASE_START*c_pw, v_rst_active),
                                (RST_RELEASE_END*c_pw, v_rst_off), (t_a, v_rst_off)]
                        invert = (lambda pts: pts) if reset.inversion else \
                            (lambda pts: [(t, vdd_f - v) for (t, v) in pts])
                        probe = lambda w: _pwl_alter(f'v{reset.name}',
                            head + invert(_low_probe(t_a, ramp, vdd_f, w))[1:])
                        arc50 = t_a + ramp/2
                        q_dir = 'fall' if reset.role == Port.Role.CLEAR else 'rise'
                        assert_dir = 'fall' if reset.inversion else 'rise'
                        trig = f'trig v(v{reset.name}) val={v50} {assert_dir}=1'
                        release = 'rise' if reset.inversion else 'fall'
                        width_meas = (f'meas tran m_mpw trig v(v{reset.name}) val={v50} '
                                      f'{assert_dir}=1 targ v(v{reset.name}) val={v50} '
                                      f'{release}=1')

                    still_old = (lambda v: v is not None and v < v_low_rail) \
                        if q_dir == 'rise' else (lambda v: v is not None and v > v_high_rail)
                    captured = (lambda v: v is not None and v > v_high_rail) \
                        if q_dir == 'rise' else (lambda v: v is not None and v < v_low_rail)
                    m_ref = (f'meas tran t_ref {trig} '
                             f'targ v(v{out}) val={v50} {q_dir}=1')
                    blind_arg = f' {fmt(t_blind)}' if t_blind else ''

                    session.execute(probe(w_ref))
                    t_step = max(ramp/4, COARSE_STEP_FLOOR)
                    t_win = arc50 + w_ref + BLIND_OFFSET*c_pw
                    session.execute(f'tran {fmt(t_step)} {fmt(t_win)}{blind_arg}')
                    t_ref = session.measure('t_ref', m_ref)
                    v_cond = session.measure('v_cond',
                        f'meas tran v_cond find v(v{out}) at={fmt(t_m)}')
                    session.execute('destroy all')
                    if t_ref is None or not still_old(v_cond):
                        continue
                    t_step = t_ref/100
                    session.execute(f'tran {fmt(t_step)} {fmt(t_win)}{blind_arg}')
                    t_ref = session.measure('t_ref', m_ref)
                    session.execute('destroy all')
                    if t_ref is None:
                        continue

                    def probe_verdict(width):
                        session.execute(probe(width))
                        t_dl = arc50 + width + 4*t_ref
                        session.execute(f'tran {fmt(t_step)} {fmt(t_dl + t_ref)}{blind_arg}')
                        m_push = session.measure('m_push',
                            m_ref.replace('t_ref', 'm_push'))
                        v_end = session.measure('v_end',
                            f'meas tran v_end find v(v{out}) at={fmt(t_dl)}')
                        return m_push is not None and m_push <= criterion*t_ref \
                           and captured(v_end)

                    def measure_width():
                        return session.measure('m_mpw', width_meas)

                    if probe_verdict(w_floor):
                        measured = measure_width()
                        session.execute('destroy all')
                    else:
                        session.execute('destroy all')
                        w_fail, w_pass = w_floor, w_ref
                        width = (w_fail + w_pass)/2
                        for _ in range(ITERS):
                            passing = probe_verdict(width)
                            session.execute('destroy all')
                            if passing:
                                w_pass = width
                                width = (width + w_fail)/2
                            else:
                                w_fail = width
                                width = (width + w_pass)/2
                        if w_pass > w_ref - SATURATION_GUARD:
                            continue
                        if not probe_verdict(w_pass):
                            session.execute('destroy all')
                            continue
                        measured = measure_width()
                        session.execute('destroy all')
                    if measured is not None:
                        widths.append(measured)

                if len(widths) == len(polarities):
                    points[slew] = max(widths)
        except ProcedureFailedException:
            raise
        except Exception as e:
            msg = f'Procedure measure_mpw_ff failed for cell {cell.name} ' \
                  f'on family {family}'
            raise ProcedureFailedException(msg) from e
        finally:
            if session:
                session.close()

        missing = set(slews) - set(points)
        if missing:
            raise ProcedureFailedException(
                f'Procedure measure_mpw_ff failed for cell {cell.name} on family '
                f'{family}: no verdict at slews {sorted(missing)}')

    result = cell.liberty
    timing_group = liberty.Group('timing')
    timing_group.add_attribute('related_pin', probe_pin)
    timing_group.add_attribute('timing_type', 'min_pulse_width')
    if points:
        table = {'clk_high': 'rise_constraint', 'clk_low': 'fall_constraint'}.get(
            family, f'{"fall" if reset.inversion else "rise"}_constraint')
        lut = LookupTable(table, f'mpw_template_{len(slews)}',
                          constrained_pin_transition=list(slews))
        for slew, value in points.items():
            quantity = value @ PySpice.Unit.u_s
            lut[slew,] = quantity.convert(settings.units.time.prefixed_unit).value
        timing_group.add_group(lut)
    result.group('pin', probe_pin).add_group(timing_group)

    return result
