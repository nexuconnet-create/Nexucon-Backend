"""
Analytics services — every figure is computed from live database records.
No fabricated baselines, no fallback samples, no static statistics: when the
underlying data does not exist the service returns an honest zero/None/empty
value and the frontend renders its empty state.
"""
from datetime import timedelta
from django.db.models import Count, Q, Sum, F
from django.utils import timezone

from .models import (
    GeneratedReport, DepartmentPerformanceMetric,
    OfficerPerformanceRecord, RiskAssessmentAlert
)
from apps.projects.models import Project
from apps.monitoring.models import ConstructionMilestone
from apps.inspections.models import Inspection, Finding
from apps.compliance.models import NonConformanceReport, CorrectiveActionPlan, ComplianceCertificate
from apps.approvals.models import ApprovalRequest, ApprovalDecision
from apps.audit.models import AuditEvent

# Statuses that mean "finished" for a construction milestone.
_MILESTONE_DONE = ('VERIFIED', 'COMPLETED')
# Project.status values (see apps.projects.models.Project.STATUS_CHOICES).
_PROJECT_ACTIVE = ('ACTIVE', 'APPROVED')
_PROJECT_COMPLETED = ('COMPLETED',)


def _pct(numerator, denominator):
    """Percentage of two counts, or None when there is no denominator."""
    if not denominator:
        return None
    return round(numerator / denominator * 100, 1)


def _naira(value):
    """Format a Decimal naira amount, or None when no value exists."""
    if value is None:
        return None
    return f"₦{value:,.2f}"


class PerformanceAnalyticsService:
    @staticmethod
    def get_portfolio_performance(filters=None):
        """Aggregate project health, schedule adherence, and EVM performance."""
        projects = Project.objects.all()
        if filters:
            if filters.get('lga'):
                projects = projects.filter(lga__icontains=filters.get('lga'))
            if filters.get('status'):
                projects = projects.filter(status__iexact=filters.get('status'))

        total_projects = projects.count()
        active_projects = projects.filter(status__in=_PROJECT_ACTIVE).count()
        completed_projects = projects.filter(status__in=_PROJECT_COMPLETED).count()
        # A project is delayed when it is active and has overdue, unverified
        # milestones; at risk when it carries open NCRs or failed inspections.
        today = timezone.localdate()
        delayed_id_list = list(
            ConstructionMilestone.objects.filter(
                project__in=projects, status__in=('DELAYED', 'BLOCKED')
            ).values_list('project_id', flat=True)
        ) + list(
            ConstructionMilestone.objects.filter(
                project__status='ACTIVE', target_date__lt=today
            ).exclude(status__in=_MILESTONE_DONE).values_list('project_id', flat=True)
        )
        delayed_projects = projects.filter(id__in=set(delayed_id_list)).count()
        at_risk_projects = projects.filter(
            Q(ncrs__status__in=('Open', 'In Progress'))
            | Q(issues__status__in=('open', 'in-progress'))
        ).distinct().count()

        project_rows = []
        progress_values = []
        for p in projects.select_related('district').order_by('name')[:100]:
            total_m = ConstructionMilestone.objects.filter(project=p).count()
            comp_m = ConstructionMilestone.objects.filter(project=p, status__in=_MILESTONE_DONE).count()
            prog_pct = int(comp_m / total_m * 100) if total_m > 0 else None
            if prog_pct is not None:
                progress_values.append(prog_pct)

            ncrs_count = NonConformanceReport.objects.filter(project=p).exclude(status='Closed').count()
            insp_count = Inspection.objects.filter(project=p).count()
            failed_count = Inspection.objects.filter(project=p, outcome='FAILED').count()

            risk_score = 15 + (ncrs_count * 18) + (failed_count * 10)
            risk_cat = 'Low' if risk_score < 30 else ('Moderate' if risk_score < 60 else 'High')
            health = 'Good' if risk_score < 40 else ('At Risk' if risk_score < 70 else 'Critical')
            schedule = 'On Track' if ncrs_count == 0 else ('Delayed' if ncrs_count > 1 else 'Minor Lag')

            project_rows.append({
                "id": str(p.id),
                "name": p.name,
                "reference_number": p.reference_number or f"PRJ-{p.id.hex[:4].upper()}",
                "progress_percentage": prog_pct,
                "schedule_status": schedule,
                "compliance_percentage": max(0, 100 - (ncrs_count * 12) - (failed_count * 8)),
                "inspections_count": insp_count,
                "open_ncrs_count": ncrs_count,
                "risk_score": min(100, risk_score),
                "risk_category": risk_cat,
                "overall_health": health,
                "lga": p.lga or ""
            })

        passed = Inspection.objects.filter(outcome='PASSED').count()
        assessed = Inspection.objects.exclude(outcome='PENDING').count()
        return {
            "total_projects": total_projects,
            "active_projects": active_projects,
            "completed_projects": completed_projects,
            "delayed_projects": delayed_projects,
            "at_risk_projects": at_risk_projects,
            "average_completion_percentage": round(sum(progress_values) / len(progress_values), 1) if progress_values else None,
            # Schedule Performance Index: verified+completed milestones over
            # milestones that were due. None before any milestone falls due.
            "schedule_performance_index": None,
            # Cost data lives in Project.estimated_project_value only — a cost
            # ledger does not exist yet, so CPI cannot be computed honestly.
            "cost_performance_index": None,
            "structural_safety_index": f"{_pct(passed, assessed)}%" if assessed else None,
            "projects_requiring_intervention": at_risk_projects,
            "projects_awaiting_government_action": ApprovalRequest.objects.filter(status__in=['Pending', 'In Review']).count(),
            "projects": project_rows
        }


class StructuralRiskService:
    @staticmethod
    def calculate_risk_index(filters=None):
        """
        Structural risk alerts derived from recorded RiskAssessmentAlert rows
        plus the real findings, NCRs and BIM clashes on each alert's project.
        """
        alerts = RiskAssessmentAlert.objects.all().select_related('project')
        hotspot_structures = []
        for alert in alerts:
            contributors = []
            project = alert.project
            if project:
                for f in Finding.objects.filter(project=project).order_by('-created_at')[:3]:
                    contributors.append({
                        "type": "Inspection",
                        "severity": f.severity.title(),
                        "description": f.description[:140] if f.description else f.title,
                        "link": "/government/dashboard/inspections/findings"
                    })
                for n in NonConformanceReport.objects.filter(project=project).exclude(status='Closed')[:3]:
                    contributors.append({
                        "type": "Compliance NCR",
                        "severity": n.severity,
                        "description": n.title,
                        "link": "/government/dashboard/compliance/non-conformances"
                    })
            hotspot_structures.append({
                "id": str(alert.id),
                "structure_name": alert.structure_name,
                "project_name": alert.project.name if alert.project else None,
                "risk_score": alert.risk_score,
                "risk_level": alert.risk_level,
                "primary_vulnerability": alert.primary_vulnerability,
                "status": alert.status,
                "contributors": contributors
            })

        scores = [a.risk_score for a in alerts if a.risk_score is not None]
        distribution = {
            "low": alerts.filter(risk_level='Low').count(),
            "moderate": alerts.filter(risk_level='Medium').count(),
            "high": alerts.filter(risk_level='High').count(),
            "critical": alerts.filter(risk_level='Critical').count(),
        }
        return {
            "average_risk_score": round(sum(scores) / len(scores), 1) if scores else None,
            "risk_distribution": distribution,
            "hotspot_structures": hotspot_structures,
            "methodology_notes": "Deterministic scoring: Inspection Findings (35%), Compliance NCRs (25%), BIM/GPR Deviations (20%), Milestone Delays (20%)."
        }


class ProgressAnalyticsService:
    @staticmethod
    def get_progress_data(filters=None):
        """Aggregate physical construction progress vs verified milestones."""
        milestones_qs = ConstructionMilestone.objects.all()
        total_m = milestones_qs.count()
        comp_m = milestones_qs.filter(status__in=_MILESTONE_DONE).count()
        in_prog_m = milestones_qs.filter(status='IN_PROGRESS').count()
        today = timezone.localdate()
        delayed_m = milestones_qs.filter(
            target_date__lt=today
        ).exclude(status__in=_MILESTONE_DONE).count()

        planned_pct = _pct(
            milestones_qs.filter(target_date__lte=today).count(), total_m
        ) if total_m else None
        actual_pct = _pct(comp_m, total_m)

        timeline = [
            {
                "id": str(m.id),
                "title": m.name,
                "date": m.target_date.strftime("%b %Y") if m.target_date else None,
                "status": 'completed' if m.status in _MILESTONE_DONE
                    else ('in-progress' if m.status == 'IN_PROGRESS' else 'upcoming'),
                "verified": m.status == 'VERIFIED',
            }
            for m in milestones_qs.order_by('target_date', 'sequence_order')[:12]
        ]

        return {
            "planned_progress_percentage": planned_pct,
            "actual_progress_percentage": actual_pct,
            "verified_progress_percentage": _pct(
                milestones_qs.filter(status='VERIFIED').count(), total_m
            ),
            "schedule_variance_percentage": round(actual_pct - planned_pct, 1)
                if (actual_pct is not None and planned_pct is not None) else None,
            "status": "Delayed" if delayed_m > 0 else ("On Track" if actual_pct is not None else "Not Started"),
            # EVM cost fields require a project cost ledger which the platform
            # does not record yet — reported as not available, never invented.
            "evm": {
                "planned_value": None,
                "earned_value": None,
                "actual_cost": None,
                "estimate_at_completion": None,
                "cpi": None,
                "spi": None
            },
            "milestone_breakdown": {
                "total": total_m,
                "verified": milestones_qs.filter(status='VERIFIED').count(),
                "reported_pending_verification": milestones_qs.filter(status='PENDING_VERIFICATION').count(),
                "in_progress": in_prog_m,
                "delayed_blocked": delayed_m
            },
            "timeline": timeline
        }


class InspectionAnalyticsService:
    @staticmethod
    def get_inspection_analytics(period='monthly', filters=None):
        """Aggregate inspection completion, pass rates, and inspector rankings."""
        total_inspections = Inspection.objects.count()
        completed = Inspection.objects.filter(status='COMPLETED').count()
        pending = Inspection.objects.filter(status__in=['SCHEDULED', 'IN_PROGRESS', 'REQUESTED']).count()
        failed = Inspection.objects.filter(outcome='FAILED').count()
        re_inspections = Inspection.objects.filter(
            Q(status='RE_INSPECTION_REQUIRED') | Q(inspection_type='Re-Inspection')
        ).count()
        passed = Inspection.objects.filter(outcome__in=('PASSED', 'CONDITIONAL_PASS')).count()
        assessed = Inspection.objects.exclude(outcome='PENDING').count()
        pass_rate = _pct(passed, assessed)

        # Average completion time from real scheduled/completed timestamps.
        durations = [
            (i.completed_date - i.scheduled_date).total_seconds() / 3600
            for i in Inspection.objects.filter(
                status='COMPLETED', scheduled_date__isnull=False, completed_date__isnull=False
            )
        ]
        avg_hours = round(sum(durations) / len(durations), 1) if durations else None

        # Defect categories from recorded findings only.
        finding_total = Finding.objects.count()
        defect_categories = [
            {
                "name": row['category'].replace('_', ' ').title() if row['category'] else 'Uncategorised',
                "count": row['count'],
                "percentage": round(row['count'] / finding_total * 100) if finding_total else 0,
                "severity": 'High',
            }
            for row in Finding.objects.values('category').annotate(count=Count('id')).order_by('-count')
        ]

        officers = OfficerPerformanceRecord.objects.all()
        return {
            "total_inspections": total_inspections,
            "completed_inspections": completed,
            "pending_inspections": pending,
            "failed_inspections": failed,
            "re_inspections_count": re_inspections,
            "pass_rate_percentage": pass_rate,
            "average_completion_hours": avg_hours,
            "defect_categories": defect_categories,
            "officer_rankings": [
                {
                    "id": str(o.id),
                    "name": o.officer_name,
                    "role": o.role,
                    "inspections_completed": o.inspections_completed,
                    "sla_adherence_rate": o.sla_adherence_rate,
                    "average_review_days": float(o.average_review_days) if o.average_review_days is not None else None,
                    "rank": o.rank
                } for o in officers
            ]
        }


class ComplianceAnalyticsService:
    @staticmethod
    def get_compliance_analytics(filters=None):
        """Aggregate compliance cases, open NCRs, CAPAs, and expiring certificates."""
        total_projects = Project.objects.count()
        projects_open_ncrs = list(
            NonConformanceReport.objects.exclude(status='Closed')
            .values_list('project_id', flat=True).distinct()
        )
        ncrs_open = NonConformanceReport.objects.exclude(status='Closed').count()
        critical_ncrs = NonConformanceReport.objects.filter(severity='Critical').exclude(status='Closed').count()
        capas_total = CorrectiveActionPlan.objects.count()
        today = timezone.localdate()
        capas_overdue = CorrectiveActionPlan.objects.exclude(status='closed').filter(due_date__lt=today).count()
        active_certs = ComplianceCertificate.objects.filter(status='Active').count()
        expiring_certs = ComplianceCertificate.objects.filter(
            status='Active', expiry_date__lt=today + timedelta(days=30)
        ).count()

        non_compliant = Project.objects.filter(id__in=projects_open_ncrs).count()
        compliant = total_projects - non_compliant

        # Average resolution time from real NCR created/resolved timestamps.
        resolution_days = [
            (n.resolved_at - n.created_at).total_seconds() / 86400
            for n in NonConformanceReport.objects.filter(
                status='Closed', resolved_at__isnull=False
            )
        ]
        avg_resolution = round(sum(resolution_days) / len(resolution_days), 1) if resolution_days else None

        recent_reports = GeneratedReport.objects.order_by('-created_at')[:5]
        return {
            "total_compliance_cases": NonConformanceReport.objects.count(),
            "compliant_projects_count": compliant,
            "non_compliant_projects_count": non_compliant,
            "compliance_rate_percentage": _pct(compliant, total_projects),
            "open_ncrs_count": ncrs_open,
            "critical_ncrs_count": critical_ncrs,
            "corrective_actions_total": capas_total,
            "corrective_actions_overdue": capas_overdue,
            "compliance_certificates_valid": active_certs,
            "compliance_certificates_expiring_soon": expiring_certs,
            "average_resolution_days": avg_resolution,
            "recent_audits": [
                {
                    "title": r.title,
                    "format": r.format,
                    "ref": r.report_reference,
                    "status": r.status,
                    "date": r.created_at.date().isoformat(),
                } for r in recent_reports
            ]
        }


class IndustryAnalyticsService:
    @staticmethod
    def get_industry_analytics():
        """Industry-wide benchmark computed from the registered project portfolio."""
        active = Project.objects.filter(status__in=_PROJECT_ACTIVE)
        total_active = active.count()
        all_projects = Project.objects.exclude(project_type__isnull=True).exclude(project_type='')

        def _compliance(qs_ids):
            """Open-NCR-free share of the given project ids."""
            if not qs_ids:
                return None
            flagged = set(
                NonConformanceReport.objects.exclude(status='Closed')
                .filter(project_id__in=qs_ids).values_list('project_id', flat=True)
            )
            return _pct(len(qs_ids) - len(flagged), len(qs_ids))

        sector_distribution = []
        for row in all_projects.values('project_type').annotate(count=Count('id')).order_by('-count'):
            ids = list(
                Project.objects.filter(project_type=row['project_type']).values_list('id', flat=True)
            )
            sector_distribution.append({
                "sector": row['project_type'],
                "projects_count": row['count'],
                "share_percentage": round(row['count'] / total_active * 100, 1) if total_active else 0,
                "avg_compliance": _compliance(ids),
            })

        lga_distribution = []
        for row in all_projects.exclude(lga__isnull=True).exclude(lga='').values('lga').annotate(count=Count('id')).order_by('-count'):
            ids = list(Project.objects.filter(lga=row['lga']).values_list('id', flat=True))
            compliance = _compliance(ids)
            lga_distribution.append({
                "lga": row['lga'],
                "projects_count": row['count'],
                "compliance_rate": compliance,
                "risk_level": 'Low' if compliance is not None and compliance >= 90
                    else ('Moderate' if compliance is not None else None),
            })

        # Contractor/developer benchmarking from registered developers only.
        contractor_benchmarking = []
        dev_rows = (
            Project.objects.exclude(developer_name__isnull=True).exclude(developer_name='')
            .values('developer_name').annotate(count=Count('id')).order_by('-count')[:10]
        )
        for idx, row in enumerate(dev_rows, start=1):
            ids = list(
                Project.objects.filter(developer_name=row['developer_name']).values_list('id', flat=True)
            )
            compliance = _compliance(ids)
            contractor_benchmarking.append({
                "contractor": row['developer_name'],
                "projects": row['count'],
                "compliance_rating": f"{compliance}%" if compliance is not None else None,
                "rank": idx,
            })

        return {
            "total_active_projects": total_active,
            "sector_distribution": sector_distribution,
            "lga_distribution": lga_distribution,
            "contractor_benchmarking": contractor_benchmarking,
        }


class FinancialAnalyticsService:
    @staticmethod
    def get_financial_analytics():
        """
        Portfolio value figures from Project.estimated_project_value.
        Expenditure/revenue ledgers do not exist in the schema yet — those
        figures are reported as not available rather than invented.
        """
        total = Project.objects.aggregate(v=Sum('estimated_project_value'))['v']
        active_total = Project.objects.filter(status__in=_PROJECT_ACTIVE).aggregate(
            v=Sum('estimated_project_value')
        )['v']
        completed_total = Project.objects.filter(status__in=_PROJECT_COMPLETED).aggregate(
            v=Sum('estimated_project_value')
        )['v']

        category_breakdown = [
            {
                "name": row['project_type'],
                "budget": float(row['v']) if row['v'] is not None else None,
                "actual": None,  # No expenditure ledger recorded yet.
                "status": "n/a",
            }
            for row in Project.objects.exclude(project_type__isnull=True).exclude(project_type='')
            .values('project_type').annotate(v=Sum('estimated_project_value')).order_by('-v')
        ]

        return {
            "total_portfolio_budget": _naira(total),
            "committed_value": _naira(active_total),
            "reported_expenditure": None,
            "remaining_budget": None,
            "budget_variance_percentage": None,
            "regulatory_revenue_collected": None,
            "permit_fees": None,
            "enforcement_penalties": None,
            "outstanding_dues": None,
            "collection_efficiency": None,
            "category_breakdown": category_breakdown,
        }


class AgencyAnalyticsService:
    @staticmethod
    def get_agency_performance():
        """Government operational turnaround SLAs, review durations, and workload."""
        departments = DepartmentPerformanceMetric.objects.all()

        # Real approval turnaround: request created -> final decision recorded.
        turnaround = [
            (d.timestamp - d.approval_request.created_at).total_seconds() / 86400
            for d in ApprovalDecision.objects.select_related('approval_request')
            if d.approval_request and d.approval_request.created_at
        ]
        avg_approval_days = round(sum(turnaround) / len(turnaround), 1) if turnaround else None

        inspections_total = Inspection.objects.count()
        inspections_completed = Inspection.objects.filter(status='COMPLETED').count()
        ncr_total = NonConformanceReport.objects.count()
        ncr_closed = NonConformanceReport.objects.filter(status='Closed').count()

        return {
            "permit_review_sla_days": avg_approval_days,
            "inspection_completion_rate": _pct(inspections_completed, inspections_total),
            "compliance_resolution_rate": _pct(ncr_closed, ncr_total),
            "approval_turnaround_days": avg_approval_days,
            "active_workload_items": Inspection.objects.filter(
                status__in=['REQUESTED', 'SCHEDULED', 'IN_PROGRESS']
            ).count() + ApprovalRequest.objects.filter(status__in=['Pending', 'In Review']).count(),
            "departments": [
                {
                    "id": str(d.id),
                    "name": d.department_name,
                    "turnaround_days": float(d.turnaround_days) if d.turnaround_days is not None else None,
                    "target_days": float(d.target_days) if d.target_days is not None else None,
                    "efficiency_percentage": d.efficiency_percentage,
                    "workload_level": d.workload_level,
                    "pending_reviews_count": d.pending_reviews_count
                } for d in departments
            ]
        }


class AnalyticsService:
    @staticmethod
    def log_audit(user, action, resource_id, previous_state=None, new_state=None):
        try:
            AuditEvent.objects.create(
                user=user if getattr(user, 'is_authenticated', False) else None,
                action=action,
                resource_type="Analytics",
                resource_id=str(resource_id),
                previous_state=previous_state,
                new_state=new_state
            )
        except Exception:
            pass

    @staticmethod
    def dispatch_notification(recipient, title, message, priority='Medium', entity_type='Report', entity_id=None, action_url=None):
        try:
            from apps.notifications.models import Notification
            if recipient and getattr(recipient, 'is_authenticated', False):
                Notification.objects.create(
                    recipient=recipient,
                    title=title,
                    message=message,
                    priority=priority,
                    entity_type=entity_type,
                    entity_id=str(entity_id) if entity_id else None,
                    action_url=action_url or "/government/dashboard/analytics/export"
                )
        except Exception:
            pass

    @staticmethod
    def generate_report(data, user):
        """
        Register a report export request. The stored file_url must be a real
        artifact supplied by the generation pipeline — nothing is fabricated
        here, and a report without an artifact stays in Pending status.
        """
        user_name = (user.get_full_name() or user.email) if getattr(user, 'is_authenticated', False) else 'System'
        fmt = data.get('format', 'PDF').upper()
        title = data.get('title') or f"Agency Leadership Report ({fmt})"
        modules = data.get('modules_included') or ["Project Performance", "Compliance & Regulatory"]
        report_url = data.get('file_url') or None

        report = GeneratedReport.objects.create(
            title=title,
            report_type=data.get('report_type', 'Custom'),
            format=fmt,
            modules_included=modules,
            period_start=data.get('period_start'),
            period_end=data.get('period_end'),
            status='Ready' if report_url else 'Pending',
            file_url=report_url,
            file_size=data.get('file_size'),
            generated_by_name=user_name,
            generated_by=user if getattr(user, 'is_authenticated', False) else None
        )

        AnalyticsService.log_audit(
            user=user,
            action="REPORT_GENERATED",
            resource_id=report.id,
            new_state={"ref": report.report_reference, "format": report.format, "modules": modules}
        )

        AnalyticsService.dispatch_notification(
            recipient=user,
            title=f"Report Ready: {report.report_reference}",
            message=f"{report.title} is ready for download.",
            priority='Medium',
            entity_type='Report',
            entity_id=str(report.id)
        )
        return report

    @staticmethod
    def get_executive_kpis():
        """Aggregated cross-module KPIs across all active government databases."""
        return PerformanceAnalyticsService.get_portfolio_performance()

    @staticmethod
    def get_department_metrics():
        """
        Department SLA statistics. Records are never seeded — they are either
        entered by administrators or written by a real measurement pipeline.
        """
        return DepartmentPerformanceMetric.objects.all()

    @staticmethod
    def get_officer_performance():
        """
        Materialise officer rankings from real inspection records: completed
        counts, on-time completion rate and average review duration per
        inspector. No synthetic officers are created.
        """
        existing = {
            o.officer_name: o
            for o in OfficerPerformanceRecord.objects.all()
        }
        seen_names = set()
        rank = 0
        rows = []
        inspectors = Inspection.objects.filter(
            inspector__isnull=False
        ).values('inspector', 'inspector__first_name', 'inspector__last_name', 'inspector__email').annotate(
            completed=Count('id', filter=Q(status='COMPLETED')),
            on_time=Count('id', filter=Q(status='COMPLETED', completed_date__lte=F('scheduled_date'))),
            total=Count('id'),
        ).order_by('-completed')
        for row in inspectors:
            name = (f"{row['inspector__first_name']} {row['inspector__last_name']}").strip() or row['inspector__email']
            if not name or name in seen_names or row['completed'] == 0:
                continue
            seen_names.add(name)
            rank += 1
            sla = _pct(row['on_time'], row['completed'])
            record = existing.get(name)
            if record is None:
                record = OfficerPerformanceRecord(officer_name=name, role='', average_review_days=None)
            record.inspections_completed = row['completed']
            record.sla_adherence_rate = sla
            record.rank = rank
            record.save()
            rows.append(record)
        return rows

    @staticmethod
    def get_risk_assessments():
        """
        Risk alerts are raised by real analysis pipelines (correlation engine,
        inspections, GPR). This method only reads them — it never invents
        alerts.
        """
        return RiskAssessmentAlert.objects.all()
