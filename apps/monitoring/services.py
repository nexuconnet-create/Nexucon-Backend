import math
from django.utils import timezone
from .models import DailySiteUpdate, FieldObservation, SiteIssue, ConstructionMilestone, SiteVerification
from apps.projects.models import Project
from apps.audit.models import AuditEvent

class MonitoringService:
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
        project = Project.objects.get(pk=project_id)
        
        progress = int(data.get('progress_percentage', 0))
        update_type = data.get('update_type', 'DAILY_PHOTO')

        update = DailySiteUpdate.objects.create(
            project=project,
            update_type=update_type,
            reported_by=user if getattr(user, 'is_authenticated', False) else None,
            reported_by_name=data.get('reported_by_name') or (user.get_full_name() or user.email),
            progress_percentage=progress,
            work_summary=data.get('work_summary', ''),
            photos=data.get('photos', []),
            drone_survey_data=data.get('drone_survey_data', {}),
            weather_condition=data.get('weather_condition', 'Clear / Sunny'),
            workforce_count=int(data.get('workforce_count', 0)),
            gps_coordinates=data.get('gps_coordinates', {}),
            status=data.get('status', 'Active'),
            priority=data.get('priority', 'Medium')
        )

        # Update Project progress if progress was reported
        if progress > 0 and hasattr(project, 'progress'):
            try:
                project.progress = max(getattr(project, 'progress', 0) or 0, progress)
                project.save()
            except Exception:
                pass

        MonitoringService.log_audit(
            user=user,
            action="DAILY_SITE_UPDATE_LOGGED",
            resource_id=update.id,
            new_state={"ref": update.update_reference, "type": update.update_type, "progress": progress}
        )
        return update

    @staticmethod
    def create_observation(data, user):
        """Record field observation from site visit."""
        project_id = data.get('project_id') or data.get('project')
        project = Project.objects.get(pk=project_id)

        obs = FieldObservation.objects.create(
            project=project,
            category=data.get('category', 'QUALITY'),
            title=data.get('title', 'Field Observation'),
            description=data.get('description', ''),
            severity=data.get('severity', 'LOW'),
            status=data.get('status', 'OPEN'),
            assigned_officer_id=data.get('assigned_officer_id'),
            assigned_officer_name=data.get('assigned_officer_name'),
            observed_by_name=data.get('observed_by_name') or (user.get_full_name() or user.email),
            gps_coordinates=data.get('gps_coordinates', {}),
            evidence_photos=data.get('evidence_photos', []),
            corrective_action=data.get('corrective_action', '')
        )

        MonitoringService.log_audit(
            user=user,
            action="FIELD_OBSERVATION_RECORDED",
            resource_id=obs.id,
            new_state={"ref": obs.observation_reference, "category": obs.category, "severity": obs.severity}
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
        project = Project.objects.get(pk=project_id)

        issue = SiteIssue.objects.create(
            project=project,
            title=data.get('title', 'Site Issue'),
            description=data.get('description', ''),
            severity=data.get('severity', 'MEDIUM'),
            status=data.get('status', 'OPEN'),
            assigned_to_name=data.get('assigned_to_name', 'Site Engineer'),
            reported_by_name=data.get('reported_by_name') or (user.get_full_name() or user.email),
            due_date=data.get('due_date'),
            resolution_evidence=data.get('resolution_evidence', []),
            is_escalated=data.get('is_escalated', False)
        )

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
        project = Project.objects.get(pk=project_id)

        milestone = ConstructionMilestone.objects.create(
            project=project,
            name=data.get('name', 'Milestone'),
            target_date=data.get('target_date', timezone.now().date()),
            status=data.get('status', 'UPCOMING'),
            progress_percentage=int(data.get('progress_percentage', 0)),
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
        milestone.verified_by_name = actor.get_full_name() or actor.email
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
        project = Project.objects.get(pk=project_id)

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
            verified_by_name=user.get_full_name() or user.email if user else 'Field Officer',
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
