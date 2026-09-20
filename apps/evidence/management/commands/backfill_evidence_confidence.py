"""Backfill evidence confidence on pre-existing findings and evidence rows.

Before commit 912130c (7 Sep meeting item 6) the platform had no computed
evidence confidence: high-severity findings carried risk_score 0.78, and the
code paths of the day either stored that risk value in the evidence record's
confidence field or left it null. Existing database rows therefore still show
"78%" (or nothing) where newly created rows show an honest, computed
confidence of 0.90-0.95.

This command recomputes them. It never invents a number for evidence that
cannot carry one (e.g. records with no computable measurement): those stay
null, so the UI keeps hiding the badge for them.

Recalculation rules (same spirit as apps.evidence.ingestion.pundit_confidence
and the manual-finding completeness rule in CorrelationFindingViewSet.create):

- PUNDIT-sourced evidence (source_type 'pundit'): 0.90 base when velocity is
  computable, +0.03 multi-reading, +0.02 crack depth, capped at 0.95.
- Manual finding evidence (source_model digital_eye.ManualFinding): 0.93 when
  the logged payload carries technical parameters (depth_mm / deviation_mm),
  else 0.90.
- Any other evidence whose stored confidence is still a raw severity/risk
  number (0.78, the old HIGH mapping): recompute from the recorded severity
  with the corrected mapping (high -> 0.93), keeping 0.95 for critical.

Dry run by default:

    python manage.py backfill_evidence_confidence
    python manage.py backfill_evidence_confidence --execute

NOT affected by the `unverified` quality standing (Sep 2026): that standing
stops a physically implausible pulse velocity from being graded, and lives on
PUNDITTest.quality_grade. This command recomputes EvidenceRecord.confidence,
which `pundit_confidence` derives from measurement completeness alone — path
length, transit time, reading count, crack depth. It never reads a grade, so
a reading that became `unverified` keeps the same confidence it always had.
The rule below is imported from apps.evidence.ingestion rather than copied, so
the two cannot drift apart again.
"""
from django.core.management.base import BaseCommand

from apps.digital_eye.models import PUNDITTest
from apps.evidence.ingestion import pundit_confidence as pundit_confidence_for
from apps.evidence.models import EvidenceRecord

# The old severity->risk numbers that were being stored as "confidence".
OLD_RISK_VALUES = {0.78, 0.95, 0.50, 0.25, 0.20, 0.15}

SEVERITY_CONFIDENCE = {
    'critical': 0.95,
    'high': 0.93,
    'medium': 0.50,
    'low': 0.25,
}


def manual_confidence_for(payload):
    """Same rule as CorrelationFindingViewSet.create."""
    has_tech_params = (
        payload.get('depth_mm') is not None
        or payload.get('deviation_mm') is not None
    )
    return 0.93 if has_tech_params else 0.90


class Command(BaseCommand):
    help = ('Recompute evidence confidence on rows created before the '
            'evidence-based confidence code landed (dry run unless --execute).')

    def add_arguments(self, parser):
        parser.add_argument(
            '--execute',
            action='store_true',
            help='Actually write the new values. Without this flag nothing is changed.',
        )

    def handle(self, *args, **options):
        execute = options['execute']
        changes = []

        for record in EvidenceRecord.objects.exclude(confidence__isnull=True).iterator():
            payload = record.payload or {}

            new_conf = None
            if record.source_type == 'pundit' and record.source_id:
                try:
                    test = PUNDITTest.objects.filter(id=record.source_id).first()
                except (ValueError, TypeError):
                    test = None
                new_conf = pundit_confidence_for(test) if test else None
            elif record.source_model == 'digital_eye.ManualFinding':
                new_conf = manual_confidence_for(payload)
            elif record.confidence in OLD_RISK_VALUES:
                severity = (payload.get('severity') or '').lower()
                new_conf = SEVERITY_CONFIDENCE.get(severity)

            if new_conf is None or abs(new_conf - record.confidence) < 0.001:
                continue
            changes.append((record, new_conf))

        if not changes:
            self.stdout.write(self.style.SUCCESS(
                'No stale evidence confidence values found — nothing to backfill.'))
            return

        for record, new_conf in changes:
            self.stdout.write(
                f'{record.evidence_reference}: {record.confidence} -> {new_conf} '
                f'({record.source_type}/{record.source_model})')
            if execute:
                record.confidence = new_conf
                record.save(update_fields=['confidence'])

        # Re-hash so the record's integrity hash matches the new content.
        if execute:
            for record, _ in changes:
                record.evidence_hash = record.compute_hash()
                record.save(update_fields=['evidence_hash', 'updated_at'])
            self.stdout.write(self.style.SUCCESS(
                f'Backfilled {len(changes)} evidence record(s).'))
        else:
            self.stdout.write(self.style.WARNING(
                f'Dry run: {len(changes)} record(s) would be updated. '
                'Re-run with --execute to apply.'))
