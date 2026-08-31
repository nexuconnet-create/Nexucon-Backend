from django.contrib import admin
from .models import DailySiteUpdate, FieldObservation, SiteIssue, ConstructionMilestone, SiteVerification

@admin.register(DailySiteUpdate)
class DailySiteUpdateAdmin(admin.ModelAdmin):
    pass

@admin.register(FieldObservation)
class FieldObservationAdmin(admin.ModelAdmin):
    pass

@admin.register(SiteIssue)
class SiteIssueAdmin(admin.ModelAdmin):
    pass

@admin.register(ConstructionMilestone)
class ConstructionMilestoneAdmin(admin.ModelAdmin):
    pass

@admin.register(SiteVerification)
class SiteVerificationAdmin(admin.ModelAdmin):
    pass

