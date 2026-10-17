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
- ``cond``: optional 5th ``:``-field naming the companion conditioning
  the arc was measured under (``GATE1``, ``D0``, ``SCE0SCD1``) — the
  liberty ``when`` in embryo. Applies to the delay families,
  ``min_pulse_width`` (whose keys keep related ``-`` and a ``-``
  second axis regardless) and ``capacitance``; never to constraints.
  This consumer folds conditioned entries worst-case.

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

DELAY_TYPES = frozenset(('combinational', 'rising_edge', 'falling_edge'))
ASSERT_TYPES = frozenset(('clear', 'preset'))
CONSTRAINT_TYPES = frozenset(
    f'{kind}_{edge}' for kind in ('setup', 'hold', 'recovery', 'removal')
    for edge in ('rising', 'falling'))
MPW_TYPE = 'min_pulse_width'
CAP_TYPE = 'capacitance'
# families that may carry the optional conditioning field (the liberty
# `when` in embryo); constraint conditioning is the scenario's business
CONDITIONABLE = DELAY_TYPES | {MPW_TYPE, CAP_TYPE}


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
                if len(fields) == 4:
                    cond = ''
                elif len(fields) == 5:
                    cond = fields[4]
                    if fields[2] not in CONDITIONABLE or not cond:
                        raise ValueError(
                            f'{path}: the conditioning field only applies to '
                            f'{sorted(CONDITIONABLE)}: {match.group(1)}')
                else:
                    raise ValueError(f'{path}: malformed key: {match.group(1)}')
                if fields[2] == MPW_TYPE and \
                   (fields[1] != '-' or len(axes) != 2 or axes[1] != '-'):
                    raise ValueError(
                        f'{path}: min_pulse_width keys keep related = - and a '
                        f'- second axis whatever the conditioning: {match.group(1)}')
                if not axes:
                    raise ValueError(f'{path}: malformed key: {match.group(1)}')
                db.entries[(*fields[:4], cond, tuple(axes))] = float(match.group(2))
        return db

    def set(self, pin, related, timing_type, lut, axes, value, cond=''):
        self.entries[(pin, related, timing_type, lut, cond,
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
        for (pin, related, ttype, lut, cond, axes), value in sorted(self.entries.items()):
            head = f'{pin}:{related}:{ttype}:{lut}' + (f':{cond}' if cond else '')
            lines.append(f'set DB({head},{",".join(axes)}) {value:.6E}')
        tmp = path.with_suffix(path.suffix + '.tmp')
        tmp.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        tmp.replace(path)

    def tables(self, excluded=None):
        """Group entries: (pin, related, timing_type, lut) -> {axes: value},
        folding conditioned entries worst-case (the db keeps what was
        measured; the liberty consumer publishes the pessimistic table).

        ``excluded(pin, related, ttype, cond)`` names the conditioned
        entries that leave the fold (usage-illegal states, see
        ``illegal_excluder``). An excluded entry is dropped only while a
        legal sibling exists at the same point : the fold never empties
        an arc, nor invents a value."""
        grouped = {}
        legal = {}   # (key, axes) -> a non-excluded entry has been folded
        for (pin, related, ttype, lut, cond, axes), value in self.entries.items():
            key = (pin, related, ttype, lut)
            table = grouped.setdefault(key, {})
            out = bool(cond) and excluded is not None and excluded(pin, related, ttype, cond)
            if axes not in table:
                table[axes] = value
                legal[key, axes] = not out
            elif out and legal[key, axes]:
                continue
            elif not out and not legal[key, axes]:
                table[axes] = value          # first legal entry supersedes the illegal ones
                legal[key, axes] = True
            else:
                table[axes] = max(table[axes], value)
        return grouped


def parse_cond(cond, pins):
    """Conditioning field -> companion levels : ``LATCH1DN1`` ->
    {'LATCH': True, 'DN': True}. Longest pin name first (RESET_B before B)."""
    state, rest = {}, cond
    names = sorted(pins, key=len, reverse=True)
    while rest:
        for name in names:
            if rest.startswith(name) and len(rest) > len(name) and rest[len(name)] in '01':
                state[name] = rest[len(name)] == '1'
                rest = rest[len(name) + 1:]
                break
        else:
            raise ValueError(f'unparseable conditioning field "{cond}" (pins {sorted(pins)})')
    return state


def excluder(pins, rules):
    """Predicate for ``MeasurementDB.tables`` : true when the entry's
    conditioning, with the toggling pin (the measured pin for caps and
    mpw, the related pin for delay arcs) at either level, satisfies one
    of ``rules`` (pin -> level dicts) : the measurement crossed a state
    the cell's user declares illegal. A rule naming an undetermined pin
    does not match."""
    def excluded(pin, related, ttype, cond):
        state = parse_cond(cond, pins)
        toggling = pin if related == '-' else related
        for level in (False, True):
            crossed = dict(state)
            crossed.setdefault(toggling, level)
            if any(all(crossed.get(p) == v for p, v in rule.items()) for rule in rules):
                return True
        return False
    return excluded


def collect_caps(db, excluded=None):
    """pin -> direction -> [SI values] of every capacitance entry (slews x
    companion conditionings), UNFOLDED : the reduction is the caller's.
    Entries whose conditioning the ``excluded`` predicate names leave
    the collection while a legal sibling exists for the (pin,
    direction) ; never an empty list."""
    legal, illegal = {}, {}
    for (pin, related, ttype, lut, cond, axes), value in db.entries.items():
        if ttype != CAP_TYPE:
            continue
        out = bool(cond) and excluded is not None and excluded(pin, related, ttype, cond)
        (illegal if out else legal).setdefault(pin, {}).setdefault(lut, []).append(value)
    caps = {}
    for pin in set(legal) | set(illegal):
        for d in set(legal.get(pin, {})) | set(illegal.get(pin, {})):
            vals = legal.get(pin, {}).get(d) or illegal[pin][d]
            caps.setdefault(pin, {})[d] = vals
    return caps


def cap_fold(values):
    """The published capacitance of one (pin, direction) from its
    measured values over slews and companion conditionings : (mean, min,
    max). Ratified 17/10 against the IHP sg13g2 tables (DECISIONS) : the
    MEAN is what the vendor publishes (within ~10 %), the worst case
    sits 17-32 % above it ; min and max feed the *_capacitance_range
    attributes, the vendor's own way of keeping the spread visible."""
    return (sum(values) / len(values), min(values), max(values))


def illegal_excluder(cell):
    """The cell's ``illegal:`` rules as a fold excluder, None when it has none."""
    if not getattr(cell, 'illegal', None):
        return None
    return excluder([p.name for p in cell.all_pins()], cell.illegal)


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

    excluder = illegal_excluder(cell)
    caps = collect_caps(db, excluder)        # unfolded : the fold is cap_fold's
    for (pin, related, ttype, lut_name), points in db.tables(excluder).items():
        if ttype == CAP_TYPE:
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
        if ttype == 'combinational':
            timing_group.add_attribute(
                'timing_sense', _combinational_sense(cell, pin, related))
        elif ttype in DELAY_TYPES:
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
        # Reduction policy (DECISIONS, ratified 17/10 against the IHP
        # tables) : per direction the MEAN over slews and companion
        # conditionings (minus the illegal states), the spread published
        # as <dir>_capacitance_range (min, max) ; the pin scalar is the
        # mean of the two directions. "Moyennes de moyennes, soyons
        # lisses" — the worst case sat 17-32 % above the vendor.
        means = {}
        for direction, values in sorted(directions.items()):
            mean, lo, hi = cap_fold(values)
            means[direction] = mean
            pin_group.add_attribute(f'{direction}_capacitance', to_cap(mean))
            pin_group.add_attribute(f'{direction}_capacitance_range',
                                    [round(to_cap(lo), 6), round(to_cap(hi), 6)])
        pin_group.add_attribute('capacitance', to_cap(sum(means.values()) / len(means)))

    return result


def _combinational_sense(cell, pin, related):
    """timing_sense of a combinational arc, derived from the declared
    function of the output (the db does not carry it): sweep the other
    operands, watch the output's monotonicity in the related pin."""
    function = cell.functions.get(pin)
    if function is None or related not in function.operands:
        return 'non_unate'
    others = [name for name in function.operands if name != related]
    rises = falls = False
    for n in range(2 ** len(others)):
        base = dict(zip(others, (int(c) for c in f'{n:0{len(others)}b}'))) \
            if others else {}
        low = function.eval(**base, **{related: 0})
        high = function.eval(**base, **{related: 1})
        rises |= high > low
        falls |= high < low
    if rises and not falls:
        return 'positive_unate'
    if falls and not rises:
        return 'negative_unate'
    return 'non_unate'


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
