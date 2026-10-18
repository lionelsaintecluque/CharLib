"""Round-trip tests for the measurement DB parser/writer (no simulator)."""
import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from charlib.measurements import MeasurementDB, excluder, parse_cond, collect_caps, cap_fold


SAMPLE = """\
# corner TT 1.2V 25C
set DB(CLK:-:min_pulse_width:rise_constraint,0.0186,-) 1.0058E-10
set DB(D:-:capacitance:rise,0.0186,2) 1.72032E-15
set DB(D:CLK:hold_rising:rise_constraint,0.0186,0.0186) -5.48E-11
set DB(D:CLK:setup_rising:fall_constraint,0.0186,0.0186) 1.126E-10
set DB(Q:CLK:rising_edge:cell_rise,0.0186,0.001) 1.81096E-10
"""


class TestMeasurementDB(unittest.TestCase):
    def _load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cell_db.tcl'
            path.write_text(text)
            return MeasurementDB.load(path)

    def test_parse(self):
        db = self._load(SAMPLE)
        self.assertEqual(len(db.entries), 5)
        self.assertEqual(db.header, ['# corner TT 1.2V 25C'])
        self.assertAlmostEqual(
            db.entries[('D', 'CLK', 'hold_rising', 'rise_constraint', '',
                        ('0.0186', '0.0186'))], -5.48e-11)
        self.assertIn(('CLK', '-', 'min_pulse_width', 'rise_constraint', '',
                       ('0.0186', '-')), db.entries)

    def test_roundtrip_sorted_and_stable(self):
        db = self._load(SAMPLE)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'out_db.tcl'
            db.save(path)
            text1 = path.read_text()
            MeasurementDB.load(path).save(path)
            self.assertEqual(text1, path.read_text())
            lines = [l for l in text1.splitlines() if not l.startswith('#')]
            self.assertEqual(lines, sorted(lines))

    def test_missing_key_means_not_measured(self):
        db = self._load(SAMPLE)
        self.assertNotIn(('Q', 'CLK', 'rising_edge', 'cell_fall', '',
                          ('0.0186', '0.001')), db.entries)

    def test_power_keys_parse(self):
        # vernier branch power (18/10) : energy per output edge, leakage per state
        db = self._load("set DB(Y:A:internal_power:rise_power:B1,0.0186,0.001) 9.4E-16\n"
                        "set DB(Y:-:leakage_power:value:A1B1,-,-) 1.62E-10\n")
        self.assertIn(('Y', 'A', 'internal_power', 'rise_power', 'B1', ('0.0186', '0.001')), db.entries)
        self.assertIn(('Y', '-', 'leakage_power', 'value', 'A1B1', ('-', '-')), db.entries)
        self.assertAlmostEqual(db.entries[('Y', '-', 'leakage_power', 'value', 'A1B1', ('-', '-'))], 1.62e-10)

    def test_malformed_entry_is_loud(self):
        with self.assertRaises(ValueError):
            self._load('set DB(oops) 1.0\n')
        with self.assertRaises(ValueError):
            self._load('this is not an entry\n')

    def test_tables_grouping(self):
        db = self._load(SAMPLE)
        tables = db.tables()
        self.assertIn(('Q', 'CLK', 'rising_edge', 'cell_rise'), tables)
        self.assertEqual(tables[('D', 'CLK', 'setup_rising', 'fall_constraint')],
                         {('0.0186', '0.0186'): 1.126e-10})

    def test_set_and_merge(self):
        db = MeasurementDB()
        db.set('Q', 'CLK', 'rising_edge', 'cell_rise', (0.0186, 0.001), 1.8e-10)
        other = self._load(SAMPLE)
        other.merge(db)
        self.assertAlmostEqual(
            other.entries[('Q', 'CLK', 'rising_edge', 'cell_rise', '',
                           ('0.0186', '0.001'))], 1.8e-10)


class TestConditioningField(unittest.TestCase):
    def _load(self, text):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'cell_db.tcl'
            path.write_text(text)
            return MeasurementDB.load(path)

    def test_fold_worst_case(self):
        db = self._load(
            'set DB(CLK:-:min_pulse_width:rise_constraint:D0,0.0186,-) 1.0E-10\n'
            'set DB(CLK:-:min_pulse_width:rise_constraint:D1,0.0186,-) 1.3E-10\n')
        table = db.tables()[('CLK', '-', 'min_pulse_width', 'rise_constraint')]
        self.assertAlmostEqual(table[('0.0186', '-')], 1.3e-10)

    def test_fold_leaves_illegal_states_out(self):
        db = self._load(
            'set DB(LATCH:-:capacitance:rise:DP0DN0,2.5,-) 4.7E-15\n'
            'set DB(LATCH:-:capacitance:rise:DP1DN1,2.5,-) 6.1E-15\n'
            'set DB(DP:-:capacitance:rise:LATCH1DN1,2.5,-) 3.2E-15\n'
            'set DB(DP:-:capacitance:rise:LATCH0DN1,2.5,-) 2.5E-15\n'
            'set DB(LATCH:-:min_pulse_width:rise_constraint:DP1,0.0186,-) 7.7E-11\n'
            'set DB(LATCH:-:min_pulse_width:rise_constraint:DN1,0.0186,-) 7.6E-11\n'
            'set DB(QP:DN:combinational:cell_fall:LATCH1DP1,0.0186,0.001) 1.0E-10\n')
        excluded = excluder(['LATCH', 'DP', 'DN', 'QP', 'QN'],
                            [{'LATCH': True, 'DP': True, 'DN': True}])
        tables = db.tables(excluded)
        # the illegal state leaves the fold of the enable's and the dominants' caps
        self.assertAlmostEqual(tables[('LATCH', '-', 'capacitance', 'rise')][('2.5', '-')], 4.7e-15)
        self.assertAlmostEqual(tables[('DP', '-', 'capacitance', 'rise')][('2.5', '-')], 2.5e-15)
        # a rule naming an undetermined pin does not match : mpw stays worst-case
        self.assertAlmostEqual(
            tables[('LATCH', '-', 'min_pulse_width', 'rise_constraint')][('0.0186', '-')], 7.7e-11)
        # the only entry of an arc is never dropped, illegal or not
        self.assertAlmostEqual(
            tables[('QP', 'DN', 'combinational', 'cell_fall')][('0.0186', '0.001')], 1.0e-10)
        # no excluder : the former worst-case fold
        self.assertAlmostEqual(db.tables()[('LATCH', '-', 'capacitance', 'rise')][('2.5', '-')], 6.1e-15)

    def test_caps_are_collected_unfolded_and_folded_as_mean_and_range(self):
        db = self._load(
            'set DB(A:-:capacitance:rise:B0,2.5,-) 2.25E-15\n'
            'set DB(A:-:capacitance:rise:B1,2.5,-) 3.75E-15\n'
            'set DB(A:-:capacitance:rise:B1,0.5,-) 3.74E-15\n'
            'set DB(LATCH:-:capacitance:rise:DP0DN0,2.5,-) 4.7E-15\n'
            'set DB(LATCH:-:capacitance:rise:DP1DN1,2.5,-) 6.1E-15\n')
        caps = collect_caps(db)
        self.assertEqual(sorted(caps['A']['rise']), [2.25e-15, 3.74e-15, 3.75e-15])
        mean, lo, hi = cap_fold(caps['A']['rise'])
        self.assertAlmostEqual(mean, (2.25e-15 + 3.74e-15 + 3.75e-15) / 3)
        self.assertEqual((lo, hi), (2.25e-15, 3.75e-15))
        # the illegal state leaves the collection (a legal sibling exists)
        ex = excluder(['LATCH', 'DP', 'DN', 'A', 'B'], [{'LATCH': True, 'DP': True, 'DN': True}])
        self.assertEqual(collect_caps(db, ex)['LATCH']['rise'], [4.7e-15])
        # ... but never empties it
        ex_all = excluder(['LATCH', 'DP', 'DN', 'A', 'B'], [{'B': False}, {'B': True}])
        self.assertEqual(len(collect_caps(db, ex_all)['A']['rise']), 3)

    def test_parse_cond_longest_pin_first(self):
        self.assertEqual(parse_cond('D0RESET_B1', ['D', 'RESET_B', 'B', 'GATE']),
                         {'D': False, 'RESET_B': True})
        with self.assertRaises(ValueError):
            parse_cond('X1', ['D'])

    def test_cond_roundtrip(self):
        import tempfile
        db = self._load(
            'set DB(Q:GATE:combinational:cell_rise:GATE1,0.0186,0.001) 2.0E-10\n')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'out.tcl'
            db.save(path)
            self.assertIn(':GATE1,0.0186,0.001', path.read_text())
            self.assertEqual(MeasurementDB.load(path).entries, db.entries)

    def test_cond_refused_on_constraints(self):
        with self.assertRaises(ValueError):
            self._load('set DB(D:CLK:setup_rising:rise_constraint:GATE1,0.0186,0.0186) 1E-10\n')

    def test_mpw_shape_enforced(self):
        with self.assertRaises(ValueError):
            self._load('set DB(GATE:GATE:min_pulse_width:rise_constraint:D0,0.0186,0.001) 1E-10\n')


if __name__ == '__main__':
    unittest.main()
