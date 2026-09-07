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

PUNDIT_GRADE_RISK = {
    'excellent': ('info', 0.05),
    'good': ('low', 0.20),
    'questionable': ('medium', 0.50),
    'poor': ('high', 0.78),
    'very_poor': ('critical', 0.95),
    'pending': (None, None),
}


def _ecs_of(velocity_km_s):
    """E.C.S (N/mm2) through the report's disclosed calibration curve —
    the single source of truth lives in apps.reports.ndt_reports."""
    from apps.reports.ndt_reports import estimated_compressive_strength
    return estimated_compressive_strength(velocity_km_s)


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
        """BS 1881-203 / ASTM C597 concrete quality classification."""
        if velocity_km_s is None:
            return 'pending'
        for threshold, grade in PUNDIT_GRADE_THRESHOLDS:
            if velocity_km_s >= threshold:
                return grade
        return 'very_poor'

    @staticmethod
    def compute_crack_depth_mm(crack_path_length_mm, crack_pulse_time_us, uncracked_pulse_time_us):
        """Time-difference crack depth: d = L/2 * sqrt((t_c/t_0)^2 - 1)."""
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
                mean_ecs = _ecs_of(velocity)
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
        if has_readings:
            # Multi-reading crack tests: the element verdict is the mean of
            # the per-point depths (persisted by the serializer); the scalar
            # columns hold only point A for legacy consumers.
            crack_depth = test.element_mean_crack_depth_mm()
        else:
            crack_depth = cls.compute_crack_depth_mm(
                test.crack_path_length_mm, test.crack_pulse_time_us, test.uncracked_pulse_time_us,
            )

        grade = cls.grade_quality(velocity)
        if grade != 'pending':
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
        if grade != 'pending':
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
            confidence=1.0 if velocity is not None else None,
            model_provider=provider,
            model_version=model_version,
        )
        record.evidence.set([evidence])
        return record

    # -------------------------------------------------- LLM narrative (D1)
    @classmethod
    def _fact_pack(cls, test, rows, velocity, grade, crack_depth):
        """Compact, numbers-only description of the real measurements — the
        ONLY data the LLM ever sees, so it cannot invent values."""
        pack = {
            'project': getattr(test.project, 'name', None),
            'element': test.structural_element or None,
            'floor': test.floor or None,
            'test_type': test.get_test_type_display(),
            'path_length_mm': rows[0]['path_mm'] if rows else test.path_length_mm,
            'transducer_frequency_khz': test.transducer_frequency_khz,
            'weather_condition': test.weather_condition or None,
            'surface_temperature_c': test.surface_temperature_c,
            'surface_condition': test.surface_condition or None,
            'test_points': [
                {'point': r['label'],
                 'transit_time_us': r['transit_us'],
                 'uncracked_transit_time_us': r['uncracked_us'],
                 'velocity_km_s': None if r['velocity_km_s'] is None else round(r['velocity_km_s'], 3),
                 'ecs_n_mm2': None if r['ecs_mpa'] is None else round(r['ecs_mpa'], 1),
                 'crack_depth_mm': None if r['crack_depth_mm'] is None else round(r['crack_depth_mm'], 1),
                 'surface_condition': r['surface_condition'] or None}
                for r in rows
            ],
            'element_mean_velocity_km_s': None if velocity is None else round(velocity, 3),
            'element_mean_ecs_n_mm2': None if velocity is None else (
                None if _ecs_of(velocity) is None else round(_ecs_of(velocity), 1)),
            'bs_1881_203_quality_grade': grade,
            'crack_depth_mm': None if crack_depth is None else round(crack_depth, 1),
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
                    or any(r.get('velocity_km_s') or r.get('crack_depth_mm')
                           or r.get('surface_condition')
                           for r in fact_pack.get('test_points', [])))
        if not measured:
            # Nothing measured — no narrative to contextualise; the honest
            # deterministic "insufficient measurements" record stands.
            return None
        try:
            data = AIService.generate_structured_json(
                "You are the Nexucon PUNDIT ultrasonic NDT analysis layer. "
                "Based ONLY on the following real field measurements, write 2-4 concise "
                "engineering observations about this structural element's concrete "
                "condition (velocity bands, point-to-point variation, strength estimate, "
                "any crack indication). Do not invent facts, numbers, or events that are "
                "not present in the data. Return JSON: "
                '{"observations": ["..."]}\n\n'
                f"Measured data: {fact_pack}"
            )
            observations = data.get('observations') if isinstance(data, dict) else None
            if observations and isinstance(observations, list) and observations:
                return (
                    [str(o) for o in observations],
                    AIService._get_provider(),
                    str(AIService._get_gemini_model() if AIService._get_provider() == 'gemini'
                        else AIService._get_openai_model()),
                )
        except Exception as e:  # noqa: BLE001 — provider down must never break analysis
            logger.info("PUNDIT LLM narrative unavailable (%s) — deterministic record stands.", e)
        return None

    # ------------------------------------------------ project-level narrative
    @classmethod
    def analyze_project(cls, project, requested_by=None):
        """
        One project-level PUNDIT analysis (review meeting D1): aggregates the
        per-element verdicts of every PUNDITTest on the project, writes the
        deterministic project summary, and asks the configured LLM for a
        project narrative from the same real numbers. Provider failure keeps
        the deterministic record. Returns the AIAnalysisRecord.
        """
        from apps.evidence.models import AIAnalysisRecord
        from apps.digital_eye.models import PUNDITTest  # noqa: F401 — imported late to avoid cycles

        tests = list(PUNDITTest.objects.filter(project=project))

        steps = [f"Project PUNDIT roll-up over {len(tests)} recorded element(s)."]
        element_summaries = []
        worst = ('info', 0.0)
        rank = {'info': 0, 'low': 1, 'medium': 2, 'high': 3, 'critical': 4}
        for test in tests:
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
                + (f": mean velocity {velocity:.2f} km/s, grade '{grade}'."
                   if velocity is not None else ": no computable velocity.")
            )
            element_summaries.append({
                'element': test.structural_element or None,
                'floor': test.floor or None,
                'n_points': len(test.reading_rows()),
                'mean_velocity_km_s': None if velocity is None else round(velocity, 3),
                'grade': grade,
                'crack_depth_mm': None if test.crack_depth_mm is None
                else round(test.crack_depth_mm, 1),
            })

        graded = [s['grade'] for s in element_summaries if s['grade'] != 'pending']
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

        observations = deterministic_observations
        provider, model_version = 'deterministic', 'BS 1881-203 / ASTM C597 v1'
        fact_pack = {
            'project': project.name,
            'elements': [{k: v for k, v in s.items() if v is not None}
                         for s in element_summaries],
            'counts': {'graded': len(graded), 'good_or_better': good, 'below_good': poor},
        }
        try:
            from apps.common.ai_service import AIService
            data = AIService.generate_structured_json(
                "You are the Nexucon PUNDIT ultrasonic NDT analysis layer. "
                "Based ONLY on the following real per-element ultrasonic test "
                "results, write 3-5 concise engineering observations for the "
                "project (overall concrete quality, element-to-element variation, "
                "any weak elements or crack indications, floors affected). Do not "
                "invent facts, numbers, or events that are not present in the data. "
                'Return JSON: {"observations": ["..."]}\n\n'
                f"Measured data: {fact_pack}"
            )
            llm_obs = data.get('observations') if isinstance(data, dict) else None
            if llm_obs and isinstance(llm_obs, list) and llm_obs:
                observations = [str(o) for o in llm_obs]
                provider = AIService._get_provider()
                model_version = str(
                    AIService._get_gemini_model() if provider == 'gemini'
                    else AIService._get_openai_model())
                steps.append(
                    f"Project narrative synthesised by {provider} ({model_version}) "
                    "from the per-element measurements above.")
        except Exception as e:  # noqa: BLE001 — provider down must never break the roll-up
            logger.info("PUNDIT project LLM narrative unavailable (%s) — "
                        "deterministic record stands.", e)

        record = AIAnalysisRecord.objects.create(
            project=project,
            analysis_type='pundit',
            risk_level=worst[0],
            risk_score=worst[1] or None,
            observations=observations,
            correlations=[],
            recommendations=cls._project_recommendations(element_summaries),
            reasoning_log="\n".join(steps),
            requires_human_review=True,
            confidence=1.0 if graded else None,
            model_provider=provider,
            model_version=model_version,
        )
        return record

    @staticmethod
    def _project_recommendations(element_summaries):
        recs = []
        poor = [s for s in element_summaries
                if s['grade'] in ('poor', 'very_poor')]
        questionable = [s for s in element_summaries if s['grade'] == 'questionable']
        cracked = [s for s in element_summaries
                   if (s['crack_depth_mm'] or 0) > 25]
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
    'high': 0.78,
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
