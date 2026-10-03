from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import (
    AIAnalysisRecord, CorrelationFinding, EvidenceFile, EvidenceRecord,
    FindingRevision,
)


class EvidenceFileSerializer(serializers.ModelSerializer):
    """The bytes behind a record, and the last thing verification said about them.

    ``last_verify_ok`` is nullable and stays that way here: ``null`` means no
    verification has run, which is a different report from ``false``. A client
    that renders the two the same way is showing "failed" for "not checked yet".
    """

    file_url = serializers.SerializerMethodField()

    class Meta:
        model = EvidenceFile
        fields = [
            'id', 'file_name', 'content_type', 'file_size_bytes', 'sha256_hash',
            'file_url', 'last_verify_ok', 'last_verified_at', 'last_verify_note',
            'uploaded_by', 'created_at',
        ]
        read_only_fields = fields

    @extend_schema_field(serializers.URLField(allow_null=True))
    def get_file_url(self, obj):
        """A URL the caller can fetch the bytes from, or ``None``.

        ``None`` rather than an exception when the storage backend cannot
        produce one (a private bucket with no signing configured, say): the file
        is still stored and still verified, and the honest answer to "where can
        I read it?" is that this deployment has not been told how to hand it out.
        """
        try:
            return obj.file.url
        except Exception:  # noqa: BLE001 — a backend without URLs is a real deployment
            return None


class EvidenceRecordSerializer(serializers.ModelSerializer):
    """A registry record, with the file behind it when there is one.

    The file is served on the *list* as well as the detail: the evidence
    screen is a grid of captures that has to play each voice note and show
    each photo, and it holds only what this serializer returned. Withholding
    the file here would make the one screen a capture uploads to the one
    screen that cannot play it back.
    """

    project_name = serializers.CharField(source='project.name', read_only=True)
    source_type_display = serializers.CharField(source='get_source_type_display', read_only=True)
    file = EvidenceFileSerializer(read_only=True)

    class Meta:
        model = EvidenceRecord
        fields = [
            'id', 'evidence_reference', 'project', 'project_name', 'source_type',
            'source_type_display', 'structural_element_id', 'bim_guid', 'coordinates',
            'captured_at', 'confidence', 'source_model', 'source_id', 'inspection',
            'payload', 'evidence_hash', 'created_at', 'file',
        ]
        read_only_fields = ['id', 'evidence_reference', 'evidence_hash', 'created_at', 'updated_at']


class EvidenceFileUploadSerializer(serializers.Serializer):
    """`POST evidence/upload/` request body (multipart or JSON).

    Every field beyond ``file`` and ``project`` is the uploader's own context.
    None of them is required and none has a default: a capture with no recorded
    coordinates is one where the device reported none, which is not the same
    claim as coordinates recorded as zero.

    The fields below ``description`` are the field app's own vocabulary — the
    category and severity it asked the inspector to pick, the transcript the
    device heard. They are recorded as *claims by the uploader* rather than
    promoted into the registry's own columns, because the registry's severity
    and element identifiers are the correlation engine's, not a picker's.
    """

    #: The capture kinds a client may declare. A subset of
    #: ``EvidenceRecord.SOURCE_TYPES`` — asserted by a test — so the registry
    #: never records a type it cannot name.
    UPLOAD_SOURCE_TYPES = ('uploaded_file', 'photo', 'voice_note')

    file = serializers.FileField(
        help_text='The capture itself — photo, scan export, PDF, instrument dump.')
    project = serializers.UUIDField()
    inspection = serializers.UUIDField(
        required=False, allow_null=True,
        help_text='The site visit this evidence belongs to, when it belongs to one.')

    source_type = serializers.ChoiceField(
        choices=UPLOAD_SOURCE_TYPES, required=False, default='uploaded_file',
        help_text='What kind of capture this is. Defaults to a file of no declared kind.')
    structural_element_id = serializers.CharField(
        required=False, allow_blank=True, max_length=100)
    bim_guid = serializers.CharField(required=False, allow_blank=True, max_length=64)
    coordinates = serializers.JSONField(
        required=False, allow_null=True,
        help_text="{'latitude', 'longitude', 'x', 'y', 'z'} as the device reported them.")
    captured_at = serializers.DateTimeField(required=False, allow_null=True, format='iso-8601')
    confidence = serializers.FloatField(
        required=False, allow_null=True, min_value=0.0, max_value=1.0)
    description = serializers.CharField(
        required=False, allow_blank=True, max_length=2000,
        help_text="The uploader's own note about what this is.")

    category = serializers.CharField(required=False, allow_blank=True, max_length=120)
    severity = serializers.CharField(required=False, allow_blank=True, max_length=20)
    batch_id = serializers.CharField(required=False, allow_blank=True, max_length=64)
    transcript = serializers.CharField(required=False, allow_blank=True, max_length=5000)
    translations = serializers.JSONField(
        required=False, allow_null=True,
        help_text="The same transcript in other languages, keyed by language code.")
    duration_seconds = serializers.IntegerField(
        required=False, allow_null=True, min_value=0,
        help_text='How long the recording runs, as the device measured it.')

    sha256 = serializers.CharField(
        required=False, allow_blank=True, max_length=64,
        help_text='The client\'s own digest. When sent, it must match or the '
                  'upload is refused naming both values.')
    file_size_bytes = serializers.IntegerField(
        required=False, allow_null=True, min_value=1,
        help_text="The client's own byte count. When sent, it must match.")

    def validate_sha256(self, value):
        value = (value or '').strip().lower()
        if value and (len(value) != 64 or any(c not in '0123456789abcdef' for c in value)):
            raise serializers.ValidationError(
                'Must be 64 hexadecimal characters — a SHA-256 digest.')
        return value

    def validate_coordinates(self, value):
        if value is None:
            return None
        if not isinstance(value, dict):
            raise serializers.ValidationError(
                'Must be an object of coordinate keys, e.g. {"latitude": 6.4}.')
        return value

    def validate_translations(self, value):
        if value is None:
            return None
        if not isinstance(value, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in value.items()):
            raise serializers.ValidationError(
                'Must be an object of language code to translated text, '
                'e.g. {"yo": "..."}.')
        return value


class EvidenceVerificationSerializer(serializers.Serializer):
    """Response schema for `/verify/`. Every field is an answer, not a placeholder.

    ``file_bytes_ok`` is null — not true, not false — when no file is attached or
    when the file is too large to re-read. That third state is the whole reason
    the field is nullable.
    """

    evidence_reference = serializers.CharField()
    payload_ok = serializers.BooleanField()
    evidence_hash = serializers.CharField(allow_blank=True)
    file_present = serializers.BooleanField()
    file_bytes_ok = serializers.BooleanField(allow_null=True)
    file_sha256 = serializers.CharField(allow_null=True, allow_blank=True)
    file_size_bytes = serializers.IntegerField(allow_null=True)
    note = serializers.CharField(allow_blank=True)
    verified_at = serializers.DateTimeField(format='iso-8601')


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
