"""
Digital Eye tests (implementation plan §5A).

Covers:
  * PUNDIT ultrasonic pulse-velocity math (BS 1881-203 / ASTM C597) —
    velocity = path length / transit time, concrete quality grading bands
    and crack-depth by the time-difference method.
  * Tersus GNSS (MVP SI) device telemetry ingest through the heartbeat
    endpoint — telemetry is stored verbatim from the report and never
    fabricated (null in, null out).
  * API authentication on the Digital Eye endpoints.
  * Sensor-data file upload validation and SHA-256 checksum computation.
"""
import hashlib
from unittest import mock
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.audit.models import AuditEvent
from apps.common.ai_service import AIProviderUnavailable
from apps.evidence.models import AIAnalysisRecord, EvidenceRecord
from apps.projects.models import Project

from .adapters import GNSSProjection, GPRAdapter, PUNDITAdapter
from .models import (
    BIMElementMapping, BIMModelGeometry, FieldDevice, GPRAnomaly, GPRSurvey,
    GnssBenchmark, GnssBoundaryPoint, GnssSurvey, LiveStream, PUNDITReading,
    PUNDITTest, SensorDataFile, TrimbleConnection, TrimbleProject,
)

User = get_user_model()


# ======================================================================
# PUNDIT pulse-velocity math (BS 1881-203 / ASTM C597)
# ======================================================================

class PUNDITMathTestCase(TestCase):
    """Deterministic velocity / grading / crack-depth computations."""

    def test_velocity_computed_from_real_measurements(self):
        # v = L / t. mm/us is numerically km/s: 250 mm over 62.5 us -> 4.0 km/s.
        self.assertAlmostEqual(PUNDITAdapter.compute_velocity_km_s(250.0, 62.5), 4.0)
        # Other realistic field measurements.
        self.assertAlmostEqual(PUNDITAdapter.compute_velocity_km_s(400.0, 100.0), 4.0)
        self.assertAlmostEqual(PUNDITAdapter.compute_velocity_km_s(300.0, 100.0), 3.0)
        self.assertAlmostEqual(PUNDITAdapter.compute_velocity_km_s(150.0, 50.0), 3.0)

    def test_velocity_zero_or_invalid_inputs_return_none(self):
        """No division by zero and no invented velocity on bad input."""
        invalid_inputs = [
            (250.0, 0),        # zero transit time
            (0, 62.5),         # zero path length
            (None, 62.5),      # path length never measured
            (250.0, None),     # transit time never measured
            (None, None),      # nothing measured
            (-250.0, 62.5),    # negative path
            (250.0, -1.0),     # negative transit time
        ]
        for path_length, transit_time in invalid_inputs:
            self.assertIsNone(
                PUNDITAdapter.compute_velocity_km_s(path_length, transit_time),
                msg=f"expected None for path={path_length!r}, transit={transit_time!r}")

    def test_quality_grading_follows_bs1881_bands(self):
        cases = [
            (None, 'pending'),   # unmeasured -> pending, never a guessed grade
            (4.6, 'excellent'),
            (4.5, 'excellent'),  # band boundary is inclusive
            (4.49, 'good'),
            (3.75, 'good'),
            (3.74, 'questionable'),
            (3.0, 'questionable'),
            (2.99, 'poor'),
            (2.0, 'poor'),
            (1.99, 'very_poor'),
            (0.5, 'very_poor'),
        ]
        for velocity, expected_grade in cases:
            self.assertEqual(
                PUNDITAdapter.grade_quality(velocity), expected_grade,
                msg=f"grade for v={velocity!r}")

    def test_crack_depth_time_difference_method(self):
        # d = L/2 * sqrt((t_c/t_0)^2 - 1)
        depth = PUNDITAdapter.compute_crack_depth_mm(300.0, 70.0, 60.0)
        expected = (300.0 / 2.0) * ((70.0 / 60.0) ** 2 - 1) ** 0.5
        self.assertAlmostEqual(depth, expected)

    def test_crack_depth_unmeasurable_inputs_return_none(self):
        invalid_inputs = [
            (300.0, 60.0, 70.0),   # cracked path not slower -> no measurable depth
            (300.0, 60.0, 60.0),   # equal times -> ratio 1
            (300.0, 0, 60.0),      # zero transit time
            (300.0, 70.0, 0),      # zero uncracked time
            (None, 70.0, 60.0),    # missing path length
            (300.0, None, 60.0),   # missing cracked time
            (300.0, 70.0, None),   # missing uncracked time
        ]
        for args in invalid_inputs:
            self.assertIsNone(
                PUNDITAdapter.compute_crack_depth_mm(*args),
                msg=f"expected None for crack-depth args={args!r}")


# ======================================================================
# API base
# ======================================================================

class DigitalEyeAPITestBase(APITestCase):
    """Authenticated API access + a project in scope (superuser sees all)."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_superuser(
            username='de_officer@nexucon.com',
            email='de_officer@nexucon.com',
            password='Password123!',
        )
        refresh = RefreshToken.for_user(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        self.project = Project.objects.create(
            name='Lekki Digital Eye Test Site',
            project_type='Commercial',
            status='ACTIVE',
        )


# ======================================================================
# API authentication
# ======================================================================

class DigitalEyeAuthenticationTestCase(APITestCase):
    """Every Digital Eye endpoint requires authentication."""

    LIST_URL_NAMES = [
        'field-device-list',
        'sensor-file-list',
        'gpr-survey-list',
        'gpr-anomaly-list',
        'pundit-test-list',
        'gnss-survey-list',
        'gnss-benchmark-list',
        'gnss-boundary-point-list',
        'bim-element-list',
        'live-stream-list',
        'trimble-connection-list',
        'trimble-project-list',
    ]

    def test_anonymous_list_requests_are_unauthorized(self):
        for url_name in self.LIST_URL_NAMES:
            response = self.client.get(reverse(url_name))
            self.assertEqual(
                response.status_code, status.HTTP_401_UNAUTHORIZED,
                msg=f'{url_name} should require authentication')


# ======================================================================
# PUNDIT API — serializer validation + deterministic analyze endpoint
# ======================================================================

class PUNDITAPITestCase(DigitalEyeAPITestBase):
    def _post_test(self, payload):
        return self.client.post(reverse('pundit-test-list'), payload, format='json')

    def test_pulse_velocity_test_requires_both_measurements(self):
        # Path length given, transit time missing.
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'pulse_velocity',
            'path_length_mm': 250.0,
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('pulse_time_us', response.data['errors'])

    def test_pulse_velocity_test_rejects_zero_or_negative_measurements(self):
        for bad_value in (0, -62.5):
            response = self._post_test({
                'project': str(self.project.id),
                'test_type': 'pulse_velocity',
                'path_length_mm': 250.0,
                'pulse_time_us': bad_value,
            })
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST,
                             msg=f'pulse_time_us={bad_value} must be rejected')
            self.assertIn('pulse_time_us', response.data['errors'])

        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'pulse_velocity',
            'path_length_mm': 0,
            'pulse_time_us': 62.5,
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('path_length_mm', response.data['errors'])

    def test_crack_depth_test_requires_its_three_measurements(self):
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'crack_depth',
            'crack_path_length_mm': 300.0,
            'crack_pulse_time_us': 70.0,
            # uncracked_pulse_time_us missing
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('uncracked_pulse_time_us', response.data['errors'])

    def test_analyze_computes_velocity_and_grade_from_measurements(self):
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'pulse_velocity',
            'structural_element': 'COL-C24',
            'path_length_mm': 250.0,
            'pulse_time_us': 62.5,
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        test_id = response.data['id']

        analysis = self.client.post(
            reverse('pundit-test-analyze', kwargs={'pk': test_id}))
        self.assertEqual(analysis.status_code, status.HTTP_200_OK)
        # 250 mm / 62.5 us = 4.0 km/s -> 'good' per BS 1881-203 bands.
        self.assertAlmostEqual(float(analysis.data['velocity_km_s']), 4.0)
        self.assertEqual(analysis.data['quality_grade'], 'good')

        # Computed values are persisted on the test record.
        test = PUNDITTest.objects.get(pk=test_id)
        self.assertAlmostEqual(test.velocity_km_s, 4.0)
        self.assertEqual(test.quality_grade, 'good')

    def test_correction_re_runs_analysis_and_audits_the_edit(self):
        """Editing a measurement re-derives the stored outputs (velocity,
        grade, crack depth) so a corrected record can never render with its
        stale pre-correction numbers — and the edit lands on the audit trail."""
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'pulse_velocity',
            'structural_element': 'COL-C24',
            'path_length_mm': 250.0,
            'pulse_time_us': 62.5,
        })
        test_id = response.data['id']
        self.client.post(reverse('pundit-test-analyze', kwargs={'pk': test_id}))

        # Operator corrects the transit time: 250 mm / 100 µs = 2.5 km/s.
        patched = self.client.patch(
            reverse('pundit-test-detail', kwargs={'pk': test_id}),
            {'pulse_time_us': 100.0}, format='json')
        self.assertEqual(patched.status_code, status.HTTP_200_OK)

        test = PUNDITTest.objects.get(pk=test_id)
        self.assertAlmostEqual(test.velocity_km_s, 2.5)
        # 2.5 km/s sits in the 2.0-3.0 'poor' band (BS 1881-203 / Whitehurst).
        self.assertEqual(test.quality_grade, 'poor')

        audit = AuditEvent.objects.filter(
            action='digital_eye.pundit_test.update',
            resource_id=str(test.id)).order_by('-timestamp').first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.metadata.get('corrected_fields'), ['pulse_time_us'])

    def test_non_measurement_edit_is_audited_but_not_reanalyzed(self):
        """Correcting a label (notes, element) never touches the derived
        outputs — the previous analysis stays authoritative."""
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'pulse_velocity',
            'path_length_mm': 250.0,
            'pulse_time_us': 62.5,
        })
        test_id = response.data['id']
        self.client.post(reverse('pundit-test-analyze', kwargs={'pk': test_id}))

        patched = self.client.patch(
            reverse('pundit-test-detail', kwargs={'pk': test_id}),
            {'notes': 'Grid reference corrected after site visit.'}, format='json')
        self.assertEqual(patched.status_code, status.HTTP_200_OK)

        test = PUNDITTest.objects.get(pk=test_id)
        self.assertAlmostEqual(test.velocity_km_s, 4.0)
        self.assertEqual(test.quality_grade, 'good')

        audit = AuditEvent.objects.filter(
            action='digital_eye.pundit_test.update',
            resource_id=str(test.id)).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.metadata.get('corrected_fields'), [])

    def test_analyze_without_measurements_stays_pending(self):
        # A record with no measurements (e.g. created before the device
        # export is attached) must never get an invented velocity.
        test = PUNDITTest.objects.create(
            project=self.project,
            test_type='pulse_velocity',
            structural_element='COL-C24',
        )
        analysis = self.client.post(
            reverse('pundit-test-analyze', kwargs={'pk': str(test.id)}))
        self.assertEqual(analysis.status_code, status.HTTP_200_OK)
        self.assertIsNone(analysis.data['velocity_km_s'])
        self.assertEqual(analysis.data['quality_grade'], 'pending')

        test.refresh_from_db()
        self.assertIsNone(test.velocity_km_s)
        self.assertEqual(test.quality_grade, 'pending')

    def test_serializer_exposes_frontend_contract_fields(self):
        """project_name / test_location / transducer_type / E.C.S feed the PUNDIT dashboard."""
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'pulse_velocity',
            'structural_element': 'COL-C24',
            'transducer_type': 'direct',
            'test_location': 'Grid D-7 Core Section',
            'transducer_frequency_khz': 54,
            'path_length_mm': 250.0,
            'pulse_time_us': 62.5,
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        test_id = response.data['id']

        self.client.post(reverse('pundit-test-analyze', kwargs={'pk': test_id}))

        row = self.client.get(reverse('pundit-test-list')).data
        self.assertEqual(len(row), 1)
        row = row[0]
        self.assertEqual(row['project_name'], 'Lekki Digital Eye Test Site')
        self.assertEqual(row['test_location'], 'Grid D-7 Core Section')
        self.assertEqual(row['transducer_type'], 'direct')
        self.assertEqual(row['transducer_type_display'], 'Direct Transmission')
        # 250 mm / 62.5 us = 4.0 km/s -> E.C.S = 8.961*4.0 - 7.97 = 27.874 N/mm2.
        self.assertAlmostEqual(row['estimated_compressive_strength_mpa'], 27.874, places=3)

    def test_ecs_is_none_outside_calibration_range(self):
        """The E.C.S curve is never extrapolated beyond its 2.0–5.0 km/s validity."""
        # 550 mm / 100 us = 5.5 km/s -> outside the curve.
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'pulse_velocity',
            'path_length_mm': 550.0,
            'pulse_time_us': 100.0,
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        test_id = response.data['id']
        self.client.post(reverse('pundit-test-analyze', kwargs={'pk': test_id}))

        row = self.client.get(reverse('pundit-test-list')).data[0]
        self.assertAlmostEqual(row['velocity_km_s'], 5.5)
        self.assertIsNone(row['estimated_compressive_strength_mpa'])

        # Unanalysed (pending) tests must not get an invented strength either.
        PUNDITTest.objects.create(project=self.project, test_type='pulse_velocity')
        rows = self.client.get(reverse('pundit-test-list')).data
        pending = [r for r in rows if r['quality_grade'] == 'pending'][0]
        self.assertIsNone(pending['estimated_compressive_strength_mpa'])

    def test_transducer_type_must_be_a_known_arrangement(self):
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'pulse_velocity',
            'transducer_type': 'sideways',
            'path_length_mm': 250.0,
            'pulse_time_us': 62.5,
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('transducer_type', response.data['errors'])

    def test_multi_reading_auto_assigns_point_labels_when_omitted(self):
        """Review Meeting A1: Multi-point pulse-velocity test accepts readings without
        point_label, auto-assigning A, B, C... and computing element-mean velocity and grade."""
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'pulse_velocity',
            'structural_element': 'COL-C24',
            'path_length_mm': 250.0,
            'readings': [
                {'transit_time_us': 62.5},
                {'transit_time_us': 62.5},
                {'transit_time_us': 62.5},
            ],
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, msg=response.data)
        test_id = response.data['id']
        test = PUNDITTest.objects.get(pk=test_id)
        readings = list(test.readings.all().order_by('point_label'))
        self.assertEqual(len(readings), 3)
        self.assertEqual([r.point_label for r in readings], ['A', 'B', 'C'])
        for r in readings:
            self.assertEqual(r.path_length_mm, 250.0)
            self.assertAlmostEqual(r.velocity_km_s, 4.0)
        self.assertAlmostEqual(test.velocity_km_s, 4.0)
        self.assertEqual(test.quality_grade, 'good')

    def test_multi_reading_rejects_duplicate_point_labels(self):
        """Duplicate point labels on the same element are rejected with validation error."""
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'pulse_velocity',
            'structural_element': 'COL-C24',
            'path_length_mm': 250.0,
            'readings': [
                {'point_label': 'A', 'transit_time_us': 62.5},
                {'point_label': 'A', 'transit_time_us': 60.0},
            ],
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('readings', response.data['errors'])

    def test_multi_reading_crack_depth_points_and_element_mean(self):
        """A1 for crack depth: one element = N test points, each with its own
        t_c / t_0 pair. Labels auto-assign A, B, C…; per-point depths and the
        element-mean depth are computed server-side; the cracked path never
        yields a bogus pulse velocity."""
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'crack_depth',
            'structural_element': 'COL-C24',
            'crack_path_length_mm': 300.0,
            'readings': [
                {'transit_time_us': 70.0, 'uncracked_transit_time_us': 62.5},
                {'transit_time_us': 75.0, 'uncracked_transit_time_us': 62.5},
                {'transit_time_us': 80.0, 'uncracked_transit_time_us': 62.5},
            ],
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, msg=response.data)
        test = PUNDITTest.objects.get(pk=response.data['id'])
        readings = list(test.readings.all().order_by('point_label'))
        self.assertEqual([r.point_label for r in readings], ['A', 'B', 'C'])
        for r in readings:
            self.assertEqual(r.path_length_mm, 300.0)
            # The cracked-path time is not a velocity measurement.
            self.assertIsNone(r.velocity_km_s)
            self.assertIsNone(r.ecs_mpa)
        # d = L/2 * sqrt((t_c/t_0)^2 - 1) per point.
        self.assertAlmostEqual(readings[0].crack_depth_mm, 75.657, places=2)
        self.assertAlmostEqual(readings[1].crack_depth_mm, 99.4987, places=2)
        self.assertAlmostEqual(readings[2].crack_depth_mm, 119.8499, places=2)
        # Element verdict = the mean of the per-point depths, persisted on
        # the test for the report / registry.
        self.assertAlmostEqual(test.crack_depth_mm, 98.335, places=2)
        self.assertAlmostEqual(test.estimated_crack_depth_mm, 98.335, places=2)
        # Point A's measurements are written back to the scalar columns for
        # legacy consumers.
        self.assertEqual(test.crack_path_length_mm, 300.0)
        self.assertEqual(test.crack_pulse_time_us, 70.0)
        self.assertEqual(test.uncracked_pulse_time_us, 62.5)
        self.assertIsNone(test.velocity_km_s)

    def test_multi_reading_crack_point_without_measurable_depth_is_honest(self):
        """A point whose cracked time is not slower than its uncracked time
        honestly yields no depth (None) — never zero, never invented. The
        element mean runs over the measurable points only."""
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'crack_depth',
            'structural_element': 'BEAM-B2',
            'crack_path_length_mm': 300.0,
            'readings': [
                {'transit_time_us': 70.0, 'uncracked_transit_time_us': 62.5},
                {'transit_time_us': 60.0, 'uncracked_transit_time_us': 62.5},
            ],
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, msg=response.data)
        test = PUNDITTest.objects.get(pk=response.data['id'])
        readings = list(test.readings.all().order_by('point_label'))
        self.assertAlmostEqual(readings[0].crack_depth_mm, 75.657, places=2)
        self.assertIsNone(readings[1].crack_depth_mm)
        self.assertAlmostEqual(test.crack_depth_mm, 75.657, places=2)

    def test_multi_reading_crack_requires_both_times_per_point(self):
        """A crack point missing either measurement is a validation error —
        the depth cannot be computed from half a pair."""
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'crack_depth',
            'crack_path_length_mm': 300.0,
            'readings': [{'transit_time_us': 70.0}],
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('readings', response.data['errors'])

    def test_multi_reading_surface_conditions_per_point(self):
        """A1 for surface quality: one element = N test points, each carrying
        the condition observed there. Labels auto-assign A, B, C…; point A's
        condition is written back to the legacy scalar column."""
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'surface_quality',
            'structural_element': 'SLAB-S1',
            'readings': [
                {'surface_condition': 'Smooth finished, no visible cracking'},
                {'surface_condition': 'Honeycombing at mid-height'},
                {'surface_condition': 'Hairline cracks near the support'},
            ],
        })
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, msg=response.data)
        test = PUNDITTest.objects.get(pk=response.data['id'])
        readings = list(test.readings.all().order_by('point_label'))
        self.assertEqual([r.point_label for r in readings], ['A', 'B', 'C'])
        self.assertEqual(readings[1].surface_condition, 'Honeycombing at mid-height')
        # No transit times exist for surface points — nothing is invented.
        for r in readings:
            self.assertIsNone(r.transit_time_us)
            self.assertIsNone(r.crack_depth_mm)
        self.assertEqual(test.surface_condition, 'Smooth finished, no visible cracking')

    def test_multi_reading_surface_requires_condition_per_point(self):
        response = self._post_test({
            'project': str(self.project.id),
            'test_type': 'surface_quality',
            'readings': [{'surface_condition': '   '}],
        })
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('readings', response.data['errors'])


class PunditExcelImportTestCase(DigitalEyeAPITestBase):
    """Batch upload of readings from the .xlsx template (review meeting A2).
    One row per test point; an element's consecutive rows form one test with
    points A, B, C... Everything computed stays server-side; the import is
    all-or-nothing."""

    def _workbook(self, rows, name='readings.xlsx'):
        """rows: list of dicts keyed by template header (None => blank).
        The template ships with sample rows — delete them so each test writes
        exactly its own rows."""
        import io
        from .excel_import import TEMPLATE_COLUMNS, build_template_bytes
        workbook = build_template_bytes()
        sheet = workbook['READINGS']
        for row_number in range(sheet.max_row, 1, -1):
            sheet.delete_rows(row_number)
        for row_number, row in enumerate(rows, start=2):
            for column, header in enumerate(TEMPLATE_COLUMNS, start=1):
                if header in row:
                    sheet.cell(row=row_number, column=column, value=row[header])
        buffer = io.BytesIO()
        workbook.save(buffer)
        return SimpleUploadedFile(name, buffer.getvalue())

    def _post_import(self, file):
        return self.client.post(
            reverse('pundit-test-import-readings'),
            {'project': str(self.project.id), 'file': file},
            format='multipart')

    def test_template_downloads_with_headers_and_sample_rows(self):
        import io
        from openpyxl import load_workbook
        from .excel_import import TEMPLATE_COLUMNS
        response = self.client.get(reverse('pundit-test-import-template'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('spreadsheetml', response['Content-Type'])
        self.assertIn('attachment', response['Content-Disposition'])
        workbook = load_workbook(io.BytesIO(b''.join(response.streaming_content)
                                            if hasattr(response, 'streaming_content')
                                            else response.content))
        sheet = workbook['READINGS']
        headers = [c.value for c in sheet[1] if c.value is not None]
        self.assertEqual(headers, TEMPLATE_COLUMNS)
        # The template ships pre-filled with clearly-labelled sample rows so
        # it can be uploaded as-is to try the flow.
        self.assertGreater(sheet.max_row, 1)
        sample_note = sheet.cell(row=2, column=TEMPLATE_COLUMNS.index('NOTES') + 1).value
        self.assertIn('SAMPLE ROW', str(sample_note))
        elements = {sheet.cell(row=r, column=1).value
                    for r in range(2, sheet.max_row + 1)}
        self.assertIn('COL-A1', elements)      # pulse velocity
        self.assertIn('BEAM-B2', elements)     # crack depth
        self.assertIn('WALL-W1', elements)     # surface quality
        self.assertIn('HOW TO FILL', workbook.sheetnames)

    def test_uploaded_template_imports_its_sample_rows(self):
        # The as-downloaded template must import cleanly: the sample rows are
        # valid operator-shaped readings and exercise the full flow end to end.
        import io
        from .excel_import import build_template_bytes
        buffer = io.BytesIO()
        build_template_bytes().save(buffer)
        buffer.seek(0)
        upload = SimpleUploadedFile('template.xlsx', buffer.read(),
                                    content_type='application/vnd.openxmlformats-'
                                                 'officedocument.spreadsheetml.sheet')
        response = self.client.post(
            reverse('pundit-test-import-readings'),
            {'project': str(self.project.id), 'file': upload}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['tests_created'], 3)
        self.assertEqual(response.data['points_imported'], 7)
        self.assertEqual(PUNDITTest.objects.count(), 3)
        # The sample element names match no BIM element of the project, so the
        # result must say so honestly instead of implying a model link.
        self.assertTrue(all(t['bim_linked'] is False for t in response.data['tests']))

    def test_import_creates_tests_with_computed_means(self):
        rows = [
            # One element, three pulse-velocity points -> ONE test, A/B/C.
            {'STRUCTURAL ELEMENT': 'COL-C24', 'FLOOR': 'First Floor',
             'TEST TYPE': 'PULSE VELOCITY', 'PATH LENGTH L (MM)': 120,
             'TRANSIT TIME T (US)': 31.2, 'WEATHER CONDITION': 'Sunny',
             'TRANSDUCER FREQUENCY (KHZ)': 54},
            {'STRUCTURAL ELEMENT': 'COL-C24', 'FLOOR': 'First Floor',
             'TEST TYPE': 'PULSE VELOCITY', 'PATH LENGTH L (MM)': 120,
             'TRANSIT TIME T (US)': 32.2},
            {'STRUCTURAL ELEMENT': 'COL-C24', 'FLOOR': 'First Floor',
             'TEST TYPE': 'PULSE VELOCITY', 'PATH LENGTH L (MM)': 120,
             'TRANSIT TIME T (US)': 30.1},
            # A second element, single point.
            {'STRUCTURAL ELEMENT': 'SLAB-S1', 'FLOOR': 'Second Floor',
             'TEST TYPE': 'PULSE VELOCITY', 'PATH LENGTH L (MM)': 120,
             'TRANSIT TIME T (US)': 29.8},
            # Crack depth: two points with cracked + uncracked times.
            {'STRUCTURAL ELEMENT': 'BEAM-B2', 'FLOOR': 'Ground Floor',
             'TEST TYPE': 'CRACK DEPTH', 'PATH LENGTH L (MM)': 300,
             'TRANSIT TIME T (US)': 70, 'T UNCRACKED (US)': 62.5},
            {'STRUCTURAL ELEMENT': 'BEAM-B2', 'FLOOR': 'Ground Floor',
             'TEST TYPE': 'CRACK DEPTH', 'PATH LENGTH L (MM)': 300,
             'TRANSIT TIME T (US)': 75, 'T UNCRACKED (US)': 62.5},
            # Surface quality: observed conditions only.
            {'STRUCTURAL ELEMENT': 'WALL-W1', 'TEST TYPE': 'SURFACE QUALITY',
             'SURFACE CONDITION': 'Smooth finished, no visible cracking'},
            {'STRUCTURAL ELEMENT': 'WALL-W1', 'TEST TYPE': 'SURFACE QUALITY',
             'SURFACE CONDITION': 'Hairline map cracking near the joint'},
        ]
        response = self._post_import(self._workbook(rows))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['tests_created'], 4)
        self.assertEqual(response.data['points_imported'], 8)

        column = PUNDITTest.objects.get(structural_element='COL-C24')
        self.assertEqual(column.floor, 'First Floor')
        self.assertEqual(column.weather_condition, 'Sunny')
        self.assertEqual(column.transducer_frequency_khz, 54)
        labels = list(column.readings.order_by('point_label')
                      .values_list('point_label', flat=True))
        self.assertEqual(labels, ['A', 'B', 'C'])
        # Velocity / grade / E.C.S are the element means, computed server-side.
        mean_v = (120 / 31.2 + 120 / 32.2 + 120 / 30.1) / 3
        self.assertAlmostEqual(column.velocity_km_s, mean_v, places=3)
        self.assertAlmostEqual(column.pulse_velocity_ms, mean_v * 1000, places=0)
        self.assertEqual(column.quality_grade, 'good')
        self.assertAlmostEqual(column.estimated_compressive_strength_mpa,
                               8.961 * mean_v - 7.97, places=2)

        beam = PUNDITTest.objects.get(structural_element='BEAM-B2')
        self.assertIsNone(beam.velocity_km_s)  # a cracked path time is not a velocity
        mean_depth = (150 * ((70 / 62.5) ** 2 - 1) ** 0.5
                      + 150 * ((75 / 62.5) ** 2 - 1) ** 0.5) / 2
        self.assertAlmostEqual(beam.estimated_crack_depth_mm, mean_depth, places=2)

        wall = PUNDITTest.objects.get(structural_element='WALL-W1')
        self.assertEqual(wall.readings.count(), 2)
        self.assertEqual(wall.readings.order_by('point_label').first().point_label, 'A')

    def test_import_is_all_or_nothing_with_row_errors(self):
        rows = [
            {'STRUCTURAL ELEMENT': 'COL-C24', 'TEST TYPE': 'PULSE VELOCITY',
             'PATH LENGTH L (MM)': 120, 'TRANSIT TIME T (US)': 31.2},
            # Row 3 has no transit time — the whole file must be rejected.
            {'STRUCTURAL ELEMENT': 'COL-C24', 'TEST TYPE': 'PULSE VELOCITY',
             'PATH LENGTH L (MM)': 120},
        ]
        response = self._post_import(self._workbook(rows))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        error_rows = [e['row'] for e in response.data['errors'] if 'row' in e]
        self.assertIn(3, error_rows)
        self.assertFalse(PUNDITTest.objects.filter(
            structural_element='COL-C24').exists())

    def test_import_rejects_element_split_across_blocks(self):
        rows = [
            {'STRUCTURAL ELEMENT': 'COL-C24', 'TEST TYPE': 'PULSE VELOCITY',
             'PATH LENGTH L (MM)': 120, 'TRANSIT TIME T (US)': 31.2},
            {'STRUCTURAL ELEMENT': 'SLAB-S1', 'TEST TYPE': 'PULSE VELOCITY',
             'PATH LENGTH L (MM)': 120, 'TRANSIT TIME T (US)': 29.8},
            # Back to COL-C24 — a second block for the same element+type.
            {'STRUCTURAL ELEMENT': 'COL-C24', 'TEST TYPE': 'PULSE VELOCITY',
             'PATH LENGTH L (MM)': 120, 'TRANSIT TIME T (US)': 30.1},
        ]
        response = self._post_import(self._workbook(rows))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(any('two separate blocks' in e['message']
                            for e in response.data['errors']))
        self.assertEqual(PUNDITTest.objects.filter(
            project=self.project).count(), 0)

    def test_import_rejects_non_xlsx_and_missing_file(self):
        response = self.client.post(
            reverse('pundit-test-import-readings'),
            {'project': str(self.project.id),
             'file': SimpleUploadedFile('readings.csv', b'a,b,c')},
            format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('template', response.data['detail'])
        response = self.client.post(
            reverse('pundit-test-import-readings'),
            {'project': str(self.project.id)}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_import_links_bim_guid_from_element_name(self):
        BIMElementMapping.objects.create(
            project=self.project, bim_guid='3rNg7Ib9P5$wfO4JiGtdVn',
            element_name='COL-C24', element_id='COL-C24', element_type='IfcColumn')
        rows = [{'STRUCTURAL ELEMENT': 'COL-C24', 'TEST TYPE': 'PULSE VELOCITY',
                 'PATH LENGTH L (MM)': 120, 'TRANSIT TIME T (US)': 31.2}]
        response = self._post_import(self._workbook(rows))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        test = PUNDITTest.objects.get(structural_element='COL-C24')
        self.assertEqual(test.structural_element_guid, '3rNg7Ib9P5$wfO4JiGtdVn')
        # The created-test listing says it linked.
        self.assertTrue(response.data['tests'][0]['bim_linked'])

    def test_template_scopes_sample_rows_to_project_bim_elements(self):
        # ?project= resolves REAL element names from the imported model so the
        # untouched template's sample rows link to actual members on upload.
        BIMElementMapping.objects.create(
            project=self.project, bim_guid='wallguid0000000000000000',
            element_name='Basic Wall:Exterior', element_id='wall-1', element_type='IfcWall')
        BIMElementMapping.objects.create(
            project=self.project, bim_guid='beamguid0000000000000000',
            element_name='M_Concrete-Rectangular Beam:225 x 600mm:801629',
            element_id='beam-1', element_type='IfcBeam')
        BIMElementMapping.objects.create(
            project=self.project, bim_guid='slabguid0000000000000000',
            element_name='Floor:200THK RC SLAB:780904',
            element_id='slab-1', element_type='IfcSlab')
        response = self.client.get(
            reverse('pundit-test-import-template'), {'project': str(self.project.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        import io
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(response.content))
        elements = {workbook['READINGS'].cell(row=r, column=1).value
                    for r in range(2, 8)}
        self.assertIn('M_Concrete-Rectangular Beam:225 x 600mm:801629', elements)
        self.assertIn('Floor:200THK RC SLAB:780904', elements)

        # And uploading that template as-is now creates linked tests.
        buffer = io.BytesIO()
        workbook.save(buffer)
        buffer.seek(0)
        upload = SimpleUploadedFile('template.xlsx', buffer.read(),
                                    content_type='application/vnd.openxmlformats-'
                                                 'officedocument.spreadsheetml.sheet')
        response = self.client.post(
            reverse('pundit-test-import-readings'),
            {'project': str(self.project.id), 'file': upload}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        linked = [t['bim_linked'] for t in response.data['tests']]
        self.assertTrue(all(linked), response.data['tests'])
        guids = set(PUNDITTest.objects.filter(project=self.project)
                    .values_list('structural_element_guid', flat=True))
        self.assertIn('slabguid0000000000000000', guids)

    def test_import_rejects_project_outside_scope(self):
        other = User.objects.create_user(
            username='excel_scoped@nexucon.com', email='excel_scoped@nexucon.com',
            password='Password123!')
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(other).access_token}')
        response = self.client.post(
            reverse('pundit-test-import-readings'),
            {'project': str(self.project.id),
             'file': self._workbook([{'STRUCTURAL ELEMENT': 'COL-C24',
                                      'TEST TYPE': 'PULSE VELOCITY',
                                      'PATH LENGTH L (MM)': 120,
                                      'TRANSIT TIME T (US)': 31.2}])},
            format='multipart')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class PUNDITSearchFilterTestCase(DigitalEyeAPITestBase):
    """Registry search (?search=) and filters (?test_type=, ?quality_grade=)."""

    def setUp(self):
        super().setUp()
        # One analysed good slab test, one pending crack-depth test, one
        # analysed poor column test — enough to exercise every filter.
        good = PUNDITTest.objects.create(
            project=self.project, test_type='pulse_velocity',
            structural_element='SLAB-S1', test_location='Grid A-1',
            path_length_mm=250.0, pulse_time_us=62.5,  # 4.0 km/s -> good
        )
        PUNDITAdapter.analyze(good)
        PUNDITTest.objects.create(
            project=self.project, test_type='crack_depth',
            structural_element='BEAM-B2', test_location='Grid B-2',
            crack_path_length_mm=300.0, crack_pulse_time_us=70.0,
            uncracked_pulse_time_us=62.5,
        )
        poor = PUNDITTest.objects.create(
            project=self.project, test_type='pulse_velocity',
            structural_element='COL-C24', test_location='Grid C-3',
        )
        poor.path_length_mm = 250.0
        poor.pulse_time_us = 125.0  # 2.0 km/s -> poor
        PUNDITAdapter.analyze(poor)

    def test_search_matches_test_reference_substring(self):
        ref = PUNDITTest.objects.filter(structural_element='COL-C24').first().test_reference
        # Use a distinctive middle slice of the real reference.
        needle = ref[4:-4]
        rows = self.client.get(reverse('pundit-test-list'),
                               {'search': needle}).data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['test_reference'], ref)

    def test_search_matches_structural_element(self):
        rows = self.client.get(reverse('pundit-test-list'),
                               {'search': 'COL-C24'}).data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['structural_element'], 'COL-C24')

    def test_search_matches_test_location(self):
        rows = self.client.get(reverse('pundit-test-list'),
                               {'search': 'Grid B-2'}).data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['structural_element'], 'BEAM-B2')

    def test_filter_by_test_type(self):
        rows = self.client.get(reverse('pundit-test-list'),
                               {'test_type': 'crack_depth'}).data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['test_type'], 'crack_depth')

    def test_filter_by_quality_grade(self):
        rows = self.client.get(reverse('pundit-test-list'),
                               {'quality_grade': 'good'}).data
        self.assertEqual({r['quality_grade'] for r in rows}, {'good'})
        self.assertEqual(len(rows), 1)

    def test_filter_by_structural_element(self):
        rows = self.client.get(reverse('pundit-test-list'),
                               {'structural_element': 'SLAB-S1'}).data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['structural_element'], 'SLAB-S1')

    def test_unknown_filter_value_returns_empty_not_error(self):
        rows = self.client.get(reverse('pundit-test-list'),
                               {'quality_grade': 'nonexistent'}).data
        self.assertEqual(rows, [])

    def test_search_and_filter_combine(self):
        rows = self.client.get(reverse('pundit-test-list'),
                               {'test_type': 'pulse_velocity',
                                'search': 'Grid'}).data
        # Two pulse-velocity tests both have Grid locations; search narrows
        # to whichever location matches 'Grid C-3'.
        self.assertTrue(all(r['test_type'] == 'pulse_velocity' for r in rows))
        self.assertGreaterEqual(len(rows), 1)


class GPRSearchFilterTestCase(DigitalEyeAPITestBase):
    """GPR survey search + status/project filters."""

    def setUp(self):
        super().setUp()
        self.survey = GPRSurvey.objects.create(
            project=self.project, title='Foundation Zone B Void Sweep',
            survey_area='Grid 4-7', structural_element='FTG-08',
        )
        GPRSurvey.objects.create(
            project=self.project, title='Slab Rebar Cover Scan',
            survey_area='Level 2 slab', structural_element='SLB-12',
        )

    def test_search_matches_title(self):
        rows = self.client.get(reverse('gpr-survey-list'),
                               {'search': 'Void Sweep'}).data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['title'], 'Foundation Zone B Void Sweep')

    def test_filter_by_structural_element(self):
        rows = self.client.get(reverse('gpr-survey-list'),
                               {'structural_element': 'SLB-12'}).data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['structural_element'], 'SLB-12')

    def test_anomaly_filter_by_type_and_severity(self):
        GPRAnomaly.objects.create(
            survey=self.survey, anomaly_type='void', severity='high', depth_m=1.2)
        GPRAnomaly.objects.create(
            survey=self.survey, anomaly_type='rebar', severity='low')
        rows = self.client.get(reverse('gpr-anomaly-list'),
                               {'anomaly_type': 'void'}).data
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['anomaly_type'], 'void')
        rows = self.client.get(reverse('gpr-anomaly-list'),
                               {'severity': 'low'}).data
        self.assertEqual({r['severity'] for r in rows}, {'low'})


# ======================================================================
# Tersus GNSS device registry + telemetry ingest
# ======================================================================

class FieldDeviceTelemetryTestCase(DigitalEyeAPITestBase):
    def _register_device(self, **extra_payload):
        payload = {
            'device_id': 'TERSUS-MVP-001',
            'device_type': 'tersus_gnss',
            'name': 'Rover 1',
        }
        payload.update(extra_payload)
        response = self.client.post(reverse('field-device-list'), payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        return response.data['id']

    def _heartbeat(self, device_id, payload):
        return self.client.post(
            reverse('field-device-heartbeat', kwargs={'pk': device_id}),
            payload, format='json')

    def test_registration_leaves_telemetry_null(self):
        # Telemetry fields are read-only on the serializer: even if a client
        # posts them at registration they are ignored — nothing is invented.
        device_id = self._register_device(
            battery_level=99, latitude=6.5, longitude=3.4, last_seen='2026-09-01T10:00:00Z')
        device = FieldDevice.objects.get(pk=device_id)
        self.assertIsNone(device.battery_level)
        self.assertIsNone(device.latitude)
        self.assertIsNone(device.longitude)
        self.assertIsNone(device.last_seen)
        self.assertEqual(device.status, 'registered')

    def test_heartbeat_stores_reported_telemetry_verbatim(self):
        device_id = self._register_device()
        response = self._heartbeat(device_id, {
            'battery_level': 87,
            'latitude': 6.5244,
            'longitude': 3.3792,
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        device = FieldDevice.objects.get(pk=device_id)
        self.assertEqual(device.battery_level, 87)
        self.assertAlmostEqual(device.latitude, 6.5244)
        self.assertAlmostEqual(device.longitude, 3.3792)
        self.assertIsNotNone(device.last_seen)
        self.assertEqual(device.status, 'online')

    def test_heartbeat_without_telemetry_keeps_fields_null(self):
        # Null in, null out: an empty report only refreshes presence.
        device_id = self._register_device()
        response = self._heartbeat(device_id, {})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        device = FieldDevice.objects.get(pk=device_id)
        self.assertIsNone(device.battery_level)
        self.assertIsNone(device.latitude)
        self.assertIsNone(device.longitude)
        self.assertIsNotNone(device.last_seen)
        self.assertEqual(device.status, 'online')

    def test_heartbeat_rejects_invalid_battery(self):
        device_id = self._register_device()
        for bad_battery in ('eighty-five', 150, -1):
            response = self._heartbeat(device_id, {'battery_level': bad_battery})
            self.assertEqual(
                response.status_code, status.HTTP_400_BAD_REQUEST,
                msg=f'battery_level={bad_battery!r} must be rejected')

        device = FieldDevice.objects.get(pk=device_id)
        self.assertIsNone(device.battery_level)

    def test_heartbeat_rejects_non_numeric_coordinates(self):
        device_id = self._register_device()
        for field in ('latitude', 'longitude'):
            response = self._heartbeat(device_id, {field: 'not-a-number'})
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        device = FieldDevice.objects.get(pk=device_id)
        self.assertIsNone(device.latitude)
        self.assertIsNone(device.longitude)


# ======================================================================
# Sensor-data file upload
# ======================================================================

class SensorDataFileUploadTestCase(DigitalEyeAPITestBase):
    def test_upload_without_file_is_rejected(self):
        response = self.client.post(
            reverse('sensor-file-list'), {'file_type': 'photo'}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('file', str(response.data).lower())

    def test_upload_computes_checksum_and_size_from_real_bytes(self):
        payload = b'PUNDIT raw export - device bytes'
        uploaded = SimpleUploadedFile('pundit-001.dat', payload)
        response = self.client.post(
            reverse('sensor-file-list'),
            {'file': uploaded, 'file_type': 'pundit_raw', 'description': 'Column C24 test 1'},
            format='multipart')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['sha256_checksum'],
                         hashlib.sha256(payload).hexdigest())
        self.assertEqual(response.data['file_size_bytes'], len(payload))
        self.assertEqual(response.data['file_name'], 'pundit-001.dat')

    def test_ifc_import_requires_a_file(self):
        response = self.client.post(
            reverse('bim-element-import-ifc'),
            {'project': str(self.project.id)}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('file', str(response.data).lower())


# ======================================================================
# GNSS projection math (WGS84 -> UTM 31N, Lagos / Minna Datum working CRS)
# ======================================================================

class GNSSProjectionTestCase(TestCase):
    """Deterministic Transverse Mercator projection against known points."""

    def test_lagos_point_projects_to_utm_31n(self):
        # Lagos Island (6.5244 N, 3.3792 E) in UTM zone 31N: ~541,924 E /
        # ~721,189 N (verified against pyproj / published references).
        easting, northing = GNSSProjection.geographic_to_utm(6.5244, 3.3792)
        self.assertAlmostEqual(easting, 541924.30, places=1)
        self.assertAlmostEqual(northing, 721189.22, places=1)

    def test_central_meridian_gives_half_million_easting(self):
        # On the zone-31 central meridian (3 E) the easting is exactly 500 km
        # and the northing is the meridional arc.
        easting, _ = GNSSProjection.geographic_to_utm(6.0, 3.0)
        self.assertAlmostEqual(easting, 500000.0, places=6)

    def test_southern_hemisphere_adds_ten_million_northing(self):
        north = GNSSProjection.geographic_to_utm(-1.0, 3.0)[1]
        south = GNSSProjection.geographic_to_utm(-1.0, 3.0, northern_hemisphere=False)[1]
        self.assertAlmostEqual(south - north, 10000000.0, places=3)

    def test_unmeasured_coordinates_return_none(self):
        # Null in, null out — no invented projection.
        self.assertEqual(GNSSProjection.geographic_to_utm(None, 3.0), (None, None))
        self.assertEqual(GNSSProjection.geographic_to_utm(6.5, None), (None, None))

    def test_project_benchmark_persists_computed_utm(self):
        survey = GnssSurvey.objects.create(
            project=Project.objects.create(name='Benchmark Projection Site'),
            title='Control network A',
        )
        benchmark = GnssBenchmark.objects.create(
            survey=survey, point_id='BM-001', latitude=6.5244, longitude=3.3792)

        result = GNSSProjection.project_benchmark(benchmark)

        benchmark.refresh_from_db()
        self.assertEqual(result, benchmark)
        self.assertAlmostEqual(benchmark.easting, 541924.30, places=1)
        self.assertAlmostEqual(benchmark.northing, 721189.22, places=1)
        self.assertEqual(benchmark.utm_zone, '31N')


# ======================================================================
# GPR adapter — deterministic anomaly aggregation
# ======================================================================

class GPRAdapterTestCase(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='GPR Adapter Test Site')
        self.survey = GPRSurvey.objects.create(
            project=self.project, title='Car park scan',
            survey_reference='GPR-TEST-0001',
        )

    def _anomaly(self, **kwargs):
        defaults = {'survey': self.survey, 'anomaly_type': 'void', 'severity': 'low'}
        defaults.update(kwargs)
        return GPRAnomaly.objects.create(**defaults)

    def test_survey_without_anomalies_is_baseline_info(self):
        record = GPRAdapter.analyze(self.survey)

        self.assertEqual(record.analysis_type, 'gpr')
        self.assertEqual(record.risk_level, 'info')
        self.assertIsNone(record.risk_score)  # no detections -> no invented score
        self.assertFalse(record.requires_human_review)
        self.assertIn('No subsurface anomalies recorded.', record.observations)
        self.assertEqual(
            record.recommendations[0]['recommendation'],
            'No anomalies recorded; retain survey as baseline reference.')

    def test_shallow_high_severity_void_escalates_to_critical(self):
        self._anomaly(anomaly_type='void', severity='high', depth_m=0.4,
                      estimated_size_m=1.2)
        record = GPRAdapter.analyze(self.survey)

        # worst void severity 0.78, shallow (<1 m) depth factor 1.15 -> 0.897
        self.assertAlmostEqual(record.risk_score, min(0.78 * 1.15, 1.0), places=3)
        self.assertEqual(record.risk_level, 'critical')
        self.assertTrue(record.requires_human_review)
        self.assertTrue(any('void(s) detected' in o for o in record.observations))
        recs = [r['recommendation'] for r in record.recommendations]
        self.assertTrue(any('coring or trial pitting' in r for r in recs))
        self.assertTrue(any('Restrict access' in r for r in recs))

    def test_deep_medium_void_scores_medium(self):
        self._anomaly(anomaly_type='void', severity='medium', depth_m=1.8)
        record = GPRAdapter.analyze(self.survey)

        # No shallow factor: 0.50 stays medium.
        self.assertAlmostEqual(record.risk_score, 0.50, places=3)
        self.assertEqual(record.risk_level, 'medium')

    def test_low_rebar_cover_drives_high_risk(self):
        self._anomaly(anomaly_type='rebar', severity='low', rebar_cover_mm=18.0)
        record = GPRAdapter.analyze(self.survey)

        self.assertAlmostEqual(record.risk_score, 0.70, places=3)
        self.assertEqual(record.risk_level, 'high')
        self.assertTrue(any('below 25 mm' in o for o in record.observations))
        recs = [r['recommendation'] for r in record.recommendations]
        self.assertTrue(any('BS 8500' in r for r in recs))

    def test_rebar_without_cover_measurement_is_reported_not_guessed(self):
        self._anomaly(anomaly_type='rebar', severity='low', rebar_cover_mm=None)
        record = GPRAdapter.analyze(self.survey)

        self.assertIn('1 rebar detection(s) without cover measurements.',
                      record.observations)
        # No cover value -> no cover-driven risk score invented.
        self.assertEqual(record.risk_level, 'info')

    def test_critical_other_feature_uses_critical_severity_score(self):
        self._anomaly(anomaly_type='utility', severity='critical', depth_m=0.6)
        record = GPRAdapter.analyze(self.survey)

        # A critical-severity utility carries the critical risk score (0.95).
        self.assertAlmostEqual(record.risk_score, 0.95, places=3)
        self.assertEqual(record.risk_level, 'critical')
        self.assertTrue(any('other subsurface feature(s)' in o for o in record.observations))

    def test_every_anomaly_is_registered_in_the_evidence_registry(self):
        first = self._anomaly(anomaly_type='void', severity='low', depth_m=0.5)
        second = self._anomaly(anomaly_type='rebar', severity='low', rebar_cover_mm=30.0)
        record = GPRAdapter.analyze(self.survey)

        evidence_ids = set(record.evidence.values_list('source_id', flat=True))
        self.assertEqual(evidence_ids, {str(first.id), str(second.id)})
        self.assertTrue(EvidenceRecord.objects.filter(
            source_model='digital_eye.GPRAnomaly', source_id=str(first.id)).exists())

    def test_reasoning_log_documents_the_deterministic_steps(self):
        self._anomaly(anomaly_type='void', severity='medium', depth_m=0.6)
        record = GPRAdapter.analyze(self.survey)

        self.assertIn('GPR-TEST-0001: 1 detected subsurface features.',
                      record.reasoning_log)
        self.assertIn('1 shallower than 1.0 m below surface.', record.reasoning_log)
        self.assertEqual(record.model_provider, 'deterministic')


# ======================================================================
# PUNDIT adapter — persistence & crack-depth escalation
# ======================================================================

@patch("apps.common.ai_service.AIService.generate_structured_json",
       side_effect=AIProviderUnavailable("no provider configured"))
class PUNDITAdapterAnalysisTestCase(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='PUNDIT Adapter Test Site')

    def test_analyze_persists_computed_fields_and_evidence(self, _mock):
        test = PUNDITTest.objects.create(
            project=self.project, test_type='pulse_velocity',
            structural_element='COL-C24',
            path_length_mm=250.0, pulse_time_us=62.5,
        )
        record = PUNDITAdapter.analyze(test)

        test.refresh_from_db()
        self.assertAlmostEqual(test.velocity_km_s, 4.0)
        self.assertEqual(test.quality_grade, 'good')

        self.assertEqual(record.analysis_type, 'pundit')
        self.assertEqual(record.risk_level, 'low')
        self.assertEqual(record.model_provider, 'deterministic')
        self.assertTrue(record.requires_human_review)
        self.assertEqual(record.confidence, 1.0)
        # Evidence + AI analysis are linked.
        self.assertEqual(record.evidence.count(), 1)

    def test_deep_crack_escalates_risk_beyond_grade_band(self, _mock):
        # 300 mm path, cracked 70 us vs uncracked 60 us ->
        # d = 150 * sqrt((70/60)^2 - 1) = ~96.8 mm > 25 mm.
        test = PUNDITTest.objects.create(
            project=self.project, test_type='crack_depth',
            structural_element='BM-B12',
            crack_path_length_mm=300.0,
            crack_pulse_time_us=70.0,
            uncracked_pulse_time_us=60.0,
        )
        record = PUNDITAdapter.analyze(test)

        test.refresh_from_db()
        self.assertAlmostEqual(
            test.crack_depth_mm, (300.0 / 2.0) * ((70.0 / 60.0) ** 2 - 1) ** 0.5,
            places=3)
        # Crack depth exceedance must escalate, never stay 'info'.
        self.assertEqual(record.risk_level, 'high')
        self.assertGreaterEqual(record.risk_score, 0.70)
        recs = [r['recommendation'] for r in record.recommendations]
        self.assertTrue(any('crack-depth exceedance' in r for r in recs))

    def test_poor_grade_requires_urgent_structural_review(self, _mock):
        test = PUNDITTest.objects.create(
            project=self.project, test_type='pulse_velocity',
            path_length_mm=250.0, pulse_time_us=125.0,  # 2.0 km/s -> 'poor'
        )
        record = PUNDITAdapter.analyze(test)

        self.assertEqual(record.risk_level, 'high')
        recs = [r['recommendation'] for r in record.recommendations]
        self.assertTrue(any('Immediate structural engineering review' in r for r in recs))
        self.assertTrue(any(r['priority'] == 'Urgent' for r in record.recommendations))

    def test_unmeasured_test_reports_insufficient_measurements(self, _mock):
        test = PUNDITTest.objects.create(
            project=self.project, test_type='pulse_velocity',
        )
        record = PUNDITAdapter.analyze(test)

        self.assertEqual(record.observations,
                         ['Insufficient measurements to compute an NDT result.'])
        self.assertIsNone(record.confidence)
        self.assertEqual(record.recommendations, [])


@patch("apps.common.ai_service.AIService.generate_structured_json",
       return_value={"observations": [
           "Velocity 4.0 km/s places the element in the good band.",
           "Point-to-point variation is within normal bounds.",
       ]})
class PUNDITAdapterLLMNarrativeTestCase(TestCase):
    """D1: when a provider is configured the observations come from the LLM
    (fed only the real fact pack); the deterministic math is unchanged."""

    def setUp(self):
        self.project = Project.objects.create(name='PUNDIT LLM Narrative Site')

    @patch("apps.common.ai_service.AIService._get_provider", return_value="openai")
    @patch("apps.common.ai_service.AIService._get_openai_model",
           return_value="gpt-4o-mini")
    def test_llm_observations_replace_deterministic(self, _model, _provider, _mock):
        test = PUNDITTest.objects.create(
            project=self.project, test_type='pulse_velocity',
            structural_element='COL-C24',
            path_length_mm=250.0, pulse_time_us=62.5,
        )
        record = PUNDITAdapter.analyze(test)

        test.refresh_from_db()
        # Deterministic math untouched by the LLM layer.
        self.assertAlmostEqual(test.velocity_km_s, 4.0)
        self.assertEqual(test.quality_grade, 'good')
        self.assertEqual(record.model_provider, 'openai')
        self.assertEqual(record.model_version, 'gpt-4o-mini')
        self.assertEqual(record.observations[0],
                         "Velocity 4.0 km/s places the element in the good band.")
        self.assertIn("Narrative synthesised by openai", record.reasoning_log)

    def test_unmeasured_test_never_calls_the_llm(self, _mock):
        # Empty measurements: honest deterministic record, no LLM narrative
        # is attempted (nothing to contextualise, nothing to fabricate).
        test = PUNDITTest.objects.create(
            project=self.project, test_type='pulse_velocity',
        )
        record = PUNDITAdapter.analyze(test)

        self.assertEqual(record.observations,
                         ['Insufficient measurements to compute an NDT result.'])
        self.assertEqual(record.model_provider, 'deterministic')


@patch("apps.common.ai_service.AIService.generate_structured_json",
       side_effect=AIProviderUnavailable("no provider configured"))
class PUNDITProjectAnalysisTestCase(TestCase):
    """D1: project-level roll-up over per-element means."""

    def setUp(self):
        self.project = Project.objects.create(name='PUNDIT Project Roll-up Site')
        self.good = PUNDITTest.objects.create(
            project=self.project, test_type='pulse_velocity',
            structural_element='COL-G01', floor='Ground Floor',
            path_length_mm=250.0, pulse_time_us=62.5,  # 4.0 km/s -> good
        )
        PUNDITReading.objects.create(
            test=self.good, point_label='A',
            path_length_mm=250.0, transit_time_us=62.5,
        )
        PUNDITReading.objects.create(
            test=self.good, point_label='B',
            path_length_mm=250.0, transit_time_us=60.0,
        )
        self.poor = PUNDITTest.objects.create(
            project=self.project, test_type='pulse_velocity',
            structural_element='COL-G02', floor='Ground Floor',
            path_length_mm=250.0, pulse_time_us=125.0,  # 2.0 km/s -> poor
        )

    def test_project_record_aggregates_and_recommends(self, _mock):
        record = PUNDITAdapter.analyze_project(self.project)

        self.assertEqual(record.analysis_type, 'pundit')
        self.assertEqual(record.model_provider, 'deterministic')
        # Worst element risk wins (2.0 km/s -> 'poor' -> high).
        self.assertEqual(record.risk_level, 'high')
        self.assertIn('1 graded good or better', record.observations[0])
        recs = [r['recommendation'] for r in record.recommendations]
        self.assertTrue(any('graded poor or very poor' in r for r in recs))
        # Per-element means appear in the reasoning log.
        self.assertIn('COL-G01 (Ground Floor)', record.reasoning_log)

    def test_analyze_project_endpoint_runs_both_layers(self, _mock):
        director = User.objects.create_superuser(
            username='pundit-dir@nexucon.com',
            email='pundit-dir@nexucon.com', password='Password123!')
        from rest_framework.test import APIClient
        api_client = APIClient()
        api_client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(director).access_token}')
        response = api_client.post(
            reverse('pundit-test-analyze-project'),
            {'project': str(self.project.id)}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['tests_analysed'], 2)
        self.assertIn('analysis_id', response.data)
        # Per-test means were persisted by the deterministic pass.
        self.good.refresh_from_db()
        self.assertAlmostEqual(self.good.velocity_km_s, (4.0 + 250 / 60) / 2, places=3)


# ======================================================================
# Field device registry CRUD + project-scoped listing
# ======================================================================

class FieldDeviceRegistryTestCase(DigitalEyeAPITestBase):
    def test_device_create_requires_serial_device_id(self):
        response = self.client.post(reverse('field-device-list'),
                                    {'device_type': 'gpr'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('device_id', response.data['errors'])
        self.assertFalse(response.data['success'])

    def test_device_create_rejects_unknown_device_type(self):
        response = self.client.post(
            reverse('field-device-list'),
            {'device_id': 'GPR-CART-77', 'device_type': 'teleporter'},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('device_type', response.data['errors'])

    def test_device_crud_and_audit(self):
        response = self.client.post(reverse('field-device-list'), {
            'device_id': 'GPR-CART-01', 'device_type': 'gpr',
            'name': 'Cart 1', 'assigned_project': str(self.project.id),
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        device_id = response.data['id']
        self.assertEqual(response.data['registered_by'], self.user.id)
        self.assertTrue(AuditEvent.objects.filter(
            action='digital_eye.device.register', resource_id=str(device_id)).exists())

        # Retrieve + partial update.
        detail = self.client.get(reverse('field-device-detail', kwargs={'pk': device_id}))
        self.assertEqual(detail.status_code, status.HTTP_200_OK)
        self.assertEqual(detail.data['device_id'], 'GPR-CART-01')

        patch = self.client.patch(reverse('field-device-detail', kwargs={'pk': device_id}),
                                  {'name': 'Cart 1 (recalibrated)'}, format='json')
        self.assertEqual(patch.status_code, status.HTTP_200_OK)
        self.assertEqual(patch.data['name'], 'Cart 1 (recalibrated)')

    def test_project_filter_hides_projects_outside_user_scope(self):
        # A plain user without a government profile / district has no scoped
        # projects: the ?project= filter must return nothing, never leak.
        FieldDevice.objects.create(device_id='SCOPED-DEV-1', device_type='gpr',
                                   assigned_project=self.project)
        other = User.objects.create_user(
            username='scoped_engineer@nexucon.com',
            email='scoped_engineer@nexucon.com',
            password='Password123!')
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(other).access_token}')

        response = self.client.get(
            reverse('field-device-list'), {'project': str(self.project.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])

    def test_assigned_project_must_be_inside_requesters_scope(self):
        other = User.objects.create_user(
            username='scoped_creator@nexucon.com',
            email='scoped_creator@nexucon.com',
            password='Password123!')
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(other).access_token}')
        response = self.client.post(reverse('field-device-list'), {
            'device_id': 'OUT-OF-SCOPE-1', 'device_type': 'pundit',
            'assigned_project': str(self.project.id),
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('assigned_project', response.data['errors'])


# ======================================================================
# Sensor-data file deletion
# ======================================================================

class SensorDataFileLifecycleTestCase(DigitalEyeAPITestBase):
    def test_delete_removes_row_and_stored_file(self):
        uploaded = SimpleUploadedFile('rinex-001.dat', b'GNSS RINEX observation bytes')
        response = self.client.post(
            reverse('sensor-file-list'),
            {'file': uploaded, 'file_type': 'gnss_rinex'}, format='multipart')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        file_id = response.data['id']
        stored_path = SensorDataFile.objects.get(pk=file_id).file.name

        delete = self.client.delete(reverse('sensor-file-detail', kwargs={'pk': file_id}))
        self.assertEqual(delete.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(SensorDataFile.objects.filter(pk=file_id).exists())
        self.assertFalse(SensorDataFile.objects.filter(file=stored_path).exists())


# ======================================================================
# GPR surveys — CRUD, workflow transitions and the analyze endpoint
# ======================================================================

class GPRSurveyAPITestCase(DigitalEyeAPITestBase):
    def _create_survey(self, **extra):
        payload = {'project': str(self.project.id), 'title': 'Zone B foundation scan'}
        payload.update(extra)
        response = self.client.post(reverse('gpr-survey-list'), payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return response.data['id']

    def test_survey_create_requires_project(self):
        response = self.client.post(reverse('gpr-survey-list'),
                                    {'title': 'No project'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('project', response.data['errors'])

    def test_survey_create_rejects_project_outside_scope(self):
        other = User.objects.create_user(
            username='gpr_scoped@nexucon.com', email='gpr_scoped@nexucon.com',
            password='Password123!')
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(other).access_token}')
        response = self.client.post(reverse('gpr-survey-list'), {
            'project': str(self.project.id), 'title': 'Foreign project scan',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('project', response.data['errors'])

    def test_survey_create_sets_operator_and_started_at(self):
        survey_id = self._create_survey(structural_element='FTN-B')
        survey = GPRSurvey.objects.get(pk=survey_id)
        self.assertEqual(survey.created_by, self.user)
        self.assertEqual(survey.operator, self.user)
        self.assertIsNotNone(survey.started_at)
        self.assertTrue(AuditEvent.objects.filter(
            action='digital_eye.gpr_survey.create', resource_id=str(survey_id)).exists())

    def test_status_transition_to_completed_stamps_completed_at(self):
        survey_id = self._create_survey()
        response = self.client.patch(
            reverse('gpr-survey-detail', kwargs={'pk': survey_id}),
            {'status': 'completed'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        survey = GPRSurvey.objects.get(pk=survey_id)
        self.assertEqual(survey.status, 'completed')
        self.assertIsNotNone(survey.completed_at)

    def test_analyze_endpoint_runs_adapter_and_completes_survey(self):
        survey_id = self._create_survey()
        GPRAnomaly.objects.create(
            survey_id=survey_id, anomaly_type='void', severity='high', depth_m=0.5)
        GPRAnomaly.objects.create(
            survey_id=survey_id, anomaly_type='rebar', severity='low',
            rebar_cover_mm=18.0)

        response = self.client.post(
            reverse('gpr-survey-analyze', kwargs={'pk': survey_id}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        survey = GPRSurvey.objects.get(pk=survey_id)
        self.assertEqual(survey.status, 'completed')
        self.assertIsNotNone(survey.completed_at)

        record = AIAnalysisRecord.objects.get(pk=response.data['analysis_id'])
        self.assertEqual(response.data['risk_level'], record.risk_level)
        self.assertEqual(response.data['risk_score'], record.risk_score)
        self.assertEqual(response.data['observations'], record.observations)
        self.assertEqual(record.analysis_type, 'gpr')
        self.assertTrue(AuditEvent.objects.filter(
            action='digital_eye.gpr_survey.analyze',
            resource_id=str(survey_id)).exists())

    def test_analyze_without_anomalies_reports_baseline(self):
        survey_id = self._create_survey()
        response = self.client.post(
            reverse('gpr-survey-analyze', kwargs={'pk': survey_id}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['risk_level'], 'info')
        self.assertIsNone(response.data['risk_score'])


class GPRAnomalyAPITestCase(DigitalEyeAPITestBase):
    def setUp(self):
        super().setUp()
        self.survey = GPRSurvey.objects.create(project=self.project,
                                               title='Anomaly scan')

    def test_anomaly_create_and_scoped_listing(self):
        response = self.client.post(reverse('gpr-anomaly-list'), {
            'survey': str(self.survey.id), 'anomaly_type': 'void',
            'severity': 'medium', 'depth_m': 0.8,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        anomaly_id = response.data['id']
        self.assertTrue(AuditEvent.objects.filter(
            action='digital_eye.gpr_anomaly.create',
            resource_id=str(anomaly_id)).exists())

        listing = self.client.get(reverse('gpr-anomaly-list'),
                                  {'survey': str(self.survey.id)})
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(len(listing.data), 1)

    def test_rebar_without_cover_gets_flagged_description_not_invented_cover(self):
        response = self.client.post(reverse('gpr-anomaly-list'), {
            'survey': str(self.survey.id), 'anomaly_type': 'rebar',
            'severity': 'low',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['description'],
                         'Rebar detected; cover not measured.')
        self.assertIsNone(response.data['rebar_cover_mm'])

    def test_anomaly_create_rejects_invalid_severity(self):
        response = self.client.post(reverse('gpr-anomaly-list'), {
            'survey': str(self.survey.id), 'anomaly_type': 'void',
            'severity': 'catastrophic',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('severity', response.data['errors'])

    def test_list_is_scoped_to_users_projects(self):
        # A user with no scoped projects sees no anomalies at all.
        GPRAnomaly.objects.create(survey=self.survey, anomaly_type='void')
        other = User.objects.create_user(
            username='anomaly_scoped@nexucon.com',
            email='anomaly_scoped@nexucon.com', password='Password123!')
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(other).access_token}')

        listing = self.client.get(reverse('gpr-anomaly-list'))
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(listing.data, [])

        detail = self.client.get(
            reverse('gpr-anomaly-detail', kwargs={'pk': str(self.survey.anomalies.first().id)}))
        self.assertEqual(detail.status_code, status.HTTP_404_NOT_FOUND)


# ======================================================================
# GNSS surveys — benchmarks, boundary points, projection & variance
# ======================================================================

class GnssSurveyAPITestCase(DigitalEyeAPITestBase):
    def setUp(self):
        super().setUp()
        self.survey = GnssSurvey.objects.create(project=self.project,
                                                title='Setting out, grid A')

    def test_survey_create_via_api(self):
        response = self.client.post(reverse('gnss-survey-list'), {
            'project': str(self.project.id), 'title': 'Perimeter traverse',
            'method': 'rtk', 'fix_quality': 'fixed',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        survey = GnssSurvey.objects.get(pk=response.data['id'])
        self.assertEqual(survey.method, 'rtk')
        self.assertEqual(survey.created_by, self.user)
        self.assertTrue(AuditEvent.objects.filter(
            action='digital_eye.gnss_survey.create').exists())

    def test_benchmark_create_auto_projects_to_utm(self):
        response = self.client.post(reverse('gnss-benchmark-list'), {
            'survey': str(self.survey.id), 'point_id': 'BM-001',
            'latitude': 6.5244, 'longitude': 3.3792,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertAlmostEqual(response.data['easting'], 541924.30, places=1)
        self.assertAlmostEqual(response.data['northing'], 721189.22, places=1)
        self.assertEqual(response.data['utm_zone'], '31N')

    def test_boundary_point_create_auto_projects_to_utm(self):
        response = self.client.post(reverse('gnss-boundary-point-list'), {
            'survey': str(self.survey.id), 'sequence': 1,
            'latitude': 6.5244, 'longitude': 3.3792,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertAlmostEqual(response.data['easting'], 541924.30, places=1)
        self.assertAlmostEqual(response.data['northing'], 721189.22, places=1)

    def test_project_survey_projects_points_and_registers_evidence(self):
        benchmark = GnssBenchmark.objects.create(
            survey=self.survey, point_id='BM-001',
            latitude=6.5244, longitude=3.3792)
        GnssBoundaryPoint.objects.create(
            survey=self.survey, sequence=1, latitude=6.5244, longitude=3.3792)
        GnssBoundaryPoint.objects.create(
            survey=self.survey, sequence=2, latitude=6.53, longitude=3.38)

        response = self.client.post(
            reverse('gnss-survey-project-survey', kwargs={'pk': str(self.survey.id)}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.assertEqual(response.data['projected_points'], 3)
        self.assertIn('evidence_reference', response.data)
        benchmark.refresh_from_db()
        self.assertAlmostEqual(benchmark.easting, 541924.30, places=1)
        self.assertTrue(EvidenceRecord.objects.filter(
            source_model='digital_eye.GnssSurvey',
            source_id=str(self.survey.id)).exists())
        self.assertTrue(AuditEvent.objects.filter(
            action='digital_eye.gnss_survey.project').exists())

    def test_project_survey_computes_design_variance(self):
        benchmark = GnssBenchmark.objects.create(
            survey=self.survey, point_id='BM-001',
            latitude=6.5244, longitude=3.3792)
        expected_e, expected_n = GNSSProjection.geographic_to_utm(6.5244, 3.3792)

        response = self.client.post(
            reverse('gnss-survey-project-survey', kwargs={'pk': str(self.survey.id)}),
            {'design_points': {
                'BM-001': {'easting': expected_e - 1.0, 'northing': expected_n + 2.0},
                'BM-999': {'easting': 0.0, 'northing': 0.0},  # no such point
            }}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        summary = response.data['variance_summary']
        self.assertEqual(summary['points_compared'], 1)
        point = summary['points'][0]
        self.assertAlmostEqual(point['delta_easting_m'], 1.0, places=3)
        self.assertAlmostEqual(point['delta_northing_m'], -2.0, places=3)
        self.assertAlmostEqual(point['radial_m'], (1.0 ** 2 + 2.0 ** 2) ** 0.5, places=3)

        # Persisted on the survey record.
        self.survey.refresh_from_db()
        self.assertEqual(self.survey.variance_summary['points_compared'], 1)

    def test_project_survey_without_design_points_has_no_variance(self):
        response = self.client.post(
            reverse('gnss-survey-project-survey', kwargs={'pk': str(self.survey.id)}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data['variance_summary'])
        self.survey.refresh_from_db()
        self.assertIsNone(self.survey.variance_summary)

    def test_gnss_lists_are_scoped_to_users_projects(self):
        GnssBenchmark.objects.create(
            survey=self.survey, point_id='BM-001',
            latitude=6.5244, longitude=3.3792)
        other = User.objects.create_user(
            username='gnss_scoped@nexucon.com', email='gnss_scoped@nexucon.com',
            password='Password123!')
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(other).access_token}')

        for url_name in ('gnss-survey-list', 'gnss-benchmark-list',
                         'gnss-boundary-point-list'):
            listing = self.client.get(reverse(url_name))
            self.assertEqual(listing.status_code, status.HTTP_200_OK)
            self.assertEqual(listing.data, [],
                             msg=f'{url_name} must be project-scoped')


# ======================================================================
# BIM element mappings + live streams
# ======================================================================

class BIMMappingAndLiveStreamTestCase(DigitalEyeAPITestBase):
    def test_bim_element_mapping_create_and_list(self):
        response = self.client.post(reverse('bim-element-list'), {
            'project': str(self.project.id), 'bim_guid': '3rNg7Ib9P5$wfO4JiGtdVn',
            'element_id': 'COL-C24', 'element_name': 'Column C24',
            'element_type': 'IfcColumn', 'source': 'manual',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(AuditEvent.objects.filter(
            action='digital_eye.bim_element.create').exists())

        listing = self.client.get(reverse('bim-element-list'),
                                  {'project': str(self.project.id)})
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(len(listing.data), 1)
        self.assertEqual(listing.data[0]['element_id'], 'COL-C24')

    def test_bim_element_mapping_requires_project(self):
        response = self.client.post(reverse('bim-element-list'), {
            'bim_guid': '3rNg7Ib9P5$wfO4JiGtdVn', 'element_id': 'COL-C24',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('project', response.data['errors'])

    def test_live_stream_create_adopts_mapped_element_coordinates(self):
        element = BIMElementMapping.objects.create(
            project=self.project, bim_guid='3rNg7Ib9P5$wfO4JiGtdVn',
            element_id='COL-C24',
            coordinates={'x': 12.5, 'y': 3.0, 'z': 8.0})
        response = self.client.post(reverse('live-stream-list'), {
            'project': str(self.project.id), 'name': 'Zone B camera',
            'mapped_element_id': str(element.id),
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['mapped_coordinates'],
                         {'x': 12.5, 'y': 3.0, 'z': 8.0})
        self.assertEqual(response.data['mapped_element_label'], 'COL-C24')

    def test_live_stream_check_records_operator_report(self):
        stream = LiveStream.objects.create(project=self.project,
                                           name='Gate camera')
        response = self.client.post(
            reverse('live-stream-check', kwargs={'pk': str(stream.id)}),
            {'status': 'error', 'error': 'RTSP handshake timed out'},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        stream.refresh_from_db()
        self.assertEqual(stream.status, 'error')
        self.assertEqual(stream.last_error, 'RTSP handshake timed out')
        self.assertIsNotNone(stream.last_checked_at)

    def test_live_stream_check_rejects_unknown_status(self):
        stream = LiveStream.objects.create(project=self.project,
                                           name='Gate camera')
        response = self.client.post(
            reverse('live-stream-check', kwargs={'pk': str(stream.id)}),
            {'status': 'broadcasting'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('status', response.data['detail'])


# ======================================================================
# IFC import (credential-free BIM GUID mapping path)
# ======================================================================

IFC_STEP_CONTENT = b"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('test-model.ifc','2026-09-03T00:00:00Z',(''),(''),'','');
FILE_SCHEMA(('NOT-A-REAL-SCHEMA'));
ENDSEC;
DATA;
#1=IFCCOLUMN('3rNg7Ib9P5$wfO4JiGtdVn',$,'COL-C24','Column',$,$,$,$,.COLUMN.);
#2=IFCBEAM('2$Ov3Xfb9P5wfO4JiGtdVn',$,'BM-B12','Beam',$,$,$,$,.BEAM.);
ENDSEC;
END-ISO-10303-21;
"""

# A schema-valid IFC4 file whose column carries a property set, a quantity
# set, a Tag, a family type and a material layer set; its placements
# accumulate to world coordinates (100,200,300 twice = 200,400,600).
# Attribute order is IFC4: GlobalId, OwnerHistory, Name, Description,
# ObjectType, ObjectPlacement, Representation, Tag, PredefinedType.
IFC4_WITH_PSETS = b"""ISO-10303-21;
HEADER;
FILE_DESCRIPTION((''),'2;1');
FILE_NAME('test.ifc','2026-09-06T00:00:00Z',(''),(''),'','');
FILE_SCHEMA(('IFC4'));
ENDSEC;
DATA;
#1=IFCPROJECT('0YvctVUKr0kugbFTf53O9L',$,'Test',$,$,$,$,(#20),#5);
#5=IFCUNITASSIGNMENT((#6));
#6=IFCSIUNIT(*,.LENGTHUNIT.,$,.METRE.);
#7=IFCCARTESIANPOINT((100.,200.,300.));
#8=IFCDIRECTION((0.,0.,1.));
#9=IFCDIRECTION((1.,0.,0.));
#10=IFCAXIS2PLACEMENT3D(#7,#8,#9);
#20=IFCGEOMETRICREPRESENTATIONCONTEXT($,'Model',3,1.E-05,#10,$);
#30=IFCLOCALPLACEMENT($,#10);
#31=IFCLOCALPLACEMENT(#30,#10);
#40=IFCCOLUMN('3rNg7Ib9P5$wfO4JiGtdVn',$,'COL-C24','Column',$,#31,$,'E-24',$);
#100=IFCPROPERTYSET('1YAoxVd4zCRQ8RrOzz4cRk',$,'Pset_ConcreteElementCommon',$,(#101,#102));
#101=IFCPROPERTYSINGLEVALUE('Mark',$,IFCLABEL('COL-C24'),$);
#102=IFCPROPERTYSINGLEVALUE('LoadBearing',$,IFCBOOLEAN(.T.),$);
#103=IFCRELDEFINESBYPROPERTIES('2YAoxVd4zCRQ8RrOzz4cRk',$,$,$,(#40),#100);
#110=IFCELEMENTQUANTITY('3YAoxVd4zCRQ8RrOzz4cRk',$,'Qto_ColumnBaseQuantities',$,$,(#111));
#111=IFCQUANTITYVOLUME('NetVolume',$,$,0.5);
#112=IFCRELDEFINESBYPROPERTIES('4YAoxVd4zCRQ8RrOzz4cRk',$,$,$,(#40),#110);
#120=IFCCOLUMNTYPE('5YvctVUKr0kugbFTf53O9L',$,'RC Column 400x400',$,$,$,$,$,$,.COLUMN.);
#121=IFCRELDEFINESBYTYPE('6YvctVUKr0kugbFTf53O9L',$,$,$,(#40),#120);
#130=IFCMATERIAL('Concrete - Cast-in-Place Concrete');
#131=IFCMATERIALLAYERSET((#132),'Column make-up');
#132=IFCMATERIALLAYER(#130,0.2,$,$,$,$);
#133=IFCRELASSOCIATESMATERIAL('7YvctVUKr0kugbFTf53O9L',$,$,$,(#40),#131);
ENDSEC;
END-ISO-10303-21;
"""


class BIMElementImportTestCase(DigitalEyeAPITestBase):
    def test_import_rejects_project_outside_scope(self):
        other = User.objects.create_user(
            username='ifc_scoped@nexucon.com', email='ifc_scoped@nexucon.com',
            password='Password123!')
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(other).access_token}')
        response = self.client.post(
            reverse('bim-element-import-ifc'),
            {'project': str(self.project.id),
             'file': SimpleUploadedFile('model.ifc', IFC_STEP_CONTENT)},
            format='multipart')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_import_extracts_guids_and_upserts_mappings(self):
        def _import():
            return self.client.post(
                reverse('bim-element-import-ifc'),
                {'project': str(self.project.id),
                 'file': SimpleUploadedFile('model.ifc', IFC_STEP_CONTENT)},
                format='multipart')

        first = _import()
        self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)
        self.assertEqual(first.data['elements_extracted'], 2)
        self.assertEqual(first.data['mappings_created'], 2)
        self.assertEqual(first.data['mappings_updated'], 0)

        # Re-import updates the same GUIDs instead of duplicating them.
        second = _import()
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(second.data['mappings_created'], 0)
        self.assertEqual(second.data['mappings_updated'], 2)

        self.assertEqual(BIMElementMapping.objects.filter(
            project=self.project).count(), 2)
        mapping = BIMElementMapping.objects.get(bim_guid='3rNg7Ib9P5$wfO4JiGtdVn')
        self.assertEqual(mapping.element_id, 'COL-C24')
        self.assertEqual(mapping.element_type, 'IFCCOLUMN')
        self.assertEqual(mapping.source, 'ifc_upload')

    def test_import_captures_full_ifc_property_sets_and_world_coordinates(self):
        """Every property-set AND quantity-set value the model carries is
        stored on the mapping, keyed 'PsetOrQtoName.Key' — these are what
        the element-properties panel attributes to each element. Placement
        coordinates are WORLD coordinates (accumulated up the spatial
        hierarchy), not the local 0,0,0 Revit exports typically use."""
        response = self.client.post(
            reverse('bim-element-import-ifc'),
            {'project': str(self.project.id),
             'file': SimpleUploadedFile('model.ifc', IFC4_WITH_PSETS)},
            format='multipart')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        mapping = BIMElementMapping.objects.get(
            bim_guid='3rNg7Ib9P5$wfO4JiGtdVn')
        self.assertEqual(mapping.element_id, 'COL-C24')
        self.assertEqual(
            mapping.properties['Pset_ConcreteElementCommon.Mark'], 'COL-C24')
        self.assertEqual(
            mapping.properties['Pset_ConcreteElementCommon.LoadBearing'], 'True')
        self.assertEqual(
            mapping.properties['Qto_ColumnBaseQuantities.NetVolume'], '0.5')
        # Direct attributes/associations are captured alongside (and survive
        # even when a translation carries no property sets at all — e.g.
        # Autodesk APS RVT->IFC exports geometry only).
        self.assertEqual(mapping.properties['Tag'], 'E-24')
        self.assertEqual(mapping.properties['ElementType'], 'RC Column 400x400')
        # The category stays the clean IFC class name — the type ENTITY
        # (whose str() is a raw STEP line like "#64=IfcSlabType(...)") must
        # only ever land in the ElementType property, never element_type.
        self.assertEqual(mapping.element_type, 'IfcColumn')
        # Layer thickness is normalised to mm whatever unit the file uses
        # (this file is in metres, the layer is 0.2 m = 200 mm).
        self.assertEqual(
            mapping.properties['Material'],
            'Concrete - Cast-in-Place Concrete (200 mm)')
        # ifcopenshell's bookkeeping 'id' key is not a model property.
        self.assertFalse(
            [k for k in mapping.properties if k.endswith('.id')])
        self.assertEqual(mapping.coordinates['source'], 'ifc_world_placement')
        self.assertEqual(mapping.coordinates['x'], 200.0)
        self.assertEqual(mapping.coordinates['y'], 400.0)
        self.assertEqual(mapping.coordinates['z'], 600.0)

    def test_import_rejects_unsupported_format(self):
        response = self.client.post(
            reverse('bim-element-import-ifc'),
            {'project': str(self.project.id),
             'file': SimpleUploadedFile('model.pdf', b'%PDF-1.4 not a model')},
            format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Unsupported model format', response.data['detail'])
        self.assertEqual(
            BIMElementMapping.objects.filter(project=self.project).count(), 0)

    def test_import_rvt_translates_via_aps_and_upserts(self):
        """A Revit upload is routed through the APS translation pipeline and
        the translated IFC's elements are upserted (no fabricated data)."""
        import os
        import tempfile

        def fake_ensure_ifc(rvt_path):
            # The raw .rvt bytes must reach the translator untouched.
            self.assertTrue(rvt_path.lower().endswith('.rvt'))
            with open(rvt_path, 'rb') as f:
                self.assertEqual(f.read(), b'fake-rvt-bytes')
            fd, ifc_path = tempfile.mkstemp(suffix='.ifc')
            with os.fdopen(fd, 'wb') as f:
                f.write(IFC_STEP_CONTENT)
            return ifc_path

        with mock.patch('apps.processing.bim_geometry.ensure_ifc',
                        side_effect=fake_ensure_ifc):
            response = self.client.post(
                reverse('bim-element-import-ifc'),
                {'project': str(self.project.id),
                 'file': SimpleUploadedFile('model.rvt', b'fake-rvt-bytes')},
                format='multipart')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(response.data['translated_from_rvt'])
        self.assertEqual(response.data['elements_extracted'], 2)
        self.assertEqual(response.data['mappings_created'], 2)
        self.assertEqual(BIMElementMapping.objects.filter(
            project=self.project).count(), 2)

    def test_import_rvt_without_aps_credentials_is_honest(self):
        with mock.patch(
                'apps.processing.bim_geometry.ensure_ifc',
                side_effect=ValueError(
                    'Autodesk APS credentials missing. '
                    'Cannot parse proprietary .rvt file.')):
            response = self.client.post(
                reverse('bim-element-import-ifc'),
                {'project': str(self.project.id),
                 'file': SimpleUploadedFile('model.rvt', b'fake-rvt-bytes')},
                format='multipart')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # The error must tell the operator exactly what to do instead of
        # failing opaquely.
        self.assertIn('AUTODESK_CLIENT_SECRET', response.data['detail'])
        self.assertIn('export an IFC', response.data['detail'])
        self.assertEqual(
            BIMElementMapping.objects.filter(project=self.project).count(), 0)

    def test_import_rvt_translation_failure_is_honest(self):
        with mock.patch(
                'apps.processing.bim_geometry.ensure_ifc',
                side_effect=Exception('APS Translation Failed')):
            response = self.client.post(
                reverse('bim-element-import-ifc'),
                {'project': str(self.project.id),
                 'file': SimpleUploadedFile('model.rvt', b'fake-rvt-bytes')},
                format='multipart')
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)
        self.assertIn('could not translate', response.data['detail'])
        self.assertEqual(
            BIMElementMapping.objects.filter(project=self.project).count(), 0)

    # ----- B5/B6: import-card status + platform file picker -----

    def _import_ifc(self, name='model.ifc'):
        return self.client.post(
            reverse('bim-element-import-ifc'),
            {'project': str(self.project.id),
             'file': SimpleUploadedFile(name, IFC_STEP_CONTENT)},
            format='multipart')

    def _status(self):
        return self.client.get(
            reverse('bim-element-import-ifc'), {'project': str(self.project.id)})

    def test_import_status_is_honest_before_any_import(self):
        response = self._status()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data['currently_imported'])
        self.assertEqual(response.data['stored_files'], [])

    def test_import_keeps_model_file_for_reimport_and_status_lists_it(self):
        """The uploaded model file is kept on the platform (checksum-deduped
        per project) and the status endpoint reports the genuine file name of
        the model currently imported — the import card shows exactly what was
        uploaded, so a mismatched project/model is visible at a glance."""
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=tmp):
            try:
                first = self._import_ifc(name='proposed stacking area.ifc')
                self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)
                kept = SensorDataFile.objects.get(
                    project=self.project, file_type='bim_model')
                self.assertEqual(kept.file_name, 'proposed stacking area.ifc')
                self.assertEqual(kept.sha256_checksum,
                                 hashlib.sha256(IFC_STEP_CONTENT).hexdigest())
                self.assertEqual(kept.file_size_bytes, len(IFC_STEP_CONTENT))
                self.assertEqual(first.data['stored_file'], str(kept.id))
                # A real copy of the model bytes is kept in storage.
                self.assertTrue(kept.file.name)
                self.assertEqual(kept.file.size, len(IFC_STEP_CONTENT))

                # Re-importing the same content must not duplicate the kept file.
                second = self._import_ifc(name='renamed copy.ifc')
                self.assertEqual(second.status_code, status.HTTP_201_CREATED)
                self.assertEqual(SensorDataFile.objects.filter(
                    project=self.project, file_type='bim_model').count(), 1)
                self.assertEqual(second.data['stored_file'], str(kept.id))

                status_response = self._status()
                self.assertEqual(status_response.status_code, status.HTTP_200_OK)
                self.assertEqual(
                    status_response.data['currently_imported']['source_file'],
                    'renamed copy.ifc')
                self.assertFalse(
                    status_response.data['currently_imported']['translated_from_rvt'])
                self.assertEqual(len(status_response.data['stored_files']), 1)
                self.assertEqual(
                    status_response.data['stored_files'][0]['file_name'],
                    'proposed stacking area.ifc')
                self.assertEqual(
                    status_response.data['stored_files'][0]['sha256_checksum'],
                    kept.sha256_checksum)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_import_from_stored_model_file_reimports_without_a_new_copy(self):
        """The picker's platform path: POSTing a stored model's id re-runs the
        full import from the kept file (extract → mappings → geometry)."""
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp()
        with override_settings(MEDIA_ROOT=tmp):
            try:
                first = self._import_ifc()
                self.assertEqual(first.status_code, status.HTTP_201_CREATED, first.data)
                kept_id = first.data['stored_file']
                self.assertTrue(kept_id)
                # Prove the re-import genuinely re-extracts from the file.
                BIMElementMapping.objects.filter(project=self.project).delete()

                response = self.client.post(
                    reverse('bim-element-import-ifc'),
                    {'project': str(self.project.id), 'sensor_file': kept_id},
                    format='json')
                self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
                self.assertEqual(response.data['file'], 'model.ifc')
                self.assertEqual(response.data['elements_extracted'], 2)
                self.assertEqual(response.data['mappings_created'], 2)
                self.assertEqual(
                    BIMElementMapping.objects.filter(project=self.project).count(), 2)
                # No second platform copy is made from a platform copy.
                self.assertEqual(SensorDataFile.objects.filter(
                    project=self.project, file_type='bim_model').count(), 1)
                self.assertIsNone(response.data['stored_file'])
            finally:
                shutil.rmtree(tmp, ignore_errors=True)

    def test_import_from_stored_file_rejects_non_model_files(self):
        response = self.client.post(
            reverse('bim-element-import-ifc'),
            {'project': str(self.project.id), 'sensor_file': 'not-a-uuid'},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        # A sensor file of another type (e.g. a photo) can never masquerade
        # as a model file for the import path.
        photo = SensorDataFile.objects.create(
            project=self.project, file_type='photo', file_name='site.ifc')
        response = self.client.post(
            reverse('bim-element-import-ifc'),
            {'project': str(self.project.id), 'sensor_file': str(photo.id)},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            BIMElementMapping.objects.filter(project=self.project).count(), 0)


# ======================================================================
# 3D model preview geometry (stored at import time, served verbatim)
# ======================================================================

PREVIEW_MESH = [{
    'guid': '3rNg7Ib9P5$wfO4JiGtdVn', 'name': 'COL-C24', 'type': 'IfcColumn',
    'verts': [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
    'faces': [0, 1, 2],
}]


class FakeArray(list):
    """list that quacks like a numpy array for reshape(-1).tolist()."""

    def reshape(self, _dims):
        return self

    def tolist(self):
        return list(self)


FakeVerts = FakeArray([0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0])
FakeFaces = FakeArray([0, 1, 2])


class BIMModelGeometryTestCase(DigitalEyeAPITestBase):
    """The import stores tessellated preview meshes; the geometry endpoint
    serves them verbatim. Tessellation failure degrades to an honest empty
    preview — the metadata import is the primary product."""

    def _import_ifc(self, name='model.ifc', content=IFC_STEP_CONTENT):
        return self.client.post(
            reverse('bim-element-import-ifc'),
            {'project': str(self.project.id),
             'file': SimpleUploadedFile(name, content)},
            format='multipart')

    def test_import_stores_preview_geometry(self):
        with mock.patch('apps.digital_eye.bim_preview.tessellate_ifc',
                        return_value=[{
                            'guid': '3rNg7Ib9P5$wfO4JiGtdVn', 'name': 'COL-C24',
                            'type': 'IfcColumn',
                            'verts': FakeVerts, 'faces': FakeFaces,
                            'bbox': (None, None),
                        }]):
            response = self._import_ifc()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['preview_elements'], 1)

        geometry = BIMModelGeometry.objects.get(project=self.project)
        self.assertEqual(geometry.source_file, 'model.ifc')
        self.assertFalse(geometry.translated_from_rvt)
        self.assertEqual(geometry.element_count, 1)
        self.assertEqual(geometry.elements, PREVIEW_MESH)

    def test_tessellation_failure_still_imports_with_honest_empty_preview(self):
        with mock.patch('apps.digital_eye.bim_preview.tessellate_ifc',
                        side_effect=RuntimeError('ifcopenshell cannot open')):
            response = self._import_ifc()
        # The metadata import is the primary product — it must succeed.
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data['preview_elements'], 0)
        self.assertIn('preview_detail', response.data)

        geometry = BIMModelGeometry.objects.get(project=self.project)
        self.assertEqual(geometry.element_count, 0)
        self.assertEqual(geometry.elements, [])
        self.assertEqual(
            BIMElementMapping.objects.filter(project=self.project).count(), 2)

    def test_rvt_import_stores_geometry_with_rvt_flag(self):
        import os
        import tempfile

        def fake_ensure_ifc(rvt_path):
            fd, ifc_path = tempfile.mkstemp(suffix='.ifc')
            with os.fdopen(fd, 'wb') as f:
                f.write(IFC_STEP_CONTENT)
            return ifc_path

        with mock.patch('apps.processing.bim_geometry.ensure_ifc',
                        side_effect=fake_ensure_ifc), \
             mock.patch('apps.digital_eye.bim_preview.tessellate_ifc',
                        return_value=[{
                            'guid': '3rNg7Ib9P5$wfO4JiGtdVn', 'name': 'COL-C24',
                            'type': 'IfcColumn',
                            'verts': FakeVerts, 'faces': FakeFaces,
                            'bbox': (None, None),
                        }]):
            response = self._import_ifc(name='model.rvt', content=b'fake-rvt-bytes')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(response.data['translated_from_rvt'])
        self.assertEqual(response.data['preview_elements'], 1)

        geometry = BIMModelGeometry.objects.get(project=self.project)
        self.assertTrue(geometry.translated_from_rvt)
        self.assertEqual(geometry.element_count, 1)

    def test_reimport_replaces_preview_geometry(self):
        first_mesh = [{'guid': 'g1', 'name': 'E1', 'type': 'IfcColumn',
                       'verts': FakeVerts, 'faces': FakeFaces, 'bbox': (None, None)}]
        second_mesh = [{'guid': 'g1', 'name': 'E1', 'type': 'IfcColumn',
                        'verts': FakeVerts, 'faces': FakeFaces, 'bbox': (None, None)},
                       {'guid': 'g2', 'name': 'E2', 'type': 'IfcSlab',
                        'verts': FakeVerts, 'faces': FakeFaces, 'bbox': (None, None)}]

        with mock.patch('apps.digital_eye.bim_preview.tessellate_ifc',
                        return_value=first_mesh):
            self._import_ifc(name='first.ifc')
        with mock.patch('apps.digital_eye.bim_preview.tessellate_ifc',
                        return_value=second_mesh):
            self._import_ifc(name='second.ifc')

        # OneToOne — one row per project, replaced by the latest import.
        self.assertEqual(BIMModelGeometry.objects.filter(
            project=self.project).count(), 1)
        geometry = BIMModelGeometry.objects.get(project=self.project)
        self.assertEqual(geometry.source_file, 'second.ifc')
        self.assertEqual(geometry.element_count, 2)

    def test_geometry_endpoint_serves_stored_elements_verbatim(self):
        with mock.patch('apps.digital_eye.bim_preview.tessellate_ifc',
                        return_value=[{
                            'guid': '3rNg7Ib9P5$wfO4JiGtdVn', 'name': 'COL-C24',
                            'type': 'IfcColumn',
                            'verts': FakeVerts, 'faces': FakeFaces,
                            'bbox': (None, None),
                        }]):
            self._import_ifc()

        response = self.client.get(
            reverse('bim-element-geometry'),
            {'project': str(self.project.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data['project'], str(self.project.id))
        self.assertEqual(response.data['source_file'], 'model.ifc')
        self.assertEqual(response.data['element_count'], 1)
        self.assertEqual(response.data['elements'], PREVIEW_MESH)

    def test_geometry_endpoint_requires_authentication(self):
        self.client.credentials()  # drop the bearer token
        response = self.client.get(
            reverse('bim-element-geometry'),
            {'project': str(self.project.id)})
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_geometry_endpoint_404_without_imported_model(self):
        response = self.client.get(
            reverse('bim-element-geometry'),
            {'project': str(self.project.id)})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertIn('No BIM model imported', response.data['detail'])

    def test_geometry_endpoint_hides_out_of_scope_projects(self):
        other = User.objects.create_user(
            username='geo_scoped@nexucon.com', email='geo_scoped@nexucon.com',
            password='Password123!')
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(other).access_token}')
        response = self.client.get(
            reverse('bim-element-geometry'),
            {'project': str(self.project.id)})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


# ======================================================================
# Trimble Connect integration endpoints
# ======================================================================

TRIMBLE_UNCONFIGURED = override_settings(
    TRIMBLE_CLIENT_ID='', TRIMBLE_CLIENT_SECRET='', TRIMBLE_REDIRECT_URI='',
)
TRIMBLE_CONFIGURED = override_settings(
    TRIMBLE_CLIENT_ID='test-client-id', TRIMBLE_CLIENT_SECRET='test-client-secret',
    TRIMBLE_REDIRECT_URI='https://app.example.test/trimble/callback',
    TRIMBLE_AUTHORIZE_URL='https://auth.example.test/authorize',
    TRIMBLE_TOKEN_URL='https://auth.example.test/token',
)


class TrimbleConnectionPermissionsTestCase(DigitalEyeAPITestBase):
    """Only Directors (or superusers) manage Trimble connections."""

    def setUp(self):
        super().setUp()
        self.engineer = User.objects.create_user(
            username='trimble_engineer@nexucon.com',
            email='trimble_engineer@nexucon.com', password='Password123!')
        self.connection = TrimbleConnection.objects.create(name='Site BIM link')

    def _as_engineer(self):
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(self.engineer).access_token}')

    def test_non_director_cannot_create_connection(self):
        self._as_engineer()
        response = self.client.post(reverse('trimble-connection-list'),
                                    {'name': 'Rogue link'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_non_director_cannot_delete_connection(self):
        self._as_engineer()
        response = self.client.delete(
            reverse('trimble-connection-detail', kwargs={'pk': str(self.connection.id)}))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_director_can_create_and_delete_connection(self):
        response = self.client.post(reverse('trimble-connection-list'),
                                    {'name': 'Official link'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        connection_id = response.data['id']
        self.assertTrue(TrimbleConnection.objects.filter(
            created_by=self.user, pk=connection_id).exists())

        delete = self.client.delete(
            reverse('trimble-connection-detail', kwargs={'pk': connection_id}))
        self.assertEqual(delete.status_code, status.HTTP_204_NO_CONTENT)

    def test_authorize_without_credentials_returns_503(self):
        with TRIMBLE_UNCONFIGURED:
            response = self.client.post(
                reverse('trimble-connection-authorize',
                        kwargs={'pk': str(self.connection.id)}))
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertIn('TRIMBLE_CLIENT_ID', str(response.data['detail']))
        # Nothing was persisted as if the handshake had started.
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, 'disconnected')

    def test_authorize_builds_pkce_authorization_url(self):
        with TRIMBLE_CONFIGURED:
            response = self.client.post(
                reverse('trimble-connection-authorize',
                        kwargs={'pk': str(self.connection.id)}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        url = response.data['authorization_url']
        self.assertIn('https://auth.example.test/authorize?', url)
        self.assertIn('code_challenge_method=S256', url)
        self.assertIn('state=', url)

        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, 'pending_authorization')
        self.assertTrue(self.connection.pkce_verifier)

    def test_callback_requires_authorization_code(self):
        response = self.client.post(
            reverse('trimble-connection-callback',
                    kwargs={'pk': str(self.connection.id)}), {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('code', response.data['detail'])

    def test_callback_without_credentials_returns_503(self):
        # Simulate a connection mid-handshake: the PKCE verifier is set but
        # the OAuth client credentials are (or have become) unconfigured.
        self.connection.pkce_verifier = 'stored-verifier'
        self.connection.save(update_fields=['pkce_verifier'])
        with TRIMBLE_UNCONFIGURED:
            response = self.client.post(
                reverse('trimble-connection-callback',
                        kwargs={'pk': str(self.connection.id)}),
                {'code': 'auth-code-123'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)

    def test_callback_exchanges_code_for_tokens(self):
        with TRIMBLE_CONFIGURED:
            self.client.post(
                reverse('trimble-connection-authorize',
                        kwargs={'pk': str(self.connection.id)}))

            token_response = mock.Mock(status_code=200)
            token_response.json.return_value = {
                'access_token': 'at-123', 'refresh_token': 'rt-456',
                'expires_in': 3600, 'scope': 'projects',
            }
            with mock.patch('integrations.trimble.client.requests.post',
                            return_value=token_response):
                response = self.client.post(
                    reverse('trimble-connection-callback',
                            kwargs={'pk': str(self.connection.id)}),
                    {'code': 'auth-code-123'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, 'connected')
        self.assertEqual(self.connection.access_token, 'at-123')
        self.assertEqual(self.connection.refresh_token, 'rt-456')
        self.assertFalse(self.connection.pkce_verifier)  # single-use
        self.assertTrue(AuditEvent.objects.filter(
            action='trimble.authorize.complete').exists())

    def test_callback_reports_token_exchange_failure(self):
        with TRIMBLE_CONFIGURED:
            self.client.post(
                reverse('trimble-connection-authorize',
                        kwargs={'pk': str(self.connection.id)}))
            token_response = mock.Mock(status_code=400)
            token_response.text = 'invalid_grant'
            with mock.patch('integrations.trimble.client.requests.post',
                            return_value=token_response):
                response = self.client.post(
                    reverse('trimble-connection-callback',
                            kwargs={'pk': str(self.connection.id)}),
                    {'code': 'expired-code'}, format='json')

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.status, 'error')
        self.assertIn('invalid_grant', self.connection.last_error)

    def test_health_check_without_credentials_is_honestly_unhealthy(self):
        with TRIMBLE_UNCONFIGURED:
            response = self.client.post(
                reverse('trimble-connection-health',
                        kwargs={'pk': str(self.connection.id)}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['healthy'])
        self.assertEqual(response.data['status'], 'unconfigured')
        self.connection.refresh_from_db()
        self.assertEqual(self.connection.last_health_status, 'unconfigured')


class TrimbleDiscoveryAndSyncTestCase(DigitalEyeAPITestBase):
    def setUp(self):
        super().setUp()
        self.connection = TrimbleConnection.objects.create(
            name='Discovery link', status='connected')
        self.trimble_project = TrimbleProject.objects.create(
            connection=self.connection, external_id='tp-001', name='Tower A')

    def test_discover_is_director_only(self):
        engineer = User.objects.create_user(
            username='discover_engineer@nexucon.com',
            email='discover_engineer@nexucon.com', password='Password123!')
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(engineer).access_token}')
        response = self.client.post(
            reverse('trimble-connection-discover',
                    kwargs={'pk': str(self.connection.id)}))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_discover_returns_discovered_projects(self):
        with mock.patch('integrations.trimble.TrimbleClient') as client_cls:
            client_cls.return_value.discover_projects.return_value = \
                self.connection.trimble_projects.all()
            response = self.client.post(
                reverse('trimble-connection-discover',
                        kwargs={'pk': str(self.connection.id)}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['projects']), 1)
        self.assertEqual(response.data['projects'][0]['external_id'], 'tp-001')
        self.assertTrue(AuditEvent.objects.filter(action='trimble.discover').exists())

    def test_discover_maps_trimble_errors_to_502(self):
        from integrations.trimble import TrimbleError
        with mock.patch('integrations.trimble.TrimbleClient') as client_cls:
            client_cls.return_value.discover_projects.side_effect = \
                TrimbleError('HTTP 503 from Trimble')
            response = self.client.post(
                reverse('trimble-connection-discover',
                        kwargs={'pk': str(self.connection.id)}))
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)

    def test_connection_sync_runs_service_and_audits(self):
        with mock.patch('integrations.trimble.TrimbleSyncService') as service_cls:
            service_cls.return_value.sync_all_projects.return_value = [
                {'trimble_project': str(self.trimble_project.id),
                 'models': 2, 'elements': 14, 'errors': []},
            ]
            response = self.client.post(
                reverse('trimble-connection-sync',
                        kwargs={'pk': str(self.connection.id)}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'][0]['elements'], 14)
        self.assertTrue(AuditEvent.objects.filter(action='trimble.sync').exists())

    def test_connection_sync_maps_trimble_errors_to_502(self):
        from integrations.trimble import TrimbleError
        with mock.patch('integrations.trimble.TrimbleSyncService') as service_cls:
            service_cls.return_value.sync_all_projects.side_effect = \
                TrimbleError('sync failed')
            response = self.client.post(
                reverse('trimble-connection-sync',
                        kwargs={'pk': str(self.connection.id)}))
        self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)


class TrimbleProjectAPITestCase(DigitalEyeAPITestBase):
    def setUp(self):
        super().setUp()
        self.connection = TrimbleConnection.objects.create(
            name='Project link', status='connected')
        self.trimble_project = TrimbleProject.objects.create(
            connection=self.connection, external_id='tp-002', name='Tower B')

    def test_linking_requires_director(self):
        engineer = User.objects.create_user(
            username='link_engineer@nexucon.com',
            email='link_engineer@nexucon.com', password='Password123!')
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {RefreshToken.for_user(engineer).access_token}')
        response = self.client.patch(
            reverse('trimble-project-detail', kwargs={'pk': str(self.trimble_project.id)}),
            {'linked_project': str(self.project.id)}, format='json')
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_director_links_trimble_project_to_nexucon_project(self):
        response = self.client.patch(
            reverse('trimble-project-detail', kwargs={'pk': str(self.trimble_project.id)}),
            {'linked_project': str(self.project.id)}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.trimble_project.refresh_from_db()
        self.assertEqual(self.trimble_project.linked_project, self.project)

    def test_sync_requires_linked_project(self):
        response = self.client.post(
            reverse('trimble-project-sync', kwargs={'pk': str(self.trimble_project.id)}))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_sync_project_runs_sync_service(self):
        self.trimble_project.linked_project = self.project
        self.trimble_project.save()
        with mock.patch('integrations.trimble.TrimbleSyncService') as service_cls:
            service_cls.return_value.sync_project.return_value = {
                'trimble_project': str(self.trimble_project.id),
                'models': 1, 'elements': 7, 'errors': [],
            }
            response = self.client.post(
                reverse('trimble-project-sync',
                        kwargs={'pk': str(self.trimble_project.id)}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['elements'], 7)
        self.assertTrue(AuditEvent.objects.filter(
            action='trimble.project.sync').exists())


# ======================================================================
# Celery tasks (called synchronously, external HTTP mocked)
# ======================================================================

class TrimbleTasksTestCase(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name='Trimble Task Site')

    def test_health_checks_skip_disconnected_connections(self):
        connected = TrimbleConnection.objects.create(name='Live', status='connected')
        TrimbleConnection.objects.create(name='Gone', status='disconnected')

        from .tasks import trimble_health_checks
        with mock.patch('integrations.trimble.TrimbleClient') as client_cls:
            client_cls.return_value.health_check.return_value = (True, 'healthy')
            results = trimble_health_checks()

        self.assertEqual(len(results), 1)  # disconnected connection is excluded
        self.assertEqual(results[0]['connection'], str(connected.id))
        self.assertTrue(results[0]['healthy'])
        self.assertEqual(results[0]['detail'], 'healthy')

    def test_health_checks_report_failures_without_raising(self):
        TrimbleConnection.objects.create(name='Broken', status='connected')
        from .tasks import trimble_health_checks
        with mock.patch('integrations.trimble.TrimbleClient') as client_cls:
            client_cls.return_value.health_check.return_value = (False, 'HTTP 503')
            results = trimble_health_checks()

        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]['healthy'])
        self.assertEqual(results[0]['detail'], 'HTTP 503')

    def test_trimble_sync_aggregates_real_service_results(self):
        TrimbleConnection.objects.create(name='Synced', status='connected')
        from .tasks import trimble_sync
        with mock.patch('integrations.trimble.TrimbleSyncService') as service_cls:
            service_cls.return_value.sync_all_projects.return_value = [
                {'models': 2, 'elements': 9, 'errors': ['model m-2: no download URL']},
                {'models': 1, 'elements': 4, 'errors': []},
            ]
            summary = trimble_sync()

        self.assertEqual(summary['connections'], 1)
        self.assertEqual(summary['models'], 3)
        self.assertEqual(summary['elements'], 13)
        self.assertEqual(summary['errors'], ['model m-2: no download URL'])

    def test_trimble_sync_skips_non_connected_connections(self):
        TrimbleConnection.objects.create(name='Pending', status='pending_authorization')
        from .tasks import trimble_sync
        with mock.patch('integrations.trimble.TrimbleSyncService') as service_cls:
            summary = trimble_sync()
        service_cls.return_value.sync_all_projects.assert_not_called()
        self.assertEqual(summary['connections'], 0)

    def test_trimble_sync_records_connection_errors_and_continues(self):
        from integrations.trimble import TrimbleError
        TrimbleConnection.objects.create(name='Failing', status='connected')
        from .tasks import trimble_sync
        with mock.patch('integrations.trimble.TrimbleSyncService') as service_cls:
            service_cls.return_value.sync_all_projects.side_effect = \
                TrimbleError('HTTP 401 token expired')
            summary = trimble_sync()

        self.assertEqual(summary['connections'], 1)
        self.assertEqual(summary['models'], 0)
        self.assertIn('HTTP 401 token expired', summary['errors'])
