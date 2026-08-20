from rest_framework import serializers
from django.utils import timezone
import datetime
from .models import Document, Version, Approval, DocumentTemplate, DocumentFolder

class VersionSerializer(serializers.ModelSerializer):
    document_title = serializers.CharField(source='document.title', read_only=True)

    class Meta:
        model = Version
        fields = '__all__'
        read_only_fields = ('id', 'uploaded_at')


class ApprovalSerializer(serializers.ModelSerializer):
    document_title = serializers.CharField(source='document.title', read_only=True)
    document_reference = serializers.CharField(source='document.document_reference', read_only=True)

    class Meta:
        model = Approval
        fields = '__all__'
        read_only_fields = ('id', 'approval_reference', 'reviewed_at')


class DocumentTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentTemplate
        fields = '__all__'
        read_only_fields = ('id', 'created_at')


class DocumentFolderSerializer(serializers.ModelSerializer):
    class Meta:
        model = DocumentFolder
        fields = '__all__'
        read_only_fields = ('id', 'created_at')


class DocumentSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    project_reference = serializers.CharField(source='project.reference_number', read_only=True)
    versions = VersionSerializer(many=True, read_only=True)
    approvals = ApprovalSerializer(source='approval_records', many=True, read_only=True)
    versions_count = serializers.SerializerMethodField()
    expiry_status = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = '__all__'
        read_only_fields = ('id', 'document_reference', 'created_at', 'updated_at')

    def get_versions_count(self, obj):
        return obj.versions.count()

    def get_expiry_status(self, obj):
        if not obj.expiry_date:
            return 'valid'
        today = timezone.now().date()
        if obj.expiry_date < today:
            return 'expired'
        elif obj.expiry_date <= today + datetime.timedelta(days=30):
            return 'expiring_soon'
        return 'valid'
