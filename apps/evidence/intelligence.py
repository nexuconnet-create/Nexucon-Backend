"""
Project-Level AI Intelligence, Recommendation Engine and HQ Command
Intelligence (implementation plan §5 Weeks 6 & 9).

All aggregates are computed live from real database records (findings, NCRs,
inspections, corrective actions) — no cached or placeholder statistics.
LLM synthesis (observations, executive briefing narrative) is requested via
AIService when a provider is configured; on failure the deterministic
aggregate is returned unchanged with `ai_synthesis: null`.
"""
import logging
from datetime import timedelta

from django.db.models import Avg, Count, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

RISK_ORDER = {'info': 0, 'low': 1, 'medium': 2, 'high': 3, 'critical': 4}
LEVEL_FROM_SCORE = [
    (0.85, 'critical'),
    (0.65, 'high'),
    (0.45, 'medium'),
    (0.25, 'low'),
    (0.0, 'info'),
]


def level_for_score(score):
    if score is None:
        return 'info'
    for threshold, level in LEVEL_FROM_SCORE:
        if score >= threshold:
            return level
    return 'info'


class ProjectIntelligenceService:
    """
    Project-Level AI Intelligence aggregator (plan §5 Week 6):
    Overall, Structural and Compliance risk scores + AI observations and
    recommended actions.
    """

    # Weights for the overall score.
    OVERALL_WEIGHTS = {'structural': 0.5, 'compliance': 0.3, 'operational': 0.2}

    @classmethod
    def aggregate(cls, project):
        """Compute the live intelligence summary for one project."""
        from apps.compliance.models import NonConformanceReport, CorrectiveActionPlan
        from apps.inspections.models import Inspection
        from .models import AIAnalysisRecord, CorrelationFinding

        now = timezone.now()

        # --- Structural risk: worst accepted/pending correlation finding ----
        structural_findings = CorrelationFinding.objects.filter(
            project=project,
        ).exclude(status='rejected')
        structural_source_types = {'gpr', 'pundit', 'scan_defect', 'scan_alignment', 'bim_element'}
        structural_records = AIAnalysisRecord.objects.filter(
            project=project,
            analysis_type__in=['gpr', 'pundit', 'defect', 'thermal', 'bim'],
        )

        finding_scores = [
            f.risk_score for f in structural_findings
            if f.risk_score is not None and _is_structural(f)
        ]
        record_scores = [
            a.risk_score for a in structural_records if a.risk_score is not None
        ]
        # Structural risk = worst credible signal (findings dominate; per-source
        # records only apply when no correlated finding exists yet).
        if finding_scores:
            structural_score = max(finding_scores)
        elif record_scores:
            structural_score = max(record_scores)
        else:
            structural_score = None

        # --- Compliance risk: open NCRs, overdue CAPAs, failed inspections --
        ncrs = NonConformanceReport.objects.filter(project=project)
        open_ncrs = ncrs.exclude(status='Closed')
        critical_open = open_ncrs.filter(severity='Critical').count()
        major_open = open_ncrs.filter(severity='Major').count()
        overdue_capas = CorrectiveActionPlan.objects.filter(
            project=project,
        ).exclude(status='closed').filter(due_date__lt=now.date()).count()
        failed_inspections = Inspection.objects.filter(
            project=project, outcome='FAILED',
        ).count()

        compliance_score = 0.0
        if critical_open:
            compliance_score = min(1.0, 0.60 + 0.15 * critical_open)
        elif major_open:
            compliance_score = min(1.0, 0.40 + 0.10 * major_open)
        if overdue_capas:
            compliance_score = min(1.0, compliance_score + 0.10 * overdue_capas)
        if failed_inspections:
            compliance_score = min(1.0, compliance_score + 0.15)
        if not (open_ncrs.exists() or overdue_capas or failed_inspections):
            compliance_score = None if not ncrs.exists() else 0.10

        # --- Operational risk: overdue inspections, stop-work flags --------
        overdue_inspections = Inspection.objects.filter(
            project=project,
        ).filter(
            Q(status__in=['REQUESTED', 'SCHEDULED']) &
            Q(scheduled_date__lt=now)
        ).count()
        reinspection_required = Inspection.objects.filter(
            project=project, status='RE_INSPECTION_REQUIRED',
        ).count()
        operational_score = 0.0
        if overdue_inspections:
            operational_score = min(0.9, 0.30 + 0.10 * overdue_inspections)
        if reinspection_required:
            operational_score = min(0.9, operational_score + 0.15 * reinspection_required)
        if not overdue_inspections and not reinspection_required:
            operational_score = None

        # --- Overall --------------------------------------------------------
        parts = {
            'structural': structural_score,
            'compliance': compliance_score,
            'operational': operational_score,
        }
        available = {k: v for k, v in parts.items() if v is not None}
        if available:
            overall_score = sum(
                available[k] * cls.OVERALL_WEIGHTS[k] for k in available
            ) / sum(cls.OVERALL_WEIGHTS[k] for k in available)
        else:
            overall_score = None

        summary = {
            'project_id': str(project.id),
            'project_name': project.name,
            'reference_number': project.reference_number,
            'status': project.status,
            'risk_scores': {
                'overall': round(overall_score, 3) if overall_score is not None else None,
                'overall_level': level_for_score(overall_score),
                'structural': round(structural_score, 3) if structural_score is not None else None,
                'structural_level': level_for_score(structural_score),
                'compliance': round(compliance_score, 3) if compliance_score is not None else None,
                'compliance_level': level_for_score(compliance_score),
                'operational': round(operational_score, 3) if operational_score is not None else None,
            },
            'metrics': {
                'open_correlation_findings': structural_findings.filter(status='pending_review').count(),
                'accepted_findings': structural_findings.filter(status='accepted').count(),
                'critical_findings': structural_findings.filter(risk_level='critical').count(),
                'open_ncrs': open_ncrs.count(),
                'critical_open_ncrs': critical_open,
                'overdue_corrective_actions': overdue_capas,
                'failed_inspections': failed_inspections,
                'overdue_inspections': overdue_inspections,
            },
            'ai_observations': None,  # LLM synthesis below (real providers only)
            'recommended_actions': RecommendationEngine.recommend_for_project(
                project, structural_score, compliance_score, operational_score,
            ),
            'computed_at': now.isoformat(),
        }

        summary['ai_observations'] = cls._synthesise_observations(project, summary)
        return summary

    @classmethod
    def _synthesise_observations(cls, project, summary):
        """Optional LLM contextual synthesis of the real aggregate. Deterministic
        fallback text is built from the same numbers — never fabricated."""
        from apps.common.ai_service import AIService, AIProviderUnavailable, AIServiceError

        metrics = summary['metrics']
        deterministic = (
            f"Structural risk {summary['risk_scores']['structural_level']}"
            f"{' (' + str(summary['risk_scores']['structural']) + ')' if summary['risk_scores']['structural'] is not None else ' (no data)'}; "
            f"compliance risk {summary['risk_scores']['compliance_level']}; "
            f"{metrics['open_correlation_findings']} AI finding(s) awaiting review; "
            f"{metrics['open_ncrs']} open NCR(s), {metrics['overdue_corrective_actions']} overdue corrective action(s)."
        )
        try:
            data = AIService.generate_structured_json(
                "You are the Nexucon AI Evidence Intelligence layer producing decision-support "
                "observations for a government construction oversight dashboard. Based ONLY on the "
                "following real aggregate metrics, write 2-4 concise observations about this project's "
                "risk position. Do not invent facts, numbers or events not present in the data. "
                "Return JSON: {\"observations\": [\"...\"]}.\n\n"
                f"Project: {project.name} ({project.reference_number}), status {project.status}.\n"
                f"Metrics: {metrics}\nRisk scores: {summary['risk_scores']}"
            )
            observations = data.get('observations') if isinstance(data, dict) else None
            if observations and isinstance(observations, list):
                return [str(o) for o in observations]
        except (AIServiceError, AIProviderUnavailable, Exception) as e:  # noqa: BLE001
            logger.info("LLM observation synthesis unavailable (%s) — using deterministic summary.", e)
        return [deterministic]


def _is_structural(finding):
    """A finding is structural when its evidence includes structural sources."""
    structural_source_types = {'gpr', 'pundit', 'scan_defect', 'scan_alignment', 'bim_element'}
    return finding.evidence.filter(source_type__in=structural_source_types).exists()


class RecommendationEngine:
    """
    AI Recommendation Engine (plan §5 Week 6): action suggestions derived
    deterministically from the project's real risk position.
    """

    @classmethod
    def recommend_for_project(cls, project, structural_score, compliance_score, operational_score):
        from apps.compliance.models import NonConformanceReport
        from .models import CorrelationFinding

        actions = []
        critical_findings = CorrelationFinding.objects.filter(
            project=project, risk_level='critical',
        ).exclude(status='rejected').exclude(status='escalated')
        if critical_findings.exists():
            actions.append({
                'action': 'Trigger immediate statutory inspection for critical AI finding(s) and consider a stop-work order.',
                'priority': 'Urgent',
                'source': 'critical_finding',
            })

        pending = CorrelationFinding.objects.filter(project=project, status='pending_review')
        if pending.exists():
            actions.append({
                'action': f"{pending.count()} AI correlation finding(s) awaiting qualified human review — assign a reviewer.",
                'priority': 'High',
                'source': 'human_review_backlog',
            })

        if structural_score is not None and structural_score >= 0.65:
            actions.append({
                'action': 'Commission supplementary NDT testing (PUNDIT/GPR) to corroborate the structural risk signal.',
                'priority': 'High',
                'source': 'structural_risk',
            })

        open_critical_ncrs = NonConformanceReport.objects.filter(
            project=project, severity='Critical',
        ).exclude(status='Closed')
        if open_critical_ncrs.exists():
            actions.append({
                'action': f"{open_critical_ncrs.count()} critical NCR(s) open — escalate per the statutory escalation matrix.",
                'priority': 'Urgent',
                'source': 'ncr_escalation',
            })

        if compliance_score is not None and compliance_score >= 0.45:
            actions.append({
                'action': 'Schedule a compliance review; open non-conformances are raising the project compliance risk.',
                'priority': 'High',
                'source': 'compliance_risk',
            })

        if operational_score is not None and operational_score >= 0.30:
            actions.append({
                'action': 'Overdue inspections detected — reassign or reschedule field visits.',
                'priority': 'Routine',
                'source': 'operational_risk',
            })

        if not actions:
            actions.append({
                'action': 'No risk triggers — continue routine monitoring cadence.',
                'priority': 'Routine',
                'source': 'baseline',
            })
        return actions


class HQIntelligenceService:
    """
    State HQ Cross-District AI Intelligence (plan §5 Week 9): state-wide
    aggregation, district risk matrix/heatmap, executive briefing and
    inspector performance analytics.
    """

    @classmethod
    def overview(cls, district=None):
        """
        Command metrics (plan §5 Week 9):
        Total / Active / High-Risk Projects; AI-Flagged Structural & NDT
        Anomalies; Critical Subsurface / Digital Eye Issues; Active
        Inspections & Pending Approvals; Open NCRs & Overdue Corrective
        Actions — optionally restricted to one district.
        """
        from apps.approvals.models import ApprovalRequest
        from apps.compliance.models import NonConformanceReport, CorrectiveActionPlan
        from apps.digital_eye.models import GPRAnomaly
        from apps.evidence.models import CorrelationFinding
        from apps.inspections.models import Inspection
        from apps.projects.models import Project

        projects = Project.objects.all()
        if district is not None:
            projects = projects.filter(district=district)

        now = timezone.now()
        project_filter = Q(project__in=projects)
        findings = CorrelationFinding.objects.filter(project__in=projects)

        high_risk_projects = set(
            findings.filter(risk_level__in=['high', 'critical']).exclude(status='rejected')
            .values_list('project_id', flat=True)
        ) | set(
            NonConformanceReport.objects.filter(
                project__in=projects, severity='Critical',
            ).exclude(status='Closed').values_list('project_id', flat=True)
        )

        return {
            'scope': {'district_id': str(district.id), 'district_name': district.name} if district else {'scope': 'state'},
            'projects': {
                'total': projects.count(),
                'active': projects.filter(status='ACTIVE').count(),
                'high_risk': len(high_risk_projects),
            },
            'ai_findings': {
                'pending_review': findings.filter(status='pending_review').count(),
                'accepted': findings.filter(status='accepted').count(),
                'structural_high_or_critical': findings.filter(
                    risk_level__in=['high', 'critical'],
                ).exclude(status='rejected').count(),
                'by_level': {
                    level: findings.filter(risk_level=level).count()
                    for level in ('info', 'low', 'medium', 'high', 'critical')
                },
            },
            'digital_eye': {
                'gpr_anomalies': GPRAnomaly.objects.filter(survey__project__in=projects).count(),
                'critical_gpr': GPRAnomaly.objects.filter(
                    survey__project__in=projects, severity='critical',
                ).count(),
            },
            'inspections': {
                'active': Inspection.objects.filter(
                    project__in=projects, status__in=['REQUESTED', 'SCHEDULED', 'IN_PROGRESS'],
                ).count(),
                'pending_approvals': ApprovalRequest.objects.filter(
                    project__in=projects,
                ).exclude(status__in=['APPROVED', 'REJECTED', 'approved', 'rejected']).count(),
            },
            'compliance': {
                'open_ncrs': NonConformanceReport.objects.filter(
                    project__in=projects,
                ).exclude(status='Closed').count(),
                'overdue_corrective_actions': CorrectiveActionPlan.objects.filter(
                    project__in=projects,
                ).exclude(status='closed').filter(due_date__lt=now.date()).count(),
            },
            'computed_at': now.isoformat(),
        }

    @classmethod
    def district_matrix(cls):
        """
        District Risk Heatmap queries (plan §5 Week 9): per-district risk
        aggregates + drill-down counts. Districts without data show zeros —
        not fabricated values.
        """
        from apps.government.models import District

        rows = []
        for district in District.objects.filter(is_active=True).prefetch_related('projects'):
            overview = cls.overview(district=district)
            # Weighted district risk: worst project risk signal across the
            # district's projects.
            scores = [
                s for s in (
                    _worst_project_risk(p) for p in district.projects.all()
                ) if s is not None
            ]
            district_score = max(scores) if scores else None
            rows.append({
                'district_id': str(district.id),
                'district_name': district.name,
                'district_code': district.code,
                'project_count': overview['projects']['total'],
                'active_projects': overview['projects']['active'],
                'high_risk_projects': overview['projects']['high_risk'],
                'risk_score': round(district_score, 3) if district_score is not None else None,
                'risk_level': level_for_score(district_score),
                'open_ncrs': overview['compliance']['open_ncrs'],
                'pending_ai_findings': overview['ai_findings']['pending_review'],
                'critical_anomalies': overview['ai_findings']['structural_high_or_critical'],
            })
        return {
            'districts': rows,
            'computed_at': timezone.now().isoformat(),
        }

    @classmethod
    def inspector_analytics(cls):
        """
        Inspector performance & turnaround analytics (plan §5 Week 9) —
        computed from real Inspection timestamps: assigned -> completed
        turnaround, completion counts, outcome distribution.
        """
        from apps.inspections.models import Inspection
        from django.contrib.auth import get_user_model
        User = get_user_model()

        now = timezone.now()
        rows = []
        inspectors = User.objects.filter(
            assigned_inspections__isnull=False,
        ).distinct()
        for inspector in inspectors:
            inspections = Inspection.objects.filter(inspector=inspector)
            completed = inspections.filter(status='COMPLETED')
            turnarounds = [
                (i.completed_date - i.scheduled_date).total_seconds() / 86400.0
                for i in completed
                if i.completed_date and i.scheduled_date
            ]
            avg_turnaround = (
                round(sum(turnarounds) / len(turnarounds), 1) if turnarounds else None
            )
            rows.append({
                'inspector_id': str(inspector.id),
                'inspector_name': inspector.get_full_name() or inspector.email,
                'total_inspections': inspections.count(),
                'completed': completed.count(),
                'in_progress': inspections.filter(status='IN_PROGRESS').count(),
                'overdue': inspections.filter(
                    status__in=['REQUESTED', 'SCHEDULED'], scheduled_date__lt=now,
                ).count(),
                'failed': inspections.filter(outcome='FAILED').count(),
                'avg_turnaround_days': avg_turnaround,
            })
        rows.sort(key=lambda r: (-(r['completed'] or 0), r['inspector_name']))
        return {
            'inspectors': rows,
            'computed_at': now.isoformat(),
        }

    @classmethod
    def executive_briefing(cls, district=None):
        """
        AI-Assisted Executive Briefing (plan §5 Week 9): real aggregates with
        optional LLM narrative. Falls back to the deterministic briefing when
        no AI provider is available.
        """
        overview = cls.overview(district=district)
        briefing = {
            'overview': overview,
            'narrative': cls._deterministic_narrative(overview),
            'narrative_source': 'deterministic',
        }
        from apps.common.ai_service import AIService, AIServiceError, AIProviderUnavailable
        try:
            data = AIService.generate_structured_json(
                "You are the Nexucon HQ Command AI preparing an executive briefing for the Lagos State "
                "construction oversight directorate. Using ONLY the real metrics below, write 3-5 sentences "
                "summarising the portfolio risk position and the most urgent actions. Do not invent numbers, "
                "projects or events. Return JSON: {\"briefing\": \"...\"}.\n\n"
                f"Scope: {overview['scope']}\nMetrics: {overview}"
            )
            if isinstance(data, dict) and data.get('briefing'):
                briefing['narrative'] = str(data['briefing'])
                briefing['narrative_source'] = 'llm'
        except (AIServiceError, AIProviderUnavailable, Exception) as e:  # noqa: BLE001
            logger.info("LLM briefing unavailable (%s) — deterministic briefing only.", e)
        return briefing

    @staticmethod
    def _deterministic_narrative(overview):
        p = overview['projects']
        f = overview['ai_findings']
        c = overview['compliance']
        i = overview['inspections']
        d = overview['digital_eye']
        return (
            f"Portfolio: {p['total']} project(s) registered, {p['active']} active, {p['high_risk']} high-risk. "
            f"AI evidence layer: {f['pending_review']} finding(s) awaiting human review, "
            f"{f['structural_high_or_critical']} high/critical structural or NDT anomalies. "
            f"Digital Eye: {d['gpr_anomalies']} GPR anomalies ({d['critical_gpr']} critical). "
            f"{i['active']} inspection(s) in flight, {i['pending_approvals']} approval request(s) pending. "
            f"Compliance: {c['open_ncrs']} open NCR(s), {c['overdue_corrective_actions']} overdue corrective action(s). "
            "All figures are live database aggregates; AI findings are decision-support pending qualified human review."
        )


def _worst_project_risk(project):
    """Worst risk signal for a project from correlation findings / NCRs."""
    from apps.evidence.models import CorrelationFinding
    scores = [
        f.risk_score for f in CorrelationFinding.objects.filter(
            project=project,
        ).exclude(status='rejected').exclude(risk_score=None)
    ]
    return max(scores) if scores else None
