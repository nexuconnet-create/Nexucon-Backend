from django.contrib import admin
from .models import Developer, Contractor, Consultant, Inspector, LicensedProfessional, ProjectStakeholderTeam, BlacklistRecord, StakeholderMeeting, StakeholderMessage, Certification, TrainingRecord

@admin.register(Developer)
class DeveloperAdmin(admin.ModelAdmin):
    pass

@admin.register(Contractor)
class ContractorAdmin(admin.ModelAdmin):
    pass

@admin.register(Consultant)
class ConsultantAdmin(admin.ModelAdmin):
    pass

@admin.register(Inspector)
class InspectorAdmin(admin.ModelAdmin):
    pass

@admin.register(LicensedProfessional)
class LicensedProfessionalAdmin(admin.ModelAdmin):
    pass

@admin.register(ProjectStakeholderTeam)
class ProjectStakeholderTeamAdmin(admin.ModelAdmin):
    pass

@admin.register(BlacklistRecord)
class BlacklistRecordAdmin(admin.ModelAdmin):
    pass

@admin.register(StakeholderMeeting)
class StakeholderMeetingAdmin(admin.ModelAdmin):
    pass

@admin.register(StakeholderMessage)
class StakeholderMessageAdmin(admin.ModelAdmin):
    pass

@admin.register(Certification)
class CertificationAdmin(admin.ModelAdmin):
    pass

@admin.register(TrainingRecord)
class TrainingRecordAdmin(admin.ModelAdmin):
    pass

