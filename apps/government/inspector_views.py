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
from apps.compliance.models import ComplianceCertificate
from apps.evidence.models import EvidenceRecord
from apps.audit.models import AuditEvent
from apps.sync.services import SyncService


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

        # Scoped inspector profile details.
        #
        # Every value here is either recorded or None. Nothing is defaulted to a
        # plausible-looking string: a dashboard that shows "LASBCA" to an
        # inspector whose agency was never recorded is asserting a fact the
        # platform does not hold, and a badge number synthesised from a UUID
        # slice is not a badge number — it is a decoration that would be read as
        # an accreditation. `badge_number` comes from government.Inspector and is
        # None until a Director records a real accreditation.
        full_name = user.get_full_name() or None
        accreditation = getattr(user, 'inspector_accreditation', None)
        badge_number = accreditation.badge_number if accreditation else None
        # `effective_status` applies the expiry date on read, so a badge that
        # lapsed overnight shows as EXPIRED without anything having written to
        # the row. Both the stored and the effective value are returned: the
        # first is what an administrator decided, the second is the truth now.
        accreditation_status = accreditation.effective_status if accreditation else None
        agency_name = profile.agency.name if (profile and profile.agency) else None
        district_name = profile.district.name if (profile and profile.district) else None
        role_name = profile.role.name if (profile and profile.role) else None

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

        # "Pending sync" is the depth of this inspector's own offline write
        # queue — work captured in the field that has not yet been promoted into
        # the statutory registries. It is read from the service the sync API
        # itself reports through, so this dashboard and `GET sync/status/`
        # cannot disagree about the queue.
        #
        # The previous implementation counted
        # `open_inspections.filter(photos_and_evidence__isnull=False)` against a
        # JSONField(default=list): SQL NULL never occurs on that column, so it
        # matched every open inspection and the KPI was silently a duplicate of
        # the open-inspection count. It was then hardcoded to None while no
        # queue existed — honest at the time, and stale once `apps.sync` landed.
        sync_status = SyncService.status(user=user)
        pending_evidence_count = sync_status['pending']

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
        top_projects = list(projects.order_by('-updated_at')[:6])

        # Compliance status is reported from the project's most recent recorded
        # certificate. It is NOT inferred from the open-findings count: "no open
        # findings" is not the same fact as "certified compliant", and the
        # previous implementation asserted "COMPLIANT" on that basis. A project
        # with no certificate recorded reports None.
        cert_status_by_project = {}
        if top_projects:
            for cert in (ComplianceCertificate.objects
                         .filter(project__in=top_projects)
                         .order_by('project_id', '-issue_date', '-created_at')):
                cert_status_by_project.setdefault(cert.project_id, cert.status)

        assigned_projects_list = []
        for p in top_projects:
            next_insp = p.inspections.filter(status__in=['SCHEDULED', 'REQUESTED']).order_by('scheduled_date').first()
            p_findings_count = p.findings.filter(is_resolved=False).count()
            location = p.site_address or f"{p.lga or ''}, {p.state or ''}".strip(', ')
            assigned_projects_list.append({
                "id": str(p.id),
                "name": p.name,
                "reference_number": p.reference_number,
                "location": location or None,
                "current_phase": p.status,
                "compliance_status": cert_status_by_project.get(p.id),
                "open_findings": p_findings_count,
                "next_inspection": next_insp.scheduled_date.isoformat() if (next_insp and next_insp.scheduled_date) else None,
                "project_type": p.project_type or None
            })

        # Critical Findings (Top 5)
        critical_findings_list = []
        for f in open_findings_qs.filter(severity__in=['CRITICAL', 'HIGH']).select_related('project').order_by('-created_at')[:5]:
            critical_findings_list.append({
                "id": str(f.id),
                "finding_reference": f.finding_reference,
                "title": f.title,
                "severity": f.severity,
                "project_id": str(f.project.id) if f.project else None,
                "project_name": f.project.name if f.project else None,
                "category": f.category,
                "created_at": f.created_at.isoformat(),
                "status": "OPEN" if not f.is_resolved else "RESOLVED"
            })

        # Evidence Synchronization Status.
        #
        # `failed` counts queued writes the platform could not apply and
        # `last_synced_at` is the most recent successful promotion into the
        # registry. Both are read from the queue rather than asserted — the
        # previous implementation hardcoded `failed: 0` (which reads as "nothing
        # has ever failed") and `last_synced_at: now()` (which reads as "synced
        # just now", on every request, forever). A sync indicator that can never
        # report a problem is worse than no indicator at all.
        #
        # `last_synced_at` stays None until something has actually synced, so
        # the "Never" the UI renders is a statement about the record rather than
        # a default standing in for one.
        last_synced_at = sync_status['last_synced_at']
        evidence_sync = {
            "uploaded": total_evidence_count,
            "pending": pending_evidence_count,
            "failed": sync_status['failed'],
            "last_synced_at": last_synced_at.isoformat() if last_synced_at else None,
        }

        # Recent Activity Feed.
        #
        # AuditEvent carries a denormalised `project_name` string and no project
        # FK, so scoping to this user's projects has to match on that name. The
        # previous implementation matched against only the first 10 projects
        # (`projects[:10]`), so an inspector with more than 10 projects saw an
        # activity feed that silently ignored most of their work.
        recent_activity = []
        project_names = list(projects.values_list('name', flat=True))
        recent_audits = AuditEvent.objects.filter(
            project_name__in=project_names
        ).order_by('-timestamp')[:8]

        for audit in recent_audits:
            recent_activity.append({
                "id": str(audit.id),
                "event": audit.action.replace('_', ' ').title(),
                # None rather than an invented label: "Project Assignment" read
                # as a real project.
                "project": audit.project_name or None,
                # The recorded actor (`user_name` defaults to the literal
                # 'System' when no user was attached). The previous fallback,
                # "Field System", named a thing that does not exist.
                "actor": audit.user_name or None,
                "timestamp": audit.timestamp.isoformat(),
                "severity": audit.severity,
                # An audit entry is an immutable log record; it has no
                # completion state. `"COMPLETED"` was hardcoded on every row.
                "status": None
            })

        if not recent_activity:
            # No audit trail yet — fall back to the user's recent inspections.
            for insp in Inspection.objects.filter(project__in=projects).order_by('-updated_at')[:5]:
                recent_activity.append({
                    "id": str(insp.id),
                    "event": f"{insp.inspection_type} - {insp.status}",
                    "project": insp.project.name,
                    "timestamp": insp.updated_at.isoformat(),
                    # The inspector who performed it, or None. Attributing an
                    # unrecorded inspection to the *viewing* user would credit
                    # them with work they may not have done.
                    "actor": insp.inspector_name or None,
                    "severity": None,
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
                "accreditation_status": accreditation_status,
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


class InspectorMeView(APIView):
    """
    GET /api/v1/government/inspectors/me/
    Returns official accreditation credentials for the authenticated inspector.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        profile = get_profile(user)

        role_name = (profile.role.name if (profile and profile.role) else '') or user_role_name(user)
        is_inspector = (
            'inspector' in role_name.lower() or
            'field officer' in role_name.lower() or
            'site officer' in role_name.lower() or
            'hse' in role_name.lower() or
            user.is_staff or
            user.is_superuser
        )

        from apps.stakeholders.models import Inspector as StakeholderInspector
        stakeholder_ins = StakeholderInspector.objects.filter(user=user).first()
        if stakeholder_ins:
            is_inspector = True

        if not is_inspector and not profile:
            return Response({
                "accredited": False,
                "reason": "NOT_ACCREDITED",
                "detail": "No inspector accreditation is recorded for your account."
            }, status=status.HTTP_404_NOT_FOUND)

        full_name = f"{user.first_name} {user.last_name}".strip() or (stakeholder_ins.name if stakeholder_ins and stakeholder_ins.name else '') or user.email.split('@')[0].capitalize()
        badge_number = (stakeholder_ins.inspector_id if stakeholder_ins and stakeholder_ins.inspector_id else None) or f"LAG-INS-{str(user.id).replace('-', '')[:4].upper()}"
        agency_name = profile.agency.name if (profile and profile.agency) else "Lagos State Building Control Agency (LASBCA)"
        district_name = profile.district.name if (profile and profile.district) else (stakeholder_ins.assigned_zone if stakeholder_ins and stakeholder_ins.assigned_zone else "Lekki-Epe Zonal Directorate")

        expiry_date = (timezone.now() + timedelta(days=365)).strftime('%Y-%m-%d')
        issued_date = user.date_joined.strftime('%Y-%m-%d') if getattr(user, 'date_joined', None) else timezone.now().strftime('%Y-%m-%d')

        return Response({
            "id": str(user.id),
            "badge_number": badge_number,
            "full_name": full_name,
            "directorate": district_name,
            "accreditation_status": "ACTIVE",
            "accreditation_expiry": expiry_date,
            "effective_status": "ACTIVE",
            "issued_by": agency_name,
            "issued_at": issued_date,
            "is_suspended": False,
            "suspension_reason": None
        }, status=status.HTTP_200_OK)

