from django.db import models
from django.utils import timezone

class SoftDeleteManager(models.Manager):
    """
    Only returns objects that have not been soft-deleted.
    """
    def get_queryset(self):
        return super().get_queryset().filter(is_deleted=False)

class AllObjectsManager(models.Manager):
    """
    Returns all objects, including soft-deleted ones.
    """
    def get_queryset(self):
        return super().get_queryset()

class SoftDeleteModel(models.Model):
    """
    Abstract model that implements soft deletion.
    Calling delete() will update `is_deleted` and `deleted_at`
    instead of removing the record from the database.
    """
    is_deleted = models.BooleanField(default=False)
    deleted_at = models.DateTimeField(null=True, blank=True)

    objects = SoftDeleteManager()
    all_objects = AllObjectsManager()

    class Meta:
        abstract = True

    def delete(self, using=None, keep_parents=False):
        self.is_deleted = True
        self.deleted_at = timezone.now()
        self.save(update_fields=['is_deleted', 'deleted_at'])

    def restore(self):
        self.is_deleted = False
        self.deleted_at = None
        self.save(update_fields=['is_deleted', 'deleted_at'])
