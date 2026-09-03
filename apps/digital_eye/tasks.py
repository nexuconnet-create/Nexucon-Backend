"""
Digital Eye background tasks (implementation plan §5 Weeks 2–3).

Celery tasks for the Trimble Connect background sync and connection health
checks. Scheduled through the Celery beat schedule in config/celery.py.
"""
import logging

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name='digital_eye.trimble_health_checks')
def trimble_health_checks():
    """
    Run an authenticated health check on every connected Trimble connection
    and record the outcome (plan §5 Week 1: integration health checks and
    alerting).
    """
    from apps.digital_eye.models import TrimbleConnection
    from integrations.trimble import TrimbleClient

    results = []
    client = TrimbleClient()
    for connection in TrimbleConnection.objects.exclude(status='disconnected'):
        healthy, detail = client.health_check(connection)
        results.append({
            'connection': str(connection.id),
            'name': connection.name,
            'healthy': healthy,
            'detail': detail,
        })
        if not healthy:
            logger.warning('Trimble health check FAILED for %s: %s', connection.name, detail)
    logger.info('Trimble health checks completed: %s connection(s)', len(results))
    return results


@shared_task(name='digital_eye.trimble_sync')
def trimble_sync():
    """
    Background sync of Trimble Connect projects and BIM GUID mappings
    (plan §5 Week 2: "AI evidence ingestion pipeline, Celery/Redis sync").
    """
    from apps.digital_eye.models import TrimbleConnection
    from integrations.trimble import TrimbleError, TrimbleSyncService

    service = TrimbleSyncService()
    summary = {'connections': 0, 'models': 0, 'elements': 0, 'errors': []}
    for connection in TrimbleConnection.objects.filter(status='connected'):
        summary['connections'] += 1
        try:
            results = service.sync_all_projects(connection)
            for result in results:
                summary['models'] += result.get('models', 0)
                summary['elements'] += result.get('elements', 0)
                summary['errors'].extend(result.get('errors', []))
        except TrimbleError as exc:
            logger.warning('Trimble sync failed for %s: %s', connection.name, exc)
            summary['errors'].append(str(exc))
    logger.info('Trimble sync completed: %s', summary)
    return summary
