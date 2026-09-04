"""
Cross-Source Evidence Correlation Engine (implementation plan §5 Week 4).

Correlates GPR + PUNDIT + GNSS + BIM + scan + inspection evidence per
structural element (e.g. Column COL-C24) into a contextual element graph,
computes explainable multi-source risk indicators, and persists
CorrelationFindings with immutable revision history.

The risk computation is fully deterministic and explainable:
  1. Group registry evidence by (structural_element_id | bim_guid | spatial
     proximity).
  2. Score each source record from its payload severity.
  3. Weight and combine; corroborating independent source types increase the
     score (multi-source agreement), contradictions reduce it.
  4. Emit a reasoning log naming every contributing record.

Optional LLM narrative synthesis is added on top when configured, and is
stored separately from the deterministic reasoning — never replacing it.
"""
import logging
from datetime import datetime

from .models import AIAnalysisRecord, CorrelationFinding, FindingRevision
from .ingestion import EvidenceIngestionService

logger = logging.getLogger(__name__)

# Source reliability weights (deterministic, documented in the reasoning log).
SOURCE_WEIGHTS = {
    'gpr': 1.00,
    'pundit': 1.00,
    'inspection_finding': 0.95,
    'scan_defect': 0.90,
    'scan_alignment': 0.85,
    'bim_element': 0.60,
    'scan_thermal': 0.70,
    'gnss': 0.60,
    'scan_progress': 0.40,
    'inspection': 0.50,
    'document': 0.40,
    'live_stream': 0.30,
    'corrective_action': 0.50,
    'other': 0.40,
}

# Severity -> base risk contribution.
SEVERITY_SCORES = {
    'critical': 0.95,
    'high': 0.78,
    'medium': 0.50,
    'low': 0.25,
    'info': 0.05,
    'Major': 0.75,
    'Minor': 0.35,
    'CRITICAL': 0.95,
    'HIGH': 0.78,
    'MEDIUM': 0.50,
    'LOW': 0.25,
    'Critical': 0.95,
    'Major': 0.75,
    'Minor': 0.35,
    'poor': 0.78,
    'very_poor': 0.95,
    'questionable': 0.50,
    'good': 0.15,
    'excellent': 0.05,
    'fail': 0.85,
    'pass': 0.05,
}

RISK_LEVELS = [
    (0.85, 'critical'),
    (0.65, 'high'),
    (0.45, 'medium'),
    (0.25, 'low'),
    (0.0, 'info'),
]

# Spatial clustering radius (metres) for evidence without element IDs.
SPATIAL_CLUSTER_RADIUS_M = 2.0

# Agreement boost per additional corroborating source type.
AGREEMENT_BOOST = 0.06
AGREEMENT_CAP = 0.15

STRUCTURAL_SOURCES = {'gpr', 'pundit', 'scan_defect', 'scan_alignment', 'bim_element'}


def score_severity(payload, source_type):
    """Deterministic base risk for one evidence record from its payload."""
    for key in ('severity', 'quality_grade', 'status'):
        value = payload.get(key)
        if value in SEVERITY_SCORES:
            return SEVERITY_SCORES[value]
    if source_type == 'pundit':
        grade = payload.get('quality_grade')
        return SEVERITY_SCORES.get(grade)
    if source_type == 'scan_alignment':
        mean_mm = payload.get('mean_deviation_mm')
        if mean_mm is None:
            return None
        # Tolerance framework used by the compliance-check generator: mean
        # <=15 mm pass, >20 mm fail (interpolated in between).
        if mean_mm <= 15:
            return 0.10
        if mean_mm >= 20:
            return 0.80
        return 0.10 + (mean_mm - 15) / 5.0 * 0.70
    if source_type == 'gpr':
        depth = payload.get('depth_m')
        if depth is not None and depth < 1.0:
            return 0.60
        return 0.35
    return None


def risk_level_for(score):
    for threshold, level in RISK_LEVELS:
        if score >= threshold:
            return level
    return 'info'


class CorrelationEngine:
    """
    Builds the contextual structural-element graph and computes explainable
    multi-source risk for every correlated element of a project.
    """

    # ------------------------------------------------------------------
    # Grouping (contextual element graph)
    # ------------------------------------------------------------------

    @classmethod
    def _evidence_groups(cls, project):
        """
        Group the project's EvidenceRecords into correlation groups:
        by structural element id, else by BIM GUID, else by spatial cluster.
        """
        groups = {}
        ungrouped = []
        records = list(
            project.evidence_records.all().order_by('created_at')
        )

        for record in records:
            if record.structural_element_id:
                groups.setdefault(('element', record.structural_element_id), []).append(record)
            elif record.bim_guid:
                groups.setdefault(('guid', record.bim_guid), []).append(record)
            else:
                ungrouped.append(record)

        # Spatial clustering for records with geographic coordinates only.
        clusters = []
        for record in ungrouped:
            coords = (record.coordinates or {}).get('latitude'), (record.coordinates or {}).get('longitude')
            if coords[0] is None or coords[1] is None:
                # No element, no GUID, no coordinates — correlate by source
                # type alone so it is still represented in the project graph.
                groups.setdefault(('source', record.source_type), []).append(record)
                continue
            placed = False
            for cluster in clusters:
                anchor = cluster[0]
                if haversine_m(coords, ((anchor.coordinates or {}).get('latitude'),
                                        (anchor.coordinates or {}).get('longitude'))) <= SPATIAL_CLUSTER_RADIUS_M:
                    cluster.append(record)
                    placed = True
                    break
            if not placed:
                clusters.append([record])
        for i, cluster in enumerate(clusters):
            groups.setdefault(('spatial', f'cluster-{i + 1}'), []).extend(cluster)

        return groups

    # ------------------------------------------------------------------
    # Risk computation
    # ------------------------------------------------------------------

    @classmethod
    def _score_group(cls, key, records):
        """
        Deterministic multi-source risk score with an explainable log.
        Returns (risk_score, risk_level, reasoning, element_id, bim_guid).
        """
        contributions = []
        by_source = {}
        for record in records:
            base = score_severity(record.payload, record.source_type)
            if base is None:
                continue
            weight = SOURCE_WEIGHTS.get(record.source_type, 0.4)
            confidence = record.confidence if record.confidence is not None else 0.8
            contribution = base * weight * (0.5 + 0.5 * confidence)
            by_source.setdefault(record.source_type, []).append((record, contribution))
            contributions.append((record, base, weight, confidence, contribution))

        reasoning_lines = []
        if key[0] == 'element':
            reasoning_lines.append(f"Structural element {key[1]}: {len(records)} correlated evidence record(s).")
        elif key[0] == 'guid':
            reasoning_lines.append(f"BIM element GUID {key[1]}: {len(records)} correlated evidence record(s).")
        elif key[0] == 'spatial':
            reasoning_lines.append(f"Spatial cluster ({SPATIAL_CLUSTER_RADIUS_M} m radius): {len(records)} record(s).")
        else:
            reasoning_lines.append(f"Source group {key[1]}: {len(records)} record(s).")

        if not contributions:
            reasoning_lines.append("No scorable evidence in this group — risk not computed.")
            return None, 'info', "\n".join(reasoning_lines), '', ''

        # Weighted mean of contributions.
        total_weight = sum(w for _, _, w, _, _ in contributions)
        weighted_score = sum(c for _, _, _, _, c in contributions) / total_weight if total_weight else 0.0

        # Multi-source agreement: independent source types agreeing on risk
        # raise confidence in the finding.
        corroborating_types = {
            source: max(c for _, c in items)
            for source, items in by_source.items()
            if max(c for _, c in items) >= 0.25
        }
        agreement_boost = min(AGREEMENT_BOOST * max(0, len(corroborating_types) - 1), AGREEMENT_CAP)

        # Structural corroboration (plan example: GPR void + PUNDIT low
        # velocity + BIM deviation on the same column => HIGH).
        structural_types = {s for s in corroborating_types if s in STRUCTURAL_SOURCES}
        if len(structural_types) >= 2:
            agreement_boost += 0.08
            reasoning_lines.append(
                f"Multi-source structural corroboration: {len(structural_types)} independent structural sources "
                f"({', '.join(sorted(structural_types))}) indicate risk on this element."
            )

        for record, base, weight, confidence, contribution in contributions:
            reasoning_lines.append(
                f"- {record.evidence_reference} [{record.get_source_type_display()}]: base risk {base:.2f} "
                f"x source weight {weight:.2f} x confidence factor {0.5 + 0.5 * confidence:.2f} "
                f"= {contribution:.3f}."
            )

        risk_score = min(1.0, weighted_score + agreement_boost)
        reasoning_lines.append(
            f"Weighted mean {weighted_score:.3f} + cross-source agreement boost {agreement_boost:.3f} "
            f"= risk score {risk_score:.3f} ({risk_level_for(risk_score)})."
        )
        reasoning_lines.append(
            "AI output is decision-support only: this finding requires qualified human review "
            "before entering the official record."
        )

        element_id = key[1] if key[0] == 'element' else ''
        bim_guid = key[1] if key[0] == 'guid' else ''
        return round(risk_score, 3), risk_level_for(risk_score), "\n".join(reasoning_lines), element_id, bim_guid

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @classmethod
    def run(cls, project, ingested_by=None, sync_sources=True):
        """
        (Re)run correlation for a project. Optionally re-ingests all sources
        first (sync_sources=True), then recomputes every element finding.

        Findings already reviewed by a human are NOT overwritten — new
        evidence creates a new revision on the pending finding instead.
        Returns the list of findings.
        """
        if sync_sources:
            EvidenceIngestionService.ingest_all_for_project(project, ingested_by=ingested_by)

        findings = []
        groups = cls._evidence_groups(project)
        for key, records in groups.items():
            risk_score, risk_level, reasoning, element_id, bim_guid = cls._score_group(key, records)
            if not records:
                continue

            # Look for an existing pending finding for this correlation group.
            existing = CorrelationFinding.objects.filter(
                project=project, group_key=key[0] + ':' + str(key[1]), status='pending_review',
            ).first()

            title = cls._title_for(key, risk_level)
            description = (
                f"Cross-source correlation of {len(records)} evidence record(s) "
                f"({', '.join(sorted({r.get_source_type_display() for r in records}))})."
            )

            if existing:
                existing.risk_score = risk_score
                existing.risk_level = risk_level
                existing.reasoning = reasoning
                existing.description = description
                existing.evidence.set(records)
                existing.updated_at = existing.updated_at
                existing.save()
                cls._add_revision(existing, 'new_evidence', changed_by=ingested_by,
                                  notes='Correlation re-run with new evidence.')
                findings.append(existing)
                continue

            analysis = AIAnalysisRecord.objects.create(
                project=project,
                analysis_type='correlation',
                risk_level=risk_level,
                risk_score=risk_score,
                observations=[description],
                correlations=[
                    {'evidence_reference': r.evidence_reference, 'source_type': r.source_type}
                    for r in records
                ],
                recommendations=[],
                reasoning_log=reasoning,
                requires_human_review=True,
                model_provider='deterministic',
                model_version='correlation-engine-v1',
            )
            analysis.evidence.set(records)

            finding = CorrelationFinding.objects.create(
                project=project,
                structural_element_id=element_id,
                bim_guid=bim_guid,
                group_key=key[0] + ':' + str(key[1]),
                title=title,
                description=description,
                risk_level=risk_level,
                risk_score=risk_score,
                reasoning=reasoning,
                analysis=analysis,
                status='pending_review',
            )
            finding.evidence.set(records)
            cls._add_revision(finding, 'created', changed_by=ingested_by,
                              notes='Finding created by correlation engine.')
            findings.append(finding)

        logger.info("Correlation engine: %d group(s) processed for project %s", len(groups), project.id)
        return findings

    @staticmethod
    def _title_for(key, risk_level):
        label = key[1] if key[0] in ('element', 'guid') else key[1].replace('-', ' ').title()
        return f"{label}: {risk_level.upper()} multi-source risk"

    @classmethod
    def _add_revision(cls, finding, change_reason, changed_by=None, notes=''):
        """Append an immutable, hash-chained revision record."""
        next_number = finding.revision_count + 1
        snapshot = {
            'title': finding.title,
            'description': finding.description,
            'risk_level': finding.risk_level,
            'risk_score': finding.risk_score,
            'status': finding.status,
            'structural_element_id': finding.structural_element_id,
            'bim_guid': finding.bim_guid,
            'reasoning': finding.reasoning,
            'review_notes': finding.review_notes,
            'evidence_ids': [str(e.id) for e in finding.evidence.all()],
        }
        last = finding.revisions.order_by('-revision_number').first()
        previous_hash = last.revision_hash if last else ''
        revision = FindingRevision(
            finding=finding,
            revision_number=next_number,
            change_reason=change_reason,
            snapshot=snapshot,
            changed_by=changed_by,
            notes=notes,
            previous_hash=previous_hash,
        )
        revision.revision_hash = revision.compute_hash(previous_hash)
        revision.save()
        finding.revision_count = next_number
        finding.save(update_fields=['revision_count', 'updated_at'])
        return revision


def haversine_m(point_a, point_b):
    """Great-circle distance in metres between (lat, lon) pairs."""
    import math
    if None in (point_a + point_b):
        return float('inf')
    lat1, lon1, lat2, lon2 = map(math.radians, (point_a[0], point_a[1], point_b[0], point_b[1]))
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000.0 * 2 * math.asin(math.sqrt(a))
