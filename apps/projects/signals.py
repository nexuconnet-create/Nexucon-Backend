import logging
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from .models import Project, ProjectProfessional

logger = logging.getLogger(__name__)

@receiver(post_save, sender=Project)
def on_project_saved(sender, instance, created, **kwargs):
    try:
        from apps.stakeholders.services import StakeholderService
        StakeholderService.sync_project_team(instance)
    except Exception as e:
        logger.warning(f"Error syncing stakeholder team on project save ({getattr(instance, 'reference_number', '')}): {e}")

@receiver(post_delete, sender=Project)
def on_project_deleted(sender, instance, **kwargs):
    try:
        from apps.stakeholders.models import ProjectStakeholderTeam
        ProjectStakeholderTeam.objects.filter(project_reference=instance.reference_number).delete()
    except Exception as e:
        logger.warning(f"Error deleting stakeholder team on project delete ({getattr(instance, 'reference_number', '')}): {e}")

@receiver(post_save, sender=ProjectProfessional)
def on_project_professional_saved(sender, instance, **kwargs):
    try:
        if instance.project:
            from apps.stakeholders.services import StakeholderService
            StakeholderService.sync_project_team(instance.project)
    except Exception as e:
        logger.warning(f"Error syncing stakeholder team on professional save: {e}")

@receiver(post_delete, sender=ProjectProfessional)
def on_project_professional_deleted(sender, instance, **kwargs):
    try:
        if instance.project:
            from apps.stakeholders.services import StakeholderService
            StakeholderService.sync_project_team(instance.project)
    except Exception as e:
        logger.warning(f"Error syncing stakeholder team on professional delete: {e}")
