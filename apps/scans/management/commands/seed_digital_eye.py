"""
Seed the Digital Eye registries from real data.

Idempotent: safe to re-run. Deliberately seeds only *configuration* (which
devices exist, which models are deployed, which report types are offered) and
never fabricates measurements — accuracy and GNSS figures are computed from, or
reported by, real sources.
"""
from django.conf import settings
from django.core.management.base import BaseCommand

from apps.processing.models import AIModelVersion
from apps.reports.models import ReportTemplate
from apps.scans.models import ScanSession, Scanner

ACTIVE_STATES = ('initialized', 'uploading', 'processing')

AI_MODELS = [
    ('Structural Deviation Model', 'structural_deviation', 'v2.4.1'),
    ('Thermal Anomaly Model', 'thermal_anomaly', 'v1.1.0'),
    ('Rebar Detection Model', 'rebar_detection', 'v3.0.2'),
]

REPORT_TEMPLATES = [
    ('Progress Report', 'progress',
     'Standard weekly site progression summary with before/after views.', 1),
    ('Deviation Analysis', 'deviation',
     'Detailed breakdown of As-Built vs BIM anomalies.', 2),
    ('QA/QC Summary', 'qaqc',
     'Tersus S1 hardware calibration and data quality metrics.', 3),
    ('Earthworks Volume', 'earthworks',
     'Cut/fill calculations from topographic scans.', 4),
]


class Command(BaseCommand):
    help = "Seed scanner registry, AI model registry and report templates."

    def handle(self, *args, **options):
        # 1. Scanners, derived from scanner_id values real sessions reference.
        created_scanners = 0
        device_ids = (
            ScanSession.objects.exclude(scanner_id='')
            .values_list('scanner_id', flat=True)
            .distinct()
        )
        for device_id in device_ids:
            latest = (
                ScanSession.objects.filter(scanner_id=device_id)
                .order_by('-created_at')
                .first()
            )
            scanner, was_created = Scanner.objects.get_or_create(
                device_id=device_id,
                defaults={'model': 'Tersus MVP S1'},
            )
            created_scanners += int(was_created)
            if latest:
                scanner.last_seen = latest.created_at
                scanner.status = 'online' if latest.status in ACTIVE_STATES else 'idle'
                scanner.save(update_fields=['last_seen', 'status', 'updated_at'])
        self.stdout.write(
            f"Scanners: {created_scanners} created, {Scanner.objects.count()} total"
        )

        # 2. AI model registry. Version/provider is deployment configuration;
        #    accuracy is never stored, it is computed from real detections.
        provider = getattr(settings, 'AI_PROVIDER', '') or ''
        created_models = 0
        for name, task_type, version in AI_MODELS:
            _, was_created = AIModelVersion.objects.get_or_create(
                task_type=task_type,
                defaults={'name': name, 'version': version, 'provider': provider},
            )
            created_models += int(was_created)
        self.stdout.write(
            f"AI models: {created_models} created, {AIModelVersion.objects.count()} total"
        )

        # 3. Report template catalogue.
        created_templates = 0
        for name, report_type, description, order in REPORT_TEMPLATES:
            _, was_created = ReportTemplate.objects.get_or_create(
                report_type=report_type,
                defaults={'name': name, 'description': description, 'sort_order': order},
            )
            created_templates += int(was_created)
        self.stdout.write(
            f"Report templates: {created_templates} created, "
            f"{ReportTemplate.objects.count()} total"
        )
        self.stdout.write(self.style.SUCCESS("Digital Eye registries seeded."))
