from rest_framework import serializers
from .models import BIMModel, BIMModelVersion, BIMClash, BIMAnnotation, BIMProgressValidation

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
