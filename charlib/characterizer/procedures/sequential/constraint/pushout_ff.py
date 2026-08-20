"""Setup & hold constraints of an edge-triggered flip-flop, by pushout and
disturbance bisection against the active clock edge.

Port of the dfrbp hand campaign: conditioning by a reset pulse (re-armed
at t=0 of every transient), a two-cycle contract-timed sequence for the
directions that need a captured 1 first, counters blinded by the saved
window start where a prior clock edge exists, setup verdict by clk-to-Q
pushout, hold verdict by a bounded disturbance statistic.

EXPERIMENTAL: assumes the reset is already characterized (its pulse width
below is a bare constant, not a measured value) — see the banners.
Rising-edge clocks only; the measured output is the first declared one.
"""

import PySpice

from charlib.characterizer import utils
from charlib.characterizer.cell import Port
from charlib.characterizer.procedures import register, ProcedureFailedException
from charlib.characterizer.procedures.session import Session, fmt, pulse_alter
from charlib.liberty import liberty
from charlib.liberty.library import LookupTable

ITERS = 14

# ============================== CONSTANTES NUES =============================
# Impulsion de conditionnement du RESET, en secondes.
# CE CODE SUPPOSE LA CARACTERISATION DU RESET DEJA OPEREE : 100 ps de
# largeur et 400 ps de rampe sont les valeurs de la campagne dfrbp, pas des
# valeurs mesurees. Le barreau mpw(reset) de l'echelle de confiance est
# presume, pas verifie — a remplacer quand le sequencement inter-mesures
# permettra d'injecter ici le mpw mesure du pin reset.
# ============================================================================
R_MPW = 0.1e-9
R_RAMP = 0.4e-9

# ============================== CONSTANTES NUES =============================
# Chronologie de la sequence, en fractions de la periode du contrat :
#   0.25  front D de conditionnement (cycle 1)
#   1.25  descente de D au contrat (sequences deux-cycles)
#   1.2   ouverture de la fenetre sauvee (aveugle le front 1 pour les meas)
# Fractions choisies par la campagne dfrbp, pas derivees d'une mesure.
# ============================================================================
D_COND_FRACTION = 0.25
D_DROP_FRACTION = 1.25
T_BLIND_FRACTION = 1.2

# ============================== CONSTANTES NUES =============================
# Bornes de bissection et fenetres grossieres, en fractions de la demi-
# periode active (C_PW), plus le plancher du pas grossier (10 ps).
# Valeurs de la campagne dfrbp : elles encadrent le front actif assez
# largement pour atteindre les contraintes negatives officielles, sans
# justification plus profonde que la calibration qui les a validees.
# ============================================================================
COARSE_STEP_FLOOR = 10e-12
SETUP_LO_AFTER_RESET = 0.04   # borne basse (cycle 1) : juste apres le reset
SETUP_LO_AFTER_BLIND = 0.2    # borne basse (deux-cycles) : apres la fenetre
SETUP_HI = 0.3                # borne haute au-dela du front actif
SETUP_REF_WIN = 0.4           # fenetre grossiere de la reference setup
HOLD_LO = 0.2                 # borne basse avant le front actif
HOLD_HI = 0.5                 # borne haute apres le front actif
HOLD_REF_WIN = 0.8            # fenetre grossiere de la reference hold


@register('data_slews', 'clock_slews', 'metastability_constraint_load',
          'setup_pushout_criterion', 'hold_disturbance_depth',
          'qualification_frequency', 'qualification_duty_cycle')
def setup_hold_pushout_ff(cell, config, settings):
    """Find flip-flop setup & hold constraints by pushout / disturbance bisection"""
    for data_transition in ('01', '10'):
        yield (measure_ff_constraint_matrix, cell, config, settings, 'setup', data_transition)
        yield (measure_ff_constraint_matrix, cell, config, settings, 'hold', data_transition)


def _flop_pins(cell):
    clock = cell.clock
    if clock is None or clock.trigger != Port.Trigger.EDGE:
        raise ProcedureFailedException(
            f'Cell {cell.name}: setup_hold_pushout_ff needs an edge-triggered clock')
    if clock.inversion:
        raise ProcedureFailedException(
            f'Cell {cell.name}: falling-edge clocks are not supported yet')
    reset = cell.clear or cell.preset
    if reset is None:
        raise ProcedureFailedException(
            f'Cell {cell.name}: conditioning needs a set or reset pin')
    if len(cell.inputs) != 1 or not cell.outputs:
        raise ProcedureFailedException(
            f'Cell {cell.name}: only single-data-input cells are supported '
            f'(found inputs {cell.inputs}, outputs {cell.outputs})')
    return cell.inputs[0], clock, reset, cell.outputs[0]


def _pwl_alter(source, points):
    flat = ' '.join(f'{fmt(t)} {fmt(v)}' for (t, v) in points)
    return f'alter @{source}[pwl] = [ {flat} ]'


def measure_ff_constraint_matrix(cell, config, settings, kind, data_transition):
    """Measure one constraint kind for one data direction over the whole
    (data slew x clock slew) matrix, in one ngspice session."""
    data, clock, reset, out = _flop_pins(cell)

    d_dir = 'rise' if data_transition == '01' else 'fall'
    two_cycle = (kind == 'setup' and d_dir == 'fall') or \
                (kind == 'hold' and d_dir == 'rise')

    d_slews = config.parameters['data_slews']
    c_slews = config.parameters.get('clock_slews', d_slews)
    vdd = settings.primary_power.voltage * settings.units.voltage
    vss = settings.primary_ground.voltage * settings.units.voltage
    high = settings.logic_thresholds.high
    low = settings.logic_thresholds.low
    v50 = float(vdd) * 0.5
    load = config.parameters['metastability_constraint_load'] * settings.units.capacitance
    criterion = config.parameters['setup_pushout_criterion']
    depth = config.parameters['hold_disturbance_depth']
    c_per = 1 / config.parameters['qualification_frequency']
    c_pw = config.parameters['qualification_duty_cycle'] * c_per
    d_cond = D_COND_FRACTION * c_per
    d_drop = D_DROP_FRACTION * c_per
    t_blind = T_BLIND_FRACTION * c_per if two_cycle else 0

    v_rst_active = vss if reset.inversion else vdd
    v_rst_off = vdd if reset.inversion else vss

    q_edge = 'fall' if two_cycle else 'rise'
    if kind == 'setup':
        d_edge = 'fall' if two_cycle else 'rise'
    else:
        d_edge = 'rise' if two_cycle else 'fall'
    stat = 'max' if (kind == 'hold' and two_cycle) else 'min'
    threshold = depth*float(vdd) if stat == 'max' else (1 - depth)*float(vdd)
    disturbed = (lambda v: v > threshold) if stat == 'max' else (lambda v: v < threshold)

    def d_points(s_d, td, t1):
        if kind == 'hold' and two_cycle:
            return [(0, vss), (d_cond, vss), (d_cond + s_d, vdd),
                    (d_drop, vdd), (d_drop + s_d, vss), (td, vss), (t1, vdd)]
        if two_cycle or kind == 'hold':
            return [(0, vss), (d_cond, vss), (d_cond + s_d, vdd), (td, vdd), (t1, vss)]
        return [(0, vss), (td, vss), (t1, vdd)]

    points = {}

    if not settings.dry_run:
        circuit = utils.init_circuit('ff_constraint', cell.netlist, config.models,
                                     settings.named_nodes, settings.units)
        n_pwl = len(d_points(1e-9, 2e-9, 3e-9))
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
                            circuit.gnd, values=[(k*1e-9, vss) for k in range(n_pwl)])
                    elif pin.name == clock.name:
                        circuit.PulseVoltageSource(pin.name, f'v{pin.name}', circuit.gnd,
                                                   initial_value=vss, pulsed_value=vss,
                                                   pulse_width=1e-6, period=2e-6)
                    elif pin.name == reset.name:
                        circuit.PieceWiseLinearVoltageSource(pin.name, f'v{pin.name}',
                            circuit.gnd, values=[(0, v_rst_active), (R_MPW, v_rst_active),
                                                 (R_MPW + R_RAMP, v_rst_off)])
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
            stem = f'{kind}_{d_dir}'
            with open(debug_path / f'{stem}.sp', 'w', encoding='utf-8') as file:
                file.write(str(simulation))
            log_path = debug_path / f'{stem}.commands'

        session = None
        try:
            session = Session(simulator, simulation, settings, log_path=log_path)
            blind_arg = f' {fmt(t_blind)}' if two_cycle else ''
            for c_slew in c_slews:
                s_c = float(c_slew * settings.units.time) / (high - low)
                edge_t = (c_per if two_cycle else 0) + c_pw + s_c/2
                session.execute(pulse_alter(f'v{clock.name}', vss, vdd,
                                            c_pw, s_c, c_pw, c_per))
                m_ref = (f'meas tran t_ref trig v(v{clock.name}) val={v50} rise=1 '
                         f'targ v(v{out}) val={v50} {q_edge}=1')
                for d_slew in d_slews:
                    s_d = float(d_slew * settings.units.time) / (high - low)

                    ref_win = SETUP_REF_WIN if kind == 'setup' else HOLD_REF_WIN
                    ref_td = (d_drop if two_cycle else d_cond) if kind == 'setup' \
                        else edge_t + s_c/2 + 0.6*c_pw
                    session.execute(_pwl_alter(f'v{data}',
                                    d_points(s_d, ref_td, ref_td + s_d)))
                    t_step = max(s_c/4, COARSE_STEP_FLOOR)
                    t_win = edge_t + s_c/2 + ref_win*c_pw
                    session.execute(f'tran {fmt(t_step)} {fmt(t_win)}{blind_arg}')
                    t_ref = session.measure('t_ref', m_ref)
                    session.execute('destroy all')
                    if t_ref is None:
                        continue
                    t_step = t_ref/100
                    session.execute(f'tran {fmt(t_step)} {fmt(t_win)}{blind_arg}')
                    t_ref = session.measure('t_ref', m_ref)
                    session.execute('destroy all')
                    if t_ref is None:
                        continue

                    if kind == 'setup':
                        b_lo = t_blind + SETUP_LO_AFTER_BLIND*c_pw if two_cycle \
                          else R_MPW + R_RAMP + SETUP_LO_AFTER_RESET*c_pw
                        b_hi = edge_t + s_c/2 + SETUP_HI*c_pw
                        t_win = edge_t + s_c/2 + 3*t_ref
                        b_fail, b_pass, b_td = b_hi, b_lo, (b_lo + b_hi)/2
                        for _ in range(ITERS):
                            session.execute(_pwl_alter(f'v{data}',
                                            d_points(s_d, b_td, b_td + s_d)))
                            session.execute(f'tran {fmt(t_step)} {fmt(t_win)}{blind_arg}')
                            m_push = session.measure('m_push',
                                m_ref.replace('t_ref', 'm_push'))
                            session.execute('destroy all')
                            if m_push is None or m_push > criterion*t_ref:
                                b_fail = b_td
                                b_td = (b_td + b_pass)/2
                            else:
                                b_pass = b_td
                                b_td = (b_td + b_fail)/2
                        session.execute(_pwl_alter(f'v{data}',
                                        d_points(s_d, b_pass, b_pass + s_d)))
                        session.execute(f'tran {fmt(t_step)} {fmt(t_win)}{blind_arg}')
                        value = session.measure('m_setup',
                            f'meas tran m_setup trig v(v{data}) val={v50} {d_edge}=1 '
                            f'targ v(v{clock.name}) val={v50} rise=1')
                        session.execute('destroy all')
                    else:
                        # hold verdict = pushout on the capture arc, the same
                        # criterion as setup (decoded on dfrbp 2026-08-20: the
                        # survival wall sits a flat 10-20 ps beyond the official
                        # values); the disturbance statistic stays as the
                        # certificate witness — it alone sees late recovery bumps
                        t_from = edge_t + s_c/2 + 2*t_ref
                        t_dl = edge_t + s_c/2 + 5*t_ref
                        t_win = edge_t + s_c/2 + 6*t_ref
                        b_lo = edge_t - s_c/2 - HOLD_LO*c_pw
                        b_hi = edge_t + s_c/2 + HOLD_HI*c_pw
                        b_prev, b_next, b_td = b_lo, b_hi, (b_lo + b_hi)/2
                        m_dist_cmd = (f'meas tran m_dist {stat} v(v{out}) '
                                      f'from={fmt(t_from)} to={fmt(t_dl)}')
                        for _ in range(ITERS):
                            session.execute(_pwl_alter(f'v{data}',
                                            d_points(s_d, b_td, b_td + s_d)))
                            session.execute(f'tran {fmt(t_step)} {fmt(t_win)}{blind_arg}')
                            m_push = session.measure('m_push',
                                m_ref.replace('t_ref', 'm_push'))
                            session.execute('destroy all')
                            if m_push is None or m_push > criterion*t_ref:
                                b_prev = b_td
                                b_td = (b_td + b_next)/2
                            else:
                                b_next = b_td
                                b_td = (b_td + b_prev)/2
                        session.execute(_pwl_alter(f'v{data}',
                                        d_points(s_d, b_next, b_next + s_d)))
                        session.execute(f'tran {fmt(t_step)} {fmt(t_win)}{blind_arg}')
                        value = session.measure('m_hold',
                            f'meas tran m_hold trig v(v{clock.name}) val={v50} rise=1 '
                            f'targ v(v{data}) val={v50} {d_edge}=1')
                        m_push = session.measure('m_push',
                            m_ref.replace('t_ref', 'm_push'))
                        witness = session.measure('m_dist', m_dist_cmd)
                        session.execute('destroy all')
                        if m_push is None or m_push > criterion*t_ref \
                           or witness is None or disturbed(witness):
                            value = None
                    if value is not None:
                        points[(d_slew, c_slew)] = value
        except ProcedureFailedException:
            raise
        except Exception as e:
            msg = f'Procedure measure_ff_constraint_matrix failed for cell ' \
                  f'{cell.name} on {kind}/{d_dir}'
            raise ProcedureFailedException(msg) from e
        finally:
            if session:
                session.close()

        missing = {point for point in ((d, c) for d in d_slews for c in c_slews)} - set(points)
        if missing:
            raise ProcedureFailedException(
                f'Procedure measure_ff_constraint_matrix failed for cell {cell.name} '
                f'on {kind}/{d_dir}: no verdict at points {sorted(missing)}')

    result = cell.liberty
    timing_group = liberty.Group('timing')
    timing_group.add_attribute('related_pin', clock.name)
    timing_group.add_attribute('timing_type', f'{kind}_rising')
    lut_template = f'constraint_template_{len(d_slews)}x{len(c_slews)}'
    if points:
        lut = LookupTable(f'{d_dir}_constraint', lut_template,
                          constrained_pin_transition=list(d_slews),
                          related_pin_transition=list(c_slews))
        for (d_slew, c_slew), value in points.items():
            quantity = value @ PySpice.Unit.u_s
            lut[d_slew, c_slew] = quantity.convert(settings.units.time.prefixed_unit).value
        timing_group.add_group(lut)
    result.group('pin', data).add_group(timing_group)

    return result
