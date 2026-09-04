"""
Evidence pipeline background tasks (implementation plan §5 Weeks 2–4, 9).

  * correlate_projects      — re-ingest + correlate evidence for active
                              projects (the AI evidence ingestion pipeline).
  * detect_recurring_anomalies — recurring anomaly detection: flag
                              structural elements that keep generating
                              findings across runs / sources, and notify the
                              responsible reviewers (plan §5 Week 9).
"""
import logging

from celery import shared_task
from django.db.models import Count

logger = logging.getLogger(__name__)


@shared_task(name='evidence.correlate_projects')
def correlate_projects():
    """
    Run the cross-source correlation engine for every project with evidence
    records (idempotent — reviewed findings are never overwritten).
    """
    from apps.projects.models import Project
    from .correlation import CorrelationEngine
    from .models import EvidenceRecord

    project_ids = (EvidenceRecord.objects.values_list('project_id', flat=True)
                   .distinct())
    results = []
    for project in Project.objects.filter(id__in=project_ids):
        try:
            findings = CorrelationEngine.run(project, ingested_by=None)
            results.append({'project': str(project.id), 'findings': len(findings)})
        except Exception as exc:  # noqa: BLE001 — per-project isolation
            logger.exception('Correlation run failed for %s', project)
            results.append({'project': str(project.id), 'error': str(exc)})
    logger.info('Correlation run completed for %s project(s)', len(results))
    return results


@shared_task(name='evidence.detect_recurring_anomalies')
def detect_recurring_anomalies():
    """
    Recurring anomaly detection (plan §5 Week 9): identify structural
    elements with multiple correlation findings that remain unaccepted or
    keep re-appearing — these indicate a persistent problem rather than a
    one-off. Creates in-app notifications for the district/HQ reviewers.
    """
    from apps.notifications.models import InAppNotification
    from .models import CorrelationFinding

    recurring = (CorrelationFinding.objects
                 .exclude(status='rejected')
                 .values('project_id', 'structural_element_id')
                 .annotate(finding_count=Count('id'))
                 .filter(finding_count__gte=2)
                 .exclude(structural_element_id=''))
    flagged = []
    for row in recurring:
        element_findings = CorrelationFinding.objects.filter(
            project_id=row['project_id'],
            structural_element_id=row['structural_element_id'],
        ).exclude(status='rejected')
        worst = element_findings.order_by('-risk_score').first()
        if not worst:
            continue
        flagged.append({
            'project_id': str(row['project_id']),
            'structural_element_id': row['structural_element_id'],
            'findings': row['finding_count'],
            'worst_risk': worst.risk_level,
        })
        InAppNotification.objects.create(
            project_id=row['project_id'],
            type='warning' if worst.risk_level in ('critical', 'high') else 'info',
            title=f"Recurring anomaly: {row['structural_element_id']}",
            message=(f"{row['finding_count']} correlation findings on element "
                     f"{row['structural_element_id']} (worst risk "
                     f"{worst.risk_level.upper()}) — persistent issue requiring review."),
            related_entity_id=worst.id,
        )
    logger.info('Recurring anomaly detection: %s element(s) flagged', len(flagged))
    return flagged
