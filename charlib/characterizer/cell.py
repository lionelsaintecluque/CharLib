"""Encapsulates a cell to be tested."""

import itertools, os, re
from pathlib import Path

from charlib.characterizer.procedures import registered_procedures

from charlib.characterizer.logic.evaluators import OPERAND_REGEX
from charlib.characterizer.logic.functions import Function
from charlib.characterizer.port import Port, Pin, DifferentialPair
from charlib.characterizer.logic.Parser import parse_logic
from charlib.liberty import liberty

def parse_conjunction(expr, field):
    """"LATCH & DP & !DN" -> {'LATCH': True, 'DP': True, 'DN': False}"""
    out = {}
    for literal in expr.split('&'):
        match literal.replace('!', 'not ').split():
            case ['not', pin]:
                out[pin] = False
            case [pin]:
                out[pin] = True
            case _:
                raise ValueError(f'Invalid {field} declaration "{expr}": a '
                                 'conjunction of pin literals, "LATCH & DP & !DN"')
    return out


class Cell:
    """A standard cell and its functional details"""

    def __init__(self, name: str, supply_nodes: dict,  **cell_config):
        """Construct a new cell from the given configuration

        :param name: The cell name as it appears in the spice netlist.
        :param named_nodes: Supply node name: role maps from the library configuration.
        :param cell_config: The dict of properties provided in the configuration YAML.

        The cell initialization process is as follows:
        1. Process cell_config to get the following information about the cell:
            - The path to the cell's netlist.
            - Functions implemented in this cell (and optionally input and output pin names).
            - Which pins have special roles or are members of differential pairs.
            - The functions of any internal state elements and/or feedback paths.
            - Cell metadata that belongs in the liberty file (such as area)
        2. Locate the cell netlist and read the .subckt line. For each pin in the netlist:
            a. Determine pin direction from functions and/or states.
            b. Determine pin role and trigger type from special pins (default: logic/level-sensing)
            c. Determine how to construct the pin and bind it to the cell.
                - If the pin is a member of a differential pair, construct with DifferentialPair.
                - Otherwise, construct with Pin.
        3. For each output pin, construct the corresponding Function and bind it to the cell.
        4. Construct a skeleton liberty object for this cell.
        """
        self.name = name
        self.pins = {}
        self.diff_pairs = {}
        self.functions = {}

        ## 1. Process cell_config

        # Validate netlist
        netlist = cell_config['netlist']
        if isinstance(netlist, str):
            # ~ and $VAR expand as the shell would : a tracked yml may
            # spell the PDK root as $PDK_ROOT, like its decks (ngspice
            # expands it too)
            netlist = Path(os.path.expandvars(netlist)).expanduser()
        if isinstance(netlist, Path):
            if not netlist.is_file():
                raise ValueError(f'Invalid value for netlist: "{netlist}" is not a file')
            self.netlist = Path(netlist)
        else:
            raise TypeError(f'Invalid type for netlist: {type(netlist)}')

        # Map supplies and special pins to (trigger, inverted, role, declared
        # name) tuples. Keys are uppercased to match the case-normalized
        # subckt line; the declared spelling is what the pin keeps.
        special_pins = {p.upper(): (Port.Trigger.LEVEL, False, role, p)
                        for p, role in supply_nodes.items()}
        # role -> (gate pin name, gate inverted) for a set/reset declared
        # in the gated form "LATCH & DP" : the dominant acts only while
        # the gate is up (a synchronous dominant ; the gate is the latch
        # enable). A bare pin stays an asynchronous dominant.
        self.role_gates = {}
        for role in ['clock', 'set', 'reset', 'enable']:
            if role in cell_config:
                tokens = cell_config[role].replace('!', 'not ').split()
                if '&' in tokens:
                    cut = tokens.index('&')
                    gate_tokens, tokens = tokens[:cut], tokens[cut + 1:]
                    match gate_tokens:
                        case ['not', gate]:
                            self.role_gates[role] = (gate, True)
                        case [gate]:
                            self.role_gates[role] = (gate, False)
                        case _:
                            raise ValueError(
                                f'Invalid {role} declaration "{cell_config[role]}": '
                                'the gated form is "<gate> & <pin>" (either may carry not)')
                match tokens:
                    case ['posedge', pin]:
                        special_pins[pin.upper()] = (Port.Trigger.EDGE, False, role, pin)
                    case ['negedge', pin]:
                        special_pins[pin.upper()] = (Port.Trigger.EDGE, True, role, pin)
                    case ['not', pin]:
                        special_pins[pin.upper()] = (Port.Trigger.LEVEL, True, role, pin)
                    case [pin]:
                        special_pins[pin.upper()] = (Port.Trigger.LEVEL, False, role, pin)

        # usage-illegal states : the ARCHITECTURE's rule, not a truth of
        # the cell (`illegal: ["LATCH & DP & DN"]`, conjunctions of pin
        # literals). The measurement db keeps what was measured there ;
        # the liberty consumer leaves those entries out of its worst-case
        # folds (measurements.illegal_excluder).
        self.illegal = [parse_conjunction(expr, 'illegal')
                        for expr in cell_config.get('illegal', [])]
        # the both-dominants state as the cell's truth table spells it
        # (`clear_preset_var: [H, L]`), when the state functions cannot
        # tell (an ff's asynchronous dominants are not in next_state)
        declared = [str(v).upper() for v in cell_config.get('clear_preset_var', [])]
        if declared and (len(declared) != 2 or not set(declared) <= set('LHNTX')):
            raise ValueError(f'Invalid clear_preset_var {cell_config["clear_preset_var"]}: '
                             'two of L, H, N, T, X')
        self.clear_preset_var = tuple(declared) or None

        # Identify diff pairs
        diff_pairs = [tuple(pair.split()) for pair in cell_config.get('pairs', [])]

        # Parse functions to determine input and output mapping
        def _parse_expression(expression):
            # Helper function for parsing expressions of the form Y=A&B etc.
            lhs, rhs = expression.split('=')
            output = ''.join([c for c in lhs if c.isalnum() or c == '_'])
            if not parse_logic(rhs):
                raise ValueError(f'Could not parse expression "{expression}"')
            return output, rhs.strip()

        functions = {}
        states = {}
        inputs = set()
        outputs = set()
        parsed_states = {}
        if 'state' in cell_config:
            for state in cell_config['state']:
                internal_pin, state_expr = _parse_expression(state)
                parsed_states[internal_pin] = state_expr
        for function in cell_config['functions']:
            output, func_expr = _parse_expression(function)
            if func_expr in parsed_states:
                # TODO: if output not in states, indicate problem with cell config
                states[output] = func_expr # Store internal mapping
                func_expr = parsed_states[func_expr] # Read through function
            functions[output] = func_expr
            inputs.update(set(OPERAND_REGEX.findall(func_expr)))
            outputs.add(output)

        ## 2. Read .subckt line from netlist and construct pins

        # Helper function for direction matching with minimal membership checking
        def match_direction(pin_name):
            match (pin_name in inputs, pin_name in outputs):
                case True, True:  return 'inout'
                case False, True: return 'output'
                case True, False: return 'input'
            raise ValueError(f'Unable to determine direction for pin "{pin_name}"')

        # Get pin names from subckt and iterate until there are no unassigned
        # pins remaining. The subckt line is case-normalized (spice is case-
        # insensitive); every pin is mapped back to its declared spelling.
        case_map = {name.upper(): name for name in [*inputs, *outputs]}
        for pair in diff_pairs:
            case_map.update({name.upper(): name for name in pair})
        unassigned_pins = self.subckt().split()[2:]
        while unassigned_pins:
            pin = unassigned_pins.pop(0)
            if pin in special_pins:
                # This pin has a special (i.e. non-logic) role
                trigger_type, inverted, role, declared = special_pins[pin]
                self.pins[declared] = Pin(declared, 'input', role, inverted, trigger_type)
            elif pin in case_map:
                pin = case_map[pin]
                if any([pin in pair for pair in diff_pairs]):
                    # This pin is a member of a differential pair; find and build the pair
                    [pair] = [p for p in diff_pairs if pin in p]
                    (noninv_pin, inv_pin) = pair
                    self.diff_pairs[pair] = DifferentialPair(noninv_pin, inv_pin, match_direction(pin))
                    unassigned_pins.remove(pair[pair.index(pin)-1].upper())
                else:
                    # This is a standard logic pin
                    self.pins[pin] = Pin(pin, match_direction(pin))
            else:
                raise ValueError(f'Unable to determine direction for pin "{pin}": '
                                 'not among the declared inputs, outputs, pairs or '
                                 'special pins (names are matched case-insensitively '
                                 'against the netlist)')

        # Validate pin names if 'inputs' and/or 'outputs' keys are in cell_config
        # a pin that a function writes and another reads is inout (a
        # bidirectional pad : pad = c2p, p2c = pad) : it counts on both sides
        if 'inputs' in cell_config:
            if not set(cell_config['inputs']) <= set(self.inputs) | set(self.inouts):
                raise ValueError(f'Expected inputs {cell_config["inputs"]}, found {self.inputs} (inout {self.inouts})')
        if 'outputs' in cell_config:
            if not set(cell_config['outputs']) <= set(self.outputs) | set(self.inouts):
                raise ValueError(f'Expected outputs {cell_config["outputs"]}, found {self.outputs} (inout {self.inouts})')

        ## 3. Construct functions based on pin types & feedback paths
        for output, expression in functions.items():
            is_inverting = output in [pair.inverting_port_name for pair in self.diff_pairs.values()]
            state = states.get(output)
            # TODO: Pass only pins which are related to this function
            # TODO: Handle multiple clocks, etc.
            [output_pin] = list(self.filter_pins(name=output))
            self.functions[output] = Function(output_pin, expression, *self.all_pins(), state=state)

        ## 4. Add as much liberty data as we can right now
        self.liberty = liberty.Group('cell', name)
        known = {p.name for p in self.all_pins()}
        for rule in self.illegal:
            for pin in rule:
                if pin not in known:
                    raise ValueError(f'illegal state names pin {pin}, not a pin of '
                                     f'{self.name} ({sorted(known)})')
        self.liberty.add_attribute('area', cell_config.get('area', 0.0), 4)   # LEF sizes have 2 decimals, their product 4 (IHP writes 7.2576)
        def to_lib_expr(pin):
            return pin.name + "'" if pin.is_inverted() else pin.name
        inverting_pins = {pair.inverting_port_name for pair in self.diff_pairs.values()}
        for pin, var in states.items(): # Add storage groups
            # Get variable names
            if pin in inverting_pins:
                continue # skip inverting pins
            storage_vars = [var]
            for pair in self.diff_pairs.values():
                if pin == pair.noninverting_port_name:
                    try:
                        complement_var = states[pair.complement(pin)]
                    except KeyError as e:
                        raise ValueError(f'No state feedback path for differential pair member {pair.complement(pin)}') from e
                    storage_vars.append(complement_var)
                    break
            if len(storage_vars) < 2:
                # No inverting output. Append inv to the end of the first var name
                storage_vars.append(f'{var}inv')
            var_string = ', '.join(storage_vars)
            if self.clock: # ff group
                storage_group = liberty.Group('ff', var_string)
                storage_group.add_attribute('next_state', self.functions[pin].expression)
                storage_group.add_attribute('clocked_on', to_lib_expr(self.clock))
            else: # latch group
                storage_group = liberty.Group('latch', var_string)
                storage_group.add_attribute('data_in', self.functions[pin].expression)
                gated = self._gated_dominants()
                if gated and not self._data_operands(pin):
                    # A gated SR latch : the state is written by the
                    # dominants alone (no data operand). Standard liberty
                    # has no latch without data_in/enable (yosys refuses
                    # one), so it is spelled HYBRID (17/10) : a D-latch
                    # transparent while the gate and a dominant are up,
                    # the declared state function being its data, PLUS
                    # the dominants as preset/clear attributes, which
                    # gives clear_preset_var1/2 a place (below).
                    (gate, gate_inverted) = self.role_gates[gated[0].role]
                    gate_expr = f'!{gate}' if gate_inverted else gate
                    dominants = ' | '.join(to_lib_expr(d) for d in gated)
                    storage_group.add_attribute('enable', f'{gate_expr} & ({dominants})')
                elif self.enable:
                    storage_group.add_attribute('enable', to_lib_expr(self.enable))
            if self.clear:
                storage_group.add_attribute('clear', self._dominant_expr(self.clear))
            if self.preset:
                storage_group.add_attribute('preset', self._dominant_expr(self.preset))
            if self.clear and self.preset:
                both = self._clear_preset_vars(storage_vars, parsed_states)
                if both is not None:
                    storage_group.add_attribute('clear_preset_var1', both[0])
                    storage_group.add_attribute('clear_preset_var2', both[1])
            self.liberty.add_group(storage_group)
        for pin in self.all_pins(): # Add pin groups
            if pin.name in self.pg_pins:
                pin_group = liberty.Group('pg_pin', pin.name)
                pin_group.add_attribute('voltage_name', pin.name)
                pin_group.add_attribute('pg_type', pin.role)
            else:
                pin_group = liberty.Group('pin', pin.name)
                pin_group.add_attribute('direction', pin.direction)
                if pin.direction in (Port.Direction.OUT, Port.Direction.INOUT):
                    # an inout pin (a bidirectional pad) is an output with a function too (18/10)
                    # Bound the NLDM tables: without max_capacitance, STA tools
                    # extrapolate beyond the largest characterized load (wildly,
                    # for loads far outside the grid) and resizers see no max-cap
                    # violation to repair on overloaded nets.
                    loads = cell_config.get('loads')
                    if loads:
                        pin_group.add_attribute('max_capacitance', max(loads))
                    # liberty wants an expression, not an assignment: a
                    # sequential output references its storage variable,
                    # a combinational one its boolean expression
                    state_var = states.get(pin.name)
                    pin_group.add_attribute(
                        'function',
                        state_var if state_var else self.functions[pin.name].expression)
                elif pin.role == Port.Role.CLOCK \
                     or (pin.role == Port.Role.ENABLE and states):
                    # a latch enable is a clock pin for STA (time
                    # borrowing, setup/hold against it), as vendor libs
                    # spell it ; a tristate enable (no state) is not
                    pin_group.add_attribute('clock', "true")
            self.liberty.add_group(pin_group)

    def subckt(self) -> str:
        """Return the subckt line matching this cell"""
        subckt_pattern = re.compile(rf'^\s*\.subckt\s+{re.escape(self.name)}\b', re.IGNORECASE)
        with open(self.netlist, 'r') as file:
            for line in file:
                if subckt_pattern.match(line):
                    return line.upper()
            raise ValueError(f'Failed to identify a .subckt for cell {self.name} in netlist "{self.netlist}"')

    def all_pins(self):
        """Yield all pins, including those stored as members of differential pairs"""
        yield from self.pins.values()
        for pair in self.diff_pairs.values():
            yield from pair.as_pins()

    def pins_in_netlist_order(self):
        """Yield all pins in the order they appear in the netlist"""
        pins = {p.name: p for p in self.all_pins()}
        for pin_name in self.subckt().split()[2:]:
            yield pins[pin_name]

    def filter_pins(self, **attrs):
        """Return a collection of pins matching the given directions, roles, etc.

        :param attrs: Attributes (and values) to match for. Each argument may be a single value
                      or a list of values.
        """
        list_attrs = {a: v if isinstance(v, list) else [v,] for a, v in attrs.items()}
        for pin in self.all_pins():
            if any([getattr(pin, attr) not in value for attr, value in list_attrs.items()]):
                continue # skip if any attribute doesn't match
            yield pin

    @property
    def outputs(self) -> list:
        """Return a list of output logic pin names"""
        return [pin.name for pin in self.filter_pins(direction='output', role='logic')]

    @property
    def inputs(self) -> list:
        """Return a list of input logic pin names"""
        return [pin.name for pin in self.filter_pins(direction='input', role='logic')]

    @property
    def inouts(self) -> list:
        """Return a list of inout logic pin names"""
        return [pin.name for pin in self.filter_pins(direction='inout', role='logic')]

    @property
    def pg_pins(self) -> list:
        """Return a list of supply and bias pin names"""
        return [pin.name for pin in self.filter_pins(role=['primary_power', 'primary_ground', 'pwell', 'nwell'])]

    def _get_first_pin_with_role(self, role):
        """Return the first pin with the specified role. If there is no such pin, return None."""
        try:
            return next(self.filter_pins(role=role))
        except StopIteration:
            return None

    @property
    def enable(self):
        """Return the cell's enable pin, if present"""
        return self._get_first_pin_with_role('enable')

    @property
    def clock(self):
        """Return the cell's clock pin, if present"""
        return self._get_first_pin_with_role('clock')

    @property
    def preset(self):
        """Return the cell's set pin, if present"""
        return self._get_first_pin_with_role('set')

    @property
    def clear(self):
        """Return the cell's reset pin, if present"""
        return self._get_first_pin_with_role('reset')

    def _gated_dominants(self):
        """The set/reset pins declared in the gated form, in that order."""
        return [pin for pin in (self.preset, self.clear)
                if pin is not None and pin.role in self.role_gates]

    def _both_asserted(self):
        """Pin levels of the state where every dominant is asserted, its
        gate (when declared gated) up."""
        state = {}
        for dom in (self.preset, self.clear):
            state[dom.name] = not dom.is_inverted()
            if dom.role in self.role_gates:
                (gate, gate_inverted) = self.role_gates[dom.role]
                state[gate] = not gate_inverted
        return state

    def _clear_preset_vars(self, storage_vars, parsed_states):
        """liberty clear_preset_var1/var2, the storage variables while
        clear and preset are asserted together (17/10) :
        - X/X when that state is declared illegal : its exit is a race,
          the state is lost (Q=X, QN=!Q, and !X is X) ;
        - else the declared ``clear_preset_var`` (the truth table's own
          word, an ff with set and reset : sdfbbp H/L as the vendor) ;
        - else the state functions evaluated there, when the dominants
          (and gates) determine every operand : the gated SR latch,
          state DS0000 = DP & !DN -> L, DS0001 = !DP & DN -> L ;
        - else None : not derivable (an ff's asynchronous dominants are
          not in its next_state), nothing emitted."""
        from charlib.characterizer.logic.evaluators import BooleanEvaluator
        state = self._both_asserted()
        if any(all(state.get(p) == v for p, v in rule.items()) for rule in self.illegal):
            return ('X', 'X')
        if self.clear_preset_var:
            return self.clear_preset_var
        values = []
        for var in storage_vars:
            expr = parsed_states.get(var)
            if expr is None:            # the '<var>inv' complement
                if not values:
                    return None
                values.append('L' if values[0] == 'H' else 'H')
                continue
            ev = BooleanEvaluator(expr)
            if not set(ev.operands) <= set(state):
                return None
            values.append('H' if ev(**{p: state[p] for p in ev.operands}) else 'L')
        return tuple(values[:2])

    def _dominant_expr(self, pin):
        """Liberty expression of a set/reset pin : the bare pin (with its
        polarity) or, declared gated, 'gate & pin'."""
        expr = pin.name + "'" if pin.is_inverted() else pin.name
        if pin.role in self.role_gates:
            (gate, gate_inverted) = self.role_gates[pin.role]
            expr = f'{gate}{chr(39) if gate_inverted else ""} & {expr}'
        return expr

    def _data_operands(self, output):
        """The operands of an output's declared function that are neither
        a dominant nor the enable : empty for a state written by the
        dominants alone."""
        special = {pin.name for pin in (self.preset, self.clear, self.enable, self.clock)
                   if pin is not None}
        return [name for name in OPERAND_REGEX.findall(self.functions[output].expression)
                if name not in special]

    def paths(self):
        """Generator for input-to-output paths through a cell

        Yields lists in the format [input_name, input_transition, output_name, output_transition].
        All possible combinations of pin names and transitions are yielded, regardless of whether
        they are possible given this cell's functions.
        """
        # FIXME: Generate only paths which actually make sense given this cell's function
        transitions = ['01', '10']
        for path in itertools.product(self.inputs, transitions, self.outputs, transitions):
            yield path

    def nonmasking_conditions_for_path(self, input_pin, input_transition, output_pin,
                                       output_transition):
        """Find all mappings of pin states such that the desired state transitions occur.

        Returns a generator yielding dictionaries of states for which input_pin and output_pin
        undergo input_transition and output_transition respectively.

        :param input_pin: The name of the input pin of interest.
        :param input_transition: The desired state transition for the input pin. Must be '01' or
                                 '10'.
        :param output_pin: The name of the output pin of interest.
        :param output_transition: The desired state transition for the output pin. Must be '01' or
                                  '10'.
        """
        for tv in self.functions[output_pin].test_vectors:
            if tv.get(input_pin) == input_transition and tv[output_pin] == output_transition:
                yield tv

    @property
    def is_sequential(self) -> bool:
        return any([f.state is not None for f in self.functions.values()])


class CellTestConfig:
    """Capture configuration information for testing one or more cells"""

    def __init__(self, models: list, plots=[], timestep=None, measurements=None, **parameters):
        """Construct a new test configuration.

        :param models: Transistor models for the cell under test
        :param plots: A list of plot types to generate from simulation results. Defaults to None.
        :param timestep: The simulation timestep to use for transient simulations, specified in
                         settings.units.time units. Defaults to 1/8 the minimum data_slew or
                         clock_slew if not provided.
        :param **parameters: Keyword arguments containing lists of test parameters, as described
                             below:
            :param data_slews: A list of input data slew rates to test, specified in
                               settings.units.time units.
            :param clock_slews: A list of clock slew rates to test specified in settings.units.time
                               units
            :param loads: A list of output load capacitances to test, specified in
                          settings.units.capacitance units
        """
        self.models = list()
        for model in models:
            # Split to path and (optional) section, then validate both
            filename, *libname = model.split()
            filename = Path(os.path.expandvars(filename)).expanduser()
            if not filename.exists():
                raise ValueError(f'Unable to locate model at "{filename}" '
                                 '(a $VAR in the path must be exported)')
            if len(libname) > 1:
                raise ValueError(f'Expected 1 libname in model "{model}", got {len(libname)}:' \
                                 f'{libname}')
            elif not len(libname) == 1:
                libname = []
            self.models.append((filename, *libname))

        self.timestep = timestep
        self.plots = plots
        self.measurements = measurements
        supported_parameters = {param for rp in registered_procedures.values() for param in rp['parameters']}
        self.parameters = {k: parameters[k] for k in supported_parameters if k in parameters}

    def variations(self, *keys):
        """Generator for test configuration variations

        Yields dictionaries containing key-value pairs for a single combination of parameters. For
        example: {data_slews: 0.01, loads: 0.025}

        :param *keys: If provided, only return variations of provided parameter names.
        """
        parameters = self.parameters if not keys else {k: self.parameters[k] for k in keys}
        param_names, values = zip(*parameters.items())
        values = [v if isinstance(v, list) else [v] for v in values]
        for combination in itertools.product(*values):
            yield dict(zip(param_names, combination))
