from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
import datetime
from apps.projects.models import Project
from apps.monitoring.models import (
    DailySiteUpdate, FieldObservation, SiteIssue,
    ConstructionMilestone, SiteVerification
)
from apps.monitoring.services import MonitoringService
from apps.audit.models import AuditEvent

User = get_user_model()

class SiteMonitoringWorkflowTestCase(TestCase):
    def setUp(self):
        self.officer = User.objects.create_superuser(
            username="monitoring_officer@nexucon.com",
            email="monitoring_officer@nexucon.com",
            password="Password123!",
            first_name="Tunde",
            last_name="Bakare"
        )
        self.project = Project.objects.create(
            name="Eko Atlantic Towers",
            project_type="Commercial",
            status="ACTIVE",
            site_address="Plot 12, Ocean View Boulevard",
            lga="Victoria Island"
        )

    def test_log_daily_update(self):
        update = MonitoringService.log_daily_update(
            data={
                "project_id": self.project.id,
                "update_type": "DAILY_PHOTO",
                "progress_percentage": 35,
                "work_summary": "Completed 4th floor concrete pour and column rebar fixing.",
                "photos": ["https://assets.nexucon.com/photos/site_pour_1.jpg"],
                "workforce_count": 48
            },
            user=self.officer
        )
        self.assertIsNotNone(update.update_reference)
        self.assertEqual(update.progress_percentage, 35)
        self.assertTrue(AuditEvent.objects.filter(resource_id=str(update.id), action="DAILY_UPDATE_LOGGED").exists())

    def test_field_observation_lifecycle(self):
        obs = MonitoringService.create_observation(
            data={
                "project_id": self.project.id,
                "category": "SAFETY",
                "title": "Perimeter Scaffolding Catch Net Missing",
                "description": "Scaffolding level 3 lacks debris netting on south elevation.",
                "severity": "HIGH",
                "corrective_action": "Install safety debris netting before proceeding with external plastering."
            },
            user=self.officer
        )
        self.assertEqual(obs.status, "OPEN")
        self.assertEqual(obs.severity, "HIGH")

        # Resolve observation
        MonitoringService.resolve_observation(
            observation=obs,
            notes="Safety nets installed and inspected by HSE lead.",
            actor=self.officer
        )
        obs.refresh_from_db()
        self.assertEqual(obs.status, "RESOLVED")
        self.assertIsNotNone(obs.resolved_at)

    def test_site_issue_reporting_and_escalation(self):
        issue = MonitoringService.report_issue(
            data={
                "project_id": self.project.id,
                "title": "Unauthorized Drainage Connection",
                "description": "Site contractor tapping into storm drain without EPB permit.",
                "severity": "CRITICAL"
            },
            user=self.officer
        )
        self.assertEqual(issue.status, "OPEN")

        # Escalate issue
        MonitoringService.escalate_issue(issue, actor=self.officer)
        issue.refresh_from_db()
        self.assertTrue(issue.is_escalated)
        self.assertEqual(issue.status, "UNDER_REVIEW")

        # Resolve issue
        MonitoringService.resolve_issue(issue, notes="Drainage permit approved and fee paid.", evidence=[], actor=self.officer)
        issue.refresh_from_db()
        self.assertEqual(issue.status, "RESOLVED")

    def test_construction_milestone_lifecycle_and_guardrails(self):
        # 1. Create Milestone
        milestone = MonitoringService.create_milestone(
            data={
                "project_id": self.project.id,
                "name": "Substructure Raft Pour",
                "phase": "SUBSTRUCTURE",
                "milestone_code": "MS-01",
                "target_date": timezone.now().date() + datetime.timedelta(days=14),
                "duration_days": 20,
                "critical_path": True,
                "progress_percentage": 50
            },
            user=self.officer
        )
        self.assertEqual(milestone.status, "IN_PROGRESS")
        self.assertEqual(milestone.progress_percentage, 50)
        self.assertTrue(milestone.critical_path)

        # 2. Update Progress to 100% -> Guardrail: Must be PENDING_VERIFICATION, NOT VERIFIED
        MonitoringService.update_milestone_progress(
            milestone=milestone,
            data={
                "progress_percentage": 100,
                "physical_progress_notes": "Raft foundation pour completed and cube test samples cast.",
                "evidence_documents": [
                    {"name": "Concrete Cube Test 28-day.pdf", "url": "https://assets.nexucon.gov.ng/test.pdf", "category": "Laboratory Test Report"}
                ]
            },
            user=self.officer
        )
        milestone.refresh_from_db()
        self.assertEqual(milestone.progress_percentage, 100)
        self.assertEqual(milestone.status, "PENDING_VERIFICATION")
        self.assertFalse(milestone.status == "VERIFIED")

        # 3. Evaluate Gates
        gates = MonitoringService.evaluate_milestone_gates(milestone)
        self.assertTrue(gates['all_gates_passed'])

        # 4. Sign off & Verify
        verified_ms = MonitoringService.verify_milestone(
            milestone=milestone,
            data={"notes": "All concrete strength benchmarks verified."},
            actor=self.officer
        )
        self.assertEqual(verified_ms.status, "VERIFIED")
        self.assertIsNotNone(verified_ms.verified_at)
        self.assertIsNotNone(verified_ms.verification_signoff)
        self.assertIn("0xLASBCA-VERIFIED-", verified_ms.verification_signoff['signature_hash'])

        # 5. Check Audit Trail
        trail = MonitoringService.get_milestone_audit_trail(milestone.id)
        self.assertGreater(len(trail), 0)

    def test_milestone_delay_flagging(self):
        milestone = MonitoringService.create_milestone(
            data={
                "project_id": self.project.id,
                "name": "Superstructure Frame Level 10",
                "phase": "SUPERSTRUCTURE",
                "target_date": timezone.now().date() + datetime.timedelta(days=10),
                "progress_percentage": 60
            },
            user=self.officer
        )

        revised = timezone.now().date() + datetime.timedelta(days=24)
        MonitoringService.flag_milestone_delay(
            milestone=milestone,
            data={
                "reason": "Custom curved facade panels delayed at port.",
                "revised_target_date": revised
            },
            actor=self.officer
        )
        milestone.refresh_from_db()
        self.assertEqual(milestone.status, "DELAYED")
        self.assertTrue(milestone.is_delayed)
        self.assertEqual(milestone.variance_days, 14)
        self.assertEqual(milestone.risk_level, "HIGH")

    def test_site_verification_variance_calculation(self):
        # 1. Test coordinate variance calculation with matching coords (within 0.05m tolerance)
        vrf_pass = MonitoringService.record_site_verification(
            data={
                "project_id": self.project.id,
                "method": "GNSS_RTK_SURVEY",
                "captured_coordinates": {"lat": 6.4281001, "lng": 3.4219001, "elevation": 4.15},
                "approved_coordinates": {"lat": 6.4281000, "lng": 3.4219000, "elevation": 4.15},
                "tolerance_limit_meters": 0.05
            },
            user=self.officer
        )
        self.assertEqual(vrf_pass.status, "VERIFIED")
        self.assertFalse(vrf_pass.variance_detected)
        self.assertLess(vrf_pass.variance_meters, 0.05)

        # 2. Test formal certification
        certified = MonitoringService.certify_site_verification(
            verification=vrf_pass,
            data={"verified_by_name": "Surv. Olumide Balogun", "notes": "Approved"},
            actor=self.officer
        )
        self.assertEqual(certified.status, "VERIFIED")
        self.assertIsNotNone(certified.digital_cert_ref)
        self.assertIsNotNone(certified.signature_hash)

        # 3. Test coordinate variance calculation with shifted coords (> 0.05m)
        vrf_fail = MonitoringService.record_site_verification(
            data={
                "project_id": self.project.id,
                "method": "TERSU_ROVER",
                "captured_coordinates": {"lat": 6.428100, "lng": 3.421900},
                "approved_coordinates": {"lat": 6.428150, "lng": 3.421950},
            },
            user=self.officer
        )
        self.assertEqual(vrf_fail.status, "VARIANCE_DETECTED")
        self.assertTrue(vrf_fail.variance_detected)
        self.assertGreater(vrf_fail.variance_meters, 0.05)

        # 4. Test flagging encroachment
        flagged = MonitoringService.flag_site_encroachment(
            verification=vrf_fail,
            data={"reason": "Setback encroachment on North corridor"},
            actor=self.officer
        )
        self.assertEqual(flagged.status, "FLAGGED")
        self.assertTrue(flagged.encroachment_detected)
