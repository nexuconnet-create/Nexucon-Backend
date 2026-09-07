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
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
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
from apps.evidence.ingestion import EvidenceIngestionService
from apps.evidence.intelligence import ProjectIntelligenceService
from apps.evidence.models import AIAnalysisRecord, CorrelationFinding, EvidenceRecord
from apps.evidence.review import HumanReviewService, ReviewError
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
        # Deterministic confidence: complete path length + transit time -> 1.0
        self.assertEqual(record.confidence, 1.0)

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
