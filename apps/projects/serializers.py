from rest_framework import serializers
from .models import Project, ProjectMilestone, ProjectProfessional, ProjectDocument

class ProjectMilestoneSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectMilestone
        fields = '__all__'

class ProjectProfessionalSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectProfessional
        fields = ['id', 'name', 'organization', 'license_number', 'email', 'phone', 'role']
        
class ProjectDocumentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProjectDocument
        fields = ['id', 'document_type', 'file', 'name', 'uploaded_at']

class ProjectSerializer(serializers.ModelSerializer):
    milestones = ProjectMilestoneSerializer(many=True, read_only=True)
    professionals = ProjectProfessionalSerializer(many=True, required=False)
    project_documents = ProjectDocumentSerializer(many=True, read_only=True)
    last_activity_at = serializers.SerializerMethodField()
    # The zone's name alongside the `district` pk, so a consumer never has to
    # resolve the FK itself. Resolving it client-side would be wrong for a
    # project on a retired zone: the zone register is fetched active-only, so
    # the lookup would miss and the project would read as unattached when it
    # is in fact still scoped to a zone.
    district_name = serializers.SerializerMethodField()
    estimated_project_value_display = serializers.SerializerMethodField()

    class Meta:
        model = Project
        fields = '__all__'

    def get_last_activity_at(self, obj):
        """Newest real activity (own edits or captured records) — drives the
        cold-storage policy."""
        return obj.last_activity_at()

    def get_district_name(self, obj):
        return obj.district.name if obj.district_id else None

    def get_estimated_project_value_display(self, obj):
        if getattr(obj, 'is_estimated_value_na', False):
            return "Not Applicable"
        if obj.estimated_project_value is not None:
            return f"{obj.estimated_project_value:,.2f}"
        return None

    def to_internal_value(self, data):
        data = data.copy() if hasattr(data, 'copy') else dict(data)
        val = data.get('estimated_project_value')
        if isinstance(val, str) and val.strip().lower() in ('n/a', 'not applicable', 'not_applicable', 'na', 'none'):
            data['estimated_project_value'] = None
            data['is_estimated_value_na'] = True
        elif data.get('is_estimated_value_na') in (True, 'true', 'True', 1, '1'):
            data['estimated_project_value'] = None
            data['is_estimated_value_na'] = True
        elif val == '':
            data['estimated_project_value'] = None
        return super().to_internal_value(data)

    def create(self, validated_data):
        professionals_data = validated_data.pop('professionals', [])
        project = Project.objects.create(**validated_data)
        
        for prof_data in professionals_data:
            ProjectProfessional.objects.create(project=project, **prof_data)

        try:
            from apps.stakeholders.services import StakeholderService
            StakeholderService.sync_project_team(project)
        except Exception:
            pass
            
        return project

    def update(self, instance, validated_data):
        professionals_data = validated_data.pop('professionals', None)
        
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        instance.save()
        
        if professionals_data is not None:
            instance.professionals.all().delete()
            for prof_data in professionals_data:
                ProjectProfessional.objects.create(project=instance, **prof_data)

        try:
            from apps.stakeholders.services import StakeholderService
            StakeholderService.sync_project_team(instance)
        except Exception:
            pass
                
        return instance


from .models import BIMModel
class BIMModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = BIMModel
        fields = ['id', 'project', 'name', 'version', 'file', 'file_url', 'file_format', 'uploaded_by', 'created_at']
        read_only_fields = ['id', 'created_at', 'file_url', 'name', 'file_format', 'project']