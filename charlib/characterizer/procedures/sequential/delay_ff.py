"""Flip-flop propagation arcs, on the session engine.

Clock-to-output delays at both captured polarities, and the async pin's
assertion arcs (``clear`` on the true output, ``preset`` on its
complement). The machinery is the one of setup_hold_pushout_ff — reset
conditioning, the two-cycle contract-timed sequence for capture-of-0,
the saved window blinding the first edge — promoted from measuring
references to publishing tables.

Any output beyond the first is assumed to be the complement of the
first and is measured with inverted polarities in the same transients;
the calibration diff against the official tables validated that
assumption. EXPERIMENTAL as long as hard temporal constants remain
(the reset conditioning pulse imported from pushout_ff's banners).
"""

import PySpice

from charlib.characterizer import utils
from charlib.characterizer.cell import Port
from charlib.characterizer.procedures import register, ProcedureFailedException
from charlib.characterizer.procedures.session import Session, fmt, pulse_alter
from charlib.characterizer.procedures.sequential.testbench import flop_pins
from charlib.characterizer.procedures.sequential.constraint.pushout_ff import (
    R_MPW, R_RAMP, D_COND_FRACTION, D_DROP_FRACTION, T_BLIND_FRACTION,
    COARSE_STEP_FLOOR)
from charlib.liberty import liberty
from charlib.liberty.library import LookupTable

T_PERIOD = 2e-6

# ============================== CONSTANTES NUES =============================
# Fractions de la campagne dfrbp, meme statut que les bannieres de
# pushout_ff :
#   0.4   fenetre grossiere de la reference, en fractions de c_pw
#   1.0   assertion du pin async a une periode du contrat (Q=1 etabli
#         au front de c_pw, une demi-periode de marge avant l'assertion)
#   4     periodes PULSE de l'horloge du banc d'assertion (un seul front
#         actif dans la fenetre de mesure)
# ============================================================================
REF_WIN = 0.4
ASSERT_AT_PERIODS = 1.0
ASSERT_CLK_PERIODS = 4


@register('data_slews', 'clock_slews', 'loads',
          'qualification_frequency', 'qualification_duty_cycle')
def sequential_worst_case_ff(cell, config, settings):
    """Measure flip-flop clock-to-output and async assertion arcs"""
    for output_transition in ('01', '10'):
        yield (measure_capture_matrix, cell, config, settings, output_transition)
    if cell.clear or cell.preset:
        yield (measure_assertion_matrix, cell, config, settings)


def _pwl_alter(source, points):
    flat = ' '.join(f'{fmt(t)} {fmt(v)}' for (t, v) in points)
    return f'alter @{source}[pwl] = [ {flat} ]'


def _ff_pins(cell):
    data, clock, reset, out = flop_pins(cell)
    if reset is None:
        raise ProcedureFailedException(
            f'Cell {cell.name}: conditioning needs a set or reset pin')
    outs = cell.outputs
    if len(outs) > 2:
        raise ProcedureFailedException(
            f'Cell {cell.name}: at most one complementary output is supported '
            f'(found outputs {outs})')
    return data, clock, reset, outs


def _circuit(cell, config, settings, data, clock, reset, outs, n_data_pwl):
    vss = settings.primary_ground.voltage * settings.units.voltage
    vdd = settings.primary_power.voltage * settings.units.voltage
    v_rst_active = vss if reset.inversion else vdd
    load0 = config.parameters['loads'][0] * settings.units.capacitance
    circuit = utils.init_circuit('ff_delay', cell.netlist, config.models,
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
                if pin.name == data:
                    circuit.PieceWiseLinearVoltageSource(pin.name, f'v{pin.name}',
                        circuit.gnd, values=[(k*1e-9, vss) for k in range(n_data_pwl)])
                elif pin.name == clock.name:
                    circuit.PulseVoltageSource(pin.name, f'v{pin.name}', circuit.gnd,
                                               initial_value=vss, pulsed_value=vss,
                                               pulse_width=1e-6, period=T_PERIOD)
                elif pin.name == reset.name:
                    circuit.PieceWiseLinearVoltageSource(pin.name, f'v{pin.name}',
                        circuit.gnd, values=[(k*1e-9, v_rst_active) for k in range(5)])
                elif pin.name in outs:
                    circuit.C(pin.name, f'v{pin.name}', circuit.gnd, load0)
    circuit.X('dut', cell.name, *connections)
    return circuit


def _session_for(cell, config, settings, circuit, stem):
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
        with open(debug_path / f'{stem}.sp', 'w', encoding='utf-8') as file:
            file.write(str(simulation))
        log_path = debug_path / f'{stem}.commands'
    return Session(simulator, simulation, settings, log_path=log_path)


def _emit(result, samples, outs, related, timing_types, senses, loads, slews, settings):
    lut_template = f'delay_template_{len(loads)}x{len(slews)}'
    for k, out in enumerate(outs):
        timing_group = liberty.Group('timing')
        timing_group.add_attribute('related_pin', related)
        timing_group.add_attribute('timing_sense', senses[k])
        timing_group.add_attribute('timing_type', timing_types[k])
        for name, matrix in samples.items():
            if not name.startswith(f'{out}:'):
                continue
            lut = LookupTable(name.split(':')[1], lut_template,
                              total_output_net_capacitance=list(loads),
                              input_net_transition=list(slews))
            for (load, slew), value in matrix.items():
                quantity = value @ PySpice.Unit.u_s
                lut[load, slew] = quantity.convert(settings.units.time.prefixed_unit).value
            timing_group.add_group(lut)
        result.group('pin', out).add_group(timing_group)


def _measure_outputs(session, outs, dirs, trig, v50, vdd_f, low, high, samples,
                     point):
    complete = True
    for out, direction in zip(outs, dirs):
        delay = session.measure('m_tpd',
            f'meas tran m_tpd {trig} targ v(v{out}) val={v50} {direction}=1')
        (v_a, v_b) = (low, high) if direction == 'rise' else (high, low)
        transition = session.measure('m_slew',
            f'meas tran m_slew trig v(v{out}) val={v_a*vdd_f} {direction}=1 '
            f'targ v(v{out}) val={v_b*vdd_f} {direction}=1')
        if delay is None or transition is None:
            complete = False
            continue
        samples.setdefault(f'{out}:cell_{direction}', {})[point] = delay
        samples.setdefault(f'{out}:{direction}_transition', {})[point] = transition
    return complete


def measure_capture_matrix(cell, config, settings, output_transition):
    """Clock-to-output delays for one captured polarity, whole matrix, one session."""
    data, clock, reset, outs = _ff_pins(cell)
    two_cycle = output_transition == '10'
    dirs = ('fall', 'rise') if two_cycle else ('rise', 'fall')

    slews = config.parameters.get('clock_slews', config.parameters['data_slews'])
    loads = config.parameters['loads']
    vdd = settings.primary_power.voltage * settings.units.voltage
    vss = settings.primary_ground.voltage * settings.units.voltage
    high = settings.logic_thresholds.high
    low = settings.logic_thresholds.low
    v50 = float(vdd) * 0.5
    c_per = 1 / config.parameters['qualification_frequency']
    c_pw = config.parameters['qualification_duty_cycle'] * c_per
    d_cond = D_COND_FRACTION * c_per
    d_drop = D_DROP_FRACTION * c_per
    t_blind = T_BLIND_FRACTION * c_per if two_cycle else 0
    v_rst_active = vss if reset.inversion else vdd
    v_rst_off = vdd if reset.inversion else vss

    samples = {}
    if not settings.dry_run:
        circuit = _circuit(cell, config, settings, data, clock, reset, outs,
                           n_data_pwl=5 if two_cycle else 3)
        session = None
        try:
            session = _session_for(cell, config, settings, circuit,
                                   f'capture_{dirs[0]}')
            session.execute(_pwl_alter(f'v{reset.name}',
                [(0, v_rst_active), (R_MPW, v_rst_active), (R_MPW + R_RAMP, v_rst_off),
                 (R_MPW + R_RAMP + 1e-12, v_rst_off), (R_MPW + R_RAMP + 2e-12, v_rst_off)]))
            blind_arg = f' {fmt(t_blind)}' if two_cycle else ''
            edge_base = (c_per if two_cycle else 0) + c_pw
            for slew in slews:
                s_c = float(slew * settings.units.time) / (high - low)
                edge_t = edge_base + s_c/2
                session.execute(pulse_alter(f'v{clock.name}', vss, vdd,
                                            c_pw, s_c, c_pw, c_per))
                if two_cycle:
                    s_d = s_c
                    session.execute(_pwl_alter(f'v{data}',
                        [(0, vss), (d_cond, vss), (d_cond + s_d, vdd),
                         (d_drop, vdd), (d_drop + s_d, vss)]))
                else:
                    session.execute(_pwl_alter(f'v{data}',
                        [(0, vdd), (1e-9, vdd), (2e-9, vdd)]))
                trig = f'trig v(v{clock.name}) val={v50} rise=1'
                m_ref = (f'meas tran t_ref {trig} '
                         f'targ v(v{outs[0]}) val={v50} {dirs[0]}=1')

                # reference at the heaviest load: its window covers every
                # lighter point of the matrix
                for out in outs:
                    session.execute(f'alter c{out} = '
                                    f'{fmt(max(loads)*settings.units.capacitance)}')
                t_step = max(s_c/4, COARSE_STEP_FLOOR)
                t_win = edge_t + s_c/2 + REF_WIN*c_pw
                session.execute(f'tran {fmt(t_step)} {fmt(t_win)}{blind_arg}')
                t_ref = session.measure('t_ref', m_ref)
                session.execute('destroy all')
                if t_ref is None:
                    continue
                t_step = t_ref/100
                t_win = edge_t + s_c/2 + 3*t_ref
                for load in loads:
                    for out in outs:
                        session.execute(f'alter c{out} = '
                                        f'{fmt(load*settings.units.capacitance)}')
                    session.execute(f'tran {fmt(t_step)} {fmt(t_win)}{blind_arg}')
                    _measure_outputs(session, outs, dirs, trig, v50, float(vdd),
                                     low, high, samples, (load, slew))
                    session.execute('destroy all')
        except ProcedureFailedException:
            raise
        except Exception as e:
            msg = f'Procedure measure_capture_matrix failed for cell {cell.name} ' \
                  f'on capture {output_transition}'
            raise ProcedureFailedException(msg) from e
        finally:
            if session:
                session.close()

        expected = {(l, s) for l in loads for s in slews}
        for name, matrix in samples.items():
            if set(matrix) != expected:
                raise ProcedureFailedException(
                    f'Procedure measure_capture_matrix failed for cell {cell.name}: '
                    f'{name} missing points {sorted(expected - set(matrix))}')

    result = cell.liberty
    _emit(result, samples, outs, clock.name,
          timing_types=('rising_edge',) * len(outs),
          senses=('non_unate',) * len(outs),
          loads=loads, slews=slews, settings=settings)
    return result


def measure_assertion_matrix(cell, config, settings):
    """Async assertion arcs (clear/preset), whole matrix, one session."""
    data, clock, reset, outs = _ff_pins(cell)
    is_clear = reset.role == Port.Role.CLEAR
    assert_dir = 'fall' if reset.inversion else 'rise'
    dirs = ('fall', 'rise') if is_clear else ('rise', 'fall')
    timing_types = ('clear', 'preset') if is_clear else ('preset', 'clear')
    senses = tuple('positive_unate' if d == assert_dir else 'negative_unate'
                   for d in dirs)

    a_slews = config.parameters['data_slews']
    loads = config.parameters['loads']
    vdd = settings.primary_power.voltage * settings.units.voltage
    vss = settings.primary_ground.voltage * settings.units.voltage
    high = settings.logic_thresholds.high
    low = settings.logic_thresholds.low
    v50 = float(vdd) * 0.5
    c_per = 1 / config.parameters['qualification_frequency']
    c_pw = config.parameters['qualification_duty_cycle'] * c_per
    t_assert = ASSERT_AT_PERIODS * c_per
    v_rst_active = vss if reset.inversion else vdd
    v_rst_off = vdd if reset.inversion else vss
    d_level = vdd if is_clear else vss

    samples = {}
    if not settings.dry_run:
        circuit = _circuit(cell, config, settings, data, clock, reset, outs,
                           n_data_pwl=3)
        session = None
        try:
            session = _session_for(cell, config, settings, circuit, 'assertion')
            session.execute(_pwl_alter(f'v{data}',
                [(0, d_level), (1e-9, d_level), (2e-9, d_level)]))
            for a_slew in a_slews:
                s_r = float(a_slew * settings.units.time) / (high - low)
                session.execute(_pwl_alter(f'v{reset.name}',
                    [(0, v_rst_active), (R_MPW, v_rst_active),
                     (R_MPW + R_RAMP, v_rst_off),
                     (t_assert, v_rst_off), (t_assert + s_r, v_rst_active)]))
                s_c = float(min(a_slews) * settings.units.time) / (high - low)
                session.execute(pulse_alter(f'v{clock.name}', vss, vdd,
                                            c_pw, s_c, c_pw,
                                            ASSERT_CLK_PERIODS*c_per))
                trig = f'trig v(v{reset.name}) val={v50} {assert_dir}=1'
                m_ref = (f'meas tran t_ref {trig} '
                         f'targ v(v{outs[0]}) val={v50} {dirs[0]}=1')

                for out in outs:
                    session.execute(f'alter c{out} = '
                                    f'{fmt(max(loads)*settings.units.capacitance)}')
                t_step = max(s_r/4, COARSE_STEP_FLOOR)
                t_win = t_assert + s_r + REF_WIN*c_pw
                session.execute(f'tran {fmt(t_step)} {fmt(t_win)}')
                t_ref = session.measure('t_ref', m_ref)
                session.execute('destroy all')
                if t_ref is None:
                    continue
                t_step = t_ref/100
                t_win = t_assert + s_r + 3*t_ref
                for load in loads:
                    for out in outs:
                        session.execute(f'alter c{out} = '
                                        f'{fmt(load*settings.units.capacitance)}')
                    session.execute(f'tran {fmt(t_step)} {fmt(t_win)}')
                    _measure_outputs(session, outs, dirs, trig, v50, float(vdd),
                                     low, high, samples, (load, a_slew))
                    session.execute('destroy all')
        except ProcedureFailedException:
            raise
        except Exception as e:
            msg = f'Procedure measure_assertion_matrix failed for cell {cell.name}'
            raise ProcedureFailedException(msg) from e
        finally:
            if session:
                session.close()

        expected = {(l, s) for l in loads for s in a_slews}
        for name, matrix in samples.items():
            if set(matrix) != expected:
                raise ProcedureFailedException(
                    f'Procedure measure_assertion_matrix failed for cell {cell.name}: '
                    f'{name} missing points {sorted(expected - set(matrix))}')

    result = cell.liberty
    _emit(result, samples, outs, reset.name,
          timing_types=timing_types, senses=senses,
          loads=loads, slews=a_slews, settings=settings)
    return result
