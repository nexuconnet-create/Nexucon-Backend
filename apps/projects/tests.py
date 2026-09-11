from django.urls import reverse
from rest_framework.test import APITestCase
from rest_framework import status
from django.contrib.auth import get_user_model
from apps.projects.models import Project
from rest_framework_simplejwt.tokens import RefreshToken

User = get_user_model()

class ProjectAPITests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='projuser', email='projuser@test.com', password='testpass')
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
