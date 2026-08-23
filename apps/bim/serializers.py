from rest_framework import serializers
from .models import (
    BIMModel, BIMModelVersion, BIMClash, BIMAnnotation, 
    BIMProgressValidation, BIMConstructionMilestone
)

class BIMModelVersionSerializer(serializers.ModelSerializer):
    model_name = serializers.CharField(source='model.name', read_only=True)

    class Meta:
        model = BIMModelVersion
        fields = '__all__'
        read_only_fields = ('id', 'created_at')


class BIMAnnotationSerializer(serializers.ModelSerializer):
    model_name = serializers.CharField(source='model.name', read_only=True)
    project_name = serializers.CharField(source='project.name', read_only=True)

    class Meta:
        model = BIMAnnotation
        fields = '__all__'
        read_only_fields = ('id', 'annotation_reference', 'created_at', 'updated_at')


class BIMClashSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    primary_model_name = serializers.CharField(source='primary_model.name', read_only=True)
    secondary_model_name = serializers.CharField(source='secondary_model.name', read_only=True, default=None)

    class Meta:
        model = BIMClash
        fields = '__all__'
        read_only_fields = ('id', 'clash_reference', 'created_at', 'updated_at')


class BIMProgressValidationSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    model_name = serializers.CharField(source='model.name', read_only=True, default=None)

    class Meta:
        model = BIMProgressValidation
        fields = '__all__'
        read_only_fields = ('id', 'created_at', 'updated_at')


class BIMModelSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    versions = BIMModelVersionSerializer(many=True, read_only=True)
    versions_count = serializers.SerializerMethodField()
    clashes_count = serializers.SerializerMethodField()
    annotations_count = serializers.SerializerMethodField()

    class Meta:
        model = BIMModel
        fields = '__all__'
        read_only_fields = ('id', 'model_reference', 'created_at', 'updated_at')

    def get_versions_count(self, obj):
        return obj.versions.count()

    def get_clashes_count(self, obj):
        return obj.primary_clashes.filter(status='OPEN').count()

    def get_annotations_count(self, obj):
        return obj.annotations.filter(status='Open').count()


class BIMConstructionMilestoneSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    bim_model_name = serializers.CharField(source='bim_model.name', read_only=True)
    bim_model_discipline = serializers.CharField(source='bim_model.discipline', read_only=True)
    bim_model_certified = serializers.BooleanField(source='bim_model.is_digitally_certified', read_only=True)
    bim_model_status = serializers.CharField(source='bim_model.status', read_only=True)
    model_version_label = serializers.CharField(source='model_version.version_label', read_only=True, default=None)
    model_version_hash = serializers.CharField(source='model_version.commit_hash', read_only=True, default=None)
    gate_checks_summary = serializers.SerializerMethodField()

    class Meta:
        model = BIMConstructionMilestone
        fields = '__all__'
        read_only_fields = ('id', 'milestone_code', 'created_at', 'updated_at')

    def get_gate_checks_summary(self, obj):
        model_approved = bool(obj.bim_model and obj.bim_model.is_digitally_certified and obj.bim_model.status == 'Approved')
        version_verified = bool(obj.model_version and (obj.model_version.is_current or obj.model_version.version_label == obj.bim_model.current_version))
        
        clashes = obj.linked_clashes or []
        open_critical_clashes = sum(1 for c in clashes if str(c.get('severity', '')).upper() in ('CRITICAL', 'HIGH') and str(c.get('status', '')).upper() in ('OPEN', 'ASSIGNED', 'IN_REVIEW'))
        zero_critical_clashes = (open_critical_clashes == 0)
        
        tolerance_compliant = (obj.bim_deviation_mm <= obj.tolerance_max_mm)
        
        inspections = obj.linked_inspections or []
        inspections_passed = (len(inspections) == 0 or all(str(i.get('outcome', '')).upper() in ('PASSED', 'CONDITIONAL_PASS') for i in inspections))
        
        gpr_clear = obj.gpr_clearance_status in ('VERIFIED', 'NOT_APPLICABLE')
        
        all_passed = (model_approved and version_verified and zero_critical_clashes and tolerance_compliant and inspections_passed and gpr_clear)
        
        return {
            "model_approved": model_approved,
            "version_verified": version_verified,
            "zero_critical_clashes": zero_critical_clashes,
            "open_critical_clashes_count": open_critical_clashes,
            "tolerance_compliant": tolerance_compliant,
            "inspections_passed": inspections_passed,
            "gpr_clear": gpr_clear,
            "all_gates_passed": all_passed,
            "is_stamped": bool(obj.digital_stamp_reference and obj.verified_at)
        }

