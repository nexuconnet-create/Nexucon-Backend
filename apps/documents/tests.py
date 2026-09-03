from django.test import TestCase
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from unittest import mock
import datetime
from rest_framework import status
from rest_framework.test import APIClient, APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from apps.projects.models import Project
from apps.bim.models import BIMModel
from apps.inspections.models import Inspection
from apps.compliance.models import NonConformanceReport as ComplianceNCR
from apps.stakeholders.models import Developer
from apps.government.models import District, Profile, Role
from apps.documents.models import (
    Document, Version, Approval, DocumentReview, DocumentFolder, DocumentTemplate,
    DocumentAudit,
)
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

        self.bim_model = BIMModel.objects.create(
            project=self.project,
            name='Metro Structural Model',
            discipline='Structural',
            format='IFC4'
        )

    def test_upload_document_creates_version_and_folder(self):
        """Test uploading a document automatically generates v1.0 version and updates folder count."""
        doc = DocumentService.upload_document({
            "project_id": str(self.project.id),
            "title": "Ground Floor Plan - Final",
            "document_type": "SUBMITTED_DRAWING",
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
        """Test pushing a new version marks older versions as superseded without deleting history."""
        doc = DocumentService.upload_document({"project_id": str(self.project.id), "title": "Structural Calcs"}, self.user)
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
        doc = DocumentService.upload_document({"project_id": str(self.project.id), "title": "Master Schedule Phase 2"}, self.user)
        approval = DocumentService.apply_digital_signature_stamp(doc, self.user, "Officially verified.")

        self.assertIsNotNone(approval.id)
        self.assertEqual(approval.status, 'APPROVED')
        self.assertTrue(doc.is_digitally_stamped)
        self.assertIsNotNone(doc.signature_hash)
        self.assertEqual(doc.status, 'APPROVED')

    def test_review_and_decide(self):
        """Test formal regulatory review creates DocumentReview and Approval record."""
        doc = DocumentService.upload_document({"project_id": str(self.project.id), "title": "Fire Strategy Report"}, self.user)
        review = DocumentService.review_and_decide(doc, 'APPROVED', 'Compliant with Lagos fire code.', self.user)

        self.assertEqual(review.status, 'APPROVED')
        doc.refresh_from_db()
        self.assertEqual(doc.status, 'APPROVED')
        self.assertEqual(doc.reviews.count(), 1)

    def test_toggle_star(self):
        """Test starring and unstarring a document."""
        doc = DocumentService.upload_document({"project_id": str(self.project.id), "title": "Contract Document"}, self.user)
        self.assertFalse(doc.is_starred)
        DocumentService.toggle_star(doc)
        doc.refresh_from_db()
        self.assertTrue(doc.is_starred)

    def test_link_to_bim_model(self):
        """Test linking drawing to 3D BIM model."""
        doc = DocumentService.upload_document({"project_id": str(self.project.id), "title": "Structural Framing 2D Drawing"}, self.user)
        DocumentService.link_to_bim_model(doc, self.bim_model.id, self.user)
        doc.refresh_from_db()
        self.assertEqual(doc.linked_bim_model_id, self.bim_model.id)

    def test_document_stats_endpoint(self):
        """Test the stats endpoint returns metrics."""
        DocumentService.upload_document({"project_id": str(self.project.id), "title": "Site Inspection Report", "document_type": "INSPECTION_REPORT"}, self.user)
        res = self.client.get('/api/v1/documents/stats/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('total_documents', res.data)
        self.assertIn('drawings_count', res.data)
        self.assertIn('inspection_reports_count', res.data)


# ==========================================================================
# View-level coverage — Document / Version / Approval / Review / Folder /
# Template / Stats viewsets: authentication, project scoping, CRUD,
# filters and workflow actions.
# ==========================================================================

DOCS_URL = "/api/v1/documents/documents/"
VERSIONS_URL = "/api/v1/documents/versions/"
APPROVALS_URL = "/api/v1/documents/approvals/"
REVIEWS_URL = "/api/v1/documents/reviews/"
TEMPLATES_URL = "/api/v1/documents/templates/"
FOLDERS_URL = "/api/v1/documents/folders/"
STATS_URL = "/api/v1/documents/stats/"

R2_UPLOAD_META = {
    "file_url": "https://r2.example.com/nexucondocument/projects/mock/file.pdf",
    "file_size": "1.2 KB",
    "file_format": "PDF",
    "signature_hash": "0xdeadbeefdeadbeefdeadbeef",
    "key": "projects/mock/file.pdf",
}


class DocumentViewTestBase(APITestCase):
    """Shared fixture: a superuser officer, two client developers with their
    own projects, and one document per project (real ORM rows)."""

    def setUp(self):
        super().setUp()
        self.officer = User.objects.create_superuser(
            username="views_doc_officer@nexucon.com",
            email="views_doc_officer@nexucon.com",
            password="Password123!",
        )
        self.client_a = User.objects.create_user(
            username="views.doc.a@alpha.dev", email="views.doc.a@alpha.dev",
            password="Password123!", first_name="Alpha", last_name="Uploader",
        )
        self.client_b = User.objects.create_user(
            username="views.doc.b@beta.dev", email="views.doc.b@beta.dev",
            password="Password123!", first_name="Beta", last_name="Uploader",
        )
        Developer.objects.create(user=self.client_a, name="Alpha Doc Developments")
        Developer.objects.create(user=self.client_b, name="Beta Doc Developments")

        self.project_a = Project.objects.create(
            name="Alpha Documents Tower", project_type="Commercial", status="ACTIVE",
            developer_organization="Alpha Doc Developments", lga="Eti-Osa")
        self.project_b = Project.objects.create(
            name="Beta Documents Estate", project_type="Residential", status="ACTIVE",
            developer_organization="Beta Doc Developments", lga="Ibeju-Lekki")

        self.doc_a = DocumentService.upload_document(
            {"project_id": str(self.project_a.id), "title": "Alpha Structural Drawings",
             "document_type": "SUBMITTED_DRAWING", "folder": "02_Structural",
             "discipline": "Structural"}, self.client_a)
        self.doc_b = DocumentService.upload_document(
            {"project_id": str(self.project_b.id), "title": "Beta Site Photos",
             "document_type": "SITE_PHOTO", "folder": "01_Architectural",
             "discipline": "Architecture"}, self.client_b)

    def auth(self, user):
        refresh = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")


class DocumentAuthScopingTestCase(DocumentViewTestBase):
    """401 for anonymous users and strict per-project scoping for clients."""

    def test_anonymous_requests_rejected(self):
        urls = [DOCS_URL, DOCS_URL + f"{self.doc_a.id}/", VERSIONS_URL,
                APPROVALS_URL, REVIEWS_URL, TEMPLATES_URL, FOLDERS_URL, STATS_URL]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED,
                             msg=f"GET {url} must require authentication")
        self.assertEqual(self.client.post(DOCS_URL, {}, format="json").status_code,
                         status.HTTP_401_UNAUTHORIZED)

    def test_client_lists_only_own_projects_documents(self):
        self.auth(self.client_a)
        response = self.client.get(DOCS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        refs = [item["document_reference"] for item in response.data]
        self.assertIn(self.doc_a.document_reference, refs)
        self.assertNotIn(self.doc_b.document_reference, refs)

    def test_client_cannot_read_or_mutate_other_projects_document(self):
        self.auth(self.client_a)
        url_b = DOCS_URL + f"{self.doc_b.id}/"
        self.assertEqual(self.client.get(url_b).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            self.client.patch(url_b, {"title": "Hijacked"}, format="json").status_code,
            status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(url_b).status_code, status.HTTP_404_NOT_FOUND)
        self.assertTrue(Document.objects.filter(id=self.doc_b.id).exists())  # untouched

    def test_client_actions_on_other_projects_document_return_404(self):
        self.auth(self.client_a)
        base_b = DOCS_URL + f"{self.doc_b.id}/"
        for action_url, payload in [
            ("star/", {}),
            ("stamp/", {}),
            ("review/", {"status": "APPROVED"}),
            ("create-version/", {"version_label": "v2.0"}),
            ("link-bim/", {"bim_model_id": "11111111-1111-1111-1111-111111111111"}),
            ("link-inspection/", {"inspection_id": "11111111-1111-1111-1111-111111111111"}),
            ("link-compliance/", {"compliance_case_id": "11111111-1111-1111-1111-111111111111"}),
        ]:
            response = self.client.post(base_b + action_url, payload, format="json")
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND,
                             msg=f"{action_url} must be out of scope for another client")
        self.assertEqual(self.client.get(base_b + "download/").status_code,
                         status.HTTP_404_NOT_FOUND)

    def test_client_can_update_own_document(self):
        self.auth(self.client_a)
        response = self.client.patch(DOCS_URL + f"{self.doc_a.id}/",
                                     {"title": "Alpha drawings rev C"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.doc_a.refresh_from_db()
        self.assertEqual(self.doc_a.title, "Alpha drawings rev C")

    def test_superuser_sees_all_projects(self):
        self.auth(self.officer)
        response = self.client.get(DOCS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)


class DocumentCreateAPITestCase(DocumentViewTestBase):
    """POST /documents/ — real rows, validation errors, scope enforcement."""

    def test_create_document_with_json_payload(self):
        self.auth(self.officer)
        response = self.client.post(DOCS_URL, {
            "project_id": str(self.project_a.id),
            "title": "Fire Strategy Report",
            "document_type": "TECHNICAL_REPORT",
            "folder": "03_MEP_Systems",
            "discipline": "MEP",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        doc = Document.objects.get(id=response.data["id"])
        self.assertEqual(doc.project, self.project_a)
        self.assertEqual(doc.uploader, self.officer)
        self.assertEqual(doc.versions.count(), 1)          # baseline v1.0 version
        self.assertEqual(doc.versions.first().version_label, "v1.0")
        folder = DocumentFolder.objects.get(name="03_MEP_Systems", project=self.project_a)
        self.assertEqual(folder.files_count, 1)

    def test_create_document_with_file_upload(self):
        # Mock the external R2 upload only — everything else is a real row.
        self.auth(self.officer)
        upload = SimpleUploadedFile("site-plan.pdf", b"%PDF-real-bytes", content_type="application/pdf")
        with mock.patch(
                "apps.documents.services.DocumentStorageService.upload_file_to_r2",
                return_value=dict(R2_UPLOAD_META)) as r2_mock:
            response = self.client.post(DOCS_URL, {
                "project_id": str(self.project_a.id),
                "title": "Uploaded site plan",
                "document_type": "SUBMITTED_DRAWING",
                "file": upload,
            }, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        r2_mock.assert_called_once()
        doc = Document.objects.get(id=response.data["id"])
        self.assertEqual(doc.file_url, R2_UPLOAD_META["file_url"])
        self.assertEqual(doc.file_size, "1.2 KB")
        self.assertEqual(doc.signature_hash, R2_UPLOAD_META["signature_hash"])

    def test_create_without_project_returns_400(self):
        self.auth(self.officer)
        response = self.client.post(DOCS_URL, {"title": "No project"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Document.objects.count(), 2)  # nothing fabricated

    def test_create_with_invalid_project_returns_400(self):
        self.auth(self.officer)
        response = self.client.post(DOCS_URL, {
            "project_id": "not-a-uuid", "title": "Bad project"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.post(DOCS_URL, {
            "project_id": "11111111-1111-1111-1111-111111111111",
            "title": "Unknown project"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Document.objects.count(), 2)

    def test_client_cannot_upload_into_another_projects(self):
        self.auth(self.client_a)
        response = self.client.post(DOCS_URL, {
            "project_id": str(self.project_b.id), "title": "Overreach doc"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Document.objects.filter(project=self.project_b).count(), 1)


class DocumentFilterSearchTestCase(DocumentViewTestBase):
    """Query params on the document list endpoint (as superuser)."""

    def setUp(self):
        super().setUp()
        self.auth(self.officer)
        self.doc_report = DocumentService.upload_document(
            {"project_id": str(self.project_a.id), "title": "Geotechnical Report",
             "document_type": "TECHNICAL_REPORT", "discipline": "Environmental",
             "status": "PENDING_REVIEW"}, self.officer)
        self.doc_compliance = DocumentService.upload_document(
            {"project_id": str(self.project_b.id), "title": "EPA Clearance Certificate",
             "document_type": "COMPLIANCE_DOCUMENT"}, self.officer)
        self.doc_inspection = DocumentService.upload_document(
            {"project_id": str(self.project_a.id), "title": "QA Inspection Report",
             "document_type": "INSPECTION_REPORT", "status": "UNDER_REVIEW"}, self.officer)
        self.doc_inspection.is_starred = True
        self.doc_inspection.save()

    def _refs(self, **params):
        response = self.client.get(DOCS_URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["document_reference"] for item in response.data}

    def test_type_filter_aliases(self):
        drawings = self._refs(type="drawing")
        self.assertIn(self.doc_a.document_reference, drawings)      # SUBMITTED_DRAWING
        self.assertNotIn(self.doc_report.document_reference, drawings)
        reports = self._refs(type="REPORT")
        self.assertIn(self.doc_report.document_reference, reports)  # TECHNICAL_REPORT
        compliance = self._refs(type="compliance")
        self.assertIn(self.doc_compliance.document_reference, compliance)
        inspection = self._refs(type="inspection_report")
        self.assertEqual(inspection, {self.doc_inspection.document_reference})

    def test_folder_discipline_status_and_starred_filters(self):
        self.assertEqual(self._refs(folder="02_Structural"), {self.doc_a.document_reference})
        self.assertEqual(self._refs(discipline="Environmental"),
                         {self.doc_report.document_reference})
        self.assertEqual(self._refs(status="pending_review"),
                         {self.doc_report.document_reference})
        self.assertEqual(self._refs(starred="true"),
                         {self.doc_inspection.document_reference})
        starred_false = self._refs(starred="false")
        self.assertIn(self.doc_a.document_reference, starred_false)
        self.assertNotIn(self.doc_inspection.document_reference, starred_false)

    def test_project_filter_and_undefined_params_ignored(self):
        refs = self._refs(project=str(self.project_a.id))
        self.assertEqual(refs, {d.document_reference for d in
                                Document.objects.filter(project=self.project_a)})
        # Frontend placeholder values must not crash or over-filter.
        self.assertEqual(len(self._refs(project="undefined")), 5)
        self.assertEqual(len(self._refs(folder="undefined", status="all", type="ALL")), 5)

    def test_bim_model_and_inspection_filters(self):
        bim_model = BIMModel.objects.create(
            project=self.project_a, name="Alpha IFC Model",
            discipline="Structural", format="IFC4")
        inspection = Inspection.objects.create(
            project=self.project_a, inspection_type="Safety Audit")
        self.doc_a.linked_bim_model = bim_model
        self.doc_a.save()
        self.doc_report.linked_inspection = inspection
        self.doc_report.save()
        self.assertEqual(self._refs(bim_model=str(bim_model.id)),
                         {self.doc_a.document_reference})
        self.assertEqual(self._refs(inspection=str(inspection.id)),
                         {self.doc_report.document_reference})

    def test_search_by_title_uploader_and_folder(self):
        self.assertEqual(self._refs(search="Geotechnical"),
                         {self.doc_report.document_reference})
        by_uploader = self._refs(search="Beta Uploader")
        self.assertEqual(by_uploader, {self.doc_b.document_reference})
        self.assertEqual(self._refs(search="02_Structural"),
                         {self.doc_a.document_reference})


class DocumentActionsTestCase(DocumentViewTestBase):
    """Detail workflow actions on documents."""

    def setUp(self):
        super().setUp()
        self.auth(self.officer)
        self.url = DOCS_URL + f"{self.doc_a.id}/"

    def test_star_toggles(self):
        first = self.client.post(self.url + "star/", {}, format="json")
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        self.doc_a.refresh_from_db()
        self.assertTrue(self.doc_a.is_starred)
        self.client.post(self.url + "star/", {}, format="json")
        self.doc_a.refresh_from_db()
        self.assertFalse(self.doc_a.is_starred)

    def test_stamp_creates_approval_and_marks_document(self):
        response = self.client.post(self.url + "stamp/", {
            "comments": "Statutory verification complete"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        approval = Approval.objects.get(id=response.data["id"])
        self.assertEqual(approval.status, "APPROVED")
        self.assertTrue(approval.signature_hash)
        self.doc_a.refresh_from_db()
        self.assertTrue(self.doc_a.is_digitally_stamped)
        self.assertEqual(self.doc_a.status, "APPROVED")
        self.assertIn("Statutory", approval.comments)

    def test_review_records_decision(self):
        response = self.client.post(self.url + "review/", {
            "status": "CHANGES_REQUESTED",
            "comments": "Revise beam schedules"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        review = DocumentReview.objects.get(id=response.data["id"])
        self.assertEqual(review.status, "CHANGES_REQUESTED")
        self.assertEqual(review.reviewer, self.officer)
        self.doc_a.refresh_from_db()
        self.assertEqual(self.doc_a.status, "CHANGES_REQUESTED")
        self.assertEqual(self.doc_a.reviews.count(), 1)

    def test_create_version_supersedes_previous(self):
        response = self.client.post(self.url + "create-version/", {
            "version_label": "v2.0",
            "changes_summary": "Reinforcement updated"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        version = Version.objects.get(id=response.data["id"])
        self.assertEqual(version.version_label, "v2.0")
        self.assertEqual(version.status, "Current")
        self.doc_a.refresh_from_db()
        self.assertEqual(self.doc_a.current_version, "v2.0")
        self.assertEqual(
            self.doc_a.versions.get(version_number=1).status, "Superseded")

    def test_create_version_with_file_upload(self):
        upload = SimpleUploadedFile("rev-c.pdf", b"%PDF-revision-c", content_type="application/pdf")
        with mock.patch(
                "apps.documents.services.DocumentStorageService.upload_file_to_r2",
                return_value=dict(R2_UPLOAD_META)):
            response = self.client.post(self.url + "create-version/", {
                "version_label": "v3.0",
                "changes_summary": "Revision C upload",
                "file": upload,
            }, format="multipart")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.doc_a.refresh_from_db()
        self.assertEqual(self.doc_a.file_url, R2_UPLOAD_META["file_url"])
        self.assertEqual(self.doc_a.versions.count(), 2)

    def test_download_returns_url_and_logs_audit(self):
        response = self.client.get(self.url + "download/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["download_url"], self.doc_a.file_url)
        self.assertEqual(response.data["title"], self.doc_a.title)
        self.assertTrue(
            DocumentAudit.objects.filter(document=self.doc_a, action="DOWNLOADED").exists())

    def test_link_bim_inspection_and_compliance(self):
        bim_model = BIMModel.objects.create(
            project=self.project_a, name="Actions IFC Model",
            discipline="Structural", format="IFC4")
        inspection = Inspection.objects.create(
            project=self.project_a, inspection_type="Structural Review")
        ncr = ComplianceNCR.objects.create(
            project=self.project_a, title="Actions NCR",
            description="Deviation found", severity="Major")

        response = self.client.post(self.url + "link-bim/", {
            "bim_model_id": str(bim_model.id)}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.post(self.url + "link-inspection/", {
            "inspection_id": str(inspection.id)}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.post(self.url + "link-compliance/", {
            "compliance_case_id": str(ncr.id)}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.doc_a.refresh_from_db()
        self.assertEqual(self.doc_a.linked_bim_model, bim_model)
        self.assertEqual(self.doc_a.linked_inspection, inspection)
        self.assertEqual(self.doc_a.linked_compliance_case, ncr)

    def test_drawings_and_approvals_vault_listings(self):
        DocumentService.apply_digital_signature_stamp(self.doc_a, self.officer, "Stamped")
        drawings = self.client.get(DOCS_URL + "drawings/")
        self.assertEqual(drawings.status_code, status.HTTP_200_OK)
        self.assertEqual({item["document_reference"] for item in drawings.data},
                         {self.doc_a.document_reference})
        vault = self.client.get(DOCS_URL + "approvals-vault/")
        self.assertEqual(vault.status_code, status.HTTP_200_OK)
        self.assertEqual({item["document_reference"] for item in vault.data},
                         {self.doc_a.document_reference})


class VersionViewSetTestCase(DocumentViewTestBase):
    """Version listing, comparison and download."""

    def setUp(self):
        super().setUp()
        self.auth(self.officer)
        self.v2 = DocumentService.create_version(self.doc_a, {
            "version_label": "v2.0", "changes_summary": "Second revision"}, self.officer)

    def test_list_versions_with_document_filter(self):
        response = self.client.get(VERSIONS_URL, {"document": str(self.doc_a.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        labels = {item["version_label"] for item in response.data}
        self.assertEqual(labels, {"v1.0", "v2.0"})
        # Invalid / placeholder document params are ignored, not a 500.
        response = self.client.get(VERSIONS_URL, {"document": "undefined"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), Version.objects.count())

    def test_compare_versions(self):
        v1 = self.doc_a.versions.get(version_number=1)
        response = self.client.post(VERSIONS_URL + "compare/", {
            "version_a": str(v1.id), "version_b": str(self.v2.id)}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["version_a"]["label"], "v1.0")
        self.assertEqual(response.data["version_b"]["label"], "v2.0")

    def test_compare_requires_both_versions(self):
        response = self.client.post(VERSIONS_URL + "compare/", {
            "version_a": str(self.v2.id)}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_compare_with_unknown_or_invalid_ids(self):
        response = self.client.post(VERSIONS_URL + "compare/", {
            "version_a": "11111111-1111-1111-1111-111111111111",
            "version_b": str(self.v2.id)}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        response = self.client.post(VERSIONS_URL + "compare/", {
            "version_a": "garbage", "version_b": str(self.v2.id)}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_version_download(self):
        response = self.client.get(VERSIONS_URL + f"{self.v2.id}/download/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["download_url"], self.v2.file_url)
        self.assertEqual(response.data["version_label"], "v2.0")

    def test_versions_are_project_scoped(self):
        self.auth(self.client_a)
        response = self.client.get(VERSIONS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        document_ids = {str(item["document"]) for item in response.data}
        self.assertIn(str(self.doc_a.id), document_ids)
        self.assertNotIn(str(self.doc_b.id), document_ids)


class ApprovalReviewFolderTemplateTestCase(DocumentViewTestBase):
    """Approval verify, review listing, folder and template CRUD, stats."""

    def setUp(self):
        super().setUp()
        self.auth(self.officer)
        self.stamp_approval = DocumentService.apply_digital_signature_stamp(
            self.doc_a, self.officer, "Stamped for record")
        self.plain_approval = Approval.objects.create(
            document=self.doc_b, category="Project Planning",
            approved_by_name="Unsigned Board", status="PENDING")
        self.review = DocumentService.review_and_decide(
            self.doc_b, "APPROVED", "Compliant", self.officer)

    def test_approval_listing_and_filters(self):
        response = self.client.get(APPROVALS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 3)  # stamp + review-created + plain
        response = self.client.get(APPROVALS_URL, {"status": "PENDING"})
        self.assertEqual({item["approval_reference"] for item in response.data},
                         {self.plain_approval.approval_reference})
        response = self.client.get(APPROVALS_URL, {"project": str(self.project_a.id)})
        self.assertEqual({item["id"] for item in response.data},
                         {str(self.stamp_approval.id)})

    def test_approval_verify(self):
        response = self.client.get(APPROVALS_URL + f"{self.stamp_approval.id}/verify/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_valid"])
        self.assertEqual(response.data["approval_reference"],
                         self.stamp_approval.approval_reference)
        response = self.client.get(APPROVALS_URL + f"{self.plain_approval.id}/verify/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_valid"])  # no signature hash
        self.assertEqual(response.data["document_title"], self.doc_b.title)

    def test_approvals_are_project_scoped(self):
        self.auth(self.client_b)
        response = self.client.get(APPROVALS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = {item["document_title"] for item in response.data}
        self.assertIn(self.doc_b.title, titles)
        self.assertNotIn(self.doc_a.title, titles)

    def test_reviews_listing_with_document_filter(self):
        response = self.client.get(REVIEWS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        response = self.client.get(REVIEWS_URL, {"document": str(self.doc_b.id)})
        self.assertEqual(len(response.data), 1)
        response = self.client.get(REVIEWS_URL, {"document": str(self.doc_a.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_reviews_are_project_scoped(self):
        self.auth(self.client_a)
        response = self.client.get(REVIEWS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])  # the only review belongs to project_b

    def test_folder_create_and_project_filter(self):
        response = self.client.post(FOLDERS_URL, {
            "name": "05_Legal", "project": str(self.project_a.id)}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        folder = DocumentFolder.objects.get(id=response.data["id"])
        self.assertEqual(folder.project, self.project_a)

        response = self.client.get(FOLDERS_URL, {"project": str(self.project_a.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {item["name"] for item in response.data}
        self.assertIn("05_Legal", names)
        self.assertIn("02_Structural", names)  # auto-created at upload
        self.assertNotIn("01_Architectural", names)  # belongs to project_b

    def test_folders_are_project_scoped(self):
        self.auth(self.client_a)
        response = self.client.get(FOLDERS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {item["name"] for item in response.data}
        self.assertEqual(names, {"02_Structural"})

    def test_template_create_and_category_filter(self):
        response = self.client.post(TEMPLATES_URL, {
            "title": "Stop-Work Order Form",
            "category": "ENFORCEMENT",
            "description": "Statutory stop-work order template",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        template = DocumentTemplate.objects.get(id=response.data["id"])
        self.assertEqual(template.category, "ENFORCEMENT")

        DocumentTemplate.objects.create(
            title="Permit Application Form", category="PERMIT",
            description="Standard permit application")
        response = self.client.get(TEMPLATES_URL, {"category": "enforcement"})
        self.assertEqual({item["title"] for item in response.data},
                         {"Stop-Work Order Form"})
        response = self.client.get(TEMPLATES_URL, {"category": "ALL"})
        self.assertEqual(len(response.data), 2)


class DocumentStatsScopingTestCase(DocumentViewTestBase):
    """The stats viewset: aggregation, expiry windows and per-user scoping."""

    def setUp(self):
        super().setUp()
        self.expired_doc = DocumentService.upload_document(
            {"project_id": str(self.project_a.id), "title": "Expired Insurance",
             "expiry_date": "2020-01-01"}, self.officer)
        self.expiring_doc = DocumentService.upload_document(
            {"project_id": str(self.project_a.id), "title": "Expiring Permit",
             "expiry_date": (timezone.now().date() + datetime.timedelta(days=10)).isoformat()},
            self.officer)

    def test_superuser_stats(self):
        self.auth(self.officer)
        response = self.client.get(STATS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertEqual(data["total_documents"], 4)
        self.assertEqual(data["drawings_count"], 1)       # doc_a SUBMITTED_DRAWING
        self.assertEqual(data["expired_count"], 1)
        self.assertEqual(data["expiring_soon_count"], 1)
        self.assertEqual(data["folders_count"],
                         DocumentFolder.objects.filter(project__in=[
                             self.project_a, self.project_b]).count())
        # overview returns the same computation.
        overview = self.client.get(STATS_URL + "overview/")
        self.assertEqual(overview.status_code, status.HTTP_200_OK)
        self.assertEqual(overview.data["total_documents"], 4)

    def test_stats_project_filter(self):
        self.auth(self.officer)
        response = self.client.get(STATS_URL, {"project": str(self.project_b.id)})
        data = response.data
        self.assertEqual(data["total_documents"], 1)
        self.assertEqual(data["expired_count"], 0)

    def test_stats_scoped_to_client(self):
        self.auth(self.client_a)
        response = self.client.get(STATS_URL)
        data = response.data
        # Only project_a documents: doc_a + expired + expiring = 3.
        self.assertEqual(data["total_documents"], 3)
        self.assertEqual(data["expired_count"], 1)
        self.assertEqual(data["drawings_count"], 1)
