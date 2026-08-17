from rest_framework import viewsets
from .models import Inspection, Checklist, Finding
from .serializers import InspectionSerializer, ChecklistSerializer, FindingSerializer

class InspectionViewSet(viewsets.ModelViewSet):
    queryset = Inspection.objects.all().order_by('-scheduled_date')
    serializer_class = InspectionSerializer

class ChecklistViewSet(viewsets.ModelViewSet):
    queryset = Checklist.objects.all()
    serializer_class = ChecklistSerializer

class FindingViewSet(viewsets.ModelViewSet):
    queryset = Finding.objects.all().order_by('-created_at')
    serializer_class = FindingSerializer
