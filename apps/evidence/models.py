"""
Centralized AI Evidence Registry & Evidence Intelligence models (W1–W5, W6, W9).

Pipeline implemented by these models:

    Source Data -> EvidenceRecord (normalisation)
                -> AIAnalysisRecord (per-source AI analysis persistence)
                -> CorrelationFinding (cross-source correlation + risk)
                -> Human Review (HITL) -> Official Record -> Immutable Audit

Every value persisted here is derived from real sensor data, real uploads or
real LLM output — nothing is fabricated. Records that could not be analysed
store `null` analysis fields rather than placeholder values.
"""
import hashlib
import json
import os
import uuid
from datetime import datetime

from django.conf import settings
from django.db import models
from django.utils import timezone


def _short_uuid(length: int = 6) -> str:
    return uuid.uuid4().hex[:length].upper()


def generate_evidence_ref():
    return f"EV-{datetime.now().year}-{_short_uuid()}"


def generate_analysis_ref():
    return f"AIA-{datetime.now().year}-{_short_uuid()}"


def generate_finding_ref():
    return f"FND-{datetime.now().year}-{_short_uuid()}"


class EvidenceRecord(models.Model):
    """
    Unified Evidence Schema — the single registry every field source is
    normalised into (Digital Eye scanners, GPR, PUNDIT, GNSS, BIM, inspections,
    documents, live streams).

    Idempotent on (source_model, source_id): re-ingesting the same source
    updates the normalised payload instead of duplicating the record.
    """
    SOURCE_TYPES = [
        ('scan_defect', 'Digital Eye — Visual Defect'),
        ('scan_thermal', 'Digital Eye — Thermal Anomaly'),
        ('scan_alignment', 'Digital Eye — BIM Alignment / Deviation'),
        ('scan_progress', 'Digital Eye — Progress Validation'),
        ('gpr', 'Digital Eye — GPR Subsurface Radar'),
        ('pundit', 'Digital Eye — PUNDIT Ultrasonic NDT'),
        ('gnss', 'Digital Eye — Tersus GNSS Positioning'),
        ('bim_element', 'Trimble Connect — BIM Element'),
        ('inspection_finding', 'Field Inspection Finding'),
        ('inspection', 'Field Inspection Record'),
        ('document', 'Approved Document'),
        ('live_stream', 'Live Stream Observation'),
        ('corrective_action', 'Corrective Action'),
        ('uploaded_file', 'Uploaded File'),
        ('other', 'Other Source'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    evidence_reference = models.CharField(max_length=40, unique=True, default=generate_evidence_ref)
    project = models.ForeignKey(
        'projects.Project', on_delete=models.CASCADE, related_name='evidence_records',
    )

    # Unified Evidence Schema fields (per implementation plan §5A)
    source_type = models.CharField(max_length=40, choices=SOURCE_TYPES, db_index=True)
    structural_element_id = models.CharField(
        max_length=100, blank=True, default='', db_index=True,
        help_text="Structural element identifier, e.g. COL-C24",
    )
    bim_guid = models.CharField(
        max_length=64, blank=True, default='', db_index=True,
        help_text="IFC GlobalId / Trimble element GUID when known",
    )
    coordinates = models.JSONField(
        null=True, blank=True,
        help_text="{'latitude', 'longitude', 'x', 'y', 'z'} — whatever the source provides",
    )
    captured_at = models.DateTimeField(null=True, blank=True, db_index=True,
                                       help_text="When the evidence was captured in the field")
    confidence = models.FloatField(
        null=True, blank=True,
        help_text="Source confidence 0.0–1.0 where the source provides one",
    )

    # Link back to the originating record (polymorphic by name, keeps the
    # registry decoupled from every producer app's model set).
    source_model = models.CharField(max_length=100, blank=True, default='')
    source_id = models.CharField(max_length=100, blank=True, default='')

    #: The site visit this evidence was filed under, when there was one.
    #:
    #: Added because a finding's evidence otherwise reaches its inspection only
    #: by `source_model='inspections.Finding'` plus a string match on
    #: `source_id` — which is not a queryable relation, so "show me everything
    #: from this inspection" was a Python-side join. Nullable and `SET_NULL`:
    #: most evidence (a GPR survey, a scan defect) belongs to a project and to
    #: no inspection, and deleting an inspection must not delete the physical
    #: measurements taken during it.
    inspection = models.ForeignKey(
        'inspections.Inspection', on_delete=models.SET_NULL, null=True, blank=True,
        related_name='evidence_records',
        help_text='The inspection this evidence was gathered for, when there was one',
    )

    # Normalised payload — the canonical structured representation of the
    # evidence used by the correlation engine.
    payload = models.JSONField(default=dict)

    # Tamper evidence: SHA-256 of the canonical payload.
    evidence_hash = models.CharField(max_length=64, blank=True, default='')

    ingested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='ingested_evidence',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['source_model', 'source_id'],
                condition=~models.Q(source_model=''),
                name='unique_evidence_source',
            ),
        ]
        indexes = [
            models.Index(fields=['project', 'source_type']),
            models.Index(fields=['structural_element_id']),
            models.Index(fields=['bim_guid']),
        ]

    def __str__(self):
        return f"{self.evidence_reference} [{self.get_source_type_display()}] {self.project_id}"

    def canonical_payload(self) -> str:
        return json.dumps(self.payload, sort_keys=True, default=str)

    def compute_hash(self) -> str:
        return hashlib.sha256(self.canonical_payload().encode('utf-8')).hexdigest()


def evidence_file_upload_to(instance, filename):
    """``evidence/<project>/<evidence reference>/<filename>``.

    Keyed on the record's own reference rather than on the uploaded name: two
    inspectors uploading ``photo.jpg`` from the same site must not collide, and
    the stored path must not let a client's filename decide where bytes land.
    """
    safe = os.path.basename(str(filename or '')).replace('\\', '_').strip() or 'file'
    record = getattr(instance, 'record', None)
    if record is None:
        return f'evidence/unfiled/{uuid.uuid4().hex[:12]}_{safe}'
    return f'evidence/{record.project_id}/{record.evidence_reference}/{safe}'


class EvidenceFile(models.Model):
    """The bytes behind an evidence record.

    A separate table rather than a ``FileField`` on ``EvidenceRecord``, and the
    reason is ``compute_hash()``. That method is defined as SHA-256 over the
    record's canonical JSON payload, so ``/verify/`` answers "is this payload
    what was ingested?". Putting the file in the same row would make the word
    ambiguous — verify the payload, or the bytes? — and a record with no payload
    would hash ``{}`` and verify cleanly, which is a false attestation rather
    than an honest absence. Two hashes, two questions, two answers.

    ``last_verify_ok`` is **nullable on purpose**. ``default=False`` would assert
    that a file failed verification before anything had verified it, which is
    indistinguishable from a real mismatch in every downstream report.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    record = models.OneToOneField(
        EvidenceRecord, on_delete=models.CASCADE, related_name='file',
    )

    file = models.FileField(upload_to=evidence_file_upload_to, max_length=500)
    file_name = models.CharField(
        max_length=255, blank=True, default='',
        help_text='The name as uploaded — kept even if storage renames it',
    )
    content_type = models.CharField(
        max_length=120, blank=True, default='',
        help_text="The client's declared type. Not trusted, and not verified",
    )
    file_size_bytes = models.BigIntegerField(default=0)
    sha256_hash = models.CharField(
        max_length=64, blank=True, default='',
        help_text=(
            'SHA-256 over the stored bytes, taken by reading them back from '
            'storage — so the hash attests the file that exists, not the one '
            'the client said it sent.'
        ),
    )
    storage_name = models.CharField(
        max_length=1000, blank=True, default='',
        help_text='The storage backend key, kept so the bytes can be re-read',
    )

    last_verify_ok = models.BooleanField(
        null=True, blank=True,
        help_text=(
            'None until a verification has run. True only when the stored '
            'bytes re-hash to the recorded digest; False when they do not; '
            'None with a note when the file was too large to re-hash.'
        ),
    )
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_verify_note = models.CharField(
        max_length=255, blank=True, default='',
        help_text='Why verification did not pass, or why it could not be run',
    )

    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='uploaded_evidence_files',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        verbose_name = 'Evidence file'
        verbose_name_plural = 'Evidence files'
        indexes = [
            models.Index(fields=['sha256_hash']),
        ]

    def __str__(self):
        return f'{self.file_name or self.storage_name} ({self.record_id})'

    def compute_stored_hash(self):
        """SHA-256 of the bytes currently in storage.

        Read in chunks: this runs on a request, and a 20 MB file read whole
        would be 20 MB of memory held for a hash.
        """
        digest = hashlib.sha256()
        with self.file.open('rb') as handle:
            for chunk in iter(lambda: handle.read(64 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()


class AIAnalysisRecord(models.Model):
    """
    Persistent AI Analysis Record (per implementation plan §5A): stored in
    PostgreSQL with risk_level, observations, correlations, recommendations,
    and requires_human_review.

    AI output is strictly decision-support: `requires_human_review` defaults
    to True and no risk is treated as an official finding until a qualified
    human reviews the correlated result (see CorrelationFinding).
    """
    RISK_LEVELS = [
        ('info', 'Info'),
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
        ('critical', 'Critical'),
    ]

    ANALYSIS_TYPES = [
        ('gpr', 'GPR Subsurface Analysis'),
        ('pundit', 'PUNDIT Ultrasonic NDT Analysis'),
        ('gnss', 'GNSS Positioning Analysis'),
        ('defect', 'Visual Defect Analysis'),
        ('thermal', 'Thermal Analysis'),
        ('bim', 'BIM Deviation Analysis'),
        ('correlation', 'Cross-Source Correlation'),
        ('project_intelligence', 'Project-Level Intelligence'),
        ('district_intelligence', 'District / HQ Intelligence'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    analysis_reference = models.CharField(max_length=40, unique=True, default=generate_analysis_ref)
    project = models.ForeignKey(
        'projects.Project', on_delete=models.CASCADE, related_name='ai_analyses',
    )
    analysis_type = models.CharField(max_length=40, choices=ANALYSIS_TYPES, db_index=True)

    evidence = models.ManyToManyField(EvidenceRecord, blank=True, related_name='ai_analyses')

    risk_level = models.CharField(max_length=20, choices=RISK_LEVELS, default='info')
    risk_score = models.FloatField(
        null=True, blank=True,
        help_text="Explainable computed risk 0.0–1.0 (deterministic math; null when not computable)",
    )
    observations = models.JSONField(default=list, blank=True)
    correlations = models.JSONField(default=list, blank=True)
    recommendations = models.JSONField(default=list, blank=True)
    reasoning_log = models.TextField(
        blank=True, default='',
        help_text="Explainable reasoning: every input, weight and step of the computation",
    )

    requires_human_review = models.BooleanField(default=True)
    confidence = models.FloatField(null=True, blank=True, help_text="Analysis confidence 0.0–1.0")

    # Provenance of the AI output: which provider/model produced it.
    model_provider = models.CharField(max_length=100, blank=True, default='')
    model_version = models.CharField(max_length=255, blank=True, default='')

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['project', 'analysis_type']),
            models.Index(fields=['risk_level']),
        ]

    def __str__(self):
        return f"{self.analysis_reference} [{self.get_analysis_type_display()}] risk={self.risk_level}"


class CorrelationFinding(models.Model):
    """
    Cross-Source Evidence Correlation result for one structural element
    (e.g. Column COL-C24): GPR + PUNDIT + GNSS + BIM evidence correlated into
    a single explainable risk finding awaiting human review.

    Lifecycle (statutory loop, plan §5 Week 5):
        pending_review -> accepted / rejected / modified -> escalated (NCR/inspection)
    Every state transition is recorded in FindingRevision with a hash chain.
    """
    STATUS_CHOICES = [
        ('pending_review', 'Pending Human Review'),
        ('accepted', 'Accepted by Reviewer'),
        ('rejected', 'Rejected / Dismissed'),
        ('modified', 'Modified by Reviewer'),
        ('escalated', 'Escalated (NCR / Statutory Inspection)'),
    ]
    RISK_LEVELS = AIAnalysisRecord.RISK_LEVELS

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    finding_reference = models.CharField(max_length=40, unique=True, default=generate_finding_ref)
    project = models.ForeignKey('projects.Project', on_delete=models.CASCADE, related_name='correlation_findings')

    structural_element_id = models.CharField(max_length=100, blank=True, default='', db_index=True)
    bim_guid = models.CharField(max_length=64, blank=True, default='', db_index=True)
    group_key = models.CharField(
        max_length=150, blank=True, default='', db_index=True,
        help_text="Stable correlation group key (element:<id> | guid:<guid> | spatial:<cluster> | source:<type>)",
    )
    title = models.CharField(max_length=255)
    description = models.TextField(blank=True, default='')

    risk_level = models.CharField(max_length=20, choices=RISK_LEVELS, default='info')
    risk_score = models.FloatField(null=True, blank=True, help_text="Computed multi-source risk 0.0–1.0")

    evidence = models.ManyToManyField(EvidenceRecord, blank=True, related_name='correlation_findings')
    analysis = models.ForeignKey(
        AIAnalysisRecord, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='findings',
    )
    reasoning = models.TextField(
        blank=True, default='',
        help_text="Explainable correlation reasoning: which sources agreed, weights applied, math",
    )

    status = models.CharField(max_length=30, choices=STATUS_CHOICES, default='pending_review', db_index=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='reviewed_correlation_findings',
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_notes = models.TextField(blank=True, default='')

    # Statutory linkage — set when the finding is escalated.
    linked_ncr = models.ForeignKey(
        'compliance.NonConformanceReport', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='source_findings',
    )
    linked_inspection = models.ForeignKey(
        'inspections.Inspection', on_delete=models.SET_NULL,
        null=True, blank=True, related_name='source_findings',
    )

    revision_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['project', 'status']),
            models.Index(fields=['risk_level']),
        ]

    def __str__(self):
        return f"{self.finding_reference} [{self.risk_level}] {self.title}"


class FindingRevision(models.Model):
    """
    Immutable finding revision & audit history (plan §5 Week 4): every change
    to a CorrelationFinding (creation, human modification, decision, statutory
    escalation) is snapshotted here with a SHA-256 hash chained to the
    previous revision.
    """
    CHANGE_REASONS = [
        ('created', 'Finding Created by Correlation Engine'),
        ('modified', 'Modified by Human Reviewer'),
        ('decision', 'Human Review Decision'),
        ('escalated', 'Escalated to Statutory Process'),
        ('new_evidence', 'New Evidence Correlated'),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    finding = models.ForeignKey(CorrelationFinding, on_delete=models.CASCADE, related_name='revisions')
    revision_number = models.PositiveIntegerField()
    change_reason = models.CharField(max_length=30, choices=CHANGE_REASONS)

    snapshot = models.JSONField(default=dict, help_text="Complete finding state at this revision")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='finding_revisions',
    )
    notes = models.TextField(blank=True, default='')

    # Hash chain: sha256(previous_hash + canonical snapshot + revision number)
    revision_hash = models.CharField(max_length=64, blank=True, default='')
    previous_hash = models.CharField(max_length=64, blank=True, default='')
    recorded_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ['finding', 'revision_number']
        constraints = [
            models.UniqueConstraint(fields=['finding', 'revision_number'], name='unique_finding_revision'),
        ]

    def __str__(self):
        return f"{self.finding.finding_reference} rev {self.revision_number} ({self.change_reason})"

    def compute_hash(self, previous_hash: str = '') -> str:
        payload = json.dumps(self.snapshot, sort_keys=True, default=str)
        return hashlib.sha256(
            f"{previous_hash}:{self.revision_number}:{payload}".encode('utf-8')
        ).hexdigest()

    def verify_chain(self) -> bool:
        """Verify this revision's hash against its stored snapshot."""
        return self.revision_hash and self.revision_hash == self.compute_hash(self.previous_hash)
