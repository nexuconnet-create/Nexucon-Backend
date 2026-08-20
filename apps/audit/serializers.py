from rest_framework import serializers
from .models import AuditEvent

class AuditEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditEvent
        fields = '__all__'
        read_only_fields = ('id', 'audit_reference', 'timestamp', 'signature_hash', 'is_verified')
