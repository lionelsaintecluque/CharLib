"""Combinational delay measurement.

One simulation task per path through the cell. Each task drives a single
interactive ngspice session through the whole (conditions x data_slews x
loads) measurement matrix: the test circuit is parsed once, then every
point is reached with `alter` (input waveforms, load capacitance) followed
by `tran` + `meas` — never by reloading the circuit or respawning the
simulator. Simulation windows follow a two-pass discipline: the first
point of a session runs with the generous upstream window, later points
derive their window from the longest value measured so far and grow it on
measurement failure.
"""

import re

import PySpice
from numpy import average

from charlib.characterizer import utils
from charlib.characterizer.cell import Port
from charlib.characterizer.procedures import register, ProcedureFailedException
from charlib.liberty import liberty
from charlib.liberty.library import LookupTable

@register('data_slews', 'loads', 'transient_sim_end_time')
def combinational_worst_case(cell, config, settings):
    """Measure worst-case combinational transient and propagation delays"""
    for path in cell.paths():
        yield (measure_delay_matrix_for_path, cell, config, settings, path, max)

@register('data_slews', 'loads', 'transient_sim_end_time')
def combinational_average(cell, config, settings):
    """Measure combinational transient and propagation delays using a uniform average"""
    for path in cell.paths():
        yield (measure_delay_matrix_for_path, cell, config, settings, path, average)


def _fmt(value):
    """Render a quantity as a plain SI float for an interactive ngspice command."""
    return f'{float(value):.12e}'

def _pwl_alter(source, points):
    """Build the command altering a PWL source to the given (time, voltage) vertices."""
    flat = ' '.join(f'{_fmt(t)} {_fmt(v)}' for (t, v) in points)
    return f'alter @{source}[pwl] = [ {flat} ]'

def _condition_measurements(pin_map, thresholds, vdd):
    """Return (name, meas command) pairs for one nonmasking condition.

    Names and thresholds are identical to what this procedure has always
    measured; only the transport changed (interactive `meas` instead of a
    .meas card).
    """
    measurements = []
    for out_pin in pin_map.target_outputs:
        for in_pin in pin_map.target_inputs:
            if pin_map.target_inputs[in_pin] == '01':
                in_direction = 'rise'
                threshold_prop_0 = thresholds.rising
            else:
                in_direction = 'fall'
                threshold_prop_0 = thresholds.falling
            if pin_map.target_outputs[out_pin] == '01':
                out_direction = 'rise'
                threshold_prop_1 = thresholds.rising
                threshold_tran_0 = thresholds.low
                threshold_tran_1 = thresholds.high
            else:
                out_direction = 'fall'
                threshold_prop_1 = thresholds.falling
                threshold_tran_0 = thresholds.high
                threshold_tran_1 = thresholds.low
            prop_name = f'cell_{out_direction}__{in_pin}_to_{out_pin}'.lower()
            measurements.append((prop_name,
                f'meas tran {prop_name} '
                f'trig v(v{in_pin}) val={float(vdd*threshold_prop_0)} {in_direction}=1 '
                f'targ v(v{out_pin}) val={float(vdd*threshold_prop_1)} {out_direction}=1'))
            tran_name = f'{out_direction}_transition__{in_pin}_to_{out_pin}'.lower()
            measurements.append((tran_name,
                f'meas tran {tran_name} '
                f'trig v(v{out_pin}) val={float(vdd*threshold_tran_0)} {out_direction}=1 '
                f'targ v(v{out_pin}) val={float(vdd*threshold_tran_1)} {out_direction}=1'))
    return measurements

def measure_delay_matrix_for_path(cell, config, settings, path, criterion=max):
    """Measure the full delay matrix for one path through the cell, in one session.

    This method tests all nonmasking conditions for the path through the cell
    from target_input to target_output over every (data_slew, load) point,
    then assigns each point the delay selected using the passed criterion
    function. Returns a liberty cell group with full lookup tables.

    The default criterion selects the worst-case (i.e. maximum) delay across
    conditions. See combinational_worst_case for caveats.

    :param cell: A Cell object to test.
    :param config: A CellTestConfig object containing cell-specific test configuration details.
    :param settings: A CharacterizationSettings object containing library-wide configuration
                     details.
    :param path: A list in the format [input_pin, input_transition, output_pin,
                 output_transtition] describing the path under test in the cell.
    :param criterion: A function which returns a single value given a list of numeric values.
                      Default max.
    """
    [input_pin, _, output_pin, output_transition] = path
    conditions = list(cell.nonmasking_conditions_for_path(*path))

    slews = config.parameters['data_slews']
    loads = config.parameters['loads']
    vdd = settings.primary_power.voltage * settings.units.voltage
    vss = settings.primary_ground.voltage * settings.units.voltage
    low = settings.logic_thresholds.low
    high = settings.logic_thresholds.high
    t_end_config = config.parameters.get('transient_sim_end_time', 0) * settings.units.time

    # name -> {(load, slew): [one measured value per condition, in seconds]}
    samples = {}

    if conditions and not settings.dry_run:
        pin_maps = [utils.PinStateMap(cell.inputs, cell.outputs, state_map)
                    for state_map in conditions]
        # An output loaded in any condition keeps its capacitor for the whole
        # session; outputs never measured stay floating as before.
        capped_outputs = {name for pin_map in pin_maps for name in pin_map.target_outputs}

        # Build the one test circuit for this path. Every logic input gets its
        # own PWL source so conditions and slews are switched by `alter`.
        circuit = utils.init_circuit('comb_delay', cell.netlist, config.models,
                                     settings.named_nodes, settings.units)
        connections = []
        for pin in cell.pins_in_netlist_order():
            match pin.role:
                case Port.Role.LOGIC:
                    if pin.name in cell.inputs:
                        connections.append(f'v{pin.name}')
                        circuit.PieceWiseLinearVoltageSource(
                            pin.name, f'v{pin.name}', circuit.gnd,
                            values=utils.slew_pwl(vss, vss, slews[0]*settings.units.time,
                                                  3*slews[0]*settings.units.time, low, high))
                    elif pin.name in capped_outputs:
                        connections.append(f'v{pin.name}')
                        circuit.C(pin.name, f'v{pin.name}', circuit.gnd,
                                  loads[0]*settings.units.capacitance)
                    elif pin.name in cell.outputs:
                        connections.append('wfloat0')
                    else:
                        raise ValueError(f'Unable to connect unrecognized logic pin {pin.name} in cell {cell.name}')
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

        simulator = PySpice.Simulator.factory(simulator=settings.simulation.backend)
        simulation = simulator.simulation(
            circuit,
            temperature=settings.temperature,
            nominal_temperature=settings.temperature
        )
        simulation.options(trtol=1)

        shared = getattr(simulator, 'ngspice', None)
        if shared is None:
            raise ProcedureFailedException(
                f'Backend {settings.simulation.backend} does not expose an interactive '
                'ngspice session; combinational delay measurement requires ngspice-shared')

        # In debug mode the session transcript is written incrementally, so a
        # killed run still leaves the evidence of what it was doing.
        log_file = None
        if settings.debug:
            debug_path = settings.debug_dir / cell.name / __name__.split('.')[-1]
            debug_path.mkdir(parents=True, exist_ok=True)
            stem = f'{input_pin}_to_{output_pin}_{"rise" if output_transition == "01" else "fall"}'
            with open(debug_path / f'{stem}.sp', 'w', encoding='utf-8') as file:
                file.write(str(simulation))
            log_file = open(debug_path / f'{stem}.commands', 'w', encoding='utf-8')

        def execute(command):
            if log_file:
                log_file.write(command + '\n')
                log_file.flush()
            output = shared.exec_command(command)
            if log_file and output:
                log_file.write(''.join(f'* {line}\n' for line in output.splitlines()))
                log_file.flush()
            return output

        try:
            shared.destroy()
            shared.load_circuit(str(simulation))
            # Parallelism lives at the process level (one worker per core);
            # ngspice's own OpenMP threads only fight each other there — with
            # several concurrent instances their active-wait barriers slow a
            # cell-sized tran by orders of magnitude (and OMP_NUM_THREADS is
            # overridden by ngspice, so it must be set in-session).
            execute('set num_threads = 1')

            t_longest = None # longest value measured so far in this session [s]
            for pin_map in pin_maps:
                measurements = _condition_measurements(pin_map, settings.logic_thresholds, vdd)
                for slew in slews:
                    data_slew = slew * settings.units.time
                    for name in cell.inputs:
                        if name in pin_map.target_inputs:
                            (v_0, v_1) = (vss, vdd) if pin_map.target_inputs[name] == '01' else (vdd, vss)
                        else:
                            v_0 = v_1 = vss if pin_map.stable_inputs[name] == '0' else vdd
                        execute(_pwl_alter(f'v{name}',
                                           utils.slew_pwl(v_0, v_1, data_slew, 3*data_slew, low, high)))
                    t_step = float(data_slew) / 8
                    t_settled = float(3*data_slew) + float(data_slew) / (high - low)
                    t_ceiling = float(max(t_end_config, 1000*data_slew))
                    for load in loads:
                        for out_pin in capped_outputs:
                            execute(f'alter c{out_pin} = {_fmt(load*settings.units.capacitance)}')
                        # Two-pass windows: generous ceiling until something is
                        # measured, then derived from the measured values and
                        # grown on failure.
                        t_window = t_ceiling if t_longest is None \
                              else min(t_settled + 8*t_longest, t_ceiling)
                        while True:
                            execute(f'tran {_fmt(t_step)} {_fmt(t_window)}')
                            values = {}
                            for name, command in measurements:
                                try:
                                    output = execute(command)
                                except Exception:
                                    output = ''
                                match = re.search(rf'{name}\s*=\s*([\-+0-9.eE]+)', output)
                                values[name] = float(match.group(1)) if match else None
                            execute('destroy all')
                            if all(v is not None for v in values.values()) or t_window >= t_ceiling:
                                break
                            t_window = min(2*t_window, t_ceiling)
                        for name, value in values.items():
                            if value is not None:
                                samples.setdefault(name, {}).setdefault((load, slew), []).append(value)
                                t_longest = max(t_longest or 0, value)
        except ProcedureFailedException:
            raise
        except Exception as e:
            msg = f'Procedure measure_delay_matrix_for_path failed for cell {cell.name} ' \
                  f'on path {path}'
            raise ProcedureFailedException(msg) from e
        finally:
            if log_file:
                log_file.close()

        # Every point of every table must have been measured
        if not samples:
            raise ProcedureFailedException(
                f'Procedure measure_delay_matrix_for_path failed for cell {cell.name} '
                f'on path {path}: no measurement succeeded')
        for name, matrix in samples.items():
            missing = {point for point in ((l, s) for l in loads for s in slews)} - set(matrix)
            if missing:
                msg = f'Procedure measure_delay_matrix_for_path failed for cell {cell.name} ' \
                      f'on path {path}: no measurement of {name} at points {sorted(missing)}'
                raise ProcedureFailedException(msg)

    # Apply the selection criterion across conditions and build full LUTs
    result = cell.liberty
    timing_group = liberty.Group('timing')
    timing_group.add_attribute('related_pin', input_pin)
    timing_type = 'combinational_rise' if output_transition == '01' else 'combinational_fall'
    timing_group.add_attribute('timing_type', timing_type)
    lut_template = f'delay_template_{len(loads)}x{len(slews)}'
    for name, matrix in samples.items():
        lut_name, *_ = name.split('__')
        lut = LookupTable(lut_name, lut_template,
                          total_output_net_capacitance=list(loads),
                          input_net_transition=list(slews))
        for (load, slew), condition_values in matrix.items():
            value = criterion(condition_values) @ PySpice.Unit.u_s
            lut[load, slew] = value.convert(settings.units.time.prefixed_unit).value
        timing_group.add_group(lut)
    result.group('pin', output_pin).add_group(timing_group)

    return result
