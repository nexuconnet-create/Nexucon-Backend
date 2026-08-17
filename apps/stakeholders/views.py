from rest_framework import viewsets
from .models import Consultant, Contractor, Inspector, Certification, TrainingRecord
from .serializers import ConsultantSerializer, ContractorSerializer, InspectorSerializer, CertificationSerializer, TrainingRecordSerializer

class ConsultantViewSet(viewsets.ModelViewSet):
    queryset = Consultant.objects.all().order_by('-created_at')
    serializer_class = ConsultantSerializer

class ContractorViewSet(viewsets.ModelViewSet):
    queryset = Contractor.objects.all().order_by('-created_at')
    serializer_class = ContractorSerializer

class InspectorViewSet(viewsets.ModelViewSet):
    queryset = Inspector.objects.all().order_by('-created_at')
    serializer_class = InspectorSerializer

class CertificationViewSet(viewsets.ModelViewSet):
    queryset = Certification.objects.all().order_by('-created_at')
    serializer_class = CertificationSerializer

class TrainingRecordViewSet(viewsets.ModelViewSet):
    queryset = TrainingRecord.objects.all().order_by('-created_at')
    serializer_class = TrainingRecordSerializer
