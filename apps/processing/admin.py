from django.contrib import admin

# Register your models here.
from .models import ProcessingNode, AIModelVersion

@admin.register(ProcessingNode)
class ProcessingNodeAdmin(admin.ModelAdmin):
    pass

@admin.register(AIModelVersion)
class AIModelVersionAdmin(admin.ModelAdmin):
    pass

