"""Interactive ngspice session plumbing shared by matrix procedures."""

import re

from charlib.characterizer.procedures import ProcedureFailedException


def fmt(value):
    """Render a quantity as a plain SI float for an interactive ngspice command."""
    return f'{float(value):.12e}'


def pulse_alter(source, v_1, v_2, t_delay, t_ramp, t_width, t_period):
    """Build the command altering a PULSE source to the given parameters."""
    return (f'alter @{source}[pulse] = [ {fmt(v_1)} {fmt(v_2)} {fmt(t_delay)} '
            f'{fmt(t_ramp)} {fmt(t_ramp)} {fmt(t_width)} {fmt(t_period)} ]')


class Session:
    """One loaded circuit in an interactive ngspice-shared session.

    Wraps the backend's shared instance with incremental command logging
    (a killed run keeps the evidence of what it was doing) and pins
    ngspice to a single OpenMP thread: parallelism lives at the process
    level (one worker per core), and the active-wait barriers of several
    concurrent multi-threaded ngspice instances slow a cell-sized tran
    by orders of magnitude — OMP_NUM_THREADS is overridden by ngspice,
    so it must be set in-session.
    """

    def __init__(self, simulator, simulation, settings, log_path=None):
        self._shared = getattr(simulator, 'ngspice', None)
        if self._shared is None:
            raise ProcedureFailedException(
                f'Backend {settings.simulation.backend} does not expose an interactive '
                'ngspice session; matrix procedures require ngspice-shared')
        self._log = open(log_path, 'w', encoding='utf-8') if log_path else None
        self._shared.destroy()
        self._shared.load_circuit(str(simulation))
        self.execute('set num_threads = 1')

    def execute(self, command):
        """Run one interactive command, logging it and its output."""
        if self._log:
            self._log.write(command + '\n')
            self._log.flush()
        output = self._shared.exec_command(command)
        if self._log and output:
            self._log.write(''.join(f'* {line}\n' for line in output.splitlines()))
            self._log.flush()
        return output

    def measure(self, name, command):
        """Run a meas command; return the measured value in SI units, or None."""
        try:
            output = self.execute(command)
        except Exception:
            return None
        match = re.search(rf'{name}\s*=\s*([\-+0-9.eE]+)', output)
        return float(match.group(1)) if match else None

    def close(self):
        if self._log:
            self._log.close()
