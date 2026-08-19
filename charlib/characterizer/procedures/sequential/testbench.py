"""Shared test circuit for level-sensitive sequential matrix procedures."""

from charlib.characterizer import utils
from charlib.characterizer.cell import Port
from charlib.characterizer.procedures import ProcedureFailedException


def transparent_high(gate):
    """A ``clock``-declared gate names its CLOSING edge (negedge closes
    falling, so transparency is high); an ``enable``-declared gate names
    its ACTIVE level (inverted means transparent low)."""
    if gate.trigger == Port.Trigger.EDGE:
        return gate.inversion
    return not gate.inversion


def single_data_gate_output(cell):
    """Return (data, gate pin, output) for a single-data level-sensitive cell.

    The gate is the pin declared with the ``clock`` (or ``enable``) key in the
    cell config; the declared edge is the CLOSING edge.
    """
    gate = cell.clock or cell.enable
    if gate is None:
        raise ProcedureFailedException(
            f'Cell {cell.name} declares no clock or enable pin; sequential '
            'characterization needs the gate declared (closing edge) in the cell config')
    if len(cell.inputs) != 1 or len(cell.outputs) != 1:
        raise ProcedureFailedException(
            f'Cell {cell.name}: only single-data-input, single-output level-sensitive '
            f'cells are supported for now (found inputs {cell.inputs}, outputs {cell.outputs})')
    return cell.inputs[0], gate, cell.outputs[0]


def latch_circuit(cell, config, settings, data, gate, out, initial_load, period):
    """Build the one test circuit: PULSE sources on data and gate (parameters
    swapped by `alter` at every point), the load capacitor on the output."""
    vss = settings.primary_ground.voltage * settings.units.voltage
    circuit = utils.init_circuit('seq_matrix', cell.netlist, config.models,
                                 settings.named_nodes, settings.units)
    connections = []
    for pin in cell.pins_in_netlist_order():
        if pin.name in (data, gate.name):
            connections.append(f'v{pin.name}')
            circuit.PulseVoltageSource(pin.name, f'v{pin.name}', circuit.gnd,
                                       initial_value=vss, pulsed_value=vss,
                                       pulse_width=1e-6, period=period)
        elif pin.name == out:
            connections.append(f'v{pin.name}')
            circuit.C(pin.name, f'v{pin.name}', circuit.gnd, initial_load)
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
