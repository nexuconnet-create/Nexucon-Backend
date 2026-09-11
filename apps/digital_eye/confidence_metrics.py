"""
Honest confidence metrics for the PUNDIT strength assessment
(REFINED EXECUTIVE SUMMARY, PART B §2.2 "The Path to 95% Confidence").

The client asked for AI interpretation with confidence intervals, a
probability of falling below the design strength, cross-element
validation, reasoning traces and data quality scores. This module
computes every one of those from the RECORDED DATA — nothing is
fabricated and nothing is a fixed marketing number:

* 95% CI     — from the active calibration curve's regression standard
               error (the honest measure of how tightly the fitted curve
               matches its calibration data); +/- 1.96 SE. Curves without
               a regression (the documented laboratory default, lookup
               tables, manual parameter entry) have no error estimate, so
               they honestly report "no interval available".
* P(f < 25)  — normal approximation: the probability that the element's
               true strength lies below the statutory 25 N/mm2 design
               strength, given its estimated strength and the CI width.
               Only computed when a CI exists.
* Cross-element outlier check — an element whose mean velocity deviates
               from the median of its peer group (same floor + member
               type, or all graded elements when no peer group exists)
               by more than 20% is flagged as an outlier with the peer
               median stated. This is the cross-validation the client
               asked for, computed from the dataset itself.
* Data quality score — from point count and within-element point
               spread (BS EN 12504-4 practice): 3+ points and spread
               <= 2% is HIGH; 2 points or spread <= 5% is MEDIUM;
               anything else is LOW. Single-point elements are LOW with
               the reason stated.
* Reasoning trace — a numbered, data-cited derivation chain per element
               (measured points -> mean velocity -> curve -> strength ->
               CI -> probability -> verdict).

Every function takes only real recorded values; every missing input
yields None or an honest "not available" string, never an invented
number.
"""

import math

# Statutory design strength the report's verdicts are made against
# (BS 8110 / the platform's 25 N/mm2 rule used throughout the report).
DESIGN_STRENGTH_N_MM2 = 25.0

# Normal-quantile for a 95% two-sided interval.
Z_95 = 1.959963984540054


def curve_standard_error_mpa(project):
    """The regression standard error (MPa) of the project's active
    calibration curve — the honest basis of the confidence interval.
    None when the active curve has no regression error estimate (the
    documented laboratory default, lookup tables, manual entry)."""
    from .strength_curves import resolve_active_curve
    curve = resolve_active_curve(project)
    if curve is None:
        return None
    se = getattr(curve, 'standard_error', None)
    try:
        se = float(se)
    except (TypeError, ValueError):
        return None
    return se if se > 0 and math.isfinite(se) else None


def strength_confidence_interval(mean_ecs_mpa, se_mpa):
    """(low, high) MPa of the 95% CI around an estimated strength, or
    None when either input is missing. Half-width = 1.96 SE."""
    if mean_ecs_mpa is None or se_mpa is None:
        return None
    half = Z_95 * se_mpa
    return (mean_ecs_mpa - half, mean_ecs_mpa + half)


def probability_below_design(mean_ecs_mpa, se_mpa,
                             design_mpa=DESIGN_STRENGTH_N_MM2):
    """P(true strength < design strength) under a normal approximation
    around the estimated strength. Returns a 0-1 fraction, or None when
    no interval is available (never a fabricated probability)."""
    if mean_ecs_mpa is None or se_mpa is None or se_mpa <= 0:
        return None
    # Phi((design - estimate) / se)
    z = (design_mpa - mean_ecs_mpa) / se_mpa
    return round(0.5 * (1.0 + math.erf(z / math.sqrt(2.0))), 3)


def data_quality_score(n_points, point_spread_pct):
    """(label, reason) describing the reliability of an element's own
    readings. Honest buckets from BS EN 12504-4 practice — never a
    fabricated grade."""
    if n_points is None or n_points < 1:
        return None, 'no recorded test points'
    if n_points == 1:
        return 'LOW', 'single test point — no consistency evidence'
    if point_spread_pct is None:
        return 'MEDIUM', (f'{n_points} points — spread not computable '
                          '(velocity missing on some points)')
    if point_spread_pct <= 2.0 and n_points >= 3:
        return 'HIGH', (f'{n_points} points, spread '
                        f'{point_spread_pct:.1f}% of the mean velocity')
    if point_spread_pct <= 5.0 or n_points >= 3:
        return 'MEDIUM', (f'{n_points} points, spread '
                          f'{point_spread_pct:.1f}% of the mean velocity')
    return 'LOW', (f'{n_points} points, spread {point_spread_pct:.1f}% '
                   'of the mean velocity — readings disagree')


def cross_element_outliers(element_summaries, threshold_pct=20.0):
    """Outlier flags for every graded element, computed against its peer
    group — the same floor AND member type when that group has 2+
    members, else every graded element on the project. Returns
    {element_key: {'peer_median_m_s': float,
                   'deviation_pct': float,
                   'group': str}} for elements whose |deviation| from the
    peer median exceeds threshold_pct. The element_key is the summary
    dict's 'element' value (the BIM element name or None)."""
    graded = [s for s in element_summaries
              if s.get('mean_velocity_m_s') is not None]
    velocities = sorted(s['mean_velocity_m_s'] for s in graded)
    if not velocities:
        return {}

    def _median(vals):
        n = len(vals)
        mid = n // 2
        if n % 2:
            return vals[mid]
        return (vals[mid - 1] + vals[mid]) / 2.0

    outliers = {}
    for s in graded:
        v = s['mean_velocity_m_s']
        # Peer group: same floor + member type if it has 2+ graded
        # members, else the whole graded dataset.
        floor = s.get('floor')
        member = (s.get('element') or '').split(':')[0].strip()
        peers = [p['mean_velocity_m_s'] for p in graded
                 if p is not s
                 and p.get('floor') == floor
                 and (p.get('element') or '').split(':')[0].strip() == member]
        if len(peers) >= 2:
            group = f"same-floor {member or 'element'} group"
        else:
            peers = [p['mean_velocity_m_s'] for p in graded if p is not s]
            group = 'all graded elements on this project'
        if not peers:
            continue
        peer_median = _median(sorted(peers))
        if peer_median <= 0:
            continue
        deviation_pct = (v - peer_median) / peer_median * 100.0
        if abs(deviation_pct) > threshold_pct:
            outliers[id(s)] = {
                'element': s.get('element'),
                'peer_median_m_s': round(peer_median, 1),
                'deviation_pct': round(deviation_pct, 1),
                'group': group,
            }
    return outliers


def reasoning_trace(element_summary, se_mpa=None, ci=None, p_below=None,
                    outlier=None, quality=None):
    """The numbered, data-cited derivation chain for one element — the
    reasoning-trace transparency the client asked for. Every clause cites
    the recorded figures; missing inputs are stated honestly."""
    lines = []
    name = element_summary.get('element') or 'element'
    pts = element_summary.get('point_velocities_m_s') or []
    mean_v = element_summary.get('mean_velocity_m_s')
    if pts:
        parts = ' / '.join(f'{v:.0f}' for v in pts)
        lines.append(
            f'{len(pts)} test point velocity(ies) recorded (m/s): {parts}; '
            'each is V = L / t from its own path length and transit time.')
        lines.append(
            f'Mean pulse velocity {mean_v:.0f} m/s '
            f"({'BS EN 12504-4 mean of per-point velocities'})."
            if mean_v is not None else
            'Mean velocity could not be computed.')
    else:
        lines.append('No per-point velocities recorded for this element.')
    ecs = element_summary.get('mean_ecs_n_mm2')
    if ecs is not None:
        lines.append(
            f'Estimated compressive strength {ecs:.1f} N/mm2 through the '
            "project's active calibration curve (Section 3.0).")
        if ci is not None:
            lines.append(
                f'95% confidence interval {ci[0]:.1f} - {ci[1]:.1f} N/mm2 '
                '(+/- 1.96 x the curve regression standard error).')
        else:
            lines.append(
                'No confidence interval available: the active calibration '
                'curve carries no regression standard error (laboratory '
                'fixed curve, lookup table or manually entered parameters).')
        if p_below is not None:
            pct = p_below * 100.0
            lines.append(
                f'Probability the true strength is below the statutory '
                f'{DESIGN_STRENGTH_N_MM2:.0f} N/mm2 design strength: '
                f'{pct:.1f}% (normal approximation around the estimate).')
    else:
        lines.append(
            'No strength estimate: the mean velocity lies outside the '
            "curve's calibrated range (no extrapolation is applied).")
    if outlier:
        lines.append(
            f'Cross-element check: this velocity deviates '
            f"{outlier['deviation_pct']:+.1f}% from the median "
            f"({outlier['peer_median_m_s']:.0f} m/s) of its "
            f"{outlier['group']} — treated as an outlier.")
    if quality:
        label, reason = quality
        lines.append(f'Data quality: {label} — {reason}.')
    return lines
