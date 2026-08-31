import uuid

from django.db import models


class ProcessingNode(models.Model):
    """
    A worker/host that runs the scan processing pipeline.

    The API host reports its own live CPU/memory via psutil; GPU figures can
    only come from a worker calling the heartbeat endpoint, so they stay null
    until one does rather than being invented.
    """
    STATUS_CHOICES = [
        ('healthy', 'Healthy'),
        ('degraded', 'Degraded'),
        ('offline', 'Offline'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    hostname = models.CharField(max_length=255, unique=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='healthy')
    cpu_utilization = models.FloatField(null=True, blank=True, help_text="Percent 0-100")
    gpu_utilization = models.FloatField(null=True, blank=True, help_text="Percent 0-100, reported by the worker")
    memory_used_gb = models.FloatField(null=True, blank=True)
    memory_total_gb = models.FloatField(null=True, blank=True)
    gpu_workers = models.IntegerField(null=True, blank=True, help_text="Allocated GPU worker processes")
    is_api_host = models.BooleanField(default=False, help_text="True for the node serving this API")
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['hostname']

    def __str__(self):
        return self.hostname


class AIModelVersion(models.Model):
    """
    Registry of the AI models the pipeline runs.

    `version` and `provider` are real deployment configuration. Accuracy is NOT
    stored as a hand-entered figure: it is computed from observed detection
    confidence (see `apps.processing.selectors.model_performance`), so the
    dashboard never shows a number nobody measured.
    """
    TASK_CHOICES = [
        ('structural_deviation', 'Structural Deviation'),
        ('thermal_anomaly', 'Thermal Anomaly'),
        ('rebar_detection', 'Rebar Detection'),
        ('clash_detection', 'Clash Detection'),
        ('progress_validation', 'Progress Validation'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=150, help_text="Display name, e.g. 'Structural Deviation Model'")
    task_type = models.CharField(max_length=50, choices=TASK_CHOICES)
    version = models.CharField(max_length=50, help_text="e.g. v2.4.1")
    provider = models.CharField(max_length=100, blank=True, default='', help_text="e.g. gemini, local-cnn")
    is_active = models.BooleanField(default=True)
    deployed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} {self.version}"
