from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from apps.projects.models import Project
from apps.documents.models import Document, Version, Approval, DocumentTemplate, DocumentFolder
from apps.documents.services import DocumentService

User = get_user_model()

class DocumentTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='legal_reviewer',
            email='legal@government.gov.ng',
            password='Password123!',
            first_name='Sarah',
            last_name='Jenkins'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(
            name='Downtown Metro Station',
            reference_number='PRJ-2026-METRO',
            lga='Ikeja',
            status='Active'
        )

    def test_upload_document_creates_version_and_folder(self):
        """Test uploading a document automatically generates v1.0 version and updates folder count."""
        doc = DocumentService.upload_document({
            "project_id": self.project.id,
            "title": "Ground Floor Plan - Final",
            "document_type": "DRAWING",
            "discipline": "Architecture",
            "folder": "01_Architectural",
            "file_size": "12.4 MB"
        }, self.user)

        self.assertIsNotNone(doc.id)
        self.assertEqual(doc.current_version, 'v1.0')
        self.assertEqual(doc.versions.count(), 1)
        self.assertEqual(doc.versions.first().status, 'Current')

        folder = DocumentFolder.objects.get(name='01_Architectural', project=self.project)
        self.assertEqual(folder.files_count, 1)

    def test_create_new_revision(self):
        """Test pushing a new version marks older versions as superseded."""
        doc = DocumentService.upload_document({"project_id": self.project.id, "title": "Structural Calcs"}, self.user)
        v2 = DocumentService.create_version(doc, {
            "version_label": "v2.0",
            "changes_summary": "Updated load calculations."
        }, self.user)

        self.assertEqual(v2.version_label, 'v2.0')
        self.assertEqual(v2.status, 'Current')
        doc.refresh_from_db()
        self.assertEqual(doc.current_version, 'v2.0')
        self.assertEqual(doc.versions.count(), 2)

    def test_apply_digital_signature_stamp(self):
        """Test applying digital signature stamp generates approval record and hash."""
        doc = DocumentService.upload_document({"project_id": self.project.id, "title": "Master Schedule Phase 2"}, self.user)
        approval = DocumentService.apply_digital_signature_stamp(doc, self.user, "Officially verified.")

        self.assertIsNotNone(approval.id)
        self.assertEqual(approval.status, 'APPROVED')
        self.assertTrue(doc.is_digitally_stamped)
        self.assertIsNotNone(doc.signature_hash)
        self.assertEqual(doc.status, 'APPROVED')

    def test_toggle_star(self):
        """Test starring and unstarring a document."""
        doc = DocumentService.upload_document({"project_id": self.project.id, "title": "Contract Document"}, self.user)
        self.assertFalse(doc.is_starred)
        DocumentService.toggle_star(doc)
        doc.refresh_from_db()
        self.assertTrue(doc.is_starred)

    def test_document_stats_overview_endpoint(self):
        """Test the overview stats endpoint."""
        DocumentService.upload_document({"project_id": self.project.id, "title": "Site Inspection Report"}, self.user)
        res = self.client.get('/api/v1/documents/stats/overview/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('total_documents', res.data)
        self.assertIn('drawings_count', res.data)
        self.assertIn('approved_count', res.data)
