"""Liberty rendering of latches from the cell declaration (no SPICE):
the enable role gives a latch group, a dominant declared in the gated
form "<gate> & <pin>" is synchronous, a gated SR latch (no data) is
spelled as a D-latch transparent while the gate and a dominant are up."""
import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from charlib.characterizer.cell import Cell

SUPPLIES = {'VDD': 'primary_power', 'VSS': 'primary_ground'}


def _cell(subckt, **config):
    with tempfile.TemporaryDirectory() as tmp:
        netlist = Path(tmp) / 'cell.spice'
        netlist.write_text(f'.subckt {subckt}\n.ends\n')
        return Cell(subckt.split()[0], SUPPLIES, netlist=str(netlist), **config)


class TestIllegalStates(unittest.TestCase):
    def test_illegal_is_parsed_against_the_pins(self):
        cell = _cell('srl LATCH DP DN VDD VSS QP QN',
                     functions=['QP <= DS0000', 'QN <= DS0001'],
                     state=['DS0000 = DP & !DN', 'DS0001 = !DP & DN'],
                     enable='LATCH', set='LATCH & DP', reset='LATCH & DN',
                     pairs=['QP QN'], illegal=['LATCH & DP & DN'])
        self.assertEqual(cell.illegal, [{'LATCH': True, 'DP': True, 'DN': True}])
        with self.assertRaises(ValueError):
            _cell('srl LATCH DP DN VDD VSS QP QN',
                  functions=['QP <= DS0000', 'QN <= DS0001'],
                  state=['DS0000 = DP & !DN', 'DS0001 = !DP & DN'],
                  enable='LATCH', set='LATCH & DP', reset='LATCH & DN',
                  pairs=['QP QN'], illegal=['LATCH & DX'])


class TestClearPresetVars(unittest.TestCase):
    SR = dict(functions=['QP <= DS0000', 'QN <= DS0001'],
              state=['DS0000 = DP & !DN', 'DS0001 = !DP & DN'],
              enable='LATCH', set='LATCH & DP', reset='LATCH & DN', pairs=['QP QN'])

    def test_illegal_both_asserted_state_is_x(self):
        cell = _cell('srl LATCH DP DN VDD VSS QP QN', illegal=['LATCH & DP & DN'], **self.SR)
        text = cell.liberty.to_liberty()
        self.assertIn('clear_preset_var1 : X', text)
        self.assertIn('clear_preset_var2 : X', text)

    def test_illegal_elsewhere_does_not_match(self):
        cell = _cell('srl LATCH DP DN VDD VSS QP QN', illegal=['!LATCH & DP & DN'], **self.SR)
        self.assertIn('clear_preset_var1 : L', cell.liberty.to_liberty())

    def test_single_dominant_has_no_vars(self):
        cell = _cell('dlhr D GATE RESET_B VDD VSS Q',
                     functions=['Q <= DS0000'], state=['DS0000 = D'],
                     enable='GATE', reset='not RESET_B')
        self.assertNotIn('clear_preset_var', cell.liberty.to_liberty())

    def test_ff_with_both_dominants_takes_the_declared_vars(self):
        cell = _cell('sdfbbp CLK D Q Q_N RESET_B SET_B VDD VSS',
                     functions=['Q <= DS0000', 'Q_N <= DS0001'],
                     state=['DS0000 = D', 'DS0001 = !D'], pairs=['Q Q_N'],
                     clock='posedge CLK', reset='not RESET_B', set='not SET_B',
                     clear_preset_var=['H', 'L'])
        text = cell.liberty.to_liberty()
        self.assertIn('ff (DS0000, DS0001)', text)
        self.assertIn('clear_preset_var1 : H', text)
        self.assertIn('clear_preset_var2 : L', text)
        with self.assertRaises(ValueError):
            _cell('sdfbbp CLK D Q Q_N RESET_B SET_B VDD VSS',
                  functions=['Q <= DS0000'], state=['DS0000 = D'],
                  clock='posedge CLK', reset='not RESET_B', set='not SET_B',
                  clear_preset_var=['H'])

    def test_data_latch_with_both_dominants_is_not_derivable(self):
        cell = _cell('dlsr D GATE S R VDD VSS Q',
                     functions=['Q <= DS0000'], state=['DS0000 = D'],
                     enable='GATE', set='S', reset='R')
        text = cell.liberty.to_liberty()
        self.assertIn('preset : S', text)
        self.assertNotIn('clear_preset_var', text)


class TestLatchRendering(unittest.TestCase):
    def test_d_latch_is_a_latch_group_with_a_clock_enable(self):
        cell = _cell('dlh D GATE VDD VSS Q',
                     functions=['Q <= DS0000'], state=['DS0000 = D'], enable='GATE')
        text = cell.liberty.to_liberty()
        self.assertIn('latch (DS0000, DS0000inv)', text)
        self.assertIn('data_in : D', text)
        self.assertIn('enable : GATE', text)
        self.assertNotIn('ff (', text)
        gate = cell.liberty.group('pin', 'GATE').attributes
        self.assertEqual(gate['clock'].value, 'true')
        self.assertNotIn('clock', cell.liberty.group('pin', 'D').attributes)

    def test_async_dominant_stays_a_bare_pin(self):
        cell = _cell('dlhr D GATE RESET_B VDD VSS Q',
                     functions=['Q <= DS0000'], state=['DS0000 = D'],
                     enable='GATE', reset='not RESET_B')
        text = cell.liberty.to_liberty()
        self.assertIn('clear : "RESET_B\'"', text)
        self.assertIn('enable : GATE', text)
        self.assertEqual(cell.role_gates, {})

    def test_gated_dominant_on_a_data_latch(self):
        cell = _cell('dlhs D GATE S VDD VSS Q',
                     functions=['Q <= DS0000'], state=['DS0000 = D'],
                     enable='GATE', set='GATE & S')
        text = cell.liberty.to_liberty()
        self.assertIn('data_in : D', text)
        self.assertIn('enable : GATE', text)
        self.assertIn('preset : "GATE & S"', text)
        self.assertEqual(cell.role_gates, {'set': ('GATE', False)})
        self.assertEqual(cell.preset.name, 'S')

    def test_gated_sr_latch_is_a_transparent_d_latch(self):
        cell = _cell('tlatch9 LATCH DP DN QP QN VDD VSS',
                     functions=['QP <= DS0000', 'QN <= DS0001'],
                     state=['DS0000 = DP & !DN', 'DS0001 = !DP & DN'],
                     enable='LATCH', set='LATCH & DP', reset='LATCH & DN',
                     pairs=['QP QN'], inputs=[], outputs=['QP', 'QN'])
        text = cell.liberty.to_liberty()
        self.assertIn('latch (DS0000, DS0001)', text)
        self.assertIn('data_in : "DP & !DN"', text)
        self.assertIn('enable : "LATCH & (DP | DN)"', text)
        # hybrid form (17/10) : the dominants also as group attributes,
        # which gives the both-asserted state its liberty place
        self.assertIn('preset : "LATCH & DP"', text)
        self.assertIn('clear : "LATCH & DN"', text)
        self.assertIn('clear_preset_var1 : L', text)
        self.assertIn('clear_preset_var2 : L', text)
        self.assertEqual(cell.liberty.group('pin', 'LATCH').attributes['clock'].value, 'true')
        self.assertEqual(cell.inputs, [])
        # the roles still name the dominants (timing_sense of the assert arcs)
        self.assertEqual((cell.preset.name, cell.clear.name), ('DP', 'DN'))

    def test_gated_sr_latch_state_function_evaluates(self):
        cell = _cell('tlatch9 LATCH DP DN QP QN VDD VSS',
                     functions=['QP <= DS0000', 'QN <= DS0001'],
                     state=['DS0000 = DP & !DN', 'DS0001 = !DP & DN'],
                     enable='LATCH', set='LATCH & DP', reset='LATCH & DN',
                     pairs=['QP QN'])
        qp, qn = cell.functions['QP'], cell.functions['QN']
        self.assertEqual(qp.eval(LATCH=1, DP=1, DN=0, DS0000=0), 1)
        self.assertEqual(qp.eval(LATCH=1, DP=0, DN=1, DS0000=1), 0)
        self.assertEqual(qp.eval(LATCH=0, DP=0, DN=0, DS0000=1), 1)   # hold
        self.assertEqual(qn.eval(LATCH=1, DP=1, DN=0, DS0001=1), 0)
        self.assertEqual(qn.eval(LATCH=1, DP=0, DN=1, DS0001=0), 1)

    def test_malformed_gate_is_loud(self):
        with self.assertRaises(ValueError):
            _cell('x D GATE S VDD VSS Q', functions=['Q <= DS0000'], state=['DS0000 = D'],
                  enable='GATE', set='GATE & S & T')


if __name__ == '__main__':
    unittest.main()
