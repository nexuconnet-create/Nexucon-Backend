from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.cache import cache
from django.urls import reverse
from django.utils import timezone
import datetime
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework_simplejwt.tokens import RefreshToken
from apps.projects.models import Project
from apps.inspections.models import (
    Inspection, Finding, StopWorkOrder, InspectionSubmission, InspectionSignoff,
    Checklist, Issue, NonConformanceReport, CorrectiveAction,
)
from apps.inspections.services import InspectionService
from apps.inspections.execution import (
    ExecutionError, InspectionExecutionService, sha256_hex,
)
from apps.inspections.geofence import (
    evaluate_geofence, effective_geofence_radius,
    STATE_OUTSIDE, STATE_UNVERIFIABLE, STATE_VERIFIED,
    REASON_ACCURACY_NOT_REPORTED, REASON_DEVICE_ACCURACY_EXCEEDS_RADIUS,
    REASON_OUTSIDE_RADIUS, REASON_PROJECT_COORDINATES_NOT_RECORDED,
    REASON_WITHIN_RADIUS,
)
from apps.stakeholders.models import Developer
from apps.government.models import District, Profile, Role
from apps.audit.models import AuditEvent
from apps.settings.models import ChecklistItem, InspectionTemplate
from common.geo import haversine_m
from common.permissions import scoped_projects

User = get_user_model()

# A real Lagos site point, and offsets from it that are unambiguous at the
# scale of a 50 m fence:
#   0.00045° of latitude  ~= 50.0 m
#   0.00090° of latitude  ~= 100.0 m
SITE_LAT, SITE_LON = 6.4281, 3.4219

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
        self.assertTrue(reinspection.inspection_type.startswith("Re-Inspection"))
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


# ==========================================================================
# Inspection Execution — tamper-evident submission + crypto sign-off (W7)
# ==========================================================================

class InspectionExecutionServiceTestCase(TestCase):
    """Week-7 execution service: GPS check-in, tamper-evident submission hash
    and cryptographic sign-off seal."""

    def setUp(self):
        self.inspector_user = User.objects.create_user(
            username="field_inspector@nexucon.com",
            email="field_inspector@nexucon.com",
            password="Password123!",
            first_name="Ada",
            last_name="Obi",
        )
        self.project = Project.objects.create(
            name="Ikoyi Tower Execution Site",
            project_type="Commercial",
            status="ACTIVE",
        )
        self.inspection = Inspection.objects.create(
            project=self.project,
            inspection_type="Safety Audit",
        )
        # A real checklist template backing the submitted results, so the
        # submission's template linkage resolves to genuine rows.
        self.template = InspectionTemplate.objects.create(
            name="Site Safety Audit", department="Structural")
        self.item_1 = ChecklistItem.objects.create(
            template=self.template, item_order=1, title="Scaffolding secure")
        self.item_2 = ChecklistItem.objects.create(
            template=self.template, item_order=2, title="PPE compliance verified")

    def _submit(self, inspection=None, **overrides):
        target = inspection or self.inspection
        # A submission now requires a recorded check-in: the geofence verdict
        # that `gps_verified` carries comes from check-in, and submit() no
        # longer fabricates one. Check the target in first, as the field client
        # does.
        if not target.checkin_time:
            InspectionExecutionService.checkin(
                target, self.inspector_user, 6.4281, 3.4219)
        kwargs = dict(
            checklist_results=[
                {"item_id": str(self.item_1.id), "title": self.item_1.title, "result": "PASS", "notes": ""},
                {"item_id": str(self.item_2.id), "title": self.item_2.title, "result": "PASS", "notes": ""},
            ],
            evidence_files=[
                {"file_name": "scaffold.jpg", "sha256": "a" * 64, "url": "https://example.com/scaffold.jpg"},
            ],
            latitude=6.4281,
            longitude=3.4219,
        )
        kwargs.update(overrides)
        return InspectionExecutionService.submit(
            target, self.inspector_user, **kwargs)

    # ------------------------------------------------------- submission seal
    def test_submit_seals_content_in_submission_hash(self):
        submission = self._submit()
        self.assertRegex(submission.submission_hash, r"^[0-9a-f]{64}$")
        # Recomputing the hash over the identical stored content matches.
        self.assertTrue(InspectionExecutionService.verify_submission(submission))
        self.assertEqual(submission.submitted_by, self.inspector_user)

    def test_submission_hash_is_deterministic_for_identical_content(self):
        # The seal is a pure SHA-256 over the canonical payload: the same
        # content always yields the same digest.
        payload = '{"checklist": [], "gps": [6.4281, 3.4219]}'
        self.assertEqual(sha256_hex(payload), sha256_hex(payload))
        self.assertNotEqual(sha256_hex(payload), sha256_hex(payload + " "))

        # Two verifications of the same stored submission agree.
        submission = self._submit()
        first = InspectionExecutionService.verify_submission(submission)
        second = InspectionExecutionService.verify_submission(submission)
        self.assertTrue(first)
        self.assertTrue(second)

    def test_submission_hash_changes_when_content_changes(self):
        submission = self._submit()
        self.assertTrue(InspectionExecutionService.verify_submission(submission))

        # Tamper with the checklist results -> seal no longer matches.
        InspectionSubmission.objects.filter(pk=submission.pk).update(
            checklist_results=[
                {"item_id": "chk_1", "title": "Scaffolding secure", "result": "FAIL", "notes": "forged"},
            ])
        tampered = InspectionSubmission.objects.get(pk=submission.pk)
        self.assertFalse(InspectionExecutionService.verify_submission(tampered))

        # Tamper with the recorded GPS position -> seal no longer matches.
        submission = self._submit(inspection=Inspection.objects.create(
            project=self.project, inspection_type="Structural Review"))
        InspectionSubmission.objects.filter(pk=submission.pk).update(gps_latitude=0.0)
        tampered = InspectionSubmission.objects.get(pk=submission.pk)
        self.assertFalse(InspectionExecutionService.verify_submission(tampered))

    def test_submit_requires_mandatory_gps(self):
        with self.assertRaises(ExecutionError):
            self._submit(latitude=None)
        with self.assertRaises(ExecutionError):
            self._submit(longitude=None)

    def test_submit_requires_evidence_checksum(self):
        # Every attached artifact must carry its real SHA-256 — no checksum,
        # no seal.
        with self.assertRaises(ExecutionError):
            self._submit(evidence_files=[
                {"file_name": "no-checksum.bin", "url": "https://example.com/no-checksum.bin"},
            ])

    # ---------------------------------------------------------- check-in
    def test_checkin_records_gps_and_starts_inspection(self):
        InspectionExecutionService.checkin(
            self.inspection, self.inspector_user, 6.4281, 3.4219,
            accuracy_m=8.0)
        self.inspection.refresh_from_db()
        self.assertEqual(self.inspection.status, "IN_PROGRESS")
        self.assertAlmostEqual(self.inspection.gps_latitude, 6.4281)
        self.assertAlmostEqual(self.inspection.gps_longitude, 3.4219)
        self.assertEqual(self.inspection.inspector, self.inspector_user)
        # `gps_verified` now means something specific: this project records no
        # site coordinates, so the check-in cannot be verified. The old
        # implementation asserted True here regardless — which is exactly the
        # bug. See GeofenceCheckinTests for the verified cases.
        self.assertFalse(self.inspection.gps_verified)
        self.assertEqual(self.inspection.geofence_state, "UNVERIFIABLE")
        self.assertEqual(self.inspection.geofence_reason,
                         "PROJECT_COORDINATES_NOT_RECORDED")
        self.assertIsNone(self.inspection.geofence_distance_m)

    def test_checkin_rejects_missing_or_out_of_range_gps(self):
        invalid = [(None, 3.4219), (6.4281, None), ("x", "y"), (91.0, 3.4219), (-91.0, 3.4219),
                   (6.4281, 181.0), (6.4281, -181.0)]
        for lat, lng in invalid:
            with self.assertRaises(ExecutionError, msg=f"checkin({lat!r}, {lng!r})"):
                InspectionExecutionService.checkin(
                    Inspection.objects.create(project=self.project, inspection_type="Safety Audit"),
                    self.inspector_user, lat, lng)

    # ----------------------------------------------------------- sign-off
    def test_signoff_seals_submission_inspector_and_declaration(self):
        submission = self._submit()
        signoff = InspectionExecutionService.sign_off(
            submission, self.inspector_user,
            signature_text="I confirm this inspection was executed at the recorded location and time.")
        self.assertRegex(signoff.signoff_hash, r"^[0-9a-f]{64}$")
        self.assertEqual(signoff.signed_by, self.inspector_user)
        self.assertEqual(signoff.signed_by_name, self.inspector_user.get_full_name())
        # The seal verifies against the identical stored content.
        self.assertTrue(InspectionExecutionService.verify_signoff(signoff))

    def test_signoff_hash_detects_tampering(self):
        submission = self._submit()
        signoff = InspectionExecutionService.sign_off(submission, self.inspector_user, "Original declaration.")
        self.assertTrue(InspectionExecutionService.verify_signoff(signoff))

        InspectionSignoff.objects.filter(pk=signoff.pk).update(
            signature_text="Forged declaration.")
        tampered = InspectionSignoff.objects.get(pk=signoff.pk)
        self.assertFalse(InspectionExecutionService.verify_signoff(tampered))

    def test_signoff_requires_authenticated_inspector(self):
        submission = self._submit()
        with self.assertRaises(ExecutionError):
            InspectionExecutionService.sign_off(submission, None)
        with self.assertRaises(ExecutionError):
            InspectionExecutionService.sign_off(submission, AnonymousUser())
        # Nothing was persisted by the refused attempts.
        self.assertFalse(InspectionSignoff.objects.exists())

    def test_signoff_twice_is_rejected(self):
        submission = self._submit()
        InspectionExecutionService.sign_off(submission, self.inspector_user, "First signature.")
        with self.assertRaises(ExecutionError):
            InspectionExecutionService.sign_off(submission, self.inspector_user, "Second signature.")
        self.assertEqual(InspectionSignoff.objects.count(), 1)


class InspectionExecutionAPITestCase(APITestCase):
    """Execution endpoints over the API: auth, flow, verification."""

    def setUp(self):
        self.officer = User.objects.create_superuser(
            username="execution_officer@nexucon.com",
            email="execution_officer@nexucon.com",
            password="Password123!",
        )
        self.project = Project.objects.create(
            name="Execution API Site", project_type="Commercial", status="ACTIVE",
            # A recorded site point, so the geofence can actually be evaluated.
            latitude=6.4281, longitude=3.4219,
        )
        self.inspection = Inspection.objects.create(
            project=self.project, inspection_type="Foundation Inspection")
        refresh = RefreshToken.for_user(self.officer)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def _url(self, name):
        return reverse(name, kwargs={"inspection_id": str(self.inspection.id)})

    def test_execution_endpoints_require_authentication(self):
        self.client.credentials()  # anonymous
        for url_name in ("inspection-execution", "inspection-checkin",
                         "inspection-checkout", "inspection-submit",
                         "inspection-signoff", "inspection-verify"):
            response = self.client.post(self._url(url_name), {}, format="json")
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED,
                             msg=f"{url_name} should require authentication")

    def test_full_execution_flow_checkin_submit_signoff_verify(self):
        # 1. GPS + device-time check-in, on site and with a usable accuracy.
        checkin = self.client.post(self._url("inspection-checkin"), {
            "latitude": 6.4281, "longitude": 3.4219,
            "gps_accuracy_m": 8.0,
            "device_time": "2026-09-01T09:30:00Z",
        }, format="json")
        self.assertEqual(checkin.status_code, status.HTTP_200_OK)
        self.assertTrue(checkin.data["gps_verified"])
        self.assertEqual(checkin.data["geofence"]["state"], "VERIFIED")
        self.assertEqual(checkin.data["geofence"]["reason"], "WITHIN_RADIUS")
        self.assertEqual(checkin.data["geofence"]["radius_source"], "platform_default")
        self.assertEqual(checkin.data["geofence"]["radius_m"], 50)
        self.assertEqual(checkin.data["geofence"]["accuracy_m"], 8.0)

        # 2. Tamper-evident submission.
        submit = self.client.post(self._url("inspection-submit"), {
            "latitude": 6.4281, "longitude": 3.4219,
            "device_time": "2026-09-01T11:45:00Z",
            "checklist_results": [
                {"item_id": "chk_1", "title": "Excavation verified", "result": "PASS", "notes": ""},
            ],
            "evidence": [
                {"file_name": "foundation.jpg", "sha256": "b" * 64,
                 "url": "https://example.com/foundation.jpg"},
            ],
        }, format="json")
        self.assertEqual(submit.status_code, status.HTTP_201_CREATED)
        submission_hash = submit.data["submission_hash"]
        self.assertRegex(submission_hash, r"^[0-9a-f]{64}$")

        # 3. Cryptographic sign-off by the authenticated inspector.
        signoff = self.client.post(self._url("inspection-signoff"), {
            "signature_text": "I confirm this inspection was executed at the recorded location and time.",
        }, format="json")
        self.assertEqual(signoff.status_code, status.HTTP_201_CREATED)
        self.assertRegex(signoff.data["signoff_hash"], r"^[0-9a-f]{64}$")

        # 4. Independent verification: both seals validate.
        verify = self.client.get(self._url("inspection-verify"))
        self.assertEqual(verify.status_code, status.HTTP_200_OK)
        self.assertTrue(verify.data["submission_valid"])
        self.assertTrue(verify.data["signoff_valid"])

        # 5. Execution state reports integrity too.
        state = self.client.get(self._url("inspection-execution"))
        self.assertEqual(state.status_code, status.HTTP_200_OK)
        self.assertTrue(state.data["submission"]["integrity_verified"])
        self.assertTrue(state.data["signoff"]["signature_verified"])

    def test_submit_without_gps_returns_400(self):
        response = self.client.post(self._url("inspection-submit"), {
            "checklist_results": [{"item_id": "chk_1", "title": "Item", "result": "PASS"}],
            "evidence": [],
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_signoff_without_submission_returns_400(self):
        response = self.client.post(self._url("inspection-signoff"), {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


# ==========================================================================
# Client-portal scoping — a client only reaches inspections on their projects
# ==========================================================================

class ClientPortalScopingTestCase(APITestCase):
    """Execution endpoints scope through common.permissions.scoped_projects:
    a client developer only reaches inspections tied to their own projects."""

    def setUp(self):
        self.client_a = User.objects.create_user(
            username="client.a@alpha.dev", email="client.a@alpha.dev", password="Password123!",
            first_name="Alpha", last_name="Client",
        )
        self.client_b = User.objects.create_user(
            username="client.b@beta.dev", email="client.b@beta.dev", password="Password123!",
            first_name="Beta", last_name="Client",
        )
        Developer.objects.create(user=self.client_a, name="Alpha Developments")
        Developer.objects.create(user=self.client_b, name="Beta Developments")

        self.project_a = Project.objects.create(
            name="Alpha Tower", project_type="Commercial", status="ACTIVE",
            developer_organization="Alpha Developments")
        self.project_b = Project.objects.create(
            name="Beta Estate", project_type="Residential", status="ACTIVE",
            developer_organization="Beta Developments")

        self.inspection_a = Inspection.objects.create(
            project=self.project_a, inspection_type="Safety Audit")
        self.inspection_b = Inspection.objects.create(
            project=self.project_b, inspection_type="Safety Audit")

    def _authenticate(self, user):
        refresh = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_scoped_projects_contains_only_own_developers_projects(self):
        scoped_a = scoped_projects(self.client_a)
        self.assertIn(self.project_a, scoped_a)
        self.assertNotIn(self.project_b, scoped_a)

        scoped_b = scoped_projects(self.client_b)
        self.assertIn(self.project_b, scoped_b)
        self.assertNotIn(self.project_a, scoped_b)

    def test_client_reads_own_inspection_execution_state(self):
        self._authenticate(self.client_a)
        response = self.client.get(reverse(
            "inspection-execution", kwargs={"inspection_id": str(self.inspection_a.id)}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["inspection"], self.inspection_a.inspection_reference)

    def test_client_cannot_read_another_clients_inspection(self):
        self._authenticate(self.client_a)
        url_b = reverse("inspection-execution", kwargs={"inspection_id": str(self.inspection_b.id)})
        self.assertEqual(self.client.get(url_b).status_code, status.HTTP_404_NOT_FOUND)

    def test_client_cannot_write_another_clients_inspection(self):
        self._authenticate(self.client_a)
        for url_name in ("inspection-checkin", "inspection-submit", "inspection-signoff"):
            response = self.client.post(
                reverse(url_name, kwargs={"inspection_id": str(self.inspection_b.id)}),
                {}, format="json")
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND,
                             msg=f"{url_name} must be out of scope for another client")
        verify = self.client.get(
            reverse("inspection-verify", kwargs={"inspection_id": str(self.inspection_b.id)}))
        self.assertEqual(verify.status_code, status.HTTP_404_NOT_FOUND)


# ==========================================================================
# assign_and_schedule — no fabricated inspectors
# ==========================================================================

class AssignInspectorNoFabricationTestCase(TestCase):
    """assign_and_schedule raises unless a real inspector (user or name) is
    supplied — it never invents an officer."""

    def setUp(self):
        self.officer = User.objects.create_superuser(
            username="assign_officer@nexucon.com",
            email="assign_officer@nexucon.com",
            password="Password123!",
        )
        self.project = Project.objects.create(
            name="Assignment Integrity Site", project_type="Commercial", status="ACTIVE")
        self.inspection = InspectionService.create_inspection_request(
            data={"project_id": self.project.id, "inspection_type": "Structural Review"},
            user=self.officer,
        )

    def test_assign_without_inspector_raises_validation_error(self):
        with self.assertRaises(DRFValidationError):
            InspectionService.assign_and_schedule(
                self.inspection, inspector_user=None, scheduled_date=None,
                actor=self.officer)

    def test_no_inspector_is_fabricated_after_refusal(self):
        with self.assertRaises(DRFValidationError):
            InspectionService.assign_and_schedule(self.inspection, None, None, self.officer)
        self.inspection.refresh_from_db()
        self.assertEqual(self.inspection.status, "REQUESTED")
        self.assertIsNone(self.inspection.inspector)
        self.assertEqual(self.inspection.inspector_name, "Unassigned")

    def test_assign_with_explicit_inspector_name_only(self):
        scheduled = timezone.now() + datetime.timedelta(days=2)
        InspectionService.assign_and_schedule(
            self.inspection, inspector_user=None, scheduled_date=scheduled,
            actor=self.officer, inspector_name="Adaeze Okafor (external consultant)")
        self.inspection.refresh_from_db()
        self.assertEqual(self.inspection.status, "SCHEDULED")
        self.assertIsNone(self.inspection.inspector)  # name-only: no user link invented
        self.assertEqual(self.inspection.inspector_name, "Adaeze Okafor (external consultant)")


# ==========================================================================
# View-level coverage — InspectionViewSet / StopWorkOrderViewSet /
# FindingViewSet / ChecklistViewSet and the QC Issue / NCR / CorrectiveAction
# viewsets: authentication, project scoping, CRUD, filters and workflows.
# ==========================================================================

INSPECTIONS_URL = "/api/v1/inspections/"
SWO_URL = "/api/v1/inspections/stop-work-orders/"
FINDINGS_URL = "/api/v1/inspections/findings/"
CHECKLISTS_URL = "/api/v1/inspections/checklists/"
ISSUES_URL = "/api/v1/inspections/issues/"
NCRS_URL = "/api/v1/inspections/ncrs/"
CORRECTIVE_ACTIONS_URL = "/api/v1/inspections/corrective-actions/"


class InspectionViewTestBase(APITestCase):
    """Shared fixture: a superuser officer, two client developers with their
    own real projects, and one inspection per project."""

    def setUp(self):
        super().setUp()
        self.officer = User.objects.create_superuser(
            username="views_officer@nexucon.com",
            email="views_officer@nexucon.com",
            password="Password123!",
        )
        self.client_a = User.objects.create_user(
            username="views.client.a@alpha.dev", email="views.client.a@alpha.dev",
            password="Password123!", first_name="Alpha", last_name="Developer",
        )
        self.client_b = User.objects.create_user(
            username="views.client.b@beta.dev", email="views.client.b@beta.dev",
            password="Password123!", first_name="Beta", last_name="Developer",
        )
        Developer.objects.create(user=self.client_a, name="Alpha View Developments")
        Developer.objects.create(user=self.client_b, name="Beta View Developments")

        self.project_a = Project.objects.create(
            name="Alpha View Tower", project_type="Commercial", status="ACTIVE",
            developer_organization="Alpha View Developments", lga="Eti-Osa")
        self.project_b = Project.objects.create(
            name="Beta View Estate", project_type="Residential", status="ACTIVE",
            developer_organization="Beta View Developments", lga="Ibeju-Lekki")

        self.inspection_a = Inspection.objects.create(
            project=self.project_a, inspection_type="Foundation Inspection",
            status="REQUESTED", priority="High")
        self.inspection_b = Inspection.objects.create(
            project=self.project_b, inspection_type="Safety Audit",
            status="REQUESTED", priority="Normal")

    def auth(self, user):
        refresh = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")


class InspectionAuthScopingTestCase(InspectionViewTestBase):
    """401 for anonymous users and strict per-project scoping for clients."""

    def test_anonymous_requests_rejected(self):
        urls = [INSPECTIONS_URL, INSPECTIONS_URL + "stats/",
                INSPECTIONS_URL + f"{self.inspection_a.id}/"]
        for url in urls:
            self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED,
                             msg=f"GET {url} must require authentication")
        self.assertEqual(self.client.post(INSPECTIONS_URL, {}, format="json").status_code,
                         status.HTTP_401_UNAUTHORIZED)

    def test_client_lists_only_own_projects_inspections(self):
        self.auth(self.client_a)
        response = self.client.get(INSPECTIONS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        refs = [item["inspection_reference"] for item in response.data]
        self.assertIn(self.inspection_a.inspection_reference, refs)
        self.assertNotIn(self.inspection_b.inspection_reference, refs)

    def test_client_cannot_read_or_mutate_other_projects_inspection(self):
        self.auth(self.client_a)
        url_b = INSPECTIONS_URL + f"{self.inspection_b.id}/"
        self.assertEqual(self.client.get(url_b).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            self.client.patch(url_b, {"priority": "Critical"}, format="json").status_code,
            status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(url_b).status_code, status.HTTP_404_NOT_FOUND)

    def test_client_actions_on_other_projects_inspection_return_404(self):
        self.auth(self.client_a)
        base_b = INSPECTIONS_URL + f"{self.inspection_b.id}/"
        for action_url, payload in [
            ("assign/", {"inspector_name": "Ghost Officer"}),
            ("checkin/", {"latitude": 6.4, "longitude": 3.4}),
            ("complete/", {"outcome": "PASSED"}),
            ("log-finding/", {"title": "X", "description": "Y"}),
            ("issue-stop-work/", {"reason": "Z"}),
            ("create-reinspection/", {}),
        ]:
            response = self.client.post(base_b + action_url, payload, format="json")
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND,
                             msg=f"{action_url} must be out of scope for another client")

    def test_client_can_update_own_inspection(self):
        self.auth(self.client_a)
        url = INSPECTIONS_URL + f"{self.inspection_a.id}/"
        response = self.client.patch(url, {"summary_notes": "Client note"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.inspection_a.refresh_from_db()
        self.assertEqual(self.inspection_a.summary_notes, "Client note")

    def test_superuser_sees_all_projects(self):
        self.auth(self.officer)
        response = self.client.get(INSPECTIONS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_district_officer_scoped_to_district_projects(self):
        district = District.objects.create(name="Eti-Osa Views District", code="ETI-V")
        other_district = District.objects.create(name="Ibeju Views District", code="IBE-V")
        role = Role.objects.create(name="Building Officer")
        officer = User.objects.create_user(
            username="district.officer@nexucon.com", email="district.officer@nexucon.com",
            password="Password123!", first_name="Dora", last_name="District")
        Profile.objects.create(user=officer, role=role, district=district)
        self.project_a.district = district
        self.project_a.save()
        self.project_b.district = other_district
        self.project_b.save()

        self.auth(officer)
        response = self.client.get(INSPECTIONS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        refs = [item["inspection_reference"] for item in response.data]
        self.assertIn(self.inspection_a.inspection_reference, refs)
        self.assertNotIn(self.inspection_b.inspection_reference, refs)
        # Cross-district detail access is a 404, not a silent success.
        self.assertEqual(
            self.client.get(INSPECTIONS_URL + f"{self.inspection_b.id}/").status_code,
            status.HTTP_404_NOT_FOUND)


class InspectionCreateAPITestCase(InspectionViewTestBase):
    """POST /inspections/ — real rows, validation errors, scope enforcement."""

    def test_create_inspection_with_valid_data(self):
        self.auth(self.officer)
        response = self.client.post(INSPECTIONS_URL, {
            "project": str(self.project_a.id),
            "inspection_type": "Structural Review",
            "priority": "Critical",
            "summary_notes": "Column slabs verification",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data["success"])
        inspection = Inspection.objects.get(id=response.data["data"]["id"])
        self.assertEqual(inspection.project, self.project_a)
        self.assertEqual(inspection.status, "REQUESTED")
        self.assertEqual(inspection.inspection_type, "Structural Review")
        self.assertEqual(inspection.requested_by_name, self.officer.email)  # real requester
        self.assertGreater(len(inspection.checklist_results), 0)  # real default checklist
        self.assertTrue(AuditEvent.objects.filter(
            resource_id=str(inspection.id), action="INSPECTION_REQUESTED").exists())

    def test_create_with_empty_scheduled_date_and_permit_is_accepted(self):
        self.auth(self.officer)
        response = self.client.post(INSPECTIONS_URL, {
            "project": str(self.project_a.id),
            "scheduled_date": "",
            "permit": "",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        inspection = Inspection.objects.get(id=response.data["data"]["id"])
        self.assertIsNone(inspection.scheduled_date)
        self.assertIsNone(inspection.permit)

    def test_create_without_project_returns_400(self):
        self.auth(self.officer)
        response = self.client.post(INSPECTIONS_URL, {
            "inspection_type": "Structural Review"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data["success"])
        self.assertIn("project", response.data["errors"])

    def test_create_with_invalid_fields_returns_400(self):
        self.auth(self.officer)
        for payload in (
            {"project": str(self.project_a.id), "inspection_type": "Witchcraft Survey"},
            {"project": str(self.project_a.id), "priority": "Ultra"},
            {"project": "not-a-uuid"},
        ):
            response = self.client.post(INSPECTIONS_URL, payload, format="json")
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST,
                             msg=f"{payload} should fail validation")

    def test_client_cannot_create_inspection_for_another_projects(self):
        self.auth(self.client_a)
        response = self.client.post(INSPECTIONS_URL, {
            "project": str(self.project_b.id),
            "inspection_type": "Safety Audit",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            Inspection.objects.filter(project=self.project_b).count(), 1)  # only the fixture row


class InspectionFilterSearchTestCase(InspectionViewTestBase):
    """Query params on the inspection list endpoint."""

    def setUp(self):
        super().setUp()
        self.auth(self.officer)
        self.insp_scheduled = Inspection.objects.create(
            project=self.project_a, inspection_type="MEP Inspection", status="SCHEDULED")
        self.insp_active = Inspection.objects.create(
            project=self.project_a, inspection_type="Site Verification", status="IN_PROGRESS")
        self.insp_completed = Inspection.objects.create(
            project=self.project_b, inspection_type="Final Clearance", status="COMPLETED")
        self.insp_failed = Inspection.objects.create(
            project=self.project_b, inspection_type="Drainage & Environmental", status="FAILED")
        self.insp_reinspection = Inspection.objects.create(
            project=self.project_a, inspection_type="Re-Inspection", status="REQUESTED")
        Finding.objects.create(
            inspection=self.insp_completed, project=self.project_b,
            title="Cracked beam", description="Shear crack", severity="HIGH")

    def _refs(self, **params):
        response = self.client.get(INSPECTIONS_URL, params)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["inspection_reference"] for item in response.data}

    def test_status_tab_filters(self):
        self.assertIn(self.inspection_a.inspection_reference, self._refs(status="requests"))
        self.assertIn(self.insp_scheduled.inspection_reference, self._refs(status="schedule"))
        self.assertIn(self.insp_active.inspection_reference, self._refs(status="active"))
        self.assertIn(self.insp_completed.inspection_reference, self._refs(status="reports"))
        self.assertIn(self.insp_failed.inspection_reference, self._refs(status="stop-work"))
        self.assertIn(self.insp_reinspection.inspection_reference, self._refs(status="re-inspections"))
        self.assertIn(self.insp_completed.inspection_reference, self._refs(status="findings"))
        self.assertNotIn(self.inspection_b.inspection_reference, self._refs(status="findings"))

    def test_raw_status_filter_uppercased(self):
        refs = self._refs(status="completed")
        self.assertIn(self.insp_completed.inspection_reference, refs)
        self.assertNotIn(self.inspection_a.inspection_reference, refs)

    def test_project_priority_and_type_filters(self):
        refs = self._refs(project=str(self.project_a.id))
        self.assertEqual(refs, {i.inspection_reference for i in
                                Inspection.objects.filter(project=self.project_a)})
        refs = self._refs(priority="high")
        self.assertEqual(refs, {self.inspection_a.inspection_reference})
        refs = self._refs(type="foundation")
        self.assertEqual(refs, {self.inspection_a.inspection_reference})

    def test_inspector_filter_by_id_and_name(self):
        inspector = User.objects.create_user(
            username="filter.inspector@nexucon.com", email="filter.inspector@nexucon.com",
            password="Password123!", first_name="Femi", last_name="Filter")
        self.insp_scheduled.inspector = inspector
        self.insp_scheduled.inspector_name = "Femi Filter"
        self.insp_scheduled.save()
        self.insp_active.inspector_name = "Named Officer Only"
        self.insp_active.save()

        by_id = self._refs(inspector=str(inspector.id))
        self.assertIn(self.insp_scheduled.inspection_reference, by_id)
        self.assertNotIn(self.insp_active.inspection_reference, by_id)

        by_name = self._refs(inspector="Named Officer")
        self.assertIn(self.insp_active.inspection_reference, by_name)
        self.assertNotIn(self.insp_scheduled.inspection_reference, by_name)

    def test_search_matches_reference_project_name_and_type(self):
        by_reference = self._refs(search=self.insp_completed.inspection_reference[:12])
        self.assertIn(self.insp_completed.inspection_reference, by_reference)
        by_project = self._refs(search="Alpha View")
        self.assertIn(self.inspection_a.inspection_reference, by_project)
        self.assertNotIn(self.insp_completed.inspection_reference, by_project)
        by_type = self._refs(search="Drainage")
        self.assertIn(self.insp_failed.inspection_reference, by_type)


class InspectionStatsAPITestCase(InspectionViewTestBase):
    """The stats action counts only the requesting user's scoped projects."""

    def test_superuser_stats_count_all_projects(self):
        Inspection.objects.create(project=self.project_b, inspection_type="Safety Audit",
                                  status="SCHEDULED")
        self.auth(self.officer)
        response = self.client.get(INSPECTIONS_URL + "stats/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual(data["requests"], 2)
        self.assertEqual(data["schedule"], 1)
        self.assertEqual(data["total"], 3)

    def test_client_stats_only_count_own_project(self):
        Inspection.objects.create(project=self.project_b, inspection_type="Safety Audit",
                                  status="SCHEDULED")
        Finding.objects.create(
            inspection=self.inspection_a, project=self.project_a,
            title="Unresolved defect", description="Open", severity="LOW")
        self.auth(self.client_a)
        response = self.client.get(INSPECTIONS_URL + "stats/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data["data"]
        self.assertEqual(data["requests"], 1)     # only project_a's REQUESTED row
        self.assertEqual(data["schedule"], 0)     # project_b SCHEDULED invisible
        self.assertEqual(data["findings"], 1)     # own unresolved finding
        self.assertEqual(data["total"], 1)

    def test_stats_anonymous_rejected(self):
        self.assertEqual(self.client.get(INSPECTIONS_URL + "stats/").status_code,
                         status.HTTP_401_UNAUTHORIZED)


class InspectionWorkflowActionsAPITestCase(InspectionViewTestBase):
    """Detail actions: assign, checkin, complete, log-finding, stop-work, reinspection."""

    def setUp(self):
        super().setUp()
        self.auth(self.officer)
        self.inspector = User.objects.create_user(
            username="actions.inspector@nexucon.com", email="actions.inspector@nexucon.com",
            password="Password123!", first_name="Ifeoma", last_name="Action")
        self.url = INSPECTIONS_URL + f"{self.inspection_a.id}/"

    def test_assign_inspector_by_id(self):
        response = self.client.post(self.url + "assign/", {
            "inspector_id": str(self.inspector.id),
            "scheduled_date": "2026-10-01T09:00:00Z",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.inspection_a.refresh_from_db()
        self.assertEqual(self.inspection_a.status, "SCHEDULED")
        self.assertEqual(self.inspection_a.inspector, self.inspector)
        self.assertEqual(self.inspection_a.inspector_name, "Ifeoma Action")

    def test_assign_inspector_by_email_lookup(self):
        response = self.client.post(self.url + "assign/", {
            "inspector_id": "actions.inspector@nexucon.com",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.inspection_a.refresh_from_db()
        self.assertEqual(self.inspection_a.inspector, self.inspector)

    def test_assign_without_inspector_returns_400(self):
        response = self.client.post(self.url + "assign/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.inspection_a.refresh_from_db()
        self.assertEqual(self.inspection_a.status, "REQUESTED")  # unchanged

    def test_gps_checkin(self):
        response = self.client.post(self.url + "checkin/", {
            "latitude": 6.4281, "longitude": 3.4219}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.inspection_a.refresh_from_db()
        self.assertEqual(self.inspection_a.status, "IN_PROGRESS")
        self.assertTrue(self.inspection_a.gps_verified)
        self.assertAlmostEqual(self.inspection_a.gps_latitude, 6.4281)

    def test_complete_with_valid_outcome(self):
        response = self.client.post(self.url + "complete/", {
            "outcome": "PASSED",
            "checklist_results": [{"id": "chk_1", "item": "Excavation", "status": "PASSED"}],
            "summary_notes": "All good",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.inspection_a.refresh_from_db()
        self.assertEqual(self.inspection_a.status, "COMPLETED")
        self.assertEqual(self.inspection_a.outcome, "PASSED")
        self.assertIsNotNone(self.inspection_a.completed_date)

    def test_complete_with_invalid_outcome_returns_400(self):
        response = self.client.post(self.url + "complete/", {
            "outcome": "SPLENDID"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.inspection_a.refresh_from_db()
        self.assertEqual(self.inspection_a.status, "REQUESTED")  # nothing persisted

    def test_complete_with_failed_outcome_moves_status_to_failed(self):
        """A `FAILED` verdict must move `status`, not just the `outcome` column.

        `InspectionExecutionService.submit` sets `status='COMPLETED'` for every
        submission, so the verdict is what distinguishes a passed inspection
        from a failed one at the status level. The inspector PWA reaches this
        endpoint (it used to PATCH the two columns directly, which wrote
        `outcome='FAILED'` and left `status='COMPLETED'` standing) — this
        asserts the behaviour that re-pointing it depends on.
        """
        response = self.client.post(self.url + "complete/", {
            "outcome": "FAILED", "summary_notes": "Spalling at column B3"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.inspection_a.refresh_from_db()
        self.assertEqual(self.inspection_a.outcome, "FAILED")
        self.assertEqual(self.inspection_a.status, "FAILED")

    def test_complete_writes_outcome_to_the_audit_log(self):
        """The verdict gets its own audit entry, naming the outcome.

        This is the second reason the inspector PWA posts here rather than
        PATCHing the serializer: the generic update writes columns and nothing
        else, so the audit trail carried the sealed submission but never the
        verdict that submission existed to support.
        """
        self.client.post(self.url + "complete/", {
            "outcome": "CONDITIONAL_PASS", "summary_notes": "Recheck in 14 days"}, format="json")
        event = AuditEvent.objects.filter(
            resource_id=str(self.inspection_a.id),
            action="INSPECTION_COMPLETED_CONDITIONAL_PASS",
        ).first()
        self.assertIsNotNone(event, "the outcome was not recorded in the audit log")
        self.assertEqual(event.resource_type, "Inspection")
        self.assertEqual(event.user, self.officer)
        self.assertEqual(event.new_state["outcome"], "CONDITIONAL_PASS")
        self.assertEqual(event.new_state["status"], "COMPLETED")

    def test_complete_does_not_overwrite_checklist_when_omitted(self):
        """Absent `checklist_results` leaves the sealed submission's copy alone.

        The PWA deliberately omits the field: the submission already carries
        the checklist inside the hash chain, and letting a second, unsealed
        copy replace it would move a value out of the sealed record.
        """
        self.inspection_a.checklist_results = [{"item_id": "sealed", "result": "PASS"}]
        self.inspection_a.save(update_fields=["checklist_results"])

        self.client.post(self.url + "complete/", {
            "outcome": "PASSED", "summary_notes": "ok"}, format="json")

        self.inspection_a.refresh_from_db()
        self.assertEqual(self.inspection_a.checklist_results,
                         [{"item_id": "sealed", "result": "PASS"}])

    def test_log_finding_creates_real_row(self):
        response = self.client.post(self.url + "log-finding/", {
            "title": "Honeycombed column",
            "description": "Void in column B3",
            "severity": "HIGH",
            "category": "STRUCTURAL",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        finding = Finding.objects.get(id=response.data["data"]["id"])
        self.assertEqual(finding.inspection, self.inspection_a)
        self.assertEqual(finding.project, self.project_a)
        self.assertTrue(finding.finding_reference.startswith("FND-"))

    def test_issue_stop_work_suspends_project(self):
        response = self.client.post(self.url + "issue-stop-work/", {
            "reason": "Structural deviation beyond approved drawings"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        swo = StopWorkOrder.objects.get(id=response.data["data"]["id"])
        self.assertEqual(swo.status, "ACTIVE")
        self.assertEqual(swo.project, self.project_a)
        self.project_a.refresh_from_db()
        self.assertEqual(self.project_a.status, "SUSPENDED")
        self.inspection_a.refresh_from_db()
        self.assertEqual(self.inspection_a.status, "FAILED")

    def test_create_reinspection_links_parent(self):
        response = self.client.post(self.url + "create-reinspection/", {
            "inspector_name": "Followup Officer",
            "priority": "High",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        reinspection = Inspection.objects.get(id=response.data["data"]["id"])
        self.assertEqual(reinspection.parent_inspection, self.inspection_a)
        self.assertEqual(reinspection.status, "SCHEDULED")
        self.inspection_a.refresh_from_db()
        self.assertEqual(self.inspection_a.status, "RE_INSPECTION_REQUIRED")


class StopWorkOrderAPITestCase(InspectionViewTestBase):
    """StopWorkOrderViewSet: create, resolve project, filters, scoping, lift."""

    def setUp(self):
        super().setUp()
        self.swo_a = StopWorkOrder.objects.create(
            project=self.project_a, reason="Alpha site violation",
            severity="CRITICAL", status="ACTIVE")
        self.swo_b = StopWorkOrder.objects.create(
            project=self.project_b, reason="Beta site violation",
            severity="MAJOR", status="APPEALED")

    def test_anonymous_rejected(self):
        self.assertEqual(self.client.get(SWO_URL).status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self.client.post(SWO_URL, {}, format="json").status_code,
                         status.HTTP_401_UNAUTHORIZED)

    def test_create_with_project_uuid(self):
        self.auth(self.officer)
        response = self.client.post(SWO_URL, {
            "project": str(self.project_b.id),
            "reason": "Unauthorized decking",
            "severity": "CRITICAL",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        swo = StopWorkOrder.objects.get(id=response.data["data"]["id"])
        self.assertEqual(swo.project, self.project_b)
        self.assertTrue(swo.order_number.startswith("SWO-"))
        self.project_b.refresh_from_db()
        self.assertEqual(self.project_b.status, "SUSPENDED")

    def test_create_resolves_project_by_reference_number(self):
        self.auth(self.officer)
        response = self.client.post(SWO_URL, {
            "project": self.project_b.reference_number,
            "reason": "By reference",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(StopWorkOrder.objects.get(id=response.data["data"]["id"]).project,
                         self.project_b)

    def test_create_requires_project(self):
        self.auth(self.officer)
        response = self.client.post(SWO_URL, {"reason": "No target"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_with_unknown_project_returns_404(self):
        self.auth(self.officer)
        response = self.client.post(SWO_URL, {
            "project": "NXC-GOV-9999-ZZZZ", "reason": "Missing site"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_client_cannot_halt_another_projects_site(self):
        self.auth(self.client_a)
        response = self.client.post(SWO_URL, {
            "project": str(self.project_b.id), "reason": "Overreach"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(self.project_b.status, "ACTIVE")  # untouched

    def test_client_scoped_listing_and_detail(self):
        self.auth(self.client_a)
        response = self.client.get(SWO_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        numbers = [item["order_number"] for item in response.data]
        self.assertIn(self.swo_a.order_number, numbers)
        self.assertNotIn(self.swo_b.order_number, numbers)
        self.assertEqual(self.client.get(SWO_URL + f"{self.swo_b.id}/").status_code,
                         status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            self.client.post(SWO_URL + f"{self.swo_b.id}/lift/", {}, format="json").status_code,
            status.HTTP_404_NOT_FOUND)

    def test_status_search_and_project_filters(self):
        self.auth(self.officer)
        response = self.client.get(SWO_URL, {"status": "ACTIVE"})
        self.assertEqual({item["order_number"] for item in response.data},
                         {self.swo_a.order_number})
        response = self.client.get(SWO_URL, {"status": "ALL"})
        self.assertEqual(len(response.data), 2)
        response = self.client.get(SWO_URL, {"search": "Beta site"})
        self.assertEqual({item["order_number"] for item in response.data},
                         {self.swo_b.order_number})
        response = self.client.get(SWO_URL, {"project": self.project_a.reference_number})
        self.assertEqual({item["order_number"] for item in response.data},
                         {self.swo_a.order_number})

    def test_lift_reinstates_project(self):
        self.auth(self.officer)
        self.project_a.status = "SUSPENDED"
        self.project_a.save()
        response = self.client.post(SWO_URL + f"{self.swo_a.id}/lift/", {
            "justification": "Rectifications verified on site"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.swo_a.refresh_from_db()
        self.assertEqual(self.swo_a.status, "LIFTED")
        self.assertEqual(self.swo_a.lift_justification, "Rectifications verified on site")
        self.project_a.refresh_from_db()
        self.assertEqual(self.project_a.status, "ACTIVE")

    def test_stats_are_scoped_to_the_requester(self):
        self.auth(self.client_a)
        response = self.client.get(SWO_URL + "stats/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["data"]["active"], 1)   # own project only
        self.assertEqual(response.data["data"]["total"], 1)
        self.auth(self.officer)
        response = self.client.get(SWO_URL + "stats/")
        self.assertEqual(response.data["data"]["active"], 1)
        self.assertEqual(response.data["data"]["pending_appeals"], 1)


class FindingAPITestCase(InspectionViewTestBase):
    """FindingViewSet: filters, resolve action, and project scoping."""

    def setUp(self):
        super().setUp()
        self.finding_a = Finding.objects.create(
            inspection=self.inspection_a, project=self.project_a,
            title="Cracked slab", description="Hairline cracks", severity="HIGH")
        self.finding_a2 = Finding.objects.create(
            inspection=self.inspection_a, project=self.project_a,
            title="Missing PPE", description="No helmets", severity="LOW",
            is_resolved=True, resolution_notes="Rectified")
        self.finding_b = Finding.objects.create(
            inspection=self.inspection_b, project=self.project_b,
            title="Beta defect", description="Scaffolding risk", severity="CRITICAL")

    def test_anonymous_rejected(self):
        self.assertEqual(self.client.get(FINDINGS_URL).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_filters_severity_resolved_and_project(self):
        self.auth(self.officer)
        response = self.client.get(FINDINGS_URL, {"severity": "HIGH"})
        self.assertEqual({item["finding_reference"] for item in response.data},
                         {self.finding_a.finding_reference})
        response = self.client.get(FINDINGS_URL, {"is_resolved": "false"})
        self.assertEqual({item["finding_reference"] for item in response.data},
                         {self.finding_a.finding_reference, self.finding_b.finding_reference})
        response = self.client.get(FINDINGS_URL, {"is_resolved": "true"})
        self.assertEqual({item["finding_reference"] for item in response.data},
                         {self.finding_a2.finding_reference})
        response = self.client.get(FINDINGS_URL, {"project": str(self.project_b.id)})
        self.assertEqual({item["finding_reference"] for item in response.data},
                         {self.finding_b.finding_reference})

    def test_client_only_sees_own_projects_findings(self):
        self.auth(self.client_a)
        response = self.client.get(FINDINGS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        refs = {item["finding_reference"] for item in response.data}
        self.assertEqual(refs, {self.finding_a.finding_reference, self.finding_a2.finding_reference})
        self.assertEqual(
            self.client.get(FINDINGS_URL + f"{self.finding_b.id}/").status_code,
            status.HTTP_404_NOT_FOUND)

    def test_client_cannot_resolve_other_projects_finding(self):
        self.auth(self.client_a)
        response = self.client.post(FINDINGS_URL + f"{self.finding_b.id}/resolve/", {},
                                    format="json")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.finding_b.refresh_from_db()
        self.assertFalse(self.finding_b.is_resolved)

    def test_resolve_marks_finding_resolved(self):
        self.auth(self.officer)
        response = self.client.post(FINDINGS_URL + f"{self.finding_a.id}/resolve/", {
            "notes": "Epoxy injection completed"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.finding_a.refresh_from_db()
        self.assertTrue(self.finding_a.is_resolved)
        self.assertIsNotNone(self.finding_a.resolved_at)
        self.assertEqual(self.finding_a.resolution_notes, "Epoxy injection completed")

    def test_create_finding_via_api(self):
        self.auth(self.officer)
        response = self.client.post(FINDINGS_URL, {
            "inspection": str(self.inspection_a.id),
            "project": str(self.project_a.id),
            "title": "API-created finding",
            "description": "Direct POST",
            "severity": "MEDIUM",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(Finding.objects.filter(title="API-created finding").exists())


class ChecklistAPITestCase(InspectionViewTestBase):
    """ChecklistViewSet: plain authenticated CRUD."""

    def test_anonymous_rejected(self):
        self.assertEqual(self.client.get(CHECKLISTS_URL).status_code,
                         status.HTTP_401_UNAUTHORIZED)

    def test_create_and_list_checklist(self):
        self.auth(self.officer)
        response = self.client.post(CHECKLISTS_URL, {
            "name": "Foundation Verification Checklist",
            "inspection_type": "Foundation Inspection",
            "items": [{"id": "chk_1", "item": "Soil bearing verified"}],
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        checklist = Checklist.objects.get(id=response.data["id"])
        self.assertEqual(checklist.items[0]["id"], "chk_1")
        response = self.client.get(CHECKLISTS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_create_without_name_returns_400(self):
        self.auth(self.officer)
        response = self.client.post(CHECKLISTS_URL, {"inspection_type": "Safety Audit"},
                                    format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class IssueNCRCorrectiveActionAPITestCase(InspectionViewTestBase):
    """QC Issues, NCRs and Corrective Actions: auth, CRUD and nested actions."""

    def setUp(self):
        super().setUp()
        # These viewsets cache list/retrieve responses; keep tests isolated.
        cache.clear()
        self.addCleanup(cache.clear)

    def test_anonymous_crud_rejected(self):
        for url in (ISSUES_URL, NCRS_URL, CORRECTIVE_ACTIONS_URL):
            self.assertEqual(self.client.get(url).status_code,
                             status.HTTP_401_UNAUTHORIZED, msg=f"GET {url}")
            self.assertEqual(self.client.post(url, {}, format="json").status_code,
                             status.HTTP_401_UNAUTHORIZED, msg=f"POST {url}")

    def test_issue_crud_and_commenting(self):
        self.auth(self.officer)
        response = self.client.post(ISSUES_URL, {
            "title": "Rebar cover inadequate",
            "description": "Cover blocks missing on level 3",
            "priority": "high",
            "project": str(self.project_a.id),
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        issue = Issue.objects.get(id=response.data["id"])
        self.assertEqual(issue.created_by, self.officer)
        self.assertEqual(issue.status, "open")

        # List reflects the created issue.
        response = self.client.get(ISSUES_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(str(issue.id), [item["id"] for item in response.data])

        # Patch status through the API.
        response = self.client.patch(ISSUES_URL + f"{issue.id}/", {"status": "in_progress"},
                                     format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        issue.refresh_from_db()
        self.assertEqual(issue.status, "in_progress")

        # Comments via the nested action.
        response = self.client.post(ISSUES_URL + f"{issue.id}/add_comment/", {
            "text": "Contractor notified"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(issue.comments.count(), 1)
        self.assertEqual(issue.comments.first().user, self.officer)

        # A comment without text is a 400, never a crash.
        response = self.client.post(ISSUES_URL + f"{issue.id}/add_comment/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_issue_create_validation_error(self):
        self.auth(self.officer)
        response = self.client.post(ISSUES_URL, {"priority": "high"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_ncr_create_generates_reference_number(self):
        self.auth(self.officer)
        response = self.client.post(NCRS_URL, {
            "description": "Concrete strength below specified grade",
            "severity": "critical",
            "project": str(self.project_a.id),
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        ncr = NonConformanceReport.objects.get(id=response.data["id"])
        self.assertTrue(ncr.ncr_number.startswith("NCR-"))
        self.assertEqual(ncr.status, "draft")

    def test_ncr_add_corrective_action(self):
        self.auth(self.officer)
        ncr = NonConformanceReport.objects.create(
            ncr_number=f"NCR-TEST-{uuid_hex(6)}",
            description="Core test failure", severity="high")
        response = self.client.post(NCRS_URL + f"{ncr.id}/add_corrective_action/", {
            "action_description": "Re-pour affected slab after verification",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(ncr.corrective_actions.count(), 1)
        action = ncr.corrective_actions.first()
        self.assertEqual(action.status, "open")

        # Missing action_description is a 400.
        response = self.client.post(NCRS_URL + f"{ncr.id}/add_corrective_action/", {},
                                    format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_corrective_action_standalone_create_and_list(self):
        self.auth(self.officer)
        ncr = NonConformanceReport.objects.create(
            ncr_number=f"NCR-TEST-{uuid_hex(6)}",
            description="Standalone action target", severity="medium")
        response = self.client.post(CORRECTIVE_ACTIONS_URL, {
            "ncr": str(ncr.id),
            "action_description": "Verify remediation",
            "status": "open",
        }, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        action = CorrectiveAction.objects.get(id=response.data["id"])
        self.assertEqual(action.ncr, ncr)

        response = self.client.get(CORRECTIVE_ACTIONS_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn(str(action.id), [item["id"] for item in response.data])


def uuid_hex(length=6):
    import uuid as _uuid
    return _uuid.uuid4().hex[:length].upper()


# ==========================================================================
# Site geofencing (Inspector PWA Module 2)
#
# The spec: "⚠️ WARNING: You must be within 50m of the site to check in".
# Before this, `checkin()` set gps_verified=True after checking only that the
# coordinates were numeric and in global range — no distance was ever
# measured, so the flag attested nothing. These tests pin the three states and
# the reasons that produce them, and in particular pin the two cases where the
# honest answer is "could not be verified" rather than "pass".
# ==========================================================================

class GeofenceEvaluationTests(TestCase):
    """Unit coverage of evaluate_geofence / effective_geofence_radius."""

    def setUp(self):
        self.project = Project.objects.create(
            name="Lekki Site", latitude=SITE_LAT, longitude=SITE_LON)

    def test_within_radius_is_verified(self):
        result = evaluate_geofence(self.project, SITE_LAT, SITE_LON, accuracy_m=5.0)
        self.assertEqual(result.state, STATE_VERIFIED)
        self.assertEqual(result.reason, REASON_WITHIN_RADIUS)
        self.assertTrue(result.verified)
        self.assertTrue(result.checked)
        self.assertAlmostEqual(result.distance_m, 0.0, places=3)
        self.assertEqual(result.radius_m, 50)
        self.assertEqual(result.radius_source, 'platform_default')

    def test_project_radius_overrides_the_platform_default(self):
        self.project.geofence_radius_m = 250
        self.project.save()
        radius_m, source = effective_geofence_radius(self.project)
        self.assertEqual((radius_m, source), (250, 'project'))
        # ~100 m away: outside a 50 m default, inside this project's 250 m.
        result = evaluate_geofence(
            self.project, SITE_LAT + 0.0009, SITE_LON, accuracy_m=5.0)
        self.assertEqual(result.state, STATE_VERIFIED)
        self.assertEqual(result.radius_source, 'project')

    def test_just_inside_the_radius_is_verified(self):
        """The boundary itself is inclusive — 50 m away is within 50 m."""
        result = evaluate_geofence(
            self.project, SITE_LAT + 0.000449, SITE_LON, accuracy_m=1.0)
        self.assertLessEqual(result.distance_m, 50)
        self.assertEqual(result.state, STATE_VERIFIED)

    def test_outside_radius_records_the_measured_distance(self):
        result = evaluate_geofence(
            self.project, SITE_LAT + 0.0009, SITE_LON, accuracy_m=5.0)
        self.assertEqual(result.state, STATE_OUTSIDE)
        self.assertEqual(result.reason, REASON_OUTSIDE_RADIUS)
        self.assertFalse(result.verified)
        self.assertTrue(result.checked)
        self.assertAlmostEqual(result.distance_m, 100.0, delta=1.0)
        self.assertEqual(result.as_dict()['distance_m'],
                         round(result.distance_m, 1))

    def test_project_without_coordinates_is_unverifiable_not_a_pass(self):
        """An unknown site point is not "inside" anything."""
        project = Project.objects.create(name="Unsurveyed Site")
        result = evaluate_geofence(project, SITE_LAT, SITE_LON, accuracy_m=5.0)
        self.assertEqual(result.state, STATE_UNVERIFIABLE)
        self.assertEqual(result.reason, REASON_PROJECT_COORDINATES_NOT_RECORDED)
        self.assertFalse(result.verified)
        self.assertFalse(result.checked)
        self.assertIsNone(result.distance_m)

    def test_half_recorded_coordinates_are_unverifiable(self):
        """Latitude without longitude is not a position."""
        project = Project.objects.create(name="Half Surveyed", latitude=SITE_LAT)
        result = evaluate_geofence(project, SITE_LAT, SITE_LON, accuracy_m=5.0)
        self.assertEqual(result.state, STATE_UNVERIFIABLE)
        self.assertEqual(result.reason, REASON_PROJECT_COORDINATES_NOT_RECORDED)

    def test_accuracy_wider_than_the_radius_cannot_certify_it(self):
        """A ±200 m fix cannot witness a 50 m enclosure."""
        result = evaluate_geofence(self.project, SITE_LAT, SITE_LON, accuracy_m=200.0)
        self.assertEqual(result.state, STATE_UNVERIFIABLE)
        self.assertEqual(result.reason, REASON_DEVICE_ACCURACY_EXCEEDS_RADIUS)
        self.assertFalse(result.verified)
        # The distance is still reported — it was measurable, just not decisive.
        self.assertTrue(result.checked)

    def test_missing_accuracy_is_unverifiable_not_a_pass(self):
        """No reported accuracy means the fence cannot be certified."""
        for missing in (None, ''):
            result = evaluate_geofence(self.project, SITE_LAT, SITE_LON,
                                       accuracy_m=missing)
            self.assertEqual(result.state, STATE_UNVERIFIABLE)
            self.assertEqual(result.reason, REASON_ACCURACY_NOT_REPORTED)
            self.assertFalse(result.verified)
            self.assertIsNone(result.accuracy_m)

    def test_unparseable_accuracy_is_treated_as_not_reported(self):
        for bad in ('unknown', 'NaN', float('inf'), -1.0):
            result = evaluate_geofence(self.project, SITE_LAT, SITE_LON,
                                       accuracy_m=bad)
            self.assertEqual(result.reason, REASON_ACCURACY_NOT_REPORTED)
            self.assertFalse(result.verified)

    def test_accuracy_equal_to_the_radius_is_still_usable(self):
        result = evaluate_geofence(self.project, SITE_LAT, SITE_LON, accuracy_m=50.0)
        self.assertEqual(result.state, STATE_VERIFIED)


class GeofenceCheckinAPITests(APITestCase):
    """The check-in endpoint's geofence behaviour, end to end."""

    def setUp(self):
        self.officer = User.objects.create_superuser(
            username='geofence_officer@nexucon.com',
            email='geofence_officer@nexucon.com',
            password='Password123!',
        )
        self.project = Project.objects.create(
            name='Geofenced Site',
            latitude=SITE_LAT, longitude=SITE_LON,
        )
        self.inspection = Inspection.objects.create(
            project=self.project, inspection_type='Foundation Inspection')
        self.client.force_authenticate(self.officer)

    def _url(self, name, inspection=None):
        return reverse(name, kwargs={
            'inspection_id': str((inspection or self.inspection).id)})

    def _checkin(self, **body):
        return self.client.post(
            self._url('inspection-checkin'), body, format='json')

    # ------------------------------------------------------------ verified
    def test_on_site_checkin_is_verified(self):
        response = self._checkin(
            latitude=SITE_LAT, longitude=SITE_LON, gps_accuracy_m=6.0)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['gps_verified'])
        self.assertEqual(response.data['geofence']['state'], 'VERIFIED')
        self.assertEqual(response.data['geofence']['reason'], 'WITHIN_RADIUS')
        self.assertEqual(response.data['geofence']['radius_m'], 50)
        self.assertEqual(response.data['geofence']['radius_source'],
                         'platform_default')
        self.inspection.refresh_from_db()
        self.assertTrue(self.inspection.gps_verified)
        self.assertEqual(self.inspection.geofence_state, 'VERIFIED')

    # ------------------------------------------------------------- outside
    def test_off_site_checkin_is_recorded_and_reports_the_distance(self):
        """Under the default 'warn' policy the check-in succeeds but is not
        verified, and the response states how far away the inspector was."""
        response = self._checkin(
            latitude=SITE_LAT + 0.0009, longitude=SITE_LON, gps_accuracy_m=6.0)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['gps_verified'])
        self.assertEqual(response.data['geofence']['state'], 'OUTSIDE')
        self.assertEqual(response.data['geofence']['reason'], 'OUTSIDE_RADIUS')
        self.assertAlmostEqual(response.data['geofence']['distance_m'], 100.0,
                               delta=1.0)
        self.inspection.refresh_from_db()
        self.assertFalse(self.inspection.gps_verified)
        self.assertEqual(self.inspection.geofence_state, 'OUTSIDE')
        self.assertAlmostEqual(self.inspection.geofence_distance_m, 100.0,
                               delta=1.0)

    @override_settings(GEOFENCE_ENFORCEMENT='strict')
    def test_off_site_checkin_is_refused_under_strict_enforcement(self):
        response = self._checkin(
            latitude=SITE_LAT + 0.0009, longitude=SITE_LON, gps_accuracy_m=6.0)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # The refusal states the measured distance, not just "no".
        self.assertIn('100', response.data['detail'])
        self.assertIn('50', response.data['detail'])
        self.inspection.refresh_from_db()
        self.assertIsNone(self.inspection.checkin_time)

    # -------------------------------------------------------- unverifiable
    def test_checkin_without_reported_accuracy_is_not_verified(self):
        response = self._checkin(latitude=SITE_LAT, longitude=SITE_LON)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['gps_verified'])
        self.assertEqual(response.data['geofence']['state'], 'UNVERIFIABLE')
        self.assertEqual(response.data['geofence']['reason'],
                         'ACCURACY_NOT_REPORTED')
        self.assertIsNone(response.data['geofence']['accuracy_m'])

    def test_checkin_with_accuracy_wider_than_the_radius_is_not_verified(self):
        response = self._checkin(
            latitude=SITE_LAT, longitude=SITE_LON, gps_accuracy_m=200.0)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['gps_verified'])
        self.assertEqual(response.data['geofence']['reason'],
                         'DEVICE_ACCURACY_EXCEEDS_RADIUS')

    def test_checkin_on_a_site_without_coordinates_is_not_verified(self):
        unsurveyed = Project.objects.create(name='Unsurveyed Site')
        inspection = Inspection.objects.create(
            project=unsurveyed, inspection_type='Safety Audit')
        response = self.client.post(
            self._url('inspection-checkin', inspection),
            {'latitude': SITE_LAT, 'longitude': SITE_LON, 'gps_accuracy_m': 6.0},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['gps_verified'])
        self.assertEqual(response.data['geofence']['state'], 'UNVERIFIABLE')
        self.assertEqual(response.data['geofence']['reason'],
                         'PROJECT_COORDINATES_NOT_RECORDED')
        self.assertIsNone(response.data['geofence']['distance_m'])

    @override_settings(GEOFENCE_ENFORCEMENT='strict')
    def test_strict_enforcement_refuses_an_unrecorded_site(self):
        """A project with no coordinates cannot be checked into under strict."""
        unsurveyed = Project.objects.create(name='Unsurveyed Strict Site')
        inspection = Inspection.objects.create(
            project=unsurveyed, inspection_type='Safety Audit')
        response = self.client.post(
            self._url('inspection-checkin', inspection),
            {'latitude': SITE_LAT, 'longitude': SITE_LON, 'gps_accuracy_m': 6.0},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('no recorded site coordinates', response.data['detail'])

    @override_settings(GEOFENCE_ENFORCEMENT='off')
    def test_enforcement_off_claims_nothing(self):
        response = self._checkin(
            latitude=SITE_LAT, longitude=SITE_LON, gps_accuracy_m=6.0)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['gps_verified'])
        self.assertEqual(response.data['geofence']['state'], '')
        self.assertEqual(response.data['geofence']['reason'], '')

    # ------------------------------------------------------------- submit
    def test_submit_before_checkin_is_refused(self):
        response = self.client.post(self._url('inspection-submit'), {
            'latitude': SITE_LAT, 'longitude': SITE_LON,
            'checklist_results': [
                {'item_id': 'chk_1', 'title': 'Item', 'result': 'PASS'}],
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Check in before submitting', response.data['detail'])

    def test_submit_does_not_upgrade_an_unverified_checkin(self):
        """Submit reports the check-in's verdict; it must not overwrite it."""
        self._checkin(latitude=SITE_LAT + 0.0009, longitude=SITE_LON,
                      gps_accuracy_m=6.0)
        response = self.client.post(self._url('inspection-submit'), {
            'latitude': SITE_LAT + 0.0009, 'longitude': SITE_LON,
            'checklist_results': [
                {'item_id': 'chk_1', 'title': 'Item', 'result': 'PASS'}],
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.inspection.refresh_from_db()
        self.assertFalse(self.inspection.gps_verified)
        self.assertEqual(self.inspection.geofence_state, 'OUTSIDE')

    def test_strict_enforcement_refuses_submitting_an_unverified_checkin(self):
        # Check in under warn, then submit under strict. (Deliberately not
        # decorated: the check-in has to happen under 'warn' for the test to
        # exercise submit's own gate rather than check-in's.)
        self._checkin(latitude=SITE_LAT + 0.0009, longitude=SITE_LON,
                      gps_accuracy_m=6.0)
        with override_settings(GEOFENCE_ENFORCEMENT='strict'):
            response = self.client.post(self._url('inspection-submit'), {
                'latitude': SITE_LAT + 0.0009, 'longitude': SITE_LON,
                'checklist_results': [
                    {'item_id': 'chk_1', 'title': 'Item', 'result': 'PASS'}],
            }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('not geofence-verified', response.data['detail'])

    # ----------------------------------------------------------- check-out
    def test_checkout_after_checkin_records_time_and_position(self):
        self._checkin(latitude=SITE_LAT, longitude=SITE_LON, gps_accuracy_m=6.0)
        self.inspection.refresh_from_db()
        status_before = self.inspection.status

        response = self.client.post(self._url('inspection-checkout'), {
            'latitude': SITE_LAT, 'longitude': SITE_LON,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.inspection.refresh_from_db()
        self.assertIsNotNone(self.inspection.check_out_time)
        self.assertAlmostEqual(self.inspection.checkout_latitude, SITE_LAT)
        self.assertAlmostEqual(self.inspection.checkout_longitude, SITE_LON)
        # Check-out records a fact; it does not invent a workflow transition.
        self.assertEqual(self.inspection.status, status_before)

    def test_checkout_works_without_a_position_fix(self):
        self._checkin(latitude=SITE_LAT, longitude=SITE_LON, gps_accuracy_m=6.0)
        response = self.client.post(self._url('inspection-checkout'), {},
                                    format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.inspection.refresh_from_db()
        self.assertIsNotNone(self.inspection.check_out_time)
        self.assertIsNone(self.inspection.checkout_latitude)

    def test_checkout_rejects_a_lone_coordinate(self):
        self._checkin(latitude=SITE_LAT, longitude=SITE_LON, gps_accuracy_m=6.0)
        response = self.client.post(self._url('inspection-checkout'),
                                    {'latitude': SITE_LAT}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.inspection.refresh_from_db()
        self.assertIsNone(self.inspection.check_out_time)

    def test_checkout_without_checkin_is_refused(self):
        response = self.client.post(self._url('inspection-checkout'), {},
                                    format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('never checked into', response.data['detail'])

    def test_double_checkout_is_refused(self):
        self._checkin(latitude=SITE_LAT, longitude=SITE_LON, gps_accuracy_m=6.0)
        first = self.client.post(self._url('inspection-checkout'), {},
                                 format='json')
        self.assertEqual(first.status_code, status.HTTP_200_OK)
        second = self.client.post(self._url('inspection-checkout'), {},
                                  format='json')
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('already been checked out', second.data['detail'])

    def test_execution_state_reports_the_geofence(self):
        self._checkin(latitude=SITE_LAT, longitude=SITE_LON, gps_accuracy_m=6.0)
        response = self.client.get(self._url('inspection-execution'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['gps_verified'])
        self.assertTrue(response.data['geofence']['evaluated'])
        self.assertEqual(response.data['geofence']['state'], 'VERIFIED')
        self.assertEqual(response.data['geofence']['radius_m'], 50)
        self.assertEqual(response.data['gps_accuracy_m'], 6.0)

    def test_execution_state_before_checkin_reports_not_evaluated(self):
        response = self.client.get(self._url('inspection-execution'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['geofence']['evaluated'])
        self.assertEqual(response.data['geofence']['state'], '')
        self.assertIsNone(response.data['checkin_time'])

