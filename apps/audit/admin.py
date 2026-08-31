from django.contrib import admin
from .models import AuditEvent

@admin.register(AuditEvent)
class AuditEventAdmin(admin.ModelAdmin):
    pass

