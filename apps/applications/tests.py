from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from apps.projects.models import Project
from apps.applications.models import Application
from apps.applications.services import ApplicationService
from apps.permits.models import Permit
from apps.audit.models import AuditEvent

User = get_user_model()

class ApplicationWorkflowTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="test_applicant@nexucon.com",
            email="test_applicant@nexucon.com",
            password="Password123!",
            first_name="John",
            last_name="Applicant"
        )
        self.reviewer = User.objects.create_user(
            username="reviewer@nexucon.com",
            email="reviewer@nexucon.com",
            password="Password123!",
            first_name="Jane",
            last_name="Reviewer"
        )
        self.director = User.objects.create_superuser(
            username="director@nexucon.com",
            email="director@nexucon.com",
            password="Password123!"
        )
        self.project = Project.objects.create(
            name="Eko Luxury Tower",
            project_type="Commercial",
            status="PLANNING",
            site_address="Plot 10, Victoria Island",
            lga="Eti-Osa"
        )

    def test_create_application(self):
        app = ApplicationService.create_application(
            data={
                "project_id": self.project.id,
                "title": "Main Structural Permit",
                "application_type": "Building Permit",
                "priority": "High"
            },
            user=self.user
        )
        self.assertIsNotNone(app.application_reference)
        self.assertTrue(app.application_reference.startswith("APP-"))
        self.assertEqual(app.status, "SUBMITTED")
        self.assertEqual(app.project, self.project)
        self.assertEqual(app.applicant, self.user)
        # Verify Audit Event
        self.assertTrue(AuditEvent.objects.filter(resource_id=str(app.id), action="APPLICATION_CREATED").exists())

    def test_assign_reviewer(self):
        app = ApplicationService.create_application(
            data={"project_id": self.project.id, "title": "Reviewer Test"},
            user=self.user
        )
        ApplicationService.assign_reviewer(app, self.reviewer, self.director)
        app.refresh_from_db()
        self.assertEqual(app.assigned_reviewer, self.reviewer)
        self.assertEqual(app.status, "UNDER_REVIEW")

    def test_full_approval_workflow_generates_permit_and_activates_project(self):
        app = ApplicationService.create_application(
            data={"project_id": self.project.id, "title": "Full Approval Test"},
            user=self.user
        )
        # Step 1: Assign to review
        ApplicationService.assign_reviewer(app, self.reviewer, self.director)
        # Step 2: Complete Review
        ApplicationService.transition_status(app, "REVIEW_COMPLETED", self.reviewer, reason="All checks passed")
        # Step 3: Request Approval
        ApplicationService.transition_status(app, "APPROVAL_REQUESTED", self.reviewer)
        # Step 4: Final Approval by Director
        ApplicationService.transition_status(app, "APPROVED", self.director, reason="Fully verified")

        app.refresh_from_db()
        self.assertEqual(app.status, "APPROVED")
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "ACTIVE")

        # Verify Permit was automatically issued
        self.assertTrue(Permit.objects.filter(application=app).exists())
        permit = Permit.objects.get(application=app)
        self.assertEqual(permit.status, "ACTIVE")
        self.assertTrue(permit.permit_number.startswith("PRM-"))

    def test_request_documents(self):
        app = ApplicationService.create_application(
            data={"project_id": self.project.id, "title": "Doc Request Test"},
            user=self.user
        )
        ApplicationService.request_additional_documents(
            app,
            document_items=["Soil Bearing Capacity Report", "Fire Safety Plan"],
            instructions="Please submit signed copies within 7 days",
            actor=self.reviewer
        )
        app.refresh_from_db()
        self.assertEqual(len(app.document_requests), 1)
        self.assertEqual(app.document_requests[0]["requested_items"], ["Soil Bearing Capacity Report", "Fire Safety Plan"])
