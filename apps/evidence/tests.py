"""
Tests for the Centralized Evidence Registry (apps.evidence).

Covers the full pipeline described in the app's models:
    Source Data -> EvidenceRecord (ingestion, tamper-evident hashing)
                -> CorrelationFinding (cross-source correlation + risk)
                -> Project/District AI risk intelligence (decision-support only)
                -> Human-in-the-Loop review (strict: only humans decide)

All fixtures are created inside the test classes (users, projects, source
records) — no external fixture files.
"""
import datetime
import hashlib
import json
import os
import tempfile
import uuid
from io import StringIO
from unittest import mock
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from apps.audit.models import AuditEvent
from apps.common.ai_service import AIProviderUnavailable
from apps.compliance.models import CorrectiveActionPlan, NonConformanceReport
from apps.digital_eye.models import (
    BIMElementMapping, GPRAnomaly, GPRSurvey, GnssBenchmark, GnssSurvey,
    PUNDITTest,
)
from apps.evidence.correlation import CorrelationEngine
from apps.evidence.files import (
    EvidenceFileError, EvidenceFileService,
)
from apps.evidence.ingestion import EvidenceIngestionService
from apps.evidence.intelligence import ProjectIntelligenceService
from apps.evidence.models import (
    AIAnalysisRecord, CorrelationFinding, EvidenceFile, EvidenceRecord,
)
from apps.evidence.review import HumanReviewService, ReviewError
from apps.evidence.serializers import EvidenceFileUploadSerializer
from apps.evidence.tasks import correlate_projects, detect_recurring_anomalies
from apps.government.models import District, Profile
from apps.inspections.models import Finding as InspectionFinding
from apps.inspections.models import Inspection
from apps.notifications.models import InAppNotification
from apps.projects.models import Project
from apps.scans.models import (
    BIMAlignmentResult, Defect, ScanSession, ThermalAnomaly,
)

User = get_user_model()


def make_evidence_record(project, source_type, payload, *, structural_element_id='',
                          bim_guid='', coordinates=None, confidence=None):
    """Direct registry record creation (bypasses ingestion) for engine tests."""
    return EvidenceRecord.objects.create(
        project=project,
        source_type=source_type,
        structural_element_id=structural_element_id,
        bim_guid=bim_guid,
        coordinates=coordinates,
        confidence=confidence,
        payload=payload,
    )


# ======================================================================
# 1. Evidence ingestion — every supported source type
# ======================================================================

class EvidenceIngestionTestCase(TestCase):
    """Each producer normaliser converts its source record into the unified
    EvidenceRecord schema with a SHA-256 evidence hash."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="ingest_officer@nexucon.com",
            email="ingest_officer@nexucon.com",
            password="Password123!",
            first_name="Ada",
            last_name="Obi",
        )
        self.project = Project.objects.create(
            name="Ikoyi Tower Development",
            project_type="Commercial",
            status="ACTIVE",
            site_address="Plot 3, Ikoyi",
            lga="Eti-Osa",
        )
        self.session = ScanSession.objects.create(
            project=self.project, scanner_id="SCN-001",
        )

    def test_ingest_scan_defect(self):
        defect = Defect.objects.create(
            session=self.session, type="crack", severity="high",
            grid_zone="COL-C24", confidence_score=0.85,
            description="Vertical crack on column face",
            location_x=1.2, location_y=3.4, location_z=8.0,
        )
        record = EvidenceIngestionService.ingest_defect(defect, ingested_by=self.user)

        self.assertTrue(record.evidence_reference.startswith("EV-"))
        self.assertEqual(record.source_type, "scan_defect")
        self.assertEqual(record.source_model, "scans.Defect")
        self.assertEqual(str(record.source_id), str(defect.id))
        self.assertEqual(record.project, self.project)
        self.assertEqual(record.structural_element_id, "COL-C24")
        self.assertEqual(record.confidence, 0.85)
        self.assertEqual(record.payload["defect_type"], "crack")
        self.assertEqual(record.payload["severity"], "high")
        self.assertEqual(record.payload["scanner_id"], "SCN-001")
        self.assertEqual(record.coordinates, {"x": 1.2, "y": 3.4, "z": 8.0})
        self.assertEqual(record.ingested_by, self.user)

    def test_ingest_thermal_anomaly(self):
        anomaly = ThermalAnomaly.objects.create(
            session=self.session, temperature_variance=6.4, severity="medium",
            grid_zone="BMD-02", confidence_score=0.7,
            description="Hot spot near beam junction",
        )
        record = EvidenceIngestionService.ingest_thermal_anomaly(anomaly, ingested_by=self.user)

        self.assertEqual(record.source_type, "scan_thermal")
        self.assertEqual(record.source_model, "scans.ThermalAnomaly")
        self.assertEqual(record.structural_element_id, "BMD-02")
        self.assertEqual(record.payload["temperature_variance"], 6.4)
        self.assertEqual(record.payload["severity"], "medium")

    def test_ingest_bim_alignment(self):
        session2 = ScanSession.objects.create(project=self.project, scanner_id="SCN-002")
        alignment = BIMAlignmentResult.objects.create(
            session=session2, alignment_status="SUCCESS",
            mean_deviation=18.5, max_deviation=42.0, min_deviation=2.0,
        )
        record = EvidenceIngestionService.ingest_bim_alignment(alignment, ingested_by=self.user)

        self.assertEqual(record.source_type, "scan_alignment")
        self.assertEqual(record.source_model, "scans.BIMAlignmentResult")
        self.assertEqual(record.payload["alignment_status"], "SUCCESS")
        self.assertEqual(record.payload["mean_deviation_mm"], 18.5)
        self.assertEqual(record.payload["max_deviation_mm"], 42.0)
        self.assertEqual(record.payload["clash_count"], 0)

    def test_ingest_gpr_anomaly(self):
        survey = GPRSurvey.objects.create(
            project=self.project, title="Foundation Zone B survey",
            structural_element="COL-C24", antenna_frequency_mhz=400,
        )
        anomaly = GPRAnomaly.objects.create(
            survey=survey, anomaly_type="void", severity="high",
            depth_m=0.6, estimated_size_m=0.8, confidence=0.9,
            coordinates={"latitude": 6.45, "longitude": 3.45},
        )
        record = EvidenceIngestionService.ingest_gpr_anomaly(anomaly, ingested_by=self.user)

        self.assertEqual(record.source_type, "gpr")
        self.assertEqual(record.source_model, "digital_eye.GPRAnomaly")
        self.assertEqual(record.structural_element_id, "COL-C24")
        self.assertEqual(record.payload["anomaly_type"], "void")
        self.assertEqual(record.payload["depth_m"], 0.6)
        self.assertEqual(record.payload["survey_reference"], survey.survey_reference)
        self.assertEqual(record.confidence, 0.9)
        self.assertEqual(record.coordinates, {"latitude": 6.45, "longitude": 3.45})

    def test_ingest_pundit_test(self):
        test = PUNDITTest.objects.create(
            project=self.project, test_type="pulse_velocity",
            structural_element="COL-C24", path_length_mm=300.0,
            pulse_time_us=75.0, velocity_km_s=4.0, quality_grade="good",
            latitude=6.45, longitude=3.45,
        )
        record = EvidenceIngestionService.ingest_pundit_test(test, ingested_by=self.user)

        self.assertEqual(record.source_type, "pundit")
        self.assertEqual(record.source_model, "digital_eye.PUNDITTest")
        self.assertEqual(record.structural_element_id, "COL-C24")
        self.assertEqual(record.payload["quality_grade"], "good")
        self.assertEqual(record.payload["velocity_km_s"], 4.0)
        # Deterministic confidence: complete path length + transit time -> 0.90 base
        self.assertEqual(record.confidence, 0.90)

    def test_ingest_pundit_test_without_measurements_has_null_confidence(self):
        test = PUNDITTest.objects.create(
            project=self.project, test_type="pulse_velocity",
            structural_element="COL-C24",
        )
        record = EvidenceIngestionService.ingest_pundit_test(test, ingested_by=self.user)
        self.assertIsNone(record.confidence)
        self.assertIsNone(record.payload["velocity_km_s"])

    def test_ingest_gnss_survey(self):
        survey = GnssSurvey.objects.create(
            project=self.project, title="Boundary re-establishment",
            latitude=6.45, longitude=3.45, fix_quality="fixed", method="rtk",
        )
        GnssBenchmark.objects.create(
            survey=survey, point_id="BM-001", latitude=6.4501, longitude=3.4501,
            accuracy_mm=8.0,
        )
        record = EvidenceIngestionService.ingest_gnss_survey(survey, ingested_by=self.user)

        self.assertEqual(record.source_type, "gnss")
        self.assertEqual(record.source_model, "digital_eye.GnssSurvey")
        self.assertEqual(record.payload["survey_reference"], survey.survey_reference)
        self.assertEqual(record.payload["benchmark_count"], 1)
        self.assertEqual(record.payload["benchmarks"][0]["point_id"], "BM-001")
        self.assertEqual(record.payload["boundary_point_count"], 0)

    def test_ingest_bim_element(self):
        mapping = BIMElementMapping.objects.create(
            project=self.project, element_id="COL-C24",
            bim_guid="3rNg7Ib9P5Jv3yRzQeWqXm", element_name="Column C24",
            element_type="IfcColumn", level="Level 03", source="manual",
        )
        record = EvidenceIngestionService.ingest_bim_element(mapping, ingested_by=self.user)

        self.assertEqual(record.source_type, "bim_element")
        self.assertEqual(record.source_model, "digital_eye.BIMElementMapping")
        self.assertEqual(record.structural_element_id, "COL-C24")
        self.assertEqual(record.bim_guid, "3rNg7Ib9P5Jv3yRzQeWqXm")
        self.assertEqual(record.payload["element_type"], "IfcColumn")

    def test_ingest_inspection_finding(self):
        inspection = Inspection.objects.create(
            project=self.project, inspection_type="Structural Review",
        )
        finding = InspectionFinding.objects.create(
            inspection=inspection, project=self.project,
            title="Honeycombing at column base", description="Void in concrete",
            severity="HIGH",
        )
        record = EvidenceIngestionService.ingest_inspection_finding(finding, ingested_by=self.user)

        self.assertEqual(record.source_type, "inspection_finding")
        self.assertEqual(record.source_model, "inspections.Finding")
        self.assertEqual(record.payload["severity"], "HIGH")
        self.assertEqual(record.payload["inspection_reference"], inspection.inspection_reference)
        self.assertEqual(record.payload["is_resolved"], False)

    def test_generic_ingest_for_document_source(self):
        record = EvidenceIngestionService._ingest(
            project=self.project, source_type="document",
            source_model="documents.Document", source_id="doc-123",
            payload={"title": "Cube test report", "status": "pass"},
            ingested_by=self.user,
        )
        self.assertEqual(record.source_type, "document")
        self.assertEqual(record.source_id, "doc-123")
        self.assertEqual(record.payload["status"], "pass")

    def test_ingestion_is_idempotent_on_source_identity(self):
        defect = Defect.objects.create(
            session=self.session, type="spalling", severity="medium",
            grid_zone="COL-C24",
        )
        EvidenceIngestionService.ingest_defect(defect, ingested_by=self.user)
        self.assertEqual(EvidenceRecord.objects.count(), 1)

        # Re-ingest after the source record changed: update, not duplicate.
        defect.severity = "critical"
        defect.save()
        record = EvidenceIngestionService.ingest_defect(defect, ingested_by=self.user)

        self.assertEqual(EvidenceRecord.objects.count(), 1)
        self.assertEqual(record.payload["severity"], "critical")

    def test_bulk_ingestion_skips_false_positive_defects(self):
        Defect.objects.create(
            session=self.session, type="crack", severity="high",
            grid_zone="COL-C24",
        )
        Defect.objects.create(
            session=self.session, type="delamination", severity="low",
            grid_zone="BMD-02", is_false_positive=True,
        )
        survey = GPRSurvey.objects.create(
            project=self.project, title="Zone B", structural_element="COL-C24",
        )
        GPRAnomaly.objects.create(
            survey=survey, anomaly_type="void", severity="medium",
        )

        touched = EvidenceIngestionService.ingest_all_for_project(self.project, ingested_by=self.user)
        self.assertEqual(touched, 2)  # real defect + GPR anomaly; false positive skipped
        self.assertEqual(EvidenceRecord.objects.count(), 2)
        self.assertFalse(
            EvidenceRecord.objects.filter(source_type="scan_defect",
                                          payload__severity="low").exists()
        )

        # Bulk re-ingestion stays idempotent.
        touched_again = EvidenceIngestionService.ingest_all_for_project(self.project)
        self.assertEqual(touched_again, 2)
        self.assertEqual(EvidenceRecord.objects.count(), 2)


# ======================================================================
# 2. Tamper evidence — payload hashing & revision hash chain
# ======================================================================

class EvidenceTamperEvidenceTestCase(TestCase):
    """SHA-256 evidence hashes on records and the hash-chained revision
    history must detect any after-the-fact tampering."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="integrity_clerk@nexucon.com",
            email="integrity_clerk@nexucon.com",
            password="Password123!",
        )
        self.project = Project.objects.create(name="Lekki Deep Wharf")
        # An ingested record (carries a computed evidence hash)...
        self.session = ScanSession.objects.create(
            project=self.project, scanner_id="SCN-TAMPER",
        )
        self.defect = Defect.objects.create(
            session=self.session, type="crack", severity="high",
            grid_zone="COL-C24",
        )
        self.ingested = EvidenceIngestionService.ingest_defect(
            self.defect, ingested_by=self.user,
        )
        # ...and a direct registry record for correlation-engine tests.
        self.gpr = make_evidence_record(
            self.project, "gpr", {"severity": "high", "depth_m": 0.6},
            structural_element_id="FTG-07", confidence=0.8,
        )

    def test_ingested_records_carry_matching_sha256_hash(self):
        self.ingested.refresh_from_db()
        self.assertEqual(self.ingested.evidence_hash, self.ingested.compute_hash())
        self.assertEqual(len(self.ingested.evidence_hash), 64)

    def test_directly_created_records_have_empty_hash_until_ingested(self):
        # Records created outside the ingestion pipeline carry no hash yet —
        # nothing is fabricated.
        self.assertEqual(self.gpr.evidence_hash, "")

    def test_tampered_payload_is_detected_by_hash_mismatch(self):
        self.ingested.refresh_from_db()
        self.assertEqual(self.ingested.evidence_hash, self.ingested.compute_hash())

        # Tamper with the payload without recomputing the hash.
        self.ingested.payload["severity"] = "info"
        self.ingested.save()
        self.ingested.refresh_from_db()

        self.assertNotEqual(self.ingested.evidence_hash, self.ingested.compute_hash())

    def test_reingest_after_source_change_recomputes_hash(self):
        self.defect.severity = "critical"
        self.defect.save()
        record = EvidenceIngestionService.ingest_defect(self.defect, ingested_by=self.user)

        self.assertEqual(record.evidence_hash, record.compute_hash())
        self.assertEqual(record.payload["severity"], "critical")
        self.assertEqual(EvidenceRecord.objects.count(), 2)  # defect + gpr

    def test_revision_hash_chain_and_tamper_detection(self):
        findings = CorrelationEngine.run(self.project, sync_sources=False)
        element_finding = [f for f in findings if f.group_key == "element:FTG-07"][0]

        rev1 = element_finding.revisions.get(revision_number=1)
        self.assertEqual(rev1.change_reason, "created")
        self.assertEqual(rev1.previous_hash, "")
        self.assertTrue(rev1.verify_chain())

        HumanReviewService.review(element_finding, self.user, "accept", notes="Checked on site")
        rev2 = element_finding.revisions.get(revision_number=2)
        self.assertEqual(rev2.change_reason, "decision")
        self.assertEqual(rev2.changed_by, self.user)
        # The chain links every revision to its predecessor.
        self.assertEqual(rev2.previous_hash, rev1.revision_hash)
        self.assertEqual(element_finding.revision_count, 2)

        # Tampering with an immutable snapshot breaks verification.
        rev1.snapshot["risk_level"] = "info"
        rev1.save()
        rev1.refresh_from_db()
        self.assertFalse(rev1.verify_chain())

    def test_revision_numbers_are_unique_per_finding(self):
        findings = CorrelationEngine.run(self.project, sync_sources=False)
        finding = [f for f in findings if f.group_key == "element:FTG-07"][0]
        HumanReviewService.review(finding, self.user, "reject", notes="Not applicable")
        numbers = list(
            finding.revisions.values_list("revision_number", flat=True)
        )
        self.assertEqual(numbers, [1, 2])


# ======================================================================
# 3. Cross-source correlation engine
# ======================================================================

class CorrelationEngineTestCase(TestCase):
    """Grouping of related evidence, deterministic risk scoring, project
    isolation and re-run behaviour."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="correlation_analyst@nexucon.com",
            email="correlation_analyst@nexucon.com",
            password="Password123!",
        )
        self.project = Project.objects.create(name="Badore Ring Road Project")

    def test_related_evidence_on_same_element_is_correlated(self):
        gpr = make_evidence_record(
            self.project, "gpr", {"anomaly_type": "void", "severity": "high", "depth_m": 0.6},
            structural_element_id="COL-C24", confidence=0.9,
        )
        pundit = make_evidence_record(
            self.project, "pundit", {"quality_grade": "poor", "velocity_km_s": 2.4},
            structural_element_id="COL-C24",
        )
        defect = make_evidence_record(
            self.project, "scan_defect", {"defect_type": "crack", "severity": "high"},
            structural_element_id="COL-C24", confidence=0.85,
        )

        findings = CorrelationEngine.run(self.project, sync_sources=False)

        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.group_key, "element:COL-C24")
        self.assertEqual(finding.structural_element_id, "COL-C24")
        # Three independent structural sources agreeing -> high/critical risk.
        self.assertIn(finding.risk_level, ("high", "critical"))
        self.assertGreaterEqual(finding.risk_score, 0.65)
        self.assertLessEqual(finding.risk_score, 1.0)

        linked_ids = {e.id for e in finding.evidence.all()}
        self.assertEqual(linked_ids, {gpr.id, pundit.id, defect.id})

        # The reasoning log names every contributing record (explainability).
        for record in (gpr, pundit, defect):
            self.assertIn(record.evidence_reference, finding.reasoning)

        # A persistent AI analysis record backs the finding.
        analysis = finding.analysis
        self.assertIsNotNone(analysis)
        self.assertEqual(analysis.analysis_type, "correlation")
        self.assertEqual(analysis.model_provider, "deterministic")
        self.assertEqual(
            {c["evidence_reference"] for c in analysis.correlations},
            {r.evidence_reference for r in (gpr, pundit, defect)},
        )

    def test_unrelated_evidence_is_not_correlated(self):
        make_evidence_record(
            self.project, "gpr", {"severity": "high"}, structural_element_id="COL-C24",
        )
        unrelated = make_evidence_record(
            self.project, "document", {"title": "Cube test", "status": "pass"},
            structural_element_id="BMD-02",
        )

        findings = CorrelationEngine.run(self.project, sync_sources=False)
        self.assertEqual(len(findings), 2)

        by_group = {f.group_key: f for f in findings}
        self.assertEqual(by_group["element:COL-C24"].evidence.count(), 1)
        bmd_finding = by_group["element:BMD-02"]
        self.assertEqual(bmd_finding.evidence.count(), 1)
        self.assertEqual(bmd_finding.evidence.get().id, unrelated.id)
        # A passing document alone contributes no meaningful risk.
        self.assertEqual(bmd_finding.risk_level, "info")

        # No cross-contamination of evidence between groups.
        col_evidence_ids = set(
            by_group["element:COL-C24"].evidence.values_list("id", flat=True)
        )
        bmd_evidence_ids = set(bmd_finding.evidence.values_list("id", flat=True))
        self.assertFalse(col_evidence_ids & bmd_evidence_ids)

    def test_single_source_deterministic_score(self):
        # GPR 'medium' severity, weight 1.0, confidence 0.8:
        # 0.50 * 1.0 * (0.5 + 0.5*0.8) = 0.45 -> 'medium'.
        make_evidence_record(
            self.project, "gpr", {"severity": "medium", "depth_m": 1.2},
            structural_element_id="FTG-07", confidence=0.8,
        )
        findings = CorrelationEngine.run(self.project, sync_sources=False)
        self.assertEqual(len(findings), 1)
        self.assertAlmostEqual(findings[0].risk_score, 0.45, places=3)
        self.assertEqual(findings[0].risk_level, "medium")

    def test_unscorable_evidence_yields_null_risk_not_fabricated(self):
        make_evidence_record(
            self.project, "document", {"title": "Permit copy"},
            structural_element_id="DOC-01",
        )
        findings = CorrelationEngine.run(self.project, sync_sources=False)
        self.assertEqual(len(findings), 1)
        self.assertIsNone(findings[0].risk_score)
        self.assertEqual(findings[0].risk_level, "info")
        self.assertIn("risk not computed", findings[0].reasoning)

    def test_correlation_is_isolated_per_project(self):
        make_evidence_record(
            self.project, "gpr", {"severity": "high"},
            structural_element_id="COL-C24",
        )
        other_project = Project.objects.create(name="Ajah Axis Project")
        make_evidence_record(
            other_project, "pundit", {"quality_grade": "poor"},
            structural_element_id="COL-C24",
        )

        findings = CorrelationEngine.run(self.project, sync_sources=False)
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        # Same element in another project must not be merged in.
        self.assertEqual(finding.evidence.count(), 1)
        self.assertEqual(finding.project, self.project)
        self.assertEqual(
            CorrelationFinding.objects.filter(project=other_project).count(), 0
        )

    def test_spatial_and_source_type_grouping(self):
        # Two records ~1m apart with coordinates but no element/GUID -> one
        # spatial cluster; a third with nothing at all -> source-type group.
        make_evidence_record(
            self.project, "live_stream",
            {"severity": "low", "stream": "cam-01"},
            coordinates={"latitude": 6.42810, "longitude": 3.42190},
        )
        make_evidence_record(
            self.project, "live_stream",
            {"severity": "low", "stream": "cam-02"},
            coordinates={"latitude": 6.42811, "longitude": 3.42190},
        )
        make_evidence_record(
            self.project, "other", {"note": "unlocated observation"},
        )

        findings = CorrelationEngine.run(self.project, sync_sources=False)
        groups = {f.group_key: f for f in findings}
        self.assertEqual(groups["spatial:cluster-1"].evidence.count(), 2)
        self.assertEqual(groups["source:other"].evidence.count(), 1)

    def test_rerun_updates_pending_finding_without_duplicates(self):
        make_evidence_record(
            self.project, "gpr", {"severity": "high"}, structural_element_id="COL-C24",
        )
        CorrelationEngine.run(self.project, sync_sources=False)
        CorrelationEngine.run(self.project, sync_sources=False)

        self.assertEqual(CorrelationFinding.objects.count(), 1)
        finding = CorrelationFinding.objects.get()
        self.assertEqual(finding.revision_count, 2)
        self.assertEqual(
            list(finding.revisions.values_list("change_reason", flat=True)),
            ["created", "new_evidence"],
        )

    def test_human_decision_is_never_overwritten_by_rerun(self):
        make_evidence_record(
            self.project, "gpr", {"severity": "high"}, structural_element_id="COL-C24",
        )
        finding = CorrelationEngine.run(self.project, sync_sources=False)[0]
        HumanReviewService.review(finding, self.user, "accept", notes="Verified")

        CorrelationEngine.run(self.project, sync_sources=False)

        finding.refresh_from_db()
        self.assertEqual(finding.status, "accepted")
        self.assertEqual(finding.reviewed_by, self.user)
        # The re-run opened a NEW pending finding for the group; it did not
        # resurrect or alter the human-reviewed one.
        self.assertEqual(CorrelationFinding.objects.count(), 2)
        self.assertEqual(
            CorrelationFinding.objects.filter(status="pending_review").count(), 1
        )


# ======================================================================
# 4. Strict Human-in-the-Loop — no automated path decides anything
# ======================================================================

class StrictHumanInTheLoopTestCase(TestCase):
    """The AI correlation engine, ingestion pipeline and background tasks are
    decision-support only: they must never set a review/decision state."""

    def setUp(self):
        self.user = User.objects.create_user(
            username="hitl_auditor@nexucon.com",
            email="hitl_auditor@nexucon.com",
            password="Password123!",
        )
        self.project = Project.objects.create(name="Ikorodu Depot Project")

    def test_engine_outputs_are_always_pending_human_review(self):
        make_evidence_record(
            self.project, "gpr", {"severity": "critical"}, structural_element_id="COL-C01",
            confidence=0.95,
        )
        make_evidence_record(
            self.project, "pundit", {"quality_grade": "very_poor"},
            structural_element_id="COL-C01",
        )

        CorrelationEngine.run(self.project, ingested_by=self.user, sync_sources=False)

        for finding in CorrelationFinding.objects.all():
            self.assertEqual(finding.status, "pending_review")
            self.assertIsNone(finding.reviewed_by)
            self.assertIsNone(finding.reviewed_at)
        # Even a critical multi-source finding stays decision-support only.
        self.assertEqual(
            CorrelationFinding.objects.filter(risk_level="critical").count(), 1
        )
        for analysis in AIAnalysisRecord.objects.all():
            self.assertTrue(analysis.requires_human_review)
            self.assertEqual(analysis.model_provider, "deterministic")

    def test_full_pipeline_leaves_all_findings_undecided(self):
        # Ingestion + the scheduled correlation task together must not decide.
        other_project = Project.objects.create(name="Ojota Interchange")
        make_evidence_record(
            self.project, "scan_defect", {"severity": "high"},
            structural_element_id="COL-C02",
        )
        make_evidence_record(
            other_project, "gpr", {"severity": "high"}, structural_element_id="FTG-09",
        )

        result = correlate_projects.apply().get()

        self.assertEqual(len(result), 2)
        for row in result:
            self.assertIn("findings", row)
        for finding in CorrelationFinding.objects.all():
            self.assertEqual(finding.status, "pending_review")
            self.assertIsNone(finding.reviewed_by)
        # No human acted, so no evidence decision audit events may exist.
        self.assertFalse(
            AuditEvent.objects.filter(action__startswith="evidence.").exists()
        )

    def test_only_human_review_service_transitions_decision_states(self):
        make_evidence_record(
            self.project, "gpr", {"severity": "high"}, structural_element_id="COL-C03",
        )
        finding = CorrelationEngine.run(self.project, sync_sources=False)[0]

        # A human decision through the service is what moves the state.
        reviewed, digest = HumanReviewService.review(
            finding, self.user, "accept", notes="Site verified with engineer"
        )
        self.assertEqual(reviewed.status, "accepted")
        self.assertEqual(reviewed.reviewed_by, self.user)
        self.assertIsNotNone(reviewed.reviewed_at)
        self.assertEqual(len(digest), 64)

        # ...and the immutable ledger records the human (not an AI) as actor.
        event = AuditEvent.objects.get(
            action="evidence.finding.accept", resource_id=str(finding.id)
        )
        self.assertEqual(event.user, self.user)
        self.assertEqual(event.user_name, self.user.get_full_name())
        self.assertEqual(event.severity, "High")
        self.assertEqual(event.new_state, {"status": "accepted", "risk_level": "high"})


# ======================================================================
# 5. Human review service — the statutory decision loop
# ======================================================================

class HumanReviewServiceTestCase(TestCase):
    def setUp(self):
        self.reviewer = User.objects.create_user(
            username="chief_reviewer@nexucon.com",
            email="chief_reviewer@nexucon.com",
            password="Password123!",
            first_name="Chidi",
            last_name="Nwosu",
        )
        self.project = Project.objects.create(name="Apapa Port Access Road")
        self.evidence = make_evidence_record(
            self.project, "gpr", {"severity": "high"}, structural_element_id="COL-C24",
        )

    def _pending_finding(self, **kwargs):
        finding = CorrelationFinding.objects.create(
            project=self.project,
            structural_element_id="COL-C24",
            title="COL-C24: HIGH multi-source risk",
            risk_level="high",
            risk_score=0.72,
            **kwargs,
        )
        finding.evidence.set([self.evidence])
        return finding

    def test_accept_decision(self):
        finding = self._pending_finding()
        finding, digest = HumanReviewService.review(
            finding, self.reviewer, "accept", notes="Corroborated by PUNDIT retest"
        )
        self.assertEqual(finding.status, "accepted")
        self.assertEqual(finding.review_notes, "Corroborated by PUNDIT retest")
        revision = finding.revisions.latest("revision_number")
        self.assertEqual(revision.change_reason, "decision")
        self.assertEqual(revision.snapshot["decision_hash"], digest)
        self.assertIn("accept", revision.notes)
        self.assertTrue(AuditEvent.objects.filter(
            action="evidence.finding.accept", user=self.reviewer,
            resource_id=str(finding.id),
        ).exists())

    def test_reject_decision(self):
        finding = self._pending_finding()
        finding, _ = HumanReviewService.review(finding, self.reviewer, "reject", notes="Dup")
        self.assertEqual(finding.status, "rejected")
        # Rejections are lower severity in the ledger than acceptances.
        event = AuditEvent.objects.get(
            action="evidence.finding.reject", resource_id=str(finding.id)
        )
        self.assertEqual(event.severity, "Normal")

    def test_modify_decision_applies_reviewer_changes(self):
        finding = self._pending_finding()
        finding, _ = HumanReviewService.review(
            finding, self.reviewer, "modify",
            notes="Risk overstated; GPR alone",
            modifications={"title": "COL-C24: MEDIUM risk (reviewer-adjusted)",
                           "risk_level": "medium", "risk_score": 0.4},
        )
        self.assertEqual(finding.status, "modified")
        self.assertEqual(finding.title, "COL-C24: MEDIUM risk (reviewer-adjusted)")
        self.assertEqual(finding.risk_level, "medium")
        self.assertEqual(finding.risk_score, 0.4)

        # A modified finding can be re-reviewed.
        finding, _ = HumanReviewService.review(finding, self.reviewer, "accept")
        self.assertEqual(finding.status, "accepted")

    def test_invalid_decision_rejected(self):
        finding = self._pending_finding()
        with self.assertRaises(ReviewError):
            HumanReviewService.review(finding, self.reviewer, "auto_approve")
        finding.refresh_from_db()
        self.assertEqual(finding.status, "pending_review")

    def test_resolved_finding_cannot_be_re_decided(self):
        finding = self._pending_finding()
        HumanReviewService.review(finding, self.reviewer, "accept")
        with self.assertRaises(ReviewError):
            HumanReviewService.review(finding, self.reviewer, "reject")
        finding.refresh_from_db()
        self.assertEqual(finding.status, "accepted")

    def test_request_supplementary_evidence_keeps_finding_pending(self):
        finding = self._pending_finding()
        finding, revision = HumanReviewService.request_supplementary_evidence(
            finding, self.reviewer, request_type="retest_ndt", notes="Re-run PUNDIT",
        )
        self.assertEqual(finding.status, "pending_review")
        self.assertEqual(revision.change_reason, "new_evidence")
        self.assertIn("retest_ndt", revision.notes)
        self.assertTrue(AuditEvent.objects.filter(
            action="evidence.finding.supplementary_request",
            resource_id=str(finding.id),
        ).exists())

    def test_request_supplementary_invalid_type_rejected(self):
        finding = self._pending_finding()
        with self.assertRaises(ReviewError):
            HumanReviewService.request_supplementary_evidence(
                finding, self.reviewer, request_type="skip_review",
            )

    def test_trigger_statutory_inspection(self):
        finding = self._pending_finding()
        inspection = HumanReviewService.trigger_inspection(
            finding, self.reviewer, inspection_type="Structural Review",
            priority="High", notes="Immediate verification required",
        )
        self.assertEqual(inspection.project, self.project)
        self.assertEqual(inspection.status, "REQUESTED")
        self.assertIn(finding.finding_reference, inspection.summary_notes)

        finding.refresh_from_db()
        self.assertEqual(finding.status, "escalated")
        self.assertEqual(finding.linked_inspection, inspection)
        self.assertEqual(finding.reviewed_by, self.reviewer)

        # One statutory inspection per finding.
        with self.assertRaises(ReviewError):
            HumanReviewService.trigger_inspection(finding, self.reviewer)

    def test_issue_ncr_with_corrective_action(self):
        finding = self._pending_finding()
        due = datetime.date(2026, 12, 31)
        ncr, capa = HumanReviewService.issue_ncr(
            finding, self.reviewer,
            corrective_action="Excavate and backfill void with flowable fill",
            corrective_due_date=due,
        )
        self.assertEqual(ncr.project, self.project)
        self.assertEqual(ncr.status, "Open")
        self.assertEqual(ncr.source, "SITE_MONITORING")
        self.assertEqual(ncr.source_reference, finding.finding_reference)
        self.assertEqual(ncr.reporter, self.reviewer)
        # 'high' risk maps to a Major NCR severity.
        self.assertEqual(ncr.severity, "Major")

        self.assertIsNotNone(capa)
        self.assertEqual(capa.ncr, ncr)
        self.assertEqual(capa.due_date, due)
        self.assertEqual(capa.status, "todo")

        finding.refresh_from_db()
        self.assertEqual(finding.status, "escalated")
        self.assertEqual(finding.linked_ncr, ncr)

        # A finding carries at most one NCR.
        with self.assertRaises(ReviewError):
            HumanReviewService.issue_ncr(finding, self.reviewer)

    def test_issue_ncr_from_rejected_finding_forbidden(self):
        finding = self._pending_finding()
        HumanReviewService.review(finding, self.reviewer, "reject")
        with self.assertRaises(ReviewError):
            HumanReviewService.issue_ncr(finding, self.reviewer)

    def test_director_signoff_requires_prior_human_decision(self):
        finding = self._pending_finding()
        with self.assertRaises(ReviewError):
            HumanReviewService.director_signoff(finding, self.reviewer, "approve")

        HumanReviewService.review(finding, self.reviewer, "accept")
        revision, digest = HumanReviewService.director_signoff(
            finding, self.reviewer, "approve", notes="Confirmed into official record"
        )
        self.assertEqual(len(digest), 64)
        self.assertEqual(revision.snapshot["director_signoff"]["decision"], "approve")
        self.assertEqual(revision.snapshot["director_signoff"]["signoff_hash"], digest)
        self.assertTrue(AuditEvent.objects.filter(
            action="evidence.finding.director_signoff",
            resource_id=str(finding.id), severity="Critical",
        ).exists())

    def test_director_signoff_invalid_decision(self):
        finding = self._pending_finding()
        HumanReviewService.review(finding, self.reviewer, "accept")
        with self.assertRaises(ReviewError):
            HumanReviewService.director_signoff(finding, self.reviewer, "sign")


# ======================================================================
# 6. HITL review API endpoints
# ======================================================================

class HITLReviewAPITestCase(APITestCase):
    """The review endpoints are the only API surface that can move a finding
    out of pending_review — and only for authenticated, scoped humans."""

    def setUp(self):
        self.reviewer = User.objects.create_superuser(
            username="review_director@nexucon.com",
            email="review_director@nexucon.com",
            password="Password123!",
            first_name="Ngozi",
            last_name="Eze",
        )
        self.plain_user = User.objects.create_user(
            username="field_engineer@nexucon.com",
            email="field_engineer@nexucon.com",
            password="Password123!",
        )
        self.project = Project.objects.create(name="Yaba Tech Hub Project")
        evidence = make_evidence_record(
            self.project, "gpr", {"severity": "high"}, structural_element_id="COL-C24",
        )
        self.finding = CorrelationFinding.objects.create(
            project=self.project,
            structural_element_id="COL-C24",
            group_key="element:COL-C24",
            title="COL-C24: HIGH multi-source risk",
            risk_level="high",
            risk_score=0.72,
        )
        self.finding.evidence.set([evidence])
        self.review_url = reverse(
            "correlation-finding-review", kwargs={"pk": str(self.finding.id)}
        )
        self.client.force_authenticate(user=self.reviewer)

    def test_authenticated_review_accept(self):
        response = self.client.post(self.review_url, {
            "decision": "accept", "notes": "Verified against retest data",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "accepted")
        self.assertEqual(len(response.data["decision_hash"]), 64)
        self.assertEqual(response.data["reviewed_by_name"], "Ngozi Eze")

        self.finding.refresh_from_db()
        self.assertEqual(self.finding.status, "accepted")
        self.assertEqual(self.finding.reviewed_by, self.reviewer)

        # Decision recorded in the immutable audit ledger.
        self.assertTrue(AuditEvent.objects.filter(
            action="evidence.finding.accept",
            resource_id=str(self.finding.id),
            user=self.reviewer,
        ).exists())

    def test_review_invalid_decision_returns_400(self):
        response = self.client.post(self.review_url, {"decision": "approve_all"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.finding.refresh_from_db()
        self.assertEqual(self.finding.status, "pending_review")

    def test_review_twice_returns_400(self):
        self.client.post(self.review_url, {"decision": "accept"}, format="json")
        response = self.client.post(self.review_url, {"decision": "reject"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.finding.refresh_from_db()
        self.assertEqual(self.finding.status, "accepted")

    def test_anonymous_review_is_rejected_and_changes_nothing(self):
        self.client.force_authenticate(user=None)
        response = self.client.post(self.review_url, {"decision": "accept"}, format="json")

        self.assertIn(response.status_code,
                      (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.finding.refresh_from_db()
        self.assertEqual(self.finding.status, "pending_review")
        self.assertIsNone(self.finding.reviewed_by)
        self.assertFalse(AuditEvent.objects.filter(
            resource_id=str(self.finding.id)
        ).exists())

    def test_unscoped_user_cannot_review(self):
        # Authenticated, but the finding's project is outside their scope.
        self.client.force_authenticate(user=self.plain_user)
        response = self.client.post(self.review_url, {"decision": "accept"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.finding.refresh_from_db()
        self.assertEqual(self.finding.status, "pending_review")

    def test_supplementary_evidence_request_endpoint(self):
        url = reverse("correlation-finding-supplementary", kwargs={"pk": str(self.finding.id)})
        response = self.client.post(url, {
            "request_type": "live_stream", "notes": "Show column live",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["status"], "requested")
        self.assertEqual(response.data["finding"], self.finding.finding_reference)
        self.finding.refresh_from_db()
        self.assertEqual(self.finding.status, "pending_review")
        self.assertEqual(self.finding.revisions.count(), 1)

    def test_trigger_inspection_endpoint(self):
        url = reverse("correlation-finding-trigger-inspection",
                      kwargs={"pk": str(self.finding.id)})
        response = self.client.post(url, {
            "inspection_type": "Structural Review", "priority": "High",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("inspection_reference", response.data)
        self.assertEqual(response.data["status"], "REQUESTED")
        self.assertTrue(Inspection.objects.filter(
            inspection_reference=response.data["inspection_reference"]
        ).exists())
        self.finding.refresh_from_db()
        self.assertEqual(self.finding.status, "escalated")

        # Second trigger on the same finding is refused.
        response = self.client.post(url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_issue_ncr_endpoint_with_corrective_action(self):
        url = reverse("correlation-finding-issue-ncr", kwargs={"pk": str(self.finding.id)})
        response = self.client.post(url, {
            "severity": "Critical",
            "corrective_action": "Evacuate zone and pressure-grout the void",
            "corrective_due_date": "2026-12-31",
        }, format="json")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("ncr_reference", response.data)
        self.assertEqual(response.data["severity"], "Critical")
        self.assertEqual(response.data["corrective_action"]["status"], "todo")

        self.finding.refresh_from_db()
        self.assertEqual(self.finding.status, "escalated")
        ncr = self.finding.linked_ncr
        self.assertEqual(ncr.severity, "Critical")
        capa = CorrectiveActionPlan.objects.get(ncr=ncr)
        self.assertEqual(str(capa.due_date), "2026-12-31")

    def test_issue_ncr_invalid_due_date_returns_400(self):
        url = reverse("correlation-finding-issue-ncr", kwargs={"pk": str(self.finding.id)})
        response = self.client.post(url, {"corrective_due_date": "31/12/2026"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(NonConformanceReport.objects.exists())

    def test_anonymous_cannot_issue_ncr(self):
        self.client.force_authenticate(user=None)
        url = reverse("correlation-finding-issue-ncr", kwargs={"pk": str(self.finding.id)})
        response = self.client.post(url, {"severity": "Critical"}, format="json")
        self.assertIn(response.status_code,
                      (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.assertFalse(NonConformanceReport.objects.exists())
        self.finding.refresh_from_db()
        self.assertEqual(self.finding.status, "pending_review")

    def test_ai_diagnose_action_returns_acoustic_inversion_and_ncr_draft(self):
        url = reverse("correlation-finding-ai-diagnose", kwargs={"pk": str(self.finding.id)})
        response = self.client.post(url, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("acoustic_inversion", response.data)
        self.assertIn("estimated_velocity_km_s", response.data["acoustic_inversion"])
        self.assertIn("standards_compliance", response.data)
        self.assertIn("recommended_corrective_actions", response.data)
        self.assertIn("ncr_remedial_draft", response.data)
        self.assertEqual(response.data["finding_reference"], self.finding.finding_reference)

    def test_ai_diagnose_confidence_is_evidence_confidence_not_risk(self):
        # 7 Sep meeting item 6: the diagnostic JSON used to echo the finding's
        # risk score as confidence_score, so a 0.78 HIGH risk displayed as
        # "AI Confidence: 78%". It must carry the evidence confidence.
        # This finding's risk is 0.72 but its evidence confidence is 0.93.
        self.finding.evidence.update(confidence=0.93)
        url = reverse("correlation-finding-ai-diagnose", kwargs={"pk": str(self.finding.id)})
        response = self.client.post(url, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["confidence_score"], 93)
        self.assertNotEqual(response.data["confidence_score"],
                            round(self.finding.risk_score * 100))

        # No evidence confidence recorded -> null, never the risk in disguise.
        self.finding.evidence.update(confidence=None)
        response = self.client.post(url, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["confidence_score"])

    def test_director_signoff_requires_director_role(self):
        self.client.force_authenticate(user=self.plain_user)
        url = reverse("correlation-finding-director-signoff",
                      kwargs={"pk": str(self.finding.id)})
        response = self.client.post(url, {"decision": "approve"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_director_signoff_flow(self):
        # Sign-off requires a prior human decision on the finding.
        url = reverse("correlation-finding-director-signoff",
                      kwargs={"pk": str(self.finding.id)})
        response = self.client.post(url, {"decision": "approve"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        self.client.post(self.review_url, {"decision": "accept"}, format="json")
        response = self.client.post(url, {"decision": "approve", "notes": "Final"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["signoff_hash"]), 64)
        self.assertTrue(AuditEvent.objects.filter(
            action="evidence.finding.director_signoff",
            resource_id=str(self.finding.id),
        ).exists())


# ======================================================================
# 7. API permissions — authentication, read-only registry, scoping
# ======================================================================

class EvidenceAPIPermissionsTestCase(APITestCase):
    def setUp(self):
        self.superuser = User.objects.create_superuser(
            username="hq_overseer@nexucon.com",
            email="hq_overseer@nexucon.com",
            password="Password123!",
        )
        self.plain_user = User.objects.create_user(
            username="external_viewer@nexucon.com",
            email="external_viewer@nexucon.com",
            password="Password123!",
        )
        self.district_a = District.objects.create(name="Eti-Osa District", code="ETI")
        self.district_b = District.objects.create(name="Ikorodu District", code="IKD")
        self.district_user = User.objects.create_user(
            username="eti_osa_officer@nexucon.com",
            email="eti_osa_officer@nexucon.com",
            password="Password123!",
        )
        Profile.objects.create(user=self.district_user, district=self.district_a)

        self.project_a = Project.objects.create(
            name="Project Alpha", district=self.district_a,
        )
        self.project_b = Project.objects.create(
            name="Project Beta", district=self.district_b,
        )
        self.record_a = make_evidence_record(
            self.project_a, "gpr", {"severity": "high"}, structural_element_id="COL-A1",
        )
        self.record_b = make_evidence_record(
            self.project_b, "gpr", {"severity": "low"}, structural_element_id="COL-B1",
        )
        self.finding_a = CorrelationFinding.objects.create(
            project=self.project_a, title="Alpha finding", risk_level="low",
        )
        self.finding_b = CorrelationFinding.objects.create(
            project=self.project_b, title="Beta finding", risk_level="low",
        )

        self.records_url = reverse("evidence-record-list")
        self.analyses_url = reverse("ai-analysis-list")
        self.findings_url = reverse("correlation-finding-list")

    @staticmethod
    def _list_payload(response):
        """Return list results whether or not pagination is active."""
        data = response.data
        if isinstance(data, dict) and "results" in data:
            return data["results"]
        return data

    def test_list_endpoints_require_authentication(self):
        for url in (self.records_url, self.analyses_url, self.findings_url):
            response = self.client.get(url)
            self.assertIn(response.status_code,
                          (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
                          msg=url)

    def test_detail_endpoints_require_authentication(self):
        urls = (
            reverse("evidence-record-detail", kwargs={"pk": str(self.record_a.id)}),
            reverse("correlation-finding-detail", kwargs={"pk": str(self.finding_a.id)}),
        )
        for url in urls:
            response = self.client.get(url)
            self.assertIn(response.status_code,
                          (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN),
                          msg=url)

    def test_records_viewset_is_read_only(self):
        # Registry records are produced by the ingestion pipeline only.
        self.client.force_authenticate(user=self.superuser)
        response = self.client.post(self.records_url, {
            "project": str(self.project_a.id), "source_type": "gpr", "payload": {},
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(EvidenceRecord.objects.count(), 2)

    def test_superuser_sees_all_records(self):
        self.client.force_authenticate(user=self.superuser)
        response = self.client.get(self.records_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        returned_ids = {item["id"] for item in self._list_payload(response)}
        self.assertEqual(returned_ids, {str(self.record_a.id), str(self.record_b.id)})

    def test_unscoped_user_sees_no_records(self):
        # Authenticated but no role, district or developer link -> no scope.
        self.client.force_authenticate(user=self.plain_user)
        response = self.client.get(self.records_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(self._list_payload(response)), 0)

    def test_district_user_sees_only_own_district(self):
        self.client.force_authenticate(user=self.district_user)
        response = self.client.get(self.records_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = self._list_payload(response)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], str(self.record_a.id))

        # Cross-district detail access is a 404 (out of scope).
        url = reverse("evidence-record-detail", kwargs={"pk": str(self.record_b.id)})
        self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

        # Cross-district findings are equally invisible.
        response = self.client.get(self.findings_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = self._list_payload(response)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], str(self.finding_a.id))

    def test_hq_overview_requires_state_hq_or_district(self):
        self.client.force_authenticate(user=self.plain_user)
        response = self.client.get(reverse("hq-overview"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_hq_overview_district_user_gets_own_district(self):
        self.client.force_authenticate(user=self.district_user)
        response = self.client.get(reverse("hq-overview"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("projects", response.data)

    def test_hq_director_endpoints_require_director(self):
        self.client.force_authenticate(user=self.plain_user)
        for name in ("hq-district-matrix", "hq-executive-briefing", "hq-inspector-analytics"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, msg=name)

    @patch("apps.common.ai_service.AIService.generate_structured_json",
           side_effect=AIProviderUnavailable("no provider configured"))
    def test_hq_director_endpoints_serve_directors(self, _mock_ai):
        self.client.force_authenticate(user=self.superuser)
        for name in ("hq-overview", "hq-district-matrix", "hq-executive-briefing",
                     "hq-inspector-analytics"):
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, status.HTTP_200_OK, msg=name)

    @patch("apps.common.ai_service.AIService.generate_structured_json",
           side_effect=AIProviderUnavailable("no provider configured"))
    def test_project_intelligence_scoped(self, _mock_ai):
        self.client.force_authenticate(user=self.superuser)
        url = reverse("project-intelligence", kwargs={"project_id": str(self.project_a.id)})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("risk_scores", response.data)
        self.assertEqual(response.data["project_id"], str(self.project_a.id))

        # Out-of-scope project is a 404 for a user without HQ visibility.
        self.client.force_authenticate(user=self.plain_user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_correlate_endpoint_requires_authentication(self):
        response = self.client.post(
            reverse("project-correlate", kwargs={"project_id": str(self.project_a.id)})
        )
        self.assertIn(response.status_code,
                      (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_correlate_endpoint_runs_engine_and_audits(self):
        self.client.force_authenticate(user=self.superuser)
        url = reverse("project-correlate", kwargs={"project_id": str(self.project_a.id)})
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(response.data["findings_created_or_updated"], 1)
        self.assertTrue(AuditEvent.objects.filter(
            action="evidence.correlation.run",
            resource_type="Project",
            resource_id=str(self.project_a.id),
            user=self.superuser,
        ).exists())
        # Correlating is not deciding: the new finding awaits a human.
        finding = CorrelationFinding.objects.get(
            project=self.project_a, group_key="element:COL-A1"
        )
        self.assertEqual(finding.status, "pending_review")
        self.assertEqual(finding.evidence.count(), 1)


# ======================================================================
# 8. AI risk intelligence — deterministic, decision-support only
# ======================================================================

@patch("apps.common.ai_service.AIService.generate_structured_json",
       side_effect=AIProviderUnavailable("no provider configured"))
class ProjectIntelligenceTestCase(TestCase):
    def setUp(self):
        self.project = Project.objects.create(
            name="Ogudu Grade Separation", status="ACTIVE",
        )

    def _finding(self, *, risk_level, risk_score, status="pending_review",
                 source_type="gpr", element="COL-X01"):
        evidence = make_evidence_record(
            self.project, source_type, {"severity": "low"},
            structural_element_id=element,
        )
        finding = CorrelationFinding.objects.create(
            project=self.project, structural_element_id=element,
            title=f"{element} risk", risk_level=risk_level, risk_score=risk_score,
            status=status,
        )
        finding.evidence.set([evidence])
        return finding

    def test_structural_risk_is_worst_structural_finding(self, _mock):
        self._finding(risk_level="high", risk_score=0.72, source_type="gpr")
        self._finding(risk_level="medium", risk_score=0.50, source_type="document",
                      element="DOC-X01")

        summary = ProjectIntelligenceService.aggregate(self.project)

        self.assertEqual(summary["risk_scores"]["structural"], 0.72)
        self.assertEqual(summary["risk_scores"]["structural_level"], "high")
        self.assertEqual(summary["metrics"]["open_correlation_findings"], 2)
        # Both findings pending -> the reviewer backlog recommendation fires.
        sources = {a["source"] for a in summary["recommended_actions"]}
        self.assertIn("human_review_backlog", sources)
        self.assertIn("structural_risk", sources)  # 0.72 >= 0.65

    def test_rejected_findings_excluded_from_structural_risk(self, _mock):
        self._finding(risk_level="critical", risk_score=0.95, status="rejected")
        summary = ProjectIntelligenceService.aggregate(self.project)
        self.assertIsNone(summary["risk_scores"]["structural"])
        self.assertEqual(summary["risk_scores"]["structural_level"], "info")
        self.assertIsNone(summary["risk_scores"]["overall"])

    def test_compliance_risk_from_open_critical_ncr(self, _mock):
        NonConformanceReport.objects.create(
            project=self.project, title="Unauthorized deviation",
            description="Column cast outside approved gridline",
            severity="Critical", status="Open",
        )
        summary = ProjectIntelligenceService.aggregate(self.project)

        # 0.60 base + 0.15 per critical open NCR.
        self.assertEqual(summary["risk_scores"]["compliance"], 0.75)
        self.assertEqual(summary["risk_scores"]["compliance_level"], "high")
        self.assertEqual(summary["metrics"]["critical_open_ncrs"], 1)
        self.assertEqual(summary["risk_scores"]["overall"], 0.75)

    def test_critical_finding_recommends_statutory_escalation(self, _mock):
        self._finding(risk_level="critical", risk_score=0.9)
        summary = ProjectIntelligenceService.aggregate(self.project)
        urgent = [a for a in summary["recommended_actions"]
                  if a["source"] == "critical_finding"]
        self.assertEqual(len(urgent), 1)
        self.assertEqual(urgent[0]["priority"], "Urgent")

    def test_empty_project_reports_no_data_not_fabricated(self, _mock):
        empty_project = Project.objects.create(name="Greenfield Site")
        summary = ProjectIntelligenceService.aggregate(empty_project)

        self.assertIsNone(summary["risk_scores"]["structural"])
        self.assertIsNone(summary["risk_scores"]["compliance"])
        self.assertIsNone(summary["risk_scores"]["overall"])
        self.assertEqual(summary["metrics"]["open_correlation_findings"], 0)
        # Deterministic fallback observations, never fabricated narrative.
        self.assertIsInstance(summary["ai_observations"], list)
        self.assertIn("no data", summary["ai_observations"][0])
        # Intelligence is read-only: nothing was decided about compliance.
        self.assertEqual(CorrelationFinding.objects.count(), 0)

    def test_intelligence_never_changes_finding_states(self, _mock):
        finding = self._finding(risk_level="high", risk_score=0.7)
        ProjectIntelligenceService.aggregate(self.project)
        ProjectIntelligenceService.aggregate(self.project)
        finding.refresh_from_db()
        # Aggregating risk twice must not accept, reject or review anything.
        self.assertEqual(finding.status, "pending_review")
        self.assertIsNone(finding.reviewed_by)


# ======================================================================
# 9. Background tasks
# ======================================================================

class EvidenceTasksTestCase(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Festac Link Road")
        make_evidence_record(
            self.project, "gpr", {"severity": "medium"}, structural_element_id="COL-T01",
        )

    def test_correlate_projects_task(self):
        other_project = Project.objects.create(name="Oshodi Loop")
        make_evidence_record(
            other_project, "pundit", {"quality_grade": "poor"},
            structural_element_id="COL-T02",
        )

        result = correlate_projects.apply().get()

        self.assertEqual(len(result), 2)
        self.assertEqual(CorrelationFinding.objects.count(), 2)
        for row in result:
            self.assertIn("project", row)
            self.assertEqual(row["findings"], 1)
        # Scheduled runs are decision-support only.
        self.assertTrue(all(
            f.status == "pending_review" for f in CorrelationFinding.objects.all()
        ))

    def test_detect_recurring_anomalies_task(self):
        CorrelationFinding.objects.create(
            project=self.project, structural_element_id="COL-T01",
            title="T01 void", risk_level="high", risk_score=0.7,
        )
        CorrelationFinding.objects.create(
            project=self.project, structural_element_id="COL-T01",
            title="T01 second void", risk_level="medium", risk_score=0.5,
        )
        # Rejected findings do not count towards recurrence.
        CorrelationFinding.objects.create(
            project=self.project, structural_element_id="COL-T09",
            title="T09 dismissed", risk_level="low", risk_score=0.2,
            status="rejected",
        )

        result = detect_recurring_anomalies.apply().get()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["structural_element_id"], "COL-T01")
        self.assertEqual(result[0]["findings"], 2)
        self.assertEqual(result[0]["worst_risk"], "high")
        notification = InAppNotification.objects.get()
        self.assertEqual(notification.type, "warning")
        self.assertIn("COL-T01", notification.title)
        self.assertIn("persistent issue", notification.message)


class ManualFindingLoggingTestCase(APITestCase):
    """7 Sep meeting item 6: a manually logged field finding carries its risk
    on the finding — its evidence carries NO confidence, and the serializer
    reports confidence as null rather than echoing the risk score (a 0.93
    risk was being displayed as "93% confidence")."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username="field_logger@nexucon.com",
            email="field_logger@nexucon.com",
            password="Password123!",
            first_name="Sunkanmi",
            last_name="Olowonishaye",
        )
        self.project = Project.objects.create(name="Ikoyi Manual Log Test")
        self.client.force_authenticate(user=self.user)
        self.list_url = reverse("correlation-finding-list")

    def test_manual_finding_stores_risk_but_no_evidence_confidence(self):
        response = self.client.post(self.list_url, {
            "project": str(self.project.id),
            "structural_element_name": "Floor:200THK RC SLAB",
            "structural_element_guid": "232RR9q4f7eQ9Jps1smHAy",
            "title": "rebar spacing",
            "description": "include scan reference",
            "taxonomy": "REBAR_SPACING_DEFICIENCY",
            "severity": "HIGH",
            "depth_mm": 100,
            "deviation_mm": 50,
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        finding = CorrelationFinding.objects.get(id=response.data["id"])
        self.assertEqual(finding.risk_level, "high")
        self.assertAlmostEqual(finding.risk_score, 0.93)
        # The risk stays on the finding; the evidence carries a computed confidence based on completeness.
        evidence = finding.evidence.get()
        self.assertAlmostEqual(evidence.confidence, 0.93)
        # Technical parameters are preserved verbatim in the description.
        self.assertIn("Technical Parameters:", finding.description)
        self.assertIn("Depth: 100 mm", finding.description)
        self.assertIn("Variance: 50 mm", finding.description)

        # The serialized confidence reflects the evidence confidence (0.93), never the risk score.
        self.assertIn("confidence", response.data)
        self.assertEqual(response.data["confidence"], 0.93)
        # A human log is labelled as one, not as AI-inferred.
        self.assertTrue(response.data["logged_manually"])


# ======================================================================
# backfill_evidence_confidence management command
# ======================================================================

class BackfillEvidenceConfidenceTestCase(TestCase):
    """Rows created before the evidence-based confidence code landed still
    carry the old risk-as-confidence values (0.78 for HIGH). The backfill
    command recomputes them; dry run by default."""

    def setUp(self):
        from django.core.management import call_command
        self.call_command = call_command
        self.project = Project.objects.create(name="Backfill Confidence Test")

        # Old-style manual finding evidence: risk 0.78 stored as confidence,
        # technical parameters in the payload.
        self.manual_with_params = make_evidence_record(
            self.project, "other", {"severity": "high", "depth_mm": 100,
                                    "deviation_mm": 50},
            confidence=0.78,
        )
        self.manual_with_params.source_model = "digital_eye.ManualFinding"
        self.manual_with_params.save()

        # Old-style severity echo on a generic evidence record.
        self.generic_high = make_evidence_record(
            self.project, "gpr", {"severity": "high"}, confidence=0.78,
        )

        # A confidence value that is already correct must not be touched.
        self.already_ok = make_evidence_record(
            self.project, "gpr", {"severity": "high"}, confidence=0.93,
        )

        # Null confidence (no computable basis) stays null.
        self.null_conf = make_evidence_record(
            self.project, "gpr", {"severity": "low"}, confidence=None,
        )

    def test_dry_run_reports_without_writing(self):
        out = StringIO()
        self.call_command("backfill_evidence_confidence", stdout=out)
        text = out.getvalue()
        self.assertIn("Dry run", text)
        self.manual_with_params.refresh_from_db()
        self.assertAlmostEqual(self.manual_with_params.confidence, 0.78)

    def test_execute_updates_stale_values_only(self):
        out = StringIO()
        self.call_command("backfill_evidence_confidence", "--execute", stdout=out)
        self.manual_with_params.refresh_from_db()
        self.generic_high.refresh_from_db()
        self.already_ok.refresh_from_db()
        self.null_conf.refresh_from_db()

        # Manual finding with tech params -> completeness confidence.
        self.assertAlmostEqual(self.manual_with_params.confidence, 0.93)
        # Generic severity echo -> corrected severity mapping.
        self.assertAlmostEqual(self.generic_high.confidence, 0.93)
        # Correct value untouched.
        self.assertAlmostEqual(self.already_ok.confidence, 0.93)
        # No basis -> stays null, never invented.
        self.assertIsNone(self.null_conf.confidence)

        # Integrity hash re-computed for the changed rows.
        self.assertNotEqual(self.generic_high.evidence_hash, "")


@override_settings(SECURE_SSL_REDIRECT=False)
class FieldPhotoEvidenceUploadTests(APITestCase):
    def setUp(self):
        from apps.government.models import Role
        self.role, _ = Role.objects.get_or_create(name="Inspector")
        self.district = District.objects.create(name="Eti-Osa District", code="ETI")
        self.user = User.objects.create_user(
            username="field_inspector",
            email="field_inspector@nexucon.ng",
            password="testpassword123",
            first_name="Babajide",
            last_name="Inspector",
        )
        Profile.objects.create(user=self.user, role=self.role, district=self.district)
        self.project = Project.objects.create(
            name="Eti-Osa Towers Phase 1",
            district=self.district,
            status="active",
        )
        self.client.force_authenticate(user=self.user)
        self.image_bytes = b"real_jpeg_binary_field_test_photo_sample_12345"
        self.expected_sha256 = hashlib.sha256(self.image_bytes).hexdigest()

    def test_upload_photo_evidence_success(self):
        uploaded_file = SimpleUploadedFile("column_c24_crack.jpg", self.image_bytes, content_type="image/jpeg")
        url = reverse("evidence-upload")
        response = self.client.post(url, {
            "project": str(self.project.id),
            "structural_element_id": "Column C24",
            "category": "honeycombing",
            "severity": "HIGH",
            "description": "Severe aggregate segregation observed during UPV test on Column C24.",
            "sha256": self.expected_sha256,
            "coordinates": json.dumps({"latitude": 6.43, "longitude": 3.48, "accuracy": 3.5}),
            "file": uploaded_file,
        }, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        data = response.json()
        self.assertEqual(data["source_type"], "photo")
        self.assertEqual(data["evidence_hash"], self.expected_sha256)
        self.assertEqual(data["structural_element_id"], "Column C24")
        self.assertIsNotNone(data["photo_url"])
        self.assertIsNotNone(data["file"])
        self.assertEqual(data["file"]["sha256_hash"], self.expected_sha256)

        # Check EvidenceRecord exists in db
        record = EvidenceRecord.objects.get(pk=data["id"])
        self.assertEqual(record.evidence_hash, self.expected_sha256)
        self.assertEqual(record.project, self.project)

        # Check mirrored VisualObservation exists in digital_eye
        from apps.digital_eye.models import VisualObservation
        obs = VisualObservation.objects.filter(project=self.project, structural_element="Column C24").first()
        self.assertIsNotNone(obs)
        self.assertEqual(obs.category, "honeycombing")
        self.assertEqual(obs.photos.count(), 1)
        self.assertEqual(obs.photos.first().sha256_checksum, self.expected_sha256)

    def test_upload_photo_evidence_sha256_mismatch_rejected(self):
        uploaded_file = SimpleUploadedFile("corrupted.jpg", self.image_bytes, content_type="image/jpeg")
        url = reverse("evidence-upload")
        response = self.client.post(url, {
            "project": str(self.project.id),
            "structural_element_id": "Column C24",
            "sha256": "0000000000000000000000000000000000000000000000000000000000000000",
            "file": uploaded_file,
        }, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("mismatch", response.json()["detail"].lower())

    def test_verify_photo_evidence(self):
        uploaded_file = SimpleUploadedFile("beam_b12.jpg", self.image_bytes, content_type="image/jpeg")
        upload_res = self.client.post(reverse("evidence-upload"), {
            "project": str(self.project.id),
            "structural_element_id": "Beam B12",
            "file": uploaded_file,
        }, format="multipart")
        self.assertEqual(upload_res.status_code, status.HTTP_201_CREATED)
        record_id = upload_res.json()["id"]

        verify_url = reverse("evidence-verify-uuid", kwargs={"pk": record_id})
        verify_res = self.client.post(verify_url)
        self.assertEqual(verify_res.status_code, status.HTTP_200_OK)
        vdata = verify_res.json()
        self.assertTrue(vdata["payload_ok"])
        self.assertTrue(vdata["file_bytes_ok"])
        self.assertEqual(vdata["file_sha256"], self.expected_sha256)


# ======================================================================
# File evidence (Inspector PWA Part 3 — upload, and verify what was stored)
#
# The tests below run against a temporary MEDIA_ROOT so no capture is left
# behind in the repository's media directory.
# ======================================================================

_TEST_MEDIA_ROOT = tempfile.mkdtemp(prefix='nexucon_evidence_tests_media_')

local_storage_settings = override_settings(
    STORAGES={
        'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'},
        'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'},
    },
    MEDIA_ROOT=_TEST_MEDIA_ROOT,
    MEDIA_URL='/media/',
)


class _Bytes:
    """A minimal stand-in for a Django ``UploadedFile``.

    ``chunks()`` is what Django's storage reads, ``name`` is what it stores
    under, and ``size`` is what the service checks the ceiling against. Using
    the real class would drag in a request for no gain.
    """

    def __init__(self, content, name, content_type='image/jpeg'):
        if isinstance(content, str):
            content = content.encode('utf-8')
        self._content = content
        self.name = name
        self.size = len(content)
        self.content_type = content_type

    def chunks(self, chunk_size=8192):
        for start in range(0, len(self._content), chunk_size):
            yield self._content[start:start + chunk_size]

    def read(self, size=-1):
        return self._content


def _sha256(content):
    if isinstance(content, str):
        content = content.encode('utf-8')
    return hashlib.sha256(content).hexdigest()


def _stored_files():
    """Every file currently under the test MEDIA_ROOT, as relative paths."""
    found = []
    for root, _dirs, files in os.walk(_TEST_MEDIA_ROOT):
        for name in files:
            found.append(
                os.path.relpath(os.path.join(root, name), _TEST_MEDIA_ROOT)
                .replace('\\', '/'))
    return sorted(found)


def _purge_media_root():
    for root, dirs, files in os.walk(_TEST_MEDIA_ROOT, topdown=False):
        for name in files:
            os.remove(os.path.join(root, name))
        for name in dirs:
            try:
                os.rmdir(os.path.join(root, name))
            except OSError:
                pass


class _CleanMediaMixin:
    """A clean MEDIA_ROOT for every test.

    The root itself is module-level so the storage override can be a class
    decorator, but the files inside it are not shared: several tests assert on
    exactly what was written, and a capture left by the previous test would make
    "nothing was stored" pass or fail for the wrong reason.
    """

    def setUp(self):
        super().setUp()
        _purge_media_root()
        self.addCleanup(_purge_media_root)


@local_storage_settings
class EvidenceFileServiceTestCase(_CleanMediaMixin, TestCase):
    """Storing bytes, and re-reading them later to answer honestly."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(name='File Evidence Project')
        self.user = User.objects.create_user(
            username='file_uploader@nexucon.com',
            email='file_uploader@nexucon.com',
            password='Password123!',
        )

    def _record(self, **payload):
        return EvidenceRecord.objects.create(
            project=self.project, source_type='uploaded_file',
            payload=payload or {'file_name': 'cover.jpg'},
        )

    def _store(self, content=b'\x89PNG cover photo bytes', name='cover.jpg', **kwargs):
        record = kwargs.pop('record', None) or self._record()
        return record, EvidenceFileService.store(
            record=record, uploaded_file=_Bytes(content, name), **kwargs)

    # -- storing -------------------------------------------------------

    def test_the_stored_hash_is_the_hash_of_the_bytes_on_disk(self):
        content = b'\x89PNG real capture bytes'
        _record, evidence_file = self._store(content)

        self.assertEqual(evidence_file.sha256_hash, _sha256(content))
        self.assertEqual(evidence_file.file_size_bytes, len(content))
        self.assertEqual(evidence_file.file_name, 'cover.jpg')

        # And the same again, read back off the disk rather than trusted.
        with open(evidence_file.file.path, 'rb') as handle:
            self.assertEqual(hashlib.sha256(handle.read()).hexdigest(),
                             evidence_file.sha256_hash)

    def test_the_stored_path_is_keyed_on_the_record_not_the_client_name(self):
        record, evidence_file = self._store()

        self.assertEqual(
            evidence_file.storage_name,
            f'evidence/{self.project.id}/{record.evidence_reference}/cover.jpg')
        self.assertEqual(_stored_files(), [evidence_file.storage_name])

    def test_a_path_in_the_filename_is_reduced_to_a_name(self):
        _record, evidence_file = self._store(name='../../etc/passwd')

        self.assertEqual(evidence_file.file_name, 'passwd')
        self.assertTrue(evidence_file.storage_name.endswith('/passwd'))
        self.assertNotIn('..', evidence_file.storage_name)

    def test_a_second_file_for_one_record_is_refused(self):
        record, first = self._store()
        before = _stored_files()

        with self.assertRaises(EvidenceFileError) as caught:
            EvidenceFileService.store(
                record=record, uploaded_file=_Bytes(b'other', 'other.jpg'))

        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(_stored_files(), before)
        self.assertEqual(EvidenceFile.objects.filter(record=record).count(), 1)
        self.assertEqual(EvidenceFile.objects.get(record=record).id, first.id)

    def test_an_empty_file_is_refused_and_leaves_nothing(self):
        with self.assertRaises(EvidenceFileError) as caught:
            self._store(b'', name='empty.jpg')

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn('empty', str(caught.exception).lower())
        self.assertEqual(_stored_files(), [])
        self.assertFalse(EvidenceFile.objects.exists())

    def test_a_file_above_the_upload_ceiling_is_refused(self):
        with override_settings(EVIDENCE_MAX_UPLOAD_BYTES=8):
            with self.assertRaises(EvidenceFileError) as caught:
                self._store(b'x' * 9)

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn('9 bytes', str(caught.exception))
        self.assertEqual(_stored_files(), [])

    def test_a_client_hash_that_disagrees_with_storage_is_refused(self):
        with self.assertRaises(EvidenceFileError) as caught:
            self._store(b'real bytes', expected_sha256='a' * 64)

        message = str(caught.exception)
        self.assertEqual(caught.exception.status_code, 400)
        # The message names both digests, so the caller can see which end is wrong.
        self.assertIn('a' * 64, message)
        self.assertIn(_sha256(b'real bytes'), message)
        self.assertEqual(_stored_files(), [])
        self.assertFalse(EvidenceFile.objects.exists())

    def test_a_matching_client_hash_is_accepted(self):
        content = b'verified capture'
        _record, evidence_file = self._store(
            content, expected_sha256=_sha256(content).upper())

        self.assertEqual(evidence_file.sha256_hash, _sha256(content))

    def test_a_client_size_that_disagrees_with_storage_is_refused(self):
        with self.assertRaises(EvidenceFileError) as caught:
            self._store(b'twelve bytes', expected_size=99)

        self.assertEqual(caught.exception.status_code, 400)
        self.assertIn('99', str(caught.exception))
        self.assertEqual(_stored_files(), [])

    def test_nothing_is_recorded_when_the_file_cannot_be_stored(self):
        with mock.patch(
            'django.core.files.storage.FileSystemStorage.size',
            side_effect=OSError('storage unavailable'),
        ):
            with self.assertRaises(EvidenceFileError):
                self._store()

        # The bytes written before the failure are cleaned up, not orphaned.
        self.assertEqual(_stored_files(), [])
        self.assertFalse(EvidenceFile.objects.exists())

    # -- verifying -----------------------------------------------------

    def test_verify_reports_both_hashes_when_everything_is_intact(self):
        record, evidence_file = self._store()
        record.evidence_hash = record.compute_hash()
        record.save(update_fields=['evidence_hash'])

        result = EvidenceFileService.verify(record)

        self.assertTrue(result['payload_ok'])
        self.assertTrue(result['file_bytes_ok'])
        self.assertTrue(result['file_present'])
        self.assertEqual(result['file_sha256'], evidence_file.sha256_hash)
        self.assertEqual(result['note'], '')
        self.assertEqual(result['evidence_reference'], record.evidence_reference)

    def test_verify_records_its_outcome_on_the_file(self):
        record, evidence_file = self._store()

        self.assertIsNone(evidence_file.last_verify_ok)  # nothing has run yet

        EvidenceFileService.verify(record)

        evidence_file.refresh_from_db()
        self.assertTrue(evidence_file.last_verify_ok)
        self.assertIsNotNone(evidence_file.last_verified_at)
        self.assertEqual(evidence_file.last_verify_note, '')

    def test_a_changed_payload_is_reported_without_touching_the_file_result(self):
        record, _file = self._store()
        record.evidence_hash = record.compute_hash()
        record.save(update_fields=['evidence_hash'])

        record.payload = {'file_name': 'cover.jpg', 'added': 'after ingest'}
        record.save(update_fields=['payload'])

        result = EvidenceFileService.verify(record)

        self.assertFalse(result['payload_ok'])
        # The bytes are untouched, and saying otherwise would be the false
        # negative that makes the whole endpoint useless.
        self.assertTrue(result['file_bytes_ok'])

    def test_bytes_replaced_in_storage_are_reported(self):
        record, evidence_file = self._store(b'original capture bytes')

        with open(evidence_file.file.path, 'wb') as handle:
            handle.write(b'a different file entirely')

        result = EvidenceFileService.verify(record)

        self.assertFalse(result['file_bytes_ok'])
        self.assertEqual(result['file_sha256'], _sha256(b'original capture bytes'))
        self.assertIn('changed or replaced', result['note'])

        evidence_file.refresh_from_db()
        self.assertFalse(evidence_file.last_verify_ok)
        self.assertIn('changed or replaced', evidence_file.last_verify_note)

    def test_a_record_with_no_file_says_so_rather_than_failing(self):
        record = self._record()
        record.evidence_hash = record.compute_hash()
        record.save(update_fields=['evidence_hash'])

        result = EvidenceFileService.verify(record)

        self.assertFalse(result['file_present'])
        # Not False: nothing was checked, so nothing failed.
        self.assertIsNone(result['file_bytes_ok'])
        self.assertIsNone(result['file_sha256'])
        self.assertTrue(result['payload_ok'])
        self.assertIn('No file is attached', result['note'])

    def test_a_file_too_large_to_re_read_reports_none_not_true(self):
        record, evidence_file = self._store(b'x' * 64, name='big.jpg')

        with override_settings(EVIDENCE_VERIFY_MAX_BYTES=8):
            result = EvidenceFileService.verify(record)

        self.assertIsNone(result['file_bytes_ok'])
        self.assertTrue(result['file_present'])
        self.assertIn('not checked', result['note'])

        evidence_file.refresh_from_db()
        self.assertIsNone(evidence_file.last_verify_ok)
        self.assertIsNotNone(evidence_file.last_verified_at)

    def test_unreadable_storage_reports_none_with_the_reason(self):
        record, _file = self._store()

        with mock.patch.object(
            EvidenceFile, 'compute_stored_hash',
            side_effect=OSError('the bucket is gone'),
        ):
            result = EvidenceFileService.verify(record)

        self.assertIsNone(result['file_bytes_ok'])
        self.assertIn('OSError', result['note'])

    def test_the_file_backed_record_carries_only_what_was_recorded(self):
        record = EvidenceFileService.file_backed_record(
            project=self.project,
            uploaded_file=_Bytes(b'bytes', 'crack.jpg', content_type='image/jpeg'),
            structural_element_id='COL-C24',
            description='Spalling on the north face',
        )

        self.assertEqual(record.source_type, 'uploaded_file')
        self.assertEqual(record.structural_element_id, 'COL-C24')
        # Blank source identity: the uniqueness constraint is conditional on
        # source_model, so filling it in would block a second upload.
        self.assertEqual(record.source_model, '')
        self.assertEqual(record.source_id, '')
        self.assertEqual(record.payload['file_name'], 'crack.jpg')
        self.assertEqual(record.payload['declared_content_type'], 'image/jpeg')
        self.assertEqual(record.payload['description'], 'Spalling on the north face')
        # Not recorded, so absent — not present as a placeholder.
        self.assertNotIn('coordinates', record.payload)
        self.assertIsNone(record.coordinates)
        self.assertIsNone(record.confidence)
        # Ingested records carry their hash from the start.
        self.assertEqual(record.evidence_hash, record.compute_hash())


@local_storage_settings
class EvidenceFileAPITestCase(_CleanMediaMixin, APITestCase):
    """The four routes, their scoping, and the shadowing regression."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser(
            username='evidence_uploader@nexucon.com',
            email='evidence_uploader@nexucon.com',
            password='Password123!',
        )
        self.project = Project.objects.create(name='Upload API Project')
        self.other_project = Project.objects.create(name='Someone Else Project')
        self.inspection = Inspection.objects.create(
            project=self.project, inspection_reference='INS-FILE-1',
        )
        self.other_inspection = Inspection.objects.create(
            project=self.other_project, inspection_reference='INS-FILE-2',
        )
        self.client.force_authenticate(user=self.user)
        self.upload_url = reverse('evidence-upload')

    def _upload(self, *, content=b'field capture bytes', name='cover.jpg',
                project=None, **extra):
        # `str(project)` would be the project's *name* — Project.__str__ returns
        # the name, and a name is not a UUID. So take the id explicitly.
        target = self.project if project is None else project
        payload = {
            'file': SimpleUploadedFile(name, content, content_type='image/jpeg'),
            'project': str(target.id),
        }
        payload.update(extra)
        return self.client.post(self.upload_url, payload, format='multipart')

    # -- upload --------------------------------------------------------

    def test_upload_stores_a_record_and_its_bytes(self):
        response = self._upload(
            content=b'the real capture bytes',
            structural_element_id='COL-C24',
            description='Delaminated render',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        record = EvidenceRecord.objects.get(pk=response.data['id'])
        self.assertEqual(record.source_type, 'uploaded_file')
        self.assertEqual(record.ingested_by, self.user)

        self.assertEqual(response.data['file']['sha256_hash'],
                         _sha256(b'the real capture bytes'))
        self.assertEqual(response.data['file']['file_size_bytes'],
                         len(b'the real capture bytes'))
        self.assertIsNone(response.data['file']['last_verify_ok'])
        self.assertEqual(_stored_files(), [record.file.storage_name])

    def test_upload_attaches_the_file_to_the_inspection(self):
        response = self._upload(inspection=str(self.inspection.id))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(EvidenceRecord.objects.get(pk=response.data['id']).inspection,
                         self.inspection)

    def test_upload_into_a_project_outside_the_scope_is_a_404(self):
        # A district officer, not the superuser: a superuser sees every project,
        # so "out of scope" has to be tested with someone who has a scope.
        district_a = District.objects.create(name='Upload District A', code='UPA')
        district_b = District.objects.create(name='Upload District B', code='UPB')
        self.project.district = district_a
        self.project.save(update_fields=['district'])
        self.other_project.district = district_b
        self.other_project.save(update_fields=['district'])
        officer = User.objects.create_user(
            username='upload_officer@nexucon.com',
            email='upload_officer@nexucon.com',
            password='Password123!',
        )
        Profile.objects.create(user=officer, district=district_a)
        self.client.force_authenticate(user=officer)

        # The same request into their own project is accepted, so the 404 below
        # is the scope and not a route that never worked.
        self.assertEqual(self._upload().status_code, status.HTTP_201_CREATED)

        response = self._upload(project=self.other_project, name='other.jpg')

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND,
                         msg=response.data)
        self.assertFalse(EvidenceRecord.objects.filter(
            project=self.other_project).exists())

    def test_upload_naming_another_projects_inspection_is_a_404(self):
        response = self._upload(inspection=str(self.other_inspection.id))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(EvidenceRecord.objects.filter(
            project=self.project).exists())
        self.assertEqual(_stored_files(), [])

    def test_upload_with_a_matching_client_hash_is_accepted(self):
        content = b'hash-checked capture'
        response = self._upload(content=content, sha256=_sha256(content))

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['file']['sha256_hash'], _sha256(content))

    def test_upload_with_a_wrong_client_hash_leaves_nothing_behind(self):
        response = self._upload(content=b'real bytes', sha256='b' * 64)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('b' * 64, response.data['detail'])
        # The record created for the refused file is removed with it: a row
        # claiming evidence it does not hold is the defect this prevents.
        self.assertFalse(EvidenceRecord.objects.filter(
            project=self.project).exists())
        self.assertFalse(EvidenceFile.objects.exists())
        self.assertEqual(_stored_files(), [])

    def test_upload_with_a_malformed_client_hash_is_rejected_before_storage(self):
        response = self._upload(sha256='not-a-hash')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(_stored_files(), [])

    def test_upload_with_non_object_coordinates_is_rejected(self):
        response = self._upload(coordinates='[1, 2, 3]')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(EvidenceRecord.objects.exists())

    def test_upload_records_the_coordinates_the_device_reported(self):
        response = self._upload(
            coordinates='{"latitude": 6.4281, "longitude": 3.4219}')

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        record = EvidenceRecord.objects.get(pk=response.data['id'])
        self.assertEqual(record.coordinates['latitude'], 6.4281)

    def test_a_voice_note_upload_keeps_everything_the_field_app_sent(self):
        """The capture timestamp is a datetime, and a datetime is not JSON.

        Writing it straight into the payload made the whole upload a 500 —
        every field of a voice note was lost to it.
        """
        response = self._upload(
            source_type='voice_note',
            captured_at='2026-10-03T09:15:00Z',
            coordinates='{"latitude": 6.573054, "longitude": 3.265}',
            structural_element_id='S-10',
            category='structural_cracking',
            severity='MEDIUM',
            description='hello',
            transcript='how you doing',
            translations='{"en": "how you doing", "yo": "Báwo ni"}',
            duration_seconds='4',
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED,
                         msg=getattr(response, 'data', None))
        record = EvidenceRecord.objects.get(pk=response.data['id'])
        self.assertEqual(record.source_type, 'voice_note')
        self.assertEqual(response.data['source_type_display'], 'Field Voice Note')
        self.assertEqual(record.payload['transcript'], 'how you doing')
        self.assertEqual(record.payload['translations']['yo'], 'Báwo ni')
        self.assertEqual(record.payload['duration_seconds'], 4)
        self.assertEqual(record.payload['category'], 'structural_cracking')
        self.assertEqual(record.payload['severity'], 'MEDIUM')
        self.assertEqual(record.payload['description'], 'hello')
        self.assertEqual(record.payload['captured_at'], '2026-10-03T09:15:00+00:00')
        self.assertEqual(record.coordinates['latitude'], 6.573054)

    def test_the_payload_hash_survives_a_reload(self):
        """`evidence_hash` is taken at ingest and recomputed from storage.

        They only agree if what was hashed is already the JSON the database
        hands back — so a datetime normalised at serialisation time rather than
        before the write would have made every such record fail `/verify/`.
        """
        record = self._stored_record(
            captured_at='2026-10-03T09:15:00Z', transcript='how you doing')
        record.refresh_from_db()

        self.assertEqual(record.compute_hash(), record.evidence_hash)
        self.assertTrue(EvidenceFileService.verify(record)['payload_ok'])

    def test_upload_rejects_a_source_type_the_registry_cannot_name(self):
        response = self._upload(source_type='mainframe_dump')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(EvidenceRecord.objects.exists())

    def test_upload_rejects_translations_that_are_not_language_to_text(self):
        response = self._upload(translations='["yo", "ig"]')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(EvidenceRecord.objects.exists())

    def test_the_uploadable_source_types_are_registry_types(self):
        registry = {value for value, _label in EvidenceRecord.SOURCE_TYPES}
        self.assertTrue(
            set(EvidenceFileUploadSerializer.UPLOAD_SOURCE_TYPES) <= registry)

    def test_the_registry_list_carries_the_file_the_board_plays(self):
        """The evidence screen plays each voice note from the list response.

        It holds only what the list returned, so a list without the file is a
        screen that cannot play back the capture it just accepted.
        """
        record = self._stored_record(source_type='voice_note')

        response = self.client.get(reverse('evidence-record-list'))

        rows = response.data['results'] if isinstance(
            response.data, dict) else response.data
        row = next(r for r in rows if r['id'] == str(record.id))
        self.assertEqual(row['file']['file_name'], 'cover.jpg')
        self.assertIsNotNone(row['file']['file_url'])

    def test_upload_writes_an_audit_event(self):
        response = self._upload()

        event = AuditEvent.objects.filter(action='evidence.file.upload').first()
        self.assertIsNotNone(event)
        self.assertEqual(event.resource_id, str(response.data['id']))
        self.assertEqual(event.metadata['sha256_hash'],
                         _sha256(b'field capture bytes'))

    def test_upload_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self._upload()

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(_stored_files(), [])

    # -- detail, verify, by-inspection ---------------------------------

    def _stored_record(self, **extra):
        response = self._upload(**extra)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return EvidenceRecord.objects.get(pk=response.data['id'])

    def test_detail_returns_the_record_with_its_file(self):
        record = self._stored_record()

        response = self.client.get(
            reverse('evidence-detail', kwargs={'pk': record.id}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['evidence_reference'],
                         record.evidence_reference)
        self.assertEqual(response.data['file']['file_name'], 'cover.jpg')

    def test_verify_returns_all_three_answers_for_an_intact_file(self):
        record = self._stored_record()

        response = self.client.post(
            reverse('evidence-verify', kwargs={'pk': record.id}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['payload_ok'])
        self.assertTrue(response.data['file_present'])
        self.assertTrue(response.data['file_bytes_ok'])
        self.assertEqual(response.data['evidence_reference'],
                         record.evidence_reference)
        # The outcome is persisted for a caller that only reads the record.
        self.assertTrue(EvidenceFile.objects.get(record=record).last_verify_ok)

    def test_verify_reports_replaced_bytes_as_a_200_not_an_error(self):
        record = self._stored_record()
        with open(record.file.file.path, 'wb') as handle:
            handle.write(b'something else')

        response = self.client.post(
            reverse('evidence-verify', kwargs={'pk': record.id}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['file_bytes_ok'])
        self.assertTrue(response.data['payload_ok'])

    def test_verify_audits_a_failed_verification(self):
        record = self._stored_record()
        with open(record.file.file.path, 'wb') as handle:
            handle.write(b'something else')

        self.client.post(reverse('evidence-verify', kwargs={'pk': record.id}))

        event = AuditEvent.objects.filter(
            action='evidence.file.verify_failed').first()
        self.assertIsNotNone(event)
        self.assertEqual(event.severity, 'High')
        self.assertEqual(event.resource_id, str(record.id))

    def test_verify_does_not_audit_a_pass(self):
        record = self._stored_record()

        self.client.post(reverse('evidence-verify', kwargs={'pk': record.id}))

        self.assertFalse(AuditEvent.objects.filter(
            action='evidence.file.verify_failed').exists())

    def test_another_projects_record_is_a_404_on_every_route(self):
        outsider = User.objects.create_user(
            username='other_inspector@nexucon.com',
            email='other_inspector@nexucon.com',
            password='Password123!',
        )
        record = self._stored_record()
        self.client.force_authenticate(user=outsider)

        for name in ('evidence-detail', 'evidence-verify'):
            url = reverse(name, kwargs={'pk': record.id})
            response = (self.client.post(url) if name == 'evidence-verify'
                        else self.client.get(url))
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND,
                             f'{name} leaked another project\'s evidence')

    def test_by_inspection_returns_only_that_visits_evidence(self):
        mine = self._stored_record(inspection=str(self.inspection.id))
        self._stored_record()  # same project, no inspection

        response = self.client.get(reverse(
            'evidence-by-inspection', kwargs={'inspection_id': self.inspection.id}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([row['id'] for row in response.data], [str(mine.id)])

    def test_by_inspection_can_filter_to_one_source_type(self):
        self._stored_record(inspection=str(self.inspection.id))

        response = self.client.get(reverse(
            'evidence-by-inspection', kwargs={'inspection_id': self.inspection.id}),
            {'source_type': 'gpr'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

        response = self.client.get(reverse(
            'evidence-by-inspection', kwargs={'inspection_id': self.inspection.id}),
            {'source_type': 'uploaded_file'})
        self.assertEqual(len(response.data), 1)

    def test_by_inspection_serves_each_records_file_digest(self):
        """The visit's evidence panel needs each artifact's own SHA-256.

        That digest lives on ``EvidenceFile``, not on the record, and it is what
        the submission seals. If this endpoint ever drops back to the light
        registry serializer the field disappears and the panel can no longer say
        what it is attesting — which is the false-attestation shape, arrived at
        by omission rather than by fabrication.
        """
        record = self._stored_record(inspection=str(self.inspection.id))

        response = self.client.get(reverse(
            'evidence-by-inspection', kwargs={'inspection_id': self.inspection.id}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        row = response.data[0]
        self.assertEqual(row['id'], str(record.id))
        self.assertIsNotNone(row['file'])
        self.assertEqual(row['file']['file_name'], 'cover.jpg')
        self.assertEqual(row['file']['sha256_hash'],
                         EvidenceFile.objects.get(record=record).sha256_hash)

    def test_by_inspection_serves_a_record_that_has_no_file(self):
        """A record with no bytes behind it still lists, with ``file`` null.

        Instrument rows (a GPR survey, a PUNDIT test) are evidence records with
        no uploaded file. The panel has to be able to show them beside captures
        without their file field rendering as an error.
        """
        EvidenceRecord.objects.create(
            project=self.project, inspection=self.inspection,
            source_type='inspection', payload={'note': 'no bytes behind this one'},
        )

        response = self.client.get(reverse(
            'evidence-by-inspection', kwargs={'inspection_id': self.inspection.id}))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertIsNone(response.data[0]['file'])

    def test_by_inspection_rejects_an_unknown_source_type(self):
        response = self.client.get(reverse(
            'evidence-by-inspection', kwargs={'inspection_id': self.inspection.id}),
            {'source_type': 'telepathy'})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('source_type', response.data['detail'])

    def test_an_unknown_inspection_is_a_404(self):
        response = self.client.get(reverse(
            'evidence-by-inspection', kwargs={'inspection_id': uuid.uuid4()}))

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_the_files_payload_hashes_the_same_before_and_after_storing(self):
        """The upload recomputes nothing, so the hash the client holds is the
        hash of the record it can re-read."""
        record = self._stored_record(structural_element_id='COL-C24')

        self.assertEqual(record.evidence_hash, record.compute_hash())


class EvidenceRouteRegistrationTestCase(TestCase):
    """The new literal paths must not shadow the router's `/records/`.

    `scans/urls.py` and `inspections/urls.py` both carry the fixed-bug note for
    exactly this mistake, so it is asserted here rather than trusted.
    """

    def test_records_list_still_resolves(self):
        self.assertEqual(
            reverse('evidence-record-list'), '/api/v1/evidence/records/')

    def test_the_new_routes_resolve_under_their_own_names(self):
        self.assertEqual(reverse('evidence-upload'), '/api/v1/evidence/upload/')
        record_id = uuid.uuid4()
        self.assertEqual(
            reverse('evidence-verify', kwargs={'pk': record_id}),
            f'/api/v1/evidence/{record_id}/verify/')

    def test_records_detail_and_the_new_detail_are_different_routes(self):
        record_id = uuid.uuid4()
        self.assertEqual(
            reverse('evidence-record-detail', kwargs={'pk': record_id}),
            f'/api/v1/evidence/records/{record_id}/')
        self.assertEqual(
            reverse('evidence-detail', kwargs={'pk': record_id}),
            f'/api/v1/evidence/{record_id}/')
