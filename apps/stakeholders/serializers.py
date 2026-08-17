from rest_framework import serializers
from .models import Consultant, Contractor, Inspector, Certification, TrainingRecord

class CertificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Certification
        fields = '__all__'

class TrainingRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrainingRecord
        fields = '__all__'

class ConsultantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Consultant
        fields = '__all__'

class ContractorSerializer(serializers.ModelSerializer):
    class Meta:
        model = Contractor
        fields = '__all__'

class InspectorSerializer(serializers.ModelSerializer):
    trainings = TrainingRecordSerializer(many=True, read_only=True)

    class Meta:
        model = Inspector
        fields = '__all__'
