from django.contrib import admin
from .models import Project, ProjectProfessional, ProjectDocument, ProjectMilestone, BIMModel

@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    pass

@admin.register(ProjectProfessional)
class ProjectProfessionalAdmin(admin.ModelAdmin):
    pass

@admin.register(ProjectDocument)
class ProjectDocumentAdmin(admin.ModelAdmin):
    pass

@admin.register(ProjectMilestone)
class ProjectMilestoneAdmin(admin.ModelAdmin):
    pass

@admin.register(BIMModel)
class BIMModelAdmin(admin.ModelAdmin):
    pass

