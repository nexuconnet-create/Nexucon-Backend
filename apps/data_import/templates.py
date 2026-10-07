"""
CSV / JSON templates for the manual import.

The template is not documentation — it is half of the contract. The header row
rendered here and the columns the registry accepts come from one list
(``RecordType.columns``), so a file built from the template always parses, and
a column the registry does not know about is visibly absent from the template
rather than silently ignored at import.

Two example rows are included, and they are an *example sheet* in the same
sense the PUNDIT workbook's is: marked as examples, obviously not real
readings, and never imported. They are here because a bare header row leaves
the column's *units* ambiguous — "PATH LENGTH L (MM)" tells you what to type,
but an example shows the magnitude.
"""
import csv
import io
import json

from .registry import REGISTRY, columns_for

#: Example values per column, used to seed the template's sample rows. These
#: are template furniture, not data: they are written only into a file the
#: client downloads, and no path in the platform reads them back into a model.
_EXAMPLES = {
    'STRUCTURAL ELEMENT': ['Column C4, Ground Floor', 'Beam B2, First Floor'],
    'FLOOR': ['Ground', 'First'],
    'TEST TYPE': ['Pulse Velocity', 'Crack Depth'],
    'POINT': ['A', 'B'],
    'PATH LENGTH L (MM)': [300, 400],
    'TRANSIT TIME T (US)': [72.4, 108.6],
    'T UNCRACKED (US)': [0, 96.2],
    'SURFACE CONDITION': ['Dry, smooth', 'Dry, smooth'],
    'TRANSDUCER FREQUENCY (KHZ)': [54, 54],
    'TRANSDUCER TYPE': ['Direct', 'Direct'],
    'TEST LOCATION': ['Grid line 4, 1.2 m from face', 'Grid line B, mid-span'],
    'WEATHER CONDITION': ['Dry, 31 C', 'Dry, 31 C'],
    'REBOUND NUMBER': [0, 0],
    'NOTES': ['Example row — delete before importing', 'Example row — delete before importing'],
    'SURVEY TITLE': ['Foundation Zone B — Grid 4-7', 'Raft Slab — Grid 8-10'],
    'SURVEY AREA': ['Grid 4-7', 'Grid 8-10'],
    'ANTENNA FREQUENCY (MHZ)': [400, 400],
    'DEPTH RANGE (M)': [2.5, 2.5],
    'GRID SPACING (M)': [0.5, 0.5],
    'LATITUDE': [6.4281, 6.4285],
    'LONGITUDE': [3.4219, 3.4224],
    'SCAN SESSION': ['<paste the scan session id>', '<paste the scan session id>'],
    'DEFECT TYPE': ['crack', 'honeycombing'],
    'SEVERITY': ['high', 'medium'],
    'STATUS': ['OPEN', 'OPEN'],
    'LOCATION X': [12.4, 18.9],
    'LOCATION Y': [3.1, 4.4],
    'LOCATION Z': [0.8, 1.2],
    'GRID ZONE': ['G4', 'G5'],
    'ROOM LEVEL': ['Ground', 'Ground'],
    'DESCRIPTION': ['Example row — delete before importing',
                    'Example row — delete before importing'],
    'CONFIDENCE': [0.88, 0.74],
    'INSPECTION': ['<inspection reference, or leave blank>',
                   '<inspection reference, or leave blank>'],
    'TITLE': ['Example row — delete before importing',
              'Example row — delete before importing'],
    'CATEGORY': ['STRUCTURAL', 'QUALITY'],
    'CORRECTIVE ACTION': ['Hack off and re-cast the cover to C4',
                          'Rake out and repoint the bed joint'],
    'RESOLUTION DEADLINE': ['2026-10-01', ''],
    'REQUIRES REINSPECTION': ['yes', 'no'],
}


def template_filename(record_type, fmt):
    return f'nexucon-import-template-{record_type.lower()}.{fmt}'


def build_template(record_type, fmt='csv'):
    """Render a template. Returns ``(bytes, filename, content_type)``.

    PDF is refused. A "PDF template" would be a page of instructions, and a
    platform that hands one out is promising an import it cannot perform — so
    the refusal names the two formats that work instead of producing a document
    that cannot be filled in and uploaded.
    """
    fmt = (fmt or 'csv').strip().lower()
    if record_type not in REGISTRY:
        raise ValueError(
            f'"{record_type}" is not an importable record type. '
            f'Valid types: {", ".join(sorted(REGISTRY))}.')

    if fmt == 'json':
        payload = _json_example(record_type)
        return (json.dumps(payload, indent=2).encode('utf-8'),
                template_filename(record_type, 'json'), 'application/json')
    if fmt != 'csv':
        raise ValueError(
            f'A template cannot be produced as {fmt.upper()} — only CSV or '
            'JSON. A PDF has no column contract, and an imported PDF would '
            'mean the platform guessing which printed number is which '
            'measurement.')

    buffer = io.StringIO(newline='')
    writer = csv.writer(buffer)
    columns = columns_for(record_type)
    writer.writerow(columns)
    for index in range(2):
        writer.writerow([
            _example_for(column, index, record_type) for column in columns
        ])
    return (buffer.getvalue().encode('utf-8-sig'),
            template_filename(record_type, 'csv'), 'text/csv')


def _example_for(column, index, record_type):
    values = _EXAMPLES.get(column)
    if not values:
        return ''
    return values[index] if index < len(values) else values[-1]


def _json_example(record_type):
    """The template as JSON: keys are the canonical column names."""
    from .readers import normalise_key

    entry = REGISTRY[record_type]
    records = []
    for index in range(2):
        record = {'record_type': record_type}
        for column in entry.columns:
            value = _example_for(column, index, record_type)
            if value not in ('', None):
                record[normalise_key(column)] = value
        records.append(record)
    return {'record_type': record_type, 'records': records}
