"""
Inspector Web Dashboard API Views.
Provides scoped operational dashboard data for authenticated Inspectors,
consuming the shared projects, inspections, evidence, and compliance models.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.utils import timezone
from datetime import timedelta

from common.permissions import scoped_projects, get_profile, user_role_name
from apps.projects.models import Project
from apps.inspections.models import Inspection, Finding, StopWorkOrder
from apps.evidence.models import EvidenceRecord
from apps.audit.models import AuditEvent


class InspectorDashboardView(APIView):
    """
    GET /api/v1/government/inspectors/me/dashboard/
    Aggregates operational workspace data for the authenticated inspector,
    strictly scoped by project assignment and district jurisdiction.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        profile = get_profile(user)
        projects = scoped_projects(user)

        # Scoped inspector profile details
        full_name = f"{user.first_name} {user.last_name}".strip() or user.email.split('@')[0].capitalize()
        badge_number = f"LAG-INS-{str(user.id).replace('-', '')[:4].upper()}"
        agency_name = profile.agency.name if (profile and profile.agency) else "Lagos State Building Control Agency (LASBCA)"
        district_name = profile.district.name if (profile and profile.district) else "Lekki-Epe Zonal Directorate"
        role_name = (profile.role.name if (profile and profile.role) else '') or "Field Building Inspector"

        # KPIs
        assigned_projects_count = projects.count()

        open_inspections = Inspection.objects.filter(
            project__in=projects
        ).exclude(status__in=['COMPLETED', 'CANCELLED'])

        # Scoped upcoming inspections assigned to this inspector or open in district
        upcoming_inspections_count = open_inspections.filter(
            status__in=['REQUESTED', 'SCHEDULED', 'IN_PROGRESS']
        ).count()

        open_findings_qs = Finding.objects.filter(
            project__in=projects,
            is_resolved=False
        )
        open_findings_count = open_findings_qs.count()

        active_swo_count = StopWorkOrder.objects.filter(
            project__in=projects,
            status='ACTIVE'
        ).count()
        critical_findings_count = open_findings_qs.filter(severity__in=['CRITICAL', 'HIGH']).count()
        compliance_issues_count = critical_findings_count + active_swo_count

        evidence_qs = EvidenceRecord.objects.filter(project__in=projects)
        total_evidence_count = evidence_qs.count()
        pending_evidence_count = open_inspections.filter(photos_and_evidence__isnull=False).count()

        # Today's Inspections
        now = timezone.now()
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today_end = today_start + timedelta(days=1)

        today_inspections_qs = Inspection.objects.filter(
            project__in=projects,
            scheduled_date__gte=today_start,
            scheduled_date__lt=today_end
        ).select_related('project').order_by('scheduled_date')

        # Fallback to nearest scheduled/in-progress inspections if none scheduled strictly today
        if not today_inspections_qs.exists():
            today_inspections_qs = open_inspections.select_related('project').order_by('scheduled_date', '-created_at')[:5]

        today_schedule = []
        for insp in today_inspections_qs:
            today_schedule.append({
                "id": str(insp.id),
                "inspection_reference": insp.inspection_reference,
                "project_id": str(insp.project.id),
                "project_name": insp.project.name,
                "project_reference": insp.project.reference_number,
                "location": insp.project.site_address or f"{insp.project.lga or ''}, {insp.project.state or ''}".strip(', '),
                "inspection_type": insp.inspection_type,
                "status": insp.status,
                "priority": insp.priority,
                "scheduled_date": insp.scheduled_date.isoformat() if insp.scheduled_date else None,
                "gps_verified": insp.gps_verified,
            })

        # Assigned Projects Summary (Top 6)
        assigned_projects_list = []
        for p in projects.order_by('-updated_at')[:6]:
            next_insp = p.inspections.filter(status__in=['SCHEDULED', 'REQUESTED']).order_by('scheduled_date').first()
            p_findings_count = p.findings.filter(is_resolved=False).count()
            assigned_projects_list.append({
                "id": str(p.id),
                "name": p.name,
                "reference_number": p.reference_number,
                "location": p.site_address or f"{p.lga or ''}, {p.state or ''}".strip(', '),
                "current_phase": p.status,
                "compliance_status": "FLAGGED" if p_findings_count > 0 else "COMPLIANT",
                "open_findings": p_findings_count,
                "next_inspection": next_insp.scheduled_date.isoformat() if (next_insp and next_insp.scheduled_date) else None,
                "project_type": p.project_type or "Residential"
            })

        # Critical Findings (Top 5)
        critical_findings_list = []
        for f in open_findings_qs.filter(severity__in=['CRITICAL', 'HIGH']).select_related('project').order_by('-created_at')[:5]:
            critical_findings_list.append({
                "id": str(f.id),
                "finding_reference": f.finding_reference,
                "title": f.title,
                "severity": f.severity,
                "project_id": str(f.project.id) if f.project else "",
                "project_name": f.project.name if f.project else "General Assignment",
                "category": f.category,
                "created_at": f.created_at.isoformat(),
                "status": "OPEN" if not f.is_resolved else "RESOLVED"
            })

        # Evidence Synchronization Status
        evidence_sync = {
            "uploaded": total_evidence_count,
            "pending": pending_evidence_count,
            "failed": 0,
            "last_synced_at": now.isoformat()
        }

        # Recent Activity Feed
        recent_activity = []
        recent_audits = AuditEvent.objects.filter(
            project_name__in=[p.name for p in projects[:10]]
        ).order_by('-timestamp')[:8]

        for audit in recent_audits:
            recent_activity.append({
                "id": str(audit.id),
                "event": audit.action.replace('_', ' ').title(),
                "project": audit.project_name or "Project Assignment",
                "timestamp": audit.timestamp.isoformat(),
                "actor": audit.user_name or "Field System",
                "status": "COMPLETED"
            })

        if not recent_activity:
            # Fallback recent inspections
            for insp in Inspection.objects.filter(project__in=projects).order_by('-updated_at')[:5]:
                recent_activity.append({
                    "id": str(insp.id),
                    "event": f"{insp.inspection_type} - {insp.status}",
                    "project": insp.project.name,
                    "timestamp": insp.updated_at.isoformat(),
                    "actor": insp.inspector_name or full_name,
                    "status": insp.status
                })

        return Response({
            "success": True,
            "profile": {
                "id": str(user.id),
                "email": user.email,
                "name": full_name,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "badge_number": badge_number,
                "role": role_name,
                "agency": agency_name,
                "district": district_name
            },
            "kpis": {
                "assigned_projects": assigned_projects_count,
                "upcoming_inspections": upcoming_inspections_count,
                "open_findings": open_findings_count,
                "compliance_issues": compliance_issues_count,
                "pending_evidence": pending_evidence_count
            },
            "today_schedule": today_schedule,
            "assigned_projects": assigned_projects_list,
            "critical_findings": critical_findings_list,
            "evidence_sync": evidence_sync,
            "recent_activity": recent_activity
        }, status=status.HTTP_200_OK)
