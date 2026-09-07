"""Purge the fabricated demo rows left behind by the removed seeder.

`apps.digital_eye.views._seed_defaults_if_empty()` used to insert hardcoded
"Eko Atlantic Signature Tower" structural elements, a GPR scan, two PUNDIT
tests (one stamped VERIFIED with an invented 4246 m/s velocity and 42.5 MPa
strength), a CONNECTED Trimble connection and a device report whenever the
corresponding table was empty. Because it ran from ordinary GET handlers, those
rows exist in every database the API has ever served — including production.

The seeder is gone, but the rows it already wrote are not. This command removes
them. It matches on the seeder's own hardcoded primary keys, so it cannot touch
a genuine field record: real rows get UUID primary keys.

Dry run by default:

    python manage.py purge_fabricated_demo_data
    python manage.py purge_fabricated_demo_data --execute
"""
from django.core.management.base import BaseCommand
from django.db import transaction

from apps.digital_eye.models import (
    BIMStructuralElement, DeviceReportRecord, GPRScan, PUNDITTest, TrimbleConnection,
)

# The exact primary keys the removed seeder hardcoded. Genuine records are
# created with UUID primary keys, so these literals identify fabricated rows
# unambiguously — we never match on project name, which a real project could
# legitimately share.
FABRICATED_IDS = [
    (BIMStructuralElement, ['elem-001', 'elem-002', 'elem-003']),
    (GPRScan, ['gpr-001']),
    (PUNDITTest, ['pundit-001', 'pundit-02']),
    (DeviceReportRecord, ['rpt-pundit-01']),
    (TrimbleConnection, ['trimble-01']),
]


class Command(BaseCommand):
    help = ('Delete the fabricated "Eko Atlantic" demo rows written by the '
            'removed digital_eye seeder. Dry run unless --execute is passed.')

    def add_arguments(self, parser):
        parser.add_argument(
            '--execute',
            action='store_true',
            help='Actually delete the rows. Without this flag nothing is changed.',
        )

    def handle(self, *args, **options):
        execute = options['execute']

        found = []
        for model, ids in FABRICATED_IDS:
            for obj in model.objects.filter(id__in=ids):
                found.append((model, obj))

        if not found:
            self.stdout.write(self.style.SUCCESS(
                'No fabricated demo rows found — this database is clean.'))
            return

        self.stdout.write(self.style.WARNING(
            f'{len(found)} fabricated row(s) present:'))
        for model, obj in found:
            label = (getattr(obj, 'test_reference', None)
                     or getattr(obj, 'scan_reference', None)
                     or getattr(obj, 'report_reference', None)
                     or getattr(obj, 'name', None)
                     or '')
            project = getattr(obj, 'project_name', '') or ''
            self.stdout.write(
                f'  {model.__name__:24} id={obj.id:<14} {label}'
                + (f'  [{project}]' if project else ''))

        if not execute:
            self.stdout.write('')
            self.stdout.write(self.style.NOTICE(
                'Dry run — nothing deleted. Re-run with --execute to remove these rows.'))
            return

        with transaction.atomic():
            deleted = 0
            for model, obj in found:
                obj.delete()
                deleted += 1

        self.stdout.write('')
        self.stdout.write(self.style.SUCCESS(
            f'Deleted {deleted} fabricated row(s). Empty tables now honestly read as empty.'))
