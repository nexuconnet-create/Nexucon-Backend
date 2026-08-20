from django.test import TestCase
from django.contrib.auth import get_user_model
import datetime
from apps.projects.models import Project
from apps.applications.models import Application
from apps.permits.models import Permit
from apps.permits.services import PermitService

User = get_user_model()

class PermitWorkflowTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="director@nexucon.com",
            email="director@nexucon.com",
            password="Password123!"
        )
        self.project = Project.objects.create(
            name="Marina Port Redevelopment",
            project_type="Commercial",
            status="ACTIVE"
        )
        self.application = Application.objects.create(
            project=self.project,
            applicant=self.user,
            application_type="Building Permit",
            status="APPROVED"
        )
        self.permit = Permit.objects.create(
            project=self.project,
            application=self.application,
            issue_date=datetime.date.today() - datetime.timedelta(days=360),
            expiry_date=datetime.date.today() - datetime.timedelta(days=5),
            status="EXPIRED"
        )

    def test_renew_permit(self):
        renewed = PermitService.renew_permit(self.permit, self.user, extension_months=12, notes="Annual extension")
        self.assertEqual(renewed.status, "ACTIVE")
        self.assertGreater(renewed.expiry_date, datetime.date.today())
        self.assertEqual(renewed.renewal_count, 1)

    def test_send_expiry_notice(self):
        notice = PermitService.send_expiry_notice(self.permit, self.user)
        self.assertEqual(notice["permit_number"], self.permit.permit_number)
        self.assertIn("expiry_date", notice)
