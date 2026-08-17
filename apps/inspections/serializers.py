from rest_framework import serializers
from .models import Inspection, Checklist, Finding

class ChecklistSerializer(serializers.ModelSerializer):
    class Meta:
        model = Checklist
        fields = '__all__'

class FindingSerializer(serializers.ModelSerializer):
    class Meta:
        model = Finding
        fields = '__all__'

class InspectionSerializer(serializers.ModelSerializer):
    checklists = ChecklistSerializer(many=True, read_only=True)
    findings = FindingSerializer(many=True, read_only=True)

    class Meta:
        model = Inspection
        fields = '__all__'
