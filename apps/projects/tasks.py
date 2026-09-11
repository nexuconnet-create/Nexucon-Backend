"""
Cold-storage policy tasks (8 Sep 2026 review meeting): projects with no
recorded activity for 3-6 months move to cold storage. Nothing is ever
deleted — a cold project keeps every statutory record and remains directly
reachable; it only drops out of the default hot browse lists.
"""
import logging

from celery import shared_task
from django.utils import timezone

from .models import Project

logger = logging.getLogger(__name__)


def cold_store_inactive_projects(min_inactive_months=6, dry_run=False,
                                 actor=None):
    """
    Flag every hot project whose last real activity is older than
    ``min_inactive_months`` months (8 Sep meeting guidance: 3-6 months; the
    default is the conservative end). Returns the list of projects flagged
    (or, with dry_run, the list that WOULD be flagged).

    Activity is measured from the project's own edits and the newest record
    captured against it — never a guessed date.
    """
    import datetime
    cutoff = timezone.now() - datetime.timedelta(
        days=30 * min_inactive_months)
    flagged = []
    for project in Project.objects.filter(cold_storage=False):
        last = project.last_activity_at()
        if last is None or last >= cutoff:
            continue
        if dry_run:
            flagged.append(project)
            continue
        project.cold_storage = True
        project.cold_stored_at = timezone.now()
        project.save(update_fields=['cold_storage', 'cold_stored_at',
                                    'updated_at'])
        _audit_cold_storage(project, actor)
        flagged.append(project)
        logger.info('project %s moved to cold storage (last activity %s)',
                    project.reference_number, last.isoformat())
    return flagged


def _audit_cold_storage(project, actor):
    """Immutable audit trail for every cold-storage transition."""
    try:
        from apps.audit.models import AuditEvent
        from common.permissions import user_role_name
        user = actor if (actor is not None and actor.is_authenticated) else None
        AuditEvent.objects.create(
            user=user,
            user_name=(user.get_full_name() or user.email) if user else 'System',
            user_role=user_role_name(user) if user else 'System',
            action='projects.project.cold_storage',
            resource_type='Project',
            resource_id=str(project.id),
            metadata={'name': project.name, 'last_activity_at':
                      project.last_activity_at().isoformat()},
        )
    except Exception:  # noqa: BLE001 — audit failure never blocks the policy
        logger.exception('cold-storage audit write failed for %s',
                         project.reference_number)


@shared_task(name='projects.cold_store_inactive_projects')
def cold_store_inactive_projects_task(min_inactive_months=6):
    """Monthly beat entry (1st of the month)."""
    flagged = cold_store_inactive_projects(min_inactive_months)
    logger.info('cold-storage pass flagged %d project(s)', len(flagged))
    return len(flagged)
