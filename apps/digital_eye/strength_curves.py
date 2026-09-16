"""
Nexucon Link — the compressive-strength (f_cu) conversion-curve module
(8 Sep 2026 review meeting, "Neural Link", + the client's Nexucon Link
technical specification).

Why this module exists: the platform previously converted every pulse
velocity to a compressive strength through ONE fixed curve (f_cu = 8.961 x V
- 7.97). The client's engineering position (Abdulwahab Onike, 8 Sep) is that
no universal UPV-to-strength relationship exists — the correlation is
project-specific (mix design, aggregates, materials). Nexucon Link therefore
manages MULTIPLE conversion curves (linear / polynomial / exponential /
SonReb / lookup), fits project-specific curves from real calibration data
(UPV + rebound hammer + destructive cube tests) with a regression engine,
and lets each project activate the curve its calibration supports.

Curve parameter domain is METRES PER SECOND (m/s) — the unit the client's
specification, calibration CSVs and the field UI use. The platform's
canonical storage stays velocity_km_s (mm/us == km/s), so every application
call converts km/s -> m/s exactly once, here.

Honesty rules (unchanged platform-wide):
  * A strength is only ever produced INSIDE the curve's calibrated range —
    velocities outside it get None, never an extrapolated number.
  * Curves are seeded/created from REAL data only. The one seeded default is
    the laboratory's documented linear curve; the other curve types exist in
    the engine but no fabricated default parameters are invented for them.
  * SonReb needs a rebound value; without one the result is None.
  * Lookup tables interpolate inside their span and refuse outside it.
"""
import logging
import math

logger = logging.getLogger(__name__)

# Curve types supported by the engine (client spec §B.1 curve_type_enum).
CURVE_TYPES = ('linear', 'polynomial', 'exponential', 'sonreb', 'lookup')

# ---------------------------------------------------------------------------
# Standards registry — WHICH documents the model actually rests on.
#
# Meeting action item (15 Sep 2026): "Verify Standard: Document the specific
# building standards or codes used for the mathematical model. Confirm these
# references with the project guidelines to ensure accuracy."
#
# The 15 Sep minutes record the team adopting "BS 1881-23" as the reference
# for the mathematical model. No such standard exists. The document the team
# is actually working to is BS 1881-203:1986 — "Testing concrete. Part 203:
# Recommendations for measurement of velocity of ultrasonic pulses in
# concrete" — which is what the platform's curve code has cited throughout
# (see apps/reports/ndt_reports.py ECS_CALIBRATION_SOURCE). The record below
# states that plainly rather than silently repeating a number that cannot be
# checked, and lists the correlation standard that genuinely governs turning
# a UPV reading into a strength (BS EN 13791), which the earlier build did
# not cite at all.
#
# Each entry: code, title, scope, and the role it plays in this platform.
# 'role' is what the UI groups by, so the registry reads as an answer to
# "which standard covers which part of this number?".
#
# The 'statistics' role was added for the same review's other half: asked
# what R2 / SE / AIC mean, the client asked twice over for a REFERENCE
# ("show me a literature on this"; "let me share me a reference to it").
# R2 and the standard error are genuinely governed by BS EN 13791, which
# sets out the regression relationship and the treatment of its error. AIC
# is not a concrete standard and has no BS/EN number — it is Akaike's 1974
# paper, and citing anything else for it would be an invented reference.
# se_adjustment.STAT_REFERENCES ties each statistic to a code in this list.
# ---------------------------------------------------------------------------

STANDARDS = [
    {
        'code': 'BS 1881-203:1986',
        'title': ('Testing concrete — Part 203: Recommendations for '
                  'measurement of velocity of ultrasonic pulses in concrete'),
        'role': 'measurement',
        'role_label': 'Test method',
        'scope': (
            'Apparatus, procedure and the three transducer arrangements '
            '(direct / semi-direct / indirect) for measuring pulse velocity '
            'through concrete, plus the factors that influence it. Section '
            '11 covers correlation of pulse velocity with strength and '
            'states that no unique relationship exists for all concretes.'),
        'platform_use': (
            'The measurement basis for every PUNDIT test recorded on the '
            'platform, and the source of the caveat printed in report '
            'Section 3.0 that the velocity-strength relationship must be '
            'project-specific.'),
        'note': ('This is the document minuted as "BS 1881-23" on 15 Sep '
                 '2026 — no standard numbered BS 1881-23 exists. Confirm '
                 'with the project guidelines.'),
    },
    {
        'code': 'BS EN 13791:2019',
        'title': ('Assessment of in-situ compressive strength in structures '
                  'and precast concrete components'),
        'role': 'correlation',
        'role_label': 'In-situ strength from indirect tests',
        'scope': (
            'How to derive an in-situ compressive strength — including a '
            'characteristic value — from indirect methods such as '
            'ultrasonic pulse velocity and rebound number, calibrated '
            'against cores. Sets out the regression relationship and the '
            'confidence/tolerance treatment of its error.'),
        'platform_use': (
            'The basis for the calibration workflow itself: fitting a '
            'project-specific curve from real core/cube pairs, and for the '
            'confidence-margin standard-error adjustment (a lower-bound '
            'characteristic strength rather than the central estimate).'),
        'note': None,
    },
    {
        'code': 'EN 12504-4:2021',
        'title': ('Testing concrete in structures — Part 4: Determination of '
                  'ultrasonic pulse velocity'),
        'role': 'measurement',
        'role_label': 'Test method (EU)',
        'scope': ('The current European test method for ultrasonic pulse '
                  'velocity in structures, and the practical rules for '
                  'repeat readings at a test station.'),
        'platform_use': (
            'The basis for the platform\'s data-quality scoring: the '
            'requirement for repeat points per element and the '
            'within-element spread that grades HIGH / MEDIUM / LOW.'),
        'note': None,
    },
    {
        'code': 'ACI 228.2R-2018',
        'title': ('Report on Nondestructive Test Methods for Evaluation of '
                  'Concrete in Structures'),
        'role': 'correlation',
        'role_label': 'NDT strength correlation',
        'scope': (
            'NDT methods, the correlation models used to convert them to '
            'strength (including the exponential form), and the '
            'temperature correction for pulse velocity outside the 5-30 '
            'degC band. Section 5 covers data management and provenance.'),
        'platform_use': (
            'The exponential correlation model the client designated as the '
            'default curve type, the temperature correction applied before '
            'a velocity produces a strength, and the audit/provenance '
            'principle behind recording the curve snapshot on every '
            'result.'),
        'note': None,
    },
    {
        'code': 'ASTM C597',
        'title': ('Standard Test Method for Pulse Velocity Through Concrete'),
        'role': 'measurement',
        'role_label': 'Test method (US)',
        'scope': ('The US test method for pulse velocity through concrete, '
                  'covering apparatus, path length and transit-time '
                  'measurement.'),
        'platform_use': (
            'The alternative measurement reference named in the Nexucon '
            'Link specification alongside BS 1881-203.'),
        'note': None,
    },
    {
        'code': 'Akaike (1974)',
        'title': ('A New Look at the Statistical Model Identification — '
                  'IEEE Transactions on Automatic Control, 19(6), 716-723'),
        'role': 'statistics',
        'role_label': 'Model selection',
        'scope': (
            'The source of the Akaike Information Criterion. AIC scores how '
            'well a model explains the data while charging it for the number '
            'of parameters it uses, so a curve cannot buy a better fit simply '
            'by adding terms. It is a RELATIVE score: it ranks the candidate '
            'models it is given, and has no meaning on its own.'),
        'platform_use': (
            'Breaks R2 ties between the candidate curve types on the '
            'Regression Fits panel, where several models are fitted to the '
            'same calibration pairs and the closest R2 values need a '
            'tie-break. Lower is the more efficient model. Which curve wins '
            'is project-specific and is decided by that project\'s own '
            'data, never by this registry entry.'),
        'note': ('AIC only compares models fitted to the SAME calibration '
                 'pairs, and says nothing about absolute accuracy — a low AIC '
                 'on a poorly-conditioned fit is still a poor fit. Read it '
                 'beside R2 and the standard error, never instead of them.'),
    },
]

STANDARD_CODES = [s['code'] for s in STANDARDS]


def standards_registry():
    """The standards the model rests on, as an ordered list of dicts."""
    return [dict(entry) for entry in STANDARDS]

# Minimum calibration points per model (parameters + at least one residual
# degree of freedom). The client's reliability guidance (8 Sep meeting) is
# 9-15 points minimum, ~40+ for high reliability — that advisory is reported
# by the regression engine, not enforced as a hard block.
MIN_POINTS = {'linear': 2, 'polynomial': 3, 'exponential': 3, 'sonreb': 4}
RELIABILITY_ADVISORY_MIN = 9


# ---------------------------------------------------------------------------
# Plausibility guard on a calibration strength (15 Sep 2026 review)
# ---------------------------------------------------------------------------
# The client caught a core sample recorded with a laboratory strength of
# 150 N/mm2: the core's 100 x 150 mm dimensions had been typed into the
# strength field. "150 MPa is crazy. Can't get that from concrete. The
# highest I've heard about is 60."
#
# This matters more than a wrong figure on one screen. A calibration pair is
# GROUND TRUTH: it is fed to the regression, and the fitted curve is what
# every strength on the platform is then read from. A mis-entered pair does
# not stay local — it moves the curve, and with it every reported strength.
#
# The ceiling exists to catch that transcription error, and is deliberately
# a guard against mis-entry rather than a materials-science limit:
# ultra-high-performance concrete (UHPC) does exceed it. A laboratory that
# genuinely assesses UHPC raises this constant on purpose, rather than the
# platform quietly accepting a typo as data.
PLAUSIBLE_STRENGTH_MAX_MPA = 100.0


def strength_plausibility_error(f_mpa):
    """Why `f_mpa` cannot be a concrete strength, or None when it can.

    The single place this judgement is made, so every calibration entry
    point — a core sample, a manually typed pair, an uploaded file — applies
    the same one and words it the same way. A None reading (no value
    recorded) is never implausible; it simply is not a strength yet.
    """
    if f_mpa is None:
        return None
    try:
        value = float(f_mpa)
    except (TypeError, ValueError):
        return 'Compressive strength must be a number.'
    if math.isfinite(value) and value > PLAUSIBLE_STRENGTH_MAX_MPA:
        return (f'{value:g} N/mm2 is above the {PLAUSIBLE_STRENGTH_MAX_MPA:g} '
                'N/mm2 ceiling this platform accepts as a concrete strength. '
                'That is almost always a mis-entry — a core dimension such as '
                '100 x 150 typed into the strength field. Check the '
                'laboratory certificate before recording the pair.')
    return None


# ---------------------------------------------------------------------------
# Built-in default (the laboratory's documented fixed curve — see
# apps/reports/ndt_reports.py ECS_CALIBRATION_SOURCE for its provenance).
# ---------------------------------------------------------------------------

def _builtin_default_params():
    """The fixed laboratory curve expressed in the module's m/s domain.

    f_cu = 8.961 x V_km_s - 7.97 == f_cu = 0.008961 x V_m_s - 7.97.
    Params live in the SAME dict shape a stored StrengthCurve row uses so
    the fallback path and the DB path are interchangeable.
    """
    from apps.reports.ndt_reports import (
        ECS_SLOPE, ECS_INTERCEPT, ECS_VALID_MIN_KM_S, ECS_VALID_MAX_KM_S,
    )
    return {
        'curve_type': 'linear',
        'formula_params': {'m': ECS_SLOPE / 1000.0, 'c': ECS_INTERCEPT},
        'valid_range_ms': [ECS_VALID_MIN_KM_S * 1000.0, ECS_VALID_MAX_KM_S * 1000.0],
    }


BUILTIN_CURVE_NAME = 'Laboratory fixed calibration (built-in)'


def builtin_curve_snapshot(velocity_km_s=None, temperature_c=None):
    """Snapshot of the built-in fallback curve (used when no DB curve is
    seeded yet — identical maths to the seeded default)."""
    p = _builtin_default_params()
    return {
        'curve_id': None,
        'name': BUILTIN_CURVE_NAME,
        'curve_type': 'linear',
        'standard': 'BS 1881-203:1999',
        'formula': formula_display('linear', p['formula_params']),
        'formula_params': p['formula_params'],
        'valid_range_ms': p['valid_range_ms'],
        'r2_score': None,
        'standard_error': None,  # fixed curve: no regression, no error estimate
        'provenance_source': 'Laboratory fixed calibration curve (documented in report section 3.0)',
        'temperature_correction_applied': temperature_correction_applied(temperature_c),
        'se_adjustment': None,
    }


# ---------------------------------------------------------------------------
# Temperature correction (client spec, ACI 228.2R-2018 reference): applied
# only outside the 5-30 degC band, to the velocity used for the STRENGTH
# computation. The stored/displayed velocity always stays the raw measured
# value — the correction is a strength-input adjustment, disclosed in the
# curve snapshot, never a silent edit of field data.
# ---------------------------------------------------------------------------

TEMP_CORRECTION_MIN_C = 5.0
TEMP_CORRECTION_MAX_C = 30.0


def temperature_correction_applied(temperature_c):
    return temperature_c is not None and not (
        TEMP_CORRECTION_MIN_C <= temperature_c <= TEMP_CORRECTION_MAX_C)


def temperature_corrected_velocity_km_s(velocity_km_s, temperature_c):
    """V * (1 + 0.002 * (T - 20)) when T is outside 5-30 degC, else V."""
    if velocity_km_s is None:
        return None
    if not temperature_correction_applied(temperature_c):
        return velocity_km_s
    return velocity_km_s * (1.0 + 0.002 * (temperature_c - 20.0))


# ---------------------------------------------------------------------------
# Curve application
# ---------------------------------------------------------------------------

def apply_curve_params(curve_type, params, velocity_km_s,
                       rebound_number=None, valid_range_ms=None):
    """
    f_cu (MPa) from a curve given its type + params. Velocity arrives in the
    platform's canonical km/s and is converted to the m/s domain once.

    Returns None — never an invented number — when the velocity is missing,
    outside the curve's valid range, the params are unusable, or a SonReb
    curve is applied without a rebound value.
    """
    if velocity_km_s is None or velocity_km_s <= 0:
        return None
    if valid_range_ms is not None:
        lo, hi = valid_range_ms
        v_ms = velocity_km_s * 1000.0
        if not (lo <= v_ms <= hi):
            return None
    try:
        if curve_type == 'linear':
            return params['m'] * (velocity_km_s * 1000.0) + params['c']
        if curve_type == 'polynomial':
            # coeffs ascend: coeffs[0] + coeffs[1]*V + coeffs[2]*V^2 + ...
            v_ms = velocity_km_s * 1000.0
            return sum(float(c) * (v_ms ** i)
                       for i, c in enumerate(params['coeffs']))
        if curve_type == 'exponential':
            v_ms = velocity_km_s * 1000.0
            value = params['a'] * math.exp(params['b'] * v_ms) + params['c']
            return value if math.isfinite(value) else None
        if curve_type == 'sonreb':
            if rebound_number is None or rebound_number <= 0:
                return None
            v_ms = velocity_km_s * 1000.0
            value = (params['a'] * (v_ms ** params['b'])
                     * (rebound_number ** params['c']))
            return value if math.isfinite(value) else None
        if curve_type == 'lookup':
            points = sorted(params['points'], key=lambda p: p['v'])
            if len(points) < 2:
                return None
            v_ms = velocity_km_s * 1000.0
            if v_ms < points[0]['v'] or v_ms > points[-1]['v']:
                # Outside the table's span: no extrapolation, ever.
                return None
            for i in range(len(points) - 1):
                p1, p2 = points[i], points[i + 1]
                if p1['v'] <= v_ms <= p2['v']:
                    if p2['v'] == p1['v']:
                        return p1['f']
                    return (p1['f'] + (p2['f'] - p1['f'])
                            * (v_ms - p1['v']) / (p2['v'] - p1['v']))
            return points[-1]['f']
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None
    return None


def curve_slope_mpa_per_ms(curve_type, params, velocity_km_s,
                           rebound_number=None):
    """
    df/dV of the curve at this velocity, in N/mm2 per m/s.

    This is the exchange rate between the two domains, and it is what makes
    the 15 Sep 2026 client direction arithmetically possible: a standard
    error is in N/mm2 and the pulse velocity is in m/s, so the error is
    turned into the velocity increment that PRODUCES it through the curve's
    own local slope. Abdulwahab's worked example is exactly this —
    SE = 1.99 N/mm2 against his curve f = 0.01V - 20 gives
    1.99 / 0.01 = 199 m/s, which is the 4,000 -> 4,199 m/s he derived.

    On a linear curve moving the velocity by SE/m raises f by exactly SE,
    so the result is identical to correcting the strength directly; on a
    curved law it is the first-order equivalent, and the reported figure is
    whatever the curve yields at the moved velocity.

    Returns None when the curve has no single slope here (a lookup table
    segment with zero width, a SonReb curve without its rebound value,
    unusable parameters) — the caller must then decline to move the
    velocity rather than guess a slope.
    """
    if velocity_km_s is None or velocity_km_s <= 0:
        return None
    try:
        v_ms = velocity_km_s * 1000.0
        if curve_type == 'linear':
            slope = params['m']
        elif curve_type == 'polynomial':
            coeffs = params['coeffs']
            slope = sum(float(c) * i * (v_ms ** (i - 1))
                        for i, c in enumerate(coeffs) if i >= 1)
        elif curve_type == 'exponential':
            slope = params['a'] * params['b'] * math.exp(params['b'] * v_ms)
        elif curve_type == 'sonreb':
            if rebound_number is None or rebound_number <= 0:
                return None
            slope = (params['a'] * params['b'] * (v_ms ** (params['b'] - 1))
                     * (rebound_number ** params['c']))
        elif curve_type == 'lookup':
            # Piecewise linear: the slope of the segment the velocity sits
            # in, which is the curve's real slope everywhere it is defined.
            points = sorted(params['points'], key=lambda p: p['v'])
            if len(points) < 2 or v_ms < points[0]['v'] or v_ms > points[-1]['v']:
                return None
            slope = None
            for i in range(len(points) - 1):
                p1, p2 = points[i], points[i + 1]
                if p1['v'] <= v_ms <= p2['v']:
                    if p2['v'] == p1['v']:
                        return None
                    slope = (p2['f'] - p1['f']) / (p2['v'] - p1['v'])
                    break
            if slope is None:
                return None
        else:
            return None
        slope = float(slope)
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None
    return slope if math.isfinite(slope) else None


# Curve coefficients have a documented decimal standard (15 Sep 2026
# client direction: "we need to have those the minimum decimal point that
# is allowable for the A, B, C ... let's make it a standard"). Before this,
# the formula was rendered with Python's ":g", which prints a value at six
# SIGNIFICANT digits — so the same curve could show A as "0.0012" beside a
# B of "1.2e-05", and a certificate reader had to take in scientific
# notation to read a coefficient. A curve's coefficients are the record of
# what was applied, so they are shown at a fixed, comparable precision.
CURVE_PARAM_DECIMALS = 6


def format_coefficient(value):
    """A curve coefficient at the platform's documented decimal standard.

    Fixed-point at CURVE_PARAM_DECIMALS places, trailing zeros dropped, so
    two coefficients on the same curve can be read and compared at one
    precision. Every coefficient that resolution can express is shown
    without scientific notation.

    A coefficient below that resolution (|value| < 10 ** -CURVE_PARAM_DECIMALS)
    cannot be written in the standard's places without rounding it to a "0"
    it is not — which would be a false reading of the curve. Those keep their
    significant digits, and for an extreme value that means engineering
    notation. That is the honest exception, not the normal case: a real
    calibration's A and B sit far above this threshold.
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(number):
        return str(value)
    if number == 0:
        return '0'
    text = f'{number:.{CURVE_PARAM_DECIMALS}f}'
    if float(text) == 0:
        return f'{number:g}'
    if '.' in text:
        text = text.rstrip('0').rstrip('.')
    return text or '0'


def formula_display(curve_type, params):
    """Human-readable formula string for reports and UI — the exact formula
    applied, in the m/s parameter domain."""
    c = format_coefficient
    try:
        if curve_type == 'linear':
            sign = '-' if params['c'] < 0 else '+'
            return f"f_cu = {c(params['m'])} x V {sign} {c(abs(params['c']))}"
        if curve_type == 'polynomial':
            terms = ' + '.join(
                f"{c(x)} x V^{i}" if i > 1
                else (f"{c(x)} x V" if i == 1 else f"{c(x)}")
                for i, x in enumerate(params['coeffs']))
            return f"f_cu = {terms}"
        if curve_type == 'exponential':
            return (f"f_cu = {c(params['a'])} x exp({c(params['b'])} x V)"
                    + (f" - {c(abs(params['c']))}" if params['c'] < 0
                       else f" + {c(params['c'])}"))
        if curve_type == 'sonreb':
            return (f"f_cu = {c(params['a'])} x V^{c(params['b'])} "
                    f"x R^{c(params['c'])}  (R = rebound number)")
        if curve_type == 'lookup':
            return "f_cu = piecewise-linear lookup table"
    except (KeyError, TypeError, ValueError):
        pass
    return f"f_cu = {curve_type} curve (parameters on record)"


# ---------------------------------------------------------------------------
# Active-curve resolution + application (the path every f_cu flows through)
# ---------------------------------------------------------------------------

def validate_curve_params(curve_type, params):
    """
    Structural validation of a parameter set BEFORE it is persisted. Raises
    ValueError with a human-readable reason on any defect; returns None when
    the set is well-formed for its type. Numbers themselves are not judged
    here — apply_curve_params guards the maths at use time.
    """
    if not isinstance(params, dict):
        raise ValueError('Curve parameters must be a JSON object.')

    def _number(key):
        if key not in params:
            raise ValueError(f"'{key}' is required.")
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"'{key}' must be a number.")
        return value

    if curve_type == 'linear':
        _number('m')
        _number('c')
    elif curve_type == 'polynomial':
        coeffs = params.get('coeffs')
        if (not isinstance(coeffs, list) or len(coeffs) < 3
                or not all(isinstance(c, (int, float)) and not isinstance(c, bool)
                           for c in coeffs)):
            raise ValueError("'coeffs' must be a list of at least 3 numbers "
                             '[c0, c1, c2] in ascending powers of V.')
    elif curve_type == 'exponential':
        _number('a')
        _number('b')
        _number('c')
    elif curve_type == 'sonreb':
        _number('a')
        _number('b')
        _number('c')
    elif curve_type == 'lookup':
        points = params.get('points')
        if not isinstance(points, list) or len(points) < 2:
            raise ValueError("'points' must be a list of at least 2 "
                             "{'v': <m/s>, 'f': <MPa>} pairs.")
        seen = []
        for p in points:
            if not isinstance(p, dict) or 'v' not in p or 'f' not in p:
                raise ValueError("Each lookup point needs 'v' (m/s) and 'f' (MPa).")
            v, f = p['v'], p['f']
            if isinstance(v, bool) or isinstance(f, bool) \
                    or not isinstance(v, (int, float)) \
                    or not isinstance(f, (int, float)):
                raise ValueError("Lookup 'v' and 'f' values must be numbers.")
            if v <= 0:
                raise ValueError("Lookup 'v' values must be positive (m/s).")
            seen.append(float(v))
        if len(set(seen)) != len(seen):
            raise ValueError('Lookup table has duplicate velocity values.')
    else:
        raise ValueError(f"Unknown curve type '{curve_type}'.")


def resolve_active_curve(project):
    """
    The curve a project's strength computations use:
    the project's activated curve when set, else the platform default,
    else None (caller falls back to the built-in fixed curve).
    """
    if project is not None:
        setting = getattr(project, 'curve_setting', None)
        if setting is not None and setting.active_curve_id:
            curve = setting.active_curve
            if curve is not None:
                return curve
    from .models import StrengthCurve
    return StrengthCurve.objects.filter(is_default=True).first()


def curve_snapshot(curve, temperature_c=None):
    """JSON snapshot of the curve that produced a strength value — the
    formula provenance stored on every reading/test (client spec §B.1
    formula_snapshot_json)."""
    if curve is None:
        return builtin_curve_snapshot(temperature_c=temperature_c)
    return {
        'curve_id': str(curve.id),
        'name': curve.name,
        'curve_type': curve.curve_type,
        'standard': curve.standard,
        'formula': formula_display(curve.curve_type, curve.formula_params),
        'formula_params': curve.formula_params,
        'valid_range_ms': [curve.valid_range_min_ms, curve.valid_range_max_ms],
        'r2_score': curve.r2_score,
        'standard_error': curve.standard_error,
        'provenance_source': (curve.provenance or {}).get('source'),
        'temperature_correction_applied': temperature_correction_applied(temperature_c),
        'se_adjustment': None,
    }


def apply_se_adjustment_at_velocity(velocity_km_s, curve, n_points=None,
                                    rebound_number=None):
    """
    The client's method, end to end: fold the curve's standard error into
    the PULSE VELOCITY, then convert to f_cu — 15 Sep 2026, "consider the
    standard error in the post velocity before we convert it to FCU",
    illustrated as 4,000 + 199 = 4,199 m/s.

    The two domains are not directly additive (a standard error is in N/mm2,
    a velocity in m/s), so the error is converted through the curve's own
    local slope into the velocity increment that PRODUCES it, the velocity
    is moved by that increment, and the curve is re-applied. On a linear
    curve this gives exactly the same strength as correcting the strength
    directly, because f = mV + c is affine in V; on a curved law it is the
    first-order equivalent.

    The reported figure is always what the curve yields at the moved
    velocity — never a separately invented number. When the velocity cannot
    be moved honestly (no usable slope, or moving it would leave the
    calibrated range, where this platform never extrapolates) the
    correction is left on the strength and the disclosure says why.

    Returns ``(f_cu_mpa_or_None, disclosure)``.
    """
    from .se_adjustment import apply_se_adjustment

    base = curve.apply(velocity_km_s, rebound_number=rebound_number)
    adjusted, disclosure = apply_se_adjustment(base, curve, n_points=n_points)
    if base is None or not disclosure.get('applied'):
        return adjusted, disclosure

    delta_mpa = float(adjusted) - float(base)
    if delta_mpa == 0:
        return adjusted, disclosure

    slope = curve_slope_mpa_per_ms(
        getattr(curve, 'curve_type', None),
        getattr(curve, 'formula_params', None) or {},
        velocity_km_s, rebound_number=rebound_number)
    if slope is None or slope == 0:
        disclosure['velocity_step'] = None
        disclosure['detail'] = (
            f"{disclosure['summary']} "
            f"({base:.2f} -> {adjusted:.2f} N/mm2). The curve has no usable "
            'slope at this velocity, so the correction stays on the strength '
            'rather than moving a pulse velocity by a slope that does not '
            'exist.')
        return adjusted, disclosure

    delta_v_ms = delta_mpa / slope
    moved_v_km_s = velocity_km_s + delta_v_ms / 1000.0
    step = {
        'slope_mpa_per_ms': round(slope, 6),
        'delta_velocity_ms': round(delta_v_ms, 2),
        'base_velocity_ms': round(velocity_km_s * 1000.0, 2),
        'adjusted_velocity_ms': round(moved_v_km_s * 1000.0, 2),
    }
    final = curve.apply(moved_v_km_s, rebound_number=rebound_number)
    if final is None:
        # Moving the velocity left the calibrated range. Extrapolating to
        # manufacture a figure here would be exactly the invention this
        # platform refuses everywhere else.
        disclosure['velocity_step'] = step
        disclosure['detail'] = (
            f"{disclosure['summary']} "
            f"({base:.2f} -> {adjusted:.2f} N/mm2). Moving the pulse velocity "
            f"by {delta_v_ms:+.1f} m/s would leave the curve's calibrated "
            'range, where this platform never extrapolates, so the '
            'correction is reported on the strength instead.')
        return adjusted, disclosure

    disclosure['velocity_step'] = step
    disclosure['adjusted_f_cu_mpa'] = round(float(final), 4)
    disclosure['detail'] = (
        f"{disclosure['summary']}. Applied to the pulse velocity as "
        f"directed: at this velocity the curve's slope is "
        f"{slope:.4f} N/mm2 per m/s, so the correction is "
        f"{delta_v_ms:+.1f} m/s — pulse velocity "
        f"{velocity_km_s * 1000:,.0f} -> {moved_v_km_s * 1000:,.0f} m/s, "
        f"giving {base:.2f} -> {final:.2f} N/mm2.")
    return float(final), disclosure


def apply_active_curve(project, velocity_km_s, rebound_number=None,
                       temperature_c=None, n_points=None):
    """
    THE f_cu path: resolve the project's active curve, temperature-correct
    the velocity (ACI 228.2R band), apply the curve's standard-error policy
    TO THE VELOCITY, then convert (15 Sep 2026 client direction — the
    standard error is folded into the post-velocity before the f_cu
    conversion).

    ``n_points`` is how many test points were averaged into this velocity
    (None for a single reading). The confidence-margin adjustment uses the
    standard error of that mean rather than of a single measurement.

    Returns (strength_mpa_or_None, curve_snapshot). The snapshot is always
    returned so every stored strength carries its formula provenance —
    including the honest built-in fallback when no curve is seeded — and it
    now also carries the ``se_adjustment`` disclosure describing exactly
    how the standard error moved the figure, or why it did not, including
    the velocity increment it was applied as.

    The adjustment is inert ('none') on every curve until a Director
    deliberately enables one, so existing figures are unaffected.
    """
    from .se_adjustment import apply_se_adjustment

    corrected_v = temperature_corrected_velocity_km_s(velocity_km_s, temperature_c)
    curve = resolve_active_curve(project)
    if curve is None:
        p = _builtin_default_params()
        strength = apply_curve_params(
            p['curve_type'], p['formula_params'], corrected_v,
            rebound_number=rebound_number,
            valid_range_ms=p['valid_range_ms'])
        # The built-in laboratory curve carries no regression, so there is
        # no standard error to adjust by — disclosed, never assumed away.
        snapshot = builtin_curve_snapshot(temperature_c=temperature_c)
        adjusted, disclosure = apply_se_adjustment(strength, None,
                                                   n_points=n_points)
        snapshot['se_adjustment'] = disclosure
        return adjusted, snapshot

    # The standard-error policy is applied IN THE VELOCITY DOMAIN, before
    # this conversion — the client's method (15 Sep 2026, "consider the
    # standard error in the post velocity before we convert it to FCU").
    # apply_se_adjustment_at_velocity re-derives the figure from the moved
    # velocity, so the strength is not computed here first.
    adjusted, disclosure = apply_se_adjustment_at_velocity(
        corrected_v, curve, n_points=n_points, rebound_number=rebound_number)
    snapshot = curve_snapshot(curve, temperature_c=temperature_c)
    snapshot['se_adjustment'] = disclosure
    return adjusted, snapshot


# ---------------------------------------------------------------------------
# Regression engine (client spec §B.2 — calibration from real data pairs)
# ---------------------------------------------------------------------------

def _fit_stats(f_obs, f_pred, n_params):
    """R², standard error and AIC for a fitted model (None when not
    computable — never invented)."""
    n = len(f_obs)
    ss_res = float(sum((fo - fp) ** 2 for fo, fp in zip(f_obs, f_pred)))
    mean_f = sum(f_obs) / n
    ss_tot = float(sum((fo - mean_f) ** 2 for fo in f_obs))
    r2 = (1.0 - ss_res / ss_tot) if ss_tot > 0 else None
    dof = n - n_params
    std_err = math.sqrt(ss_res / dof) if dof > 0 else None
    aic = (n * math.log(ss_res / n) + 2 * n_params) if (ss_res > 0 and n > 0) else None
    return r2, std_err, aic


def curve_fit_stats(curve_type, formula_params, data_points):
    """
    R² / standard error / AIC of a curve's OWN stored parameters measured
    against its REAL stored calibration pairs — the statistics persisted on
    regressed curves (read-only to API clients; the platform derives them so
    nothing client-supplied can pose as a fit quality).

    Returns (None, None, None) whenever they cannot be computed honestly:
    no usable pairs, a lookup table (piecewise interpolation, not a fitted
    model), a SonReb curve whose pairs lack rebound values, or parameters
    that fail to evaluate. A statistic the maths cannot support (e.g.
    standard error with as many parameters as pairs — zero residual degrees
    of freedom) comes back None from _fit_stats and is stored as null.
    """
    if curve_type == 'lookup' or curve_type not in CURVE_TYPES:
        return None, None, None

    def _num(value):
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    points = [p for p in (data_points or [])
              if isinstance(p, dict) and _num(p.get('v')) and _num(p.get('f'))]
    if curve_type == 'sonreb':
        # Every prediction needs a rebound value or the pair cannot score.
        points = [p for p in points if _num(p.get('r')) and p['r'] > 0]
    if len(points) < 2:
        return None, None, None
    try:
        f_obs, f_pred = [], []
        for p in points:
            # No valid-range gate here: the pairs ARE the calibration data.
            value = apply_curve_params(curve_type, formula_params,
                                       p['v'] / 1000.0,
                                       rebound_number=p.get('r'))
            if value is None or not math.isfinite(value):
                return None, None, None
            f_obs.append(float(p['f']))
            f_pred.append(float(value))
        if curve_type == 'polynomial':
            n_params = len(formula_params.get('coeffs', []))
        else:
            n_params = {'linear': 2, 'exponential': 3, 'sonreb': 3}[curve_type]
        return _fit_stats(f_obs, f_pred, n_params)
    except (KeyError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return None, None, None


def run_regression(data_points):
    """
    Fit every supported curve type to calibration pairs
    [{v (m/s), f (MPa), r (rebound, optional)}] and rank them.

    Linear / polynomial use least squares; SonReb is fitted in log space
    (ln f = ln a + b ln V + c ln R — exactly linear, so the fit is robust);
    the exponential model f = a*exp(b*V) + c uses non-linear least squares.

    Nothing is persisted here — the caller saves the chosen curve. Types
    whose fit fails (too few points, non-positive values, no convergence)
    come back as None with the reason in `fit_errors`.
    """
    import numpy as np
    from scipy.optimize import curve_fit

    errors = {}
    results = {}

    points = [p for p in (data_points or [])
              if isinstance(p, dict)
              and p.get('v') is not None and p.get('f') is not None]
    n = len(points)
    if n < 2:
        return {'n_points': n, 'results': {t: None for t in CURVE_TYPES if t != 'lookup'},
                'fit_errors': {'_all': 'At least 2 calibration points are required.'},
                'best_fit_type': None,
                'advisory': _advisory(n)}
    for p in points:
        try:
            p['v'] = float(p['v']); p['f'] = float(p['f'])
            if 'r' in p and p['r'] is not None:
                p['r'] = float(p['r'])
        except (TypeError, ValueError):
            return {'n_points': n,
                    'results': {t: None for t in CURVE_TYPES if t != 'lookup'},
                    'fit_errors': {'_all': 'Calibration values must be numeric.'},
                    'best_fit_type': None, 'advisory': _advisory(n)}

    v = np.array([p['v'] for p in points], dtype=float)
    f = np.array([p['f'] for p in points], dtype=float)
    v_min, v_max = float(v.min()), float(v.max())
    span = np.linspace(v_min, v_max, 50)

    def _publish(curve_type, params, f_pred, n_params):
        r2, std_err, aic = _fit_stats(f.tolist(), f_pred.tolist(), n_params)
        results[curve_type] = {
            'curve_type': curve_type,
            'formula_params': params,
            'formula': formula_display(curve_type, params),
            'r2_score': r2,
            'standard_error': std_err,
            'aic': aic,
            'valid_range_ms': [v_min, v_max],
        }

    # --- Linear: f = m*V + c ------------------------------------------------
    if n >= MIN_POINTS['linear']:
        try:
            m, c = np.polyfit(v, f, 1)
            params = {'m': float(m), 'c': float(c)}
            _publish('linear', params, m * v + c, 2)
        except Exception as exc:  # noqa: BLE001 — fit failure is reported, never hidden
            errors['linear'] = f'fit failed: {exc}'
    else:
        errors['linear'] = f"needs at least {MIN_POINTS['linear']} points"

    # --- Polynomial (degree 2): f = c0 + c1*V + c2*V^2 ---------------------
    if n >= MIN_POINTS['polynomial']:
        try:
            coeffs_desc = np.polyfit(v, f, 2)
            params = {'coeffs': [float(x) for x in coeffs_desc[::-1]]}
            f_pred = sum(c * (v ** i) for i, c in enumerate(params['coeffs']))
            _publish('polynomial', params, f_pred, 3)
        except Exception as exc:  # noqa: BLE001
            errors['polynomial'] = f'fit failed: {exc}'
    else:
        errors['polynomial'] = f"needs at least {MIN_POINTS['polynomial']} points"

    # --- Exponential: f = a*exp(b*V) + c ------------------------------------
    if n >= MIN_POINTS['exponential'] and np.all(f > 0) and np.all(v > 0):
        try:
            def _exp_model(x, a, b, c):
                return a * np.exp(b * x) + c
            p0 = [max(f.min() / 50.0, 1e-6), 1e-3, float(np.median(f)) / 2.0]
            popt, _ = curve_fit(_exp_model, v, f, p0=p0, maxfev=20000)
            params = {'a': float(popt[0]), 'b': float(popt[1]), 'c': float(popt[2])}
            if all(math.isfinite(x) for x in params.values()):
                _publish('exponential', params, _exp_model(v, *popt), 3)
            else:
                errors['exponential'] = 'fit did not converge to finite parameters'
        except Exception as exc:  # noqa: BLE001
            errors['exponential'] = f'fit failed: {exc}'
    elif n >= MIN_POINTS['exponential']:
        errors['exponential'] = 'velocities and strengths must all be positive'
    else:
        errors['exponential'] = f"needs at least {MIN_POINTS['exponential']} points"

    # --- SonReb: f = a * V^b * R^c (log-linear least squares) --------------
    has_rebound = all(p.get('r') is not None and p['r'] > 0 for p in points)
    if has_rebound and n >= MIN_POINTS['sonreb'] and np.all(f > 0) and np.all(v > 0):
        r = np.array([p['r'] for p in points], dtype=float)
        try:
            # ln f = ln a + b ln V + c ln R  ->  linear system.
            A = np.column_stack([np.ones(n), np.log(v), np.log(r)])
            coef, *_ = np.linalg.lstsq(A, np.log(f), rcond=None)
            params = {'a': float(math.exp(coef[0])), 'b': float(coef[1]),
                      'c': float(coef[2])}
            _publish('sonreb', params, params['a'] * (v ** params['b']) * (r ** params['c']), 3)
        except Exception as exc:  # noqa: BLE001
            errors['sonreb'] = f'fit failed: {exc}'
    elif n >= MIN_POINTS['sonreb'] and not has_rebound:
        errors['sonreb'] = 'every calibration point needs a rebound value r'
    elif n >= MIN_POINTS['sonreb']:
        errors['sonreb'] = 'velocities, strengths and rebounds must all be positive'
    else:
        errors['sonreb'] = f"needs at least {MIN_POINTS['sonreb']} points"

    best_fit_type = None
    fitted = {t: res for t, res in results.items() if res and res['r2_score'] is not None}
    if fitted:
        # Highest R² wins; AIC breaks ties (fewer parameters preferred).
        best_fit_type = min(
            fitted, key=lambda t: (-fitted[t]['r2_score'], fitted[t]['aic']
                                   if fitted[t]['aic'] is not None else float('inf')))

    return {
        'n_points': n,
        'results': {t: results.get(t) for t in ('linear', 'polynomial', 'exponential', 'sonreb')},
        'fit_errors': errors,
        'best_fit_type': best_fit_type,
        'advisory': _advisory(n),
    }


def _advisory(n_points):
    """The client's reliability guidance (8 Sep meeting): 9-15 points is the
    floor, 40+ preferred. Reported honestly — small samples still fit."""
    if n_points is None or n_points < RELIABILITY_ADVISORY_MIN:
        return (f"{n_points or 0} calibration point(s) fitted. For a reliable "
                f"project-specific curve use at least {RELIABILITY_ADVISORY_MIN}-15 "
                f"points (40+ preferred) from trial-mix cube tests, UPV and "
                f"rebound hammer readings.")
    return None


def curve_points_for_plot(curve_type, params, valid_range_ms, rebound_number=None,
                          count=50):
    """Points for the frontend curve plot, strictly inside the curve's
    calibrated range."""
    lo, hi = valid_range_ms
    if lo is None or hi is None or hi <= lo:
        return []
    out = []
    for i in range(count):
        v_ms = lo + (hi - lo) * i / (count - 1)
        f = apply_curve_params(curve_type, params, v_ms / 1000.0,
                               rebound_number=rebound_number)
        out.append({'v': round(v_ms, 2), 'f': None if f is None else round(f, 3)})
    return out
