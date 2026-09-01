"""Unit test for the c2q Liberty arc emission (no SPICE needed)."""
import math

from charlib.liberty import liberty
from charlib.characterizer.procedures.sequential.constraint.metastability.c2q_contour import add_c2q_arc


class _FakeClock:
    name = 'CLK'

    def is_inverted(self):
        return False


def _make_cell_group():
    cell_group = liberty.Group('cell', 'dut')
    cell_group.add_group('pin', 'Q')
    return cell_group


def test_add_c2q_arc_emits_delay_and_transition():
    cell_group = _make_cell_group()
    add_c2q_arc(cell_group, _FakeClock(), 'Q', '01',
                n_cs=2, clock_slew=0.1, load=0.1, delay=0.42, transition=0.05)
    text = cell_group.to_liberty(precision=6)
    assert 'timing_type : rising_edge' in text
    assert 'cell_rise' in text
    assert 'rise_transition' in text
    assert '0.42' in text


def test_add_c2q_arc_skips_nan_transition():
    cell_group = _make_cell_group()
    add_c2q_arc(cell_group, _FakeClock(), 'Q', '10',
                n_cs=2, clock_slew=0.1, load=0.1, delay=0.33, transition=float('nan'))
    text = cell_group.to_liberty(precision=6)
    assert 'cell_fall' in text
    assert 'fall_transition' not in text
