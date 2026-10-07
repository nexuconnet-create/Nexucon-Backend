from unittest.mock import patch
import urllib.parse
from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from django.contrib.auth import get_user_model
from rest_framework_simplejwt.tokens import RefreshToken

from apps.reports.tests import _HermeticMediaMixin, NDTReportFixtureMixin
from apps.reports.ndt_reports import NDTReportService

User = get_user_model()


class NDTInspectorNotificationTests(_HermeticMediaMixin, NDTReportFixtureMixin, APITestCase):
    """
    Validates inspector email and in-app notification dispatch when an NDT report
    is ready for download.
    """

    def setUp(self):
        super().setUp()
        self.director = User.objects.create_superuser(
            username="director_ndt@nexucon.gov.ng",
            email="director_ndt@nexucon.gov.ng",
            password="Password123!"
        )
        self.inspector_user = User.objects.create_user(
            username="field_insp_babatunde",
            email="babatunde.fashola@lagos-inspectors.gov.ng",
            password="Password123!",
            first_name="Babatunde",
            last_name="Fashola"
        )
        self.project = self.make_project()
        self.device = self.make_device(self.project)
        self.make_test(self.project, self.device, path_length_mm=250.0, pulse_time_us=62.5)

        self.project.assigned_inspector_user = self.inspector_user
        self.project.assigned_inspector = 'Babatunde Fashola'
        self.project.save()

    def _as(self, user):
        refresh = RefreshToken.for_user(user)
        self.client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {str(refresh.access_token)}"
        )

    def test_resolve_project_inspectors(self):
        from apps.reports.notifications import resolve_project_inspectors
        inspectors = resolve_project_inspectors(self.project)
        emails = [i['email'] for i in inspectors]
        self.assertIn('babatunde.fashola@lagos-inspectors.gov.ng', emails)

    def test_inspectors_list_endpoint(self):
        self._as(self.director)
        url = reverse('project-ndt-inspectors-list', kwargs={'project_id': self.project.id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        emails = [i['email'] for i in response.data]
        self.assertIn('babatunde.fashola@lagos-inspectors.gov.ng', emails)

    @patch('apps.notifications.email_service.EmailService.send_email')
    def test_notify_inspectors_endpoint_and_token_download(self, mock_send):
        mock_send.return_value = {'success': True, 'id': 'mock-msg-uuid-1234'}

        self._as(self.director)
        # Generate & archive report first
        pdf_bytes = NDTReportService.generate_ndt_report(self.project)
        archived = NDTReportService.archive_ndt_report(self.project, self.director, pdf_bytes)

        url = reverse('archived-report-notify-inspectors', kwargs={'pk': archived.id})
        payload = {
            'recipients': ['babatunde.fashola@lagos-inspectors.gov.ng'],
            'custom_message': 'Urgent inspection review required.'
        }
        response = self.client.post(url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['success'])
        self.assertEqual(response.data['notified_count'], 1)

        # Check in-app Notification was created
        from apps.notifications.models import Notification, EmailDelivery
        notif = Notification.objects.filter(
            recipient=self.inspector_user,
            event_type='NDT_REPORT_READY'
        ).first()
        self.assertIsNotNone(notif)
        self.assertIn(archived.report_reference, notif.title)

        # Check EmailDelivery record
        delivery = EmailDelivery.objects.filter(
            recipient_email='babatunde.fashola@lagos-inspectors.gov.ng',
            template_key='ndt_report_ready'
        ).first()
        self.assertIsNotNone(delivery)
        self.assertEqual(delivery.status, 'SENT')

        # Check that pre-approved DocumentAccessRequest exists and works for download
        from apps.documents.models import DocumentAccessRequest
        access_req = DocumentAccessRequest.objects.filter(
            requester_email='babatunde.fashola@lagos-inspectors.gov.ng',
            report_digest=archived.content_key,
            status='APPROVED'
        ).first()
        self.assertIsNotNone(access_req)
        self.assertIsNotNone(access_req.access_token)

        # Now test download using this token
        dl_url = reverse('report-verify-download')
        dl_res = self.client.get(
            f"{dl_url}?ref={urllib.parse.quote(archived.report_reference)}"
            f"&digest={archived.content_key}&token={access_req.access_token}"
        )
        self.assertEqual(dl_res.status_code, status.HTTP_200_OK)
        self.assertEqual(dl_res['Content-Type'], 'application/pdf')
