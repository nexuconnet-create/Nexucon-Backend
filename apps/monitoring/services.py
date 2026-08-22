import math
from django.utils import timezone
from .models import DailySiteUpdate, FieldObservation, SiteIssue, ConstructionMilestone, SiteVerification
from apps.projects.models import Project
from apps.audit.models import AuditEvent
from django.db.models import Q

class MonitoringService:
    @staticmethod
    def get_project_instance(project_id):
        if not project_id:
            return Project.objects.first()
        
        import uuid
        try:
            val = uuid.UUID(str(project_id))
            p = Project.objects.filter(id=val).first()
            if p:
                return p
        except Exception:
            pass

        p = Project.objects.filter(
            Q(reference_number=str(project_id)) | 
            Q(name__icontains=str(project_id))
        ).first()

        return p or Project.objects.first()

    @staticmethod
    def get_actor_name(user, default="Site Engineer / Officer"):
        if user and getattr(user, 'is_authenticated', False):
            return user.get_full_name() or getattr(user, 'email', default)
        return default

    @staticmethod
    def log_audit(user, action, resource_id, previous_state=None, new_state=None):
        try:
            AuditEvent.objects.create(
                user=user if getattr(user, 'is_authenticated', False) else None,
                action=action,
                resource_type="SiteMonitoring",
                resource_id=str(resource_id),
                previous_state=previous_state,
                new_state=new_state
            )
        except Exception:
            pass

    @staticmethod
    def log_daily_update(data, user):
        """Create daily photo update, drone survey or progress log."""
        project_id = data.get('project_id') or data.get('project')
        project = MonitoringService.get_project_instance(project_id)
        
        progress = int(data.get('progress_percentage', 0))
        update_type = data.get('update_type', 'DAILY_PHOTO')
        author_name = data.get('reported_by_name') or MonitoringService.get_actor_name(user, "Site Supervisor")

        update = DailySiteUpdate.objects.create(
            project=project,
            update_type=update_type,
            reported_by=user if getattr(user, 'is_authenticated', False) else None,
            reported_by_name=author_name,
            contractor_notes=data.get('contractor_notes', ''),
            officer_notes=data.get('officer_notes', ''),
            photos=data.get('photos', []),
            progress_percentage=progress,
            recorded_at=timezone.now()
        )

        # Update Project progress if progress was reported
        if progress > 0 and project and hasattr(project, 'progress'):
            try:
                project.progress = max(getattr(project, 'progress', 0) or 0, progress)
                project.save()
            except Exception:
                pass

        MonitoringService.log_audit(
            user=user,
            action="DAILY_UPDATE_LOGGED",
            resource_id=update.id,
            new_state={"type": update_type, "progress": progress}
        )
        return update

    @staticmethod
    def create_observation(data, user):
        """Create/Log field observation or inspection note."""
        return MonitoringService.log_observation(data, user)

    @staticmethod
    def log_observation(data, user):
        """Log field observation or inspection note."""
        project_id = data.get('project_id') or data.get('project')
        project = MonitoringService.get_project_instance(project_id)
        author_name = data.get('observed_by_name') or MonitoringService.get_actor_name(user, "Field Inspector")

        obs = FieldObservation.objects.create(
            project=project,
            title=data.get('title', 'Field Observation'),
            description=data.get('description', ''),
            category=data.get('category', 'GENERAL'),
            status=data.get('status', 'OPEN'),
            observed_by=user if getattr(user, 'is_authenticated', False) else None,
            observed_by_name=author_name,
            photos=data.get('photos', []),
            recommended_action=data.get('recommended_action', ''),
            observed_at=timezone.now()
        )

        MonitoringService.log_audit(
            user=user,
            action="FIELD_OBSERVATION_LOGGED",
            resource_id=obs.id,
            new_state={"ref": obs.observation_reference, "category": obs.category}
        )
        return obs

    @staticmethod
    def resolve_observation(observation, notes, actor):
        """Mark field observation as resolved."""
        observation.status = 'RESOLVED'
        observation.resolution_notes = notes
        observation.resolved_at = timezone.now()
        observation.save()

        MonitoringService.log_audit(
            user=actor,
            action="FIELD_OBSERVATION_RESOLVED",
            resource_id=observation.id,
            new_state={"status": "RESOLVED", "notes": notes}
        )
        return observation

    @staticmethod
    def report_issue(data, user):
        """Report site defect, safety hazard or regulatory issue."""
        project_id = data.get('project_id') or data.get('project')
        project = MonitoringService.get_project_instance(project_id)
        author_name = data.get('reported_by_name') or MonitoringService.get_actor_name(user, "Site Inspector")

        # Safely parse due_date to avoid format exceptions
        due_date_raw = data.get('due_date')
        due_date = None
        if due_date_raw:
            try:
                cleaned = str(due_date_raw).split('T')[0].strip()
                if len(cleaned) == 10 and cleaned.count('-') == 2:
                    due_date = cleaned
            except Exception:
                due_date = None

        enforce_stop_work = bool(data.get('enforce_stop_work') or data.get('is_escalated'))

        issue = SiteIssue.objects.create(
            project=project,
            title=data.get('title', 'Site Issue'),
            description=data.get('description', ''),
            severity='CRITICAL' if enforce_stop_work else data.get('severity', 'MEDIUM'),
            status=data.get('status', 'OPEN'),
            assigned_to_name=data.get('assigned_to_name', 'Site Engineer'),
            reported_by_name=author_name,
            due_date=due_date,
            resolution_evidence=data.get('resolution_evidence', []),
            is_escalated=enforce_stop_work
        )

        # If Stop-Work Order enforcement was requested
        if enforce_stop_work and project:
            try:
                from apps.inspections.models import StopWorkOrder
                existing_swo = StopWorkOrder.objects.filter(project=project, status='ACTIVE').first()
                if not existing_swo:
                    StopWorkOrder.objects.create(
                        project=project,
                        reason=issue.description or issue.title or "Immediate site safety and building regulation breach.",
                        severity='CRITICAL',
                        issued_by_name=author_name,
                        issued_at=timezone.now(),
                        status='ACTIVE'
                    )
                project.status = 'SUSPENDED'
                project.save()
            except Exception:
                pass

        MonitoringService.log_audit(
            user=user,
            action="SITE_ISSUE_REPORTED",
            resource_id=issue.id,
            new_state={"ref": issue.issue_reference, "severity": issue.severity}
        )
        return issue

    @staticmethod
    def escalate_issue(issue, actor):
        """Escalate site issue to Directorate level."""
        issue.is_escalated = True
        issue.status = 'UNDER_REVIEW'
        issue.save()

        MonitoringService.log_audit(
            user=actor,
            action="SITE_ISSUE_ESCALATED",
            resource_id=issue.id,
            new_state={"is_escalated": True, "status": "UNDER_REVIEW"}
        )
        return issue

    @staticmethod
    def resolve_issue(issue, notes, evidence, actor):
        """Close site issue with resolution evidence."""
        issue.status = 'RESOLVED'
        issue.resolution_notes = notes
        if evidence:
            issue.resolution_evidence = evidence
        issue.save()

        MonitoringService.log_audit(
            user=actor,
            action="SITE_ISSUE_RESOLVED",
            resource_id=issue.id,
            new_state={"status": "RESOLVED", "notes": notes}
        )
        return issue

    @staticmethod
    def create_milestone(data, user):
        """Create a construction milestone schedule."""
        project_id = data.get('project_id') or data.get('project')
        project = MonitoringService.get_project_instance(project_id)

        milestone = ConstructionMilestone.objects.create(
            project=project,
            name=data.get('name', 'Milestone'),
            target_date=data.get('target_date', timezone.now().date()),
            status=data.get('status', 'UPCOMING'),
            progress_percentage=int(data.get('progress_percentage', 0) or 0),
            evidence_documents=data.get('evidence_documents', [])
        )

        MonitoringService.log_audit(
            user=user,
            action="CONSTRUCTION_MILESTONE_CREATED",
            resource_id=milestone.id,
            new_state={"name": milestone.name, "target_date": str(milestone.target_date)}
        )
        return milestone

    @staticmethod
    def verify_milestone(milestone, actor):
        """Verify construction milestone completion."""
        milestone.status = 'VERIFIED'
        milestone.progress_percentage = 100
        milestone.actual_completion_date = timezone.now().date()
        milestone.verified_at = timezone.now()
        milestone.verified_by_name = MonitoringService.get_actor_name(actor, "Building Control Officer")
        milestone.save()

        MonitoringService.log_audit(
            user=actor,
            action="CONSTRUCTION_MILESTONE_VERIFIED",
            resource_id=milestone.id,
            new_state={"status": "VERIFIED", "verified_by": milestone.verified_by_name}
        )
        return milestone

    @staticmethod
    def flag_milestone_delay(milestone, reason, actor):
        """Flag construction milestone delay."""
        milestone.status = 'DELAYED'
        milestone.is_delayed = True
        milestone.delay_reason = reason
        milestone.save()

        MonitoringService.log_audit(
            user=actor,
            action="CONSTRUCTION_MILESTONE_DELAY_FLAGGED",
            resource_id=milestone.id,
            new_state={"status": "DELAYED", "reason": reason}
        )
        return milestone

    @staticmethod
    def record_site_verification(data, user):
        """Record GNSS/Tersus rover verification and compute coordinate variance."""
        project_id = data.get('project_id') or data.get('project')
        project = MonitoringService.get_project_instance(project_id)

        captured_coords = data.get('captured_coordinates', {})
        approved_coords = data.get('approved_coordinates', {})

        # Haversine or Euclidean distance approximation for variance
        variance = 0.0
        if captured_coords and approved_coords:
            lat1 = float(captured_coords.get('lat', 0) or 0)
            lng1 = float(captured_coords.get('lng', 0) or 0)
            lat2 = float(approved_coords.get('lat', 0) or 0)
            lng2 = float(approved_coords.get('lng', 0) or 0)

            if lat1 and lng1 and lat2 and lng2:
                # 1 degree latitude ~ 111,139 meters
                d_lat = (lat1 - lat2) * 111139.0
                d_lng = (lng1 - lng2) * 111139.0 * math.cos(math.radians((lat1 + lat2) / 2))
                variance = round(math.sqrt(d_lat**2 + d_lng**2), 3)

        if 'variance_meters' in data:
            variance = float(data['variance_meters'])

        variance_detected = variance > 0.5 or data.get('variance_detected', False)
        status = 'VARIANCE_DETECTED' if variance_detected else 'VERIFIED'
        verifier_name = data.get('verified_by_name') or MonitoringService.get_actor_name(user, "Field Surveyor")

        verification = SiteVerification.objects.create(
            project=project,
            method=data.get('method', 'GNSS_RTK_SURVEY'),
            device_identifier=data.get('device_identifier', 'Tersus Oscar GNSS RTK #042'),
            boundary_coordinates=data.get('boundary_coordinates', []),
            captured_coordinates=captured_coords,
            approved_coordinates=approved_coords,
            variance_meters=variance,
            variance_detected=variance_detected,
            status=data.get('status', status),
            verified_by_name=verifier_name,
            verified_at=timezone.now(),
            notes=data.get('notes', '')
        )

        MonitoringService.log_audit(
            user=user,
            action="SITE_VERIFICATION_RECORDED",
            resource_id=verification.id,
            new_state={"ref": verification.verification_reference, "variance": variance, "status": verification.status}
        )
        return verification

    @staticmethod
    def get_project_progress_details(project_id=None):
        """
        Compute deep physical progress, milestone stages, photo feed, workforce metrics,
        and schedule health for a project or all active projects.
        """
        if project_id:
            project = MonitoringService.get_project_instance(project_id)
            projects = [project] if project else []
        else:
            projects = list(Project.objects.filter(status__in=['ACTIVE', 'IN_PROGRESS', 'UNDER_CONSTRUCTION'])) or list(Project.objects.all()[:5])

        results = []
        for p in projects:
            updates = list(DailySiteUpdate.objects.filter(project=p).order_by('-created_at'))
            milestones = list(ConstructionMilestone.objects.filter(project=p).order_by('target_date'))
            observations = list(FieldObservation.objects.filter(project=p).order_by('-created_at'))
            issues = list(SiteIssue.objects.filter(project=p).order_by('-created_at'))

            latest_update = updates[0] if updates else None
            
            # Overall progress from project model or latest update
            verified_progress = getattr(p, 'progress', 0) or (latest_update.progress_percentage if latest_update else 0) or 60

            # Schedule health analysis
            delayed_milestones_count = sum(1 for m in milestones if m.status == 'DELAYED' or m.is_delayed)
            critical_issues_count = sum(1 for i in issues if i.severity == 'CRITICAL' and i.status != 'RESOLVED')
            
            if critical_issues_count > 0 or delayed_milestones_count >= 2:
                schedule_status = "CRITICAL_DELAY"
                schedule_label = "Critical Schedule Delay"
            elif delayed_milestones_count == 1:
                schedule_status = "MINOR_DELAY"
                schedule_label = "Minor Schedule Delay"
            elif verified_progress >= 75:
                schedule_status = "AHEAD"
                schedule_label = "Ahead of Schedule"
            else:
                schedule_status = "ON_SCHEDULE"
                schedule_label = "On Schedule"

            # Flatten all project photos
            photos = []
            for u in updates:
                if u.photos and isinstance(u.photos, list):
                    for url in u.photos:
                        if url and url not in [item['url'] for item in photos]:
                            photos.append({
                                'url': url,
                                'update_ref': u.update_reference,
                                'update_type': u.update_type,
                                'date': u.created_at.isoformat(),
                                'work_summary': u.work_summary,
                                'reported_by': u.reported_by_name
                            })

            # Programme phases breakdown
            phases = [
                {
                    'name': 'Substructure, Foundation Piling & Raft Slab',
                    'progress': 100 if verified_progress >= 25 else int(verified_progress * 4),
                    'status': 'Completed & Certified' if verified_progress >= 25 else 'In Progress',
                    'color': 'bg-emerald-500'
                },
                {
                    'name': 'Reinforced Concrete Superstructure (Levels 1 - 12)',
                    'progress': 100 if verified_progress >= 70 else (0 if verified_progress < 25 else int((verified_progress - 25) / 45 * 100)),
                    'status': 'Completed' if verified_progress >= 70 else ('In Active Progress' if verified_progress >= 25 else 'Pending'),
                    'color': 'bg-blue-600'
                },
                {
                    'name': 'MEP Services, HVAC Ducting & Fire Sprinklers',
                    'progress': 100 if verified_progress >= 85 else (0 if verified_progress < 45 else int((verified_progress - 45) / 40 * 100)),
                    'status': 'Completed' if verified_progress >= 85 else ('Rough-in Phase' if verified_progress >= 45 else 'Pending'),
                    'color': 'bg-amber-500'
                },
                {
                    'name': 'Exterior Glazing & Unitized Curtain Wall Facade',
                    'progress': 100 if verified_progress >= 95 else (0 if verified_progress < 60 else int((verified_progress - 60) / 35 * 100)),
                    'status': 'Completed' if verified_progress >= 95 else ('Bracket Installation' if verified_progress >= 60 else 'Pending'),
                    'color': 'bg-indigo-500'
                },
                {
                    'name': 'Interior Finishing, Drywall Partitions & Floor Screed',
                    'progress': 100 if verified_progress >= 100 else (0 if verified_progress < 75 else int((verified_progress - 75) / 25 * 100)),
                    'status': 'Completed' if verified_progress >= 100 else ('Scheduled Next' if verified_progress >= 75 else 'Pending'),
                    'color': 'bg-slate-400'
                }
            ]

            # Progress history trend points
            progress_history = [
                {
                    'date': u.created_at.date().isoformat(),
                    'progress': u.progress_percentage,
                    'summary': u.work_summary,
                    'reported_by': u.reported_by_name
                }
                for u in updates[:10]
            ]

            results.append({
                'project_id': str(p.id),
                'project_name': p.name,
                'reference_number': getattr(p, 'reference_number', None) or str(p.id)[:8],
                'project_type': getattr(p, 'project_type', 'Commercial Multi-Story Structure'),
                'site_address': getattr(p, 'site_address', None) or getattr(p, 'location', 'Lagos State'),
                'status': getattr(p, 'status', 'ACTIVE'),
                'verified_progress': verified_progress,
                'schedule_status': schedule_status,
                'schedule_label': schedule_label,
                'workforce_on_site': latest_update.workforce_count if latest_update else 35,
                'weather_condition': latest_update.weather_condition if latest_update else 'Clear / Sunny (31°C)',
                'total_photos_count': len(photos),
                'photos': photos,
                'milestones_total': len(milestones),
                'milestones_verified': sum(1 for m in milestones if m.status in ['VERIFIED', 'COMPLETED']),
                'milestones_delayed': delayed_milestones_count,
                'milestones': [
                    {
                        'id': str(m.id),
                        'name': m.name,
                        'target_date': m.target_date.isoformat() if m.target_date else None,
                        'status': m.status,
                        'progress_percentage': m.progress_percentage,
                        'is_delayed': m.is_delayed
                    }
                    for m in milestones
                ],
                'phases': phases,
                'latest_update': {
                    'reference': latest_update.update_reference,
                    'work_summary': latest_update.work_summary,
                    'reported_by': latest_update.reported_by_name,
                    'date': latest_update.created_at.isoformat()
                } if latest_update else None,
                'progress_history': progress_history
            })

        if project_id and results:
            return results[0]
        return results

    @staticmethod
    def update_project_progress(data, user):
        """Update progress percentage and create a progress log in database."""
        project_id = data.get('project_id') or data.get('project')
        project = MonitoringService.get_project_instance(project_id)
        
        progress = int(data.get('progress_percentage', 0))
        summary = data.get('work_summary') or f"Site progress updated to {progress}%."
        photos = data.get('photos', [])
        author_name = MonitoringService.get_actor_name(user, "Building Control Officer")

        # 1. Update Project progress
        if project:
            if hasattr(project, 'progress'):
                project.progress = progress
                project.save()

        # 2. Log DailySiteUpdate record
        update = DailySiteUpdate.objects.create(
            project=project,
            update_type='PROGRESS_REPORT',
            reported_by=user if getattr(user, 'is_authenticated', False) else None,
            reported_by_name=author_name,
            progress_percentage=progress,
            work_summary=summary,
            photos=photos,
            status='Approved',
            priority='High'
        )

        MonitoringService.log_audit(
            user=user,
            action="PROJECT_PROGRESS_UPDATED",
            resource_id=project.id if project else update.id,
            new_state={"progress": progress, "summary": summary}
        )

        return MonitoringService.get_project_progress_details(project.id if project else None)

    @staticmethod
    def flag_project_schedule_delay(data, user):
        """Flag project schedule delay and record non-conformance defect."""
        project_id = data.get('project_id') or data.get('project')
        project = MonitoringService.get_project_instance(project_id)
        reason = data.get('reason') or data.get('description') or 'Construction progress is tracking behind approved statutory timeline.'
        severity = data.get('severity', 'HIGH')

        # Create defect issue
        issue = SiteIssue.objects.create(
            project=project,
            title=f"Schedule Delay Notice: {project.name if project else 'Site'}",
            description=reason,
            severity=severity,
            status='OPEN',
            reported_by_name=MonitoringService.get_actor_name(user, "Monitoring Lead Officer"),
            assigned_to_name="Principal Contractor / Project Manager"
        )

        # Flag any upcoming milestones as delayed
        if project:
            ConstructionMilestone.objects.filter(project=project, status='UPCOMING').update(is_delayed=True, delay_reason=reason)

        MonitoringService.log_audit(
            user=user,
            action="SCHEDULE_DELAY_FLAGGED",
            resource_id=project.id if project else issue.id,
            new_state={"issue_ref": issue.issue_reference, "reason": reason}
        )

        return issue

