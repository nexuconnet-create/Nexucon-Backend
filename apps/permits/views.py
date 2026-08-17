from rest_framework import viewsets
from .models import Permit
from .serializers import PermitSerializer

class PermitViewSet(viewsets.ModelViewSet):
    queryset = Permit.objects.all().order_by('-created_at')
    serializer_class = PermitSerializer
