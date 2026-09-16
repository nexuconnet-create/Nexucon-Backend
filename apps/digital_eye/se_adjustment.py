"""
Standard-error handling for the Nexucon Link calibration (15 Sep 2026
review).

The client's direction, verbatim from the minutes:

  "Abdulwahab identified a standard error of 1.99 and directed the team to
   incorporate this value into calibration calculations."
  "Abdulwahab references Lagos State's practice of adding an adjustment
   factor (such as adding 2) to close the error gap for actual values."
  "Abdulwahab instructs ... adjust the calibration formula at the back end,
   and factor standard errors into post-velocity calculations before
   converting them into FCU reports."

WHAT A STANDARD ERROR ACTUALLY IS (worth stating, because the two things
below are routinely conflated):

The residual standard error of a regression,

    s = sqrt( SSE / (n - k) ),   SSE = sum((f_observed - f_predicted)^2)

is an unbiased estimate of the spread of the calibration pairs about the
fitted curve, in MPa. It measures SCATTER, not BIAS: on its own it does not
say the curve under- or over-predicts. It is the denominator of every
confidence statement about a prediction, which is why it is the right thing
to carry into the FCU calculation — but it is not itself a correction term.

Two genuinely different adjustments follow from the real calibration data,
and this module implements both because they answer different questions:

* BIAS CORRECTION — the "+2" convention.
  mean(f_observed - f_predicted) over the real calibration pairs IS this
  curve's systematic offset on this data. Adding it back is precisely the
  manual "add 2 to close the error gap" practice the client described, but
  MEASURED from the project's own cubes/cores instead of guessed, and it
  moves with the data as more pairs are recorded. Least squares through a
  free intercept forces this residual mean to ~0, so a non-zero value is
  real evidence that the chosen model form is biased on this data — which
  is exactly when the practice is warranted.
  Sign note: a positive mean residual means the curve UNDER-predicts, so
  adding it raises the reported strength. That is the upward "close the
  gap" direction Abdulwahab described.

* CONFIDENCE MARGIN — the characteristic strength.
  f_ck = f_mean - k*s, a LOWER confidence bound on the true strength rather
  than the central estimate. This is the logic BS EN 13791:2019 uses to
  derive a characteristic in-situ compressive strength from an indirect
  (UPV / rebound) correlation, and it is the conservative number an
  assessment against a design strength should be made on. k is the
  confidence factor: 1.0 is roughly the 84% one-sided bound, 1.645 the 95%
  one-sided bound.

  When the reported strength comes from the MEAN of n test points (the
  client's "system aggregates 3 test points ... using average values"), the
  uncertainty of that mean is s/sqrt(n), not s — so the margin narrows with
  more points. That is standard error of the mean, and it is why the
  per-element point count is threaded through apply_se_adjustment().

HONESTY RULES (unchanged platform-wide, and the reason nothing here is
hardcoded):
  * Every figure is computed from the curve's REAL stored calibration
    pairs. The client's 1.99 MPa and their "+2" are the OUTPUT of this
    maths on their data — they are never inputs. No constant is seeded.
  * No pairs, no regression, or zero residual degrees of freedom => no
    standard error => no adjustment is offered or applied, and the reason
    is stated. A curve with no error estimate is never silently treated as
    if it had one.
  * The default policy is 'none'. Nothing changes for an existing curve
    until a Director deliberately turns an adjustment on, and every applied
    adjustment is disclosed in the snapshot stored on the record it
    produced.
"""

import math

# The adjustment policies a StrengthCurve may carry.
ADJUSTMENT_METHODS = ('none', 'bias_correction', 'confidence_margin')

ADJUSTMENT_METHOD_LABELS = {
    'none': 'No adjustment — report the curve estimate as fitted',
    'bias_correction': 'Bias correction — add the measured mean residual',
    'confidence_margin': 'Confidence margin — subtract k x standard error',
}

# Minimum number of real calibration pairs before ANY adjustment may be
# applied. Two or three points cannot support an error estimate worth
# acting on; the platform refuses rather than pretending otherwise.
MIN_PAIRS_FOR_ADJUSTMENT = 3

# Plain-English explanations of the statistics, served to the UI and
# printed in reports (15 Sep 2026 action item: "Create a self-explanatory
# document referencing the R-square and AIC criteria"). Defined once here
# so the Curve Manager, the reports and the API can never disagree.
STAT_DEFINITIONS = {
    'r2_score': (
        'Coefficient of determination — the proportion of the variation '
        'in the measured strengths explained by the curve (1.0 = perfect). '
        'An R2 of 0.8856 means the curve explains 88.56% of the variation '
        'in the calibration data; the remaining 11.44% is scatter the '
        'curve does not capture.'),
    'standard_error': (
        'Residual standard error s = sqrt(SSE / (n - k)) — the typical '
        'distance, in N/mm2, between a calibration measurement and the '
        'curve. Smaller is tighter. It measures the SCATTER of the data '
        'about the curve, not whether the curve is biased high or low.'),
    'mean_residual': (
        'Mean(observed - predicted) across the calibration pairs — the '
        'systematic offset, or BIAS. Positive means the curve '
        'under-predicts. This is the figure behind the "+2 to close the '
        'gap" practice, measured from the project data rather than '
        'assumed.'),
    'aic': (
        'Akaike Information Criterion — a model-quality score that '
        'penalises extra parameters, so a curve cannot buy a better fit '
        'simply by adding terms. It only compares models fitted to the '
        'SAME calibration pairs; the lower value is the more efficient '
        'model, which is why it breaks R2 ties between curve types.'),
    'point_count': (
        'How many test points were averaged into the value shown. The '
        'element verdict is the MEAN of its points, so a curve that applies '
        'a confidence margin divides the standard error by sqrt(n) of them '
        '(15 Sep 2026 review: "three test points averaged"). Averaging more '
        'points buys a narrower margin on the element estimate — which is '
        'the reason the practice exists — and a single-reading element '
        'honestly earns none.'),
    'velocity_step': (
        'Where the standard error is applied. The error is in N/mm2 and the '
        'pulse velocity is in m/s, so the two are not directly additive: the '
        'error is converted into the velocity increment that produces it '
        'using the curve\'s own slope, df/dV (N/mm2 per m/s), and the '
        'velocity is moved by that amount BEFORE the conversion to strength '
        '(15 Sep 2026 direction). A standard error of 1.99 N/mm2 on a curve '
        'of slope 0.01 N/mm2 per m/s is therefore 199 m/s of pulse velocity. '
        'On a straight-line curve this gives exactly the same strength as '
        'correcting the strength directly; on a curved law it is the '
        'first-order equivalent, and the reported figure is whatever the '
        'curve yields at the moved velocity. The increment is shown on every '
        'figure it moved, so the step can be checked by hand.'),
    'margin_scope': (
        'What the confidence margin does and does not cover. It narrows '
        'with the number of points averaged at ONE element, because that '
        'mean is more repeatable. It is NOT applied to the standard error '
        'behind the 95% confidence interval or the probability below the '
        'design strength: that figure measures how well the CURVE fits its '
        'own calibration specimens, which averaging field readings at one '
        'element cannot improve. Those two are therefore reported at their '
        'full single-reading width — the conservative reading.'),
}

# WHICH registered document governs each statistic above. The 15 Sep 2026
# review asked for the definitions twice over ("show me a literature on
# this"; "let me share me a reference to it"), so the definition alone is
# not the whole answer — each one has to name its source.
#
# Every code here must exist in strength_curves.STANDARDS; a test asserts
# it, so a citation can never drift into naming a document the registry
# does not hold. Note that AIC is cited to its paper rather than to a
# standard: there is no BS/EN number for it, and inventing one would be
# exactly the kind of unverifiable reference the registry exists to stop.
STAT_REFERENCES = {
    'r2_score': ['BS EN 13791:2019'],
    'standard_error': ['BS EN 13791:2019'],
    'mean_residual': ['BS EN 13791:2019'],
    'aic': ['Akaike (1974)'],
    'point_count': ['BS EN 13791:2019', 'EN 12504-4:2021'],
    'velocity_step': ['BS EN 13791:2019'],
    'margin_scope': ['BS EN 13791:2019'],
}


def _finite(value):
    """float(value) when it is a usable finite number, else None."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def standard_error_analysis(curve):
    """
    The honest standard-error picture of a curve's own calibration.

    Returns a dict:
        n_pairs           int    real calibration pairs on the curve
        standard_error_mpa float|None  residual standard error s (MPa)
        mean_residual_mpa float|None   mean(f_obs - f_pred); +ve => the
                                       curve under-predicts on this data
        r2_score          float|None
        aic               float|None
        adjustment_available bool  whether an adjustment can be applied
        unavailable_reason str|None honest explanation when it cannot

    A lookup table is piecewise interpolation rather than a fitted model and
    has no residual standard error, so it reports the reason rather than a
    number. Curves with no stored pairs (the documented laboratory curve,
    manually entered parameters) report the same way.
    """
    from .strength_curves import curve_fit_stats

    curve_type = getattr(curve, 'curve_type', None)
    params = getattr(curve, 'formula_params', None) or {}
    data_points = getattr(curve, 'data_points', None) or []

    def _num(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    pairs = [p for p in data_points
             if isinstance(p, dict) and _num(p.get('v')) and _num(p.get('f'))]
    n_pairs = len(pairs)

    analysis = {
        'n_pairs': n_pairs,
        'standard_error_mpa': None,
        'mean_residual_mpa': None,
        'r2_score': _finite(getattr(curve, 'r2_score', None)),
        'aic': _finite(getattr(curve, 'aic', None)),
        'adjustment_available': False,
        'unavailable_reason': None,
    }

    if curve_type == 'lookup':
        analysis['unavailable_reason'] = (
            'A lookup table interpolates between its own points rather than '
            'fitting a model, so it has no residual standard error.')
        return analysis
    if n_pairs < MIN_PAIRS_FOR_ADJUSTMENT:
        analysis['unavailable_reason'] = (
            f'{n_pairs} stored calibration pair(s); at least '
            f'{MIN_PAIRS_FOR_ADJUSTMENT} are needed before a standard error '
            'is meaningful.')
        return analysis

    # Residuals against the curve's OWN parameters — the same engine the
    # reported strengths flow through, so the error describes the curve
    # that is actually applied. The valid-range gate is deliberately not
    # used here: the pairs ARE the calibration data.
    from .strength_curves import apply_curve_params

    residuals = []
    for pair in pairs:
        predicted = apply_curve_params(curve_type, params, pair['v'] / 1000.0,
                                       rebound_number=pair.get('r'))
        if predicted is None or not math.isfinite(predicted):
            analysis['unavailable_reason'] = (
                'The curve cannot evaluate one or more of its stored '
                'calibration pairs, so no honest error estimate exists.')
            return analysis
        residuals.append(float(pair['f']) - float(predicted))

    _, standard_error, _ = curve_fit_stats(curve_type, params, pairs)
    standard_error = _finite(standard_error)

    analysis['mean_residual_mpa'] = round(
        sum(residuals) / len(residuals), 4)
    analysis['standard_error_mpa'] = (None if standard_error is None
                                      else round(standard_error, 4))
    analysis['adjustment_available'] = standard_error is not None
    if standard_error is None:
        analysis['unavailable_reason'] = (
            'This fit has as many parameters as calibration pairs (zero '
            'residual degrees of freedom), so no standard error is '
            'computable. Record more calibration pairs.')
    return analysis


def apply_se_adjustment(base_f_cu_mpa, curve, n_points=None):
    """
    Apply the curve's standard-error policy to one f_cu value.

    ``base_f_cu_mpa`` is the curve's raw estimate for the measurement;
    ``n_points`` is how many test points were averaged to produce it (None
    or 1 for a single reading) — the confidence margin uses the standard
    error of that mean, s/sqrt(n).

    Returns ``(adjusted_f_cu_mpa, disclosure)``.

    ``disclosure`` is ALWAYS returned (never None) so every number the
    platform reports carries the account of how it was arrived at — the
    same provenance principle the curve snapshot follows. When no
    adjustment applies, ``disclosure['applied']`` is False and
    ``disclosure['detail']`` says why, in the client's own terms.

    Nothing is applied unless the curve carries a real policy AND real
    calibration pairs support the error estimate it needs. A missing input
    leaves the estimate exactly as fitted — never a silently invented
    correction.
    """
    method = getattr(curve, 'se_adjustment_method', 'none') or 'none'
    factor = _finite(getattr(curve, 'se_adjustment_factor', None))
    if factor is None:
        factor = 1.0

    points = None
    if n_points is not None:
        try:
            points = int(n_points)
        except (TypeError, ValueError):
            points = None
    if points is not None and points < 1:
        points = None

    disclosure = {
        'method': method,
        'method_label': ADJUSTMENT_METHOD_LABELS.get(method, method),
        'factor': factor,
        'n_points_averaged': points,
        'base_f_cu_mpa': None if base_f_cu_mpa is None
        else round(float(base_f_cu_mpa), 4),
        'adjusted_f_cu_mpa': None if base_f_cu_mpa is None
        else round(float(base_f_cu_mpa), 4),
        'standard_error_mpa': None,
        'mean_residual_mpa': None,
        'applied': False,
        # The policy narrative WITHOUT the resulting figure. The correction
        # is finally applied in the velocity domain (see
        # strength_curves.apply_se_adjustment_at_velocity), which re-derives
        # the figure — so the sentence describing WHAT the policy does is
        # kept apart from the sentence quoting the number it produced.
        'summary': '',
        'detail': '',
    }

    if base_f_cu_mpa is None:
        disclosure['detail'] = (
            'No strength estimate to adjust (no computable strength for '
            'this measurement).')
        return None, disclosure

    if method == 'none':
        disclosure['detail'] = (
            'No standard-error adjustment is configured on this curve; the '
            'figure is the curve estimate as fitted.')
        return float(base_f_cu_mpa), disclosure

    stats = standard_error_analysis(curve)
    disclosure['standard_error_mpa'] = stats['standard_error_mpa']
    disclosure['mean_residual_mpa'] = stats['mean_residual_mpa']
    if not stats['adjustment_available']:
        disclosure['detail'] = (
            'Standard-error adjustment requested but not applied: '
            f"{stats['unavailable_reason']} The figure is the curve "
            'estimate as fitted.')
        return float(base_f_cu_mpa), disclosure

    standard_error = stats['standard_error_mpa']
    base = float(base_f_cu_mpa)

    if method == 'bias_correction':
        bias = stats['mean_residual_mpa']
        adjusted = base + bias
        disclosure['adjusted_f_cu_mpa'] = round(adjusted, 4)
        disclosure['applied'] = True
        disclosure['summary'] = (
            f"Bias correction applied: the measured mean residual of this "
            f"curve's {stats['n_pairs']} real calibration pair(s) is "
            f"{bias:+.2f} N/mm2, so the correction is added to close the "
            f"gap between predicted and actual values")
        disclosure['detail'] = (
            f"{disclosure['summary']} "
            f"({base:.2f} -> {adjusted:.2f} N/mm2).")
        return adjusted, disclosure

    if method == 'confidence_margin':
        # A margin with k <= 0 is not a lower bound: it would report the raw
        # estimate (k = 0) or something above it (k < 0) while still being
        # labelled a conservative characteristic strength. Refuse instead of
        # mislabelling the figure.
        if factor <= 0:
            disclosure['detail'] = (
                f"A confidence margin needs a positive factor, but this "
                f"curve carries k = {factor:g}. No adjustment was applied; "
                'the figure is the curve estimate as fitted.')
            return base, disclosure
        divisor = math.sqrt(points) if points and points > 1 else 1.0
        half_width = factor * standard_error / divisor
        adjusted = base - half_width
        disclosure['adjusted_f_cu_mpa'] = round(adjusted, 4)
        disclosure['applied'] = True
        averaged = (f" and the standard error of the mean of the {points} "
                    f"test points averaged (s/sqrt(n) = "
                    f"{standard_error / divisor:.2f})"
                    if divisor > 1.0 else '')
        disclosure['summary'] = (
            f"Confidence margin applied: {factor:g} x the curve's "
            f"standard error of {standard_error:.2f} N/mm2{averaged} is "
            f"deducted, reporting a characteristic (lower-bound) strength "
            f"rather than the central estimate")
        disclosure['detail'] = (
            f"{disclosure['summary']} "
            f"({base:.2f} -> {adjusted:.2f} N/mm2).")
        return adjusted, disclosure

    # An unknown method string is a data defect, not a licence to invent a
    # correction — report the estimate as fitted.
    disclosure['detail'] = (
        f"Unknown standard-error adjustment method '{method}'; no "
        'adjustment was applied.')
    return base, disclosure


def analysis_payload(curve):
    """
    The standard-error analysis of a curve shaped for the API/UI, with the
    human-readable explanation of each statistic the client asked to have
    documented (meeting action item: "Create a self-explanatory document
    referencing the R-square and AIC criteria") and a recommendation for
    which adjustment, if any, the data supports.
    """
    stats = standard_error_analysis(curve)
    bias = stats['mean_residual_mpa']
    standard_error = stats['standard_error_mpa']

    recommendation = None
    if stats['adjustment_available']:
        if bias is not None and abs(bias) >= 0.5:
            recommendation = (
                f'The curve under-predicts by {abs(bias):.2f} N/mm2 on '
                'average across its own calibration pairs. Bias correction '
                'is the adjustment these data support.'
                if bias > 0 else
                f'The curve over-predicts by {abs(bias):.2f} N/mm2 on '
                'average across its own calibration pairs. A bias '
                'correction of a negative sign is what these data support.')
        else:
            recommendation = (
                'The calibration pairs are not systematically offset '
                '(mean residual within +/- 0.5 N/mm2), so a bias '
                'correction would add scatter rather than accuracy. If a '
                'conservative figure is wanted for assessment, use the '
                'confidence margin instead.')

    return {
        **stats,
        'method': getattr(curve, 'se_adjustment_method', 'none') or 'none',
        'factor': _finite(getattr(curve, 'se_adjustment_factor', None)) or 1.0,
        'min_pairs_required': MIN_PAIRS_FOR_ADJUSTMENT,
        'recommendation': recommendation,
        'definitions': dict(STAT_DEFINITIONS),
        'references': dict(STAT_REFERENCES),
    }
