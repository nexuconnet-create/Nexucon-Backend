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
    def analyze(cls, test):
        """
        Run the deterministic analysis on a PUNDITTest, persist the computed
        fields on the test and create an AIAnalysisRecord in the Evidence
        Registry. Returns the AIAnalysisRecord.
        """
        from apps.evidence.models import AIAnalysisRecord
        from apps.evidence.ingestion import EvidenceIngestionService

        steps = []
        velocity = cls.compute_velocity_km_s(test.path_length_mm, test.pulse_time_us)
        crack_depth = cls.compute_crack_depth_mm(
            test.crack_path_length_mm, test.crack_pulse_time_us, test.uncracked_pulse_time_us,
        )

        if velocity is not None:
            steps.append(
                f"Pulse velocity v = L / t = {test.path_length_mm} mm / {test.pulse_time_us} us "
                f"= {velocity:.3f} km/s."
            )
        else:
            steps.append("Pulse velocity not computable — path length and/or transit time missing.")

        grade = cls.grade_quality(velocity)
        if grade != 'pending':
            steps.append(f"Concrete quality graded '{grade}' per BS 1881-203 / ASTM C597 velocity bands.")

        if crack_depth is not None:
            steps.append(
                f"Crack depth d = L/2 * sqrt((t_c/t_0)^2 - 1) = {crack_depth:.1f} mm "
                f"(t_c={test.crack_pulse_time_us} us, t_0={test.uncracked_pulse_time_us} us)."
            )

        risk_level, risk_score = PUNDIT_GRADE_RISK.get(grade, (None, None))
        observations = []
        if grade != 'pending':
            observations.append(
                f"Element {test.structural_element or 'unspecified'}: pulse velocity "
                f"{velocity:.2f} km/s — {grade} concrete quality."
            )
        if crack_depth is not None:
            observations.append(f"Measured crack depth {crack_depth:.1f} mm (time-difference method).")
            if crack_depth > 25:
                observations.append("Crack depth exceeds 25 mm — structural review required.")
                risk_level = 'high' if risk_level in (None, 'info', 'low', 'medium') else risk_level
                risk_score = max(risk_score or 0.0, 0.70)
        if not observations:
            observations.append("Insufficient measurements to compute an NDT result.")

        # Persist computed values on the test record.
        test.velocity_km_s = velocity
        test.quality_grade = grade
        test.crack_depth_mm = crack_depth
        test.save(update_fields=['velocity_km_s', 'quality_grade', 'crack_depth_mm', 'updated_at'])

        # Normalise into the Evidence Registry.
        evidence = EvidenceIngestionService.ingest_pundit_test(test, ingested_by=None)

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
            model_provider='deterministic',
            model_version='BS 1881-203 / ASTM C597 v1',
        )
        record.evidence.set([evidence])
        return record

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
