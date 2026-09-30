"""
PUNDIT Integration & Report Generation Workflow Tests.
Governing Review Items (30 Sep 2026):
  1. Data Workflow Overhaul: Project-specific scan folders (batches) preventing 59 vs 48 data clashes.
  2. Critical Calibration Step: Pre-analysis model calibration profile.
  3. Exponential Default Model: Non-linear exponential acoustic-strength physics (f_cu = a * exp(b * V) + c).
  4. Inspector Dashboard Enhancements: Visual defect observations with photos and site attendance register.
"""
import uuid
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.projects.models import Project
from apps.digital_eye.models import (
    CalibrationProfile,
    PunditScanBatch,
    PUNDITTest,
    SiteAttendanceRecord,
    VisualObservation,
)
from apps.digital_eye.adapters import PUNDITAdapter

User = get_user_model()


class PunditWorkflowOverhaulTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            email='lead.structural@nexucon.gov.ng',
            username='lead_structural',
            password='TestPassword123!',
            first_name='Engr.',
            last_name='Adeleke',
        )
        self.project = Project.objects.create(
            name='Dangote Refinery Admin Complex',
            project_type='Industrial',
            status='ACTIVE',
        )

    def test_exponential_calibration_curve_benchmarks(self):
        """
        Validate the exponential correlation model:
            f_cu = a * exp(b * V_km/s) + c
        Standard baseline: a = 1.20, b = 0.85, c = 0.0
        """
        curve_profile = {
            'curve_type': 'exponential',
            'params': {'a': 1.20, 'b': 0.85, 'c': 0.0},
            'design_strength_mpa': 25.0,
        }

        # 2,500 m/s (2.5 km/s) -> 10.05 MPa (POOR / FAIL)
        f_2500 = PUNDITAdapter.compute_profile_ecs(2.5, curve_profile)
        self.assertAlmostEqual(f_2500, 10.05, delta=0.2)

        # 3,000 m/s (3.0 km/s) -> 15.37 MPa (DOUBTFUL / FAIL)
        f_3000 = PUNDITAdapter.compute_profile_ecs(3.0, curve_profile)
        self.assertAlmostEqual(f_3000, 15.37, delta=0.2)

        # 3,750 m/s (3.75 km/s) -> 29.07 MPa (GOOD / PASS >= 25 MPa)
        f_3750 = PUNDITAdapter.compute_profile_ecs(3.75, curve_profile)
        self.assertAlmostEqual(f_3750, 29.07, delta=0.2)

        # 4,000 m/s (4.0 km/s) -> 35.96 MPa (HIGH STRENGTH / PASS)
        f_4000 = PUNDITAdapter.compute_profile_ecs(4.0, curve_profile)
        self.assertAlmostEqual(f_4000, 35.96, delta=0.2)

        # 4,500 m/s (4.5 km/s) -> 55.00 MPa (EXCELLENT / PASS)
        f_4500 = PUNDITAdapter.compute_profile_ecs(4.5, curve_profile)
        self.assertAlmostEqual(f_4500, 55.00, delta=0.5)

        # Verify non-linear acceleration:
        # Delta 1: from 2.5 to 3.0 (0.5 km/s diff) -> 5.32 MPa gain
        # Delta 2: from 3.5 to 4.0 (0.5 km/s diff) -> ~12.3 MPa gain
        delta_low = f_3000 - f_2500
        f_3500 = PUNDITAdapter.compute_profile_ecs(3.5, curve_profile)
        delta_high = f_4000 - f_3500
        self.assertGreater(delta_high, delta_low * 2.0, "Exponential curve must accelerate non-linearly")

    def test_batch_isolation_prevents_element_count_clash(self):
        """
        Verify that 48 elements in Batch A and 11 elements in Batch B
        do NOT merge into 59 elements when scoped to a batch.
        """
        batch_a = PunditScanBatch.objects.create(
            project=self.project,
            folder_name='Floor 2 RC Slab - Primary Grid (48 Elements)',
            batch_reference='BATCH-2026-09-048',
            element_count=48,
            inspector=self.user,
            status='RAW_INGESTED',
        )

        for i in range(48):
            PUNDITTest.objects.create(
                id=f'TEST-A-{i:03d}',
                project=self.project,
                batch=batch_a,
                structural_element=f'SLAB-P{i+1:02d}',
                floor='Floor 2',
                path_length_mm=250.0,
                pulse_time_us=65.0,  # ~3.84 km/s
                velocity_km_s=3.84,
                created_by=self.user,
            )

        batch_b = PunditScanBatch.objects.create(
            project=self.project,
            folder_name='Floor 2 Edge Beams Re-test (11 Elements)',
            batch_reference='BATCH-2026-09-011',
            element_count=11,
            inspector=self.user,
            status='RAW_INGESTED',
        )

        for i in range(11):
            PUNDITTest.objects.create(
                id=f'TEST-B-{i:03d}',
                project=self.project,
                batch=batch_b,
                structural_element=f'BEAM-EDGE-{i+1:02d}',
                floor='Floor 2',
                path_length_mm=250.0,
                pulse_time_us=75.0,  # ~3.33 km/s
                velocity_km_s=3.33,
                created_by=self.user,
            )

        # Batch A analysis must strictly see 48 elements, NEVER 59
        rec_a = PUNDITAdapter.analyze_project(self.project, batch=batch_a)
        self.assertIn("48 verified element(s)", rec_a.reasoning_log)
        self.assertNotIn("59", rec_a.reasoning_log)

        # Batch B analysis must strictly see 11 elements, NEVER 59
        rec_b = PUNDITAdapter.analyze_project(self.project, batch=batch_b)
        self.assertIn("11 verified element(s)", rec_b.reasoning_log)
        self.assertNotIn("59", rec_b.reasoning_log)

    def test_visual_observations_and_site_attendance_in_summary(self):
        """
        Verify that visual defect observations (with photos) and client/site
        attendance logs are captured and fed into the AI fact pack and reasoning log.
        """
        batch = PunditScanBatch.objects.create(
            project=self.project,
            folder_name='Floor 2 RC Slab',
            batch_reference='BATCH-2026-09-LOG',
            element_count=1,
            inspector=self.user,
        )

        PUNDITTest.objects.create(
            id='TEST-LOG-001',
            project=self.project,
            batch=batch,
            structural_element='Column C24',
            floor='Floor 2',
            path_length_mm=250.0,
            pulse_time_us=90.0,  # 2.77 km/s (poor)
            velocity_km_s=2.77,
            created_by=self.user,
        )

        VisualObservation.objects.create(
            project=self.project,
            batch=batch,
            structural_element='Column C24',
            grid_location='Grid D-7',
            floor='Floor 2',
            category='honeycombing',
            severity='HIGH',
            description='Severe aggregate segregation and voiding observed at column base.',
            inspector_name='Engr. Adeleke',
            created_by=self.user,
        )

        SiteAttendanceRecord.objects.create(
            project=self.project,
            batch=batch,
            attendee_name='Chief Babatunde Alabi',
            organization='ExxonMobil Client Team',
            role='Client Representative',
            signed_off=True,
        )

        curve_profile = {
            'curve_type': 'exponential',
            'params': {'a': 1.20, 'b': 0.85, 'c': 0.0},
            'design_strength_mpa': 25.0,
        }

        rec = PUNDITAdapter.analyze_project(self.project, batch=batch, curve_profile=curve_profile)
        self.assertTrue(rec.reasoning_log)
        self.assertIn('Column C24', rec.reasoning_log)
        self.assertIn('Calibrated correlation model applied: exponential', rec.reasoning_log)


@override_settings(SECURE_SSL_REDIRECT=False)
class PunditAPIFlowTestCase(APITestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(
            email='inspector.ndt@nexucon.gov.ng',
            username='inspector_ndt',
            password='TestPassword123!',
            first_name='Inspector',
            last_name='Suleiman',
        )
        self.project = Project.objects.create(
            name='Eko Atlantic High-Rise Tower',
            project_type='Commercial',
            status='ACTIVE',
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')

    def test_batch_lifecycle_and_calibration_api(self):
        """
        End-to-end API test:
          1. Create scan batch (status: RAW_INGESTED).
          2. Add test points to batch (status remains RAW_INGESTED - NO AUTO ANALYSIS).
          3. Calibrate batch with exponential model -> status becomes CALIBRATED.
          4. Initiate analysis -> status becomes ANALYSIS_COMPLETE.
        """
        # 1. Create Scan Batch
        resp = self.client.post(reverse('pundit-batch-list'), {
            'project': str(self.project.id),
            'folder_name': 'Ground Floor Columns (36 Stations)',
            'batch_reference': 'BATCH-GF-COL-36',
            'element_count': 36,
            'floor': 'Ground Floor',
            'notes': 'Pre-handover structural compliance check',
        }, format='json')
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED, f"Got {resp.status_code}, Location: {resp.get('Location')}, URL: {reverse('pundit-batch-list')}")
        batch_id = resp.data['id']
        self.assertEqual(resp.data['status'], 'RAW_INGESTED')

        # 2. Add Test Point
        resp_test = self.client.post(reverse('pundit-test-list'), {
            'project': str(self.project.id),
            'batch': batch_id,
            'structural_element': 'COL-G01',
            'floor': 'Ground Floor',
            'test_type': 'pulse_velocity',
            'path_length_mm': 300.0,
            'pulse_time_us': 75.0,  # 4.0 km/s
        }, format='json')
        self.assertEqual(resp_test.status_code, status.HTTP_201_CREATED)

        # Verify batch status is STILL RAW_INGESTED (NOT auto-analyzed)
        batch_get = self.client.get(reverse('pundit-batch-detail', kwargs={'pk': batch_id}))
        self.assertEqual(batch_get.data['status'], 'RAW_INGESTED')
        self.assertEqual(batch_get.data['test_count'], 1)

        # 3. Calibrate Model (Exponential)
        resp_cal = self.client.post(reverse('pundit-batch-calibrate', kwargs={'pk': batch_id}), {
            'curve_type': 'exponential',
            'params': {'a': 1.20, 'b': 0.85, 'c': 0.0},
            'design_strength_mpa': 25.0,
            'cube_correlation_points': [
                {'velocity_ms': 3800, 'cube_strength_mpa': 29.8},
                {'velocity_ms': 4200, 'cube_strength_mpa': 41.5},
            ],
            'notes': 'Calibrated against 28-day water-cured cube tests',
        }, format='json')
        self.assertEqual(resp_cal.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_cal.data['batch_status'], 'CALIBRATED')

        # 4. Initiate Analysis via analyze_project
        resp_analysis = self.client.post(reverse('pundit-test-analyze-project'), {
            'project': str(self.project.id),
            'batch_id': batch_id,
            'curve_type': 'exponential',
            'params': {'a': 1.20, 'b': 0.85, 'c': 0.0},
            'design_strength_mpa': 25.0,
        }, format='json')
        self.assertEqual(resp_analysis.status_code, status.HTTP_200_OK)
        self.assertEqual(resp_analysis.data['tests_analysed'], 1)
        self.assertEqual(resp_analysis.data['batch_folder'], 'Ground Floor Columns (36 Stations)')

        # Batch status must now be ANALYSIS_COMPLETE
        batch_final = self.client.get(reverse('pundit-batch-detail', kwargs={'pk': batch_id}))
        self.assertEqual(batch_final.data['status'], 'ANALYSIS_COMPLETE')
