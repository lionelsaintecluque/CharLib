"""Per-cell measurement DB: CharLib's measurement interchange format.

One plain Tcl-array file per cell per corner, loadable with ``source``,
greppable, diffable. The file is an interface: any tool can produce it,
CharLib can consume it (and dumps its own measurements in the same
format, which doubles as a crash-proof checkpoint).

Entry::

    set DB(<pin>:<related>:<timing_type>:<lut>,<axis1>[,<axis2>]) <value>

- ``pin``: the measured/constrained pin (liberty pin name)
- ``related``: the reference pin, ``-`` when there is none (mpw, caps)
- ``timing_type``: liberty timing_type (``rising_edge``,
  ``falling_edge``, ``setup_rising``, ``hold_rising``,
  ``recovery_rising``, ``removal_rising``, ``min_pulse_width``, and
  their falling counterparts; ``clear``/``preset`` for asynchronous
  dominants) or the quantity family (``capacitance``)
- ``lut``: the liberty table (``cell_rise``, ``rise_transition``,
  ``rise_constraint``, ...) or the direction (``rise``/``fall`` for
  capacitance)
- axes: coordinates in lib units, self-enumerating. Per family:
  delays/transitions (input slew, output load); constraints
  (related-pin slew, constrained-pin slew); min_pulse_width
  (slew, ``-``); capacitance (slew, companion-state combo)

Asynchronous dominants (``clear``/``preset``): delay-shaped tables on
the OUTPUT pin, related = the dominant pin, slew = the dominant's
ASSERTION edge. The label is per output: the same active-low RESET_B
is ``clear`` for Q and ``preset`` for Q_N. ``timing_sense`` stays out
of the DB: the consumer derives it from its own cell declaration.
- value: raw SI (seconds, farads). Unit conversion happens here, at
  the reader's boundary.

Rules: keys are exact-match coordinates; a missing key means NOT
MEASURED (failed measurements never write a value); writers rewrite
whole sorted files only (atomically); provenance goes in ``#`` header
comments, never inside keys; the corner lives in the file name, not in
the keys. Cell declaration (pins, functions, area, units) stays in the
consumer's own configuration: the DB only says what was measured.

Leakage/power keys are not specified yet: specify them here first,
before any writer emits them.
"""

import re
from pathlib import Path

_ENTRY_RE = re.compile(r'^\s*set\s+DB\(([^)]+)\)\s+(\S+)\s*$')

DELAY_TYPES = frozenset(('rising_edge', 'falling_edge'))
ASSERT_TYPES = frozenset(('clear', 'preset'))
CONSTRAINT_TYPES = frozenset(
    f'{kind}_{edge}' for kind in ('setup', 'hold', 'recovery', 'removal')
    for edge in ('rising', 'falling'))
MPW_TYPE = 'min_pulse_width'
CAP_TYPE = 'capacitance'


class MeasurementDB:
    """The entries of one per-cell measurement file."""

    def __init__(self):
        self.header = []   # provenance '#' lines, kept verbatim
        # (pin, related, timing_type, lut, axes as a tuple of strings) -> SI float
        self.entries = {}

    @classmethod
    def load(cls, path):
        db = cls()
        with open(path, encoding='utf-8') as file:
            for line in file:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith('#'):
                    db.header.append(stripped)
                    continue
                match = _ENTRY_RE.match(line)
                if not match:
                    raise ValueError(f'{path}: unparseable entry: {line.rstrip()}')
                head, *axes = (field.strip() for field in match.group(1).split(','))
                fields = head.split(':')
                if len(fields) != 4 or not axes:
                    raise ValueError(f'{path}: malformed key: {match.group(1)}')
                db.entries[(*fields, tuple(axes))] = float(match.group(2))
        return db

    def set(self, pin, related, timing_type, lut, axes, value):
        self.entries[(pin, related, timing_type, lut,
                      tuple(str(a) for a in axes))] = value

    def merge(self, other):
        self.entries.update(other.entries)
        for line in other.header:
            if line not in self.header:
                self.header.append(line)

    def save(self, path):
        """Whole sorted rewrite, atomic: a crash never corrupts the file."""
        path = Path(path)
        lines = list(self.header)
        for (pin, related, ttype, lut, axes), value in sorted(self.entries.items()):
            key = f'{pin}:{related}:{ttype}:{lut},{",".join(axes)}'
            lines.append(f'set DB({key}) {value:.6E}')
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        tmp.replace(path)

    def tables(self):
        """Group entries: (pin, related, timing_type, lut) -> {axes: value}."""
        grouped = {}
        for (pin, related, ttype, lut, axes), value in self.entries.items():
            grouped.setdefault((pin, related, ttype, lut), {})[axes] = value
        return grouped


def build_from_measurements(cell, config, settings):
    """Characterization task: build the cell's liberty groups from its
    measurement file instead of simulating. Scheduled by the analyser
    when the cell config carries a ``measurements`` path."""
    import PySpice
    from charlib.liberty import liberty
    from charlib.liberty.library import LookupTable

    def to_time(si):
        return (si @ PySpice.Unit.u_s).convert(settings.units.time.prefixed_unit).value

    def to_cap(si):
        return (si @ PySpice.Unit.u_F).convert(
            settings.units.capacitance.prefixed_unit).value

    paths = config.measurements
    if isinstance(paths, str):
        paths = [paths]
    db = MeasurementDB()
    for path in paths:
        db.merge(MeasurementDB.load(Path(path)))
    result = cell.liberty

    # (pin, related, timing_type) -> [LookupTable]: one timing group each
    timing_luts = {}
    # pin -> direction -> [SI values]: reduced to attributes below
    caps = {}

    for (pin, related, ttype, lut_name), points in db.tables().items():
        if ttype == CAP_TYPE:
            caps.setdefault(pin, {}).setdefault(lut_name, []).extend(points.values())
            continue
        if ttype in DELAY_TYPES or ttype in ASSERT_TYPES:
            if ttype in ASSERT_TYPES:
                # the per-output labelling rule implies the pairing:
                # a clear arc falls, a preset arc rises
                direction = 'fall' if ttype == 'clear' else 'rise'
                if not lut_name.startswith(('cell_', f'{direction}_')) \
                   or (lut_name.startswith('cell_') and lut_name != f'cell_{direction}'):
                    raise ValueError(
                        f'{paths}: {pin}:{related}:{ttype}:{lut_name} — a '
                        f'{ttype} arc must carry cell_{direction}/'
                        f'{direction}_transition tables (the label is per '
                        'output: the same dominant is clear for the output '
                        'it drops and preset for the one it raises)')
            # axes: (input slew, output load)
            slews = sorted({float(a[0]) for a in points})
            loads = sorted({float(a[1]) for a in points})
            lut = LookupTable(lut_name, f'delay_template_{len(loads)}x{len(slews)}',
                              total_output_net_capacitance=loads,
                              input_net_transition=slews)
            for (slew, load), value in points.items():
                lut[float(load), float(slew)] = to_time(value)
        elif ttype in CONSTRAINT_TYPES:
            # axes: (related-pin slew, constrained-pin slew)
            related_slews = sorted({float(a[0]) for a in points})
            constrained_slews = sorted({float(a[1]) for a in points})
            lut = LookupTable(
                lut_name,
                f'constraint_template_{len(constrained_slews)}x{len(related_slews)}',
                constrained_pin_transition=constrained_slews,
                related_pin_transition=related_slews)
            for (r_slew, c_slew), value in points.items():
                lut[float(c_slew), float(r_slew)] = to_time(value)
        elif ttype == MPW_TYPE:
            # axes: (slew, '-')
            slews = sorted({float(a[0]) for a in points})
            lut = LookupTable(lut_name, f'mpw_template_{len(slews)}',
                              constrained_pin_transition=slews)
            for axes, value in points.items():
                lut[float(axes[0]),] = to_time(value)
        else:
            raise ValueError(
                f'{config.measurements}: unsupported timing_type "{ttype}" '
                f'on pin {pin} (leakage/power keys are not specified yet)')
        timing_luts.setdefault((pin, related, ttype), []).append(lut)

    for (pin, related, ttype), luts in sorted(timing_luts.items()):
        timing_group = liberty.Group('timing')
        # min_pulse_width tables carry no reference pin in the DB but
        # liberty wants related_pin: it is the constrained pin itself
        timing_group.add_attribute('related_pin', pin if related == '-' else related)
        if ttype in DELAY_TYPES:
            timing_group.add_attribute('timing_sense', 'non_unate')
        elif ttype in ASSERT_TYPES:
            timing_group.add_attribute(
                'timing_sense', _assert_sense(cell, related, ttype, paths))
        timing_group.add_attribute('timing_type', ttype)
        for lut in sorted(luts, key=lambda l: l.name):
            timing_group.add_group(lut)
        result.group('pin', pin).add_group(timing_group)

    for pin, directions in sorted(caps.items()):
        pin_group = result.group('pin', pin)
        # INTERIM reduction policy (see DECISIONS): worst case over all
        # slews and companion states, per direction and overall
        worst = {d: max(values) for d, values in directions.items()}
        for direction, value in sorted(worst.items()):
            pin_group.add_attribute(f'{direction}_capacitance', to_cap(value))
        pin_group.add_attribute('capacitance', to_cap(max(worst.values())))

    return result


def _assert_sense(cell, related, ttype, paths):
    """timing_sense of a clear/preset arc, derived from the cell
    declaration (the DB does not carry it): positive_unate on the
    output that follows the dominant's assertion edge."""
    dominant = None
    for candidate in (cell.clear, cell.preset):
        if candidate is not None and candidate.name == related:
            dominant = candidate
    if dominant is None:
        raise ValueError(
            f'{paths}: {ttype} tables relate to pin "{related}" but the cell '
            'declaration names no such set/reset pin; timing_sense cannot be '
            'derived')
    assert_dir = 'fall' if dominant.inversion else 'rise'
    out_dir = 'fall' if ttype == 'clear' else 'rise'
    return 'positive_unate' if out_dir == assert_dir else 'negative_unate'


def dump_cell_group(cell_group, db_path, settings, header=()):
    """Append one measured liberty fragment into the cell's DB file.

    The reverse mapping of build_from_measurements, restricted to what a
    fragment holds losslessly: timing tables (delays, transitions,
    constraints, min_pulse_width) in SI. Pin capacitance attributes are
    already reduced values, not measurements: they are not dumped.
    Existing entries are updated, the file is rewritten sorted.
    """
    import PySpice

    # lib time unit -> SI: derive the scale through the same conversion
    # the procedures used to produce the fragment values
    scale = float((1 @ PySpice.Unit.u_s).convert(
        settings.units.time.prefixed_unit).value)

    db_path = Path(db_path)
    db = MeasurementDB.load(db_path) if db_path.exists() else MeasurementDB()
    for line in header:
        if line not in db.header:
            db.header.append(line)

    for pin_group in cell_group.subgroups_with_name('pin'):
        pin = pin_group.identifier
        for timing_group in pin_group.subgroups_with_name('timing'):
            attributes = timing_group.attributes
            if 'timing_type' not in attributes:
                continue
            ttype = attributes['timing_type'].value
            related = attributes.get('related_pin')
            related = related.value if related else '-'
            for lut in timing_group.groups.values():
                if not hasattr(lut, 'index_values'):
                    continue
                entries = _lut_entries(lut, ttype, scale)
                if entries is None:
                    continue
                if ttype == MPW_TYPE:
                    related = '-'
                for axes, value in entries:
                    db.set(pin, related, ttype, lut.name, axes, value)
    db.save(db_path)


def _lut_entries(lut, ttype, scale):
    """((axis labels), SI value) pairs for one LookupTable, in DB axis
    order; ``scale`` is lib-time-units per second. Returns None for
    families the format does not specify."""
    import itertools

    def label(value):
        return f'{value:g}'

    variables = list(lut.template.variables.keys())
    if (ttype in DELAY_TYPES or ttype in ASSERT_TYPES) \
       and variables == ['total_output_net_capacitance',
                         'input_net_transition']:
        loads, slews = lut.index_values
        return [((label(slew), label(load)), float(lut[load, slew]) / scale)
                for load, slew in itertools.product(loads, slews)]
    if ttype in CONSTRAINT_TYPES and variables == ['constrained_pin_transition',
                                                   'related_pin_transition']:
        constrained, related = lut.index_values
        return [((label(r), label(c)), float(lut[c, r]) / scale)
                for c, r in itertools.product(constrained, related)]
    if ttype == MPW_TYPE and variables == ['constrained_pin_transition']:
        (slews,) = lut.index_values
        return [((label(s), '-'), float(lut[s,]) / scale)
                for s in slews]
    return None
