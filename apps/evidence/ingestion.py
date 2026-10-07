"""
Evidence Ingestion Pipeline (implementation plan §5 Week 2).

Normalises heterogeneous field sources into the Centralized Evidence Registry:
every producer (Digital Eye scans, GPR surveys, PUNDIT tests, GNSS surveys,
BIM element mappings, inspection findings, approved documents, live streams)
has an ingest function that converts its record into the unified Evidence
schema (source type, structural element, BIM GUID, coordinates, timestamp,
confidence, payload).

Ingestion is idempotent on (source_model, source_id) and computes a SHA-256
evidence hash over the canonical payload for tamper evidence.
"""
import logging
from datetime import date, datetime, time
from decimal import Decimal
from uuid import UUID

from django.utils import timezone

from .models import EvidenceRecord

logger = logging.getLogger(__name__)


def json_native(value):
    """``value`` reduced to what a ``JSONField`` can store and hand back.

    A ``JSONField`` is written with ``json.dumps``, so a ``datetime`` handed to
    one raises rather than being stored — and a value that did survive would
    come back as something else. Payloads carry the capture timestamp the
    uploader's device reported, which the request parses into a ``datetime``.

    Normalising *before* the record is written is the point, not merely before
    it is serialised: ``evidence_hash`` is a hash of the payload, and the
    uploader is promised that the hash taken at ingest is the hash ``/verify/``
    recomputes from storage. Those two only agree if what was hashed is already
    the JSON the database will give back.
    """
    if isinstance(value, dict):
        return {str(key): json_native(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_native(item) for item in value]
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (Decimal, UUID)):
        return str(value)
    return value


class EvidenceIngestionService:
    """Single entry point for normalising field records into the registry."""

    # ------------------------------------------------------------------
    # Core normalisation
    # ------------------------------------------------------------------

    @classmethod
    def _ingest(
        cls, *, project, source_type, source_model, source_id,
        structural_element_id='', bim_guid='', coordinates=None,
        captured_at=None, confidence=None, payload=None, ingested_by=None,
    ) -> EvidenceRecord:
        payload = json_native(payload or {})
        coordinates = json_native(coordinates) if coordinates is not None else None
        defaults = {
            'project': project,
            'source_type': source_type,
            'structural_element_id': structural_element_id or '',
            'bim_guid': bim_guid or '',
            'coordinates': coordinates,
            'captured_at': captured_at,
            'confidence': confidence,
            'payload': payload,
            'ingested_by': ingested_by,
        }
        if source_model and source_id:
            record, created = EvidenceRecord.objects.update_or_create(
                source_model=source_model, source_id=str(source_id),
                defaults=defaults,
            )
        else:
            record = EvidenceRecord.objects.create(**defaults)
            created = True

        record.evidence_hash = record.compute_hash()
        record.save(update_fields=['evidence_hash', 'updated_at'])

        if created:
            logger.info(
                "Evidence %s ingested: %s/%s for project %s",
                record.evidence_reference, source_model, source_id, project_id_str(project),
            )
        return record

    # ------------------------------------------------------------------
    # Producer-specific normalisers
    # ------------------------------------------------------------------

    @classmethod
    def ingest_record(cls, **kwargs) -> EvidenceRecord:
        """Public entry point for a caller that has already normalised.

        Every producer method below this line knows how to read one instrument
        and hands ``_ingest`` a shaped record. The offline sync queue is not a
        producer: it replays a record the client already normalised, and its
        only job is to land it idempotently. Exposing ``_ingest`` through a
        named public method keeps that caller out of a private one without
        pretending the queue knows anything about GPR or PUNDIT.
        """
        return cls._ingest(**kwargs)

    @classmethod
    def ingest_defect(cls, defect, ingested_by=None):
        """Digital Eye visual defect (apps.scans.Defect)."""
        session = defect.session
        return cls._ingest(
            project=session.project,
            source_type='scan_defect',
            source_model='scans.Defect', source_id=defect.id,
            structural_element_id=defect.grid_zone or '',
            coordinates=coordinates_from(defect),
            captured_at=defect.created_at,
            confidence=defect.confidence_score,
            payload={
                'defect_type': defect.type,
                'severity': defect.severity,
                'status': defect.status,
                'description': defect.description,
                'session_id': str(session.id),
                'scanner_id': session.scanner_id,
                'image_url': defect.image_url,
                'is_false_positive': defect.is_false_positive,
                'room_level': defect.room_level,
            },
            ingested_by=ingested_by,
        )

    @classmethod
    def ingest_thermal_anomaly(cls, anomaly, ingested_by=None):
        """Digital Eye thermal anomaly (apps.scans.ThermalAnomaly)."""
        session = anomaly.session
        return cls._ingest(
            project=session.project,
            source_type='scan_thermal',
            source_model='scans.ThermalAnomaly', source_id=anomaly.id,
            structural_element_id=anomaly.grid_zone or '',
            coordinates=coordinates_from(anomaly),
            captured_at=anomaly.created_at,
            confidence=anomaly.confidence_score,
            payload={
                'temperature_variance': anomaly.temperature_variance,
                'severity': anomaly.severity,
                'status': anomaly.status,
                'description': anomaly.description,
                'session_id': str(session.id),
                'scanner_id': session.scanner_id,
                'image_url': anomaly.image_url,
                'room_level': anomaly.room_level,
            },
            ingested_by=ingested_by,
        )

    @classmethod
    def ingest_bim_alignment(cls, alignment, ingested_by=None):
        """Digital Eye BIM alignment / deviation result (apps.scans.BIMAlignmentResult)."""
        session = alignment.session
        return cls._ingest(
            project=session.project,
            source_type='scan_alignment',
            source_model='scans.BIMAlignmentResult', source_id=alignment.id,
            coordinates=None,
            captured_at=alignment.updated_at,
            confidence=None,
            payload={
                'alignment_status': alignment.alignment_status,
                'mean_deviation_mm': alignment.mean_deviation,
                'max_deviation_mm': alignment.max_deviation,
                'min_deviation_mm': alignment.min_deviation,
                'top_deviations': alignment.top_deviations or [],
                'clash_count': len(alignment.clashes or []),
                'session_id': str(session.id),
            },
            ingested_by=ingested_by,
        )

    @classmethod
    def ingest_gpr_anomaly(cls, anomaly, ingested_by=None):
        """GPR subsurface anomaly (apps.digital_eye.GPRAnomaly)."""
        survey = anomaly.survey
        return cls._ingest(
            project=survey.project,
            source_type='gpr',
            source_model='digital_eye.GPRAnomaly', source_id=anomaly.id,
            structural_element_id=survey.structural_element or '',
            coordinates=anomaly.coordinates or survey_coordinates(survey),
            captured_at=survey.completed_at or survey.created_at,
            confidence=anomaly.confidence,
            payload={
                'anomaly_type': anomaly.anomaly_type,
                'depth_m': anomaly.depth_m,
                'estimated_size_m': anomaly.estimated_size_m,
                'severity': anomaly.severity,
                'description': anomaly.description,
                'survey_reference': survey.survey_reference,
                'antenna_frequency_mhz': survey.antenna_frequency_mhz,
            },
            ingested_by=ingested_by,
        )

    @classmethod
    def ingest_pundit_test(cls, test, ingested_by=None):
        """PUNDIT ultrasonic NDT test (apps.digital_eye.PUNDITTest)."""
        return cls._ingest(
            project=test.project,
            source_type='pundit',
            source_model='digital_eye.PUNDITTest', source_id=test.id,
            structural_element_id=test.structural_element or '',
            coordinates=coordinates_from_point(test.latitude, test.longitude),
            captured_at=test.tested_at or test.created_at,
            confidence=test.velocity_km_s and pundit_confidence(test) or None,
            payload={
                'test_type': test.test_type,
                'path_length_mm': test.path_length_mm,
                'pulse_time_us': test.pulse_time_us,
                'velocity_km_s': test.velocity_km_s,
                'quality_grade': test.quality_grade,
                'crack_depth_mm': test.crack_depth_mm,
                'standard': 'BS 1881-203 / ASTM C597',
                'test_reference': test.test_reference,
                'element': test.structural_element,
            },
            ingested_by=ingested_by,
        )

    @classmethod
    def ingest_gnss_survey(cls, survey, ingested_by=None):
        """Tersus GNSS positioning survey summary (apps.digital_eye.GnssSurvey)."""
        benchmarks = list(survey.benchmarks.all().values(
            'point_id', 'latitude', 'longitude', 'easting', 'northing',
            'ellipsoidal_height', 'accuracy_mm',
        ))
        return cls._ingest(
            project=survey.project,
            source_type='gnss',
            source_model='digital_eye.GnssSurvey', source_id=survey.id,
            coordinates=coordinates_from_point(survey.latitude, survey.longitude),
            captured_at=survey.completed_at or survey.created_at,
            confidence=None,
            payload={
                'survey_reference': survey.survey_reference,
                'method': survey.method,
                'projection': survey.projection,
                'fix_quality': survey.fix_quality,
                'benchmark_count': len(benchmarks),
                'benchmarks': benchmarks,
                'boundary_point_count': survey.boundary_points.count(),
                'variance_summary': survey.variance_summary,
            },
            ingested_by=ingested_by,
        )

    @classmethod
    def ingest_bim_element(cls, mapping, ingested_by=None):
        """Trimble Connect / uploaded BIM element (apps.digital_eye.BIMElementMapping)."""
        return cls._ingest(
            project=mapping.project,
            source_type='bim_element',
            source_model='digital_eye.BIMElementMapping', source_id=mapping.id,
            structural_element_id=mapping.element_id,
            bim_guid=mapping.bim_guid,
            coordinates=mapping.coordinates,
            captured_at=mapping.created_at,
            confidence=None,
            payload={
                'element_id': mapping.element_id,
                'element_name': mapping.element_name,
                'element_type': mapping.element_type,
                'level': mapping.level,
                'discipline': mapping.discipline,
                'source': mapping.source,
            },
            ingested_by=ingested_by,
        )

    @classmethod
    def ingest_inspection_finding(cls, finding, ingested_by=None):
        """Field inspection finding (apps.inspections.Finding)."""
        return cls._ingest(
            project=finding.project or (finding.inspection and finding.inspection.project),
            source_type='inspection_finding',
            source_model='inspections.Finding', source_id=finding.id,
            coordinates=None,
            captured_at=finding.created_at,
            confidence=None,
            payload={
                'finding_reference': finding.finding_reference,
                'title': finding.title,
                'severity': finding.severity,
                'category': finding.category,
                'description': finding.description,
                'is_resolved': finding.is_resolved,
                'requires_reinspection': finding.requires_reinspection,
                'inspection_reference': finding.inspection.inspection_reference,
            },
            ingested_by=ingested_by,
        )

    @classmethod
    def ingest_all_for_project(cls, project, ingested_by=None) -> int:
        """
        Bulk (re)ingestion for a project: scans defects & anomalies, GPR,
        PUNDIT, GNSS, BIM elements, inspection findings. Returns the number
        of registry records touched.
        """
        from apps.scans.models import Defect, ThermalAnomaly, BIMAlignmentResult
        from apps.digital_eye.models import GPRAnomaly, PUNDITTest, GnssSurvey, BIMElementMapping
        from apps.inspections.models import Finding

        count = 0
        for defect in Defect.objects.filter(session__project=project, is_false_positive=False):
            cls.ingest_defect(defect, ingested_by); count += 1
        for anomaly in ThermalAnomaly.objects.filter(session__project=project):
            cls.ingest_thermal_anomaly(anomaly, ingested_by); count += 1
        for alignment in BIMAlignmentResult.objects.filter(session__project=project):
            cls.ingest_bim_alignment(alignment, ingested_by); count += 1
        for gpr in GPRAnomaly.objects.filter(survey__project=project):
            cls.ingest_gpr_anomaly(gpr, ingested_by); count += 1
        for test in PUNDITTest.objects.filter(project=project):
            cls.ingest_pundit_test(test, ingested_by); count += 1
        for survey in GnssSurvey.objects.filter(project=project):
            cls.ingest_gnss_survey(survey, ingested_by); count += 1
        for mapping in BIMElementMapping.objects.filter(project=project):
            cls.ingest_bim_element(mapping, ingested_by); count += 1
        for finding in Finding.objects.filter(project=project):
            cls.ingest_inspection_finding(finding, ingested_by); count += 1
        return count


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def project_id_str(project):
    return str(project.id) if project else 'none'


def coordinates_from(record):
    """Normalised coordinates from a scan-detected record (x/y/z location)."""
    if any(v is not None for v in (record.location_x, record.location_y, record.location_z)):
        return {
            'x': record.location_x, 'y': record.location_y, 'z': record.location_z,
        }
    return None


def coordinates_from_point(latitude, longitude):
    if latitude is None or longitude is None:
        return None
    return {'latitude': latitude, 'longitude': longitude}


def survey_coordinates(survey):
    return coordinates_from_point(survey.latitude, survey.longitude)


def pundit_confidence(test):
    """
    Deterministic confidence for a PUNDIT velocity result: reflects
    measurement completeness and data richness.

    - Base 0.90 for having path length + transit time (velocity computable)
    - +0.03 for multi-reading tests (more data points = higher confidence)
    - +0.02 for crack depth measurement present
    - Capped at 0.95 (AI confidence never reaches 1.0)

    Well-instrumented tests with multiple readings typically score 0.93–0.95.

    NOT the same figure as `PUNDITAdapter._evidence_confidence`, and the two
    disagreeing is correct rather than a bug. This one answers a per-record
    question — *is this one measurement complete enough to trust?* — and is
    grade-independent, so a velocity that is later declared `unverified` still
    carries the same confidence it always had. `_evidence_confidence` answers
    a project-level question — *how much does this project's evidence support
    the analysis drawn from it?* — and is size-weighted across elements. Do
    not raise one to match the other; surface both, labelled, so a reader can
    see they are two figures and not one that is broken.
    """
    if not (test.path_length_mm and test.pulse_time_us):
        return None

    confidence = 0.90  # Base: velocity is computable

    # Multi-reading tests contribute more data points
    if test.readings.exists() and test.readings.count() > 1:
        confidence += 0.03

    # Crack depth measurement adds diagnostic depth
    if test.crack_depth_mm is not None or (
        test.crack_pulse_time_us and test.uncracked_pulse_time_us
    ):
        confidence += 0.02

    return min(confidence, 0.95)
