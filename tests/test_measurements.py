"""Round-trip tests for the measurement DB parser/writer (no simulator)."""
import sys, tempfile, unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from charlib.measurements import MeasurementDB


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
