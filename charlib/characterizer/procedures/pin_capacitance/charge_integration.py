"""Input capacitance by charge integration, on driven, conditioned states.

Every pin of the cell is driven to a defined logic level — nothing floats
behind an isolation RC. The target pin swings VSS->VDD->VSS and the net
charge drawn from its source is integrated over each edge; C = |Q| / VDD.
Both edges are measured for every combination of the other input levels,
in one ngspice session per pin; the published rise/fall capacitances are
the worst state, and ``capacitance`` is their average — the convention of
the official tables, verified on the calibration cell.

A level-sensitive gate pin is held TRANSPARENT while another pin is
measured: the operating point then conditions the internal state for
free, where an opaque gate would leave the bistable DC solution
ambiguous. Integration windows extend an order of magnitude beyond the
ramp: internal switching lags the pin edge, and the net charge was
measured to be insensitive to any wider window.
"""

import itertools

import PySpice

from charlib.characterizer import utils
from charlib.characterizer.cell import Port
from charlib.characterizer.procedures import register, ProcedureFailedException
from charlib.characterizer.procedures.session import Session, fmt, pulse_alter
from charlib.characterizer.procedures.sequential.testbench import transparent_high

T_PERIOD = 2e-6
DRIVEN_ROLES = ['logic', 'clock', 'set', 'reset', 'enable']

@register('data_slews', 'charge_integration_t_slew', 'charge_integration_t_wait')
def charge_integration(cell, config, settings):
    """Measure input capacitance for each input pin using charge integration and return a liberty cell group"""
    for target_pin in cell.filter_pins(direction=['input'], role=DRIVEN_ROLES):
        yield (measure_pin_cap_by_charge_integration, cell, settings, config, target_pin)


def measure_pin_cap_by_charge_integration(cell, settings, config, target_pin):
    """Integrate the charge drawn by target_pin over a full up-down swing,
    for every combination of the other input levels, in one session."""
    vdd = settings.primary_power.voltage * settings.units.voltage
    vss = settings.primary_ground.voltage * settings.units.voltage

    t_slew_val = config.parameters.get('charge_integration_t_slew', 0)
    if t_slew_val == 0:
        t_slew_val = min(config.parameters['data_slews'])
    t_wait_val = config.parameters.get('charge_integration_t_wait', 0)
    if t_wait_val == 0:
        t_wait_val = 1000 * t_slew_val
    t_slew = float(t_slew_val * settings.units.time)
    t_wait = float(t_wait_val * settings.units.time)
    t_tail = 10 * t_slew

    t_rise = t_wait
    t_fall = t_wait + t_slew + max(t_wait, t_tail)
    t_end = t_fall + t_slew + t_tail

    other_pins = [pin for pin in cell.filter_pins(direction=['input'], role=DRIVEN_ROLES)
                  if pin.name != target_pin.name]

    def held_levels(pin):
        """The levels a non-target pin is held at across measurement states."""
        if pin.role in (Port.Role.CLOCK, Port.Role.ENABLE):
            return [vdd if transparent_high(pin) else vss]
        return [vss, vdd]

    circuit = utils.init_circuit(f'cell-{cell.name}-pin-{target_pin.name}-cap',
                                 cell.netlist, config.models,
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
                if pin.direction == Port.Direction.IN:
                    circuit.PulseVoltageSource(pin.name, f'v{pin.name}', circuit.gnd,
                                               initial_value=vss, pulsed_value=vss,
                                               pulse_width=1e-6, period=T_PERIOD)
    circuit.X('dut', cell.name, *connections)

    simulator = PySpice.Simulator.factory(simulator=settings.simulation.backend)
    simulation = simulator.simulation(
        circuit,
        temperature=settings.temperature,
        nominal_temperature=settings.temperature
    )

    log_path = None
    if settings.debug:
        debug_path = settings.debug_dir / cell.name / __name__.split('.')[-1]
        debug_path.mkdir(parents=True, exist_ok=True)
        with open(debug_path / f'{target_pin.name}.sp', 'w', encoding='utf-8') as file:
            file.write(str(simulation))
        log_path = debug_path / f'{target_pin.name}.commands'

    rise_caps, fall_caps = [], []
    if not settings.dry_run:
        session = None
        try:
            session = Session(simulator, simulation, settings, log_path=log_path)
            session.execute(pulse_alter(f'v{target_pin.name}', vss, vdd,
                                        t_rise, t_slew, t_fall - t_rise - t_slew, T_PERIOD))
            for levels in itertools.product(*(held_levels(pin) for pin in other_pins)):
                for pin, level in zip(other_pins, levels):
                    session.execute(pulse_alter(f'v{pin.name}', level, level,
                                                t_rise, t_slew, 1e-6, T_PERIOD))
                session.execute(f'tran {fmt(t_slew/10)} {fmt(t_end)}')
                q_rise = session.measure('q_rise',
                    f'meas tran q_rise integ i(v{target_pin.name}) '
                    f'from={fmt(t_rise - t_slew)} to={fmt(t_rise + t_slew + t_tail)}')
                q_fall = session.measure('q_fall',
                    f'meas tran q_fall integ i(v{target_pin.name}) '
                    f'from={fmt(t_fall - t_slew)} to={fmt(t_fall + t_slew + t_tail)}')
                session.execute('destroy all')
                if q_rise is None or q_fall is None:
                    raise ProcedureFailedException(
                        f'Charge integration failed for cell {cell.name}, pin '
                        f'{target_pin.name} with {dict(zip((p.name for p in other_pins), levels))}')
                rise_caps.append(abs(q_rise) / float(vdd))
                fall_caps.append(abs(q_fall) / float(vdd))
        except ProcedureFailedException:
            raise
        except Exception as e:
            msg = f'Procedure measure_pin_cap_by_charge_integration failed for ' \
                  f'cell {cell.name}, pin {target_pin.name}'
            raise ProcedureFailedException(msg) from e
        finally:
            if session:
                session.close()

    result = cell.liberty
    if rise_caps and fall_caps:
        def to_lib(cap):
            return (cap @ PySpice.Unit.u_F).convert(settings.units.capacitance.prefixed_unit).value
        rise_cap = max(rise_caps)
        fall_cap = max(fall_caps)
        pin_group = result.group('pin', target_pin.name)
        pin_group.add_attribute('rise_capacitance', to_lib(rise_cap))
        pin_group.add_attribute('fall_capacitance', to_lib(fall_cap))
        pin_group.add_attribute('capacitance', to_lib((rise_cap + fall_cap) / 2))
    return result
