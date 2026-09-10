"""
Move projects with no recorded activity for a prolonged period (8 Sep 2026
meeting: 3-6 months) into cold storage. Nothing is deleted — the flag only
drops the project from the default hot browse lists; every record stays
directly reachable and a Director can restore it via the API.

Usage:
    python manage.py cold_store_inactive_projects            # flag, 6 months
    python manage.py cold_store_inactive_projects --min-inactive-months 3
    python manage.py cold_store_inactive_projects --dry-run  # report only
"""
from django.core.management.base import BaseCommand

from apps.projects.tasks import cold_store_inactive_projects


class Command(BaseCommand):
    help = ('Flag projects inactive for months as cold storage (records are '
            'never deleted — see the 8 Sep 2026 review meeting notes).')

    def add_arguments(self, parser):
        parser.add_argument(
            '--min-inactive-months', type=int, default=6,
            help='Inactivity threshold in months (meeting guidance: 3-6; '
                 'default: the conservative 6).')
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report the projects that would be flagged without '
                 'changing anything.')

    def handle(self, *args, **options):
        months = options['min_inactive_months']
        if months < 1:
            self.stderr.write('--min-inactive-months must be at least 1.')
            return
        flagged = cold_store_inactive_projects(
            min_inactive_months=months, dry_run=options['dry_run'])
        prefix = 'Would move' if options['dry_run'] else 'Moved'
        for project in flagged:
            self.stdout.write(f"{prefix} {project.reference_number} "
                              f"({project.name}) to cold storage; last "
                              f"activity {project.last_activity_at():%Y-%m-%d}.")
        self.stdout.write(f"{prefix} {len(flagged)} project(s) to cold "
                          f"storage (threshold: {months} months).")
