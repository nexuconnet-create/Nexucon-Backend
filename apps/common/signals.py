import logging
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.core.cache import cache

logger = logging.getLogger(__name__)

def invalidate_cache(sender, instance, **kwargs):
    """
    Global cache invalidation signal handler.
    Clears the Django cache whenever key models are created, updated, or deleted.
    """
    try:
        cache.clear()
        logger.debug(f"Cache cleared due to change in model {sender.__name__} (id: {getattr(instance, 'id', None)})")
    except Exception as e:
        logger.error(f"Error clearing cache on {sender.__name__} signal: {e}")

def register_signals():
    """Dynamically register invalidation signals for domain models."""
    from apps.projects.models import Project, BIMModel
    from apps.scans.models import ScanSession, ScanMetadata, ScanPlan, Defect, ThermalAnomaly, ProgressValidationResult, ScanFile
    from apps.inspections.models import Issue, IssueComment
    from apps.audit.models import AuditEvent

    monitored_models = [
        Project,
        BIMModel,
        ScanSession,
        ScanMetadata,
        ScanPlan,
        Defect,
        ThermalAnomaly,
        ProgressValidationResult,
        ScanFile,
        Issue,
        IssueComment,
        AuditEvent,
    ]

    for model in monitored_models:
        post_save.connect(invalidate_cache, sender=model, weak=False)
        post_delete.connect(invalidate_cache, sender=model, weak=False)

register_signals()
