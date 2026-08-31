from celery import shared_task
import logging
from apps.notifications.models import WebhookEndpoint
from apps.notifications.services import WebhookService

logger = logging.getLogger(__name__)

@shared_task(name="apps.notifications.tasks.dispatch_webhooks")
def dispatch_webhooks(event_type: str, payload: dict, project_id: str = None):
    """
    Asynchronously finds all active webhooks for a project (and global ones) 
    and dispatches the payload to them.
    """
    logger.info(f"Dispatching webhooks for event {event_type}")
    
    endpoints = WebhookEndpoint.objects.filter(is_active=True)
    if project_id:
        endpoints = endpoints.filter(project_id=project_id) | endpoints.filter(project__isnull=True)
    else:
        endpoints = endpoints.filter(project__isnull=True)

    success_count = 0
    for endpoint in endpoints:
        delivery = WebhookService.send_webhook(endpoint, event_type, payload)
        if delivery.success:
            success_count += 1

    logger.info(f"Dispatched {event_type} to {endpoints.count()} endpoints. {success_count} successful.")
    return success_count
