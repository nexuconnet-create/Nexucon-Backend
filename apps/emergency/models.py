from django.db import models
from apps.projects.models import Project
import uuid

class EmergencyEvent(models.Model):
    """
    Critical emergency events (e.g., Building Collapse).
    """
    EVENT_TYPES = (
        ('COLLAPSE', 'Building Collapse'),
        ('FIRE', 'Fire Incident'),
        ('STRUCTURAL_FAILURE', 'Critical Structural Failure'),
        ('OTHER', 'Other Emergency'),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='emergencies')
    event_type = models.CharField(max_length=50, choices=EVENT_TYPES)
    description = models.TextField()
    reported_at = models.DateTimeField(auto_now_add=True)
    is_active = models.BooleanField(default=True)

    def __str__(self):
        return f"{self.event_type} at {self.project.name}"


class ResponderDispatch(models.Model):
    """
    Log of automated dispatches to emergency services (Fire, Police).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    event = models.ForeignKey(EmergencyEvent, on_delete=models.CASCADE, related_name='dispatches')
    service_name = models.CharField(max_length=100, help_text="e.g., Fire Service, Police, Paramedics")
    dispatched_at = models.DateTimeField(auto_now_add=True)
    payload_sent = models.JSONField(help_text="GIS location + project details sent")
    status = models.CharField(max_length=50, default='SENT')

    def __str__(self):
        return f"Dispatch to {self.service_name} for {self.event.project.name}"
