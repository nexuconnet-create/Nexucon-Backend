from django.test import TestCase
from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken
from apps.projects.models import Project
from apps.applications.models import Application
from apps.applications.services import ApplicationService
from apps.permits.models import Permit
from apps.stakeholders.models import Developer
from apps.government.models import District, Profile, Role
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


# ==========================================================================
# View-level coverage — ApplicationViewSet: authentication, project scoping,
# CRUD, filters, review workflow and document-request endpoints.
# ==========================================================================

APPLICATIONS_URL = "/api/v1/applications/"


class ApplicationViewTestBase(APITestCase):
    """Shared fixture: superuser director, two client developers with their
    own projects, and one application per project."""

    def setUp(self):
        super().setUp()
        self.director = User.objects.create_superuser(
            username="views_director@nexucon.com",
            email="views_director@nexucon.com",
            password="Password123!",
        )
        self.client_a = User.objects.create_user(
            username="views.app.a@alpha.dev", email="views.app.a@alpha.dev",
            password="Password123!", first_name="Alpha", last_name="Applicant",
        )
        self.client_b = User.objects.create_user(
            username="views.app.b@beta.dev", email="views.app.b@beta.dev",
            password="Password123!", first_name="Beta", last_name="Applicant",
        )
        Developer.objects.create(user=self.client_a, name="Alpha App Developments")
        Developer.objects.create(user=self.client_b, name="Beta App Developments")

        self.project_a = Project.objects.create(
            name="Alpha Application Tower", project_type="Commercial", status="PLANNING",
            developer_organization="Alpha App Developments", lga="Eti-Osa")
        self.project_b = Project.objects.create(
            name="Beta Application Estate", project_type="Residential", status="PLANNING",
            developer_organization="Beta App Developments", lga="Ibeju-Lekki")

        self.app_a = Application.objects.create(
            project=self.project_a, applicant=self.client_a,
            title="Alpha Building Permit", application_type="Building Permit",
            status="SUBMITTED", priority="High")
        self.app_b = Application.objects.create(
            project=self.project_b, applicant=self.client_b,
            title="Beta Renovation Permit", application_type="Renovation Permit",
            status="SUBMITTED", priority="Normal")

    def auth(self, user):
        refresh = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")


class ApplicationAuthScopingTestCase(ApplicationViewTestBase):
    """401 for anonymous users and strict per-project scoping for clients."""

    def test_anonymous_requests_rejected(self):
        urls = [APPLICATIONS_URL, APPLICATIONS_URL + "stats/",
                APPLICATIONS_URL + "review-queue/",
                APPLICATIONS_URL + f"{self.app_a.id}/"]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED,
                             msg=f"GET {url} must require authentication")
        self.assertEqual(self.client.post(APPLICATIONS_URL, {}, format="json").status_code,
                         status.HTTP_401_UNAUTHORIZED)

    def test_client_lists_only_own_projects_applications(self):
        self.auth(self.client_a)
        response = self.client.get(APPLICATIONS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        refs = [item["application_reference"] for item in response.data]
        self.assertIn(self.app_a.application_reference, refs)
        self.assertNotIn(self.app_b.application_reference, refs)

    def test_client_cannot_read_or_mutate_other_projects_application(self):
        self.auth(self.client_a)
        url_b = APPLICATIONS_URL + f"{self.app_b.id}/"
        self.assertEqual(self.client.get(url_b).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            self.client.patch(url_b, {"title": "Hijacked"}, format="json").status_code,
            status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(url_b).status_code, status.HTTP_404_NOT_FOUND)

    def test_client_actions_on_other_projects_application_return_404(self):
        self.auth(self.client_a)
        base_b = APPLICATIONS_URL + f"{self.app_b.id}/"
        for action_url, payload in [
            ("transition/", {"status": "UNDER_REVIEW"}),
            ("assign-reviewer/", {"reviewer_name": "Ghost Reviewer"}),
            ("request-docs/", {"document_items": ["Site Plan"]}),
            ("update-doc-request/", {"request_id": "REQ-001"}),
            ("update-review-item/", {"item_id": "doc_arch", "status": "PASSED"}),
        ]:
            response = self.client.post(base_b + action_url, payload, format="json")
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND,
                             msg=f"{action_url} must be out of scope for another client")
        # Nothing changed on the other tenant's application.
        self.app_b.refresh_from_db()
        self.assertEqual(self.app_b.status, "SUBMITTED")
        self.assertEqual(self.app_b.assigned_reviewer_name, None)

    def test_client_can_update_own_application(self):
        self.auth(self.client_a)
        response = self.client.patch(APPLICATIONS_URL + f"{self.app_a.id}/",
                                     {"title": "Alpha permit — revised"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.app_a.refresh_from_db()
        self.assertEqual(self.app_a.title, "Alpha permit — revised")

    def test_superuser_sees_all_applications(self):
        self.auth(self.director)
        response = self.client.get(APPLICATIONS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_district_officer_scoped_to_district_applications(self):
        district = District.objects.create(name="Eti-Osa App District", code="ETI-A")
        other_district = District.objects.create(name="Ibeju App District", code="IBE-A")
        role = Role.objects.create(name="Desk Officer")
        officer = User.objects.create_user(
            username="app.officer@nexucon.com", email="app.officer@nexucon.com",
            password="Password123!", first_name="Ada", last_name="Desk")
        Profile.objects.create(user=officer, role=role, district=district)
        self.project_a.district = district
        self.project_a.save()
        self.project_b.district = other_district
        self.project_b.save()

        self.auth(officer)
        response = self.client.get(APPLICATIONS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        refs = [item["application_reference"] for item in response.data]
        self.assertIn(self.app_a.application_reference, refs)
        self.assertNotIn(self.app_b.application_reference, refs)
        self.assertEqual(
            self.client.get(APPLICATIONS_URL + f"{self.app_b.id}/").status_code,
            status.HTTP_404_NOT_FOUND)


class ApplicationCreateAPITestCase(ApplicationViewTestBase):
    """POST /applications/ — real rows, validation errors, scope enforcement."""

    def test_create_application_with_valid_data(self):
        self.auth(self.director)
        response = self.client.post(APPLICATIONS_URL, {
            "project": str(self.project_a.id),
            "title": "Main Structural Permit",
            "application_type": "Structural Approval",
            "priority": "Critical",
            "jurisdiction": "Lagos Mainland",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["success"])
        application = Application.objects.get(id=response.data["data"]["id"])
        self.assertEqual(application.project, self.project_a)
        self.assertEqual(application.applicant, self.director)
        self.assertEqual(application.status, "SUBMITTED")
        self.assertTrue(application.application_reference.startswith("APP-"))
        self.assertEqual(len(application.review_items), 4)  # real default checklist
        self.assertTrue(AuditEvent.objects.filter(
            resource_id=str(application.id), action="APPLICATION_CREATED").exists())

    def test_create_defaults_title_and_type(self):
        self.auth(self.director)
        response = self.client.post(APPLICATIONS_URL, {
            "project": str(self.project_a.id)}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        application = Application.objects.get(id=response.data["data"]["id"])
        self.assertEqual(application.title, "Permit Application")
        self.assertEqual(application.application_type, "Building Permit")

    def test_create_without_project_returns_400(self):
        self.auth(self.director)
        response = self.client.post(APPLICATIONS_URL, {"title": "No project"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project", response.data["errors"])

    def test_create_with_unknown_project_returns_400(self):
        self.auth(self.director)
        response = self.client.post(APPLICATIONS_URL, {
            "project": "11111111-1111-1111-1111-111111111111"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # No row was silently attached to an arbitrary project.
        self.assertEqual(Application.objects.count(), 2)

    def test_create_with_invalid_fields_returns_400(self):
        self.auth(self.director)
        for payload in (
            {"project": str(self.project_a.id), "priority": "Ultra"},
            {"project": str(self.project_a.id), "review_deadline": "not-a-date"},
            {"project": "garbage"},
        ):
            response = self.client.post(APPLICATIONS_URL, payload, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST,
                             msg=f"{payload} should fail validation")

    def test_client_cannot_file_application_for_another_projects(self):
        self.auth(self.client_a)
        response = self.client.post(APPLICATIONS_URL, {
            "project": str(self.project_b.id),
            "title": "Overreaching application",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Application.objects.filter(project=self.project_b).count(), 1)


class ApplicationFilterSearchTestCase(ApplicationViewTestBase):
    """Query params on the application list endpoint (as superuser)."""

    def setUp(self):
        super().setUp()
        self.auth(self.director)
        self.app_review = Application.objects.create(
            project=self.project_a, applicant=self.client_a,
            title="Under Review App", status="UNDER_REVIEW")
        self.app_review_done = Application.objects.create(
            project=self.project_b, applicant=self.client_b,
            title="Review Completed App", status="REVIEW_COMPLETED")
        self.app_conditional = Application.objects.create(
            project=self.project_a, applicant=self.client_a,
            title="Conditional App", status="CONDITIONAL_APPROVAL")
        self.app_approved = Application.objects.create(
            project=self.project_b, applicant=self.client_b,
            title="Approved App", status="APPROVED")
        self.app_rejected = Application.objects.create(
            project=self.project_a, applicant=self.client_a,
            title="Rejected App", status="REJECTED")
        self.app_expired = Application.objects.create(
            project=self.project_b, applicant=self.client_b,
            title="Expired App", status="EXPIRED")

    def _refs(self, **params):
        response = self.client.get(APPLICATIONS_URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["application_reference"] for item in response.data}

    def test_status_filters(self):
        self.assertIn(self.app_a.application_reference, self._refs(status="submitted"))
        self.assertIn(self.app_review.application_reference, self._refs(status="REVIEW"))
        self.assertIn(self.app_review_done.application_reference, self._refs(status="UNDER_REVIEW"))
        self.assertIn(self.app_conditional.application_reference, self._refs(status="CONDITIONAL"))
        self.assertIn(self.app_approved.application_reference, self._refs(status="approved"))
        self.assertIn(self.app_rejected.application_reference, self._refs(status="rejected"))
        expired = self._refs(status="expired")
        self.assertIn(self.app_expired.application_reference, expired)
        self.assertNotIn(self.app_approved.application_reference, expired)

    def test_status_all_and_unknown_exact_match(self):
        self.assertEqual(len(self._refs(status="ALL")), 8)
        self.assertEqual(self._refs(status="DRAFT"), set())

    def test_project_priority_type_filters(self):
        refs = self._refs(project=str(self.project_a.id))
        self.assertEqual(refs, {a.application_reference for a in
                                Application.objects.filter(project=self.project_a)})
        self.assertEqual(self._refs(priority="high"), {self.app_a.application_reference})
        self.assertEqual(self._refs(application_type="Renovation"),
                         {self.app_b.application_reference})
        self.assertEqual(self._refs(type="Structural".lower()), set())  # icontains is case-sensitive-ish

    def test_search_by_title_reference_project_and_applicant(self):
        self.assertEqual(self._refs(search="Conditional App"), {self.app_conditional.application_reference})
        by_reference = self._refs(search=self.app_approved.application_reference[:10])
        self.assertIn(self.app_approved.application_reference, by_reference)
        by_project = self._refs(search="Alpha Application")
        self.assertIn(self.app_a.application_reference, by_project)
        self.assertNotIn(self.app_b.application_reference, by_project)
        self.assertIn(self.app_a.application_reference, self._refs(search="views.app.a@alpha.dev"))
        by_first_name = self._refs(search="Alpha")
        self.assertIn(self.app_a.application_reference, by_first_name)
        self.assertNotIn(self.app_b.application_reference, by_first_name)


class ApplicationStatsAndQueueTestCase(ApplicationViewTestBase):
    """Stats and review-queue actions, including scoping."""

    def setUp(self):
        super().setUp()
        Application.objects.create(project=self.project_a, applicant=self.client_a,
                                   title="Under Review A", status="UNDER_REVIEW")
        Application.objects.create(project=self.project_b, applicant=self.client_b,
                                   title="Approved B", status="APPROVED")
        Application.objects.create(project=self.project_b, applicant=self.client_b,
                                   title="Rejected B", status="REJECTED")

    def test_superuser_stats_count_all(self):
        self.auth(self.director)
        response = self.client.get(APPLICATIONS_URL + "stats/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual(data["total"], 5)
        self.assertEqual(data["submitted"], 2)
        self.assertEqual(data["under_review"], 1)
        self.assertEqual(data["approved"], 1)
        self.assertEqual(data["rejected"], 1)

    def test_client_stats_only_own_project(self):
        self.auth(self.client_a)
        response = self.client.get(APPLICATIONS_URL + "stats/")
        data = response.data["data"]
        self.assertEqual(data["total"], 2)
        self.assertEqual(data["submitted"], 1)
        self.assertEqual(data["under_review"], 1)
        self.assertEqual(data["approved"], 0)   # project_b approval invisible

    def test_review_queue_contains_only_active_statuses(self):
        self.auth(self.director)
        response = self.client.get(APPLICATIONS_URL + "review-queue/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        statuses = {item["status"] for item in response.data["data"]}
        self.assertEqual(statuses, {"SUBMITTED", "UNDER_REVIEW"})
        self.assertEqual(len(response.data["data"]), 3)

    def test_review_queue_scoped_to_client(self):
        self.auth(self.client_a)
        response = self.client.get(APPLICATIONS_URL + "review-queue/")
        titles = {item["title"] for item in response.data["data"]}
        self.assertEqual(titles, {"Alpha Building Permit", "Under Review A"})


class ApplicationTransitionAPITestCase(ApplicationViewTestBase):
    """The transition action: valid state machine moves, validation errors."""

    def setUp(self):
        super().setUp()
        self.auth(self.director)
        self.url = APPLICATIONS_URL + f"{self.app_a.id}/transition/"

    def test_valid_transition(self):
        response = self.client.post(self.url, {
            "status": "UNDER_REVIEW", "reason": "Assigned to structural desk"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.app_a.refresh_from_db()
        self.assertEqual(self.app_a.status, "UNDER_REVIEW")
        self.assertEqual(self.app_a.decision_reason, "Assigned to structural desk")
        self.assertIsNotNone(self.app_a.decision_date)

    def test_invalid_transition_returns_400(self):
        response = self.client.post(self.url, {"status": "APPROVED"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.app_a.refresh_from_db()
        self.assertEqual(self.app_a.status, "SUBMITTED")  # unchanged

    def test_missing_status_returns_400(self):
        response = self.client.post(self.url, {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_full_approval_flow_issues_permit_and_activates_project(self):
        for next_status in ("UNDER_REVIEW", "REVIEW_COMPLETED",
                            "APPROVAL_REQUESTED", "APPROVED"):
            response = self.client.post(self.url, {"status": next_status}, format="json")
            self.assertEqual(response.status_code, status.HTTP_200_OK,
                             msg=f"transition to {next_status} failed: {response.data}")
        self.app_a.refresh_from_db()
        self.assertEqual(self.app_a.status, "APPROVED")
        self.project_a.refresh_from_db()
        self.assertEqual(self.project_a.status, "ACTIVE")
        permit = Permit.objects.get(application=self.app_a)
        self.assertEqual(permit.status, "ACTIVE")
        self.assertEqual(permit.project, self.project_a)

    def test_conditional_approval_records_conditions_and_permit(self):
        # SUBMITTED -> UNDER_REVIEW -> CONDITIONAL_APPROVAL (state machine).
        first = self.client.post(self.url, {"status": "UNDER_REVIEW"}, format="json")
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        response = self.client.post(self.url, {
            "status": "CONDITIONAL_APPROVAL",
            "conditions": "Submit revised structural calculations within 30 days",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.app_a.refresh_from_db()
        self.assertEqual(self.app_a.status, "CONDITIONAL_APPROVAL")
        self.assertIn("structural calculations", self.app_a.conditions)
        permit = Permit.objects.get(application=self.app_a)
        self.assertIn("structural calculations", permit.conditions)

    def test_rejection_with_reason(self):
        response = self.client.post(self.url, {
            "status": "REJECTED", "reason": "Site plan deviates from approved layout"},
            format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.app_a.refresh_from_db()
        self.assertEqual(self.app_a.status, "REJECTED")
        self.assertEqual(self.app_a.decision_reason, "Site plan deviates from approved layout")


class ApplicationAssignmentAndDocsAPITestCase(ApplicationViewTestBase):
    """assign-reviewer, request-docs, update-doc-request, update-review-item."""

    def setUp(self):
        super().setUp()
        self.auth(self.director)
        self.reviewer = User.objects.create_user(
            username="api.reviewer@nexucon.com", email="api.reviewer@nexucon.com",
            password="Password123!", first_name="Rita", last_name="Reviewer")
        self.url = APPLICATIONS_URL + f"{self.app_a.id}/"

    def test_assign_reviewer_by_id(self):
        response = self.client.post(self.url + "assign-reviewer/", {
            "reviewer_id": str(self.reviewer.id),
            "review_deadline": "2026-10-31",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.app_a.refresh_from_db()
        self.assertEqual(self.app_a.assigned_reviewer, self.reviewer)
        self.assertEqual(self.app_a.assigned_reviewer_name, "Rita Reviewer")
        self.assertEqual(str(self.app_a.review_deadline), "2026-10-31")
        self.assertEqual(self.app_a.status, "UNDER_REVIEW")  # auto-promoted from SUBMITTED

    def test_assign_reviewer_by_name_only(self):
        response = self.client.post(self.url + "assign-reviewer/", {
            "reviewer_name": "External Consultant Chidi"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.app_a.refresh_from_db()
        self.assertEqual(self.app_a.assigned_reviewer_name, "External Consultant Chidi")
        self.assertEqual(self.app_a.status, "UNDER_REVIEW")

    def test_assign_reviewer_without_input_assigns_self(self):
        response = self.client.post(self.url + "assign-reviewer/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.app_a.refresh_from_db()
        self.assertEqual(self.app_a.assigned_reviewer, self.director)
        self.assertEqual(self.app_a.status, "UNDER_REVIEW")

    def test_assign_reviewer_with_invalid_reviewer_id_does_not_crash(self):
        response = self.client.post(self.url + "assign-reviewer/", {
            "reviewer_id": "not-a-uuid"}, format="json")
        self.assertIn(response.status_code,
                      (status.HTTP_200_OK, status.HTTP_400_BAD_REQUEST))
        # A crash would surface as 500 — assert it never does.

    def test_request_docs_requires_items(self):
        response = self.client.post(self.url + "request-docs/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.client.post(
            self.url + "request-docs/", {"document_items": []}, format="json").status_code,
            status.HTTP_400_BAD_REQUEST)

    def test_request_docs_creates_request_entry(self):
        response = self.client.post(self.url + "request-docs/", {
            "document_items": ["Soil Test Report", "Fire Strategy"],
            "instructions": "Submit within 7 days",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.app_a.refresh_from_db()
        self.assertEqual(len(self.app_a.document_requests), 1)
        request_entry = self.app_a.document_requests[0]
        self.assertEqual(request_entry["id"], "REQ-001")
        self.assertEqual(request_entry["status"], "PENDING_SUBMISSION")
        self.assertEqual(request_entry["requested_items"], ["Soil Test Report", "Fire Strategy"])
        self.assertIn("Soil Test Report", self.app_a.required_action)

    def test_update_doc_request_progress(self):
        self.client.post(self.url + "request-docs/", {
            "document_items": ["Soil Test Report", "Fire Strategy"]}, format="json")
        response = self.client.post(self.url + "update-doc-request/", {
            "request_id": "REQ-001",
            "item_name": "Soil Test Report",
            "item_status": "VERIFIED",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.app_a.refresh_from_db()
        request_entry = self.app_a.document_requests[0]
        self.assertEqual(request_entry["items_progress"]["Soil Test Report"], "VERIFIED")
        self.assertEqual(request_entry["progress"], 50)
        self.assertEqual(request_entry["status"], "IN_PROGRESS")

        # Completing every item marks the request COMPLETED.
        self.client.post(self.url + "update-doc-request/", {
            "request_id": "REQ-001",
            "item_name": "Fire Strategy",
            "item_status": "VERIFIED",
        }, format="json")
        self.app_a.refresh_from_db()
        request_entry = self.app_a.document_requests[0]
        self.assertEqual(request_entry["progress"], 100)
        self.assertEqual(request_entry["status"], "COMPLETED")

    def test_update_review_item_existing_and_new(self):
        response = self.client.post(self.url + "update-review-item/", {
            "item_id": "doc_arch", "status": "PASSED", "notes": "Drawings compliant"},
            format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.app_a.refresh_from_db()
        arch = next(i for i in self.app_a.review_items if i["id"] == "doc_arch")
        self.assertEqual(arch["status"], "PASSED")
        self.assertEqual(arch["notes"], "Drawings compliant")

        # Unknown item id appends a new checklist entry.
        response = self.client.post(self.url + "update-review-item/", {
            "item_id": "custom_9", "name": "Traffic Impact Study",
            "status": "PENDING"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.app_a.refresh_from_db()
        custom = next(i for i in self.app_a.review_items if i["id"] == "custom_9")
        self.assertEqual(custom["name"], "Traffic Impact Study")
