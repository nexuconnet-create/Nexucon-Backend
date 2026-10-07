import uuid
from django.test import TestCase, override_settings
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from rest_framework import status
from apps.government.models import Agency, District
from apps.projects.models import Project
from apps.evidence.models import EvidenceRecord

User = get_user_model()

@override_settings(SECURE_SSL_REDIRECT=False)
class InspectorMeAndEvidenceTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.inspector_user = User.objects.create_user(
            username='inspector.test',
            email='inspector.test@nexucon.net',
            password='TestPassword123!',
            first_name='Babatunde',
            last_name='Fashola'
        )
        self.agency = Agency.objects.create(name='LASBCA', code='LASBCA')
        self.district = District.objects.create(name='Lekki-Epe Zonal Directorate', code='LEK-EPE')

        # Create stakeholder Inspector record
        from apps.stakeholders.models import Inspector
        self.stakeholder_ins = Inspector.objects.create(
            user=self.inspector_user,
            name='Babatunde Fashola',
            inspector_id='LAG-INS-7788',
            assigned_zone='Lekki-Epe Zonal Directorate'
        )

        self.project = Project.objects.create(
            name='Eko Atlantic Marina Tower',
            reference_number='PRJ-EKO-001',
            status='Active'
        )

        # Create test evidence record
        self.evidence = EvidenceRecord.objects.create(
            project=self.project,
            source_type='scan_defect',
            structural_element_id='COL-C12'
        )

    def test_inspector_me_endpoint_returns_accreditation(self):
        self.client.force_authenticate(user=self.inspector_user)
        res = self.client.get('/api/v1/government/inspectors/me/')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        data = res.json()
        self.assertEqual(data['badge_number'], 'LAG-INS-7788')
        self.assertEqual(data['full_name'], 'Babatunde Fashola')
        self.assertEqual(data['directorate'], 'Lekki-Epe Zonal Directorate')
        self.assertEqual(data['accreditation_status'], 'ACTIVE')
        self.assertFalse(data['is_suspended'])

    def test_unaccredited_user_returns_404(self):
        regular_user = User.objects.create_user(
            username='civilian',
            email='civilian@example.com',
            password='Password123!'
        )
        self.client.force_authenticate(user=regular_user)
        res = self.client.get('/api/v1/government/inspectors/me/')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)
        data = res.json()
        self.assertEqual(data['reason'], 'NOT_ACCREDITED')

    def test_evidence_records_with_photo_source_type_filter(self):
        self.client.force_authenticate(user=self.inspector_user)
        res = self.client.get('/api/v1/evidence/records/?source_type=photo')
        self.assertEqual(res.status_code, status.HTTP_200_OK)

    def test_evidence_records_with_all_filter(self):
        self.client.force_authenticate(user=self.inspector_user)
        res = self.client.get('/api/v1/evidence/records/?source_type=ALL')
        self.assertEqual(res.status_code, status.HTTP_200_OK)


"""
Tests for the Government Inspector Dashboard (apps.government.inspector_views).

The dashboard is the first screen a field inspector opens, and it is the one
place in the platform where a value shown to a user is most easily invented —
a badge number, an agency name, a compliance verdict. Each of those was, at
some point, synthesised rather than recorded:

  * `badge_number` was ``f"LAG-INS-{uuid[:4]}"`` — a decoration that reads as an
    accreditation.
  * `agency` / `district` / `role` fell back to hardcoded Lagos strings, so an
    inspector whose agency was never recorded was shown "LASBCA" as fact.
  * `compliance_status` was ``"COMPLIANT"`` whenever a project had no open
    findings — which is not the same claim as "certified compliant".
  * `project_type` fell back to ``"Residential"`` for every untyped project.
  * `evidence_sync.failed` was hardcoded ``0`` and `last_synced_at` hardcoded
    ``now()``, so the sync indicator could only ever report success.
  * `pending_evidence` filtered ``photos_and_evidence__isnull=False`` against a
    ``JSONField(default=list)`` — SQL NULL never occurs there, so it matched
    every open inspection and the KPI silently duplicated another one.

These tests pin the honest contract in its place: every value is either
recorded or explicitly ``None``.
"""
import datetime

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase
from django.urls import reverse
from django.urls.resolvers import URLResolver
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase

import config.urls as config_urls
from apps.audit.models import AuditEvent
from apps.compliance.models import ComplianceCertificate
from apps.government.models import Agency, District, Inspector, Profile, Role
from apps.inspections.models import Inspection
from apps.projects.models import Project
from apps.sync.models import SyncQueueItem

User = get_user_model()


class UrlRegistrationTests(TestCase):
    """config/urls.py must include each app URLconf exactly once.

    ``apps.digital_eye.urls`` was included twice, which registered every one of
    its route names twice and emitted duplicate drf-spectacular operations. The
    first registration always won resolution, so the duplicate produced no
    behavioural difference — only a doubled schema and a doubled route table.
    """

    def _resolver_count(self, route):
        return sum(
            1
            for pattern in config_urls.urlpatterns
            if isinstance(pattern, URLResolver) and str(pattern.pattern) == route
        )

    def test_digital_eye_urlconf_included_once(self):
        self.assertEqual(self._resolver_count('api/v1/digital-eye/'), 1)

    def test_evidence_urlconf_included_once(self):
        self.assertEqual(self._resolver_count('api/v1/evidence/'), 1)

    def test_digital_eye_routes_still_resolve(self):
        """Removing the duplicate must not have removed the routes themselves."""
        self.assertEqual(
            reverse('field-device-list'), '/api/v1/digital-eye/devices/')
        self.assertEqual(
            reverse('pundit-test-list'), '/api/v1/digital-eye/pundit-tests/')
        self.assertEqual(
            reverse('strength-curve-list'),
            '/api/v1/digital-eye/nexucon-link/curves/')


class InspectorDashboardWithoutProfileTests(APITestCase):
    """An inspector with no Profile gets honest absences, not Lagos defaults."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='unprofiled@nexucon.com',
            email='unprofiled@nexucon.com',
            password='Password123!',
            first_name='',
            last_name='',
        )
        self.client.force_authenticate(self.user)

    def _dashboard(self):
        response = self.client.get(reverse('inspector-me-dashboard'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_badge_number_is_not_synthesised_from_the_user_id(self):
        profile = self._dashboard()['profile']
        self.assertIsNone(profile['badge_number'])
        # The old implementation produced "LAG-INS-XXXX". Guard against a
        # regression that reintroduces any prefixed-UUID shape.
        self.assertNotIn('LAG-INS', str(profile))

    def test_agency_district_and_role_are_none_not_defaults(self):
        profile = self._dashboard()['profile']
        # assertIsNone, not assertFalse: the contract is "not recorded", and an
        # empty string would render as a blank rather than as an absence.
        self.assertIsNone(profile['agency'])
        self.assertIsNone(profile['district'])
        self.assertIsNone(profile['role'])

    def test_name_is_none_rather_than_derived_from_the_email(self):
        """An email local-part is not a name."""
        profile = self._dashboard()['profile']
        self.assertIsNone(profile['name'])
        self.assertNotEqual(profile['name'], 'Unprofiled')

    def test_name_is_reported_when_actually_recorded(self):
        self.user.first_name = 'Amina'
        self.user.last_name = 'Bello'
        self.user.save()
        self.assertEqual(self._dashboard()['profile']['name'], 'Amina Bello')

    def test_evidence_sync_can_report_a_failure(self):
        """The sync indicator must be able to report a problem.

        It previously could not. `failed` was a literal ``0`` and
        `last_synced_at` was ``now()``, so the card read "nothing has failed,
        synced just now" on an account that had never queued anything — a
        status light wired to a constant.
        """
        SyncQueueItem.objects.create(
            inspector=self.user,
            client_item_id='failed-1',
            entity_type=SyncQueueItem.ENTITY_FINDING,
            action=SyncQueueItem.ACTION_CREATE,
            payload={'title': 'Spalling to column C-4'},
            sync_status=SyncQueueItem.STATUS_FAILED,
            retry_count=2,
            last_error='Project not found.',
        )
        sync = self._dashboard()['evidence_sync']
        self.assertEqual(sync['failed'], 1)
        # Nothing has ever promoted successfully, and the card must say that
        # rather than reporting the moment of the request.
        self.assertIsNone(sync['last_synced_at'])

    def test_evidence_sync_reports_the_most_recent_successful_sync(self):
        synced_at = timezone.now() - datetime.timedelta(hours=3)
        SyncQueueItem.objects.create(
            inspector=self.user,
            client_item_id='synced-1',
            entity_type=SyncQueueItem.ENTITY_FINDING,
            action=SyncQueueItem.ACTION_CREATE,
            payload={'title': 'Crack to beam B-2'},
            sync_status=SyncQueueItem.STATUS_SYNCED,
            synced_at=synced_at,
        )
        sync = self._dashboard()['evidence_sync']
        self.assertEqual(sync['failed'], 0)
        self.assertEqual(sync['last_synced_at'], synced_at.isoformat())

    def test_pending_evidence_counts_queued_writes(self):
        """`pending_evidence` is queue depth, not another KPI wearing its name.

        The old implementation filtered ``photos_and_evidence__isnull=False``
        against a ``JSONField(default=list)``, where SQL NULL never occurs — so
        it matched every open inspection and the figure silently duplicated the
        open-inspection count.
        """
        for index in range(3):
            SyncQueueItem.objects.create(
                inspector=self.user,
                client_item_id=f'pending-{index}',
                entity_type=SyncQueueItem.ENTITY_INSPECTION,
                action=SyncQueueItem.ACTION_UPDATE,
                payload={'status': 'IN_PROGRESS'},
            )
        data = self._dashboard()
        self.assertEqual(data['kpis']['pending_evidence'], 3)
        # This account holds no projects at all, so the figure cannot be a
        # project-derived count under a different label.
        self.assertEqual(data['kpis']['assigned_projects'], 0)

    def test_pending_evidence_is_scoped_to_the_inspector(self):
        """One inspector's queue depth must not include another's work."""
        other = User.objects.create_user(
            username='other.inspector@nexucon.com',
            email='other.inspector@nexucon.com',
            password='Password123!',
        )
        SyncQueueItem.objects.create(
            inspector=other,
            client_item_id='theirs-1',
            entity_type=SyncQueueItem.ENTITY_INSPECTION,
            action=SyncQueueItem.ACTION_UPDATE,
            payload={'status': 'IN_PROGRESS'},
        )
        self.assertEqual(self._dashboard()['kpis']['pending_evidence'], 0)


class InspectorDashboardScopedTests(APITestCase):
    """With a real Profile, the dashboard reports recorded values."""

    def setUp(self):
        self.agency = Agency.objects.create(
            name='Lagos State Building Control Agency', code='LASBCA')
        self.district = District.objects.create(
            name='Lekki-Epe Zonal Directorate', code='LEZ')
        self.role = Role.objects.create(name='Inspector', permissions=[])
        self.user = User.objects.create_user(
            username='field.inspector@nexucon.com',
            email='field.inspector@nexucon.com',
            password='Password123!',
            first_name='Tunde',
            last_name='Adeyemi',
        )
        Profile.objects.create(
            user=self.user, agency=self.agency, role=self.role,
            district=self.district,
        )
        self.client.force_authenticate(self.user)

        self.project = Project.objects.create(
            name='Lekki Phase 1 Mixed-Use',
            site_address='Plot 12, Admiralty Way, Lekki',
            district=self.district,
            status='ACTIVE',
            project_type='',  # deliberately unrecorded
        )
        self.inspection = Inspection.objects.create(
            project=self.project,
            inspection_type='Foundation Inspection',
            status='SCHEDULED',
            inspector=self.user,
            inspector_name='Tunde Adeyemi',
            scheduled_date=datetime.datetime.now(datetime.timezone.utc),
        )

    def _dashboard(self):
        response = self.client.get(reverse('inspector-me-dashboard'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_profile_reports_the_recorded_agency_role_and_district(self):
        profile = self._dashboard()['profile']
        self.assertEqual(profile['agency'], 'Lagos State Building Control Agency')
        self.assertEqual(profile['district'], 'Lekki-Epe Zonal Directorate')
        self.assertEqual(profile['role'], 'Inspector')
        self.assertEqual(profile['name'], 'Tunde Adeyemi')

    def test_badge_is_still_absent_without_a_recorded_accreditation(self):
        """A Profile is not an accreditation — the badge stays unrecorded."""
        self.assertIsNone(self._dashboard()['profile']['badge_number'])

    def test_project_type_is_none_when_not_recorded(self):
        project = self._dashboard()['assigned_projects'][0]
        self.assertIsNone(project['project_type'])
        self.assertNotEqual(project['project_type'], 'Residential')

    def test_compliance_status_is_none_without_a_certificate(self):
        """No certificate recorded means no compliance status — not "COMPLIANT"."""
        project = self._dashboard()['assigned_projects'][0]
        self.assertIsNone(project['compliance_status'])

    def test_compliance_status_reports_the_real_certificate_status(self):
        ComplianceCertificate.objects.create(
            project=self.project,
            title='Structural Fitness Certificate',
            category='Building Code',
            issue_date=datetime.date(2026, 1, 15),
            expiry_date=datetime.date(2027, 1, 14),
            status='Expired',
        )
        project = self._dashboard()['assigned_projects'][0]
        self.assertEqual(project['compliance_status'], 'Expired')

    def test_latest_certificate_wins(self):
        for issue_date, cert_status in (
            (datetime.date(2024, 1, 1), 'Expired'),
            (datetime.date(2026, 6, 1), 'Active'),
        ):
            ComplianceCertificate.objects.create(
                project=self.project,
                title='Structural Fitness Certificate',
                category='Building Code',
                issue_date=issue_date,
                expiry_date=issue_date + datetime.timedelta(days=365),
                status=cert_status,
            )
        project = self._dashboard()['assigned_projects'][0]
        self.assertEqual(project['compliance_status'], 'Active')

    def test_open_findings_do_not_manufacture_a_compliance_verdict(self):
        """The old code asserted COMPLIANT from a zero finding count."""
        project = self._dashboard()['assigned_projects'][0]
        self.assertEqual(project['open_findings'], 0)
        self.assertIsNone(project['compliance_status'])


class RecentActivityTests(APITestCase):
    """The activity feed must not invent actors or cover only 10 projects."""

    def setUp(self):
        self.role = Role.objects.create(name='Inspector', permissions=[])
        self.district = District.objects.create(name='Ikeja Zonal', code='IKJ')
        self.user = User.objects.create_user(
            username='activity@nexucon.com',
            email='activity@nexucon.com',
            password='Password123!',
            first_name='Ngozi',
            last_name='Okafor',
        )
        Profile.objects.create(
            user=self.user, role=self.role, district=self.district)
        self.client.force_authenticate(self.user)

    def _dashboard(self):
        response = self.client.get(reverse('inspector-me-dashboard'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_feed_covers_projects_beyond_the_first_ten(self):
        """The old query matched only ``projects[:10]``, hiding the rest."""
        projects = [
            Project.objects.create(
                name=f'Scoped Project {i}', district=self.district)
            for i in range(12)
        ]
        # Only the 12th project has an inspection; it must still appear.
        Inspection.objects.create(
            project=projects[11],
            inspection_type='Site Verification',
            status='SCHEDULED',
        )
        activity = self._dashboard()['recent_activity']
        self.assertTrue(activity)
        self.assertEqual(activity[0]['project'], 'Scoped Project 11')

    def test_unrecorded_actor_and_status_are_none(self):
        Inspection.objects.create(
            project=Project.objects.create(
                name='Unassigned Works', district=self.district),
            inspection_type='Safety Audit',
            status='SCHEDULED',
            inspector=None,
            inspector_name='',
        )
        entry = self._dashboard()['recent_activity'][0]
        # The old fallback credited the *viewing* user with the inspection.
        self.assertIsNone(entry['actor'])
        self.assertNotEqual(entry['actor'], 'Ngozi Okafor')


# ==========================================================================
# Inspector accreditation (Inspector PWA Module 1)
#
# The model exists to replace a fabricated badge number with a recorded one.
# The tests below therefore spend most of their effort on the ABSENT state:
# that `me/` reports 404 rather than an empty credential, and that no code
# path anywhere in the platform invents an accreditation for a user who has
# none.
# ==========================================================================

class InspectorAccreditationModelTests(TestCase):
    """`effective_status` is derived on read and never written back."""

    def setUp(self):
        self.user = User.objects.create_user(
            username='accredited@nexucon.com', email='accredited@nexucon.com',
            password='Password123!', first_name='Maryam', last_name='Bello',
        )

    def _accreditation(self, **overrides):
        fields = {
            'user': self.user,
            'badge_number': 'LAG-INS-0042',
            'full_name': 'Maryam Bello',
            'directorate': 'Lekki-Epe Zonal Directorate',
        }
        fields.update(overrides)
        return Inspector.objects.create(**fields)

    def test_a_new_accreditation_is_active_and_valid(self):
        accreditation = self._accreditation()
        self.assertEqual(accreditation.effective_status, 'ACTIVE')
        self.assertTrue(accreditation.is_valid)

    def test_an_expiry_in_the_future_stays_active(self):
        accreditation = self._accreditation(
            accreditation_expiry=timezone.localdate() + datetime.timedelta(days=30))
        self.assertEqual(accreditation.effective_status, 'ACTIVE')

    def test_an_expiry_in_the_past_reads_as_expired_without_writing_back(self):
        accreditation = self._accreditation(
            accreditation_expiry=timezone.localdate() - datetime.timedelta(days=1))
        self.assertEqual(accreditation.effective_status, 'EXPIRED')
        self.assertFalse(accreditation.is_valid)
        # The stored value is what an administrator decided and is untouched.
        accreditation.refresh_from_db()
        self.assertEqual(accreditation.accreditation_status, 'ACTIVE')

    def test_expiry_today_is_still_valid(self):
        """A badge expiring today has not lapsed yet."""
        accreditation = self._accreditation(
            accreditation_expiry=timezone.localdate())
        self.assertEqual(accreditation.effective_status, 'ACTIVE')

    def test_a_suspension_outranks_an_unexpired_date(self):
        accreditation = self._accreditation(
            accreditation_status='SUSPENDED',
            accreditation_expiry=timezone.localdate() + datetime.timedelta(days=30))
        self.assertEqual(accreditation.effective_status, 'SUSPENDED')
        self.assertFalse(accreditation.is_valid)

    def test_a_revocation_outranks_the_expiry_date(self):
        """A revoked badge is REVOKED, not EXPIRED — an administrator's
        decision is not overwritten by the calendar."""
        accreditation = self._accreditation(
            accreditation_status='REVOKED',
            accreditation_expiry=timezone.localdate() - datetime.timedelta(days=5))
        self.assertEqual(accreditation.effective_status, 'REVOKED')

    def test_no_expiry_recorded_does_not_mean_expired(self):
        accreditation = self._accreditation(accreditation_expiry=None)
        self.assertEqual(accreditation.effective_status, 'ACTIVE')

    def test_full_name_does_not_follow_later_user_edits(self):
        """A badge is issued to a name; editing the account must not rewrite
        the accredited identity on inspections already signed under it."""
        accreditation = self._accreditation()
        self.user.first_name = 'Miriam'
        self.user.last_name = 'Bello-Adeyemi'
        self.user.save()
        accreditation.refresh_from_db()
        self.assertEqual(accreditation.full_name, 'Maryam Bello')

    def test_badge_numbers_are_unique(self):
        self._accreditation()
        other = User.objects.create_user(
            username='other@nexucon.com', email='other@nexucon.com',
            password='Password123!')
        with self.assertRaises(IntegrityError):
            Inspector.objects.create(user=other, badge_number='LAG-INS-0042',
                                     full_name='Someone Else')


class InspectorAccreditationAPITests(APITestCase):
    """The HTTP contract, including the honest 404 on `me/`."""

    def setUp(self):
        self.director = User.objects.create_user(
            username='director@nexucon.com', email='director@nexucon.com',
            password='Password123!', first_name='Dee', last_name='Rector')
        self.director_role = Role.objects.create(name='Director')
        Profile.objects.create(user=self.director, role=self.director_role,
                               is_state_hq=True)

        self.inspector = User.objects.create_user(
            username='inspector@nexucon.com', email='inspector@nexucon.com',
            password='Password123!', first_name='Ngozi', last_name='Okafor')
        self.inspector_role = Role.objects.create(name='Inspector')
        Profile.objects.create(user=self.inspector, role=self.inspector_role)

        self.client.force_authenticate(self.inspector)

    def _issue(self, **overrides):
        body = {'user': str(self.inspector.id), 'badge_number': 'LAG-INS-0101',
                'full_name': 'Ngozi Okafor', 'directorate': 'Ikeja Directorate'}
        body.update(overrides)
        return self.client.post(reverse('government-inspector-list'), body, format='json')

    # ------------------------------------------------------- the absent state
    def test_me_returns_404_when_no_accreditation_is_recorded(self):
        response = self.client.get(reverse('government-inspector-me'))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(response.data['reason'], 'NOT_ACCREDITED')
        # Explicitly null, not an empty string masquerading as a badge.
        self.assertIsNone(response.data['badge_number'])
        self.assertIsNone(response.data['accreditation_status'])

    def test_me_returns_the_real_accreditation_once_issued(self):
        self.client.force_authenticate(self.director)
        self.assertEqual(self._issue().status_code, status.HTTP_201_CREATED)

        self.client.force_authenticate(self.inspector)
        response = self.client.get(reverse('government-inspector-me'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['badge_number'], 'LAG-INS-0101')
        self.assertEqual(response.data['full_name'], 'Ngozi Okafor')
        self.assertEqual(response.data['effective_status'], 'ACTIVE')
        self.assertTrue(response.data['is_valid'])

    # ------------------------------------------------------------ issuing
    def test_an_inspector_cannot_issue_an_accreditation(self):
        response = self._issue()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(Inspector.objects.count(), 0)

    def test_a_director_can_issue_an_accreditation(self):
        self.client.force_authenticate(self.director)
        response = self._issue()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # The issuer is taken from the request, never from the body.
        self.assertEqual(response.data['issued_by'], 'Dee Rector')
        self.assertIsNotNone(response.data['issued_at'])

    def test_the_issuer_cannot_be_self_declared_in_the_body(self):
        self.client.force_authenticate(self.director)
        response = self._issue(issued_by='Someone Important',
                               issued_at='2020-01-01T00:00:00Z')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['issued_by'], 'Dee Rector')
        self.assertNotEqual(response.data['issued_at'], '2020-01-01T00:00:00Z')

    def test_a_duplicate_badge_number_is_refused(self):
        self.client.force_authenticate(self.director)
        self._issue()
        other = User.objects.create_user(
            username='other_inspector@nexucon.com',
            email='other_inspector@nexucon.com', password='Password123!')
        response = self._issue(user=str(other.id))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Field errors arrive under the project-wide `errors` envelope.
        self.assertIn('badge_number', response.data['errors'])
        self.assertEqual(Inspector.objects.count(), 1)

    def test_an_already_lapsed_accreditation_cannot_be_issued_as_active(self):
        """Creating a badge that expired last year and marking it ACTIVE would
        put a false credential on record from the moment it is issued."""
        self.client.force_authenticate(self.director)
        past = (timezone.localdate() - datetime.timedelta(days=365)).isoformat()
        response = self._issue(accreditation_expiry=past)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('accreditation_expiry', response.data['errors'])
        self.assertEqual(Inspector.objects.count(), 0)

    def test_a_lapsed_accreditation_may_be_recorded_as_expired(self):
        self.client.force_authenticate(self.director)
        past = (timezone.localdate() - datetime.timedelta(days=365)).isoformat()
        response = self._issue(accreditation_expiry=past,
                               accreditation_status='EXPIRED')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['effective_status'], 'EXPIRED')

    def test_a_blank_badge_number_is_refused(self):
        self.client.force_authenticate(self.director)
        response = self._issue(badge_number='   ')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # -------------------------------------------------------------- listing
    def test_an_inspector_sees_only_their_own_accreditation(self):
        self.client.force_authenticate(self.director)
        self._issue()
        other = User.objects.create_user(
            username='colleague@nexucon.com', email='colleague@nexucon.com',
            password='Password123!')
        self._issue(user=str(other.id), badge_number='LAG-INS-0102',
                    full_name='Colleague Name')

        self.client.force_authenticate(self.inspector)
        response = self.client.get(reverse('government-inspector-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['badge_number'], 'LAG-INS-0101')

    def test_a_director_sees_every_accreditation(self):
        self.client.force_authenticate(self.director)
        self._issue()
        response = self.client.get(reverse('government-inspector-list'))
        self.assertEqual(len(response.data), 1)

    def test_an_inspector_can_read_their_own_row_but_not_a_strangers(self):
        self.client.force_authenticate(self.director)
        created = self._issue()
        self.client.force_authenticate(self.inspector)
        own = self.client.get(reverse('government-inspector-detail', kwargs={
            'inspector_id': created.data['id']}))
        self.assertEqual(own.status_code, status.HTTP_200_OK)

        other_user = User.objects.create_user(
            username='stranger@nexucon.com', email='stranger@nexucon.com',
            password='Password123!')
        stranger = Inspector.objects.create(
            user=other_user, badge_number='LAG-INS-0999', full_name='Stranger')
        foreign = self.client.get(reverse('government-inspector-detail', kwargs={
            'inspector_id': stranger.id}))
        self.assertEqual(foreign.status_code, status.HTTP_404_NOT_FOUND)

    # ------------------------------------------------------------- amending
    def test_a_director_can_suspend_an_accreditation_and_it_is_audited(self):
        self.client.force_authenticate(self.director)
        created = self._issue()
        response = self.client.patch(
            reverse('government-inspector-detail', kwargs={'inspector_id': created.data['id']}),
            {'accreditation_status': 'SUSPENDED',
             'suspension_reason': 'Pending conduct review'},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['effective_status'], 'SUSPENDED')
        self.assertFalse(response.data['is_valid'])

        from apps.audit.models import AuditEvent
        event = AuditEvent.objects.filter(
            action='government.inspector.amend').first()
        self.assertIsNotNone(event)
        self.assertIn('accreditation_status', event.metadata['changed'])

    def test_an_inspector_cannot_amend_an_accreditation(self):
        self.client.force_authenticate(self.director)
        created = self._issue()
        self.client.force_authenticate(self.inspector)
        response = self.client.patch(
            reverse('government-inspector-detail', kwargs={'inspector_id': created.data['id']}),
            {'badge_number': 'SELF-ISSUED'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class InspectorDashboardAccreditationTests(APITestCase):
    """The dashboard must report the accreditation, never invent one."""

    def setUp(self):
        self.user = User.objects.create_superuser(
            username='dashboard_inspector@nexucon.com',
            email='dashboard_inspector@nexucon.com',
            password='Password123!', first_name='Ada', last_name='Nwosu')

    def _dashboard(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(reverse('inspector-me-dashboard'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data['profile']

    def test_badge_is_null_and_status_null_when_none_is_recorded(self):
        profile = self._dashboard()
        self.assertIsNone(profile['badge_number'])
        self.assertIsNone(profile['accreditation_status'])
        self.assertEqual(profile['name'], 'Ada Nwosu')

    def test_badge_and_status_come_from_the_accreditation_once_issued(self):
        Inspector.objects.create(
            user=self.user, badge_number='LAG-INS-7777',
            full_name='Ada Nwosu', directorate='HQ Directorate')
        profile = self._dashboard()
        self.assertEqual(profile['badge_number'], 'LAG-INS-7777')
        self.assertEqual(profile['accreditation_status'], 'ACTIVE')

    def test_a_lapsed_badge_reports_as_expired_on_the_dashboard(self):
        Inspector.objects.create(
            user=self.user, badge_number='LAG-INS-8888',
            full_name='Ada Nwosu',
            accreditation_expiry=timezone.localdate() - datetime.timedelta(days=1))
        profile = self._dashboard()
        self.assertEqual(profile['accreditation_status'], 'EXPIRED')


class InspectorAccreditationRouteTests(TestCase):
    """Route names and the `me/` vs `<uuid:>` shadowing rule."""

    def test_routes_resolve(self):
        self.assertEqual(reverse('government-inspector-me'),
                         '/api/v1/government/inspectors/me/')
        self.assertEqual(reverse('government-inspector-list'),
                         '/api/v1/government/inspectors/')
        self.assertEqual(
            reverse('government-inspector-detail',
                    kwargs={'inspector_id': '11111111-1111-1111-1111-111111111111'}),
            '/api/v1/government/inspectors/11111111-1111-1111-1111-111111111111/')

    def test_me_is_a_literal_route_not_a_uuid_lookup(self):
        from django.urls import resolve
        self.assertEqual(resolve('/api/v1/government/inspectors/me/').url_name,
                         'government-inspector-me')

    def test_the_government_inspector_names_do_not_collide_with_stakeholders(self):
        """`apps.stakeholders` registers basename='inspector' on a router, which
        already claims `inspector-list` and `inspector-detail`. Django resolves
        duplicate route names last-registered-wins, so an unprefixed name here
        would silently reverse to the contractor-side inspector directory —
        which is exactly what happened before the `government-` prefix was added.
        """
        self.assertEqual(reverse('government-inspector-list'),
                         '/api/v1/government/inspectors/')
        self.assertEqual(reverse('inspector-list'),
                         '/api/v1/stakeholders/inspectors/')


class DistrictRouteTests(TestCase):
    """The zone routes, and the deliberate absence of a DELETE route."""

    def test_routes_resolve(self):
        self.assertEqual(reverse('government-district-list'),
                         '/api/v1/government/districts/')
        self.assertEqual(
            reverse('government-district-detail',
                    kwargs={'district_id': '11111111-1111-1111-1111-111111111111'}),
            '/api/v1/government/districts/11111111-1111-1111-1111-111111111111/')


class DistrictAPITests(APITestCase):
    """Operational zones: who may shape them, and what the ledger records.

    The register was previously reachable only through Django admin because
    `District` had no API at all, and the one endpoint the UI did call
    (`/evidence/hq/districts/`) is a Director-only risk heatmap that returns
    `{districts, computed_at}` — a shape the client's list `unwrap` discards, so
    the zone dropdown was empty for every user. These tests pin the real
    contract in its place.
    """

    def setUp(self):
        self.agency_head_role = Role.objects.create(name='Agency Head')
        self.director_role = Role.objects.create(name='Director')
        self.inspector_role = Role.objects.create(name='Inspector')

        self.agency_head = User.objects.create_user(
            username='head@nexucon.com', email='head@nexucon.com',
            password='Password123!', first_name='Ada', last_name='Bello')
        Profile.objects.create(user=self.agency_head, role=self.agency_head_role,
                               is_state_hq=True)

        self.director = User.objects.create_user(
            username='director@nexucon.com', email='director@nexucon.com',
            password='Password123!', first_name='Dee', last_name='Rector')
        Profile.objects.create(user=self.director, role=self.director_role,
                               is_state_hq=True)

        self.inspector = User.objects.create_user(
            username='inspector@nexucon.com', email='inspector@nexucon.com',
            password='Password123!')
        Profile.objects.create(user=self.inspector, role=self.inspector_role)

        # A user with no government Profile at all — a client-side account.
        self.outsider = User.objects.create_user(
            username='outsider@nexucon.com', email='outsider@nexucon.com',
            password='Password123!')

        self.client.force_authenticate(self.director)

    def _create(self, **overrides):
        body = {'name': 'Eti-Osa', 'code': 'Z-ETI', 'state_region': 'Lagos'}
        body.update(overrides)
        return self.client.post(reverse('government-district-list'), body,
                                format='json')

    def _audit(self):
        return AuditEvent.objects.filter(resource_type='District')

    # --------------------------------------------------- the honest empty state
    def test_the_register_is_empty_until_a_zone_is_created(self):
        response = self.client.get(reverse('government-district-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    # ------------------------------------------------------------- creating
    def test_a_director_can_create_a_zone(self):
        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['name'], 'Eti-Osa')
        self.assertEqual(response.data['code'], 'Z-ETI')
        self.assertTrue(response.data['is_active'])
        self.assertEqual(District.objects.count(), 1)

    def test_an_agency_head_can_create_a_zone(self):
        self.client.force_authenticate(self.agency_head)
        self.assertEqual(self._create().status_code, status.HTTP_201_CREATED)
        self.assertEqual(District.objects.count(), 1)

    def test_an_inspector_cannot_create_a_zone(self):
        self.client.force_authenticate(self.inspector)
        response = self._create()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(District.objects.count(), 0)

    def test_a_client_side_account_cannot_create_a_zone(self):
        self.client.force_authenticate(self.outsider)
        self.assertEqual(self._create().status_code,
                         status.HTTP_403_FORBIDDEN)
        self.assertEqual(District.objects.count(), 0)

    def test_a_zone_without_a_name_is_refused(self):
        response = self._create(name='   ')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('name', response.data['errors'])
        self.assertEqual(District.objects.count(), 0)

    def test_a_duplicate_name_is_refused(self):
        self._create()
        response = self._create(code='Z-OTHER')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('name', response.data['errors'])
        self.assertEqual(District.objects.count(), 1)

    def test_a_duplicate_code_is_refused(self):
        self._create()
        response = self._create(name='Ikeja')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('code', response.data['errors'])
        self.assertEqual(District.objects.count(), 1)

    # -------------------------------------------------------------- reading
    def test_a_non_staff_account_cannot_read_the_register(self):
        self.client.force_authenticate(self.outsider)
        response = self.client.get(reverse('government-district-list'))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_an_inspector_can_read_the_register(self):
        """The register is the zone vocabulary the dashboard displays, not a
        credential — an inspector may see which zones exist."""
        self._create()
        self.client.force_authenticate(self.inspector)
        response = self.client.get(reverse('government-district-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    def test_the_register_defaults_to_active_zones_only(self):
        self._create(name='Eti-Osa', code='Z-ETI')
        retired = self._create(name='Ikeja', code='Z-IKE').data
        District.objects.filter(pk=retired['id']).update(is_active=False)

        response = self.client.get(reverse('government-district-list'))
        self.assertEqual([z['code'] for z in response.data], ['Z-ETI'])

    def test_active_all_includes_the_retired_zones(self):
        self._create(name='Eti-Osa', code='Z-ETI')
        retired = self._create(name='Ikeja', code='Z-IKE').data
        District.objects.filter(pk=retired['id']).update(is_active=False)

        response = self.client.get(reverse('government-district-list'),
                                   {'active': 'all'})
        self.assertEqual(sorted(z['code'] for z in response.data),
                         ['Z-ETI', 'Z-IKE'])

    def test_the_counts_reported_are_the_real_ones(self):
        zone = self._create().data
        Project.objects.create(name='Lekki Phase 1', district_id=zone['id'],
                               project_type='')
        Project.objects.create(name='Ikoyi Towers', district_id=zone['id'],
                               project_type='')

        response = self.client.get(reverse('government-district-list'))
        self.assertEqual(response.data[0]['project_count'], 2)
        self.assertEqual(response.data[0]['staff_count'], 0)

    # ------------------------------------------------------------ retiring
    def test_a_zone_with_no_projects_can_be_retired(self):
        zone = self._create().data
        response = self.client.patch(
            reverse('government-district-detail', kwargs={'district_id': zone['id']}),
            {'is_active': False}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['is_active'])

    def test_retiring_a_zone_that_still_holds_projects_is_refused(self):
        zone = self._create().data
        Project.objects.create(name='Lekki Phase 1', district_id=zone['id'],
                               project_type='')

        response = self.client.patch(
            reverse('government-district-detail', kwargs={'district_id': zone['id']}),
            {'is_active': False}, format='json')
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertIn('1 project', response.data['detail'])
        # Nothing was written.
        self.assertTrue(District.objects.get(pk=zone['id']).is_active)

    def test_an_inspector_cannot_amend_a_zone(self):
        zone = self._create().data
        self.client.force_authenticate(self.inspector)
        response = self.client.patch(
            reverse('government-district-detail', kwargs={'district_id': zone['id']}),
            {'name': 'Renamed'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(District.objects.get(pk=zone['id']).name, 'Eti-Osa')

    def test_delete_is_not_routed(self):
        """`Profile.district` and `Project.district` are SET_NULL, so a hard
        delete would silently detach every project and officer scoped to the
        zone. It is not offered at all."""
        zone = self._create().data
        response = self.client.delete(
            reverse('government-district-detail', kwargs={'district_id': zone['id']}))
        self.assertEqual(response.status_code,
                         status.HTTP_405_METHOD_NOT_ALLOWED)
        self.assertEqual(District.objects.count(), 1)

    # --------------------------------------------------------------- ledger
    def test_creating_a_zone_writes_one_audit_event(self):
        zone = self._create().data
        event = self._audit().get()
        self.assertEqual(event.action, 'government.district.create')
        self.assertEqual(event.resource_id, zone['id'])
        self.assertEqual(event.metadata['code'], 'Z-ETI')

    def test_a_patch_that_changes_nothing_writes_no_audit_event(self):
        zone = self._create().data
        before = self._audit().count()
        response = self.client.patch(
            reverse('government-district-detail', kwargs={'district_id': zone['id']}),
            {'name': 'Eti-Osa'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self._audit().count(), before)

    def test_an_amend_records_the_before_and_the_after(self):
        zone = self._create().data
        self.client.patch(
            reverse('government-district-detail', kwargs={'district_id': zone['id']}),
            {'lead_officer_name': 'Engr. A. Onike'}, format='json')

        event = self._audit().get(action='government.district.amend')
        self.assertEqual(event.metadata['changed']['lead_officer_name'],
                         {'from': '', 'to': 'Engr. A. Onike'})
