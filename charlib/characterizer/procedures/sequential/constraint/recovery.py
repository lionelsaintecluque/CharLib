"""Recovery & removal: the async pin's release edge bracketed around the
active clock edge, exactly as setup and hold bracket the data edge.

Port of the dfrbp hand campaign. Conditioning: the async pin asserted
from t=0 and the data pin held at the value the assertion opposes — the
operating point IS the asserted state, and the release is the only
event. Recovery (the release's setup): reference = clock-to-output with
the release far ahead; the release edge is bisected toward the active
edge under the pushout verdict, the criterion shared with setup.
Removal (the assertion's hold): the release is bisected behind the
edge; the edge samples the adverse data during assertion and the
assertion must win — verdict is the bounded disturbance statistic
against the asserted rail, the certificate re-checking it as witness.
Signs: recovery = t(edge) - t(release); removal = t(release) - t(edge).

The constrained axis (the async pin's transition) reuses ``data_slews``;
the related axis is ``clock_slews``. Rising-edge clocks only; the
measured output is the first declared one. The assertion preceding the
edge is assumed to satisfy the pin's own min_pulse_width (its rung).

Criteria ratified full-grid (x1.2 shared with setup; the removal depth
is insensitive — 0.1/0.3/0.5 identical). EXPERIMENTAL as long as hard
temporal constants remain in this file (see the banner).
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
# Chronologie et bornes, en fractions de la demi-periode active (c_pw) :
#   0.2   release de reference, loin devant le front actif
#   0.25  retard du front actif derriere la fin de la release de reference
#   0.3   largeur de l'impulsion d'horloge (periode PULSE = 4 periodes)
#   0.4   fenetre grossiere de la reference
#   0.04 / 0.3  bornes de bissection recovery ; 0.2 / 0.5  bornes removal
#   2 ps  garde de saturation : la bissection removal collee a sa borne
#         haute signifie qu'aucune release sure n'existe dans la fenetre
# Valeurs de la campagne dfrbp, validees par calibration, sans autre
# justification — meme statut que les bannieres de pushout_ff.
# ============================================================================
REF_RELEASE = 0.2
EDGE_DELAY = 0.25
CLK_WIDTH = 0.3
CLK_PERIODS = 4
REF_WIN = 0.4
RECOVERY_LO = 0.04
RECOVERY_HI = 0.3
REMOVAL_LO = 0.2
REMOVAL_HI = 0.5
SATURATION_GUARD = 2e-12
COARSE_STEP_FLOOR = 10e-12


@register('data_slews', 'clock_slews', 'metastability_constraint_load',
          'setup_pushout_criterion', 'hold_disturbance_depth',
          'qualification_frequency', 'qualification_duty_cycle')
def recovery_constraint(cell, config, settings):
    """Find the minimum time a control pin must be active before the trigger.

    This is analagous to setup time for an asynchronous control pin.
    For example, for a rising-edge DFF with an asynchronous reset, recovery time is the minimum
    time the reset signal must be active before the rising clock edge in order to reset the device
    state."""
    if cell.clear or cell.preset:
        yield (measure_release_matrix, cell, config, settings, 'recovery')


def _pwl_alter(source, points):
    flat = ' '.join(f'{fmt(t)} {fmt(v)}' for (t, v) in points)
    return f'alter @{source}[pwl] = [ {flat} ]'


def measure_release_matrix(cell, config, settings, kind):
    """Measure one release constraint over the whole (async slew x clock slew)
    matrix, in one ngspice session."""
    data, clock, async_pin, out = flop_pins(cell)
    if async_pin is None:
        raise ProcedureFailedException(
            f'Cell {cell.name}: {kind} needs a set or reset pin')

    is_clear = async_pin.role == Port.Role.CLEAR
    release_dir = 'rise' if async_pin.inversion else 'fall'
    q_capture = 'rise' if is_clear else 'fall'

    a_slews = config.parameters['data_slews']
    c_slews = config.parameters.get('clock_slews', a_slews)
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

    v_active = vss if async_pin.inversion else vdd
    v_off = vdd if async_pin.inversion else vss
    d_level = vdd if is_clear else vss
    stat = 'max' if is_clear else 'min'
    threshold = depth*float(vdd) if is_clear else (1 - depth)*float(vdd)
    disturbed = (lambda v: v > threshold) if is_clear else (lambda v: v < threshold)

    def release_points(td, t1):
        return [(0, v_active), (td, v_active), (t1, v_off)]

    points = {}

    if not settings.dry_run:
        circuit = utils.init_circuit('release_constraint', cell.netlist, config.models,
                                     settings.named_nodes, settings.units)
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
                    if pin.name == async_pin.name:
                        circuit.PieceWiseLinearVoltageSource(pin.name, f'v{pin.name}',
                            circuit.gnd, values=release_points(1e-9, 2e-9))
                    elif pin.name in (data, clock.name):
                        circuit.PulseVoltageSource(pin.name, f'v{pin.name}', circuit.gnd,
                                                   initial_value=vss, pulsed_value=vss,
                                                   pulse_width=1e-6, period=T_PERIOD)
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
            with open(debug_path / f'{kind}.sp', 'w', encoding='utf-8') as file:
                file.write(str(simulation))
            log_path = debug_path / f'{kind}.commands'

        session = None
        try:
            session = Session(simulator, simulation, settings, log_path=log_path)
            session.execute(pulse_alter(f'v{data}', d_level, d_level,
                                        1e-9, 1e-9, 1e-6, T_PERIOD))
            for a_slew in a_slews:
                s_r = float(a_slew * settings.units.time) / (high - low)
                r_ref = REF_RELEASE * c_pw
                e1 = r_ref + s_r + EDGE_DELAY * c_pw
                for c_slew in c_slews:
                    s_c = float(c_slew * settings.units.time) / (high - low)
                    edge_t = e1 + s_c/2
                    session.execute(pulse_alter(f'v{clock.name}', vss, vdd,
                                                e1, s_c, CLK_WIDTH*c_pw, CLK_PERIODS*c_per))
                    m_ref = (f'meas tran t_ref trig v(v{clock.name}) val={v50} rise=1 '
                             f'targ v(v{out}) val={v50} {q_capture}=1')

                    session.execute(_pwl_alter(f'v{async_pin.name}',
                                    release_points(r_ref, r_ref + s_r)))
                    t_step = max(s_c/4, COARSE_STEP_FLOOR)
                    t_win = edge_t + s_c/2 + REF_WIN*c_pw
                    session.execute(f'tran {fmt(t_step)} {fmt(t_win)}')
                    t_ref = session.measure('t_ref', m_ref)
                    session.execute('destroy all')
                    if t_ref is None:
                        continue
                    t_step = t_ref/100
                    session.execute(f'tran {fmt(t_step)} {fmt(t_win)}')
                    t_ref = session.measure('t_ref', m_ref)
                    session.execute('destroy all')
                    if t_ref is None:
                        continue

                    if kind == 'recovery':
                        b_lo = RECOVERY_LO*c_pw
                        b_hi = edge_t + s_c/2 + RECOVERY_HI*c_pw
                        t_win = edge_t + s_c/2 + 3*t_ref
                        b_fail, b_pass, b_td = b_hi, b_lo, (b_lo + b_hi)/2
                        for _ in range(ITERS):
                            session.execute(_pwl_alter(f'v{async_pin.name}',
                                            release_points(b_td, b_td + s_r)))
                            session.execute(f'tran {fmt(t_step)} {fmt(t_win)}')
                            m_push = session.measure('m_push',
                                m_ref.replace('t_ref', 'm_push'))
                            session.execute('destroy all')
                            if m_push is None or m_push > criterion*t_ref:
                                b_fail = b_td
                                b_td = (b_td + b_pass)/2
                            else:
                                b_pass = b_td
                                b_td = (b_td + b_fail)/2
                        session.execute(_pwl_alter(f'v{async_pin.name}',
                                        release_points(b_pass, b_pass + s_r)))
                        session.execute(f'tran {fmt(t_step)} {fmt(t_win)}')
                        value = session.measure('m_recovery',
                            f'meas tran m_recovery trig v(v{async_pin.name}) val={v50} '
                            f'{release_dir}=1 targ v(v{clock.name}) val={v50} rise=1')
                        session.execute('destroy all')
                    else:
                        t_from = e1
                        t_dl = edge_t + s_c/2 + 5*t_ref
                        t_win = edge_t + s_c/2 + 6*t_ref
                        b_lo = edge_t - s_c/2 - REMOVAL_LO*c_pw
                        b_hi = edge_t + s_c/2 + REMOVAL_HI*c_pw
                        b_prev, b_next, b_td = b_lo, b_hi, (b_lo + b_hi)/2
                        m_dist_cmd = (f'meas tran m_dist {stat} v(v{out}) '
                                      f'from={fmt(t_from)} to={fmt(t_dl)}')
                        for _ in range(ITERS):
                            session.execute(_pwl_alter(f'v{async_pin.name}',
                                            release_points(b_td, b_td + s_r)))
                            session.execute(f'tran {fmt(t_step)} {fmt(t_win)}')
                            m_dist = session.measure('m_dist', m_dist_cmd)
                            session.execute('destroy all')
                            if m_dist is None or disturbed(m_dist):
                                b_prev = b_td
                                b_td = (b_td + b_next)/2
                            else:
                                b_next = b_td
                                b_td = (b_td + b_prev)/2
                        if b_next > b_hi - SATURATION_GUARD:
                            continue
                        session.execute(_pwl_alter(f'v{async_pin.name}',
                                        release_points(b_next, b_next + s_r)))
                        session.execute(f'tran {fmt(t_step)} {fmt(t_win)}')
                        value = session.measure('m_removal',
                            f'meas tran m_removal trig v(v{clock.name}) val={v50} rise=1 '
                            f'targ v(v{async_pin.name}) val={v50} {release_dir}=1')
                        witness = session.measure('m_dist', m_dist_cmd)
                        session.execute('destroy all')
                        if witness is None or disturbed(witness):
                            value = None
                    if value is not None:
                        points[(a_slew, c_slew)] = value
        except ProcedureFailedException:
            raise
        except Exception as e:
            msg = f'Procedure measure_release_matrix failed for cell {cell.name} ' \
                  f'on {kind}'
            raise ProcedureFailedException(msg) from e
        finally:
            if session:
                session.close()

        missing = {point for point in ((a, c) for a in a_slews for c in c_slews)} - set(points)
        if missing:
            raise ProcedureFailedException(
                f'Procedure measure_release_matrix failed for cell {cell.name} on '
                f'{kind}: no verdict at points {sorted(missing)}')

    result = cell.liberty
    timing_group = liberty.Group('timing')
    timing_group.add_attribute('related_pin', clock.name)
    timing_group.add_attribute('timing_type', f'{kind}_rising')
    lut_template = f'constraint_template_{len(a_slews)}x{len(c_slews)}'
    if points:
        lut = LookupTable(f'{release_dir}_constraint', lut_template,
                          constrained_pin_transition=list(a_slews),
                          related_pin_transition=list(c_slews))
        for (a_slew, c_slew), value in points.items():
            quantity = value @ PySpice.Unit.u_s
            lut[a_slew, c_slew] = quantity.convert(settings.units.time.prefixed_unit).value
        timing_group.add_group(lut)
    result.group('pin', async_pin.name).add_group(timing_group)

    return result
