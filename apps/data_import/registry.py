"""
Record-type registry: what an imported row is, and how it becomes a registry row.

Four record types, and each one names the serializer that already owns its
writes. That is the rule this module exists to keep: **an import is not a second
write path.** A `UPV` row is validated by ``PUNDITTestSerializer`` — the same
one the manual entry form uses — so a validation rule tightened for the form is
tightened here, and a value only the server may compute (pulse velocity, quality
grade, estimated strength, the E.C.S through the project's active curve) is
still computed by the server when the values arrive in a file.

What a registry entry declares
------------------------------
``columns``
    The template's columns, in order. The CSV template endpoint renders them, so
    the file the client downloads and the file this module accepts can never
    disagree — that pairing is what makes the template a contract rather than a
    suggestion.

``required``
    The keys that must be non-blank on every row. Checked before any serializer
    runs, because "row 7 has no structural element" is a better message than
    whatever a serializer says when a structural element is missing.

``groups``
    Whether consecutive rows form one record. Only UPV does: one reading point
    is not a test, and a `PUNDITTest` per row would fill the registry with
    one-point tests that no field engineer produced. The grouping rule — same
    structural element, same test type, same floor — is the rule
    ``digital_eye.excel_import`` already applies to the spreadsheet template,
    kept identical so a CSV and a workbook of the same readings produce the same
    records.

``build``
    Rows → ``(payload, save_kwargs)``: the serializer's input, plus any value
    the serializer marks read-only but that the server still has to supply (a
    defect's scan session is provenance, not a field a client picks, so it
    travels as a save argument). Returns rather than raises where it can,
    because a rejected row's message is data: it is stored on the
    ``ImportRecord`` and shown to the inspector next to the row number.

``owner_fields``
    The model's "who did this" foreign keys, which the commit fills from the
    uploading user. ``GPRSurvey`` and ``PUNDITTest`` both carry ``created_by``
    and ``operator``, and the manual create endpoints set them — so a survey
    typed into the form shows an operator and an imported one would show a
    blank, which reads as "nobody did this" rather than as "imported". The
    fields are named here rather than set inside ``build`` because the value
    comes from the caller, not from the file.
"""
from dataclasses import dataclass, field
from typing import Callable

from apps.digital_eye.excel_import import TEST_TYPE_ALIASES

# ----------------------------------------------------------------------
# Value coercion
# ----------------------------------------------------------------------

#: Booleans, spelled the ways a spreadsheet spells them.
_TRUE = {'true', 'yes', 'y', '1'}
_FALSE = {'false', 'no', 'n', '0'}


def _text(value):
    if value is None:
        return ''
    text = str(value).strip()
    return text


def _number(value, label):
    """A float, or an error naming the column. Never a silent 0."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if isinstance(value, bool):
        return None, f'{label} must be a number, not a yes/no value.'
    try:
        return float(str(value).strip()), None
    except (TypeError, ValueError):
        return None, f'{label} must be a number — the file has {value!r}.'


def _integer(value, label):
    number, error = _number(value, label)
    if error:
        return None, error
    if number is None:
        return None, None
    if number != int(number):
        return None, f'{label} must be a whole number — the file has {value!r}.'
    return int(number), None


def _boolean(value, label):
    text = _text(value).lower()
    if not text:
        return None, None
    if text in _TRUE:
        return True, None
    if text in _FALSE:
        return False, None
    return None, f'{label} must be yes or no — the file has {value!r}.'


def _choice(value, valid, label):
    text = _text(value)
    if not text:
        return '', None
    if text in valid:
        return text, None
    folded = {option.lower(): option for option in valid}
    if text.lower() in folded:
        # Case is presentation. `High` and `HIGH` are one severity, and a
        # spreadsheet's autocapitalise should not cost the inspector a row.
        return folded[text.lower()], None
    return None, (f'{label} must be one of {", ".join(sorted(valid))} '
                  f'— the file has {value!r}.')


def _test_type(value, label='TEST TYPE'):
    text = _text(value)
    if not text:
        return '', None
    lowered = text.lower()
    for needle, canonical in TEST_TYPE_ALIASES:
        if needle in lowered:
            return canonical, None
    return None, (f'{label} must name a pulse velocity, crack depth or surface '
                  f'quality test — the file has {value!r}.')


# ----------------------------------------------------------------------
# Build context and result
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class ImportContext:
    """Everything a build needs that does not come from the file itself."""

    user: object
    project: object
    inspection: object = None
    request: object = None
    #: Scan sessions belonging to the batch's project, keyed by their id, and
    #: the project's inspections keyed by reference. Both are resolved once per
    #: validation pass rather than once per row — a 5,000-row defect file would
    #: otherwise issue 5,000 lookups to reject each row individually.
    scan_sessions: dict = field(default_factory=dict)
    inspections_by_reference: dict = field(default_factory=dict)


class RowError(Exception):
    """One row could not be turned into a record. The message names the row."""


@dataclass(frozen=True)
class RecordType:
    record_type: str
    model_label: str
    serializer_path: str
    columns: tuple
    required: tuple
    build: Callable
    groups: bool = False
    description: str = ''
    column_notes: tuple = ()
    owner_fields: tuple = ()

    def serializer_class(self):
        module_path, _, class_name = self.serializer_path.rpartition('.')
        module = __import__(module_path, fromlist=[class_name])
        return getattr(module, class_name)


# ----------------------------------------------------------------------
# UPV — PUNDIT test readings
# ----------------------------------------------------------------------

UPV_COLUMNS = (
    'STRUCTURAL ELEMENT', 'FLOOR', 'TEST TYPE', 'POINT',
    'PATH LENGTH L (MM)', 'TRANSIT TIME T (US)', 'T UNCRACKED (US)',
    'SURFACE CONDITION', 'TRANSDUCER FREQUENCY (KHZ)', 'TRANSDUCER TYPE',
    'TEST LOCATION', 'WEATHER CONDITION', 'REBOUND NUMBER', 'NOTES',
)
UPV_REQUIRED = ('STRUCTURAL ELEMENT', 'TEST TYPE')

#: The same contract in its folded key form — what ``readers.normalise_key``
#: produces from a header, which is what the registry actually sees.
#: ``UPV_COLUMNS`` is what a person writes in a spreadsheet; these are the keys
#: those columns become. Split by what they mean, because the two are not
#: interchangeable in the import path:
#:
#:   * a **measurement** is a number only the instrument knows, and a file
#:     without any of them is not a capture;
#:   * a **context** value is the inspector's judgement, which a full-template
#:     export may carry or which the app may supply instead.
#:
#: A device's column mapping may target any key in either set, and nothing
#: else — which is why this lives here beside the columns rather than in the
#: one importer that happened to need it first.
UPV_MEASUREMENT_KEYS = frozenset({
    'point', 'path_length_l_mm', 'transit_time_t_us', 't_uncracked_us',
    'surface_condition', 'rebound_number', 'notes',
})
UPV_CONTEXT_KEYS = frozenset({
    'structural_element', 'floor', 'test_type', 'test_location',
    'weather_condition', 'transducer_type', 'transducer_frequency_khz',
})
UPV_ACCEPTED_KEYS = UPV_MEASUREMENT_KEYS | UPV_CONTEXT_KEYS


def _build_upv(rows, ctx):
    """One test from consecutive readings of one element.

    The measurement each test type needs is checked here rather than left to
    the serializer, so a blank transit time is reported as "row 9 has no
    transit time" instead of as a serializer complaint about a nested reading.
    """
    first = rows[0].data
    test_type, error = _test_type(first.get('test_type'))
    if error:
        raise RowError(error)

    readings = []
    for row in rows:
        data = row.data
        reading = {}

        point = _text(data.get('point'))
        if point:
            reading['point_label'] = point

        path_length, error = _number(data.get('path_length_l_mm'),
                                     'PATH LENGTH L (MM)')
        if error:
            raise RowError(f'Row {row.row_number}: {error}')
        transit, error = _number(data.get('transit_time_t_us'),
                                 'TRANSIT TIME T (US)')
        if error:
            raise RowError(f'Row {row.row_number}: {error}')
        uncracked, error = _number(data.get('t_uncracked_us'),
                                   'T UNCRACKED (US)')
        if error:
            raise RowError(f'Row {row.row_number}: {error}')

        surface_condition = _text(data.get('surface_condition'))

        if test_type == 'pulse_velocity':
            if path_length is None:
                raise RowError(
                    f'Row {row.row_number}: PATH LENGTH L (MM) is required for '
                    'a pulse-velocity point.')
            if transit is None:
                raise RowError(
                    f'Row {row.row_number}: TRANSIT TIME T (US) is the field '
                    'measurement for a pulse-velocity point — it cannot be blank.')
            reading['path_length_mm'] = path_length
            reading['transit_time_us'] = transit
        elif test_type == 'crack_depth':
            if path_length is None:
                raise RowError(
                    f'Row {row.row_number}: PATH LENGTH L (MM) — the transducer '
                    'spacing — is required for a crack-depth point.')
            if transit is None or uncracked is None:
                raise RowError(
                    f'Row {row.row_number}: crack depth needs BOTH the cracked '
                    '(TRANSIT TIME T) and uncracked (T UNCRACKED) transit '
                    'times. They are field measurements.')
            reading['path_length_mm'] = path_length
            reading['transit_time_us'] = transit
            reading['uncracked_transit_time_us'] = uncracked
        else:
            if not surface_condition:
                raise RowError(
                    f'Row {row.row_number}: SURFACE CONDITION is the field '
                    'record for a surface-quality point — it cannot be blank.')
            reading['surface_condition'] = surface_condition

        if test_type == 'surface_quality' and surface_condition:
            reading['surface_condition'] = surface_condition
        rebound, error = _number(data.get('rebound_number'), 'REBOUND NUMBER')
        if error:
            raise RowError(f'Row {row.row_number}: {error}')
        if rebound is not None:
            reading['rebound_number'] = rebound
        notes = _text(data.get('notes'))
        if notes:
            reading['notes'] = notes

        readings.append(reading)

    payload = {
        'project': ctx.project.id,
        'test_type': test_type,
        'structural_element': _text(first.get('structural_element')),
        'floor': _text(first.get('floor')),
        'test_location': _text(first.get('test_location')),
        'weather_condition': _text(first.get('weather_condition')),
        'readings': readings,
    }

    transducer_type, error = _choice(
        first.get('transducer_type'),
        ('direct', 'semi_direct', 'indirect'), 'TRANSDUCER TYPE')
    if error:
        raise RowError(f'Row {rows[0].row_number}: {error}')
    if transducer_type:
        payload['transducer_type'] = transducer_type

    frequency, error = _integer(first.get('transducer_frequency_khz'),
                                'TRANSDUCER FREQUENCY (KHZ)')
    if error:
        raise RowError(f'Row {rows[0].row_number}: {error}')
    if frequency is not None:
        payload['transducer_frequency_khz'] = frequency

    return payload, {}


def group_upv_rows(rows):
    """Split rows into the tests they describe. Returns ``(groups, error)``.

    One element, one floor, one test type, on consecutive rows — the rule
    ``ImportService._group`` already applies to the wizard, stated here as well
    because the telemetry file path needs the same split without the record
    types and registry lookups that method is written around. The rule is what
    must not drift between the two paths, and it is one line above.

    A block that is interrupted and resumed is an error rather than two tests:
    it is almost always a sorting accident, and splitting it silently would
    file the same element twice in the registry. ``error`` is '' when every row
    grouped.

    Note what this is *not*. It is not a limit on how much a file may hold. A
    file of forty elements groups into forty tests and imports; what it refuses
    is the one shape that would put a real number under the wrong element.
    """
    groups = []
    seen = {}
    for row in rows:
        data = row.data
        key = (_text(data.get('structural_element')),
               _text(data.get('test_type')).lower(),
               _text(data.get('floor')))
        if groups and groups[-1][0] == key:
            groups[-1][1].append(row)
            continue
        if key in seen:
            return [], (
                f'Rows for element {key[0]!r} (test type {key[1]!r}, floor '
                f'{key[2] or "not recorded"}) appear in two separate blocks, '
                f'the first starting at row {seen[key]}. Keep one element\'s '
                'points on consecutive rows so they form a single test.')
        seen[key] = row.row_number
        groups.append((key, [row]))
    return groups, ''


# ----------------------------------------------------------------------
# GPR — survey headers
# ----------------------------------------------------------------------

GPR_COLUMNS = (
    'SURVEY TITLE', 'SURVEY AREA', 'STRUCTURAL ELEMENT', 'ANTENNA FREQUENCY (MHZ)',
    'DEPTH RANGE (M)', 'GRID SPACING (M)', 'LATITUDE', 'LONGITUDE', 'NOTES',
)
GPR_REQUIRED = ('SURVEY TITLE',)


def _build_gpr(rows, ctx):
    if len(rows) > 1:
        raise RowError(
            'A GPR survey is one row. This record has more than one — split it '
            'or remove the duplicate lines.')
    data = rows[0].data
    payload = {
        'project': ctx.project.id,
        'title': _text(data.get('survey_title')),
        'survey_area': _text(data.get('survey_area')),
        'structural_element': _text(data.get('structural_element')),
        'notes': _text(data.get('notes')),
    }
    if not payload['title']:
        raise RowError('Row %d: SURVEY TITLE is required — a survey with no '
                       'title cannot be told apart from the next one.'
                       % rows[0].row_number)

    for key, column, label, caster in (
        ('antenna_frequency_mhz', 'antenna_frequency_mhz',
         'ANTENNA FREQUENCY (MHZ)', _number),
        ('depth_range_m', 'depth_range_m', 'DEPTH RANGE (M)', _number),
        ('grid_spacing_m', 'grid_spacing_m', 'GRID SPACING (M)', _number),
        ('latitude', 'latitude', 'LATITUDE', _number),
        ('longitude', 'longitude', 'LONGITUDE', _number),
    ):
        value, error = caster(data.get(column), label)
        if error:
            raise RowError(f'Row {rows[0].row_number}: {error}')
        if value is not None:
            payload[key] = value
    return payload, {}


# ----------------------------------------------------------------------
# SLAM — defects located by a scan
# ----------------------------------------------------------------------

SLAM_COLUMNS = (
    'SCAN SESSION', 'DEFECT TYPE', 'SEVERITY', 'STATUS', 'LOCATION X',
    'LOCATION Y', 'LOCATION Z', 'GRID ZONE', 'ROOM LEVEL', 'DESCRIPTION',
    'CONFIDENCE',
)
SLAM_REQUIRED = ('SCAN SESSION', 'DEFECT TYPE', 'SEVERITY')

DEFECT_TYPES = ('crack', 'spalling', 'corrosion', 'thermal_anomaly',
                'deformation', 'delamination', 'honeycombing', 'voids',
                'moisture_ingress', 'reinforcement_exposure')
DEFECT_SEVERITIES = ('low', 'medium', 'high', 'critical')
DEFECT_STATUSES = ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'REJECTED')


def _build_slam(rows, ctx):
    """A defect located by a SLAM scan, filed against the session that found it.

    The session is required rather than optional. A defect's coordinates are
    meaningless without the scan they were measured in — ``location_x`` is a
    position in that session's point cloud, not on the earth — so a defect with
    no session would be a row that looks like a measurement and is not one.
    """
    if len(rows) > 1:
        raise RowError('A SLAM defect is one row.')
    data = rows[0].data

    session_ref = _text(data.get('scan_session'))
    session = ctx.scan_sessions.get(session_ref)
    if session is None:
        if not session_ref:
            raise RowError(f'Row {rows[0].row_number}: SCAN SESSION is required.')
        available = ', '.join(sorted(ctx.scan_sessions)) or 'none recorded'
        raise RowError(
            f'Row {rows[0].row_number}: scan session {session_ref!r} was not '
            f'found in this project. Scan sessions in this project: {available}.')

    defect_type, error = _choice(data.get('defect_type'), DEFECT_TYPES,
                                 'DEFECT TYPE')
    if error:
        raise RowError(f'Row {rows[0].row_number}: {error}')
    severity, error = _choice(data.get('severity'), DEFECT_SEVERITIES, 'SEVERITY')
    if error:
        raise RowError(f'Row {rows[0].row_number}: {error}')

    payload = {
        'type': defect_type,
        'severity': severity,
        'description': _text(data.get('description')),
        'grid_zone': _text(data.get('grid_zone')),
        'room_level': _text(data.get('room_level')),
    }
    status, error = _choice(data.get('status'), DEFECT_STATUSES, 'STATUS')
    if error:
        raise RowError(f'Row {rows[0].row_number}: {error}')
    if status:
        payload['status'] = status

    for key, column, label in (('location_x', 'location_x', 'LOCATION X'),
                               ('location_y', 'location_y', 'LOCATION Y'),
                               ('location_z', 'location_z', 'LOCATION Z')):
        value, error = _number(data.get(column), label)
        if error:
            raise RowError(f'Row {rows[0].row_number}: {error}')
        if value is not None:
            payload[key] = value

    confidence, error = _number(data.get('confidence'), 'CONFIDENCE')
    if error:
        raise RowError(f'Row {rows[0].row_number}: {error}')
    if confidence is not None:
        if not 0 <= confidence <= 1:
            raise RowError(
                f'Row {rows[0].row_number}: CONFIDENCE is a fraction between 0 '
                f'and 1 — the file has {confidence}. Write 0.9, not 90.')
        payload['confidence_score'] = confidence

    # `session` is read-only on DefectSerializer — a defect's scan is provenance,
    # not a field a client picks — so it travels as a save argument instead. The
    # *object*, not its id: `save()` merges its arguments straight into the
    # validated data and Django then assigns them to the FK, so an id raises the
    # same ValueError it would raise on a raw `Defect.objects.create()`.
    return payload, {'session': session}


# ----------------------------------------------------------------------
# FINDING — inspection findings
# ----------------------------------------------------------------------

FINDING_COLUMNS = (
    'INSPECTION', 'TITLE', 'DESCRIPTION', 'SEVERITY', 'CATEGORY',
    'CORRECTIVE ACTION', 'RESOLUTION DEADLINE', 'REQUIRES REINSPECTION',
)
FINDING_REQUIRED = ('TITLE', 'DESCRIPTION')

FINDING_SEVERITIES = ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')
FINDING_CATEGORIES = ('STRUCTURAL', 'SAFETY', 'ENVIRONMENTAL', 'MEP',
                      'PERMIT_DEVIATION', 'QUALITY')


def _build_finding(rows, ctx):
    """A finding, filed against an inspection.

    The inspection comes from the batch, or from the row when the row names
    one. It is never defaulted: a finding that reached the registry without an
    inspection would be a defect with no site visit behind it, and no way to
    find out later which one it was.
    """
    if len(rows) > 1:
        raise RowError('A finding is one row. This record has more than one — '
                       'split it or remove the duplicate lines.')
    data = rows[0].data

    severity, error = _choice(data.get('severity'), FINDING_SEVERITIES, 'SEVERITY')
    if error:
        raise RowError(f'Row {rows[0].row_number}: {error}')
    category, error = _choice(data.get('category'), FINDING_CATEGORIES, 'CATEGORY')
    if error:
        raise RowError(f'Row {rows[0].row_number}: {error}')

    inspection = ctx.inspection
    reference = _text(data.get('inspection'))
    if reference:
        # A row may name its own inspection, but only one inside the batch's
        # project — an import must not become a way to file a finding against
        # an inspection the uploader cannot open.
        inspection = ctx.inspections_by_reference.get(reference)
        if inspection is None:
            available = ', '.join(sorted(ctx.inspections_by_reference)) or 'none'
            raise RowError(
                f'Row {rows[0].row_number}: inspection {reference!r} was not '
                f'found in this project. Inspections in this project: '
                f'{available}.')

    if inspection is None:
        raise RowError(
            f'Row {rows[0].row_number}: a finding belongs to an inspection. '
            'Name one in the INSPECTION column, or upload the file against an '
            'inspection.')

    payload = {
        'inspection': inspection.id,
        'project': ctx.project.id,
        'title': _text(data.get('title')),
        'description': _text(data.get('description')),
    }
    if severity:
        payload['severity'] = severity
    if category:
        payload['category'] = category

    # `Finding.corrective_action_required` is a TextField — the column holds
    # *what* has to be done ("hack off and re-cast the cover"), not a yes/no.
    # The template column is named CORRECTIVE ACTION for that reason; naming it
    # "… REQUIRED" invited a yes/no, which the model would have stored as the
    # literal text "yes".
    corrective = _text(data.get('corrective_action'))
    if corrective:
        payload['corrective_action_required'] = corrective

    reinspection, error = _boolean(data.get('requires_reinspection'),
                                   'REQUIRES REINSPECTION')
    if error:
        raise RowError(f'Row {rows[0].row_number}: {error}')
    if reinspection is not None:
        payload['requires_reinspection'] = reinspection
    # Left unset when the column is blank: the model's default is False, and
    # setting it here would only restate that default while looking like a rule.

    deadline = _text(data.get('resolution_deadline'))
    if deadline:
        payload['resolution_deadline'] = deadline
    return payload, {}


# ----------------------------------------------------------------------
# The registry
# ----------------------------------------------------------------------

REGISTRY = {
    'UPV': RecordType(
        record_type='UPV',
        model_label='digital_eye.PUNDITTest',
        serializer_path='apps.digital_eye.serializers.PUNDITTestSerializer',
        columns=UPV_COLUMNS,
        required=UPV_REQUIRED,
        build=_build_upv,
        groups=True,
        owner_fields=('created_by', 'operator'),
        description=('Ultrasonic pulse velocity readings. Consecutive rows for '
                     'one structural element form one test.'),
    ),
    'GPR': RecordType(
        record_type='GPR',
        model_label='digital_eye.GPRSurvey',
        serializer_path='apps.digital_eye.serializers.GPRSurveySerializer',
        columns=GPR_COLUMNS,
        required=GPR_REQUIRED,
        build=_build_gpr,
        owner_fields=('created_by', 'operator'),
        description='Ground-penetrating radar survey headers.',
    ),
    'SLAM': RecordType(
        record_type='SLAM',
        model_label='scans.Defect',
        serializer_path='apps.scans.serializers.DefectSerializer',
        columns=SLAM_COLUMNS,
        required=SLAM_REQUIRED,
        build=_build_slam,
        description=('Defects located by a scan session, filed against the '
                     'session that produced them.'),
    ),
    'FINDING': RecordType(
        record_type='FINDING',
        model_label='inspections.Finding',
        serializer_path='apps.inspections.serializers.FindingSerializer',
        columns=FINDING_COLUMNS,
        required=FINDING_REQUIRED,
        build=_build_finding,
        description='Inspection findings.',
    ),
}

KNOWN_RECORD_TYPES = tuple(sorted(REGISTRY))


def columns_for(record_type):
    entry = REGISTRY.get(record_type)
    return list(entry.columns) if entry else []


def template_keys(record_type):
    """The canonical key for each template column, in template order."""
    from .readers import normalise_key
    return [normalise_key(column) for column in columns_for(record_type)]
