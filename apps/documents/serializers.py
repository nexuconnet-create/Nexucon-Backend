from rest_framework import serializers
from .models import Document, Version, Approval

class VersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = Version
        fields = '__all__'

class ApprovalSerializer(serializers.ModelSerializer):
    class Meta:
        model = Approval
        fields = '__all__'

class DocumentSerializer(serializers.ModelSerializer):
    versions = VersionSerializer(many=True, read_only=True)
    approvals = ApprovalSerializer(many=True, read_only=True)

    class Meta:
        model = Document
        fields = '__all__'
