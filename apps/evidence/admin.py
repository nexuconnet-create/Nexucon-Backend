from django.contrib import admin

from .models import AIAnalysisRecord, CorrelationFinding, EvidenceRecord, FindingRevision


@admin.register(EvidenceRecord)
class EvidenceRecordAdmin(admin.ModelAdmin):
    list_display = ('evidence_reference', 'project', 'source_type',
                    'structural_element_id', 'bim_guid', 'captured_at', 'created_at')
    list_filter = ('source_type', 'project')
    search_fields = ('evidence_reference', 'structural_element_id', 'bim_guid',
                     'source_model', 'source_id')
    readonly_fields = [f.name for f in EvidenceRecord._meta.fields]


@admin.register(AIAnalysisRecord)
class AIAnalysisRecordAdmin(admin.ModelAdmin):
    list_display = ('analysis_reference', 'project', 'analysis_type', 'risk_level',
                    'risk_score', 'requires_human_review', 'created_at')
    list_filter = ('analysis_type', 'risk_level', 'requires_human_review', 'project')
    search_fields = ('analysis_reference',)
    readonly_fields = [f.name for f in AIAnalysisRecord._meta.fields]


@admin.register(CorrelationFinding)
class CorrelationFindingAdmin(admin.ModelAdmin):
    list_display = ('finding_reference', 'project', 'structural_element_id',
                    'risk_level', 'risk_score', 'status', 'reviewed_by', 'created_at')
    list_filter = ('status', 'risk_level', 'project')
    search_fields = ('finding_reference', 'structural_element_id', 'bim_guid', 'title')
    readonly_fields = [f.name for f in CorrelationFinding._meta.fields]


@admin.register(FindingRevision)
class FindingRevisionAdmin(admin.ModelAdmin):
    list_display = ('finding', 'revision_number', 'change_reason', 'changed_by', 'recorded_at')
    list_filter = ('change_reason',)
    search_fields = ('finding__finding_reference',)
    readonly_fields = [f.name for f in FindingRevision._meta.fields]

    def has_add_permission(self, request):
        return False  # immutable append-only history
