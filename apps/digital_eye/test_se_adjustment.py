"""
Standard-error handling in the Nexucon Link calibration (15 Sep 2026 review).

The client directed the team to "incorporate a standard error of 1.99 into
calibration calculations", to follow Lagos State's practice of "adding an
adjustment factor (such as adding 2) to close the error gap for actual
values", and to "factor standard errors into post-velocity calculations
before converting them into FCU reports".

The governing principle these tests pin down: the client's 1.99 N/mm2 and
their "+2" are OUTPUTS of the maths applied to real calibration data, never
inputs. Nothing is hardcoded, and where the data cannot support an error
estimate the platform says so rather than inventing one.

Covers:
  * the residual standard error / mean residual maths (hand-computed);
  * both adjustment policies and the n-point standard error of the mean;
  * the honest refusal paths (lookup tables, too few pairs, zero residual
    degrees of freedom, unknown policy);
  * the standards registry behind the mathematical model;
  * the API surface (serializer validation, se-analysis, preview, settings);
  * the disclosure that carries the adjustment into the FCU report.
"""
from django.contrib.auth import get_user_model
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework_simplejwt.tokens import RefreshToken

from apps.audit.models import AuditEvent
from apps.projects.models import Project

from .tests import DigitalEyeAPITestBase

User = get_user_model()


# ======================================================================
# The maths
# ======================================================================

class StandardErrorAnalysisTestCase(DigitalEyeAPITestBase):
    """
    se_adjustment.standard_error_analysis / apply_se_adjustment.

    Every expected number below was computed by hand from the calibration
    pairs the test names.
    """

    def _curve(self, curve_type='linear', params=None, points=None,
               method='none', factor=1.0):
        from .models import StrengthCurve
        return StrengthCurve.objects.create(
            name=f'SE test {curve_type} {method}',
            curve_type=curve_type,
            project=self.project,
            formula_params=params or {},
            data_points=points or [],
            se_adjustment_method=method,
            se_adjustment_factor=factor,
        )

    def test_standard_error_and_mean_residual_match_hand_computation(self):
        # f = 0.01*V - 20 over four points; residuals +1, -1, +1, -1.
        # SSE = 4, n - k = 4 - 2 = 2  =>  s = sqrt(4/2) = sqrt(2)
        # mean residual = 0  (this fit is not biased on this data)
        from .se_adjustment import standard_error_analysis
        curve = self._curve(params={'m': 0.01, 'c': -20.0}, points=[
            {'v': 3000.0, 'f': 11.0},   # predicted 10 -> +1
            {'v': 3200.0, 'f': 11.0},   # predicted 12 -> -1
            {'v': 3400.0, 'f': 15.0},   # predicted 14 -> +1
            {'v': 3600.0, 'f': 15.0},   # predicted 16 -> -1
        ])
        stats = standard_error_analysis(curve)
        self.assertEqual(stats['n_pairs'], 4)
        self.assertAlmostEqual(stats['standard_error_mpa'], 2 ** 0.5, places=3)
        self.assertAlmostEqual(stats['mean_residual_mpa'], 0.0, places=6)
        self.assertTrue(stats['adjustment_available'])
        self.assertIsNone(stats['unavailable_reason'])

    def test_mean_residual_measures_bias_when_the_curve_under_predicts(self):
        # Every pair sits exactly 2.0 above the curve: this is the "+2"
        # convention, measured from the data instead of assumed.
        from .se_adjustment import standard_error_analysis
        curve = self._curve(params={'m': 0.01, 'c': -20.0}, points=[
            {'v': 3000.0, 'f': 12.0},
            {'v': 3200.0, 'f': 14.0},
            {'v': 3400.0, 'f': 16.0},
            {'v': 3600.0, 'f': 18.0},
        ])
        stats = standard_error_analysis(curve)
        self.assertAlmostEqual(stats['mean_residual_mpa'], 2.0, places=6)

    def test_lookup_table_reports_the_honest_reason_not_a_number(self):
        # Piecewise interpolation is not a fitted model, so there is no
        # residual standard error and none is invented.
        from .se_adjustment import standard_error_analysis
        curve = self._curve(curve_type='lookup', params={
            'points': [[3000.0, 10.0], [3500.0, 20.0], [4000.0, 30.0]]},
            points=[{'v': 3000.0, 'f': 10.0}, {'v': 3500.0, 'f': 20.0},
                    {'v': 4000.0, 'f': 30.0}])
        stats = standard_error_analysis(curve)
        self.assertIsNone(stats['standard_error_mpa'])
        self.assertFalse(stats['adjustment_available'])
        self.assertIn('lookup table', stats['unavailable_reason'])

    def test_fewer_than_three_pairs_reports_the_shortfall(self):
        from .se_adjustment import (MIN_PAIRS_FOR_ADJUSTMENT,
                                    standard_error_analysis)
        curve = self._curve(params={'m': 0.01, 'c': -20.0}, points=[
            {'v': 3000.0, 'f': 11.0}, {'v': 3200.0, 'f': 11.0}])
        stats = standard_error_analysis(curve)
        self.assertEqual(stats['n_pairs'], 2)
        self.assertIsNone(stats['standard_error_mpa'])
        self.assertFalse(stats['adjustment_available'])
        self.assertIn(str(MIN_PAIRS_FOR_ADJUSTMENT),
                      stats['unavailable_reason'])

    def test_zero_residual_degrees_of_freedom_reports_the_reason(self):
        # exponential has three parameters; three pairs leave n - k = 0, so
        # the standard error is honestly undefined rather than zero.
        from .se_adjustment import standard_error_analysis
        e = 2.718281828459045
        curve = self._curve(curve_type='exponential',
                            params={'a': 0.5, 'b': 0.001, 'c': 0.0},
                            points=[{'v': 3000.0, 'f': 0.5 * e ** 3.0},
                                    {'v': 3500.0, 'f': 0.5 * e ** 3.5},
                                    {'v': 4000.0, 'f': 0.5 * e ** 4.0}])
        stats = standard_error_analysis(curve)
        self.assertFalse(stats['adjustment_available'])
        self.assertIn('degrees of freedom', stats['unavailable_reason'])

    def test_default_policy_is_none_and_changes_nothing(self):
        from .se_adjustment import apply_se_adjustment
        curve = self._curve(params={'m': 0.01, 'c': -20.0}, points=[
            {'v': 3000.0, 'f': 12.0}, {'v': 3200.0, 'f': 14.0},
            {'v': 3400.0, 'f': 16.0}, {'v': 3600.0, 'f': 18.0}])
        adjusted, disclosure = apply_se_adjustment(25.0, curve)
        self.assertEqual(adjusted, 25.0)
        self.assertFalse(disclosure['applied'])
        self.assertEqual(disclosure['method'], 'none')
        self.assertIn('as fitted', disclosure['detail'])

    def test_bias_correction_adds_the_measured_residual(self):
        from .se_adjustment import apply_se_adjustment
        curve = self._curve(params={'m': 0.01, 'c': -20.0},
                            method='bias_correction', points=[
                                {'v': 3000.0, 'f': 12.0},
                                {'v': 3200.0, 'f': 14.0},
                                {'v': 3400.0, 'f': 16.0},
                                {'v': 3600.0, 'f': 18.0}])
        adjusted, disclosure = apply_se_adjustment(25.0, curve)
        self.assertTrue(disclosure['applied'])
        self.assertAlmostEqual(disclosure['mean_residual_mpa'], 2.0, places=6)
        self.assertAlmostEqual(adjusted, 27.0, places=6)
        self.assertIn('Bias correction applied', disclosure['detail'])

    def test_confidence_margin_subtracts_k_times_the_standard_error(self):
        from .se_adjustment import apply_se_adjustment
        curve = self._curve(params={'m': 0.01, 'c': -20.0},
                            method='confidence_margin', factor=1.645, points=[
                                {'v': 3000.0, 'f': 11.0},
                                {'v': 3200.0, 'f': 11.0},
                                {'v': 3400.0, 'f': 15.0},
                                {'v': 3600.0, 'f': 15.0}])
        # s = sqrt(2); a single reading divides by 1.
        adjusted, disclosure = apply_se_adjustment(25.0, curve)
        self.assertTrue(disclosure['applied'])
        self.assertAlmostEqual(adjusted, 25.0 - 1.645 * 2 ** 0.5, places=4)

    def test_confidence_margin_narrows_with_the_points_averaged(self):
        # The client's three-point average: the uncertainty of the MEAN is
        # s/sqrt(n), so the margin is smaller than for a single reading.
        from .se_adjustment import apply_se_adjustment
        curve = self._curve(params={'m': 0.01, 'c': -20.0},
                            method='confidence_margin', factor=1.645, points=[
                                {'v': 3000.0, 'f': 11.0},
                                {'v': 3200.0, 'f': 11.0},
                                {'v': 3400.0, 'f': 15.0},
                                {'v': 3600.0, 'f': 15.0}])
        single, _ = apply_se_adjustment(25.0, curve, n_points=1)
        averaged, disclosure = apply_se_adjustment(25.0, curve, n_points=3)
        s = 2 ** 0.5
        self.assertAlmostEqual(single, 25.0 - 1.645 * s, places=4)
        self.assertAlmostEqual(averaged, 25.0 - 1.645 * s / 3 ** 0.5, places=4)
        self.assertGreater(averaged, single)
        self.assertEqual(disclosure['n_points_averaged'], 3)
        self.assertIn('standard error of the mean', disclosure['detail'])

    def test_requested_adjustment_without_the_data_is_refused_honestly(self):
        from .se_adjustment import apply_se_adjustment
        curve = self._curve(params={'m': 0.01, 'c': -20.0},
                            method='bias_correction',
                            points=[{'v': 3000.0, 'f': 11.0}])
        adjusted, disclosure = apply_se_adjustment(25.0, curve)
        self.assertEqual(adjusted, 25.0)
        self.assertFalse(disclosure['applied'])
        self.assertIn('not applied', disclosure['detail'])

    def test_unknown_method_never_invents_a_correction(self):
        # An unrecognised policy string is a data defect, not a licence to
        # apply something. The estimate is reported as fitted.
        from .se_adjustment import apply_se_adjustment
        curve = self._curve(params={'m': 0.01, 'c': -20.0},
                            method='confidence_margin', points=[
                                {'v': 3000.0, 'f': 11.0},
                                {'v': 3200.0, 'f': 11.0},
                                {'v': 3400.0, 'f': 15.0}])
        curve.se_adjustment_method = 'add_two_point_oh'   # not persisted
        adjusted, disclosure = apply_se_adjustment(25.0, curve)
        self.assertEqual(adjusted, 25.0)
        self.assertFalse(disclosure['applied'])
        self.assertIn('Unknown', disclosure['detail'])

    def test_confidence_margin_factor_must_be_positive_to_apply(self):
        # A zero or negative k would report the raw estimate, or something
        # above it, while claiming to be a conservative lower bound. The
        # maths refuses rather than mislabelling the figure.
        from .se_adjustment import apply_se_adjustment
        curve = self._curve(params={'m': 0.01, 'c': -20.0},
                            method='confidence_margin', factor=0.0, points=[
                                {'v': 3000.0, 'f': 11.0},
                                {'v': 3200.0, 'f': 11.0},
                                {'v': 3400.0, 'f': 15.0}])
        adjusted, disclosure = apply_se_adjustment(25.0, curve)
        self.assertEqual(adjusted, 25.0)
        self.assertFalse(disclosure['applied'])

    def test_no_strength_estimate_yields_no_adjustment(self):
        from .se_adjustment import apply_se_adjustment
        curve = self._curve(params={'m': 0.01, 'c': -20.0},
                            method='bias_correction')
        adjusted, disclosure = apply_se_adjustment(None, curve)
        self.assertIsNone(adjusted)
        self.assertIsNone(disclosure['adjusted_f_cu_mpa'])
        self.assertFalse(disclosure['applied'])

    def test_disclosure_always_accounts_for_itself(self):
        from .se_adjustment import apply_se_adjustment
        curve = self._curve(params={'m': 0.01, 'c': -20.0})
        for method in ('none', 'bias_correction', 'confidence_margin'):
            curve.se_adjustment_method = method
            _, disclosure = apply_se_adjustment(25.0, curve)
            self.assertIsNotNone(disclosure)
            self.assertTrue(disclosure['detail'],
                            msg=f'{method} produced an empty disclosure')

    def test_analysis_payload_documents_every_statistic(self):
        from .se_adjustment import analysis_payload
        curve = self._curve(params={'m': 0.01, 'c': -20.0}, points=[
            {'v': 3000.0, 'f': 12.0}, {'v': 3200.0, 'f': 14.0},
            {'v': 3400.0, 'f': 16.0}, {'v': 3600.0, 'f': 18.0}])
        payload = analysis_payload(curve)
        for key in ('r2_score', 'standard_error', 'mean_residual', 'aic'):
            self.assertIn(key, payload['definitions'])
            self.assertTrue(payload['definitions'][key].strip())
        self.assertEqual(payload['min_pairs_required'], 3)
        # The bias on this data is +2.0, so the honest recommendation names
        # bias correction rather than leaving the Director to guess.
        self.assertIn('under-predicts', payload['recommendation'])

    def test_analysis_payload_recommends_against_bias_when_unbiased(self):
        from .se_adjustment import analysis_payload
        curve = self._curve(params={'m': 0.01, 'c': -20.0}, points=[
            {'v': 3000.0, 'f': 11.0}, {'v': 3200.0, 'f': 11.0},
            {'v': 3400.0, 'f': 15.0}, {'v': 3600.0, 'f': 15.0}])
        payload = analysis_payload(curve)
        self.assertIn('not systematically offset', payload['recommendation'])


# ======================================================================
# The standards behind the mathematical model
# ======================================================================

class StandardsRegistryTestCase(APITestCase):
    """15 Sep 2026 action item: document the specific building standards or
    codes used for the mathematical model."""

    def test_every_entry_is_complete_and_traceable(self):
        from .strength_curves import standards_registry
        registry = standards_registry()
        self.assertTrue(registry)
        for entry in registry:
            for field in ('code', 'title', 'role', 'role_label', 'scope',
                          'platform_use'):
                self.assertTrue(entry.get(field),
                                msg=f'{entry.get("code")} missing {field}')
            self.assertIn(entry['role'],
                          ('measurement', 'correlation', 'statistics'))

    def test_registry_records_the_bs_1881_23_discrepancy(self):
        # The minutes say "BS 1881-23", which does not exist. The registry
        # names the real document and records the discrepancy rather than
        # repeating an unverifiable code to the client.
        from .strength_curves import STANDARDS
        entry = next(e for e in STANDARDS if e['code'] == 'BS 1881-203:1986')
        self.assertIn('BS 1881-23', entry['note'])
        self.assertIn('exists', entry['note'])

    def test_in_situ_strength_standard_governs_the_calibration(self):
        from .strength_curves import STANDARDS
        entry = next(e for e in STANDARDS if e['code'] == 'BS EN 13791:2019')
        self.assertEqual(entry['role'], 'correlation')
        self.assertIn('in-situ', entry['title'])

    def test_the_exponential_model_has_a_named_standard_basis(self):
        from .strength_curves import STANDARDS
        aci = next(e for e in STANDARDS if e['code'] == 'ACI 228.2R-2018')
        self.assertIn('exponential', aci['platform_use'].lower())

    def test_standards_endpoint_serves_the_registry(self):
        user = User.objects.create_superuser(
            username='se_std@nexucon.com', email='se_std@nexucon.com',
            password='Password123!')
        refresh = RefreshToken.for_user(user)
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        response = self.client.get(reverse('strength-curve-standards'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        codes = [e['code'] for e in response.data['standards']]
        self.assertIn('BS 1881-203:1986', codes)
        self.assertIn('BS EN 13791:2019', codes)

    def test_standards_endpoint_requires_authentication(self):
        response = self.client.get(reverse('strength-curve-standards'))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_the_model_selection_entry_cites_akaike_not_a_standard(self):
        # R2 and the standard error are genuinely governed by BS EN 13791.
        # AIC is not a concrete standard — it is Akaike's 1974 paper — so the
        # registry names the paper. A BS/EN number here would be exactly the
        # invented reference this registry exists to prevent.
        from .strength_curves import STANDARDS
        entry = next(e for e in STANDARDS if e['role'] == 'statistics')
        # The author and year live in the citation code; the title is the
        # paper's own title, which does not name its author.
        self.assertEqual(entry['code'], 'Akaike (1974)')
        self.assertIn('Statistical Model Identification', entry['title'])
        self.assertIn('IEEE', entry['title'])

    def test_every_statistic_citation_resolves_to_a_registered_document(self):
        # The load-bearing one: a statistic can never cite a document the
        # registry does not hold, so no surface can show a source that does
        # not exist.
        from .se_adjustment import STAT_REFERENCES
        from .strength_curves import STANDARD_CODES
        for key, codes in STAT_REFERENCES.items():
            self.assertTrue(codes, msg=f'{key} cites nothing')
            for code in codes:
                self.assertIn(code, STANDARD_CODES,
                              msg=f'{key} cites unregistered {code!r}')

    def test_every_documented_statistic_cites_a_source_and_vice_versa(self):
        # The client asked for a reference for the statistics BY NAME. A
        # definition with no source is precisely the gap he was pointing at,
        # and a citation with no definition is a dangling one.
        from .se_adjustment import STAT_DEFINITIONS, STAT_REFERENCES
        self.assertEqual(set(STAT_DEFINITIONS), set(STAT_REFERENCES))

    def test_the_published_statistic_keys_are_pinned(self):
        # The Curve Manager and the settings page each carry a LABEL map for
        # these keys, and an unlabelled one renders as raw snake_case — the
        # opposite of the self-explanatory panel the 15 Sep review asked for.
        # Pinning the set here makes adding a statistic a deliberate act that
        # fails this test until the frontend labels are extended to match.
        from .se_adjustment import STAT_DEFINITIONS
        self.assertEqual(
            set(STAT_DEFINITIONS),
            {'r2_score', 'standard_error', 'mean_residual', 'aic',
             'point_count', 'velocity_step', 'margin_scope'})


# ======================================================================
# The API surface
# ======================================================================

class StandardErrorAPITestCase(DigitalEyeAPITestBase):
    """Serializer validation, the se-analysis endpoint, and the live preview
    carrying both the raw and the adjusted figure."""

    def _curve(self, **overrides):
        from .models import StrengthCurve
        payload = {
            'name': 'SE API calibration',
            'curve_type': 'linear',
            'project': self.project,
            'formula_params': {'m': 0.01, 'c': -20.0},
            'data_points': [{'v': 3000.0, 'f': 12.0}, {'v': 3200.0, 'f': 14.0},
                            {'v': 3400.0, 'f': 16.0}, {'v': 3600.0, 'f': 18.0}],
        }
        payload.update(overrides)
        return StrengthCurve.objects.create(**payload)

    def _activate(self, curve):
        from .models import ProjectCurveSetting
        setting, _ = ProjectCurveSetting.objects.get_or_create(
            project=self.project)
        setting.active_curve = curve
        setting.save()

    def test_curve_exposes_the_se_analysis_and_the_method_label(self):
        curve = self._curve()
        response = self.client.get(
            reverse('strength-curve-detail', args=[curve.id]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['se_adjustment_method'], 'none')
        self.assertIn('se_adjustment_method_display', response.data)
        self.assertEqual(response.data['se_analysis']['n_pairs'], 4)
        self.assertAlmostEqual(
            response.data['se_analysis']['mean_residual_mpa'], 2.0, places=4)

    def test_adjustment_method_must_be_a_known_policy(self):
        response = self.client.post(reverse('strength-curve-list'), {
            'name': 'Bad policy', 'curve_type': 'linear',
            'project': str(self.project.id),
            'formula_params': {'m': 0.01, 'c': -20.0},
            'se_adjustment_method': 'add_2',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_none_policy_requires_real_calibration_pairs(self):
        # A curve with no pairs cannot support an error estimate, so the
        # platform refuses to configure an adjustment that could never apply
        # — better a clear refusal than a policy that silently does nothing.
        response = self.client.post(reverse('strength-curve-list'), {
            'name': 'No pairs', 'curve_type': 'linear',
            'project': str(self.project.id),
            'formula_params': {'m': 0.01, 'c': -20.0},
            'se_adjustment_method': 'bias_correction',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('se_adjustment_method', response.data['errors'])

    def test_confidence_margin_requires_a_positive_factor(self):
        response = self.client.post(reverse('strength-curve-list'), {
            'name': 'Zero k', 'curve_type': 'linear',
            'project': str(self.project.id),
            'formula_params': {'m': 0.01, 'c': -20.0},
            'data_points': [{'v': 3000.0, 'f': 12.0},
                            {'v': 3200.0, 'f': 14.0},
                            {'v': 3400.0, 'f': 16.0}],
            'se_adjustment_method': 'confidence_margin',
            'se_adjustment_factor': 0.0,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('se_adjustment_factor', response.data['errors'])

    def test_a_valid_adjustment_can_be_configured_and_persists(self):
        response = self.client.post(reverse('strength-curve-list'), {
            'name': 'Bias-corrected calibration', 'curve_type': 'linear',
            'project': str(self.project.id),
            'formula_params': {'m': 0.01, 'c': -20.0},
            'data_points': [{'v': 3000.0, 'f': 12.0},
                            {'v': 3200.0, 'f': 14.0},
                            {'v': 3400.0, 'f': 16.0},
                            {'v': 3600.0, 'f': 18.0}],
            'se_adjustment_method': 'bias_correction',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED,
                         msg=str(response.data))
        self.assertEqual(response.data['se_adjustment_method'],
                         'bias_correction')
        self.assertTrue(response.data['se_analysis']['adjustment_available'])

    def test_se_analysis_endpoint_reports_the_projects_active_curve(self):
        curve = self._curve(se_adjustment_method='bias_correction')
        self._activate(curve)
        response = self.client.get(reverse('strength-curve-se-analysis'),
                                   {'project': str(self.project.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['curve']['id'], str(curve.id))
        self.assertEqual(response.data['analysis']['method'],
                         'bias_correction')
        self.assertAlmostEqual(
            response.data['analysis']['mean_residual_mpa'], 2.0, places=4)

    def test_se_analysis_on_the_platform_default_reports_no_error_estimate(self):
        # With no project curve activated the platform default applies. It is
        # a fixed documented correlation with no stored calibration pairs, so
        # there is honestly no standard error — and the reason says why.
        response = self.client.get(reverse('strength-curve-se-analysis'),
                                   {'project': str(self.project.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['curve']['is_default'])
        analysis = response.data['analysis']
        self.assertEqual(analysis['n_pairs'], 0)
        self.assertIsNone(analysis['standard_error_mpa'])
        self.assertFalse(analysis['adjustment_available'])
        self.assertIn('at least 3', analysis['unavailable_reason'])
        # The definitions still ship, so the UI can explain what is missing.
        self.assertIn('standard_error', analysis['definitions'])

    def test_se_analysis_requires_a_project_in_scope(self):
        response = self.client.get(reverse('strength-curve-se-analysis'))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_se_analysis_ships_the_source_of_each_statistic(self):
        # The definitions panel on the Curve Manager renders these; without
        # them the page states a number with no provenance — which is what
        # the 15 Sep review objected to ("show me a literature on this").
        curve = self._curve(se_adjustment_method='bias_correction')
        self._activate(curve)
        response = self.client.get(reverse('strength-curve-se-analysis'),
                                   {'project': str(self.project.id)})
        references = response.data['analysis']['references']
        self.assertEqual(references['standard_error'], ['BS EN 13791:2019'])
        self.assertEqual(references['aic'], ['Akaike (1974)'])

    def test_the_no_curve_fallback_also_ships_the_references(self):
        # On this path the built-in fixed curve has no regression, but the
        # page still renders the definitions panel — so it must still be able
        # to say where each statistic comes from.
        from .models import StrengthCurve
        StrengthCurve.objects.filter(is_default=True).delete()
        response = self.client.get(reverse('strength-curve-se-analysis'),
                                   {'project': str(self.project.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data['curve'])
        self.assertEqual(response.data['analysis']['references']['aic'],
                         ['Akaike (1974)'])

    def test_preview_separates_the_raw_and_adjusted_strength(self):
        # V = 4000 m/s through f = 0.01*V - 20 gives 20.0 N/mm2; the measured
        # +2.0 bias takes the reported figure to 22.0. Both are returned, so
        # the adjustment is never hidden inside a single number.
        curve = self._curve(se_adjustment_method='bias_correction')
        self._activate(curve)
        response = self.client.post(reverse('strength-curve-preview'), {
            'project': str(self.project.id),
            'path_length_mm': 120.0,
            'transit_time_us': 30.0,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK,
                         msg=str(response.data))
        self.assertAlmostEqual(response.data['f_cu_mpa'], 22.0, places=2)
        self.assertAlmostEqual(response.data['f_cu_unadjusted_mpa'], 20.0,
                               places=2)
        self.assertTrue(response.data['se_adjustment']['applied'])
        self.assertEqual(response.data['status'], 'ok')

    def test_preview_records_the_point_count_for_averaged_readings(self):
        curve = self._curve(se_adjustment_method='confidence_margin',
                            se_adjustment_factor=1.645)
        self._activate(curve)
        response = self.client.post(reverse('strength-curve-preview'), {
            'project': str(self.project.id),
            'path_length_mm': 120.0,
            'transit_time_us': 30.0,
            'n_points': 3,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK,
                         msg=str(response.data))
        self.assertEqual(response.data['n_points'], 3)
        self.assertEqual(response.data['se_adjustment']['n_points_averaged'], 3)
        # This curve's pairs sit a constant +2.0 off the line, so
        # SSE = 4 x 4 = 16, n - k = 2, and s = sqrt(8) = 2.8284.
        # f = 0.01*4000 - 20 = 20.0; the margin for the mean of three points
        # is 1.645 * s / sqrt(3) = 2.6863 -> 17.3137.
        s = 8 ** 0.5
        expected = 20.0 - 1.645 * s / 3 ** 0.5
        # The endpoint reports f_cu to two decimals.
        self.assertAlmostEqual(response.data['f_cu_mpa'],
                               round(expected, 2), places=2)
        self.assertAlmostEqual(response.data['f_cu_unadjusted_mpa'], 20.0,
                               places=2)
        # A conservative lower bound, but a narrower one than a single
        # reading would carry (s/sqrt(3) < s).
        self.assertLess(expected, 20.0)
        self.assertGreater(expected, 20.0 - 1.645 * s)

    def test_preview_rejects_a_non_positive_point_count(self):
        response = self.client.post(reverse('strength-curve-preview'), {
            'path_length_mm': 120.0, 'transit_time_us': 30.0, 'n_points': 0,
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_preview_rejects_a_non_numeric_point_count(self):
        response = self.client.post(reverse('strength-curve-preview'), {
            'path_length_mm': 120.0, 'transit_time_us': 30.0,
            'n_points': 'three',
        }, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class NexuconLinkSettingsTestCase(DigitalEyeAPITestBase):
    """Platform system settings (the wireframe's "System Settings" layer),
    including exponential as the default curve type per the 15 Sep 2026
    direction that concrete behaviour is non-linear."""

    def test_defaults_are_exponential_and_the_measurement_standard(self):
        response = self.client.get(reverse('nexucon-link-settings'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['preferred_curve_type'], 'exponential')
        self.assertEqual(response.data['default_standard'], 'BS 1881-203:1986')
        self.assertIn('Exponential',
                      response.data['preferred_curve_type_display'])

    def test_the_curve_manager_alias_serves_the_same_settings(self):
        canonical = self.client.get(reverse('nexucon-link-settings'))
        alias = self.client.get(reverse('strength-curve-link-settings'))
        self.assertEqual(alias.status_code, status.HTTP_200_OK)
        self.assertEqual(canonical.data, alias.data)

    def test_exponential_survives_a_round_trip(self):
        # Re-reading must not silently reset the preference to a linear
        # default — that would undo the client's direction invisibly.
        response = self.client.patch(reverse('nexucon-link-settings'), {
            'preferred_curve_type': 'exponential'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK,
                         msg=str(response.data))
        response = self.client.get(reverse('nexucon-link-settings'))
        self.assertEqual(response.data['preferred_curve_type'], 'exponential')

    def test_an_unknown_curve_type_is_rejected(self):
        response = self.client.patch(reverse('nexucon-link-settings'), {
            'preferred_curve_type': 'quadratic-ish'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_write_is_director_only_and_read_is_not(self):
        from apps.government.models import District, Profile, Role
        district = District.objects.create(name='Settings District',
                                           code='SET-D')
        self.project.district = district
        self.project.save()
        staff = User.objects.create_user(
            username='nl_staff@nexucon.com', email='nl_staff@nexucon.com',
            password='Password123!')
        Profile.objects.create(user=staff, role=Role.objects.create(
            name='Field Officer'), district=district)
        refresh = RefreshToken.for_user(staff)
        self.client.credentials(
            HTTP_AUTHORIZATION=f'Bearer {refresh.access_token}')
        self.assertEqual(
            self.client.get(reverse('nexucon-link-settings')).status_code,
            status.HTTP_200_OK)
        self.assertEqual(
            self.client.patch(reverse('nexucon-link-settings'),
                              {'preferred_curve_type': 'linear'},
                              format='json').status_code,
            status.HTTP_403_FORBIDDEN)

    def test_write_is_audited(self):
        self.client.patch(reverse('nexucon-link-settings'), {
            'velocity_unit': 'km/s'}, format='json')
        self.assertTrue(AuditEvent.objects.filter(
            action='digital_eye.nexucon_link.settings.update').exists())

    def test_write_requires_authentication(self):
        self.client.credentials()
        response = self.client.patch(reverse('nexucon-link-settings'), {
            'preferred_curve_type': 'linear'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_a_single_singleton_row_is_kept(self):
        from .models import NexuconLinkSettings
        self.client.get(reverse('nexucon-link-settings'))
        self.client.get(reverse('nexucon-link-settings'))
        self.assertEqual(NexuconLinkSettings.objects.count(), 1)
        self.assertEqual(NexuconLinkSettings.objects.first().id, 1)


# ======================================================================
# The client's actual instruction: the error must reach the FCU report
# ======================================================================

class SEAdjustmentFlowsIntoTheReportTestCase(DigitalEyeAPITestBase):
    """"Factor standard errors into post-velocity calculations before
    converting them into FCU reports" — so the report Section 3.0 has to
    state what was done to every Section 5.0 figure."""

    def _curve(self, method='none'):
        from .models import StrengthCurve
        return StrengthCurve.objects.create(
            name=f'Report calibration ({method})',
            curve_type='linear',
            project=self.project,
            formula_params={'m': 0.01, 'c': -20.0},
            data_points=[{'v': 3000.0, 'f': 12.0}, {'v': 3200.0, 'f': 14.0},
                         {'v': 3400.0, 'f': 16.0}, {'v': 3600.0, 'f': 18.0}],
            se_adjustment_method=method,
            se_adjustment_factor=1.645,
        )

    def _activate(self, curve):
        from .models import ProjectCurveSetting
        setting, _ = ProjectCurveSetting.objects.get_or_create(
            project=self.project)
        setting.active_curve = curve
        setting.save()

    def test_no_policy_prints_the_honest_no_adjustment_statement(self):
        from apps.reports.ndt_reports import ecs_report_disclosure
        self._activate(self._curve('none'))
        paragraph, _ = ecs_report_disclosure(self.project)
        self.assertIn('standard error', paragraph.lower())
        self.assertIn('No standard-error adjustment is applied', paragraph)

    def test_bias_correction_is_disclosed_in_the_report(self):
        from apps.reports.ndt_reports import ecs_report_disclosure
        self._activate(self._curve('bias_correction'))
        paragraph, _ = ecs_report_disclosure(self.project)
        self.assertIn('Bias correction', paragraph)
        self.assertIn('+2.00 N/mm2', paragraph)

    def test_confidence_margin_is_disclosed_as_a_lower_bound(self):
        from apps.reports.ndt_reports import ecs_report_disclosure
        self._activate(self._curve('confidence_margin'))
        paragraph, _ = ecs_report_disclosure(self.project)
        self.assertIn('Confidence margin', paragraph)
        self.assertIn('characteristic strength', paragraph)

    def test_a_policy_that_cannot_apply_is_disclosed_as_such(self):
        # Requested but unsupported: the report must say the figures are
        # unadjusted, and why — never imply a correction that did not happen.
        from apps.reports.ndt_reports import ecs_report_disclosure
        from .models import StrengthCurve
        curve = StrengthCurve.objects.create(
            name='Unsupported adjustment', curve_type='linear',
            project=self.project,
            formula_params={'m': 0.01, 'c': -20.0},
            data_points=[{'v': 3000.0, 'f': 12.0}],
            se_adjustment_method='bias_correction')
        self._activate(curve)
        paragraph, _ = ecs_report_disclosure(self.project)
        self.assertIn('could not be applied', paragraph)
        self.assertIn('as fitted', paragraph)

    def test_aic_definition_is_carried_into_the_report(self):
        from apps.reports.ndt_reports import ecs_report_disclosure
        self._activate(self._curve('none'))
        paragraph, _ = ecs_report_disclosure(self.project)
        self.assertIn('Akaike Information Criterion', paragraph)

    def test_the_measurement_standard_caveat_uses_the_registry_code(self):
        from apps.reports.ndt_reports import ecs_report_disclosure
        self._activate(self._curve('none'))
        paragraph, _ = ecs_report_disclosure(self.project)
        self.assertIn('BS 1881-203:1986', paragraph)
        # The report previously printed a "BS 1881-203:1999" that does not
        # correspond to the document actually referenced.
        self.assertNotIn('1999', paragraph)

    def test_a_policy_in_force_states_where_the_error_is_applied(self):
        """Section 3.0 is the report's account of how f_cu was arrived at,
        so a policy in force must say there that the error moves the pulse
        velocity rather than the strength — the client's direction."""
        from apps.reports.ndt_reports import ecs_report_disclosure
        self._activate(self._curve('confidence_margin'))
        paragraph, _ = ecs_report_disclosure(self.project)
        self.assertIn('N/mm2 per m/s', paragraph)
        self.assertIn('199 m/s', paragraph)

    def test_a_project_with_no_policy_makes_no_such_claim(self):
        from apps.reports.ndt_reports import ecs_report_disclosure
        self._activate(self._curve('none'))
        paragraph, _ = ecs_report_disclosure(self.project)
        self.assertNotIn('N/mm2 per m/s', paragraph)

    def test_a_policy_the_data_cannot_support_makes_no_such_claim(self):
        """Requested but unapplied: the figures are as fitted, so there is
        no velocity step to describe and the report must not describe one."""
        from apps.reports.ndt_reports import ecs_report_disclosure
        from .models import StrengthCurve
        curve = StrengthCurve.objects.create(
            name='Unsupported margin', curve_type='linear',
            project=self.project,
            formula_params={'m': 0.01, 'c': -20.0},
            data_points=[{'v': 3000.0, 'f': 12.0}],
            se_adjustment_method='confidence_margin')
        self._activate(curve)
        paragraph, _ = ecs_report_disclosure(self.project)
        self.assertIn('could not be applied', paragraph)
        self.assertNotIn('N/mm2 per m/s', paragraph)


# ======================================================================
# The correction is applied to the VELOCITY (the client's method)
# ======================================================================

class CorrectionMovesThePulseVelocityTestCase(DigitalEyeAPITestBase):
    """
    The standard error is folded into the PULSE VELOCITY, before the
    conversion to f_cu — the client's direction, verbatim: "consider the
    standard error in the post velocity before we convert it to FCU", and
    "it will subsequently be added into the post velocity before the FCU
    report comes out".

    Abdulwahab worked it as 4,000 + 199 = 4,199 m/s. The 199 is not
    arbitrary: the error is in N/mm2 and the velocity is in m/s, so it is
    converted through the curve's own slope df/dV. On the curve quoted in
    the review (f = 0.01V - 20) the slope is 0.01 N/mm2 per m/s, and
    1.99 / 0.01 = 199 m/s exactly. These tests pin that exchange rate, the
    resulting step, and the direction of the move.
    """

    # f = 0.01V - 20 — the curve quoted in the review.
    LINEAR = {'m': 0.01, 'c': -20.0}
    # Deliberately off the line, so the fit carries a real residual
    # standard error for the correction to move the velocity by.
    #   predicted 10 / 12 / 14 / 16; residuals +0.4 +0.1 +0.3 +0.2
    #   SSE = 0.30, n - k = 2  =>  s = sqrt(0.15) = 0.3873 N/mm2
    PAIRS = [{'v': 3000, 'f': 10.4}, {'v': 3200, 'f': 12.1},
             {'v': 3400, 'f': 14.3}, {'v': 3600, 'f': 16.2}]

    def _curve(self, curve_type='linear', params=None, points=None,
               method='confidence_margin', factor=1.0,
               min_ms=None, max_ms=None):
        from .models import StrengthCurve
        return StrengthCurve.objects.create(
            name=f'velocity step {curve_type} {method}',
            curve_type=curve_type,
            project=self.project,
            formula_params=self.LINEAR if params is None else params,
            data_points=self.PAIRS if points is None else points,
            se_adjustment_method=method,
            se_adjustment_factor=factor,
            valid_range_min_ms=min_ms,
            valid_range_max_ms=max_ms,
        )

    def _activate(self, curve):
        from .models import ProjectCurveSetting
        setting, _ = ProjectCurveSetting.objects.get_or_create(
            project=self.project)
        setting.active_curve = curve
        setting.save()

    # -- the exchange rate between the two domains ----------------------

    def test_the_curve_slope_is_the_exchange_rate_between_the_domains(self):
        from .strength_curves import curve_slope_mpa_per_ms
        self.assertAlmostEqual(
            curve_slope_mpa_per_ms('linear', self.LINEAR, 4.0), 0.01,
            places=9)

    def test_the_clients_own_arithmetic_falls_out_of_the_slope(self):
        """1.99 N/mm2 against a slope of 0.01 N/mm2 per m/s IS 199 m/s —
        the 4,000 -> 4,199 m/s derived in the review."""
        from .strength_curves import curve_slope_mpa_per_ms
        slope = curve_slope_mpa_per_ms('linear', self.LINEAR, 4.0)
        self.assertAlmostEqual(1.99 / slope, 199.0, places=6)
        self.assertAlmostEqual(4000.0 + 1.99 / slope, 4199.0, places=6)

    def test_a_curved_law_has_a_slope_that_moves_with_the_velocity(self):
        """The exchange rate is local, not a constant: on a curved law the
        increment needed to produce a given error depends on where on the
        curve the measurement sits."""
        from .strength_curves import curve_slope_mpa_per_ms
        params = {'a': 4.0, 'b': 0.00035, 'c': 2.0}
        low = curve_slope_mpa_per_ms('exponential', params, 3.5)
        high = curve_slope_mpa_per_ms('exponential', params, 4.5)
        self.assertGreater(high, low)
        # At 3,800 m/s: df/dV = a*b*exp(b*V) = 0.0014 * exp(1.33)
        self.assertAlmostEqual(
            curve_slope_mpa_per_ms('exponential', params, 3.8),
            0.0014 * 2.718281828459045 ** 1.33, places=6)

    # -- the step itself ------------------------------------------------

    def test_the_step_is_the_error_divided_by_the_slope(self):
        from .strength_curves import apply_se_adjustment_at_velocity
        from .se_adjustment import standard_error_analysis

        curve = self._curve()
        standard_error = standard_error_analysis(curve)['standard_error_mpa']
        self.assertAlmostEqual(standard_error, 0.15 ** 0.5, places=3)

        _, disclosure = apply_se_adjustment_at_velocity(4.0, curve, n_points=1)
        step = disclosure['velocity_step']

        self.assertAlmostEqual(step['slope_mpa_per_ms'], 0.01, places=6)
        # A confidence margin deducts, so the velocity moves DOWN by
        # s / slope = 0.3873 / 0.01 = 38.73 m/s.
        self.assertAlmostEqual(step['delta_velocity_ms'],
                               -standard_error / 0.01, places=2)
        self.assertAlmostEqual(step['base_velocity_ms'], 4000.0, places=2)
        self.assertAlmostEqual(step['adjusted_velocity_ms'],
                               4000.0 - standard_error / 0.01, places=2)

    def test_a_bias_correction_moves_the_velocity_up(self):
        """The direction is the policy's, not a fixed sign: the "+2 to
        close the gap" convention raises the velocity, because a curve that
        under-predicts is corrected by making the concrete look faster."""
        from .strength_curves import apply_se_adjustment_at_velocity
        from .se_adjustment import standard_error_analysis

        curve = self._curve(method='bias_correction')
        bias = standard_error_analysis(curve)['mean_residual_mpa']
        self.assertGreater(bias, 0)  # this curve under-predicts on its pairs

        _, disclosure = apply_se_adjustment_at_velocity(4.0, curve, n_points=1)
        step = disclosure['velocity_step']
        self.assertAlmostEqual(step['delta_velocity_ms'], bias / 0.01, places=2)
        self.assertGreater(step['delta_velocity_ms'], 0)
        self.assertGreater(step['adjusted_velocity_ms'],
                           step['base_velocity_ms'])

    def test_a_linear_curve_gives_the_same_strength_either_way(self):
        """The client's method is not a different answer on a straight
        line — f = mV + c is affine, so moving V by SE/m raises f by exactly
        SE. Both routes must land on the same figure."""
        from .strength_curves import apply_se_adjustment_at_velocity
        from .se_adjustment import apply_se_adjustment

        curve = self._curve()
        base = curve.apply(4.0)
        strength_space, _ = apply_se_adjustment(base, curve, n_points=1)
        velocity_space, disclosure = apply_se_adjustment_at_velocity(
            4.0, curve, n_points=1)

        self.assertAlmostEqual(velocity_space, strength_space, places=6)
        self.assertAlmostEqual(disclosure['adjusted_f_cu_mpa'],
                               strength_space, places=4)

    def test_a_curved_law_is_where_the_two_methods_part_company(self):
        """On a curved law the velocity-domain figure is the one reported —
        it is the curve evaluated at the moved velocity, not a separately
        invented number. The two routes differ by the second-order term,
        and the client asked for the velocity route."""
        from .strength_curves import apply_se_adjustment_at_velocity
        from .se_adjustment import apply_se_adjustment

        curve = self._curve(curve_type='exponential',
                            params={'a': 4.0, 'b': 0.00035, 'c': 2.0})
        base = curve.apply(3.8)
        strength_space, _ = apply_se_adjustment(base, curve, n_points=1)
        velocity_space, disclosure = apply_se_adjustment_at_velocity(
            3.8, curve, n_points=1)

        # The reported figure is the curve at the moved velocity. The
        # velocity in the disclosure is rounded to 2 dp for display, so
        # re-applying it lands within a few thousandths of a MPa — close
        # enough to check the step by hand, which is what it is for.
        self.assertAlmostEqual(velocity_space, disclosure['adjusted_f_cu_mpa'],
                               places=4)
        moved_km_s = (disclosure['velocity_step']['adjusted_velocity_ms']
                      / 1000.0)
        self.assertAlmostEqual(velocity_space, curve.apply(moved_km_s),
                               places=3)
        # The two domains genuinely differ here — which is why the reported
        # one has to be the one the client asked for.
        self.assertGreater(abs(velocity_space - strength_space), 1e-4)

    def test_more_averaged_points_narrow_the_step(self):
        """The margin narrows with the number of points averaged, so the
        velocity moves less — the increment inherits the point count."""
        from .strength_curves import apply_se_adjustment_at_velocity

        curve = self._curve()
        _, one = apply_se_adjustment_at_velocity(4.0, curve, n_points=1)
        _, three = apply_se_adjustment_at_velocity(4.0, curve, n_points=3)
        self.assertLess(abs(three['velocity_step']['delta_velocity_ms']),
                        abs(one['velocity_step']['delta_velocity_ms']))
        # s/sqrt(3) — exactly the square-root narrowing, no more.
        self.assertAlmostEqual(
            three['velocity_step']['delta_velocity_ms'],
            one['velocity_step']['delta_velocity_ms'] / 3 ** 0.5, places=2)

    # -- what the disclosure has to carry -------------------------------

    def test_the_disclosure_carries_the_velocity_step(self):
        from .strength_curves import apply_se_adjustment_at_velocity

        curve = self._curve()
        _, disclosure = apply_se_adjustment_at_velocity(4.0, curve, n_points=1)
        step = disclosure['velocity_step']

        for key in ('slope_mpa_per_ms', 'delta_velocity_ms',
                    'base_velocity_ms', 'adjusted_velocity_ms'):
            self.assertIn(key, step)
        # The narrative names the slope and the increment in m/s, so the
        # figure can be checked by hand from the report alone.
        self.assertIn('slope', disclosure['detail'])
        self.assertIn('m/s', disclosure['detail'])
        self.assertIn('N/mm2', disclosure['detail'])

    def test_the_record_stores_the_velocity_domain_figure(self):
        """End to end: the figure apply_active_curve returns IS the
        velocity-domain one, and the snapshot on it explains the step."""
        from .strength_curves import apply_active_curve

        self._activate(self._curve())
        strength, snapshot = apply_active_curve(self.project, 4.0)
        disclosure = snapshot['se_adjustment']
        step = disclosure['velocity_step']

        self.assertTrue(disclosure['applied'])
        self.assertAlmostEqual(disclosure['adjusted_f_cu_mpa'], strength,
                               places=4)
        self.assertAlmostEqual(step['adjusted_velocity_ms'],
                               step['base_velocity_ms']
                               + step['delta_velocity_ms'], places=2)
        # The base is the uncorrected curve estimate; the reported figure is
        # strictly below it under a confidence margin.
        self.assertAlmostEqual(disclosure['base_f_cu_mpa'], 20.0, places=4)
        self.assertLess(strength, disclosure['base_f_cu_mpa'])

    # -- the honest refusals --------------------------------------------

    def test_a_move_off_the_calibrated_range_is_refused(self):
        """Moving the velocity must never walk off the calibrated range:
        this platform does not extrapolate, so the correction stays on the
        strength and the disclosure says why."""
        from .strength_curves import apply_se_adjustment_at_velocity
        from .se_adjustment import apply_se_adjustment

        # The margin deducts, so put the floor just under 4,000 m/s — the
        # base reading passes, the moved one does not.
        curve = self._curve(min_ms=3990.0, max_ms=4600.0)
        base = curve.apply(4.0)
        self.assertIsNotNone(base)

        strength_space, _ = apply_se_adjustment(base, curve, n_points=1)
        final, disclosure = apply_se_adjustment_at_velocity(
            4.0, curve, n_points=1)

        self.assertAlmostEqual(final, strength_space, places=6)
        self.assertIn('calibrated range', disclosure['detail'])
        # The step WAS computed — it was declined, not hidden.
        self.assertIsNotNone(disclosure['velocity_step'])
        self.assertLess(disclosure['velocity_step']['adjusted_velocity_ms'],
                        3990.0)

    def test_a_curve_with_no_slope_keeps_the_correction_on_the_strength(self):
        """A curve that is flat here has no exchange rate between N/mm2 and
        m/s. Rather than invent one, the correction stays on the strength."""
        from .strength_curves import apply_se_adjustment_at_velocity

        # b = 0 makes the exponential flat: f = a + c, so df/dV = 0.
        curve = self._curve(curve_type='exponential',
                            params={'a': 10.0, 'b': 0.0, 'c': 5.0})
        final, disclosure = apply_se_adjustment_at_velocity(
            4.0, curve, n_points=1)

        self.assertTrue(disclosure['applied'])
        self.assertIsNone(disclosure['velocity_step'])
        self.assertIn('slope', disclosure['detail'])
        self.assertAlmostEqual(disclosure['adjusted_f_cu_mpa'], final,
                               places=4)
        # A constant curve cannot move, so the strength-domain figure is the
        # only honest answer — and it is still reported in full. Hand
        # computation against f = 15: residuals -4.6, -2.9, -0.7, +1.2;
        # SSE = 31.5, n - k = 4 - 3 = 1  =>  s = sqrt(31.5) = 5.6125.
        self.assertAlmostEqual(final, 15.0 - 31.5 ** 0.5, places=4)

    def test_no_policy_configured_leaves_the_velocity_alone(self):
        from .strength_curves import apply_active_curve

        self._activate(self._curve(method='none'))
        strength, snapshot = apply_active_curve(self.project, 4.0)
        disclosure = snapshot['se_adjustment']

        self.assertFalse(disclosure['applied'])
        self.assertIsNone(disclosure.get('velocity_step'))
        self.assertAlmostEqual(strength, 20.0, places=6)  # 0.01 x 4000 - 20

    def test_the_velocity_step_definition_is_carried_into_the_report(self):
        """The client's action item was a self-explanatory document; the
        definition of where the error is applied is part of it."""
        from .se_adjustment import STAT_DEFINITIONS
        text = STAT_DEFINITIONS['velocity_step']
        self.assertIn('m/s', text)
        self.assertIn('1.99', text)
        self.assertIn('199', text)
