from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
import datetime
from apps.projects.models import Project
from apps.inspections.models import Inspection, Finding, StopWorkOrder
from apps.inspections.services import InspectionService
from apps.audit.models import AuditEvent

User = get_user_model()

class InspectionWorkflowTestCase(TestCase):
    def setUp(self):
        self.inspector_user = User.objects.create_user(
            username="inspector_mike@nexucon.com",
            email="inspector_mike@nexucon.com",
            password="Password123!",
            first_name="Mike",
            last_name="Ross"
        )
        self.officer = User.objects.create_superuser(
            username="head_officer@nexucon.com",
            email="head_officer@nexucon.com",
            password="Password123!"
        )
        self.project = Project.objects.create(
            name="Lekki Maritime Terminal",
            project_type="Commercial",
            status="ACTIVE",
            site_address="Plot 5, Lekki Free Trade Zone",
            lga="Ibeju-Lekki"
        )

    def test_create_inspection_request(self):
        insp = InspectionService.create_inspection_request(
            data={
                "project_id": self.project.id,
                "inspection_type": "Foundation Inspection",
                "priority": "High",
                "summary_notes": "Foundation rebar inspection before pouring concrete."
            },
            user=self.officer
        )
        self.assertIsNotNone(insp.inspection_reference)
        self.assertTrue(insp.inspection_reference.startswith("INS-"))
        self.assertEqual(insp.status, "REQUESTED")
        self.assertEqual(insp.project, self.project)
        self.assertGreater(len(insp.checklist_results), 0)
        self.assertTrue(AuditEvent.objects.filter(resource_id=str(insp.id), action="INSPECTION_REQUESTED").exists())

    def test_assign_and_schedule(self):
        insp = InspectionService.create_inspection_request(
            data={"project_id": self.project.id, "inspection_type": "Structural Review"},
            user=self.officer
        )
        scheduled_time = timezone.now() + datetime.timedelta(days=1)
        InspectionService.assign_and_schedule(insp, self.inspector_user, scheduled_time, self.officer)
        insp.refresh_from_db()
        self.assertEqual(insp.status, "SCHEDULED")
        self.assertEqual(insp.inspector, self.inspector_user)
        self.assertEqual(insp.inspector_name, self.inspector_user.get_full_name())

    def test_gps_checkin_and_completion(self):
        insp = InspectionService.create_inspection_request(
            data={"project_id": self.project.id, "inspection_type": "Safety Audit"},
            user=self.officer
        )
        InspectionService.assign_and_schedule(insp, self.inspector_user, timezone.now(), self.officer)
        
        # Step 1: Check in with GPS coordinates
        InspectionService.check_in(insp, lat=6.4281, lng=3.4219, actor=self.inspector_user)
        insp.refresh_from_db()
        self.assertEqual(insp.status, "IN_PROGRESS")
        self.assertTrue(insp.gps_verified)
        self.assertEqual(insp.gps_latitude, 6.4281)

        # Step 2: Complete inspection with PASSED outcome
        checklist = [{"id": "chk_1", "item": "Scaffolding secure", "status": "PASSED"}]
        InspectionService.complete_inspection(insp, outcome="PASSED", checklist_results=checklist, summary_notes="All clear", actor=self.inspector_user)
        insp.refresh_from_db()
        self.assertEqual(insp.status, "COMPLETED")
        self.assertEqual(insp.outcome, "PASSED")

    def test_log_finding_and_reinspection(self):
        insp = InspectionService.create_inspection_request(
            data={"project_id": self.project.id, "inspection_type": "Structural Review"},
            user=self.officer
        )
        finding = InspectionService.log_finding(
            inspection=insp,
            data={
                "title": "Beam Honeycombing",
                "description": "Severe void in concrete column B3.",
                "severity": "HIGH",
                "corrective_action_required": "Chipping and epoxy pressure grouting required.",
                "requires_reinspection": True
            },
            actor=self.inspector_user
        )
        self.assertEqual(finding.inspection, insp)
        self.assertEqual(finding.severity, "HIGH")

        # Auto-create re-inspection
        reinspection = InspectionService.create_reinspection(
            original_inspection=insp,
            scheduled_date=timezone.now() + datetime.timedelta(days=7),
            actor=self.officer
        )
        self.assertEqual(reinspection.inspection_type, "Re-Inspection")
        self.assertEqual(reinspection.parent_inspection, insp)

    def test_issue_stop_work_and_lift(self):
        swo = InspectionService.issue_stop_work(
            project=self.project,
            reason="Unauthorized structural modifications exceeding approved permit.",
            severity="CRITICAL",
            actor=self.officer
        )
        self.assertEqual(swo.status, "ACTIVE")
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "SUSPENDED")

        # Lift the SWO
        InspectionService.lift_stop_work(
            swo=swo,
            justification="Site engineer presented revised calculations and rectifications were inspected.",
            actor=self.officer
        )
        swo.refresh_from_db()
        self.assertEqual(swo.status, "LIFTED")
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "ACTIVE")
