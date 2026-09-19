"""
Proposing a column mapping from an instrument's own export.

An instrument writes its export with its own column names. The platform reads
by its own. Bridging the two is what a device's ``column_mapping`` is for, and
until now the only way to write one was to open the CSV in a text editor, read
the header row by eye, and type a JSON object — spelling the instrument's words
on the left and guessing a contract key on the right from a list printed in a
paragraph. This module does the reading half.

Two rules shape it.

**It proposes; a person disposes.** Nothing here writes anything, and nothing
here is applied to a record. The header names are *read* — they are facts about
the file. The targets are *proposed* — they are claims about what an instrument
writes, and every one of them is checked by a human before it becomes part of a
device's record. That distinction is not politeness: as ``FieldDevice`` says of
this field, a wrong mapping is indistinguishable from a right one once rows have
been written from it. The click is the whole safety mechanism.

**It never guesses a number.** A column proposed as ``path_length_l_mm`` has its
actual values examined, and where they cannot be millimetres the conversion is
proposed along with the reason. This is the one place the suggester is allowed
to be clever, because it is the one place being wrong is silent: an export whose
``Distance`` holds metres, mapped to a millimetre key without a scale, produces
a pulse velocity a thousand times too low — positive, plausible, and graded.
Nothing downstream can tell that from a slow reading, so the check has to happen
here, in front of the person who can see the instrument.
"""
from dataclasses import dataclass
from statistics import median

from apps.data_import.readers import normalise_key, read_headers
from apps.data_import.registry import (
    UPV_ACCEPTED_KEYS, UPV_COLUMNS, UPV_MEASUREMENT_KEYS,
)

#: The contract keys, each spelled the way the platform's template spells it.
#: Built from ``UPV_COLUMNS`` rather than written out again, so a column added
#: to the contract cannot exist as a key with no name to show a person.
COLUMN_LABELS = {normalise_key(name): name for name in UPV_COLUMNS}

#: Below this, a column of numbers cannot be a path length in millimetres.
#:
#: Not a tolerance — a physical impossibility. The transducers a PUNDIT uses are
#: themselves wider than this, so no reading across an element can be shorter.
#: A column whose values sit under it is in some other unit, and metres is the
#: only one that makes the numbers a path length again.
#:
#: Deliberately not extended upward to catch centimetres. A value of 25 could be
#: 25 mm across a small cube or 25 cm across a wall, both real measurements, and
#: the platform has no way to tell which — so it says nothing rather than
#: converting on a coin toss.
MIN_PLAUSIBLE_PATH_LENGTH_MM = 10.0

#: Header names an instrument writes, folded, mapped to the contract key they
#: mean. Seeded from the Proceq Pundit PL-200 operating instructions and from
#: ordinary field naming.
#:
#: Every entry is a claim, and the claim is only ever *shown* to a person and
#: pre-selected for them — never written. The first real PL-200 export is what
#: settles whether these are right, and when it arrives the suggester displays
#: what it read, so a wrong entry is visible on screen rather than buried in a
#: record.
ALIASES = {
    # Path length. The instrument's own unit is not assumed from the name —
    # see ``_path_length_scale``, which reads the values.
    'distance': 'path_length_l_mm',
    'distance_mm': 'path_length_l_mm',
    'distance_m': 'path_length_l_mm',
    'path_length': 'path_length_l_mm',
    'path_length_mm': 'path_length_l_mm',
    'path_length_l': 'path_length_l_mm',
    'length': 'path_length_l_mm',
    'length_mm': 'path_length_l_mm',
    'specimen_length': 'path_length_l_mm',
    'specimen_length_mm': 'path_length_l_mm',

    # Transit time.
    'time': 'transit_time_t_us',
    'time_us': 'transit_time_t_us',
    'time_1': 'transit_time_t_us',
    'time1': 'transit_time_t_us',
    'transit_time': 'transit_time_t_us',
    'transit_time_us': 'transit_time_t_us',
    'transit_time_t': 'transit_time_t_us',
    'travel_time': 'transit_time_t_us',
    'travel_time_us': 'transit_time_t_us',
    'pulse_time': 'transit_time_t_us',
    'pulse_time_us': 'transit_time_t_us',
    'pulse_transit_time': 'transit_time_t_us',

    # The uncracked transit time of a crack-depth test.
    'uncracked_time': 't_uncracked_us',
    'uncracked_time_us': 't_uncracked_us',
    'time_uncracked': 't_uncracked_us',
    't_uncracked': 't_uncracked_us',
    't_uncracked_us': 't_uncracked_us',

    # Structural element.
    'element': 'structural_element',
    'member': 'structural_element',
    'structural_member': 'structural_element',
    'member_id': 'structural_element',
    'element_id': 'structural_element',
    'component': 'structural_element',

    # Point.
    'point_no': 'point',
    'point_number': 'point',
    'point_id': 'point',
    'test_point': 'point',
    'mark': 'point',
    'mark_no': 'point',
    'mark_number': 'point',
    'station': 'point',

    # Location and floor.
    'location': 'test_location',
    'test_position': 'test_location',
    'grid': 'test_location',
    'grid_reference': 'test_location',
    'level': 'floor',
    'storey': 'floor',
    'story': 'floor',
    'elevation': 'floor',

    # Conditions.
    'weather': 'weather_condition',
    'surface': 'surface_condition',
    'surface_preparation': 'surface_condition',
    'surface_state': 'surface_condition',

    # Transducer.
    'transducer': 'transducer_type',
    'probe': 'transducer_type',
    'probe_type': 'transducer_type',
    'frequency': 'transducer_frequency_khz',
    'frequency_khz': 'transducer_frequency_khz',
    'transducer_frequency': 'transducer_frequency_khz',
    'probe_frequency': 'transducer_frequency_khz',

    # Rebound.
    'rebound': 'rebound_number',
    'rebound_value': 'rebound_number',

    # Free text.
    'note': 'notes',
    'remark': 'notes',
    'remarks': 'notes',
    'comment': 'notes',
    'comments': 'notes',
    'description': 'notes',
}

#: Headers the platform recognises as *something* and deliberately does not
#: read. Each carries the reason, which is shown beside the column so the
#: operator can see what is being left out and why rather than finding a gap
#: in the record later.
#:
#: These exist because the alternative — no entry at all — reads as "the
#: platform does not know this column", which invites someone to map it to the
#: nearest-looking key. Several of these are near-misses that would be accepted
#: by the registry and then mean the wrong thing.
DECLINED = {
    'velocity': (
        'The platform computes pulse velocity from path length ÷ transit time. '
        'Importing the instrument\'s own figure as well would record a second, '
        'unreconciled number.'),
    'pulse_velocity': (
        'The platform computes pulse velocity from path length ÷ transit time. '
        'Importing the instrument\'s own figure as well would record a second, '
        'unreconciled number.'),
    'velocity_km_s': (
        'The platform computes pulse velocity from path length ÷ transit time. '
        'Importing the instrument\'s own figure as well would record a second, '
        'unreconciled number.'),
    'time_2': (
        'The second transit time is ambiguous — on a repeat reading it is the '
        'same measurement again, and on a crack-depth test it is the uncracked '
        'time. Choosing wrong would put a real number in the wrong column, so '
        'it is left for the operator to place.'),
    'time2': (
        'The second transit time is ambiguous — on a repeat reading it is the '
        'same measurement again, and on a crack-depth test it is the uncracked '
        'time. Choosing wrong would put a real number in the wrong column, so '
        'it is left for the operator to place.'),
    'measurement_type': (
        'This is the transducer arrangement (direct, indirect, surface), not '
        'the test type. Mapping one to the other would feed the registry a '
        'value it reads as a different field entirely.'),
    'test_setup': (
        'This is the transducer arrangement, not the test type. Mapping one to '
        'the other would feed the registry a value it reads as a different '
        'field entirely.'),
    'crack_depth': (
        'No column in the PUNDIT contract carries a crack depth. The platform '
        'records the two transit times and derives the depth itself.'),
    'correction_factor': (
        'A correction factor changes the velocity the instrument reports. The '
        'platform records the measured path length and transit time as they '
        'were, so the correction has to be applied where the reading is made, '
        'not applied silently on the way in.'),
    'id': (
        'The instrument\'s own row identifier is not a platform column. Every '
        'reading is identified by the session it arrives in.'),
    'serial_number': (
        'The instrument\'s serial number belongs on the device record, not on '
        'each reading.'),
    'date_time': (
        'The platform dates a reading from the session the file arrives in, so '
        'a file\'s own timestamp is not read.'),
    'timestamp': (
        'The platform dates a reading from the session the file arrives in, so '
        'a file\'s own timestamp is not read.'),
    'date': (
        'The platform dates a reading from the session the file arrives in, so '
        'a file\'s own timestamp is not read.'),
    'name': (
        'Ambiguous on its own. If this holds the test point, map it to POINT; '
        'if it holds the element, map it to STRUCTURAL ELEMENT.'),
}


@dataclass(frozen=True)
class ColumnSuggestion:
    """What the platform makes of one column, for a person to accept or fix.

    ``header`` and ``samples`` are read from the file. ``target``, ``scale``
    and ``note`` are the proposal — the parts a human is being asked to check.
    """

    header: str
    samples: list
    target: str | None
    scale: float
    #: ``exact`` — the header already is a contract key. ``alias`` — a known
    #: instrument name for one. ``declined`` — recognised and deliberately not
    #: read, with ``note`` saying why. ``unknown`` — nothing to go on.
    basis: str
    note: str

    def as_dict(self):
        return {
            'header': self.header,
            'samples': list(self.samples),
            'target': self.target,
            'target_label': COLUMN_LABELS.get(self.target or '', ''),
            'scale': self.scale,
            'basis': self.basis,
            'note': self.note,
        }


def _numbers(samples):
    """The sample values that are numbers, in the order they were written."""
    values = []
    for sample in samples or ():
        try:
            values.append(float(str(sample).strip()))
        except (TypeError, ValueError):
            continue
    return values


def _path_length_scale(samples):
    """``(scale, note)`` for a column proposed as a path length in millimetres.

    Two parsed values is the floor: one number cannot distinguish a column in
    metres from a column with a typo in it, and a note built on a single value
    would be a claim the evidence does not carry.
    """
    values = _numbers(samples)
    if len(values) < 2:
        return 1.0, ''
    typical = median(values)
    if typical >= MIN_PLAUSIBLE_PATH_LENGTH_MM or typical <= 0:
        return 1.0, ''
    return 1000.0, (
        f'Values run around {typical:g}, and read as millimetres that is not a '
        f'path length across any real element — the transducers alone are '
        f'wider. Read as metres and converted to millimetres (×1000). If the '
        f'instrument is exporting some other unit, set the mapping by hand.')


def suggest_columns(headers, samples):
    """One ``ColumnSuggestion`` per header, in the order the file lists them.

    Pure: nothing is read from a database and nothing is written to one.
    """
    suggestions = []
    for header in headers:
        folded = normalise_key(header)
        values = list(samples.get(header, ()))
        note = ''
        if folded in UPV_ACCEPTED_KEYS:
            target, basis = folded, 'exact'
        elif folded in ALIASES:
            target, basis = ALIASES[folded], 'alias'
        elif folded in DECLINED:
            # Checked after ALIASES so a name that means something here wins
            # over a name that is merely familiar.
            target, basis, note = None, 'declined', DECLINED[folded]
        else:
            target, basis = None, 'unknown'

        scale = 1.0
        if target == 'path_length_l_mm':
            scale, unit_note = _path_length_scale(values)
            note = unit_note or note

        suggestions.append(ColumnSuggestion(
            header=header, samples=values, target=target, scale=scale,
            basis=basis, note=note))
    return suggestions


def proposed_mapping(suggestions):
    """The mapping these suggestions amount to, in the shape the API accepts.

    Three shapes come out of it, and the third is the one that makes this
    usable at all:

    * a scale of 1 is written as the plain key, not as an object with a scale
      of 1, so a mapping recorded here looks like every mapping recorded before
      it and stays readable to anyone who opens the field by hand;
    * a column needing conversion carries ``{'to': ..., 'scale': ...}``;
    * a column the platform declined is written as ``null``.

    The last one matters most. A column the mapping does not mention is refused
    on import — that is the guard that keeps an unaccounted-for column out of
    the registry — so a proposal that merely *omitted* the four PL-200 columns
    the contract has no home for would leave the file exactly as unimportable
    as it was before. ``null`` says the column was looked at and set aside,
    which is a thing a person can mean and a machine cannot.

    A column nothing is known about is left out rather than nulled. The
    platform has no basis for setting that one aside, and doing it silently is
    the misread the guard exists to prevent; the inspector decides it, and the
    editor records their decision when they save.
    """
    mapping = {}
    for suggestion in suggestions:
        if suggestion.basis == 'declined':
            mapping[suggestion.header] = None
        elif suggestion.target:
            if suggestion.scale == 1.0:
                mapping[suggestion.header] = suggestion.target
            else:
                mapping[suggestion.header] = {
                    'to': suggestion.target, 'scale': suggestion.scale}
    return mapping


def accepted_columns():
    """The contract, for a picker. Server-supplied so it cannot drift."""
    return [
        {
            'key': key,
            'label': COLUMN_LABELS.get(key, key),
            'group': 'measurement' if key in UPV_MEASUREMENT_KEYS else 'context',
        }
        for key in sorted(UPV_ACCEPTED_KEYS,
                          key=lambda k: (k not in UPV_MEASUREMENT_KEYS, k))
    ]


def describe(content, import_type, sample_limit=5):
    """Read a file and describe its columns. The one entry point callers need.

    Raises ``apps.data_import.readers.ImportReadError`` when the file cannot be
    read, with the same message the importer would give for the same bytes.
    """
    headers, samples = read_headers(content, import_type, sample_limit)
    suggestions = suggest_columns(headers, samples)
    return {
        'columns': [suggestion.as_dict() for suggestion in suggestions],
        'mapping': proposed_mapping(suggestions),
        'accepted': accepted_columns(),
    }
