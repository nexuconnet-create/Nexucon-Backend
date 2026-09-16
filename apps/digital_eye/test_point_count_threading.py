"""
The client's "3 test points averaged" rule (15 Sep 2026 review) has to reach
the figures the platform RECORDS and REPORTS — not only the live preview.

Before this, `apply_active_curve` was called with an `n_points` from exactly
one place (the preview action), so a strength saved from a 3-point element
average carried the SINGLE-reading confidence margin. These tests pin the
s/sqrt(n) divisor at every stored/reported call site.

Everything here is computed from real recorded readings on a real curve;
nothing is mocked and no figure is asserted that the maths does not produce.
"""

from django.contrib.auth import get_user_model
from rest_framework.test import APITestCase

from apps.projects.models import Project

from .models import PUNDITReading, PUNDITTest, ProjectCurveSetting, StrengthCurve
from .se_adjustment import apply_se_adjustment

User = get_user_model()

# Four real calibration pairs on a straight line, so the fitted curve has a
# genuine (non-zero) residual standard error to divide.
CALIBRATION = [
    {'v': 3000.0, 'f': 12.0},
    {'v': 3200.0, 'f': 14.0},
    {'v': 3400.0, 'f': 16.0},
    {'v': 3600.0, 'f': 18.0},
]

# The client's own averaging case: 120 mm at 30 / 29 / 33.3 us.
AVERAGED_TRANSITS = [30.0, 29.0, 33.3]


class PointCountTestBase(APITestCase):
    """A project with one curve, activated, and a helper for real elements."""

    def setUp(self):
        super().setUp()
        self.project = Project.objects.create(
            name='PUNDIT Point Count Site', project_type='Commercial',
            status='ACTIVE')

    def make_curve(self, method='confidence_margin', factor=1.645,
                   data_points=None, name=None):
        curve = StrengthCurve.objects.create(
            name=name or f'Averaging calibration ({method})',
            curve_type='linear', project=self.project,
            formula_params={'m': 0.01, 'c': -20.0},
            data_points=(CALIBRATION if data_points is None
                         else data_points),
            se_adjustment_method=method, se_adjustment_factor=factor)
        setting, _ = ProjectCurveSetting.objects.get_or_create(
            project=self.project)
        setting.active_curve = curve
        setting.save()
        return curve

    def make_element(self, transits, path_mm=120.0,
                     element='COL-AVG', floor='Ground Floor'):
        """A real pulse-velocity test with one reading per transit time.

        A transit time of None is a recorded point that yields no velocity —
        used to prove such a point cannot inflate the divisor.

        NOTE: this writes the readings at the MODEL level, so the test's own
        element-level velocity fields stay unset (only the serializer sets
        those). Surfaces that read the element-level fields — the map, for
        one — must use ``post_element`` instead, which goes through the real
        write path.
        """
        test = PUNDITTest.objects.create(
            project=self.project, test_type='pulse_velocity',
            structural_element=element, floor=floor, path_length_mm=path_mm)
        for label, transit in zip('ABCDEF', transits):
            PUNDITReading.objects.create(
                test=test, point_label=label, path_length_mm=path_mm,
                transit_time_us=transit)
        return test

    def post_element(self, transits, element='COL-API', coord=None):
        """Create an element through the real API (the serializer's
        ``_sync_readings``), so every derived field the platform stores —
        element mean velocity, mean E.C.S, the curve snapshot — is populated
        exactly as it is for an operator's POST. ``coord`` sets the GPS
        position the report map plots."""
        from django.urls import reverse
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import RefreshToken

        user = User.objects.create_superuser(
            username=f'api_{element}@nexucon.com'.lower(),
            email=f'api_{element}@nexucon.com'.lower(),
            password='Password123!')
        client = APIClient()
        refresh = RefreshToken.for_user(user)
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        payload = {
            'project': str(self.project.id),
            'test_type': 'pulse_velocity',
            'structural_element': element,
            'floor': 'Ground Floor',
            'path_length_mm': 120.0,
            'readings': [{'transit_time_us': t} for t in transits],
        }
        if coord is not None:
            payload['latitude'], payload['longitude'] = coord
        response = client.post(reverse('pundit-test-list'), payload,
                               format='json')
        self.assertEqual(response.status_code, 201, msg=response.data)
        return PUNDITTest.objects.get(pk=response.data['id'])


class ElementPointCountTestCase(PointCountTestBase):
    """`element_point_count` — the n in the s/sqrt(n) divisor."""

    def test_counts_every_point_that_yielded_a_velocity(self):
        self.assertEqual(
            self.make_element(AVERAGED_TRANSITS).element_point_count(), 3)

    def test_a_point_without_a_velocity_cannot_inflate_the_divisor(self):
        test = self.make_element(AVERAGED_TRANSITS)
        PUNDITReading.objects.create(
            test=test, point_label='D', path_length_mm=120.0,
            transit_time_us=None)
        # 4 recorded points, but only 3 carry a velocity — the mean is over
        # 3, so the margin may only be divided by sqrt(3).
        self.assertEqual(len(test.reading_rows()), 4)
        self.assertEqual(test.element_point_count(), 3)

    def test_a_single_reading_element_counts_as_one(self):
        self.assertEqual(self.make_element([30.0]).element_point_count(), 1)

    def test_an_element_with_no_computable_velocity_counts_as_one(self):
        # Honest floor: the figure is whatever one reading would give, so it
        # earns no averaging benefit rather than dividing by zero.
        self.assertEqual(self.make_element([]).element_point_count(), 1)


class DivisorArithmeticTestCase(PointCountTestBase):
    """The margin really is k*s/sqrt(n) — the client's arithmetic, checked."""

    def test_the_margin_narrows_by_root_n(self):
        curve = self.make_curve()
        base = 30.0
        _, three = apply_se_adjustment(base, curve, n_points=3)
        _, one = apply_se_adjustment(base, curve, n_points=1)
        s = three['standard_error_mpa']
        self.assertGreater(s, 0.0)
        # The disclosure rounds its figures to 4 dp, so the differences are
        # compared at the precision the disclosure actually carries.
        self.assertAlmostEqual(base - one['adjusted_f_cu_mpa'],
                               curve.se_adjustment_factor * s, places=4)
        self.assertAlmostEqual(base - three['adjusted_f_cu_mpa'],
                               curve.se_adjustment_factor * s / (3 ** 0.5),
                               places=4)

    def test_averaging_three_points_reports_more_strength_than_one(self):
        curve = self.make_curve()
        _, three = apply_se_adjustment(30.0, curve, n_points=3)
        _, one = apply_se_adjustment(30.0, curve, n_points=1)
        self.assertGreater(three['adjusted_f_cu_mpa'],
                           one['adjusted_f_cu_mpa'])
        self.assertEqual(three['n_points_averaged'], 3)

    def test_the_disclosure_names_the_point_count_it_used(self):
        curve = self.make_curve()
        _, three = apply_se_adjustment(30.0, curve, n_points=3)
        self.assertIn('3 test points averaged', three['detail'])


class StoredVerdictUsesThePointCountTestCase(PointCountTestBase):
    """The element verdict the platform SAVES carries the averaged margin."""

    def test_saved_element_strength_is_the_three_point_figure(self):
        from .strength_curves import apply_active_curve

        curve = self.make_curve()
        test = self.make_element(AVERAGED_TRANSITS)
        test.refresh_confidence_metrics()
        test.save()

        # The curve's raw (unadjusted) estimate for this element mean.
        _, snapshot = apply_active_curve(
            self.project, test.element_mean_velocity_km_s(), n_points=3)
        base = snapshot['se_adjustment']['base_f_cu_mpa']
        expected_three, _ = apply_se_adjustment(base, curve, n_points=3)
        expected_one, _ = apply_se_adjustment(base, curve, n_points=1)

        self.assertIsNotNone(test.estimated_compressive_strength_mpa)
        self.assertAlmostEqual(test.estimated_compressive_strength_mpa,
                               expected_three, places=1)
        # And it is genuinely the averaged figure, not the single-reading one.
        self.assertGreater(test.estimated_compressive_strength_mpa,
                           expected_one)
        self.assertEqual(
            test.strength_curve_snapshot['se_adjustment']['n_points_averaged'],
            3)

    def test_a_single_point_element_keeps_the_full_margin(self):
        curve = self.make_curve()
        single = self.make_element([30.0], element='COL-ONE')
        single.refresh_confidence_metrics()
        single.save()
        self.assertEqual(
            single.strength_curve_snapshot['se_adjustment']
            ['n_points_averaged'], 1)

    def test_a_no_policy_curve_leaves_the_estimate_as_fitted(self):
        from .strength_curves import apply_active_curve

        self.make_curve(method='none')
        test = self.make_element(AVERAGED_TRANSITS)
        test.refresh_confidence_metrics()
        test.save()
        _, snapshot = apply_active_curve(
            self.project, test.element_mean_velocity_km_s(), n_points=3)
        self.assertAlmostEqual(
            test.estimated_compressive_strength_mpa,
            snapshot['se_adjustment']['base_f_cu_mpa'], places=1)


class StoredReasoningTraceStatesThePolicyTestCase(PointCountTestBase):
    """The numbered chain must show WHY the reported figure differs from the
    raw curve estimate — two bare numbers would read as a contradiction."""

    def test_an_applied_policy_is_a_step_in_the_trace(self):
        self.make_curve()
        test = self.make_element(AVERAGED_TRANSITS)
        test.refresh_confidence_metrics()
        test.save()
        trace = '\n'.join(test.ai_reasoning_traces)
        self.assertIn('Standard-error policy', trace)
        self.assertIn('Confidence margin', trace)

    def test_a_curve_with_no_policy_adds_no_boilerplate(self):
        # 'none' is the default on every curve: printing it on every element
        # would bury the derivation chain in noise.
        self.make_curve(method='none')
        test = self.make_element(AVERAGED_TRANSITS)
        test.refresh_confidence_metrics()
        test.save()
        self.assertNotIn('Standard-error policy',
                         '\n'.join(test.ai_reasoning_traces))

    def test_a_requested_but_refused_policy_is_still_stated(self):
        # One calibration pair cannot support any error estimate. The client
        # asked for the adjustment, so the refusal is part of the record.
        self.make_curve(method='bias_correction',
                        data_points=[{'v': 3000.0, 'f': 12.0}],
                        name='Too few pairs to adjust')
        test = self.make_element(AVERAGED_TRANSITS)
        test.refresh_confidence_metrics()
        test.save()
        trace = '\n'.join(test.ai_reasoning_traces)
        self.assertIn('Standard-error policy', trace)
        self.assertIn('not applied', trace)

    def test_the_trace_still_states_the_measurement_chain(self):
        # The policy step is an addition, not a replacement.
        self.make_curve()
        test = self.make_element(AVERAGED_TRANSITS)
        test.refresh_confidence_metrics()
        test.save()
        trace = '\n'.join(test.ai_reasoning_traces)
        self.assertIn('test point velocity(ies) recorded', trace)
        self.assertIn('Mean pulse velocity', trace)


class ReportSectionUsesThePointCountTestCase(PointCountTestBase):
    """The report's Section 5.0 element verdicts use the averaged margin."""

    def test_three_point_element_reports_more_strength_than_one_point(self):
        from apps.reports.ndt_reports import NDTReportService

        curve = self.make_curve()
        three = self.make_element(AVERAGED_TRANSITS, element='COL-THREE')
        one = self.make_element([30.0], element='COL-ONE')

        rows = NDTReportService._element_data([three, one])
        by_element = {r['element']: r for r in rows}
        self.assertIn('COL-THREE', by_element)
        self.assertIn('COL-ONE', by_element)
        self.assertIsNotNone(by_element['COL-THREE']['mean_ecs'])
        self.assertIsNotNone(by_element['COL-ONE']['mean_ecs'])

        # Same curve, same policy, same mean velocity — the only difference
        # is that one element's mean is over 3 points and the other's over 1.
        self.assertGreater(by_element['COL-THREE']['mean_ecs'],
                           by_element['COL-ONE']['mean_ecs'])

        # Cross-checked against the arithmetic rather than a magic number.
        curve_s = by_element['COL-THREE']['mean_ecs']
        from .strength_curves import apply_active_curve
        _, snap = apply_active_curve(
            self.project, three.element_mean_velocity_km_s(), n_points=3)
        base = snap['se_adjustment']['base_f_cu_mpa']
        expected, _ = apply_se_adjustment(base, curve, n_points=3)
        self.assertAlmostEqual(curve_s, expected, places=1)
        self.assertGreater(curve_s, 0.0)


class ApiWritePathCarriesThePointCountTestCase(PointCountTestBase):
    """End-to-end through the real serializer (`_sync_readings`, the path the
    operator's POST actually takes) — not just the model layer."""

    def _post_element(self, transits, element='COL-API'):
        return self.post_element(transits, element=element)

    def test_posted_three_point_element_stores_the_averaged_figure(self):
        from .strength_curves import apply_active_curve

        curve = self.make_curve()
        test = self._post_element(AVERAGED_TRANSITS)

        self.assertEqual(test.element_point_count(), 3)
        self.assertIsNotNone(test.estimated_compressive_strength_mpa)

        _, snap = apply_active_curve(
            self.project, test.element_mean_velocity_km_s(), n_points=3)
        base = snap['se_adjustment']['base_f_cu_mpa']
        expected_three, _ = apply_se_adjustment(base, curve, n_points=3)
        expected_one, _ = apply_se_adjustment(base, curve, n_points=1)

        self.assertAlmostEqual(test.estimated_compressive_strength_mpa,
                               expected_three, places=1)
        self.assertGreater(test.estimated_compressive_strength_mpa,
                           expected_one)
        self.assertEqual(
            test.strength_curve_snapshot['se_adjustment']['n_points_averaged'],
            3)

    def test_posted_single_point_element_keeps_the_full_margin(self):
        self.make_curve()
        test = self._post_element([30.0], element='COL-API-ONE')
        self.assertEqual(test.element_point_count(), 1)
        self.assertEqual(
            test.strength_curve_snapshot['se_adjustment']
            ['n_points_averaged'], 1)

    def test_a_point_that_yielded_no_velocity_cannot_inflate_the_divisor(self):
        """The divisor counts the points in the mean, not the rows submitted.

        An operator can submit a point that recorded no transit time (or no
        path length): the row is stored, but it contributes nothing to the
        element mean. Counting it would divide the margin by sqrt(3) when
        the averaged data only supports sqrt(2) — a tighter confidence than
        the readings earn, on the very figure the client asked to be
        conservative about.
        """
        from .strength_curves import apply_active_curve

        curve = self.make_curve()
        test = self._post_element([30.0, 29.0, None], element='COL-API-GAP')

        self.assertEqual(len(test.reading_rows()), 3)
        self.assertEqual(test.element_point_count(), 2)
        self.assertEqual(
            test.strength_curve_snapshot['se_adjustment']['n_points_averaged'],
            2)

        base = test.strength_curve_snapshot['se_adjustment']['base_f_cu_mpa']
        expected_two, _ = apply_se_adjustment(base, curve, n_points=2)
        expected_three, _ = apply_se_adjustment(base, curve, n_points=3)
        self.assertAlmostEqual(test.estimated_compressive_strength_mpa,
                               expected_two, places=1)
        self.assertLess(test.estimated_compressive_strength_mpa,
                        expected_three)
        # The mean itself is unchanged by the broken row: still the mean of
        # the two points that produced a velocity.
        recomputed, _ = apply_active_curve(
            self.project, test.element_mean_velocity_km_s(), n_points=2)
        self.assertAlmostEqual(
            test.estimated_compressive_strength_mpa, recomputed, places=1)

    def test_the_api_response_serializes_the_computed_strength(self):
        """The figure the operator sees back is the stored one — the two
        must not diverge."""
        from django.urls import reverse
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import RefreshToken

        self.make_curve()
        user = User.objects.create_superuser(
            username='avg_reader@nexucon.com',
            email='avg_reader@nexucon.com', password='Password123!')
        client = APIClient()
        refresh = RefreshToken.for_user(user)
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        test = self._post_element(AVERAGED_TRANSITS, element='COL-API-READ')
        response = client.get(
            reverse('pundit-test-detail', args=[test.pk]))
        self.assertEqual(response.status_code, 200)
        self.assertAlmostEqual(
            response.data['estimated_compressive_strength_mpa'],
            test.estimated_compressive_strength_mpa, places=1)


class WorkedExampleStaysHandRecomputableTestCase(PointCountTestBase):
    """7 Sep review: every figure must be recomputable by hand. A curve that
    carries a standard-error policy used to make the worked example print
    "f_cu = 0.01 x 4000 - 20 = 19.42", which does not compute. The equation
    must use the pre-policy figure, then state the policy step."""

    def _example(self, element, curve=None):
        from apps.reports.ndt_reports import NDTReportService
        rows = NDTReportService._element_data([element])
        return NDTReportService._worked_example(
            self.project, rows, curve), rows[0]

    def test_the_equation_uses_the_pre_policy_figure(self):
        curve = self.make_curve()
        element = self.make_element(AVERAGED_TRANSITS)
        example, row = self._example(element, curve)

        base = row['mean_ecs_unadjusted']
        self.assertIsNotNone(base)
        # m=0.01, c=-20, V in m/s: the printed result must BE that arithmetic.
        expected = 0.01 * (row['mean_v'] * 1000) - 20.0
        self.assertAlmostEqual(base, expected, places=1)
        self.assertIn(f'{base:.2f} N/mm2', example)
        # And the reported figure is NOT what the bare equation yields —
        # the policy moved it, which is precisely why both are printed.
        self.assertGreater(row['mean_ecs'], 0.0)
        self.assertNotAlmostEqual(row['mean_ecs'], base, places=2)
        self.assertNotIn(f'= {row["mean_ecs"]:.2f} N/mm2 (V in m/s)', example)

    def test_the_policy_step_and_reported_figure_are_stated(self):
        curve = self.make_curve()
        element = self.make_element(AVERAGED_TRANSITS)
        example, row = self._example(element, curve)
        self.assertIn('Confidence margin applied', example)
        self.assertIn('3 test points averaged', example)
        self.assertIn(
            f"reported strength is therefore {row['mean_ecs']:.2f} N/mm2",
            example)

    def test_a_curve_with_no_policy_prints_only_the_equation(self):
        self.make_curve(method='none')
        element = self.make_element(AVERAGED_TRANSITS)
        example, row = self._example(element, None)
        self.assertIn('Worked example', example)
        self.assertNotIn('reported strength is therefore', example)
        # Unadjusted equals adjusted when nothing is applied.
        self.assertAlmostEqual(row['mean_ecs_unadjusted'], row['mean_ecs'],
                               places=2)

    def test_the_equation_stays_true_for_a_single_point_element(self):
        curve = self.make_curve()
        element = self.make_element([30.0])
        example, row = self._example(element, curve)
        self.assertAlmostEqual(row['mean_ecs_unadjusted'],
                               0.01 * (row['mean_v'] * 1000) - 20.0,
                               places=1)
        self.assertIn('mean of 1 point', example)


class GeneratedPdfTextStaysHandRecomputableTestCase(PointCountTestBase):
    """The end of the chain: the PDF that reaches the client. The printed
    equation must compute, and the policy that moved the reported figure
    must be stated in the same document — otherwise Section 5.0 shows a
    strength the stated arithmetic cannot produce."""

    def _pdf_flat(self):
        from apps.reports.ndt_reports import NDTReportService
        from apps.reports.tests import _pdf_text
        pdf = NDTReportService.generate_ndt_report(self.project)
        return ' '.join(_pdf_text(pdf).split())

    def _device(self):
        from apps.digital_eye.models import FieldDevice
        return FieldDevice.objects.create(
            device_reference='DE-AVG-1', device_type='pundit',
            name='Pundit PL-2', model='Pundit PL-2', manufacturer='Proceq',
            device_id='SN-70001', status='online',
            assigned_project=self.project)

    def setUp(self):
        super().setUp()
        self.device = self._device()

    def _element_with_device(self, transits, element='COL-PDF'):
        test = PUNDITTest.objects.create(
            project=self.project, device=self.device,
            test_type='pulse_velocity', structural_element=element,
            floor='Ground Floor', path_length_mm=120.0)
        for label, transit in zip('ABCDEF', transits):
            PUNDITReading.objects.create(
                test=test, point_label=label, path_length_mm=120.0,
                transit_time_us=transit)
        return test

    def test_the_pdf_prints_a_true_equation_and_the_policy_step(self):
        curve = self.make_curve()
        element = self._element_with_device(AVERAGED_TRANSITS)
        element.save()

        flat = self._pdf_flat()
        from apps.reports.ndt_reports import NDTReportService
        row = NDTReportService._element_data([element])[0]
        base = row['mean_ecs_unadjusted']
        self.assertIsNotNone(base)

        # 1. The printed arithmetic is true: m*V + c really does give base.
        self.assertIn(f'{base:.2f} N/mm2', flat)
        self.assertAlmostEqual(base, 0.01 * (row['mean_v'] * 1000) - 20.0,
                               places=1)

        # 2. The policy step and the reported figure are in the PDF too, so
        #    the Section 5.0 strength is reachable from the working.
        self.assertIn('Confidence margin applied', flat)
        self.assertIn(f"{row['mean_ecs']:.2f} N/mm2", flat)
        self.assertIn('3 test points averaged', flat)

    def test_the_worked_example_and_the_table_agree_on_the_strength(self):
        """The worked example's closing figure and the Section 5.0 table
        cell must be the same number — they are the same verdict."""
        from apps.reports.ndt_reports import NDTReportService
        self.make_curve()
        element = self._element_with_device(AVERAGED_TRANSITS)
        element.save()
        rows = NDTReportService._element_data([element])
        example = NDTReportService._worked_example(self.project, rows)
        self.assertIn(
            f"reported strength is therefore {rows[0]['mean_ecs']:.2f} N/mm2",
            example)

    def test_a_no_policy_project_pdf_carries_no_policy_claim(self):
        self.make_curve(method='none')
        element = self._element_with_device(AVERAGED_TRANSITS)
        element.save()
        flat = self._pdf_flat()
        # The disclosure paragraph legitimately states that no adjustment is
        # applied; what must NOT appear is a claim that one was.
        self.assertNotIn('Confidence margin applied', flat)
        self.assertNotIn('Bias correction applied', flat)
        self.assertIn('No standard-error adjustment is applied', flat)


class ExcelExportDisclosesThePolicyTestCase(PointCountTestBase):
    """The RESULTS sheet's AVERAGE COMPRESSIVE STRENGTH column carries the
    same figure the report prints — so its header must carry the same
    disclosure. Otherwise the sheet claims parity with the report while
    hiding the one thing the report explains."""

    def _subtitle(self):
        from openpyxl import load_workbook
        from .excel_export import build_results_workbook
        workbook = build_results_workbook(self.project)
        return workbook['RESULTS'].cell(row=2, column=1).value

    def test_an_applied_policy_is_stated_in_the_sheet_header(self):
        self.make_curve()
        self.make_element(AVERAGED_TRANSITS).save()
        subtitle = self._subtitle()
        self.assertIn('Standard-error policy applied', subtitle)
        self.assertIn('Confidence margin applied', subtitle)
        self.assertIn('3 test points averaged', subtitle)

    def test_the_header_keeps_its_report_parity_claim(self):
        # The parity sentence must survive alongside the new disclosure.
        self.make_curve()
        self.make_element(AVERAGED_TRANSITS).save()
        subtitle = self._subtitle()
        self.assertIn('Section 5.0 tables exactly', subtitle)
        self.assertIn('velocities in m/s', subtitle)

    def test_a_no_policy_project_adds_nothing_to_the_header(self):
        self.make_curve(method='none')
        self.make_element(AVERAGED_TRANSITS).save()
        subtitle = self._subtitle()
        self.assertIn('Section 5.0 tables exactly', subtitle)
        self.assertNotIn('Standard-error policy applied', subtitle)

    def test_a_refused_policy_is_still_stated_in_the_header(self):
        self.make_curve(method='bias_correction',
                        data_points=[{'v': 3000.0, 'f': 12.0}],
                        name='Too few pairs for the sheet')
        self.make_element(AVERAGED_TRANSITS).save()
        subtitle = self._subtitle()
        self.assertIn('Standard-error policy applied', subtitle)
        self.assertIn('not applied', subtitle)


class MapPopupCarriesThePolicyTestCase(PointCountTestBase):
    """The map marker's strength is the same figure the report prints, so its
    popup carries the same standard-error disclosure rather than showing an
    adjusted number with no provenance."""

    def _feature_props(self, element):
        from django.urls import reverse
        from rest_framework.test import APIClient
        from rest_framework_simplejwt.tokens import RefreshToken

        user = User.objects.create_superuser(
            username='map_officer@nexucon.com',
            email='map_officer@nexucon.com', password='Password123!')
        client = APIClient()
        refresh = RefreshToken.for_user(user)
        client.credentials(HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        response = client.get(
            reverse('project-report-map',
                    kwargs={'project_id': self.project.id}))
        self.assertEqual(response.status_code, 200, msg=getattr(
            response, 'data', None))
        features = response.data['test_points']['features']
        self.assertEqual(len(features), 1)
        return features[0]['properties']

    def test_an_applied_policy_is_carried_into_the_popup(self):
        self.make_curve()
        element = self.post_element(AVERAGED_TRANSITS, element='COL-MAP',
                                    coord=(6.4281, 3.4219))
        props = self._feature_props(element)
        self.assertIsNotNone(props['strength_n_mm2'])
        self.assertIn('Confidence margin applied', props['strength_note'])
        self.assertIn('3 test points averaged', props['strength_note'])
        # The marker's strength is the same figure the registry and the
        # report print — one number, one provenance.
        self.assertAlmostEqual(props['strength_n_mm2'],
                               element.estimated_compressive_strength_mpa,
                               places=1)

    def test_a_no_policy_project_carries_no_note(self):
        self.make_curve(method='none')
        element = self.post_element(AVERAGED_TRANSITS, element='COL-MAP-NONE',
                                    coord=(6.4281, 3.4219))
        props = self._feature_props(element)
        self.assertIsNotNone(props['strength_n_mm2'])
        self.assertIsNone(props['strength_note'])

    def test_a_refused_policy_is_carried_into_the_popup(self):
        self.make_curve(method='bias_correction',
                        data_points=[{'v': 3000.0, 'f': 12.0}],
                        name='Too few pairs for the map')
        element = self.post_element(AVERAGED_TRANSITS, element='COL-MAP-REF',
                                    coord=(6.4281, 3.4219))
        props = self._feature_props(element)
        self.assertIn('not applied', props['strength_note'])


class CrossElementTraceCarriesThePolicyTestCase(PointCountTestBase):
    """The report's cross-element confidence table states the policy too —
    and, importantly, its interval is NOT narrowed by sqrt(n), because that
    standard error measures the CURVE's scatter, which averaging field
    readings at one element does not reduce."""

    def _summaries(self, ecs, n_points):
        return [{
            'element': 'COL-AVG', 'floor': 'Ground Floor',
            'grid_location': 'A1', 'mean_velocity_m_s': 4000.0,
            'mean_ecs_n_mm2': ecs, 'grade': 'good',
            'n_points': n_points, 'point_spread_pct': 1.0,
            'point_velocities_m_s': [4010.0, 3990.0, 4000.0][:n_points],
            'se_adjustment': {
                'method': 'confidence_margin', 'method_label': 'Confidence margin',
                'applied': True, 'n_points_averaged': n_points,
                'detail': f'Confidence margin applied (n={n_points}).',
            },
        }]

    def test_the_policy_appears_in_the_cross_element_trace(self):
        from .adapters import PUNDITAdapter

        metrics = PUNDITAdapter._confidence_metrics(
            self.project, self._summaries(28.0, 3))
        self.assertEqual(len(metrics), 1)
        trace = '\n'.join(metrics[0]['reasoning_trace'])
        self.assertIn('Standard-error policy', trace)
        self.assertIn('n=3', trace)

    def test_the_interval_stays_the_curve_standard_error_width(self):
        from . import confidence_metrics as cm
        from .adapters import PUNDITAdapter

        self.make_curve()
        se = cm.curve_standard_error_mpa(self.project)
        self.assertIsNotNone(se)
        metrics = PUNDITAdapter._confidence_metrics(
            self.project, self._summaries(28.0, 3))
        low, high = metrics[0]['confidence_interval_n_mm2']
        # The table prints each bound to 1 dp, so compare the bounds — the
        # difference of two rounded numbers is not itself a rounded number.
        self.assertAlmostEqual(low, round(28.0 - cm.Z_95 * se, 1), places=1)
        self.assertAlmostEqual(high, round(28.0 + cm.Z_95 * se, 1), places=1)
        # Full width on purpose: dividing the CURVE's residual standard error
        # by sqrt(n) would claim that averaging field readings tightens the
        # calibration curve, which is false — and would quietly flatter
        # probability_below_design on a safety decision. Stated as a
        # half-width so the test fails if someone later divides by sqrt(n):
        # that would give ~3.3, not 5.5.
        self.assertAlmostEqual((high - low) / 2.0, cm.Z_95 * se, places=1)
