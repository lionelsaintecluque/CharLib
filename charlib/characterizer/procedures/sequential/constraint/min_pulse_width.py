"""Minimum transparency pulse, by bisection over valid probe pulses.

The stimulus generator only emits valid waveforms, as explicit PWL
vertices: full swing with the requested ramp down to one ramp of width,
then a slope-preserving triangle whose peak shrinks with the width. The
domain floor is the pulse still peaking at 90 % of the swing — the rule
the official tables follow (4/3 x slew) — and the generator refuses
anything below. Whatever gets simulated is therefore a legitimate
pulse, and the value written to the table is the one measured on the
probe's own node.

Conditioning is three-phase, by stimulus only: the operating point
stores the old value through the open gate, the gate closes, the data
flips while opaque, and a witness checks the stored value before the
probe is trusted. The verdict is composite: the gate-to-output delay
must not be pushed out beyond criterion x reference, AND the output
must be at the new rail at the deadline. The domain floor is probed
first with the full verdict — if it passes, that run is the point and
its own certificate. Both data polarities are measured; the worst one
enters the table.
"""

import PySpice

from charlib.characterizer import utils
from charlib.characterizer.cell import Port
from charlib.characterizer.procedures import register, ProcedureFailedException
from charlib.characterizer.procedures.session import Session, fmt, pulse_alter
from charlib.characterizer.procedures.sequential.testbench import (
    single_data_gate_output, transparent_high)
from charlib.liberty import liberty
from charlib.liberty.library import LookupTable

T_START = 0.5e-9
DATA_RAMP = 0.1e-9
OPAQUE_SETTLE = 0.2e-9
PROBE_SETTLE = 0.3e-9
ITERS = 14
T_PERIOD = 2e-6

@register('data_slews', 'clock_slews', 'metastability_constraint_load',
          'min_pulse_width_pushout_criterion',
          'qualification_frequency', 'qualification_duty_cycle')
def min_pulse_width_constraint(cell, config, settings):
    """Find the minimum pulse width required for the trigger to activate the device.

    This is analagous to the min_pulse_width property of a set, reset, enable, or clock pin.
    """
    for pin in cell.filter_pins(direction='input', trigger=Port.Trigger.EDGE):
        yield (find_min_pulse_width, cell, config, settings, pin)


def _probe_alter(gate_name, v_open_step, t_start, t_ramp, width):
    """Emit the probe pulse of the given 50-50 width, as explicit PWL vertices.

    Full swing above one ramp of width; below, a slope-preserving triangle
    whose peak shrinks with the width. The narrowest valid pulse peaks at
    90 % of the swing, at width = 0.8 ramp (the official 4/3 x slew floor).
    """
    if width < 0.8 * t_ramp:
        raise ValueError(f'probe peak below 90% of the swing: {width} < {0.8*t_ramp}')
    shrink = min((1 + width/t_ramp) / 2, 1)
    v_peak = v_open_step * shrink
    t_rise = t_ramp * shrink
    plateau = max(width - t_ramp, 1e-15)
    vertices = ((0, 0), (t_start, 0),
                (t_start + t_rise, v_peak),
                (t_start + t_rise + plateau, v_peak),
                (t_start + 2*t_rise + plateau, 0))
    flat = ' '.join(f'{fmt(t)} {fmt(v)}' for t, v in vertices)
    return f'alter @v{gate_name}B[pwl] = [ {flat} ]'


def _stacked_gate_circuit(cell, config, settings, data, gate, out, load):
    """The gate pin is driven by two stacked sources: v<gate>A carries the
    closing waveform, v<gate>B adds the probe pulse. The probe width is
    read back on their middle node."""
    vss = settings.primary_ground.voltage * settings.units.voltage
    circuit = utils.init_circuit('min_pulse_width', cell.netlist, config.models,
                                 settings.named_nodes, settings.units)
    connections = []
    for pin in cell.pins_in_netlist_order():
        if pin.name == data:
            connections.append(f'v{pin.name}')
            circuit.PulseVoltageSource(pin.name, f'v{pin.name}', circuit.gnd,
                                       initial_value=vss, pulsed_value=vss,
                                       pulse_width=1e-6, period=T_PERIOD)
        elif pin.name == gate.name:
            connections.append(f'v{pin.name}')
            circuit.PulseVoltageSource(f'{pin.name}A', f'v{pin.name}', 'nprobe',
                                       initial_value=vss, pulsed_value=vss,
                                       pulse_width=1e-6, period=T_PERIOD)
            circuit.PieceWiseLinearVoltageSource(f'{pin.name}B', 'nprobe', circuit.gnd,
                                                 values=[(0, vss), (1e-9, vss),
                                                         (2e-9, vss), (3e-9, vss),
                                                         (4e-9, vss)])
        elif pin.name == out:
            connections.append(f'v{pin.name}')
            circuit.C(pin.name, f'v{pin.name}', circuit.gnd, load)
        else:
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
                    raise ValueError(f'Unable to connect unrecognized pin {pin.name} in cell {cell.name}')
    circuit.X('dut', cell.name, *connections)
    return circuit


def find_min_pulse_width(cell, config, settings, input_pin):
    """Measure the minimum transparency pulse of the gate, for every gate
    slew, both data polarities, in one ngspice session."""
    data, gate, out = single_data_gate_output(cell)
    if input_pin.name != gate.name:
        raise ProcedureFailedException(
            f'Cell {cell.name}: min_pulse_width is only supported on the declared '
            f'gate pin for now (asked for {input_pin.name}, gate is {gate.name})')

    is_transparent_high = transparent_high(gate)
    open_dir = 'rise' if is_transparent_high else 'fall'
    return_dir = 'fall' if is_transparent_high else 'rise'

    slews = config.parameters.get('clock_slews', config.parameters['data_slews'])
    vdd = settings.primary_power.voltage * settings.units.voltage
    vss = settings.primary_ground.voltage * settings.units.voltage
    v_transp, v_opaque = (vdd, vss) if is_transparent_high else (vss, vdd)
    v_open_step = float(v_transp) - float(v_opaque)
    high = settings.logic_thresholds.high
    low = settings.logic_thresholds.low
    v50 = float(vdd) * 0.5
    v_low_rail = 0.1 * float(vdd)
    v_high_rail = 0.9 * float(vdd)
    load = config.parameters['metastability_constraint_load'] * settings.units.capacitance
    criterion = config.parameters['min_pulse_width_pushout_criterion']
    # the transparency the design grants; every window derives from it
    t_contract = config.parameters['qualification_duty_cycle'] \
               / config.parameters['qualification_frequency']

    # slew -> worst measured width over both polarities [s]
    points = {}
    disqualified = []

    if not settings.dry_run:
        circuit = _stacked_gate_circuit(cell, config, settings, data, gate, out, load)
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
            with open(debug_path / f'{gate.name}.sp', 'w', encoding='utf-8') as file:
                file.write(str(simulation))
            log_path = debug_path / f'{gate.name}.commands'

        session = None
        try:
            session = Session(simulator, simulation, settings, log_path=log_path)
            session.execute(f'alter c{out} = {fmt(load)}')
            for slew in slews:
                s_gate = float(slew * settings.units.time) / (high - low)
                t_close = T_START
                t_flip = t_close + s_gate + OPAQUE_SETTLE
                t_probe = t_flip + DATA_RAMP + PROBE_SETTLE
                w_floor = 0.8 * s_gate
                w_wide = t_contract

                widths = []
                for (v_old, v_new) in ((vss, vdd), (vdd, vss)):
                    out_dir = 'rise' if float(v_new) > float(v_old) else 'fall'
                    still_old = (lambda v: v is not None and v < v_low_rail) \
                        if out_dir == 'rise' else (lambda v: v is not None and v > v_high_rail)
                    captured = (lambda v: v is not None and v > v_high_rail) \
                        if out_dir == 'rise' else (lambda v: v is not None and v < v_low_rail)
                    m_delay = (f'meas tran {{name}} trig v(v{gate.name}) val={v50} '
                               f'{open_dir}=1 targ v(v{out}) val={v50} {out_dir}=1')

                    session.execute(pulse_alter(f'v{gate.name}A', v_transp, v_opaque,
                                                t_close, s_gate, 1e-6, T_PERIOD))
                    session.execute(pulse_alter(f'v{data}', v_old, v_new,
                                                t_flip, DATA_RAMP, 1e-6, T_PERIOD))
                    session.execute(_probe_alter(gate.name, v_open_step,
                                                 t_probe, s_gate, w_wide))

                    t_step = max(s_gate/4, 5e-12)
                    session.execute(f'tran {fmt(t_step)} {fmt(t_probe + s_gate + t_contract)}')
                    t_ref = session.measure('t_ref', m_delay.format(name='t_ref'))
                    v_cond = session.measure('v_cond',
                        f'meas tran v_cond find v(v{out}) at={fmt(t_probe)}')
                    session.execute('destroy all')
                    if t_ref is None or not still_old(v_cond):
                        disqualified.append((slew, out_dir))
                        continue

                    t_step = t_ref/100
                    session.execute(f'tran {fmt(t_step)} {fmt(t_probe + s_gate/2 + 2.5*t_ref)}')
                    t_ref = session.measure('t_ref', m_delay.format(name='t_ref'))
                    session.execute('destroy all')
                    if t_ref is None:
                        disqualified.append((slew, out_dir))
                        continue

                    def probe_verdict(width):
                        session.execute(_probe_alter(gate.name, v_open_step,
                                                     t_probe, s_gate, width))
                        t_deadline = t_probe + width + s_gate + 4*t_ref
                        session.execute(f'tran {fmt(t_step)} {fmt(t_deadline + t_ref)}')
                        m_push = session.measure('m_push', m_delay.format(name='m_push'))
                        v_end = session.measure('v_end',
                            f'meas tran v_end find v(v{out}) at={fmt(t_deadline)}')
                        return m_push is not None and m_push <= criterion*t_ref \
                           and captured(v_end)

                    def measure_width():
                        threshold = fmt(v_open_step/2)
                        return session.measure('m_mpw',
                            f'meas tran m_mpw trig v(nprobe) val={threshold} '
                            f'{open_dir}=1 targ v(nprobe) val={threshold} '
                            f'{return_dir}=1')

                    # if the narrowest valid pulse passes, that run is the
                    # point and its own certificate
                    if probe_verdict(w_floor):
                        measured = measure_width()
                        session.execute('destroy all')
                    else:
                        session.execute('destroy all')
                        w_fail, w_pass = w_floor, w_wide
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
                        if not probe_verdict(w_pass):
                            session.execute('destroy all')
                            continue
                        measured = measure_width()
                        session.execute('destroy all')
                    if measured is not None:
                        widths.append(measured)

                if len(widths) == 2:
                    points[slew] = max(widths)
        except ProcedureFailedException:
            raise
        except Exception as e:
            msg = f'Procedure find_min_pulse_width failed for cell {cell.name} ' \
                  f'on pin {gate.name}'
            raise ProcedureFailedException(msg) from e
        finally:
            if session:
                session.close()

        if disqualified:
            raise ProcedureFailedException(
                f'Cell {cell.name} disqualified: cannot settle inside its '
                f'{t_contract*1e9:g} ns qualification contract at (slew, direction) '
                f'{sorted(disqualified)}')
        missing = set(slews) - set(points)
        if missing:
            raise ProcedureFailedException(
                f'Procedure find_min_pulse_width failed for cell {cell.name} on pin '
                f'{gate.name}: no verdict at slews {sorted(missing)}')

    result = cell.liberty
    timing_group = liberty.Group('timing')
    timing_group.add_attribute('related_pin', gate.name)
    timing_group.add_attribute('timing_type', 'min_pulse_width')
    if points:
        lut = LookupTable(f'{open_dir}_constraint', f'mpw_template_{len(slews)}',
                          constrained_pin_transition=list(slews))
        for slew, value in points.items():
            quantity = value @ PySpice.Unit.u_s
            lut[slew,] = quantity.convert(settings.units.time.prefixed_unit).value
        timing_group.add_group(lut)
    result.group('pin', gate.name).add_group(timing_group)

    return result
