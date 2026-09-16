"""Purge registry records that are the template's EXAMPLE rows, imported.

`build_template_bytes` ships a separate EXAMPLE sheet that illustrates the
three test types with placeholder numbers. That sheet is never read by the
importer — only READINGS is — but a template downloaded before that was true
carried the same rows in READINGS, so importing one wrote the illustration
into the registry as if it were field data. On 6 Sep 2026 that happened five
times: 15 tests named COL-A1 / BEAM-B2 / WALL-W1 whose readings are the
example numbers verbatim (120 mm at 30.1 / 29.8 / 30.3 us, and so on).

This is example data standing where a measurement should be — the exact thing
the no-dummy-data rule forbids — so it is removed rather than left to be
reported as a real element's strength.

It matches records against the template's OWN example values, read from
`excel_import.example_rows` rather than a copied list, so it cannot drift from
the template and cannot touch a genuine measurement: a real element would have
to reproduce the illustration's numbers AND its per-type shape exactly to be
caught, and any such record is the illustration by definition.

Dry run by default:

    python manage.py purge_example_template_data
    python manage.py purge_example_template_data --execute
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.digital_eye.excel_import import example_rows
from apps.digital_eye.models import PUNDITTest

# Indices into a TEMPLATE_COLUMNS-ordered example row (excel_import).
_COL_ELEMENT = 0
_COL_TYPE = 2
_COL_POINT = 3
_COL_PATH = 4
_COL_TRANSIT = 5
_COL_UNCRACKED = 6
_COL_SURFACE = 7

# The importer's test-type spelling -> the model's stored value.
_TYPE_MAP = {
    'pulse velocity': 'pulse_velocity',
    'crack depth': 'crack_depth',
    'surface quality': 'surface_quality',
}


def _example_shapes():
    """``{test_type: [shape, ...]}`` — one shape per illustration group.

    A shape is the tuple of per-point measurements the group records, in
    order, so it describes a whole test rather than a single row.
    """
    shapes = {}
    group = []
    group_key = None

    def flush():
        if group and group_key:
            shapes.setdefault(group_key[1], []).append(tuple(group))

    for row in example_rows():
        test_type = _TYPE_MAP.get(str(row[_COL_TYPE] or '').strip().lower())
        if test_type is None:
            continue
        # The importer starts a new test when the element or the test type
        # changes, so the illustration's groups are read the same way. The
        # element name keys the group but is deliberately NOT part of the
        # shape: the same illustration re-downloaded with real element names
        # substituted is still the illustration.
        key = (str(row[_COL_ELEMENT] or ''), test_type)
        if group and key != group_key:
            flush()
            group = []
        group_key = key
        group.append((
            str(row[_COL_POINT] or ''),
            _as_float(row[_COL_PATH]),
            _as_float(row[_COL_TRANSIT]),
            _as_float(row[_COL_UNCRACKED]),
            _as_text(row[_COL_SURFACE]),
        ))
    flush()
    return shapes


def _as_float(value):
    return None if value is None else float(value)


def _as_text(value):
    """A blank cell and an unset column are the same absence.

    ``surface_condition`` is a non-null CharField defaulting to '', so a
    stored reading has '' where the template's cell is simply empty; without
    this the two would never compare equal on a pulse or crack test.
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _test_shape(test):
    """The same shape, read from the stored readings. ``None`` when the test
    has no readings (a legacy scalar record cannot be an imported example)."""
    rows = list(test.readings.all())
    if not rows:
        return None
    return tuple(
        (str(r.point_label or ''), r.path_length_mm, r.transit_time_us,
         r.uncracked_transit_time_us, _as_text(r.surface_condition))
        for r in rows
    )


class Command(BaseCommand):
    help = ("Delete PUNDIT tests whose readings are the Excel template's "
            "EXAMPLE rows, imported as if they were field data. Dry run "
            "unless --execute is passed.")

    def add_arguments(self, parser):
        parser.add_argument(
            '--execute',
            action='store_true',
            help='Actually delete the rows. Without this flag nothing is changed.',
        )

    def handle(self, *args, **options):
        execute = options['execute']
        shapes = _example_shapes()

        found = []
        for test in PUNDITTest.objects.select_related('project'):
            candidates = shapes.get(test.test_type)
            if not candidates:
                continue
            shape = _test_shape(test)
            if shape is not None and shape in candidates:
                found.append(test)

        if not found:
            self.stdout.write(self.style.SUCCESS(
                'No imported template example rows found — the registry '
                'contains no illustration data.'))
            return

        self.stdout.write(self.style.WARNING(
            f'{len(found)} test(s) are the template\'s example rows, '
            f'imported as field data:'))
        for test in found:
            project = test.project.name if test.project else 'no project'
            self.stdout.write(
                f'  {test.created_at:%Y-%m-%d %H:%M}  '
                f'{test.test_type:<16} '
                f'{(test.structural_element or "")[:34]:<34} '
                f'readings={test.readings.count()}  [{project}]')

        if not execute:
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE(
                'Dry run — nothing deleted. Re-run with --execute to remove '
                'these rows.'))
            return

        with transaction.atomic():
            # Readings cascade with their test; capture the count first.
            readings = sum(t.readings.count() for t in found)
            for test in found:
                test.delete()

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Deleted {len(found)} example test(s) and their {readings} '
            f'reading(s). The registry now holds only recorded measurements.'))
