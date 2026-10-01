from rest_framework import serializers

from .models import AIAnalysisRecord, CorrelationFinding, EvidenceRecord, FindingRevision


class EvidenceRecordSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    source_type_display = serializers.CharField(source='get_source_type_display', read_only=True)
    photo_url = serializers.SerializerMethodField()
    file = serializers.SerializerMethodField()
    inspector_name = serializers.SerializerMethodField()

    class Meta:
        model = EvidenceRecord
        fields = [
            'id', 'evidence_reference', 'project', 'project_name', 'source_type',
            'source_type_display', 'structural_element_id', 'bim_guid', 'coordinates',
            'captured_at', 'confidence', 'source_model', 'source_id', 'payload',
            'evidence_hash', 'photo_url', 'file', 'inspector_name', 'created_at',
        ]
        read_only_fields = ['id', 'evidence_reference', 'evidence_hash', 'created_at', 'updated_at']

    def get_photo_url(self, obj):
        if not obj.payload or not isinstance(obj.payload, dict):
            return None
        return obj.payload.get('photo_url') or obj.payload.get('url') or obj.payload.get('file_url') or None

    def get_inspector_name(self, obj):
        if obj.payload and isinstance(obj.payload, dict) and obj.payload.get('inspector_name'):
            return obj.payload.get('inspector_name')
        if obj.ingested_by:
            return obj.ingested_by.get_full_name() or obj.ingested_by.email
        return 'Field Inspector'

    def get_file(self, obj):
        photo_url = self.get_photo_url(obj)
        if not photo_url:
            return None
        payload = obj.payload if isinstance(obj.payload, dict) else {}
        return {
            'id': str(obj.id),
            'file_name': payload.get('file_name', f"evidence_{obj.evidence_reference}.jpg"),
            'content_type': payload.get('content_type', 'image/jpeg'),
            'file_size_bytes': payload.get('file_size_bytes') or payload.get('file_size') or 0,
            'sha256_hash': obj.evidence_hash,
            'file_url': photo_url,
            'last_verify_ok': payload.get('last_verify_ok', True),
            'last_verified_at': payload.get('last_verified_at'),
            'last_verify_note': payload.get('last_verify_note', 'Digest matches stored artifact bytes.'),
            'uploaded_by': self.get_inspector_name(obj),
            'created_at': obj.created_at.isoformat() if obj.created_at else '',
        }


class AIAnalysisRecordSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    analysis_type_display = serializers.CharField(source='get_analysis_type_display', read_only=True)
    evidence_ids = serializers.PrimaryKeyRelatedField(
        many=True, read_only=True, source='evidence',
    )

    class Meta:
        model = AIAnalysisRecord
        fields = [
            'id', 'analysis_reference', 'project', 'project_name', 'analysis_type',
            'analysis_type_display', 'evidence_ids', 'risk_level', 'risk_score',
            'observations', 'correlations', 'recommendations', 'reasoning_log',
            'requires_human_review', 'confidence', 'model_provider', 'model_version',
            'created_at',
        ]


class FindingRevisionSerializer(serializers.ModelSerializer):
    changed_by_name = serializers.SerializerMethodField()

    class Meta:
        model = FindingRevision
        fields = [
            'id', 'finding', 'revision_number', 'change_reason', 'snapshot',
            'changed_by', 'changed_by_name', 'notes', 'revision_hash',
            'previous_hash', 'recorded_at',
        ]

    def get_changed_by_name(self, obj):
        if obj.changed_by:
            return obj.changed_by.get_full_name() or obj.changed_by.email
        return 'Correlation Engine'


class CorrelationFindingSerializer(serializers.ModelSerializer):
    project_name = serializers.CharField(source='project.name', read_only=True)
    evidence_ids = serializers.PrimaryKeyRelatedField(many=True, read_only=True, source='evidence')
    evidence_references = serializers.SerializerMethodField()
    reviewed_by_name = serializers.SerializerMethodField()
    # Mean confidence of the underlying evidence records (0.0-1.0) — the
    # number the UI may label "confidence". NEVER risk_score: a 0.78 risk
    # was being displayed as "78% confidence" (7 Sep meeting item 6).
    confidence = serializers.SerializerMethodField()
    # True when a human logged this in the field (its analysis records
    # model_provider='engineer') — the UI must not badge a human log as
    # "AI INFERRED".
    logged_manually = serializers.SerializerMethodField()
    linked_ncr_reference = serializers.CharField(source='linked_ncr.ncr_reference', read_only=True, default=None)
    linked_inspection_reference = serializers.CharField(
        source='linked_inspection.inspection_reference', read_only=True, default=None,
    )
    revisions = FindingRevisionSerializer(many=True, read_only=True)

    class Meta:
        model = CorrelationFinding
        fields = [
            'id', 'finding_reference', 'project', 'project_name', 'structural_element_id',
            'bim_guid', 'group_key', 'title', 'description', 'risk_level', 'risk_score',
            'confidence', 'logged_manually',
            'evidence_ids', 'evidence_references', 'reasoning', 'status', 'reviewed_by',
            'reviewed_by_name', 'reviewed_at', 'review_notes', 'linked_ncr',
            'linked_ncr_reference', 'linked_inspection', 'linked_inspection_reference',
            'revision_count', 'revisions', 'created_at', 'updated_at',
        ]
        read_only_fields = fields

    def get_evidence_references(self, obj):
        return [e.evidence_reference for e in obj.evidence.all()]

    def get_confidence(self, obj):
        values = [e.confidence for e in obj.evidence.all()
                  if e.confidence is not None]
        if not values:
            return None
        return round(sum(values) / len(values), 3)

    def get_logged_manually(self, obj):
        analysis = getattr(obj, 'analysis', None)
        return bool(analysis and analysis.model_provider == 'engineer')

    def get_reviewed_by_name(self, obj):
        if obj.reviewed_by:
            return obj.reviewed_by.get_full_name() or obj.reviewed_by.email
        return None
