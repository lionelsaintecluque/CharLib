"""Removal constraint — the release side of the machinery in recovery.py."""

from charlib.characterizer.procedures import register
from charlib.characterizer.procedures.sequential.constraint.recovery import (
    measure_release_matrix)

@register('data_slews', 'clock_slews', 'metastability_constraint_load',
          'setup_pushout_criterion', 'hold_disturbance_depth',
          'qualification_frequency', 'qualification_duty_cycle')
def removal_constraint(cell, config, settings):
    """Find the mimimum time a control pin must remain active after the trigger.

    This is analagous to hold time for an asynchronous control pin.
    For example, for a rising-edge DFF with an asynchronous reset, removal time is the minimum time
    that the reset signal must remain active after the rising clock edge in order to reset the
    device state."""
    if cell.clear or cell.preset:
        yield (measure_release_matrix, cell, config, settings, 'removal')
