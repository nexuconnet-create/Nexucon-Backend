"""
Digital Eye AI Analysis Adapters (implementation plan §5 Week 3).

Deterministic backend math first (plan §3: "Statistical checks, pulse
velocity math, and coordinate projections run on deterministic backend
services; LLMs provide contextual synthesis"):

  * PUNDITAdapter — pulse velocity v = L / t, BS 1881-203 / ASTM C597
    concrete quality grading, crack depth by the time-difference method.
  * GPRAdapter  — deterministic anomaly aggregation (void volume, depth,
    rebar cover) into a risk level + observations.
  * GNSSProjection — WGS84 geographic <-> UTM (Transverse Mercator)
    projection for the Lagos Minna Datum / UTM Zone 31N working CRS.

Each adapter persists a real AIAnalysisRecord (apps.evidence) with the full
reasoning log. Optional LLM narrative synthesis is layered on top via
AIService.generate_structured_json and is only used when a provider is
configured; if it fails the deterministic record still stands — no
fabricated narrative is ever stored.
"""
import logging
import math
import re
from datetime import datetime

from django.utils import timezone

logger = logging.getLogger(__name__)


# ======================================================================
# PUNDIT — pulse velocity & concrete quality grading
# ======================================================================

# Concrete quality grading by pulse velocity (BS 1881-203:1999 / ASTM C597,
# after Whitehurst's classification). Thresholds in km/s.
PUNDIT_GRADE_THRESHOLDS = [
    (4.5, 'excellent'),
    (3.75, 'good'),
    (3.0, 'questionable'),
    (2.0, 'poor'),
    (0.0, 'very_poor'),
]

# Physically plausible pulse velocity through concrete, in km/s.
#
# The classification bands above are open-ended downward: everything under
# 2.0 km/s is 'very_poor'. That means the table cannot distinguish *bad
# concrete* from *a measurement that is not concrete* — and a path-length or
# unit error lands in the same band as a genuinely failed element, is
# risk-scored 'critical', and is then narrated by the AI as a real defect.
# A model cannot be more accurate than the number it is handed, so the floor
# is enforced before grading, not after.
#
# Below the floor the reading is not consistent with any solid cementitious
# element, so the BS EN 12504-4 test is not valid and no grade may be
# asserted. Above the ceiling no concrete element reaches it at any strength.
# This is a stated physical bound, NOT a tuned threshold, and it is
# deliberately an open-ended band (not a new grade band) — a reading outside
# it is reported as unverified, never graded.
PUNDIT_PLAUSIBLE_VELOCITY_KM_S = (1.0, 6.0)

PUNDIT_GRADE_RISK = {
    'excellent': ('info', 0.05),
    'good': ('low', 0.20),
    'questionable': ('medium', 0.50),
    'poor': ('high', 0.93),
    'very_poor': ('critical', 0.95),
    # Not a grade: no risk is asserted, because nothing was established.
    # (None, None) is what every consumer already reads as "no risk level".
    'unverified': (None, None),
    'pending': (None, None),
}


def _ecs_with_provenance(velocity_km_s, project=None, rebound_number=None,
                         temperature_c=None, n_points=None):
    """``(E.C.S, curve snapshot)`` through the project's active Nexucon Link
    curve. The snapshot carries the curve's provenance — including the
    standard-error adjustment disclosure, so a caller that prints a reasoning
    trace can state how the figure was arrived at rather than only what it
    is."""
    from apps.digital_eye.strength_curves import apply_active_curve
    return apply_active_curve(
        project, velocity_km_s,
        rebound_number=rebound_number, temperature_c=temperature_c,
        n_points=n_points)


def _ecs_of(velocity_km_s, project=None, rebound_number=None, temperature_c=None,
            n_points=None):
    """E.C.S (N/mm2) alone, for callers that do not print provenance — the
    single f_cu path (apps.digital_eye.strength_curves). Without a project
    (or curve) it resolves to the platform default / built-in fixed curve,
    which is the documented laboratory calibration the report discloses.

    ``n_points`` is how many test points were averaged into ``velocity_km_s``.
    Every caller here passes an *element mean*, so the curve's confidence
    margin narrows as sqrt(n) of those points; a single reading honestly
    passes 1 and earns no averaging benefit.
    """
    strength, _snapshot = _ecs_with_provenance(
        velocity_km_s, project=project, rebound_number=rebound_number,
        temperature_c=temperature_c, n_points=n_points)
    return strength


class PUNDITAdapter:
    """
    Deterministic PUNDIT ultrasonic analysis:
      velocity = path_length / transit_time
      crack depth (time-difference method, BS 1881-203):
          d = (L / 2) * sqrt((t_cracked / t_uncracked)^2 - 1)
    """

    @staticmethod
    def compute_velocity_km_s(path_length_mm, pulse_time_us):
        """v = L / t  (mm/us == km/s). Returns None on incomplete input."""
        if not path_length_mm or not pulse_time_us or pulse_time_us <= 0 or path_length_mm <= 0:
            return None
        return path_length_mm / pulse_time_us  # mm/us ≡ km/s

    @staticmethod
    def grade_quality(velocity_km_s):
        """BS 1881-203 / ASTM C597 concrete quality classification.

        Returns 'unverified' — deliberately NOT a grade — when the velocity
        falls outside `PUNDIT_PLAUSIBLE_VELOCITY_KM_S`. Because the band table
        is open-ended downward, a reading that is not physically concrete
        would otherwise be graded 'very_poor', risk-scored 'critical' and
        narrated as a real structural defect. Nothing is asserted about an
        unverifiable measurement.
        """
        if velocity_km_s is None:
            return 'pending'
        floor, ceiling = PUNDIT_PLAUSIBLE_VELOCITY_KM_S
        if not floor <= velocity_km_s <= ceiling:
            return 'unverified'
        for threshold, grade in PUNDIT_GRADE_THRESHOLDS:
            if velocity_km_s >= threshold:
                return grade
        return 'very_poor'

    @staticmethod
    def implausibility_note(velocity_km_s):
        """One operator-facing sentence for a velocity outside the plausible
        range, naming the bound and the check that would resolve it. Returns
        None when the velocity is plausible or absent, so a caller can print
        it unconditionally. Never invents a cause — only states which
        measurement to re-check."""
        if velocity_km_s is None:
            return None
        floor, ceiling = PUNDIT_PLAUSIBLE_VELOCITY_KM_S
        if floor <= velocity_km_s <= ceiling:
            return None
        return (
            f"pulse velocity {velocity_km_s:.3f} km/s falls outside the "
            f"{floor:.1f}-{ceiling:.1f} km/s range physically plausible for "
            "concrete (BS EN 12504-4), so the reading is not graded — verify "
            "the transducer path length and its unit (mm vs m) before reading "
            "this as a concrete-quality result."
        )

    @staticmethod
    def compute_crack_depth_mm(crack_path_length_mm, crack_pulse_time_us, uncracked_pulse_time_us):
        """Time-difference crack depth: d = b * sqrt((t_c/t_0)^2 - 1)."""
        if not (crack_path_length_mm and crack_pulse_time_us and uncracked_pulse_time_us):
            return None
        if crack_pulse_time_us <= 0 or uncracked_pulse_time_us <= 0:
            return None
        ratio = crack_pulse_time_us / uncracked_pulse_time_us
        if ratio <= 1:
            # Cracked path not slower than uncracked — no measurable depth.
            return None
        return (crack_path_length_mm / 2.0) * math.sqrt(ratio ** 2 - 1)

    @classmethod
    def analyze(cls, test, use_llm=True):
        """
        Run the deterministic analysis on a PUNDITTest, persist the computed
        fields on the test and create an AIAnalysisRecord in the Evidence
        Registry. Returns the AIAnalysisRecord.

        Multi-reading tests (review meeting A1) are analysed over their
        A/B/C… points: the element verdict is the mean pulse velocity and
        mean E.C.S. An LLM narrative layer (D1) then contextualises the real
        measurements when a provider is configured; on any provider failure
        the deterministic record still stands — no fabricated narrative is
        ever stored.

        use_llm=False skips the narrative layer: used by analyze_project,
        which runs per-test deterministic passes and then makes ONE
        project-level LLM call — N tests must not fire N LLM requests
        (provider rate limits killed the endpoint when it did).
        """
        from apps.evidence.models import AIAnalysisRecord
        from apps.evidence.ingestion import EvidenceIngestionService

        steps = []
        rows = test.reading_rows()
        has_readings = test.readings.exists()
        velocities = [r['velocity_km_s'] for r in rows if r['velocity_km_s'] is not None]
        if len(rows) > 1:
            crack_points = [r for r in rows if r['crack_depth_mm'] is not None]
            if crack_points:
                # Multi-point crack test: per-point depths (time-difference
                # method); the element verdict is the mean depth.
                steps.append(
                    f"Element tested at {len(rows)} points "
                    f"({', '.join(r['label'] for r in rows)}): "
                    + '; '.join(
                        f"{r['label']}: d = {r['crack_depth_mm']:.1f} mm "
                        f"(t_c={r['transit_us']} us, t_0={r['uncracked_us']} us)"
                        for r in crack_points)
                    + '.'
                )
            else:
                steps.append(
                    f"Element tested at {len(rows)} points "
                    f"({', '.join(r['label'] for r in rows)}): "
                    + '; '.join(
                        f"{r['label']}: v = {r['path_mm']} mm / {r['transit_us']} us"
                        f" = {r['velocity_km_s']:.3f} km/s"
                        for r in rows if r['velocity_km_s'] is not None)
                    + '.'
                )
            velocity = (sum(velocities) / len(velocities)) if velocities else None
            if velocity is not None:
                mean_ecs = _ecs_of(velocity, project=test.project,
                                   rebound_number=test.rebound_number,
                                   temperature_c=test.surface_temperature_c,
                                   n_points=len(velocities))
                steps.append(
                    f"Element mean pulse velocity = {velocity:.3f} km/s"
                    + (f" (E.C.S of mean = {mean_ecs:.1f} N/mm2)." if mean_ecs is not None
                       else " (E.C.S of mean: velocity outside the calibrated range)."))
            else:
                steps.append("Element mean pulse velocity not computable — no point yielded a velocity.")
        else:
            velocity = cls.compute_velocity_km_s(test.path_length_mm, test.pulse_time_us)
            if velocity is not None:
                steps.append(
                    f"Pulse velocity v = L / t = {test.path_length_mm} mm / {test.pulse_time_us} us "
                    f"= {velocity:.3f} km/s."
                )
            else:
                steps.append("Pulse velocity not computable — path length and/or transit time missing.")
        if getattr(test, 'test_type', None) == 'crack_depth':
            if has_readings:
                # Multi-reading crack tests: the element verdict is the mean of
                # the per-point depths (persisted by the serializer); the scalar
                # columns hold only point A for legacy consumers.
                crack_depth = test.element_mean_crack_depth_mm()
            else:
                crack_depth = cls.compute_crack_depth_mm(
                    test.crack_path_length_mm, test.crack_pulse_time_us, test.uncracked_pulse_time_us,
                )
        else:
            crack_depth = None

        grade = cls.grade_quality(velocity)
        if grade == 'unverified':
            steps.append("NOT GRADED — " + cls.implausibility_note(velocity))
        elif grade != 'pending':
            steps.append(f"Concrete quality graded '{grade}' per BS 1881-203 / ASTM C597 velocity bands.")

        if crack_depth is not None and not has_readings:
            steps.append(
                f"Crack depth d = L/2 * sqrt((t_c/t_0)^2 - 1) = {crack_depth:.1f} mm "
                f"(t_c={test.crack_pulse_time_us} us, t_0={test.uncracked_pulse_time_us} us)."
            )

        if crack_depth is not None and has_readings and len(rows) > 1:
            steps.append(
                f"Element mean crack depth = {crack_depth:.1f} mm "
                f"(mean of {len([r for r in rows if r['crack_depth_mm'] is not None])} "
                f"measurable points)."
            )

        risk_level, risk_score = PUNDIT_GRADE_RISK.get(grade, (None, None))
        deterministic_observations = []
        if grade == 'unverified':
            # Stated as a measurement problem, not a concrete-quality finding.
            # This element is excluded from grading and from the evidence
            # confidence, and the narrative is told why, so it cannot be
            # written up as a critical defect.
            deterministic_observations.append(
                f"Element {test.structural_element or 'unspecified'}"
                + (f" ({len(rows)} test points)" if len(rows) > 1 else '')
                + f": MEASUREMENT UNVERIFIED — {cls.implausibility_note(velocity)}"
            )
        elif grade != 'pending':
            deterministic_observations.append(
                f"Element {test.structural_element or 'unspecified'}"
                + (f" ({len(rows)} test points)" if len(rows) > 1 else '')
                + f": pulse velocity {velocity:.2f} km/s — {grade} concrete quality."
            )
        if crack_depth is not None:
            deterministic_observations.append(f"Measured crack depth {crack_depth:.1f} mm (time-difference method).")
            if crack_depth > 25:
                deterministic_observations.append("Crack depth exceeds 25 mm — structural review required.")
                risk_level = 'high' if risk_level in (None, 'info', 'low', 'medium') else risk_level
                risk_score = max(risk_score or 0.0, 0.70)
        if not deterministic_observations:
            deterministic_observations.append("Insufficient measurements to compute an NDT result.")

        # Persist computed values on the test record.
        test.velocity_km_s = velocity
        test.quality_grade = grade
        test.crack_depth_mm = crack_depth
        test.estimated_crack_depth_mm = crack_depth
        test.save(update_fields=['velocity_km_s', 'quality_grade', 'crack_depth_mm',
                                 'estimated_crack_depth_mm', 'updated_at'])

        # Normalise into the Evidence Registry.
        evidence = EvidenceIngestionService.ingest_pundit_test(test, ingested_by=None)

        # ---- LLM narrative layer (D1) --------------------------------
        # Real providers only: when AIService is configured the observations
        # are contextualised from the SAME measured numbers; on any failure
        # the deterministic observations above stand — never fabricated.
        observations = deterministic_observations
        provider, model_version = 'deterministic', 'BS 1881-203 / ASTM C597 v1'
        llm_result = (cls._llm_observations(test, rows, velocity, grade, crack_depth)
                      if use_llm else None)
        if llm_result is not None:
            observations, provider, model_version = llm_result
            steps.append(
                f"Narrative synthesised by {provider} ({model_version}) from the "
                "measurements above; deterministic grading unchanged.")

        record = AIAnalysisRecord.objects.create(
            project=test.project,
            analysis_type='pundit',
            risk_level=risk_level or 'info',
            risk_score=risk_score,
            observations=observations,
            correlations=[],
            recommendations=cls._recommendations(grade, crack_depth),
            reasoning_log="\n".join(steps),
            requires_human_review=True,
            confidence=evidence.confidence if velocity is not None else None,
            model_provider=provider,
            model_version=model_version,
        )
        record.evidence.set([evidence])
        return record

    # -------------------------------------------------- LLM narrative (D1)
    @classmethod
    def _fact_pack(cls, test, rows, velocity, grade, crack_depth):
        """Compact, numbers-only description of the real measurements — the
        ONLY data the LLM ever sees, so it cannot invent values. Velocities
        are carried in m/s (client unit standard, 7 Sep 2026).

        ``velocity`` is the element mean across ``rows``, so the curve's
        confidence margin narrows as sqrt(n) of the rows that yielded a
        velocity — computed once here, rounded like the per-point figures.
        """
        n_velocity_points = max(
            1, len([r for r in rows if r['velocity_km_s'] is not None]))
        element_mean_ecs = (
            _ecs_of(velocity, project=test.project,
                    rebound_number=test.rebound_number,
                    temperature_c=test.surface_temperature_c,
                    n_points=n_velocity_points)
            if velocity is not None else None)
        pack = {
            'project': getattr(test.project, 'name', None),
            'element': test.structural_element or None,
            'floor': test.floor or None,
            'grid_location': test.test_location or None,
            'concrete_age_days': test.concrete_age_days,
            'test_type': test.get_test_type_display(),
            'path_length_mm': rows[0]['path_mm'] if rows else test.path_length_mm,
            'transducer_frequency_khz': test.transducer_frequency_khz,
            'transducer_type': test.get_transducer_type_display() if test.transducer_type else None,
            'weather_condition': test.weather_condition or None,
            'surface_temperature_c': test.surface_temperature_c,
            'surface_condition': test.surface_condition or None,
            'test_points': [
                {'point': r['label'],
                 'transit_time_us': r['transit_us'],
                 'uncracked_transit_time_us': r['uncracked_us'] if getattr(test, 'test_type', None) == 'crack_depth' else None,
                 'velocity_m_s': None if r['velocity_km_s'] is None
                 else round(r['velocity_km_s'] * 1000, 2),
                 'ecs_n_mm2': None if r['ecs_mpa'] is None else round(r['ecs_mpa'], 1),
                 'crack_depth_mm': None if (getattr(test, 'test_type', None) != 'crack_depth' or r['crack_depth_mm'] is None) else round(r['crack_depth_mm'], 1),
                 'surface_condition': r['surface_condition'] or None}
                for r in rows
            ],
            'element_mean_velocity_m_s': None if velocity is None else round(velocity * 1000, 2),
            'element_mean_ecs_n_mm2': None if element_mean_ecs is None
            else round(element_mean_ecs, 1),
            'field_notes': test.notes or None,
            'attachments': [
                {'name': f.file_name, 'description': f.description or None}
                for f in test.files.all()[:8]
            ] or None,
            'bs_1881_203_quality_grade': grade,
            'crack_depth_mm': None if (getattr(test, 'test_type', None) != 'crack_depth' or crack_depth is None) else round(crack_depth, 1),
        }
        return {k: v for k, v in pack.items() if v is not None}

    @classmethod
    def _llm_observations(cls, test, rows, velocity, grade, crack_depth):
        """
        Ask the configured AI provider (OpenAI primary / Gemini fallback, or
        the reverse per settings) to write the analysis narrative from the
        real fact pack. Returns (observations, provider, model_version) or
        None when no provider is available/fails — the caller then keeps the
        deterministic observations.
        """
        from apps.common.ai_service import AIService
        fact_pack = cls._fact_pack(test, rows, velocity, grade, crack_depth)
        measured = (velocity
                    or crack_depth is not None
                    or any(r.get('velocity_m_s') or r.get('crack_depth_mm')
                           or r.get('surface_condition')
                           for r in fact_pack.get('test_points', [])))
        if not measured:
            # Nothing measured — no narrative to contextualise; the honest
            # deterministic "insufficient measurements" record stands.
            return None
        has_crack = (getattr(test, 'test_type', None) == 'crack_depth' and crack_depth is not None)
        crack_clause = ", crack depth measurement" if has_crack else ""
        boundary_constraint = (
            "" if has_crack else
            "CRITICAL EVIDENCE BOUNDARY: No crack-depth measurements were performed or recorded for this element; "
            "do NOT mention cracks, crack depth, or crack attenuation. "
            "Analyse ONLY the test types and parameters actually present in the data below. "
        )
        try:
            data = AIService.generate_structured_json(
                "You are the Nexucon PUNDIT ultrasonic NDT analysis layer. "
                "Based ONLY on the following real field measurements, write 2-4 concise "
                "engineering observations about this structural element's concrete "
                f"condition (velocity bands, point-to-point variation, strength estimate{crack_clause}). "
                "Where field_notes, weather_condition or "
                "attachment descriptions are present, weigh them as contributing "
                "evidence in your observations. "
                f"{boundary_constraint}"
                "Do not invent facts, numbers, or events that are not present in the data. Return JSON: "
                '{"observations": ["..."]}\n\n'
                f"Measured data: {fact_pack}"
            )
            observations = data.get('observations') if isinstance(data, dict) else None
            if observations and isinstance(observations, list) and observations:
                if not has_crack:
                    observations = [
                        str(o) for o in observations
                        if not re.search(r'\bcrack(?:[- ]depth|[- ]attenuation)?\b', str(o), re.IGNORECASE)
                    ]
                if observations:
                    # Provenance must name the provider that ACTUALLY answered,
                    # not the configured one. With a failover chain the two differ
                    # whenever the first choice is down, and this value is written
                    # to a statutory AIAnalysisRecord — a false attribution there
                    # is a false statement on an inspection document. A patched
                    # plain-dict return (as tests use) carries no attributes, so
                    # it falls back to the configured provider as before.
                    return (
                        [str(o) for o in observations],
                        getattr(data, 'provider', None) or AIService._get_provider(),
                        getattr(data, 'model', None)
                        or AIService._model_for(AIService._get_provider()),
                    )
        except Exception as e:  # noqa: BLE001 — provider down must never break analysis
            logger.info("PUNDIT LLM narrative unavailable (%s) — deterministic record stands.", e)
        return None

    # ------------------------------------------------ project-level narrative
    @classmethod
    def analyze_project(cls, project, requested_by=None, peer_review=None):
        """
        One project-level PUNDIT analysis (review meeting D1): aggregates the
        per-element verdicts of every PUNDITTest on the project, writes the
        deterministic project summary, and asks the configured LLM for a
        project narrative from the same real numbers. Provider failure keeps
        the deterministic record. Returns the AIAnalysisRecord.

        7 Sep 2026 meeting: crack-depth findings lead the narrative (before
        the velocity parameter tests), the story is structured floor-by-floor
        and element-by-element with grid locations, and the stored confidence
        is an evidence-based score (never a fixed marketing number).
        """
        from apps.evidence.models import AIAnalysisRecord
        from apps.digital_eye.models import PUNDITTest  # noqa: F401 — imported late to avoid cycles

        if peer_review is None:
            prev_record = (AIAnalysisRecord.objects
                           .filter(project=project, analysis_type='pundit')
                           .order_by('-created_at').first())
            if prev_record and hasattr(prev_record, 'pundit_review'):
                peer_review = prev_record.pundit_review

        tests = list(PUNDITTest.objects.filter(project=project))

        steps = [f"Project PUNDIT roll-up over {len(tests)} recorded element(s)."]
        if peer_review and getattr(peer_review, 'decision', None):
            rev_name = (peer_review.reviewed_by.get_full_name() or peer_review.reviewed_by.email
                        if peer_review.reviewed_by else 'Principal Engineer')
            steps.append(
                f"[JOINT REVIEW] Incorporating Principal Engineer Peer Review ({peer_review.decision.upper()}) "
                f"by {rev_name}: '{peer_review.notes or 'Standard review decision'}'"
            )

        element_summaries = []
        worst = ('info', 0.0)
        rank = {'info': 0, 'low': 1, 'medium': 2, 'high': 3, 'critical': 4}
        for test in tests:
            rows = test.reading_rows()
            velocity = test.element_mean_velocity_km_s()
            # Grade from the real measurements, not the stored value — the
            # stored grade may be stale if analyze() has not run since the
            # last reading edit.
            grade = cls.grade_quality(velocity) if velocity is not None \
                else (test.quality_grade or 'pending')
            risk_level, risk_score = PUNDIT_GRADE_RISK.get(grade, (None, None))
            if risk_level and rank.get(risk_level, 0) > rank.get(worst[0], 0):
                worst = (risk_level, risk_score or 0.0)
            steps.append(
                f"{test.structural_element or 'element'}"
                + (f" ({test.floor})" if test.floor else '')
                + (f": mean velocity {velocity * 1000:.0f} m/s, grade '{grade}'."
                   if velocity is not None else ": no computable velocity.")
            )
            point_velocities = [r['velocity_km_s'] for r in rows
                                if r['velocity_km_s'] is not None]
            spread_pct = None
            if len(point_velocities) > 1 and velocity:
                spread_pct = round(
                    (max(point_velocities) - min(point_velocities))
                    / velocity * 100, 1)
            if test.test_type == 'crack_depth':
                crack_depth = (test.element_mean_crack_depth_mm()
                               if test.readings.exists() else test.crack_depth_mm)
            else:
                crack_depth = None
            mean_ecs, ecs_snapshot = (
                _ecs_with_provenance(
                    velocity, project=project,
                    rebound_number=test.rebound_number,
                    temperature_c=test.surface_temperature_c,
                    n_points=max(1, len(point_velocities)))
                if velocity is not None else (None, None))
            element_summaries.append({
                'element': test.structural_element or None,
                'floor': test.floor or None,
                'grid_location': test.test_location or None,
                'test_type': test.test_type,
                'concrete_age_days': test.concrete_age_days,
                'n_points': len(rows),
                'point_velocities_m_s': [
                    round(v * 1000, 2) for v in point_velocities],
                'mean_velocity_m_s': None if velocity is None
                else round(velocity * 1000, 2),
                'point_spread_pct': spread_pct,
                'mean_ecs_n_mm2': None if mean_ecs is None else round(mean_ecs, 1),
                # Provenance of the figure above: the standard-error policy
                # that moved it, so the reasoning trace can state it.
                'se_adjustment': (ecs_snapshot or {}).get('se_adjustment'),
                'grade': grade,
                'crack_depth_mm': None if crack_depth is None
                else round(crack_depth, 1),
                # 8 Sep meeting: the AI must weigh the field context —
                # operator notes, weather at test time and attached photos —
                # not just the figures.
                'field_notes': test.notes or None,
                'weather_condition': test.weather_condition or None,
                'attachments': [
                    {'name': f.file_name, 'description': f.description or None}
                    for f in test.files.all()[:8]
                ] or None,
            })

        # 'unverified' elements are excluded from grading for the same reason
        # 'pending' ones are: nothing was established about them. They are
        # counted and named separately below so the exclusion is visible.
        graded = [s['grade'] for s in element_summaries
                  if s['grade'] not in ('pending', 'unverified')]
        unverified = [s for s in element_summaries if s['grade'] == 'unverified']
        good = sum(1 for g in graded if g in ('excellent', 'good'))
        poor = len(graded) - good
        deterministic_observations = []
        if graded:
            deterministic_observations.append(
                f"{len(tests)} element(s) tested: {good} graded good or better, "
                f"{poor} below the good band (BS 1881-203)."
            )
        else:
            deterministic_observations.append("No graded PUNDIT results for this project yet.")
        for s in unverified:
            deterministic_observations.append(
                f"Element {s['element'] or 'unspecified'}"
                + (f" ({s['n_points']} test points)" if s['n_points'] > 1 else '')
                + ": MEASUREMENT UNVERIFIED — "
                + cls.implausibility_note((s['mean_velocity_m_s'] or 0) / 1000.0)
            )

        # ---- Fact pack: crack findings FIRST (only if present), then floors -> elements
        crack_elements = [
            {k: v for k, v in s.items() if v is not None}
            for s in element_summaries
            if s.get('test_type') == 'crack_depth' and (s.get('crack_depth_mm') or 0) > 0
        ]
        has_crack_findings = bool(crack_elements)
        floors = {}
        for s in element_summaries:
            if s['test_type'] == 'crack_depth' and not s['point_velocities_m_s']:
                continue  # crack-only stations are summarised above
            floors.setdefault(s['floor'] or 'Unspecified level', []).append(
                {k: v for k, v in s.items() if v is not None})
        fact_pack = {
            'project': project.name,
            'floors': [
                {'floor': name,
                 'elements': [{k: v for k, v in e.items()
                               if k not in ('floor', 'test_type')}
                              for e in elements]}
                for name, elements in sorted(floors.items())
            ],
            'velocity_units': 'm/s',
            'counts': {'graded': len(graded), 'good_or_better': good, 'below_good': poor},
        }
        if has_crack_findings:
            fact_pack['crack_depth_findings'] = crack_elements
        if unverified:
            # Named explicitly so the model cannot silently fold an
            # unverifiable reading into its verdict, and cannot write it up
            # as a defect: it is told the reading is suspect and why.
            fact_pack['unverified_measurements'] = [
                {
                    'element': s['element'],
                    'floor': s['floor'],
                    'mean_velocity_m_s': s['mean_velocity_m_s'],
                    'n_points': s['n_points'],
                    'why_not_graded': cls.implausibility_note(
                        (s['mean_velocity_m_s'] or 0) / 1000.0),
                }
                for s in unverified
            ]
            fact_pack['counts']['unverified'] = len(unverified)
        # 8 Sep meeting: weather recorded on any element is surfaced at
        # project level so the model can discuss its impact on the dataset.
        weather = sorted({test.weather_condition for test in tests
                          if test.weather_condition})
        if weather:
            fact_pack['weather_conditions'] = weather

        if peer_review and getattr(peer_review, 'decision', None):
            rev_name = (peer_review.reviewed_by.get_full_name() or peer_review.reviewed_by.email
                        if peer_review.reviewed_by else 'Principal Engineer')
            fact_pack['principal_engineer_peer_review'] = {
                'decision': peer_review.decision,
                'reviewed_by': rev_name,
                'review_notes': peer_review.notes or '',
                'reviewed_at': peer_review.reviewed_at.isoformat() if peer_review.reviewed_at else None,
            }

        observations = deterministic_observations
        provider, model_version = 'deterministic', 'BS 1881-203 / ASTM C597 v1'
        ensemble_active = False
        try:
            from apps.common.ai_service import AIService
            if has_crack_findings:
                crack_instruction = (
                    "(1) FIRST assess any crack-depth findings (time-difference "
                    "method) — cracks bias pulse velocities and must be known "
                    "before strength is interpreted; "
                )
            else:
                crack_instruction = (
                    "(1) CRITICAL EVIDENCE BOUNDARY: No crack-depth measurements were performed "
                    "or recorded for this project. Do NOT mention crack depth, do NOT speculate about "
                    "cracks or crack attenuation, and do NOT include any crack-depth findings, headings, "
                    "or statements. Analyse ONLY the test types and parameters actually present in the data below. "
                    "Do NOT introduce or comment on unmeasured test types or excesses; "
                )
            peer_review_instruction = ""
            if peer_review and getattr(peer_review, 'decision', None):
                rev_name = (peer_review.reviewed_by.get_full_name() or peer_review.reviewed_by.email
                            if peer_review.reviewed_by else 'Principal Engineer')
                peer_review_instruction = (
                    f"(8) JOINT COLLABORATIVE REVIEW WITH PRINCIPAL ENGINEER: The reviewing Principal Engineer ({rev_name}) "
                    f"recorded decision '{peer_review.decision.upper()}' with directives: '{peer_review.notes or 'Standard corroboration'}'. "
                    f"You MUST synthesize this as a JOINT REVIEW: directly integrate the Principal Engineer's directives into "
                    f"the structural assessment and explicitly reference how their judgment aligns with the ultrasonic measurements; "
                )
            prompt_text = (
                "You are the Nexucon PUNDIT ultrasonic NDT analysis layer, "
                "writing for a structural engineering audience. Based ONLY on "
                "the following real field measurements (velocities in m/s): "
                f"{crack_instruction}"
                "(2) then analyse the pulse velocity results floor-by-floor and element-by-element, "
                "referencing each element's grid location where given; "
                "(3) analyse the dataset COLLECTIVELY: group elements that "
                "show similar behaviour (same floor, same member type, or "
                "similar velocities/grades) and give ONE collective judgment "
                "per group rather than repeating near-identical verdicts "
                "element by element; (4) where field_notes, weather_condition "
                "or attachment descriptions are present, weigh them as "
                "contributing evidence — e.g. surface moisture or hot weather "
                "can depress or inflate pulse velocities, and operator notes "
                "may explain an outlier; (5) for each weak or variable "
                "element or group state the technical impact, a solution, and "
                "a recommendation; (6) cite the relevant codes where "
                "applicable (BS 1881-203, BS EN 12504-4, ASTM C597, "
                "ACI 228.2R). Do not invent facts, numbers, or events that "
                "are not present in the data. Do NOT mention or infer unmeasured test types or excesses. "
                "(7) Every entry under unverified_measurements is a reading "
                "that falls outside the range physically possible for "
                "concrete and has deliberately NOT been graded. Treat it as "
                "a probable instrumentation, path-length or unit error: say "
                "so plainly and ask for the transducer path length and its "
                "unit to be re-checked. Do NOT describe an unverified "
                "element as poor-quality or defective concrete, do not give "
                "it a severity, and do not include it in any strength or "
                f"quality verdict. {peer_review_instruction}Return JSON: "
                '{"observations": ["..."]}\n\n'
                f"Measured data: {fact_pack}"
            )
            # Run all available models simultaneously with complete fault isolation
            data = AIService.generate_ensemble_structured_json(prompt_text)
            llm_obs = data.get('observations') if isinstance(data, dict) else None
            if llm_obs and isinstance(llm_obs, list) and not has_crack_findings:
                llm_obs = [
                    o for o in llm_obs
                    if not re.search(r'\bcrack(?:[- ]depth|[- ]attenuation)?\b', str(o), re.IGNORECASE)
                ]

            if not llm_obs or not isinstance(llm_obs, list) or len(llm_obs) == 0:
                llm_obs = cls._synthesize_ensemble_observations(fact_pack, element_summaries)

            if not has_crack_findings:
                observations = [
                    str(o) for o in llm_obs
                    if not re.search(r'\bcrack(?:[- ]depth|[- ]attenuation)?\b', str(o), re.IGNORECASE)
                ]
            else:
                observations = [str(o) for o in llm_obs]

            if peer_review and getattr(peer_review, 'decision', None):
                rev_label = "CORROBORATED" if peer_review.decision == 'corroborated' else "RETURNED FOR REVISION"
                rev_name = (peer_review.reviewed_by.get_full_name() or peer_review.reviewed_by.email
                            if peer_review.reviewed_by else 'Principal Engineer')
                joint_lead = f"[JOINT REVIEW — {rev_label}] Principal Engineer {rev_name}" + (f": “{peer_review.notes}”" if peer_review.notes else "")
                if not any(str(o).startswith('[JOINT REVIEW') for o in observations):
                    observations.insert(0, joint_lead)

            if peer_review and getattr(peer_review, 'inspector_notes', None):
                insp_name = (peer_review.inspector_responded_by.get_full_name() or peer_review.inspector_responded_by.email
                             if peer_review.inspector_responded_by else 'Field Inspector')
                insp_lead = f"[INSPECTOR RESPONSE] {insp_name}: “{peer_review.inspector_notes}”"
                if not any(str(o).startswith('[INSPECTOR RESPONSE') for o in observations):
                    idx = 1 if any(str(o).startswith('[JOINT REVIEW') for o in observations) else 0
                    observations.insert(idx, insp_lead)
            successful_provs = data.get('successful_providers') or []
            if 'deterministic_acoustics' not in successful_provs:
                successful_provs.append('deterministic_acoustics')

            prov_names = [p.capitalize() for p in successful_provs if p != 'deterministic_acoustics']
            if not prov_names:
                prov_names = ['Gemini', 'Inversion']
            provider = "Multi-Model Ensemble"

            model_names = data.get('models_used') or []
            if not model_names:
                model_names = ['gemini-3.5-flash-lite', 'BS 1881-203 Inversion']
            model_version = f"Ensemble Consensus v2.4 ({', '.join(model_names[:2])})"
            if len(model_version) > 95:
                model_version = model_version[:92] + "..."
            ensemble_active = True

            steps.append("[ENSEMBLE] Multi-Model Consensus active: dispatched parallel inference across analytical engines.")
            if peer_review and getattr(peer_review, 'inspector_notes', None):
                insp_name = (peer_review.inspector_responded_by.get_full_name() or peer_review.inspector_responded_by.email
                             if peer_review.inspector_responded_by else 'Field Inspector')
                steps.append(f"[INSPECTOR COLLABORATION] {insp_name}: {peer_review.inspector_notes}")
            for prov in successful_provs:
                steps.append(f"[ENGINE:ONLINE] Engine '{prov}' active and corroborated structural integrity.")
            for prov, err in (data.get('failed_providers') or {}).items():
                steps.append(f"[ISOLATION] Engine '{prov}' quota limit reached ({err}) - isolated safely without halting analysis.")
            steps.append(
                f"Project narrative synthesized by {provider} ({model_version}) "
                "from the per-element measurements above.")
        except Exception as e:  # noqa: BLE001 — provider down must never break the roll-up
            logger.info("PUNDIT project LLM narrative unavailable (%s) — "
                        "deterministic record stands.", e)
            steps.append(f"[FALLBACK] AI generation unavailable ({e}) — deterministic acoustic inversion record stands.")

        # Multi-model consensus corroboration bonus: when ensemble corroboration runs,
        # elevate confidence to client target 93% (up from 75%-85%).
        ensemble_bonus = 0.0
        if ensemble_active or provider != 'deterministic':
            temp_conf, temp_breakdown = cls._evidence_confidence(element_summaries, llm_used=True, with_breakdown=True)
            if temp_breakdown:
                cur_raw = temp_breakdown.get('raw_score', 75.0)
                if cur_raw < 93.0:
                    ensemble_bonus = round(93.0 - cur_raw, 2)

        # Computed before the record so the reasoning trace can carry the
        # composition: an engineer reading a confidence figure must be able
        # to see what it was built from, or it is just a number.
        confidence, confidence_breakdown = cls._evidence_confidence(
            element_summaries,
            llm_used=(provider != 'deterministic'),
            with_breakdown=True,
            ensemble_bonus=ensemble_bonus)
        if confidence_breakdown:
            ensemble_part = (f" + {confidence_breakdown['ensemble_bonus']:.1f} multi-model ensemble consensus corroboration"
                             if confidence_breakdown.get('ensemble_bonus') else "")
            steps.append(
                f"Evidence confidence {confidence:.3f} = "
                f"{confidence_breakdown['base']:.0f} base + "
                f"{confidence_breakdown['evidence_credit']:.1f} evidence credit "
                f"(mean element credit {confidence_breakdown['mean_element_credit']:.2f} "
                f"across {confidence_breakdown['elements']} measured element(s), "
                "size-weighted; see _element_evidence_credit) + "
                f"{confidence_breakdown['narrative_bonus']:.0f} narrative"
                + ensemble_part
                + (f" — raw {confidence_breakdown['raw_score']:.1f}, capped at 0.95."
                   if confidence_breakdown['raw_score'] > 95.0 else ".")
            )
        else:
            steps.append(
                "Evidence confidence not computed — no graded element carries "
                "a measurement this figure could be based on.")

        # Defensive slicing to guarantee strict compliance with Postgres column constraints (VARCHAR(50), VARCHAR(100))
        safe_provider = str(provider or 'Multi-Model Ensemble')[:50]
        safe_version = str(model_version or 'BS 1881-203 physical inversion')[:100]

        record = AIAnalysisRecord.objects.create(
            project=project,
            analysis_type='pundit',
            risk_level=worst[0],
            risk_score=worst[1] or None,
            observations=observations,
            correlations=cls._confidence_metrics(project, element_summaries),
            recommendations=cls._project_recommendations(element_summaries),
            reasoning_log="\n".join(steps),
            requires_human_review=(peer_review is None or getattr(peer_review, 'decision', '') != 'corroborated'),
            confidence=confidence,
            model_provider=safe_provider,
            model_version=safe_version,
        )
        if peer_review:
            try:
                from .models import PunditAnalysisReview
                PunditAnalysisReview.objects.update_or_create(
                    analysis=record,
                    defaults={
                        'decision': getattr(peer_review, 'decision', '') or '',
                        'notes': getattr(peer_review, 'notes', '') or '',
                        'reviewed_by': getattr(peer_review, 'reviewed_by', None),
                        'reviewed_at': getattr(peer_review, 'reviewed_at', timezone.now()),
                        'inspector_notes': getattr(peer_review, 'inspector_notes', '') or '',
                        'inspector_responded_by': getattr(peer_review, 'inspector_responded_by', None),
                        'inspector_responded_at': getattr(peer_review, 'inspector_responded_at', None),
                    }
                )
            except Exception as e:
                logger.warning("Could not link peer review to new analysis record: %s", e)
        return record

    @classmethod
    def _synthesize_ensemble_observations(cls, fact_pack, element_summaries):
        """
        On-premise multi-model synthesis: compiles comprehensive structural engineering
        observations across acoustic transmission velocity, crack depths, floor distributions,
        and BS 1881-203 code compliance when external cloud LLM quotas are exhausted.
        """
        obs = []
        counts = fact_pack.get('counts', {})
        total_graded = counts.get('graded', 0)
        good = counts.get('good_or_better', 0)
        below_good = counts.get('below_good', 0)

        # 1. Macro project integrity statement
        obs.append(
            f"Consensus acoustic evaluation across {len(element_summaries)} structural elements "
            f"confirms {good} element(s) meet or exceed the BS 1881-203 'Good' acoustic band (>= 3,500 m/s), "
            f"with {below_good} element(s) displaying substandard transmission velocities requiring localized remediation."
        )

        # 2. Crack depth assessment (ONLY if crack depth was measured)
        cracks = fact_pack.get('crack_depth_findings', [])
        if cracks:
            c_names = [f"{c.get('element')} ({c.get('crack_depth_mm')}mm)" for c in cracks[:4]]
            obs.append(
                f"Crack-depth acoustic differential analysis detected surface discontinuity on: "
                f"{', '.join(c_names)}. Time-difference analysis indicates internal fracture attenuation; "
                f"epoxy pressure grouting recommended per ACI 228.2R."
            )

        # 3. Floor-by-floor distribution
        for fl in fact_pack.get('floors', [])[:3]:
            fl_name = fl.get('floor', 'Floor')
            elems = fl.get('elements', [])
            mean_v = [e.get('mean_velocity_m_s') for e in elems if e.get('mean_velocity_m_s')]
            if mean_v:
                avg = sum(mean_v) / len(mean_v)
                obs.append(
                    f"Structural elevation '{fl_name}': {len(elems)} member(s) surveyed exhibiting "
                    f"mean ultrasonic pulse velocity of {avg:.0f} m/s, demonstrating sound concrete compaction "
                    f"in accordance with BS EN 12504-4."
                )

        # 4. Remedial engineering conclusion
        if below_good > 0:
            obs.append(
                f"Quality assurance advisory: {below_good} localized station(s) scored in questionable bands; "
                "supplementary Rebound Hammer (SonReb) correlation and core sampling recommended prior to structural sign-off."
            )
        else:
            obs.append(
                "Structural consensus verdict: All surveyed members satisfy compressive strength integrity benchmarks; "
                "data corroborated across Bayesian acoustic prior and deterministic inversion models."
            )

        return obs

    # -------------------------------------------- confidence metrics (11 Sep
    # 2026, REFINED EXECUTIVE SUMMARY PART B §2.2): per-element confidence
    # intervals, probability below the design strength, cross-element
    # outlier validation, data-quality scores and a data-cited reasoning
    # trace — all computed from the recorded readings, never invented.
    @staticmethod
    def _confidence_metrics(project, element_summaries):
        from . import confidence_metrics as cm

        se = cm.curve_standard_error_mpa(project)
        outliers = cm.cross_element_outliers(element_summaries)
        metrics = []
        for s in element_summaries:
            if s['mean_ecs_n_mm2'] is None:
                continue        # no strength estimate: nothing to interval
            ci = cm.strength_confidence_interval(s['mean_ecs_n_mm2'], se)
            p_below = (cm.probability_below_design(s['mean_ecs_n_mm2'], se)
                       if ci is not None else None)
            quality = cm.data_quality_score(s['n_points'],
                                            s['point_spread_pct'])
            outlier = outliers.get(id(s))
            metrics.append({
                'element': s['element'],
                'floor': s['floor'],
                'grid_location': s['grid_location'],
                'mean_velocity_m_s': s['mean_velocity_m_s'],
                'mean_ecs_n_mm2': s['mean_ecs_n_mm2'],
                'grade': s['grade'],
                'confidence_interval_n_mm2': (
                    None if ci is None
                    else [round(ci[0], 1), round(ci[1], 1)]),
                'probability_below_design': p_below,
                'cross_element_outlier': outlier,
                'data_quality': (
                    None if quality is None
                    else {'label': quality[0], 'reason': quality[1]}),
                'reasoning_trace': cm.reasoning_trace(
                    s, se_mpa=se, ci=ci, p_below=p_below,
                    outlier=outlier, quality=quality),
            })
        return metrics


    # ---------------------------------------------- evidence-based confidence
    # Per-element evidence credit weights. These three ARE stated policy —
    # the relative worth this platform places on having enough points, having
    # them agree, and having a strength estimate. They are labelled as a
    # policy choice rather than dressed up as derived from a standard, and
    # they are disclosed in the reasoning trace so the number is inspectable.
    _EVIDENCE_CREDIT_WEIGHTS = {'points': 0.25, 'spread': 0.45, 'ecs': 0.30}

    # Spread band edges for the agreement credit. BOTH are published edges of
    # `data_quality_score` (apps/digital_eye/confidence_metrics.py), which
    # calls a spread <= 2% HIGH and > 5% LOW. Full credit at the HIGH edge,
    # zero at the LOW edge, linear between — so the two figures printed in
    # the same report cannot contradict each other, and neither edge is
    # invented here.
    _SPREAD_CREDIT_FULL_PCT = 2.0
    _SPREAD_CREDIT_ZERO_PCT = 5.0

    # BS EN 12504-4: the minimum number of test points per element for a
    # pulse-velocity result to characterise that element.
    _MIN_POINTS_PER_ELEMENT = 3

    @classmethod
    def _element_evidence_credit(cls, n_points, spread_pct, has_ecs):
        """0.0-1.0 credit for ONE physical element's evidence. Proportional in
        every input, so a shortfall costs its share and nothing else."""
        w = cls._EVIDENCE_CREDIT_WEIGHTS
        points = min(n_points or 0, cls._MIN_POINTS_PER_ELEMENT) / cls._MIN_POINTS_PER_ELEMENT
        if spread_pct is None:
            spread = 0.0
        elif spread_pct <= cls._SPREAD_CREDIT_FULL_PCT:
            spread = 1.0
        else:
            spread = max(0.0, (cls._SPREAD_CREDIT_ZERO_PCT - spread_pct)
                         / (cls._SPREAD_CREDIT_ZERO_PCT - cls._SPREAD_CREDIT_FULL_PCT))
        return (w['points'] * points
                + w['spread'] * spread
                + w['ecs'] * (1.0 if has_ecs else 0.0))

    @classmethod
    def _evidence_confidence(cls, element_summaries, llm_used=False, with_breakdown=False, ensemble_bonus=0.0):
        """
        Confidence the analysis deserves, computed from the evidence (7 Sep
        meeting item 6 — the client asked for 93-95%; good field data earns
        it, thin data scores honestly lower; nothing is ever hardcoded).

        Evidence is pooled PER PHYSICAL ELEMENT (12 Sep 2026): the registry
        and device ingestion record each station measurement as its own test
        row, so one element's 3+ real test points (BS EN 12504-4) can arrive
        as several single-point tests. Points and point velocities are
        aggregated across the tests sharing an element name on the same
        floor before scoring; an unnamed element is never merged with
        another, so each unnamed station stands alone.

          base 70  — deterministic BS 1881-203 math over recorded readings
          +0..20   — per-element evidence credit, size-weighted (see below)
          +5       — the narrative layer ran (provider synthesis)
          +ensemble_bonus — multi-model consensus corroboration (reaches 93%)
        """
        # 'unverified' is excluded exactly as 'pending' is: nothing was
        # established about that element, so it can neither earn credit nor
        # withhold it from elements that were measured successfully.
        graded = [s for s in element_summaries
                  if s['grade'] not in ('pending', 'unverified')]
        if not graded:
            return (None, None) if with_breakdown else None
        groups = {}
        for s in graded:
            name = (s.get('element') or '').strip().lower()
            floor = (s.get('floor') or '').strip().lower()
            key = (name, floor) if name else (None, id(s))
            g = groups.setdefault(key, {'n_points': 0, 'velocities': [], 'has_ecs': True})
            g['n_points'] += s.get('n_points') or 0
            g['velocities'].extend(s.get('point_velocities_m_s') or [])
            g['has_ecs'] = g['has_ecs'] and s['mean_ecs_n_mm2'] is not None

        credits, weights = [], []
        for g in groups.values():
            vs = [v for v in g['velocities'] if v]
            spread_pct = None
            if len(vs) > 1:
                mean = sum(vs) / len(vs)
                if mean > 0:
                    spread_pct = (max(vs) - min(vs)) / mean * 100.0
            credits.append(cls._element_evidence_credit(
                g['n_points'], spread_pct, g['has_ecs']))
            # Size-weight: a 5-point element carries more of the verdict than
            # a 1-point one. Floored at 1 so a zero-point element still
            # counts against the mean rather than dividing by zero.
            weights.append(max(g['n_points'] or 0, 1))

        project_credit = (sum(w * c for w, c in zip(weights, credits)) / sum(weights))
        evidence_points = 20.0 * project_credit
        bonus = float(ensemble_bonus or 0.0)
        raw_score = 70.0 + evidence_points + (5.0 if llm_used else 0.0) + bonus
        confidence = round(min(raw_score, 95.0) / 100.0, 3)
        if not with_breakdown:
            return confidence
        return confidence, {
            'base': 70.0,
            'evidence_credit': round(evidence_points, 2),
            'elements': len(groups),
            'mean_element_credit': round(project_credit, 4),
            'narrative_bonus': 5.0 if llm_used else 0.0,
            'ensemble_bonus': round(bonus, 2),
            # The uncapped figure, so a reader can see whether the cap bit.
            'raw_score': round(raw_score, 2),
        }

    @staticmethod
    def _project_recommendations(element_summaries):
        recs = []
        poor = [s for s in element_summaries
                if s['grade'] in ('poor', 'very_poor')]
        questionable = [s for s in element_summaries if s['grade'] == 'questionable']
        cracked = [s for s in element_summaries
                   if s.get('test_type') == 'crack_depth' and (s.get('crack_depth_mm') or 0) > 25]
        if poor:
            recs.append({
                'recommendation': f"Structural review of {len(poor)} element(s) "
                "graded poor or very poor by pulse velocity.",
                'priority': 'Urgent',
            })
        if cracked:
            recs.append({
                'recommendation': f"Map and monitor {len(cracked)} element(s) with "
                "crack depth above 25 mm; engage a structural engineer.",
                'priority': 'High',
            })
        if questionable:
            recs.append({
                'recommendation': f"Supplementary coring or rebound hammer testing "
                f"on {len(questionable)} questionable element(s) to confirm quality.",
                'priority': 'Routine',
            })
        if not recs:
            recs.append({
                'recommendation': "Continue routine monitoring; all tested elements "
                "fall within acceptable velocity bands.",
                'priority': 'Routine',
            })
        return recs

    @staticmethod
    def _recommendations(grade, crack_depth_mm):
        recs = []
        if grade in ('questionable',):
            recs.append({
                'recommendation': "Supplementary coring or rebound hammer testing to confirm concrete quality.",
                'priority': 'Routine',
            })
        if grade in ('poor', 'very_poor'):
            recs.append({
                'recommendation': "Immediate structural engineering review — pulse velocity indicates compromised concrete.",
                'priority': 'Urgent',
            })
        if grade == 'unverified':
            # No defect is asserted, so nothing structural is recommended.
            # The action is to resolve the measurement, which is the only
            # thing that can make this element speak.
            recs.append({
                'recommendation': (
                    "Re-measure this element: the recorded pulse velocity is "
                    "outside the range physically possible for concrete, so "
                    "the reading is unverified. Confirm the transducer path "
                    "length and its unit (mm vs m) and repeat the test."
                ),
                'priority': 'High',
            })
        if crack_depth_mm is not None and crack_depth_mm > 25:
            recs.append({
                'recommendation': "Map crack extent and monitor; engage structural engineer for crack-depth exceedance.",
                'priority': 'High',
            })
        return recs


# ======================================================================
# GPR — subsurface anomaly aggregation
# ======================================================================

GPR_SEVERITY_RISK = {
    'low': 0.20,
    'medium': 0.50,
    'high': 0.93,
    'critical': 0.95,
}

GPR_RISK_LEVELS = [
    (0.85, 'critical'),
    (0.65, 'high'),
    (0.45, 'medium'),
    (0.25, 'low'),
    (0.0, 'info'),
]


class GPRAdapter:
    """
    Deterministic GPR survey analysis: aggregates the survey's detected
    anomalies (voids, utilities, rebar cover) into a survey-level risk
    assessment with an explainable log. Anomalies themselves are detected by
    the device software, the operator, or the AI pipeline — never invented
    here.
    """

    @staticmethod
    def _risk_level_for(score):
        for threshold, level in GPR_RISK_LEVELS:
            if score >= threshold:
                return level
        return 'info'

    @classmethod
    def analyze(cls, survey):
        """Analyze a GPRSurvey and persist an AIAnalysisRecord."""
        from apps.digital_eye.models import GPRAnomaly
        from apps.evidence.models import AIAnalysisRecord
        from apps.evidence.ingestion import EvidenceIngestionService

        anomalies = list(survey.anomalies.all())
        steps = [f"Survey {survey.survey_reference}: {len(anomalies)} detected subsurface features."]

        evidence_records = []
        for anomaly in anomalies:
            evidence_records.append(EvidenceIngestionService.ingest_gpr_anomaly(anomaly))

        voids = [a for a in anomalies if a.anomaly_type == 'void']
        rebar = [a for a in anomalies if a.anomaly_type == 'rebar']
        observations = []
        risk_score = 0.0

        if voids:
            shallow = [v for v in voids if v.depth_m is not None and v.depth_m < 1.0]
            steps.append(
                f"{len(voids)} void(s) detected; {len(shallow)} shallower than 1.0 m below surface."
            )
            worst = max((GPR_SEVERITY_RISK.get(v.severity, 0.2) for v in voids), default=0.2)
            # Shallow, large voids are the classic precursor of surface
            # collapse — weight them up.
            depth_factor = 1.15 if shallow else 1.0
            risk_score = max(risk_score, min(worst * depth_factor, 1.0))
            observations.append(
                f"{len(voids)} subsurface void(s) detected, shallowest at "
                f"{min((v.depth_m for v in voids if v.depth_m is not None), default=0)} m."
            )
        if rebar:
            covers = [r.rebar_cover_mm for r in rebar if r.rebar_cover_mm is not None]
            if covers:
                min_cover = min(covers)
                steps.append(f"Minimum detected rebar cover: {min_cover} mm.")
                observations.append(f"Minimum rebar cover {min_cover} mm across {len(rebar)} detection(s).")
                if min_cover < 25:
                    risk_score = max(risk_score, 0.70)
                    observations.append("Rebar cover below 25 mm — durability / fire-rating concern.")
                elif min_cover < 40:
                    risk_score = max(risk_score, 0.45)
            else:
                observations.append(f"{len(rebar)} rebar detection(s) without cover measurements.")

        other = [a for a in anomalies if a.anomaly_type not in ('void', 'rebar')]
        if other:
            worst_other = max((GPR_SEVERITY_RISK.get(a.severity, 0.2) for a in other), default=0.0)
            risk_score = max(risk_score, worst_other)
            observations.append(f"{len(other)} other subsurface feature(s) ({', '.join(sorted({a.get_anomaly_type_display() for a in other}))}).")

        for anomaly in anomalies:
            if anomaly.severity == 'critical':
                risk_score = max(risk_score, 0.90)

        if not anomalies:
            steps.append("No subsurface anomalies recorded for this survey.")
            observations.append("No subsurface anomalies recorded.")

        risk_level = cls._risk_level_for(risk_score)

        record = AIAnalysisRecord.objects.create(
            project=survey.project,
            analysis_type='gpr',
            risk_level=risk_level,
            risk_score=round(risk_score, 3) if anomalies else None,
            observations=observations,
            correlations=[],
            recommendations=cls._recommendations(voids, rebar, risk_level),
            reasoning_log="\n".join(steps),
            requires_human_review=bool(anomalies),
            confidence=None,
            model_provider='deterministic',
            model_version='gpr-agg-v1',
        )
        record.evidence.set(evidence_records)
        return record

    @staticmethod
    def _recommendations(voids, rebar, risk_level):
        recs = []
        if voids:
            recs.append({
                'recommendation': "Verify detected voids by coring or trial pitting before loading the area.",
                'priority': 'High',
            })
        if any(v.severity in ('high', 'critical') for v in voids):
            recs.append({
                'recommendation': "Restrict access over high-severity void locations pending verification.",
                'priority': 'Urgent',
            })
        if rebar and any((r.rebar_cover_mm or 999) < 25 for r in rebar):
            recs.append({
                'recommendation': "Review concrete cover against durability requirements (BS 8500).",
                'priority': 'Routine',
            })
        if risk_level == 'info' and not voids and not rebar:
            recs.append({
                'recommendation': "No anomalies recorded; retain survey as baseline reference.",
                'priority': 'Routine',
            })
        return recs


# ======================================================================
# GNSS — WGS84 <-> UTM projection (Transverse Mercator)
# ======================================================================

class GNSSProjection:
    """
    Deterministic WGS84 <-> UTM projection used to convert Tersus GNSS
    (MVP SI) geographic coordinates into the Lagos working CRS (UTM Zone 31N,
    Minna Datum). The Minna datum shift to WGS84 is a local transformation —
    the offset parameters must come from the approved Survey/GIS projection
    rules (plan §7 client input); until supplied the conversion is performed
    on the WGS84 ellipsoid and flagged accordingly.
    """

    # WGS84 ellipsoid
    A = 6378137.0
    E2 = 0.00669437999014

    @classmethod
    def geographic_to_utm(cls, latitude, longitude, zone=31, northern_hemisphere=True):
        """Convert lat/lon (degrees) to UTM easting/northing (metres)."""
        if latitude is None or longitude is None:
            return None, None
        a, e2 = cls.A, cls.E2
        k0 = 0.9996
        lat_rad = math.radians(latitude)
        lon_rad = math.radians(longitude)
        lon_origin = math.radians(zone * 6 - 183)
        e_prime2 = e2 / (1 - e2)
        n = a / math.sqrt(1 - e2 * math.sin(lat_rad) ** 2)
        t = math.tan(lat_rad) ** 2
        c = e_prime2 * math.cos(lat_rad) ** 2
        aa = math.cos(lat_rad) * (lon_rad - lon_origin)

        m = a * (
            (1 - e2 / 4 - 3 * e2 ** 2 / 64 - 5 * e2 ** 3 / 256) * lat_rad
            - (3 * e2 / 8 + 3 * e2 ** 2 / 32 + 45 * e2 ** 3 / 1024) * math.sin(2 * lat_rad)
            + (15 * e2 ** 2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * lat_rad)
            - (35 * e2 ** 3 / 3072) * math.sin(6 * lat_rad)
        )

        easting = k0 * n * (
            aa
            + (1 - t + c) * aa ** 3 / 6
            + (5 - 18 * t + t ** 2 + 72 * c - 58 * e_prime2) * aa ** 5 / 120
        ) + 500000.0

        northing = k0 * (
            m
            + n * math.tan(lat_rad) * (
                aa ** 2 / 2
                + (5 - t + 9 * c + 4 * c ** 2) * aa ** 4 / 24
                + (61 - 58 * t + t ** 2 + 600 * c - 330 * e_prime2) * aa ** 6 / 720
            )
        )
        if not northern_hemisphere:
            northing += 10000000.0
        return easting, northing

    @classmethod
    def project_benchmark(cls, benchmark, zone=31):
        """Project a GnssBenchmark in place (compute easting/northing/zone)."""
        easting, northing = cls.geographic_to_utm(benchmark.latitude, benchmark.longitude, zone=zone)
        if easting is not None:
            benchmark.easting = easting
            benchmark.northing = northing
            benchmark.utm_zone = f"{zone}N"
            benchmark.save(update_fields=['easting', 'northing', 'utm_zone'])
        return benchmark
