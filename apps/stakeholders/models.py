from django.db import models
from django.conf import settings
import uuid

class BaseStakeholder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    company_name = models.CharField(max_length=255, blank=True, null=True)
    registration_number = models.CharField(max_length=100, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_blacklisted = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


class Consultant(BaseStakeholder):
    specialty = models.CharField(max_length=100, help_text="e.g., Structural Engineer, Architect")


class Contractor(BaseStakeholder):
    class_category = models.CharField(max_length=50, help_text="e.g., Class A, Class B")


class Inspector(BaseStakeholder):
    agency = models.CharField(max_length=100, help_text="e.g., LASBCA")
    field_expertise = models.CharField(max_length=100)


class Certification(models.Model):
    """
    Professional certifications like COREN, CCPC.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='certifications')
    authority = models.CharField(max_length=100, help_text="e.g., COREN, ARCON, CCPC")
    license_number = models.CharField(max_length=100, unique=True)
    issue_date = models.DateField()
    expiry_date = models.DateField()
    is_verified = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.authority} - {self.license_number} ({self.user.email})"


class TrainingRecord(models.Model):
    """
    Inspector training and certification tracking (e.g., LABCA-certified inspectors).
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    inspector = models.ForeignKey(Inspector, on_delete=models.CASCADE, related_name='trainings')
    course_name = models.CharField(max_length=200)
    completion_date = models.DateField()
    certificate_url = models.URLField(blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.course_name} - {self.inspector.user.email}"
