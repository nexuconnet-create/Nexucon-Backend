from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.forms.models import model_to_dict
from apps.projects.models import Project
from apps.permits.models import Permit
from .models import AuditEvent
from common.middleware.audit import get_current_user, get_current_ip

# Store previous state before save
@receiver(pre_save, sender=Project)
@receiver(pre_save, sender=Permit)
def store_previous_state(sender, instance, **kwargs):
    if instance.pk:
        try:
            old_instance = sender.objects.get(pk=instance.pk)
            instance._previous_state = model_to_dict(old_instance)
        except sender.DoesNotExist:
            instance._previous_state = None
    else:
        instance._previous_state = None

def sanitize_state(state):
    if not state:
        return state
    sanitized = {}
    for k, v in state.items():
        if hasattr(v, 'wkt'):
            sanitized[k] = v.wkt
        elif hasattr(v, 'isoformat'):  # For datetime/date
            sanitized[k] = v.isoformat()
        elif isinstance(v, (dict, list, str, int, float, bool, type(None))):
            sanitized[k] = v
        else:
            sanitized[k] = str(v)
    return sanitized

@receiver(post_save, sender=Project)
@receiver(post_save, sender=Permit)
def audit_model_changes(sender, instance, created, **kwargs):
    user = get_current_user()
    ip_address = get_current_ip()
    
    action = "Created" if created else "Updated"
    new_state = model_to_dict(instance)
    
    # Filter out large or unjsonable fields if necessary
    if 'documents' in new_state:
        del new_state['documents']
        
    previous_state = getattr(instance, '_previous_state', None)
    
    AuditEvent.objects.create(
        user=user,
        action=action,
        resource_type=sender.__name__,
        resource_id=str(instance.pk),
        ip_address=ip_address,
        previous_state=sanitize_state(previous_state),
        new_state=sanitize_state(new_state)
    )
