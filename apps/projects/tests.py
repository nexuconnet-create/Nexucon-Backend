from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from django.contrib.auth import get_user_model
from django.core.cache import cache
from apps.projects.models import Project
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()

class ProjectAPITests(APITestCase):
    def setUp(self):
        # A superuser, because `ProjectViewSet.get_queryset` narrows an
        # ordinary authenticated user to projects carrying their own email and
        # hides the seeded mock projects from them. That narrowing is not what
        # this test is about — it is about creating a project and reading it
        # back — so it runs as the kind of caller the endpoint admits without
        # qualification. The narrowing itself is covered by
        # `ProjectVisibilityTestCase` below.
        self.user = User.objects.create_superuser(
            username='projuser', email='projuser@test.com', password='testpass')
        refresh = RefreshToken.for_user(self.user)
        self.token = str(refresh.access_token)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {self.token}')

    def test_create_and_list_project(self):
        # Create a project
        create_url = reverse('project-list')  # DRF router default name
        data = {'name': 'Demo Project', 'description': 'Test description'}
        response = self.client.post(create_url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        proj_id = response.data['id']
        # List projects
        response = self.client.get(create_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(any(p['id'] == proj_id for p in response.data))


# ======================================================================
# Who may see which projects. `ProjectViewSet.get_queryset` narrows an
# ordinary authenticated user to projects carrying their own email, and
# hides the four seeded mock projects from everyone except SiteIQ,
# government inspectors and superusers. That rule is live in production,
# and the fixtures above run as admitted callers precisely so this file
# does not have to keep re-testing it — so it is tested here instead,
# once, against the behaviour rather than the implementation.
# ======================================================================

class ProjectVisibilityTestCase(APITestCase):
    MOCK_NAME = 'Eko Atlantic Marina Towers'

    def setUp(self):
        # The list endpoint is `cache_page`d and the cache outlives a single
        # test, so a response cached by one test would answer the next. It
        # also holds the ratelimit counters, which this clears with it.
        cache.clear()
        self.ordinary = User.objects.create_user(
            username='ordinary', email='ordinary@test.com', password='testpass')
        self.siteiq = User.objects.create_user(
            username='siteiq', email='siteiq@nexucon.net', password='testpass')

    def _as(self, user):
        refresh = RefreshToken.for_user(user)
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {str(refresh.access_token)}')

    def _listed_names(self):
        res = self.client.get(reverse('project-list'))
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        return [project['name'] for project in res.data]

    def test_an_ordinary_user_does_not_see_a_project_that_is_not_theirs(self):
        Project.objects.create(name='Someone Elses Tower')
        self._as(self.ordinary)
        self.assertNotIn('Someone Elses Tower', self._listed_names())

    def test_an_ordinary_user_sees_the_project_carrying_their_email(self):
        Project.objects.create(name='My Own Tower',
                               developer_email='ordinary@test.com')
        self._as(self.ordinary)
        self.assertIn('My Own Tower', self._listed_names())

    def test_the_seeded_mock_projects_are_hidden_from_an_ordinary_user(self):
        # Carries their email, so only the mock-name exclusion can hide it.
        Project.objects.create(name=self.MOCK_NAME,
                               developer_email='ordinary@test.com')
        self._as(self.ordinary)
        self.assertNotIn(self.MOCK_NAME, self._listed_names())

    def test_siteiq_sees_the_seeded_mock_projects(self):
        Project.objects.create(name=self.MOCK_NAME)
        self._as(self.siteiq)
        self.assertIn(self.MOCK_NAME, self._listed_names())

    def test_the_list_response_varies_on_authorization(self):
        """Otherwise the cached list is served across users.

        The list is cached for fifteen minutes and the body differs per
        caller, so without this the first caller to miss the cache would
        answer everyone else with their own project list.
        """
        self._as(self.ordinary)
        res = self.client.get(reverse('project-list'))
        self.assertIn('Authorization', res.headers.get('Vary', ''))


# ======================================================================
# Cold storage (8 Sep 2026 review meeting): projects inactive for 3-6
# months drop out of the hot browse lists. Nothing is ever deleted — the
# flag only changes default visibility, and a Director can restore.
# ======================================================================

import datetime

from django.utils import timezone

from apps.audit.models import AuditEvent
from apps.digital_eye.models import PUNDITTest
from apps.projects.tasks import cold_store_inactive_projects


class ColdStoragePolicyTestCase(APITestCase):
    def setUp(self):
        # The ProjectViewSet list/retrieve actions are cache_page(15 min)
        # wrapped — clear it so a previous test's cached page never leaks
        # into these assertions.
        from django.core.cache import cache
        cache.clear()
        self.director = User.objects.create_superuser(
            username='cs_director@nexucon.com',
            email='cs_director@nexucon.com', password='Password123!')
        refresh = RefreshToken.for_user(self.director)
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        self.hot = Project.objects.create(
            name='Active Lekki Site', project_type='Commercial',
            status='ACTIVE')
        self.stale = Project.objects.create(
            name='Long-Dormant Site', project_type='Commercial',
            status='ACTIVE')

    def _age_project(self, project, months):
        """auto_now/auto_now_add cannot be set through save() — the
        update() queryset path writes both timestamps verbatim, as they
        would be on a project that has truly sat idle for months."""
        aged = timezone.now() - datetime.timedelta(days=30 * months + 5)
        Project.objects.filter(pk=project.pk).update(
            created_at=aged, updated_at=aged)

    def test_last_activity_considers_newest_related_record(self):
        # The project row is old, but a PUNDIT test was recorded minutes
        # ago — the project is NOT inactive.
        self._age_project(self.stale, 8)
        PUNDITTest.objects.create(
            project=self.stale, test_type='pulse_velocity',
            structural_element='COL-A1', floor='Ground Floor',
            path_length_mm=120.0, pulse_time_us=30.0)
        self.assertLess(
            self.stale.last_activity_at(),
            timezone.now() + datetime.timedelta(minutes=1))
        self.assertGreater(
            self.stale.last_activity_at(),
            timezone.now() - datetime.timedelta(minutes=5))

    def test_policy_flags_only_long_inactive_projects(self):
        self._age_project(self.stale, 8)
        flagged = cold_store_inactive_projects(min_inactive_months=6)
        self.assertEqual([p.pk for p in flagged], [self.stale.pk])
        self.stale.refresh_from_db()
        self.hot.refresh_from_db()
        self.assertTrue(self.stale.cold_storage)
        self.assertIsNotNone(self.stale.cold_stored_at)
        self.assertFalse(self.hot.cold_storage)
        # Every transition is audited.
        self.assertTrue(AuditEvent.objects.filter(
            resource_id=str(self.stale.pk),
            action='projects.project.cold_storage').exists())
        # A second pass never touches the already-cold project again.
        self.assertEqual(cold_store_inactive_projects(
            min_inactive_months=6), [])
        self.assertEqual(AuditEvent.objects.filter(
            resource_id=str(self.stale.pk),
            action='projects.project.cold_storage').count(), 1)

    def test_dry_run_changes_nothing(self):
        self._age_project(self.stale, 8)
        would = cold_store_inactive_projects(min_inactive_months=6,
                                             dry_run=True)
        self.assertEqual([p.pk for p in would], [self.stale.pk])
        self.stale.refresh_from_db()
        self.assertFalse(self.stale.cold_storage)

    def test_three_month_threshold_matches_meeting_guidance(self):
        self._age_project(self.stale, 4)  # 4 months — inside the 3-6 band
        flagged = cold_store_inactive_projects(min_inactive_months=3)
        self.assertEqual([p.pk for p in flagged], [self.stale.pk])

    def test_cold_projects_leave_the_default_list_but_stay_reachable(self):
        self.stale.cold_storage = True
        self.stale.save()
        # Default list: hot only.
        response = self.client.get(reverse('project-list'))
        ids = [row['id'] for row in response.data]
        self.assertIn(str(self.hot.id), ids)
        self.assertNotIn(str(self.stale.id), ids)
        # ?include_cold=true: everything.
        response = self.client.get(reverse('project-list'),
                                   {'include_cold': 'true'})
        ids = [row['id'] for row in response.data]
        self.assertIn(str(self.stale.id), ids)
        # ?storage=cold: only the cold one.
        response = self.client.get(reverse('project-list'),
                                   {'storage': 'cold'})
        self.assertEqual([row['id'] for row in response.data],
                         [str(self.stale.id)])
        # Direct detail access still works — records are never hidden.
        response = self.client.get(
            reverse('project-detail', args=[self.stale.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['cold_storage'])

    def test_director_restores_a_cold_project(self):
        self.stale.cold_storage = True
        self.stale.save()
        response = self.client.post(
            reverse('project-restore-from-cold-storage',
                    args=[self.stale.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK,
                         msg=str(response.data))
        self.stale.refresh_from_db()
        self.assertFalse(self.stale.cold_storage)
        self.assertIsNone(self.stale.cold_stored_at)
        # Back in the default list.
        response = self.client.get(reverse('project-list'))
        self.assertIn(str(self.stale.id),
                      [row['id'] for row in response.data])
        # Restoring a hot project is a 400, not a silent no-op.
        response = self.client.post(
            reverse('project-restore-from-cold-storage',
                    args=[self.hot.id]))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_restore_requires_director_role(self):
        staff = User.objects.create_user(
            username='cs_staff@nexucon.com', email='cs_staff@nexucon.com',
            password='Password123!')
        refresh = RefreshToken.for_user(staff)
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        self.stale.cold_storage = True
        self.stale.save()
        response = self.client.post(
            reverse('project-restore-from-cold-storage',
                    args=[self.stale.id]))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


# ======================================================================
# E3 (4 Sep 2026 review meeting): role reconciliation — "Agency Head" is a
# first-class government role (the one every government onboarding
# creates), so common.permissions must know it: full project visibility,
# mirroring apps.government.permissions where Agency Head >= Director.
# ======================================================================

from django.test import TestCase  # noqa: E402


class AgencyHeadScopeTestCase(TestCase):
    """scoped_projects under the reconciled role model."""

    def test_agency_head_sees_all_projects(self):
        from apps.government.models import Agency, Profile, Role
        from common.permissions import (GOVERNMENT_ROLES, scoped_projects,
                                        user_is_agency_head)

        agency = Agency.objects.create(
            name='Lagos State Building Control Agency', code='LASBCA')
        head_role = Role.objects.create(name='Agency Head')
        head = User.objects.create_user(
            username='head@lasbca.gov', email='head@lasbca.gov',
            password='Password123!')
        Profile.objects.create(user=head, agency=agency, role=head_role)

        p1 = Project.objects.create(
            name='Ikoyi Tower', project_type='Commercial', status='ACTIVE')
        p2 = Project.objects.create(
            name='Epe Roadworks', project_type='Infrastructure',
            status='PLANNING')

        self.assertIn('Agency Head', GOVERNMENT_ROLES)
        self.assertTrue(user_is_agency_head(head))
        scoped = scoped_projects(head)
        self.assertIn(p1, scoped)
        self.assertIn(p2, scoped)

    def test_roleless_districtless_staff_still_sees_nothing(self):
        """The reconciliation must not widen anyone else's scope."""
        from apps.government.models import Profile
        from common.permissions import scoped_projects, user_is_agency_head

        staff = User.objects.create_user(
            username='staff@lasbca.gov', email='staff@lasbca.gov',
            password='Password123!')
        Profile.objects.create(user=staff)  # no role, no district
        Project.objects.create(
            name='Somewhere Estate', project_type='Residential',
            status='ACTIVE')

        self.assertFalse(user_is_agency_head(staff))
        self.assertEqual(scoped_projects(staff).count(), 0)


# ======================================================================
# Step 7 — the `assigned_inspector` escalation: foreign key, backfill, and
# the audit/backfill commands.
# ======================================================================

import logging  # noqa: E402
from io import StringIO  # noqa: E402
from unittest import mock  # noqa: E402

from django.core.management import call_command  # noqa: E402
from django.db.models import Q  # noqa: E402

from apps.projects.models import resolve_inspector_user  # noqa: E402


class _InspectorFixtureMixin:
    """An Inspector is a user holding the Inspector role on a Profile."""

    def make_inspector(self, email, first_name, last_name, district=None):
        from apps.government.models import Profile, Role

        role, _ = Role.objects.get_or_create(name='Inspector')
        user = User.objects.create_user(
            username=email, email=email, password='Password123!',
            first_name=first_name, last_name=last_name)
        Profile.objects.create(user=user, role=role, district=district)
        return user

    def make_project(self, name, assigned_inspector=None):
        return Project.objects.create(
            name=name, project_type='Commercial', status='ACTIVE',
            assigned_inspector=assigned_inspector)

    def legacy_text(self, project, text):
        """Write the text without going through `save()`.

        This is what a row written before the foreign key existed looks like:
        the text is populated and the key is not. A queryset `update()` is used
        precisely *because* the model's `save()` would resolve it.
        """
        Project.objects.filter(pk=project.pk).update(assigned_inspector=text)
        return Project.objects.get(pk=project.pk)


class AssignedInspectorResolutionTestCase(_InspectorFixtureMixin, TestCase):
    """`resolve_inspector_user` and the save-time mirror."""

    def test_unique_name_resolves_to_that_user(self):
        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        resolved, candidates = resolve_inspector_user('Bello Wahab')
        self.assertEqual(resolved, user)
        self.assertEqual(candidates, 1)

    def test_email_resolves_when_the_text_is_an_email(self):
        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        resolved, candidates = resolve_inspector_user('bello@lasbca.gov')
        self.assertEqual(resolved, user)
        self.assertEqual(candidates, 1)

    def test_a_shared_name_resolves_to_nobody(self):
        self.make_inspector('a@lasbca.gov', 'John', 'Doe')
        self.make_inspector('b@lasbca.gov', 'John', 'Doe')
        resolved, candidates = resolve_inspector_user('John Doe')
        self.assertIsNone(resolved)
        self.assertEqual(candidates, 2)

    def test_an_unknown_name_resolves_to_nobody(self):
        resolved, candidates = resolve_inspector_user('Nobody At All')
        self.assertIsNone(resolved)
        self.assertEqual(candidates, 0)

    def test_an_inactive_user_is_not_a_candidate(self):
        user = self.make_inspector('gone@lasbca.gov', 'Gone', 'Away')
        user.is_active = False
        user.save(update_fields=['is_active'])
        resolved, candidates = resolve_inspector_user('Gone Away')
        self.assertIsNone(resolved)
        self.assertEqual(candidates, 0)

    def test_blank_text_resolves_to_nobody_without_a_user_query(self):
        with mock.patch(
                'apps.projects.models.resolve_inspector_user') as resolver:
            self.assertEqual(resolve_inspector_user('   '), (None, 0))
        resolver.assert_not_called()

    def test_creating_with_a_name_populates_the_key(self):
        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        project = self.make_project('Ikoyi Tower', 'Bello Wahab')
        project.refresh_from_db()
        self.assertEqual(project.assigned_inspector_user_id, user.id)

    def test_saving_the_text_alone_still_writes_the_key(self):
        """The defect this guards: `update_fields` dropping the resolved FK.

        `apps/settings/services.py` assigns an inspector with
        `save(update_fields=['assigned_inspector'])`. If resolution wrote only
        the fields the caller named, that write would be silently discarded and
        the assignment would stop granting access.
        """
        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        project = self.make_project('Ikoyi Tower')
        project.assigned_inspector = 'Bello Wahab'
        project.save(update_fields=['assigned_inspector'])
        project.refresh_from_db()
        self.assertEqual(project.assigned_inspector_user_id, user.id)

    def test_saving_the_key_alone_rewrites_the_text(self):
        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        project = self.make_project('Ikoyi Tower')
        project.assigned_inspector_user = user
        project.save(update_fields=['assigned_inspector_user'])
        project.refresh_from_db()
        self.assertEqual(project.assigned_inspector, 'Bello Wahab')

    def test_a_save_that_touches_neither_field_does_not_resolve(self):
        self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        project = self.legacy_text(self.make_project('Ikoyi Tower'),
                                   'Bello Wahab')
        project.status = 'COMPLETED'
        with mock.patch(
                'apps.projects.models.resolve_inspector_user') as resolver:
            project.save(update_fields=['status'])
        resolver.assert_not_called()
        project.refresh_from_db()
        self.assertIsNone(project.assigned_inspector_user_id)

    def test_an_ambiguous_name_is_refused_and_logged(self):
        self.make_inspector('a@lasbca.gov', 'John', 'Doe')
        self.make_inspector('b@lasbca.gov', 'John', 'Doe')
        with self.assertLogs('apps.projects.models',
                             level=logging.WARNING) as logs:
            project = self.make_project('Ikoyi Tower', 'John Doe')
        project.refresh_from_db()
        self.assertIsNone(project.assigned_inspector_user_id)
        logged = '\n'.join(logs.output)
        self.assertIn('matches 2 active users', logged)
        self.assertIn('no assignment made', logged)

    def test_a_name_matching_nobody_says_so_rather_than_calling_it_ambiguous(self):
        """Zero matches and several matches are different problems.

        Reporting "ambiguous" for a name nobody holds would send whoever reads
        the log looking for a second user who does not exist.
        """
        with self.assertLogs('apps.projects.models',
                             level=logging.WARNING) as logs:
            project = self.make_project('Ikoyi Tower', 'Nobody At All')
        project.refresh_from_db()
        self.assertIsNone(project.assigned_inspector_user_id)
        logged = '\n'.join(logs.output)
        self.assertIn('matches no active user', logged)
        self.assertNotIn('ambiguous', logged)


class AssignedInspectorScopeTestCase(_InspectorFixtureMixin, TestCase):
    """The escalation itself, and the parity the switch had to preserve."""

    def test_a_unique_name_grants_exactly_what_the_old_match_granted(self):
        """Parity: on unambiguous data the two rules reach the same projects.

        The old rule is reproduced literally — `Q(assigned_inspector=<name>)` —
        and compared against `scoped_projects`, which now tests the key. If the
        switch had narrowed or widened access for clean data, these sets would
        differ.
        """
        from common.permissions import scoped_projects

        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        mine = self.make_project('Ikoyi Tower', 'Bello Wahab')
        self.make_project('Epe Roadworks', 'Someone Else')

        old_rule = set(Project.objects.filter(
            Q(assigned_inspector=str(user.get_full_name() or user.email))
        ).values_list('id', flat=True))
        new_rule = set(scoped_projects(user).values_list('id', flat=True))

        self.assertEqual(old_rule, {mine.id})
        self.assertEqual(old_rule, new_rule)

    def test_a_shared_name_grants_neither_user_the_project(self):
        from common.permissions import scoped_projects

        first = self.make_inspector('a@lasbca.gov', 'John', 'Doe')
        second = self.make_inspector('b@lasbca.gov', 'John', 'Doe')
        shared = self.make_project('Ikoyi Tower', 'John Doe')

        # The old rule showed this project to both users, because it matched the
        # string. That is the escalation.
        old_rule = set(Project.objects.filter(
            Q(assigned_inspector='John Doe')).values_list('id', flat=True))
        self.assertEqual(old_rule, {shared.id})

        self.assertNotIn(shared, scoped_projects(first))
        self.assertNotIn(shared, scoped_projects(second))

    def test_renaming_a_profile_does_not_change_what_they_can_reach(self):
        from common.permissions import scoped_projects

        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        project = self.make_project('Ikoyi Tower')
        project.assigned_inspector_user = user
        project.save(update_fields=['assigned_inspector_user'])
        self.assertIn(project, scoped_projects(user))

        user.first_name = 'Bello-Osagie'
        user.save(update_fields=['first_name'])

        self.assertIn(project, scoped_projects(user))

    def test_typing_a_name_that_resolves_to_nobody_grants_nothing(self):
        """The write path an authenticated user could reach.

        `ProjectSerializer` exposes `assigned_inspector` (`fields = '__all__'`),
        so the text is writable. The unambiguous case still hands the project
        over — that is `test_a_unique_name_grants_exactly_what_the_old_match_
        granted`. This is the other half: a name that resolves to nobody.
        """
        from common.permissions import scoped_projects

        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        project = self.make_project('Ikoyi Tower', 'Impostor Name')
        project.refresh_from_db()

        self.assertIsNone(project.assigned_inspector_user_id)
        self.assertNotIn(project, scoped_projects(user))

    def test_a_legacy_row_grants_nothing_until_the_backfill_runs(self):
        from common.permissions import scoped_projects

        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        project = self.legacy_text(self.make_project('Ikoyi Tower'),
                                   'Bello Wahab')

        self.assertIsNone(project.assigned_inspector_user_id)
        self.assertNotIn(project, scoped_projects(user))

        call_command('backfill_project_assigned_inspector_user', '--execute',
                     stdout=StringIO())

        project.refresh_from_db()
        self.assertEqual(project.assigned_inspector_user_id, user.id)
        self.assertIn(project, scoped_projects(user))

    def test_an_inspection_still_grants_the_project(self):
        from apps.inspections.models import Inspection
        from common.permissions import scoped_projects

        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        project = self.make_project('Ikoyi Tower')
        Inspection.objects.create(project=project, inspector=user,
                                  inspection_type='ROUTINE')
        self.assertIn(project, scoped_projects(user))

    def test_matching_the_text_alone_is_not_a_fallback(self):
        """The mirror is deliberately not matched, even as a backstop.

        A row whose text matches a user but whose key is empty is a row the
        backfill has not reached. Falling back to the text for those rows would
        restore the escalation for exactly the rows an attacker would create.
        """
        from common.permissions import scoped_projects

        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        project = self.legacy_text(self.make_project('Ikoyi Tower'),
                                   'Bello Wahab')
        self.assertEqual(project.assigned_inspector, 'Bello Wahab')
        self.assertNotIn(project, scoped_projects(user))


class AssignableProjectsEndpointTestCase(_InspectorFixtureMixin, APITestCase):
    """`GET /projects/assignable/` — the set a write will actually accept.

    The defect this closes: the registry *browse* (`/projects/`) is
    `IsAuthenticatedOrReadOnly` and unscoped on purpose, so a picker fed from
    it offered every project on the platform while the write behind it
    resolved through `scoped_projects(user)`. An officer choosing a project
    they had no standing on was answered *Invalid pk … does not exist* — a true
    statement about a set they were never shown, and one that reads as the
    platform being broken.
    """

    def setUp(self):
        super().setUp()
        self.inspector = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        self.mine = self.make_project('Ikoyi Tower', 'Bello Wahab')

        self.other = self.make_inspector('ade@lasbca.gov', 'Ade', 'Okoro')
        self.theirs = self.make_project('Victoria Island Plaza', 'Ade Okoro')

        refresh = RefreshToken.for_user(self.inspector)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')

    def _assignable_ids(self):
        response = self.client.get(reverse('project-assignable'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row['id'] for row in response.data]

    def _browse_ids(self):
        response = self.client.get(reverse('project-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return [row['id'] for row in response.data]

    def test_it_returns_the_projects_in_the_callers_scope(self):
        ids = self._assignable_ids()
        self.assertIn(str(self.mine.id), ids)
        self.assertNotIn(str(self.theirs.id), ids)

    def test_the_browse_list_is_untouched(self):
        """The narrowing is confined to the new route.

        `/projects/` is a public registry browse and several screens rely on
        it being unscoped. If a later edit "fixes" that viewset by scoping
        `get_queryset`, this fails rather than those screens quietly emptying.
        """
        browse = self._browse_ids()
        self.assertIn(str(self.mine.id), browse)
        self.assertIn(str(self.theirs.id), browse)

    def test_two_officers_are_not_served_each_others_answer(self):
        """The reason this route is not decorated with `cache_page`.

        `ProjectViewSet.list` is cached, and `cache_page` keys on the URL
        alone — it does not vary on the Authorization header. The cache
        backend is the default per-process LocMemCache, so a cached
        user-scoped response would be handed to the next caller. Two officers
        asking the same URL in the same process must still get their own sets.
        """
        mine = self._assignable_ids()

        refresh = RefreshToken.for_user(self.other)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        theirs = self._assignable_ids()

        self.assertNotEqual(mine, theirs)
        self.assertNotIn(str(self.theirs.id), mine)
        self.assertNotIn(str(self.mine.id), theirs)

    def test_a_caller_with_no_scope_is_told_nothing_is_assignable(self):
        """An empty list is the honest answer, not a failure.

        A user with no government profile has no scoped projects at all. The
        picker must show nothing and say so, rather than offer the platform's
        whole registry and fail on every choice.
        """
        nobody = User.objects.create_user(
            username='stranger@example.com',
            email='stranger@example.com',
            password='Password123!',
        )
        refresh = RefreshToken.for_user(nobody)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')

        self.assertEqual(self._assignable_ids(), [])


class BackfillCommandTestCase(_InspectorFixtureMixin, TestCase):
    """`backfill_project_assigned_inspector_user`."""

    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        call_command('backfill_project_assigned_inspector_user', *args,
                     stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_dry_run_writes_nothing(self):
        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        project = self.legacy_text(self.make_project('Ikoyi Tower'),
                                   'Bello Wahab')

        out, _ = self.run_command()

        self.assertIn('Nothing was written', out)
        project.refresh_from_db()
        self.assertIsNone(project.assigned_inspector_user_id)
        self.assertEqual(
            Project.objects.filter(assigned_inspector_user=user).count(), 0)

    def test_execute_assigns_and_reports_clear(self):
        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        project = self.legacy_text(self.make_project('Ikoyi Tower'),
                                   'Bello Wahab')

        out, _ = self.run_command('--execute')

        self.assertIn('CLEAR', out)
        self.assertIn('Assigned 1 project(s).', out)
        project.refresh_from_db()
        self.assertEqual(project.assigned_inspector_user_id, user.id)

    def test_an_ambiguous_row_is_reported_and_left_unassigned(self):
        self.make_inspector('a@lasbca.gov', 'John', 'Doe')
        self.make_inspector('b@lasbca.gov', 'John', 'Doe')
        project = self.legacy_text(self.make_project('Ikoyi Tower'), 'John Doe')

        out, _ = self.run_command('--execute')

        self.assertIn('Names matching more than one active user', out)
        self.assertIn('REVIEW', out)
        project.refresh_from_db()
        self.assertIsNone(project.assigned_inspector_user_id)

    def test_it_refuses_to_write_while_a_user_would_lose_projects(self):
        """`--execute` alone is not enough when parity fails.

        The only way a user loses a project here is a shared name: the old rule
        gave both holders the row and the key can hold one. That is access the
        switch removes, so the command stops and says so.
        """
        self.make_inspector('a@lasbca.gov', 'John', 'Doe')
        self.make_inspector('b@lasbca.gov', 'John', 'Doe')
        project = self.legacy_text(self.make_project('Ikoyi Tower'), 'John Doe')

        out, err = self.run_command('--execute')

        self.assertIn('Users losing projects:', out)
        self.assertIn('Refusing to write', err)
        project.refresh_from_db()
        self.assertIsNone(project.assigned_inspector_user_id)

    def test_force_writes_but_cannot_manufacture_an_assignment(self):
        """`--force` overrides the refusal to write, not the resolution rule."""
        self.make_inspector('a@lasbca.gov', 'John', 'Doe')
        self.make_inspector('b@lasbca.gov', 'John', 'Doe')
        project = self.legacy_text(self.make_project('Ikoyi Tower'), 'John Doe')

        out, err = self.run_command('--execute', '--force')

        self.assertIn('Assigned 0 project(s).', out)
        self.assertNotIn('Refusing to write', err)
        project.refresh_from_db()
        self.assertIsNone(project.assigned_inspector_user_id)

    def test_an_already_assigned_project_is_skipped(self):
        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        project = self.make_project('Ikoyi Tower', 'Bello Wahab')
        project.refresh_from_db()
        self.assertEqual(project.assigned_inspector_user_id, user.id)

        out, _ = self.run_command('--execute')

        self.assertIn('already assigned (skipped): 1', out)


class ScopeEscalationAuditCommandTestCase(_InspectorFixtureMixin, TestCase):
    """`audit_project_scope_escalation` — read-only by construction."""

    def run_command(self, *args):
        out, err = StringIO(), StringIO()
        call_command('audit_project_scope_escalation', *args,
                     stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_execute_is_refused_and_names_the_writing_command(self):
        _, err = self.run_command('--execute')
        self.assertIn('read-only', err)
        self.assertIn('backfill_project_assigned_inspector_user', err)

    def test_it_reports_clear_when_the_text_grants_nothing_extra(self):
        user = self.make_inspector('bello@lasbca.gov', 'Bello', 'Wahab')
        project = self.make_project('Ikoyi Tower', 'Bello Wahab')
        project.refresh_from_db()
        self.assertEqual(project.assigned_inspector_user_id, user.id)

        out, _ = self.run_command()
        self.assertIn('CLEAR', out)
        self.assertIn('Nothing was written', out)

    def test_it_names_the_text_that_resolves_to_nobody(self):
        self.make_project('Ikoyi Tower', 'Ghost Inspector')
        out, _ = self.run_command()
        self.assertIn('Assignments resolving to no active user', out)
        self.assertIn('Ghost Inspector', out)

    def test_it_reports_a_shared_name_as_a_stop(self):
        self.make_inspector('a@lasbca.gov', 'John', 'Doe')
        self.make_inspector('b@lasbca.gov', 'John', 'Doe')
        self.legacy_text(self.make_project('Ikoyi Tower'), 'John Doe')

        out, _ = self.run_command('--all-roles')
        self.assertIn('STOP', out)
        self.assertIn("'John Doe'", out)

    def test_the_default_view_is_limited_to_inspectors(self):
        """A Director named in a project field is not this audit's subject."""
        from apps.government.models import Profile, Role

        role, _ = Role.objects.get_or_create(name='Director')
        director = User.objects.create_user(
            username='dir@lasbca.gov', email='dir@lasbca.gov',
            password='Password123!', first_name='Ada', last_name='Nwosu')
        Profile.objects.create(user=director, role=role)
        self.make_project('Ikoyi Tower', 'Ada Nwosu')

        out, _ = self.run_command()
        self.assertIn('Users in scope: 0', out)

        out_all, _ = self.run_command('--all-roles')
        self.assertIn('Users in scope: 1', out_all)


# ======================================================================
# Operational zone on a project (18 Sep 2026). `Project.district` is the FK
# that scopes which officers can see a project and which district it appears
# under on the HQ heatmap. It was always writable through the API — but no
# frontend ever sent it: the register-project form wrote a free-text
# "LGA / District" into `Project.lga`, so no project ever carried a zone.
# `district_name` is new, so a consumer never has to resolve the FK itself
# (and so a project on a retired zone still names it).
# ======================================================================

class ProjectZoneAssignmentTestCase(APITestCase):
    def setUp(self):
        from apps.government.models import District

        self.user = User.objects.create_superuser(
            username='zoneuser', email='zoneuser@test.com', password='testpass')
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {str(refresh.access_token)}')

        self.zone = District.objects.create(
            name='Eti-Osa', code='Z-ETI', state_region='Lagos')
        self.other_zone = District.objects.create(
            name='Ikeja', code='Z-IKJ', state_region='Lagos')

    def test_a_new_project_can_be_registered_into_a_zone(self):
        res = self.client.post(reverse('project-list'), {
            'name': 'Zone Scoped Tower', 'district': str(self.zone.id),
        }, format='json')
        self.assertEqual(res.status_code, status.HTTP_201_CREATED, res.data)
        self.assertEqual(res.data['district'], self.zone.id)
        self.assertEqual(res.data['district_name'], 'Eti-Osa')

    def test_a_project_can_be_moved_between_zones(self):
        project = Project.objects.create(name='Movable', district=self.zone)
        res = self.client.patch(
            reverse('project-detail', args=[project.id]),
            {'district': str(self.other_zone.id)}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        project.refresh_from_db()
        self.assertEqual(project.district_id, self.other_zone.id)
        self.assertEqual(res.data['district_name'], 'Ikeja')

    def test_a_project_can_be_taken_out_of_its_zone(self):
        project = Project.objects.create(name='Unassignable', district=self.zone)
        res = self.client.patch(
            reverse('project-detail', args=[project.id]),
            {'district': None}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.data)
        project.refresh_from_db()
        self.assertIsNone(project.district_id)
        self.assertIsNone(res.data['district_name'])

    def test_an_unzoned_project_reports_no_zone(self):
        project = Project.objects.create(name='Never Zoned')
        res = self.client.get(reverse('project-detail', args=[project.id]))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIsNone(res.data['district'])
        self.assertIsNone(res.data['district_name'])

    def test_a_retired_zone_is_still_named_on_its_projects(self):
        """Retiring a zone must not make its projects read as unassigned."""
        self.zone.is_active = False
        self.zone.save(update_fields=['is_active'])
        project = Project.objects.create(name='On Retired Zone', district=self.zone)

        res = self.client.get(reverse('project-detail', args=[project.id]))
        self.assertEqual(res.data['district'], self.zone.id)
        self.assertEqual(res.data['district_name'], 'Eti-Osa')
