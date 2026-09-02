from rest_framework import serializers
from .models import (
    BIMStructuralElement,
    TrimbleConnection,
    GPRScan,
    PunditTest,
    DigitalEyeFinding,
    AIAnalysisRecord,
    ProcessingQueueJob,
    EvidenceSpatialPoint,
    DeviceReportRecord,
)


class BIMStructuralElementSerializer(serializers.ModelSerializer):
    class Meta:
        model = BIMStructuralElement
        fields = '__all__'


class TrimbleConnectionSerializer(serializers.ModelSerializer):
    class Meta:
        model = TrimbleConnection
        fields = '__all__'


class GPRScanSerializer(serializers.ModelSerializer):
    class Meta:
        model = GPRScan
        fields = '__all__'


class PunditTestSerializer(serializers.ModelSerializer):
    class Meta:
        model = PunditTest
        fields = '__all__'


class DigitalEyeFindingSerializer(serializers.ModelSerializer):
    class Meta:
        model = DigitalEyeFinding
        fields = '__all__'


class AIAnalysisRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIAnalysisRecord
        fields = '__all__'


class ProcessingQueueJobSerializer(serializers.ModelSerializer):
    class Meta:
        model = ProcessingQueueJob
        fields = '__all__'


class EvidenceSpatialPointSerializer(serializers.ModelSerializer):
    class Meta:
        model = EvidenceSpatialPoint
        fields = '__all__'


class DeviceReportRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = DeviceReportRecord
        fields = '__all__'
