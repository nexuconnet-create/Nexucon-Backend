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
        self.assertTrue(AuditEvent.objects.filter(resource_id=str(update.id), action="DAILY_SITE_UPDATE_LOGGED").exists())

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

    def test_construction_milestone_workflow(self):
        milestone = MonitoringService.create_milestone(
            data={
                "project_id": self.project.id,
                "name": "Substructure & Basement Pour",
                "target_date": timezone.now().date() + datetime.timedelta(days=14),
                "progress_percentage": 60
            },
            user=self.officer
        )
        self.assertEqual(milestone.status, "UPCOMING")

        # Flag delay
        MonitoringService.flag_milestone_delay(milestone, reason="Heavy rainfall delayed curing.", actor=self.officer)
        milestone.refresh_from_db()
        self.assertEqual(milestone.status, "DELAYED")
        self.assertTrue(milestone.is_delayed)

        # Verify milestone
        MonitoringService.verify_milestone(milestone, actor=self.officer)
        milestone.refresh_from_db()
        self.assertEqual(milestone.status, "VERIFIED")
        self.assertEqual(milestone.progress_percentage, 100)
        self.assertEqual(milestone.verified_by_name, self.officer.get_full_name())

    def test_site_verification_variance_calculation(self):
        # 1. Test coordinate variance calculation with matching coords (within 0.5m)
        vrf_pass = MonitoringService.record_site_verification(
            data={
                "project_id": self.project.id,
                "method": "GNSS_RTK_SURVEY",
                "captured_coordinates": {"lat": 6.428100, "lng": 3.421900},
                "approved_coordinates": {"lat": 6.428102, "lng": 3.421901},
            },
            user=self.officer
        )
        self.assertEqual(vrf_pass.status, "VERIFIED")
        self.assertFalse(vrf_pass.variance_detected)

        # 2. Test coordinate variance calculation with shifted coords (> 0.5m)
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
        self.assertGreater(vrf_fail.variance_meters, 0.5)
