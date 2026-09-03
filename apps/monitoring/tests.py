import datetime
import uuid
from django.test import TestCase
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APITestCase
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework_simplejwt.tokens import RefreshToken
from apps.projects.models import Project
from apps.stakeholders.models import Developer
from apps.inspections.models import Inspection, StopWorkOrder
from apps.monitoring.models import (
    DailySiteUpdate, MissedSiteVisitRecord, FieldObservation, SiteIssue,
    ConstructionMilestone, SiteVerification
)
from apps.monitoring.services import MonitoringService
from apps.audit.models import AuditEvent
from common.permissions import scoped_projects

User = get_user_model()

class SiteMonitoringWorkflowTestCase(TestCase):
    def setUp(self):
        self.officer = User.objects.create_superuser(
            username="monitoring_officer@nexucon.com",
            email="monitoring_officer@nexucon.com",
            password="Password123!",
            first_name="Tunde",
            last_name="Bakare"
        )
        self.project = Project.objects.create(
            name="Eko Atlantic Towers",
            project_type="Commercial",
            status="ACTIVE",
            site_address="Plot 12, Ocean View Boulevard",
            lga="Victoria Island"
        )

    def test_log_daily_update(self):
        update = MonitoringService.log_daily_update(
            data={
                "project_id": self.project.id,
                "update_type": "DAILY_PHOTO",
                "progress_percentage": 35,
                "work_summary": "Completed 4th floor concrete pour and column rebar fixing.",
                "photos": ["https://assets.nexucon.com/photos/site_pour_1.jpg"],
                "workforce_count": 48
            },
            user=self.officer
        )
        self.assertIsNotNone(update.update_reference)
        self.assertEqual(update.progress_percentage, 35)
        self.assertTrue(AuditEvent.objects.filter(resource_id=str(update.id), action="DAILY_UPDATE_LOGGED").exists())

    def test_field_observation_lifecycle(self):
        obs = MonitoringService.create_observation(
            data={
                "project_id": self.project.id,
                "category": "SAFETY",
                "title": "Perimeter Scaffolding Catch Net Missing",
                "description": "Scaffolding level 3 lacks debris netting on south elevation.",
                "severity": "HIGH",
                "corrective_action": "Install safety debris netting before proceeding with external plastering."
            },
            user=self.officer
        )
        self.assertEqual(obs.status, "OPEN")
        self.assertEqual(obs.severity, "HIGH")

        # Resolve observation
        MonitoringService.resolve_observation(
            observation=obs,
            notes="Safety nets installed and inspected by HSE lead.",
            actor=self.officer
        )
        obs.refresh_from_db()
        self.assertEqual(obs.status, "RESOLVED")
        self.assertIsNotNone(obs.resolved_at)

    def test_site_issue_reporting_and_escalation(self):
        issue = MonitoringService.report_issue(
            data={
                "project_id": self.project.id,
                "title": "Unauthorized Drainage Connection",
                "description": "Site contractor tapping into storm drain without EPB permit.",
                "severity": "CRITICAL"
            },
            user=self.officer
        )
        self.assertEqual(issue.status, "OPEN")

        # Escalate issue
        MonitoringService.escalate_issue(issue, actor=self.officer)
        issue.refresh_from_db()
        self.assertTrue(issue.is_escalated)
        self.assertEqual(issue.status, "UNDER_REVIEW")

        # Resolve issue
        MonitoringService.resolve_issue(issue, notes="Drainage permit approved and fee paid.", evidence=[], actor=self.officer)
        issue.refresh_from_db()
        self.assertEqual(issue.status, "RESOLVED")

    def test_construction_milestone_lifecycle_and_guardrails(self):
        # 1. Create Milestone
        milestone = MonitoringService.create_milestone(
            data={
                "project_id": self.project.id,
                "name": "Substructure Raft Pour",
                "phase": "SUBSTRUCTURE",
                "milestone_code": "MS-01",
                "target_date": timezone.now().date() + datetime.timedelta(days=14),
                "duration_days": 20,
                "critical_path": True,
                "progress_percentage": 50
            },
            user=self.officer
        )
        self.assertEqual(milestone.status, "IN_PROGRESS")
        self.assertEqual(milestone.progress_percentage, 50)
        self.assertTrue(milestone.critical_path)

        # 2. Update Progress to 100% -> Guardrail: Must be PENDING_VERIFICATION, NOT VERIFIED
        MonitoringService.update_milestone_progress(
            milestone=milestone,
            data={
                "progress_percentage": 100,
                "physical_progress_notes": "Raft foundation pour completed and cube test samples cast.",
                "evidence_documents": [
                    {"name": "Concrete Cube Test 28-day.pdf", "url": "https://assets.nexucon.gov.ng/test.pdf", "category": "Laboratory Test Report"}
                ]
            },
            user=self.officer
        )
        milestone.refresh_from_db()
        self.assertEqual(milestone.progress_percentage, 100)
        self.assertEqual(milestone.status, "PENDING_VERIFICATION")
        self.assertFalse(milestone.status == "VERIFIED")

        # 3. Evaluate Gates
        gates = MonitoringService.evaluate_milestone_gates(milestone)
        self.assertTrue(gates['all_gates_passed'])

        # 4. Sign off & Verify
        verified_ms = MonitoringService.verify_milestone(
            milestone=milestone,
            data={"notes": "All concrete strength benchmarks verified."},
            actor=self.officer
        )
        self.assertEqual(verified_ms.status, "VERIFIED")
        self.assertIsNotNone(verified_ms.verified_at)
        self.assertIsNotNone(verified_ms.verification_signoff)
        self.assertRegex(verified_ms.verification_signoff['signature_hash'], r'^0x[0-9A-F]{24}$')

        # 5. Check Audit Trail
        trail = MonitoringService.get_milestone_audit_trail(milestone.id)
        self.assertGreater(len(trail), 0)

    def test_milestone_delay_flagging(self):
        milestone = MonitoringService.create_milestone(
            data={
                "project_id": self.project.id,
                "name": "Superstructure Frame Level 10",
                "phase": "SUPERSTRUCTURE",
                "target_date": timezone.now().date() + datetime.timedelta(days=10),
                "progress_percentage": 60
            },
            user=self.officer
        )

        revised = timezone.now().date() + datetime.timedelta(days=24)
        MonitoringService.flag_milestone_delay(
            milestone=milestone,
            data={
                "reason": "Custom curved facade panels delayed at port.",
                "revised_target_date": revised
            },
            actor=self.officer
        )
        milestone.refresh_from_db()
        self.assertEqual(milestone.status, "DELAYED")
        self.assertTrue(milestone.is_delayed)
        self.assertEqual(milestone.variance_days, 14)
        self.assertEqual(milestone.risk_level, "HIGH")

    def test_site_verification_variance_calculation(self):
        # 1. Test coordinate variance calculation with matching coords (within 0.05m tolerance)
        vrf_pass = MonitoringService.record_site_verification(
            data={
                "project_id": self.project.id,
                "method": "GNSS_RTK_SURVEY",
                "captured_coordinates": {"lat": 6.4281001, "lng": 3.4219001, "elevation": 4.15},
                "approved_coordinates": {"lat": 6.4281000, "lng": 3.4219000, "elevation": 4.15},
                "tolerance_limit_meters": 0.05
            },
            user=self.officer
        )
        self.assertEqual(vrf_pass.status, "VERIFIED")
        self.assertFalse(vrf_pass.variance_detected)
        self.assertLess(vrf_pass.variance_meters, 0.05)

        # 2. Test formal certification
        certified = MonitoringService.certify_site_verification(
            verification=vrf_pass,
            data={"verified_by_name": "Surv. Olumide Balogun", "notes": "Approved"},
            actor=self.officer
        )
        self.assertEqual(certified.status, "VERIFIED")
        self.assertIsNotNone(certified.digital_cert_ref)
        self.assertIsNotNone(certified.signature_hash)

        # 3. Test coordinate variance calculation with shifted coords (> 0.05m)
        vrf_fail = MonitoringService.record_site_verification(
            data={
                "project_id": self.project.id,
                "method": "TERSU_ROVER",
                "captured_coordinates": {"lat": 6.428100, "lng": 3.421900},
                "approved_coordinates": {"lat": 6.428150, "lng": 3.421950},
            },
            user=self.officer
        )
        self.assertEqual(vrf_fail.status, "VARIANCE_DETECTED")
        self.assertTrue(vrf_fail.variance_detected)
        self.assertGreater(vrf_fail.variance_meters, 0.05)

        # 4. Test flagging encroachment
        flagged = MonitoringService.flag_site_encroachment(
            verification=vrf_fail,
            data={"reason": "Setback encroachment on North corridor"},
            actor=self.officer
        )
        self.assertEqual(flagged.status, "FLAGGED")
        self.assertTrue(flagged.encroachment_detected)


# ===========================================================================
# Service-level coverage: project resolution, actor identity, audit logging
# ===========================================================================

class MonitoringServiceHelpersTestCase(TestCase):
    def setUp(self):
        self.officer = User.objects.create_superuser(
            username="helpers_officer@nexucon.com",
            email="helpers_officer@nexucon.com",
            password="Password123!",
            first_name="Ada",
            last_name="Obi",
        )
        self.project = Project.objects.create(
            name="Marina Heights Residence", project_type="Residential",
            status="ACTIVE", site_address="24 Marina Road", lga="Lagos Island",
        )

    def test_get_project_instance_by_uuid(self):
        self.assertEqual(MonitoringService.get_project_instance(self.project.id).id, self.project.id)

    def test_get_project_instance_by_uuid_string(self):
        self.assertEqual(MonitoringService.get_project_instance(str(self.project.id)).id, self.project.id)

    def test_get_project_instance_by_reference_number(self):
        self.assertEqual(
            MonitoringService.get_project_instance(self.project.reference_number).id, self.project.id)

    def test_get_project_instance_by_name_substring(self):
        self.assertEqual(MonitoringService.get_project_instance("marina heights").id, self.project.id)

    def test_get_project_instance_unknown_uuid_raises(self):
        with self.assertRaises(DRFValidationError):
            MonitoringService.get_project_instance(uuid.uuid4())

    def test_get_project_instance_unknown_value_raises(self):
        with self.assertRaises(DRFValidationError):
            MonitoringService.get_project_instance("no-such-project-xyz")

    def test_get_project_instance_empty_raises(self):
        with self.assertRaises(DRFValidationError):
            MonitoringService.get_project_instance(None)
        with self.assertRaises(DRFValidationError):
            MonitoringService.get_project_instance("")

    def test_get_actor_name_prefers_full_name_then_email(self):
        self.assertEqual(MonitoringService.get_actor_name(self.officer), "Ada Obi")
        bare = User.objects.create_user(
            username="bare@nexucon.com", email="bare@nexucon.com", password="Password123!")
        self.assertEqual(MonitoringService.get_actor_name(bare), "bare@nexucon.com")
        self.assertEqual(MonitoringService.get_actor_name(bare, "Fallback"), "bare@nexucon.com")
        self.assertEqual(MonitoringService.get_actor_name(None, "Fallback"), "Fallback")

    def test_log_audit_creates_immutable_event(self):
        MonitoringService.log_audit(
            user=self.officer, action="UNIT_TEST_ACTION", resource_id="res-1",
            previous_state={"a": 1}, new_state={"b": 2})
        event = AuditEvent.objects.get(action="UNIT_TEST_ACTION", resource_id="res-1")
        self.assertEqual(event.resource_type, "SiteMonitoring")
        self.assertEqual(event.user, self.officer)
        self.assertEqual(event.previous_state, {"a": 1})
        self.assertEqual(event.new_state, {"b": 2})
        self.assertTrue(event.signature_hash.startswith("0x"))


# ===========================================================================
# Daily update / progress / telemetry services
# ===========================================================================

class DailyUpdateServiceTestCase(TestCase):
    def setUp(self):
        self.officer = User.objects.create_superuser(
            username="daily_officer@nexucon.com",
            email="daily_officer@nexucon.com",
            password="Password123!",
            first_name="Ngozi", last_name="Eze",
        )
        self.project = Project.objects.create(
            name="Lekki Deep Blue Estate", project_type="Residential", status="ACTIVE",
            latitude=6.4281, longitude=3.4219, lga="Eti-Osa",
        )

    def test_log_daily_update_builds_field_verification_stamp(self):
        update = MonitoringService.log_daily_update(
            data={
                "project_id": self.project.id,
                "update_type": "DAILY_PHOTO",
                "work_summary": "Level 3 blockwork completed.",
                "inspector_name": "Insp. Bala Yusuf",
                "inspector_badge": "BC-1188",
                "gps_coordinates": {"lat": 6.4281, "lng": 3.4219},
                "workforce_count": 22,
                "weather_condition": "Overcast / Light Drizzle",
                "progress_percentage": 45,
            },
            user=self.officer,
        )
        self.assertEqual(update.project, self.project)
        self.assertEqual(update.inspector, self.officer)
        self.assertEqual(update.reported_by, self.officer)
        self.assertEqual(update.inspector_name, "Insp. Bala Yusuf")
        self.assertEqual(update.inspector_badge, "BC-1188")
        self.assertEqual(update.workforce_count, 22)
        self.assertEqual(update.weather_condition, "Overcast / Light Drizzle")
        stamp = update.field_verification_stamp
        self.assertEqual(stamp["inspector_name"], "Insp. Bala Yusuf")
        self.assertEqual(stamp["inspector_badge"], "BC-1188")
        self.assertEqual(stamp["origin"], "FIELD_INSPECTOR")
        self.assertTrue(stamp["gps_lock"])
        self.assertIn("certified_at", stamp)
        self.assertTrue(
            AuditEvent.objects.filter(resource_id=str(update.id), action="DAILY_UPDATE_LOGGED").exists())

    def test_log_daily_update_uses_supplied_stamp_verbatim(self):
        supplied = {"inspector_name": "Insp. Kemi", "seal": "custom-stamp"}
        update = MonitoringService.log_daily_update(
            data={"project_id": self.project.id, "work_summary": "Photos taken.",
                  "field_verification_stamp": supplied},
            user=self.officer)
        self.assertEqual(update.field_verification_stamp, supplied)

    def test_log_daily_update_parses_inspection_date_string(self):
        update = MonitoringService.log_daily_update(
            data={"project_id": self.project.id, "work_summary": "Log.",
                  "inspection_date": "2026-08-15T10:30:00Z"},
            user=self.officer)
        self.assertEqual(update.inspection_date, datetime.date(2026, 8, 15))

    def test_log_daily_update_accepts_date_object(self):
        update = MonitoringService.log_daily_update(
            data={"project_id": self.project.id, "work_summary": "Log.",
                  "inspection_date": datetime.date(2026, 1, 5)},
            user=self.officer)
        self.assertEqual(update.inspection_date, datetime.date(2026, 1, 5))

    def test_log_daily_update_unparseable_date_falls_back_to_today(self):
        update = MonitoringService.log_daily_update(
            data={"project_id": self.project.id, "work_summary": "Log.",
                  "inspection_date": "31/12/2026"},
            user=self.officer)
        self.assertEqual(update.inspection_date, datetime.date.today())

    def test_log_daily_update_anonymous_actor_leaves_fks_null(self):
        update = MonitoringService.log_daily_update(
            data={"project_id": self.project.id, "work_summary": "Unattributed sync.",
                  "origin_type": "OFFLINE_FIELD_SYNC"},
            user=None)
        self.assertIsNone(update.inspector)
        self.assertIsNone(update.reported_by)
        self.assertEqual(update.reported_by_name, "Unattributed")
        self.assertEqual(update.inspector_name, "Unattributed")
        self.assertEqual(update.origin_type, "OFFLINE_FIELD_SYNC")
        self.assertFalse(update.field_verification_stamp["gps_lock"])

    def test_log_daily_update_unresolvable_project_raises(self):
        with self.assertRaises(DRFValidationError):
            MonitoringService.log_daily_update(
                data={"project_id": "ghost-project", "work_summary": "x"}, user=self.officer)

    def test_update_project_progress_creates_log_and_returns_details(self):
        details = MonitoringService.update_project_progress(
            data={"project_id": self.project.id, "progress_percentage": 65,
                  "work_summary": "Slab pour completed on wing B."},
            user=self.officer)
        log = DailySiteUpdate.objects.get(project=self.project, update_type="PROGRESS_REPORT")
        self.assertEqual(log.progress_percentage, 65)
        self.assertEqual(log.status, "Approved")
        self.assertEqual(log.reported_by, self.officer)
        self.assertEqual(details["project_id"], str(self.project.id))
        self.assertEqual(details["verified_progress"], 65)
        self.assertTrue(AuditEvent.objects.filter(
            action="PROJECT_PROGRESS_UPDATED", resource_id=str(self.project.id)).exists())

    def test_flag_project_schedule_delay_creates_issue_and_flags_upcoming_milestones(self):
        upcoming = ConstructionMilestone.objects.create(
            project=self.project, name="Roof installation", status="UPCOMING",
            target_date=timezone.now().date() + datetime.timedelta(days=20))
        verified = ConstructionMilestone.objects.create(
            project=self.project, name="Already verified", status="VERIFIED",
            target_date=timezone.now().date() - datetime.timedelta(days=5))

        issue = MonitoringService.flag_project_schedule_delay(
            data={"project_id": self.project.id,
                  "reason": "Rebar supply disruption halting slab cycles."},
            user=self.officer)

        self.assertTrue(issue.title.startswith("Schedule Delay Notice:"))
        self.assertEqual(issue.status, "OPEN")
        self.assertEqual(issue.severity, "HIGH")
        upcoming.refresh_from_db()
        self.assertTrue(upcoming.is_delayed)
        self.assertIn("Rebar supply", upcoming.delay_reason)
        verified.refresh_from_db()
        self.assertFalse(verified.is_delayed)
        self.assertTrue(AuditEvent.objects.filter(action="SCHEDULE_DELAY_FLAGGED").exists())

    def test_calculate_location_telemetry_computes_distance_to_site(self):
        telemetry = MonitoringService.calculate_location_telemetry(6.4291, 3.4219, self.project.id)
        # 0.001 degrees of latitude is roughly 111 meters north of the site.
        self.assertIsNotNone(telemetry["distance_to_centroid_meters"])
        self.assertGreater(telemetry["distance_to_centroid_meters"], 100)
        self.assertLess(telemetry["distance_to_centroid_meters"], 125)
        self.assertIn("https://www.google.com/maps?q=6.4291,3.4219", telemetry["google_maps_url"])
        self.assertIsNone(telemetry["accuracy_meters"])

    def test_calculate_location_telemetry_uses_only_device_supplied_readings(self):
        telemetry = MonitoringService.calculate_location_telemetry(
            6.4281, 3.4219, self.project.id,
            telemetry_source={"accuracy": 3.5, "satellites_tracked": 19,
                              "rtk_fix_status": "FIXED", "source": "RTK_ROVER",
                              "laser_distance_meters": 12.4,
                              "setback_measured_meters": 6.2,
                              "setback_target_meters": 5.0,
                              "cors_station_ref": "LASG-CORS-01"})
        self.assertEqual(telemetry["accuracy_meters"], 3.5)
        self.assertEqual(telemetry["satellites_tracked"], 19)
        self.assertEqual(telemetry["rtk_fix_status"], "FIXED")
        self.assertEqual(telemetry["source"], "RTK_ROVER")
        self.assertEqual(telemetry["setback_status"], "PASS")
        self.assertEqual(telemetry["cors_station_ref"], "LASG-CORS-01")
        self.assertEqual(telemetry["distance_to_centroid_meters"], 0.0)
        self.assertIn("Lekki Deep Blue Estate", telemetry["address"])

    def test_calculate_location_telemetry_setback_fail(self):
        telemetry = MonitoringService.calculate_location_telemetry(
            6.4, 3.4, None,
            telemetry_source={"setback_measured_meters": 3.0, "setback_target_meters": 5.0})
        self.assertEqual(telemetry["setback_status"], "FAIL")
        self.assertIsNone(telemetry["distance_to_centroid_meters"])
        self.assertIsNone(telemetry["address"])

    def test_calculate_location_telemetry_invalid_coords_raise(self):
        with self.assertRaises(DRFValidationError):
            MonitoringService.calculate_location_telemetry("north", "east")
        with self.assertRaises(DRFValidationError):
            MonitoringService.calculate_location_telemetry(None, None)

    def test_calculate_location_telemetry_non_numeric_setback_is_ignored(self):
        telemetry = MonitoringService.calculate_location_telemetry(
            6.4, 3.4, None,
            telemetry_source={"setback_measured_meters": "six", "setback_target_meters": 5.0})
        self.assertIsNone(telemetry["setback_status"])

    def test_get_daily_update_telemetry_without_gps_reports_nulls(self):
        update = MonitoringService.log_daily_update(
            data={"project_id": self.project.id, "work_summary": "No GPS device."},
            user=self.officer)
        telemetry = MonitoringService.get_daily_update_telemetry(update.id)
        self.assertIsNone(telemetry["latitude"])
        self.assertIsNone(telemetry["longitude"])
        self.assertIsNone(telemetry["google_maps_url"])
        self.assertIsNone(telemetry["setback_status"])

    def test_get_daily_update_telemetry_with_gps(self):
        update = MonitoringService.log_daily_update(
            data={"project_id": self.project.id, "work_summary": "GPS tagged.",
                  "gps_coordinates": {"lat": 6.4281, "lng": 3.4219, "accuracy": 4.0}},
            user=self.officer)
        telemetry = MonitoringService.get_daily_update_telemetry(update.id)
        self.assertEqual(telemetry["latitude"], 6.4281)
        self.assertEqual(telemetry["accuracy_meters"], 4.0)
        self.assertIn("google.com/maps", telemetry["google_maps_url"])

    def test_update_daily_update_telemetry_merges_payload(self):
        update = MonitoringService.log_daily_update(
            data={"project_id": self.project.id, "work_summary": "Tagged.",
                  "gps_coordinates": {"lat": 6.4281, "lng": 3.4219}},
            user=self.officer)
        MonitoringService.update_daily_update_telemetry(
            update=update,
            telemetry_data={"satellites_tracked": 17, "rtk_fix_status": "FLOAT"},
            user=self.officer)
        update.refresh_from_db()
        self.assertEqual(update.gps_coordinates["lat"], 6.4281)
        self.assertEqual(update.gps_coordinates["satellites_tracked"], 17)
        self.assertEqual(update.gps_coordinates["rtk_fix_status"], "FLOAT")
        self.assertTrue(AuditEvent.objects.filter(
            action="DAILY_UPDATE_TELEMETRY_SYNCED", resource_id=str(update.id)).exists())


# ===========================================================================
# Missed site visit justification workflow
# ===========================================================================

class MissedSiteVisitServiceTestCase(TestCase):
    def setUp(self):
        self.officer = User.objects.create_superuser(
            username="msv_officer@nexucon.com",
            email="msv_officer@nexucon.com",
            password="Password123!",
            first_name="Chidi", last_name="Nwosu",
        )
        self.project = Project.objects.create(
            name="Ikoyi Garden Terraces", project_type="Residential", status="ACTIVE")

    def test_log_missed_visit_defaults_and_audit(self):
        record = MonitoringService.log_missed_site_visit(
            data={"project_id": self.project.id, "justification_notes": "   "},
            user=self.officer)
        self.assertEqual(record.project, self.project)
        self.assertEqual(record.inspector, self.officer)
        self.assertEqual(record.inspector_name, "Chidi Nwosu")
        self.assertEqual(record.reason_category, "ADVERSE_WEATHER")
        self.assertEqual(record.status, "SUBMITTED")
        self.assertEqual(record.scheduled_date, datetime.date.today())
        self.assertEqual(
            record.justification_notes,
            "Site visit could not proceed due to documented operational blocker.")
        self.assertTrue(record.record_reference.startswith("MSV-"))
        self.assertTrue(AuditEvent.objects.filter(
            action="MISSED_SITE_VISIT_DOCUMENTED", resource_id=str(record.id)).exists())

    def test_log_missed_visit_with_full_details(self):
        record = MonitoringService.log_missed_site_visit(
            data={
                "project_id": self.project.id,
                "inspector_name": "Insp. Sade Adeniyi",
                "inspector_badge": "BC-3301",
                "scheduled_date": "2026-07-20",
                "reason_category": "ACCESS_DENIED",
                "justification_notes": "Developer locked the gate pending title dispute.",
                "evidence_photos": ["https://assets.nexucon.com/locked-gate.jpg"],
                "status": "SUBMITTED",
            },
            user=self.officer)
        self.assertEqual(record.inspector_name, "Insp. Sade Adeniyi")
        self.assertEqual(record.inspector_badge, "BC-3301")
        self.assertEqual(record.scheduled_date, datetime.date(2026, 7, 20))
        self.assertEqual(record.reason_category, "ACCESS_DENIED")
        self.assertEqual(record.evidence_photos, ["https://assets.nexucon.com/locked-gate.jpg"])

    def test_acknowledge_missed_visit_defaults(self):
        record = MonitoringService.log_missed_site_visit(
            data={"project_id": self.project.id, "justification_notes": "Flooded access road."},
            user=self.officer)
        acknowledged = MonitoringService.acknowledge_missed_site_visit(
            visit_id=record.id, data={}, user=self.officer)
        self.assertEqual(acknowledged.status, "ACKNOWLEDGED")
        self.assertEqual(acknowledged.supervisor_acknowledged_by, self.officer)
        self.assertIsNotNone(acknowledged.acknowledged_at)
        self.assertIn("Acknowledged", acknowledged.supervisor_acknowledgment)
        self.assertTrue(AuditEvent.objects.filter(
            action="MISSED_SITE_VISIT_ACKNOWLEDGED", resource_id=str(record.id)).exists())

    def test_acknowledge_missed_visit_custom_status_and_notes(self):
        record = MonitoringService.log_missed_site_visit(
            data={"project_id": self.project.id, "justification_notes": "x"}, user=self.officer)
        acknowledged = MonitoringService.acknowledge_missed_site_visit(
            visit_id=record.id,
            data={"status": "FLAGGED_UNJUSTIFIED",
                  "supervisor_acknowledgment": "No supporting evidence supplied."},
            user=self.officer)
        self.assertEqual(acknowledged.status, "FLAGGED_UNJUSTIFIED")
        self.assertEqual(acknowledged.supervisor_acknowledgment, "No supporting evidence supplied.")

    def test_acknowledge_unknown_visit_returns_none(self):
        self.assertIsNone(MonitoringService.acknowledge_missed_site_visit(
            visit_id=uuid.uuid4(), data={}, user=self.officer))

    def test_log_missed_visit_unparseable_date_falls_back_to_today(self):
        record = MonitoringService.log_missed_site_visit(
            data={"project_id": self.project.id, "justification_notes": "x",
                  "scheduled_date": "not-a-date"},
            user=self.officer)
        self.assertEqual(record.scheduled_date, datetime.date.today())


# ===========================================================================
# Milestone creation, progress, gate evaluation, verification, delay
# ===========================================================================

class MilestoneGateServiceTestCase(TestCase):
    def setUp(self):
        self.officer = User.objects.create_superuser(
            username="gate_officer@nexucon.com",
            email="gate_officer@nexucon.com",
            password="Password123!",
            first_name="Ifeoma", last_name="Adeyemi",
        )
        self.project = Project.objects.create(
            name="Victoria Island Civic Centre", project_type="Institutional", status="ACTIVE")
        self.today = timezone.now().date()

    def _milestone(self, **kwargs):
        defaults = dict(
            project=self.project, name="Gate Test Milestone",
            target_date=self.today + datetime.timedelta(days=30),
        )
        defaults.update(kwargs)
        return ConstructionMilestone.objects.create(**defaults)

    @staticmethod
    def _gate(result, key):
        return next(g for g in result["gates"] if g["key"] == key)

    def test_all_gates_pass_on_clean_milestone(self):
        ms = self._milestone(evidence_documents=[{"name": "Cube test 28-day.pdf"}])
        result = MonitoringService.evaluate_milestone_gates(ms)
        self.assertTrue(result["all_gates_passed"])
        self.assertFalse(result["is_blocked"])
        self.assertEqual(len(result["gates"]), 5)
        self.assertEqual(result["blockers"], [])
        for gate in result["gates"]:
            self.assertEqual(gate["status"], "PASSED", gate["key"])
        self.assertEqual(result["summary"], "All statutory verification gates satisfied")

    def test_dependency_gate_fails_on_unverified_predecessor_by_id(self):
        pred = self._milestone(name="Piling & pile caps", status="IN_PROGRESS")
        ms = self._milestone(dependencies=[{"id": str(pred.id), "code": pred.milestone_code,
                                            "name": pred.name, "is_blocking": True}])
        result = MonitoringService.evaluate_milestone_gates(ms)
        self.assertFalse(result["all_gates_passed"])
        self.assertTrue(result["is_blocked"])
        dep_gate = self._gate(result, "dependencies")
        self.assertEqual(dep_gate["status"], "FAILED")
        self.assertTrue(any("Piling & pile caps" in b for b in result["blockers"]))

    def test_dependency_gate_fails_on_predecessor_referenced_by_code(self):
        pred = self._milestone(name="Raft pour", status="PENDING_VERIFICATION")
        ms = self._milestone(dependencies=[{"code": pred.milestone_code}])
        result = MonitoringService.evaluate_milestone_gates(ms)
        self.assertEqual(self._gate(result, "dependencies")["status"], "FAILED")

    def test_dependency_gate_passes_when_predecessors_verified_or_completed(self):
        pred1 = self._milestone(name="Substructure", status="VERIFIED")
        pred2 = self._milestone(name="Frame", status="COMPLETED")
        ms = self._milestone(dependencies=[
            {"id": str(pred1.id)}, {"id": str(pred2.id)}])
        result = MonitoringService.evaluate_milestone_gates(ms)
        self.assertEqual(self._gate(result, "dependencies")["status"], "PASSED")

    def test_inspection_gate_fails_on_linked_failed_inspection(self):
        ms = self._milestone(linked_inspection_ids=[
            {"id": "ins-1", "type": "Foundation Inspection", "status": "FAILED", "outcome": "FAILED"}])
        result = MonitoringService.evaluate_milestone_gates(ms)
        gate = self._gate(result, "inspections")
        self.assertEqual(gate["status"], "FAILED")
        self.assertTrue(any("Foundation Inspection" in b for b in result["blockers"]))

    def test_inspection_gate_passes_on_linked_passed_inspection(self):
        ms = self._milestone(linked_inspection_ids=[
            {"id": "ins-2", "type": "Structural Review", "outcome": "PASSED"}])
        result = MonitoringService.evaluate_milestone_gates(ms)
        self.assertEqual(self._gate(result, "inspections")["status"], "PASSED")

    def test_inspection_gate_falls_back_to_project_inspections(self):
        Inspection.objects.create(
            project=self.project, inspection_type="Safety Audit", outcome="FAILED")
        result = MonitoringService.evaluate_milestone_gates(self._milestone())
        gate = self._gate(result, "inspections")
        self.assertEqual(gate["status"], "FAILED")
        self.assertTrue(any("1 mandatory site inspection(s) failed" in b for b in result["blockers"]))

    def test_inspection_gate_passes_when_project_inspections_passed(self):
        Inspection.objects.create(
            project=self.project, inspection_type="Safety Audit", outcome="PASSED")
        result = MonitoringService.evaluate_milestone_gates(self._milestone())
        self.assertEqual(self._gate(result, "inspections")["status"], "PASSED")

    def test_defect_gate_fails_on_open_critical_issue(self):
        SiteIssue.objects.create(
            project=self.project, title="Honeycombing in shear wall",
            description="Critical structural defect", severity="CRITICAL", status="OPEN")
        result = MonitoringService.evaluate_milestone_gates(self._milestone())
        gate = self._gate(result, "defects")
        self.assertEqual(gate["status"], "FAILED")
        self.assertTrue(result["is_blocked"])
        self.assertTrue(any("critical safety/structural defect" in b for b in result["blockers"]))

    def test_defect_gate_ignores_resolved_critical_issue(self):
        SiteIssue.objects.create(
            project=self.project, title="Honeycombing in shear wall",
            description="Rectified and re-tested", severity="CRITICAL", status="RESOLVED")
        result = MonitoringService.evaluate_milestone_gates(self._milestone())
        self.assertEqual(self._gate(result, "defects")["status"], "PASSED")

    def test_defect_gate_fails_on_active_stop_work_order(self):
        StopWorkOrder.objects.create(
            project=self.project, reason="Unshored deep excavation adjacent to public way",
            status="ACTIVE")
        result = MonitoringService.evaluate_milestone_gates(self._milestone())
        gate = self._gate(result, "defects")
        self.assertEqual(gate["status"], "FAILED")
        self.assertTrue(any("Stop-Work Order" in b for b in result["blockers"]))

    def test_bim_gate_fails_on_deviation_over_tolerance(self):
        ms = self._milestone(bim_deviation_mm=22.5, bim_tolerance_max_mm=15.0)
        result = MonitoringService.evaluate_milestone_gates(ms)
        gate = self._gate(result, "bim_survey")
        self.assertEqual(gate["status"], "FAILED")
        self.assertTrue(any("BIM deviation (22.5mm vs max 15.0mm)" in b for b in result["blockers"]))

    def test_bim_gate_fails_on_gnss_variance_over_limit(self):
        ms = self._milestone(survey_variance_meters=0.07)
        result = MonitoringService.evaluate_milestone_gates(ms)
        self.assertEqual(self._gate(result, "bim_survey")["status"], "FAILED")
        self.assertTrue(any("GNSS variance (0.07m)" in b for b in result["blockers"]))

    def test_evidence_gate_fails_without_lab_documents(self):
        result = MonitoringService.evaluate_milestone_gates(self._milestone())
        gate = self._gate(result, "evidence_vault")
        self.assertEqual(gate["status"], "FAILED")
        self.assertTrue(any("laboratory test certificates" in b for b in result["blockers"]))
        self.assertFalse(result["all_gates_passed"])

    def test_relaxed_requirements_skip_optional_gates(self):
        ms = self._milestone(verification_requirements={
            "require_inspections_passed": False,
            "require_zero_critical_defects": False,
            "require_survey_within_tolerance": False,
            "require_lab_test_evidence": False,
        })
        Inspection.objects.create(project=self.project, outcome="FAILED")
        StopWorkOrder.objects.create(project=self.project, reason="x", status="ACTIVE")
        result = MonitoringService.evaluate_milestone_gates(ms)
        self.assertTrue(result["all_gates_passed"])
        for gate in result["gates"]:
            if gate["key"] != "dependencies":
                self.assertFalse(gate["required"], gate["key"])

    def test_verify_milestone_blocked_when_gates_fail(self):
        ms = self._milestone()  # no lab evidence attached
        with self.assertRaises(ValueError) as ctx:
            MonitoringService.verify_milestone(ms, data={}, actor=self.officer)
        self.assertIn("Verification gates failed", str(ctx.exception))
        ms.refresh_from_db()
        self.assertNotEqual(ms.status, "VERIFIED")
        self.assertEqual(ms.progress_percentage, 0)

    def test_verify_milestone_override_gate_certifies(self):
        ms = self._milestone()  # gates fail on missing evidence
        verified = MonitoringService.verify_milestone(
            ms, data={"override_gate": True, "notes": "Directorate waiver with lab results pending."},
            actor=self.officer)
        self.assertEqual(verified.status, "VERIFIED")
        self.assertEqual(verified.progress_percentage, 100)
        self.assertIsNotNone(verified.actual_completion_date)
        self.assertEqual(verified.verified_by_name, "Ifeoma Adeyemi")
        self.assertFalse(verified.is_delayed)
        self.assertTrue(verified.verification_signoff["override_applied"])
        self.assertIn("waiver", verified.verification_signoff["notes"])
        self.assertRegex(verified.verification_signoff["signature_hash"], r"^0x[0-9A-F]{24}$")

    def test_verify_milestone_unblocks_blocked_successors(self):
        pred = self._milestone(name="Foundation verification",
                               evidence_documents=[{"name": "soil-report.pdf"}])
        successor = self._milestone(name="Superstructure commence", status="BLOCKED",
                                    sequence_order=2)
        MonitoringService.verify_milestone(pred, data={}, actor=self.officer)
        successor.refresh_from_db()
        self.assertEqual(successor.status, "PLANNED")
        self.assertEqual(successor.progress_percentage, 0)

    def test_submit_milestone_for_verification_sets_pending(self):
        ms = self._milestone(status="IN_PROGRESS", progress_percentage=100)
        submitted = MonitoringService.submit_milestone_for_verification(
            ms, data={"physical_progress_notes": "Awaiting statutory audit."}, user=self.officer)
        self.assertEqual(submitted.status, "PENDING_VERIFICATION")
        self.assertEqual(submitted.physical_progress_notes, "Awaiting statutory audit.")
        self.assertTrue(AuditEvent.objects.filter(
            action="MILESTONE_VERIFICATION_SUBMITTED", resource_id=str(ms.id)).exists())

    def test_update_milestone_progress_status_transitions(self):
        ms = self._milestone(status="PLANNED", progress_percentage=0)
        updated = MonitoringService.update_milestone_progress(
            ms, data={"progress_percentage": 30}, user=self.officer)
        self.assertEqual(updated.status, "IN_PROGRESS")
        self.assertEqual(updated.actual_start_date, datetime.date.today())

        updated = MonitoringService.update_milestone_progress(
            ms, data={"progress_percentage": 100}, user=self.officer)
        self.assertEqual(updated.status, "PENDING_VERIFICATION")
        self.assertNotEqual(updated.status, "VERIFIED")

    def test_update_milestone_progress_clamps_range(self):
        ms = self._milestone(status="PLANNED")
        updated = MonitoringService.update_milestone_progress(
            ms, data={"progress_percentage": -20}, user=self.officer)
        self.assertEqual(updated.progress_percentage, 0)
        updated = MonitoringService.update_milestone_progress(
            ms, data={"progress_percentage": 150}, user=self.officer)
        self.assertEqual(updated.progress_percentage, 100)

    def test_update_milestone_progress_merges_evidence_without_duplicates(self):
        ms = self._milestone(evidence_documents=[
            {"name": "Cube test 7-day.pdf", "url": "https://assets/7day.pdf"}])
        updated = MonitoringService.update_milestone_progress(
            ms, data={
                "progress_percentage": 50,
                "evidence_documents": [
                    {"name": "Cube test 7-day.pdf", "url": "https://assets/duplicate.pdf"},
                    {"name": "Cube test 28-day.pdf", "url": "https://assets/28day.pdf"},
                ],
                "evidence_photos": ["https://assets/site-1.jpg", "https://assets/site-1.jpg"],
            },
            user=self.officer)
        names = [d["name"] for d in updated.evidence_documents]
        self.assertEqual(names.count("Cube test 7-day.pdf"), 1)
        self.assertIn("Cube test 28-day.pdf", names)
        self.assertEqual(updated.evidence_photos, ["https://assets/site-1.jpg"])
        self.assertTrue(AuditEvent.objects.filter(
            action="MILESTONE_PROGRESS_UPDATED", resource_id=str(ms.id)).exists())

    def test_attach_milestone_evidence_appends_documents_and_photos(self):
        ms = self._milestone(evidence_documents=[{"name": "existing.pdf"}])
        updated = MonitoringService.attach_milestone_evidence(
            ms, data={
                "documents": [{"name": "structural-signoff.pdf", "url": "https://assets/s.pdf"}],
                "photos": ["https://assets/beam-1.jpg"],
            },
            user=self.officer)
        self.assertEqual(len(updated.evidence_documents), 2)
        self.assertEqual(updated.evidence_photos, ["https://assets/beam-1.jpg"])
        self.assertTrue(AuditEvent.objects.filter(
            action="MILESTONE_EVIDENCE_ATTACHED", resource_id=str(ms.id)).exists())

    def test_create_milestone_invalid_target_date_is_rejected(self):
        with self.assertRaises(DRFValidationError):
            MonitoringService.create_milestone(
                data={"project_id": self.project.id, "name": "Bad date",
                      "target_date": "31/12/2026"},
                user=self.officer)
        self.assertFalse(ConstructionMilestone.objects.filter(name="Bad date").exists())

    def test_create_milestone_invalid_planned_start_is_dropped_not_invented(self):
        ms = MonitoringService.create_milestone(
            data={"project_id": self.project.id, "name": "Dropped start",
                  "target_date": "2026-10-01", "planned_start_date": "01/09/2026"},
            user=self.officer)
        self.assertIsNone(ms.planned_start_date)
        self.assertEqual(ms.target_date, datetime.date(2026, 10, 1))
        self.assertIsNone(ms.baseline_start_date)

    def test_create_milestone_defaults(self):
        ms = MonitoringService.create_milestone(
            data={"project_id": self.project.id, "name": "Defaulted milestone"},
            user=self.officer)
        self.assertEqual(ms.status, "PLANNED")
        self.assertEqual(ms.progress_percentage, 0)
        self.assertEqual(ms.target_date, datetime.date.today())
        self.assertEqual(ms.milestone_code[:3], "MS-")
        self.assertTrue(ms.verification_requirements["require_engineer_signoff"])
        self.assertTrue(AuditEvent.objects.filter(
            action="CONSTRUCTION_MILESTONE_CREATED", resource_id=str(ms.id)).exists())

    def test_flag_milestone_delay_without_revised_date(self):
        ms = self._milestone(status="IN_PROGRESS")
        flagged = MonitoringService.flag_milestone_delay(
            ms, data={"reason": "Curtain wall fabrication backlog."}, actor=self.officer)
        self.assertEqual(flagged.status, "DELAYED")
        self.assertTrue(flagged.is_delayed)
        self.assertEqual(flagged.risk_level, "HIGH")
        self.assertEqual(flagged.delay_reason, "Curtain wall fabrication backlog.")
        self.assertEqual(flagged.variance_days, 0)
        self.assertTrue(AuditEvent.objects.filter(
            action="CONSTRUCTION_MILESTONE_DELAY_FLAGGED", resource_id=str(ms.id)).exists())

    def test_flag_milestone_delay_invalid_revised_date_keeps_variance(self):
        ms = self._milestone(target_date=datetime.date(2026, 9, 30))
        flagged = MonitoringService.flag_milestone_delay(
            ms, data={"reason": "x", "revised_target_date": "not-a-date"}, actor=self.officer)
        self.assertEqual(flagged.status, "DELAYED")
        # A rejected date must not fabricate a slippage figure.
        self.assertEqual(flagged.variance_days, 0)
        self.assertEqual(flagged.target_date, datetime.date(2026, 9, 30))

    def test_flag_milestone_delay_revised_before_target_clamps_to_one_day(self):
        ms = self._milestone(target_date=datetime.date(2026, 9, 30))
        flagged = MonitoringService.flag_milestone_delay(
            ms, data={"reason": "x", "revised_target_date": "2026-09-20"}, actor=self.officer)
        self.assertEqual(flagged.variance_days, 1)
        self.assertEqual(flagged.target_date, datetime.date(2026, 9, 20))

    def test_get_milestone_audit_trail_returns_lifecycle_events(self):
        ms = MonitoringService.create_milestone(
            data={"project_id": self.project.id, "name": "Audited milestone",
                  "evidence_documents": [{"name": "lab.pdf"}]},
            user=self.officer)
        MonitoringService.update_milestone_progress(
            ms, data={"progress_percentage": 40}, user=self.officer)
        MonitoringService.verify_milestone(ms, data={}, actor=self.officer)

        trail = MonitoringService.get_milestone_audit_trail(ms.id)
        actions = [e["action"] for e in trail]
        self.assertIn("CONSTRUCTION_MILESTONE_CREATED", actions)
        self.assertIn("MILESTONE_PROGRESS_UPDATED", actions)
        self.assertIn("CONSTRUCTION_MILESTONE_VERIFIED", actions)
        verified_event = next(e for e in trail if e["action"] == "CONSTRUCTION_MILESTONE_VERIFIED")
        self.assertEqual(verified_event["user_name"], "Ifeoma Adeyemi")
        self.assertIn("sig_hash", verified_event["new_state"])

    def test_get_milestone_audit_trail_unknown_id_is_empty(self):
        self.assertEqual(MonitoringService.get_milestone_audit_trail(uuid.uuid4()), [])


class IssueServiceExtrasTestCase(TestCase):
    """Issue reporting extras: due-date parsing and stop-work enforcement."""

    def setUp(self):
        self.officer = User.objects.create_superuser(
            username="issue_officer@nexucon.com",
            email="issue_officer@nexucon.com",
            password="Password123!",
            first_name="Emeka", last_name="Okafor",
        )
        self.project = Project.objects.create(
            name="Ajah Logistics Hub", project_type="Industrial", status="ACTIVE")

    def test_report_issue_parses_iso_due_date(self):
        issue = MonitoringService.report_issue(
            data={"project_id": self.project.id, "title": "Missing perimeter fence",
                  "description": "Site boundary unfenced.", "due_date": "2026-09-30T00:00:00Z"},
            user=self.officer)
        self.assertEqual(issue.due_date, "2026-09-30")

    def test_report_issue_rejects_malformed_due_date(self):
        issue = MonitoringService.report_issue(
            data={"project_id": self.project.id, "title": "Bad date",
                  "description": "x", "due_date": "30/09/2026"},
            user=self.officer)
        self.assertIsNone(issue.due_date)

    def test_report_issue_enforcing_stop_work_creates_swo_and_suspends_project(self):
        issue = MonitoringService.report_issue(
            data={"project_id": self.project.id, "title": "Unshored excavation collapse risk",
                  "description": "5m deep excavation unsupported adjacent to roadway.",
                  "enforce_stop_work": True},
            user=self.officer)
        self.assertEqual(issue.severity, "CRITICAL")
        self.assertTrue(issue.is_escalated)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "SUSPENDED")
        swo = StopWorkOrder.objects.get(project=self.project, status="ACTIVE")
        self.assertEqual(swo.severity, "CRITICAL")
        self.assertEqual(swo.issued_by_name, "Emeka Okafor")

    def test_report_issue_does_not_duplicate_existing_swo(self):
        StopWorkOrder.objects.create(
            project=self.project, reason="Pre-existing enforcement", status="ACTIVE")
        MonitoringService.report_issue(
            data={"project_id": self.project.id, "title": "Second breach",
                  "description": "y", "enforce_stop_work": True},
            user=self.officer)
        self.assertEqual(StopWorkOrder.objects.filter(project=self.project).count(), 1)

    def test_report_issue_without_enforcement_keeps_severity(self):
        issue = MonitoringService.report_issue(
            data={"project_id": self.project.id, "title": "Minor cracking",
                  "description": "x", "severity": "LOW"},
            user=self.officer)
        self.assertEqual(issue.severity, "LOW")
        self.assertFalse(issue.is_escalated)
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, "ACTIVE")


# ===========================================================================
# Site verification extras: telemetry integrity, certification, audit trail
# ===========================================================================

class SiteVerificationServiceExtrasTestCase(TestCase):
    def setUp(self):
        self.officer = User.objects.create_superuser(
            username="vrf_officer@nexucon.com",
            email="vrf_officer@nexucon.com",
            password="Password123!",
            first_name="Tobi", last_name="Salami",
        )
        self.project = Project.objects.create(
            name="Oniru Beach Apartments", project_type="Residential", status="ACTIVE")

    def _verification(self, **extra):
        data = {
            "project_id": self.project.id,
            "captured_coordinates": {"lat": 6.4281000, "lng": 3.4219000, "elevation": 4.10},
            "approved_coordinates": {"lat": 6.4281000, "lng": 3.4219000, "elevation": 4.10},
        }
        data.update(extra)
        return MonitoringService.record_site_verification(data, user=self.officer)

    def test_recorded_telemetry_is_the_device_payload(self):
        vrf = self._verification(telemetry_data={
            "satellites_tracked": 31, "hdop": 0.6, "vdop": 0.9,
            "rtk_fix_status": "FIXED", "base_station_ref": "LASG-CORS-ONIRU-02"})
        self.assertEqual(vrf.telemetry_data["satellites_tracked"], 31)
        self.assertEqual(vrf.telemetry_data["base_station_ref"], "LASG-CORS-ONIRU-02")

    def test_missing_telemetry_is_recorded_as_empty_not_invented(self):
        vrf = self._verification()
        self.assertEqual(vrf.telemetry_data, {})
        self.assertNotIn("satellites_tracked", vrf.telemetry_data)
        self.assertNotIn("rtk_fix_status", vrf.telemetry_data)

    def test_get_verification_telemetry_returns_stored_payload(self):
        vrf = self._verification(telemetry_data={"satellites_tracked": 12})
        telemetry = MonitoringService.get_verification_telemetry(vrf.id)
        self.assertEqual(telemetry, {"satellites_tracked": 12})

    def test_get_verification_telemetry_never_fabricates_readings(self):
        vrf = self._verification()
        telemetry = MonitoringService.get_verification_telemetry(vrf.id)
        self.assertEqual(telemetry, {})
        self.assertNotIn("hdop", telemetry)

    def test_get_verification_telemetry_unknown_id_returns_empty(self):
        self.assertEqual(MonitoringService.get_verification_telemetry(uuid.uuid4()), {})

    def test_elevation_variance_is_computed(self):
        vrf = self._verification(
            captured_coordinates={"lat": 6.4281000, "lng": 3.4219000, "elevation": 4.10},
            approved_coordinates={"lat": 6.4281000, "lng": 3.4219000, "elevation": 4.35})
        self.assertEqual(vrf.elevation_variance_meters, 0.25)
        self.assertFalse(vrf.variance_detected)

    def test_explicit_variance_overrides_computed_value(self):
        vrf = self._verification(variance_meters=0.42, elevation_variance_meters=0.9)
        self.assertEqual(vrf.variance_meters, 0.42)
        self.assertEqual(vrf.elevation_variance_meters, 0.9)
        self.assertTrue(vrf.variance_detected)
        self.assertEqual(vrf.status, "VARIANCE_DETECTED")

    def test_encroachment_within_tolerance_still_flags_variance(self):
        vrf = self._verification(encroachment_detected=True,
                                 encroachment_details="North setback reduced by 0.8m")
        self.assertEqual(vrf.variance_meters, 0.0)
        self.assertTrue(vrf.variance_detected)
        self.assertEqual(vrf.status, "VARIANCE_DETECTED")

    def test_certify_blocked_by_detected_variance(self):
        vrf = self._verification(variance_meters=0.30)
        with self.assertRaises(ValueError) as ctx:
            MonitoringService.certify_site_verification(vrf, data={}, actor=self.officer)
        self.assertIn("Cannot certify site verification", str(ctx.exception))
        vrf.refresh_from_db()
        self.assertNotEqual(vrf.status, "VERIFIED")
        self.assertIsNone(vrf.digital_cert_ref)

    def test_certify_with_tolerance_override_issues_certificate_and_seal(self):
        vrf = self._verification(variance_meters=0.06)
        certified = MonitoringService.certify_site_verification(
            vrf, data={"override_tolerance": True,
                       "verified_by_name": "Surv. Ada Umeh",
                       "notes": "Variance accepted within surveyor tolerance waiver."},
            actor=self.officer)
        self.assertEqual(certified.status, "VERIFIED")
        self.assertTrue(certified.digital_cert_ref.startswith("CERT-VRF-"))
        self.assertRegex(certified.signature_hash, r"^0xLASBCA-VRF-SURV-[0-9A-F]{16}$")
        self.assertEqual(certified.verified_by_name, "Surv. Ada Umeh")
        self.assertIsNotNone(certified.verified_at)

    def test_verification_audit_trail_records_recording_and_certification(self):
        vrf = self._verification()
        MonitoringService.certify_site_verification(vrf, data={}, actor=self.officer)
        trail = MonitoringService.get_verification_audit_trail(vrf.id)
        actions = [e["action"] for e in trail]
        self.assertIn("SITE_VERIFICATION_RECORDED", actions)
        self.assertIn("SITE_VERIFICATION_CERTIFIED", actions)
        certified_event = next(e for e in trail if e["action"] == "SITE_VERIFICATION_CERTIFIED")
        self.assertIn("signature_hash", certified_event["new_state"])
        self.assertEqual(certified_event["user_name"], "Tobi Salami")

    def test_attach_verification_evidence_appends_documents(self):
        vrf = self._verification(evidence_documents=[{"name": "survey-plan.pdf"}])
        updated = MonitoringService.attach_verification_evidence(
            vrf, data={"documents": [{"name": "rinex-log.obs"}],
                       "photos": ["https://assets/beacon-1.jpg"]},
            actor=self.officer)
        self.assertEqual(len(updated.evidence_documents), 2)
        self.assertEqual(updated.evidence_photos, ["https://assets/beacon-1.jpg"])
        self.assertTrue(AuditEvent.objects.filter(
            action="SITE_VERIFICATION_EVIDENCE_ATTACHED", resource_id=str(vrf.id)).exists())

    def test_flag_site_encroachment_sets_flagged_state_and_audit(self):
        vrf = self._verification()
        flagged = MonitoringService.flag_site_encroachment(
            vrf, data={"reason": "Eastern boundary wall over planning line",
                       "details": "0.6m into adjoining setback reserve"},
            actor=self.officer)
        self.assertEqual(flagged.status, "FLAGGED")
        self.assertTrue(flagged.encroachment_detected)
        self.assertTrue(flagged.variance_detected)
        self.assertIn("Eastern boundary wall", flagged.encroachment_details)
        self.assertIn("0.6m", flagged.encroachment_details)
        event = AuditEvent.objects.get(
            action="SITE_ENCROACHMENT_FLAGGED", resource_id=str(vrf.id))
        self.assertEqual(event.previous_state["status"], "VERIFIED")


# ===========================================================================
# Project progress details aggregation
# ===========================================================================

class ProjectProgressDetailsTestCase(TestCase):
    def setUp(self):
        self.officer = User.objects.create_superuser(
            username="progress_officer@nexucon.com",
            email="progress_officer@nexucon.com",
            password="Password123!",
        )
        self.project = Project.objects.create(
            name="Ikeja Data Centre Phase 2", project_type="Industrial", status="ACTIVE")
        self.other_project = Project.objects.create(
            name="Draft Blueprint Scheme", project_type="Commercial", status="DRAFT")

    def _update(self, **kwargs):
        defaults = dict(project=self.project, work_summary="Field update.")
        defaults.update(kwargs)
        return DailySiteUpdate.objects.create(**defaults)

    def test_details_report_only_real_field_data(self):
        self._update(
            update_type="DAILY_PHOTO",
            work_summary="Level 2 slab poured.",
            progress_percentage=55,
            workforce_count=38,
            weather_condition="Humid / 29C",
            photos=["https://assets/slab-1.jpg"],
        )
        details = MonitoringService.get_project_progress_details(self.project.id)
        self.assertEqual(details["project_name"], "Ikeja Data Centre Phase 2")
        self.assertEqual(details["verified_progress"], 55)
        self.assertEqual(details["workforce_on_site"], 38)
        self.assertEqual(details["weather_condition"], "Humid / 29C")
        self.assertEqual(details["total_photos_count"], 1)
        self.assertEqual(details["photos"][0]["url"], "https://assets/slab-1.jpg")
        self.assertIsNotNone(details["latest_update"])
        self.assertEqual(details["schedule_status"], "ON_SCHEDULE")

    def test_details_without_updates_report_absence_not_invented_figures(self):
        details = MonitoringService.get_project_progress_details(self.project.id)
        self.assertEqual(details["verified_progress"], 0)
        self.assertIsNone(details["workforce_on_site"])
        self.assertIsNone(details["weather_condition"])
        self.assertIsNone(details["latest_update"])
        self.assertEqual(details["total_photos_count"], 0)
        self.assertEqual(details["milestones_total"], 0)
        self.assertEqual(details["schedule_status"], "ON_SCHEDULE")

    def test_details_critical_delay_with_critical_issue(self):
        self._update(progress_percentage=80)
        SiteIssue.objects.create(
            project=self.project, title="Structural crack", description="x",
            severity="CRITICAL", status="OPEN")
        details = MonitoringService.get_project_progress_details(self.project.id)
        self.assertEqual(details["schedule_status"], "CRITICAL_DELAY")
        self.assertEqual(details["schedule_label"], "Critical Schedule Delay")

    def test_details_minor_delay_with_single_delayed_milestone(self):
        self._update(progress_percentage=50)
        ConstructionMilestone.objects.create(
            project=self.project, name="MEP rough-in", status="DELAYED", is_delayed=True,
            target_date=timezone.now().date())
        details = MonitoringService.get_project_progress_details(self.project.id)
        self.assertEqual(details["schedule_status"], "MINOR_DELAY")
        self.assertEqual(details["milestones_delayed"], 1)

    def test_details_ahead_of_schedule_at_high_progress(self):
        self._update(progress_percentage=90)
        details = MonitoringService.get_project_progress_details(self.project.id)
        self.assertEqual(details["schedule_status"], "AHEAD")

    def test_details_milestone_and_photo_rollup(self):
        ConstructionMilestone.objects.create(
            project=self.project, name="Substructure", status="VERIFIED",
            progress_percentage=100,
            target_date=timezone.now().date() - datetime.timedelta(days=10))
        ConstructionMilestone.objects.create(
            project=self.project, name="Frame", status="IN_PROGRESS", progress_percentage=40,
            target_date=timezone.now().date() + datetime.timedelta(days=10))
        self._update(update_type="DAILY_PHOTO", photos=["https://assets/a.jpg", "https://assets/b.jpg"])
        self._update(update_type="DAILY_PHOTO", photos=["https://assets/a.jpg"])

        details = MonitoringService.get_project_progress_details(self.project.id)
        self.assertEqual(details["milestones_total"], 2)
        self.assertEqual(details["milestones_verified"], 1)
        # The duplicate photo URL is flattened to a single feed entry.
        self.assertEqual(details["total_photos_count"], 2)
        self.assertEqual(len(details["progress_history"]), 2)
        self.assertEqual(len(details["phases"]), 5)

    def test_details_without_project_id_covers_active_projects_only(self):
        self._update(progress_percentage=10)
        results = MonitoringService.get_project_progress_details()
        self.assertIsInstance(results, list)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["project_name"], "Ikeja Data Centre Phase 2")

    def test_details_accepts_explicit_project_queryset(self):
        self._update(progress_percentage=10)
        results = MonitoringService.get_project_progress_details(
            projects=Project.objects.filter(id=self.other_project.id))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["project_name"], "Draft Blueprint Scheme")

    def test_details_unknown_project_raises(self):
        with self.assertRaises(DRFValidationError):
            MonitoringService.get_project_progress_details("ghost-project")


# ===========================================================================
# API authentication: every monitoring endpoint requires authentication
# ===========================================================================

class MonitoringAuthRequiredTestCase(APITestCase):
    """All monitoring endpoints must reject unauthenticated requests with 401."""

    LIST_URL_NAMES = [
        'site-update-list', 'missed-site-visit-list', 'field-observation-list',
        'site-issue-list', 'construction-milestone-list', 'site-verification-list',
        'monitoring-stats-list', 'site-progress-list',
    ]

    def test_list_endpoints_require_authentication(self):
        for name in self.LIST_URL_NAMES:
            response = self.client.get(reverse(name))
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED, name)

    def test_detail_endpoints_require_authentication(self):
        for name in ('site-update-detail', 'construction-milestone-detail',
                     'site-verification-detail', 'missed-site-visit-detail'):
            response = self.client.get(reverse(name, kwargs={'pk': str(uuid.uuid4())}))
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED, name)

    def test_write_actions_require_authentication(self):
        pk = str(uuid.uuid4())
        posts = [
            ('site-update-list', None),
            ('missed-site-visit-list', None),
            ('field-observation-list', None),
            ('site-issue-list', None),
            ('construction-milestone-list', None),
            ('site-verification-list', None),
            ('site-update-calculate-location', None),
            ('site-progress-update-progress', None),
            ('site-progress-flag-delay', None),
            ('missed-site-visit-acknowledge', {'pk': pk}),
            ('construction-milestone-verify', {'pk': pk}),
            ('construction-milestone-update-progress', {'pk': pk}),
            ('site-verification-certify', {'pk': pk}),
            ('site-issue-escalate', {'pk': pk}),
            ('field-observation-resolve', {'pk': pk}),
        ]
        for name, kwargs in posts:
            response = self.client.post(reverse(name, kwargs=kwargs), {}, format='json')
            self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED, name)


# ===========================================================================
# Tenant scoping: client developers only reach their own projects
# ===========================================================================

class MonitoringTenantScopingTestCase(APITestCase):
    """Monitoring endpoints scope through common.permissions.scoped_projects:
    a client developer must only list/create/act on records tied to their own
    developer organization's projects."""

    def setUp(self):
        self.client_a = User.objects.create_user(
            username="client.a@alpha.dev", email="client.a@alpha.dev", password="Password123!",
            first_name="Alpha", last_name="Client")
        self.client_b = User.objects.create_user(
            username="client.b@beta.dev", email="client.b@beta.dev", password="Password123!",
            first_name="Beta", last_name="Client")
        Developer.objects.create(user=self.client_a, name="Alpha Developments")
        Developer.objects.create(user=self.client_b, name="Beta Developments")

        self.project_a = Project.objects.create(
            name="Alpha Tower", project_type="Commercial", status="ACTIVE",
            developer_organization="Alpha Developments")
        self.project_b = Project.objects.create(
            name="Beta Estate", project_type="Residential", status="ACTIVE",
            developer_organization="Beta Developments")

        self.update_a = DailySiteUpdate.objects.create(
            project=self.project_a, work_summary="Alpha update", update_type="DAILY_PHOTO")
        self.visit_a = MissedSiteVisitRecord.objects.create(
            project=self.project_a, justification_notes="Alpha visit missed")
        self.obs_a = FieldObservation.objects.create(
            project=self.project_a, title="Alpha observation", description="d")
        self.issue_a = SiteIssue.objects.create(
            project=self.project_a, title="Alpha issue", description="d")
        self.milestone_a = ConstructionMilestone.objects.create(
            project=self.project_a, name="Alpha milestone",
            target_date=timezone.now().date() + datetime.timedelta(days=10))
        self.vrf_a = SiteVerification.objects.create(project=self.project_a)

        self.update_b = DailySiteUpdate.objects.create(
            project=self.project_b, work_summary="Beta update", update_type="DAILY_PHOTO")
        self.visit_b = MissedSiteVisitRecord.objects.create(
            project=self.project_b, justification_notes="Beta visit missed")
        self.obs_b = FieldObservation.objects.create(
            project=self.project_b, title="Beta observation", description="d")
        self.issue_b = SiteIssue.objects.create(
            project=self.project_b, title="Beta issue", description="d")
        self.milestone_b = ConstructionMilestone.objects.create(
            project=self.project_b, name="Beta milestone",
            target_date=timezone.now().date() + datetime.timedelta(days=10))
        self.vrf_b = SiteVerification.objects.create(project=self.project_b)

    def _authenticate(self, user):
        refresh = RefreshToken.for_user(user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")

    def test_scoped_projects_contains_only_own_developers_projects(self):
        scoped_a = scoped_projects(self.client_a)
        self.assertIn(self.project_a, scoped_a)
        self.assertNotIn(self.project_b, scoped_a)

    def test_lists_only_own_project_records(self):
        self._authenticate(self.client_a)
        expected = {
            'site-update-list': (self.update_a, self.update_b),
            'missed-site-visit-list': (self.visit_a, self.visit_b),
            'field-observation-list': (self.obs_a, self.obs_b),
            'site-issue-list': (self.issue_a, self.issue_b),
            'construction-milestone-list': (self.milestone_a, self.milestone_b),
            'site-verification-list': (self.vrf_a, self.vrf_b),
        }
        for url_name, (own, other) in expected.items():
            response = self.client.get(reverse(url_name))
            self.assertEqual(response.status_code, status.HTTP_200_OK, url_name)
            returned_ids = {str(row['id']) for row in response.data}
            self.assertIn(str(own.id), returned_ids, url_name)
            self.assertNotIn(str(other.id), returned_ids, url_name)

    def test_detail_cross_tenant_access_returns_404(self):
        self._authenticate(self.client_a)
        targets = {
            'site-update-detail': self.update_b,
            'missed-site-visit-detail': self.visit_b,
            'field-observation-detail': self.obs_b,
            'site-issue-detail': self.issue_b,
            'construction-milestone-detail': self.milestone_b,
            'site-verification-detail': self.vrf_b,
        }
        for url_name, obj in targets.items():
            response = self.client.get(reverse(url_name, kwargs={'pk': str(obj.id)}))
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, url_name)

    def test_write_actions_on_other_tenants_records_return_404(self):
        self._authenticate(self.client_a)
        post_targets = [
            ('construction-milestone-verify', self.milestone_b),
            ('construction-milestone-update-progress', self.milestone_b),
            ('construction-milestone-flag-delay', self.milestone_b),
            ('construction-milestone-attach-evidence', self.milestone_b),
            ('construction-milestone-submit-verification', self.milestone_b),
            ('site-verification-certify', self.vrf_b),
            ('site-verification-flag-encroachment', self.vrf_b),
            ('site-verification-attach-evidence', self.vrf_b),
            ('missed-site-visit-acknowledge', self.visit_b),
            ('field-observation-resolve', self.obs_b),
            ('site-issue-escalate', self.issue_b),
            ('site-issue-resolve', self.issue_b),
            ('site-update-telemetry', self.update_b),
        ]
        for url_name, obj in post_targets:
            response = self.client.post(
                reverse(url_name, kwargs={'pk': str(obj.id)}), {}, format='json')
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND,
                             f"{url_name} must be out of scope for another client")
        get_targets = [
            ('construction-milestone-gate-status', self.milestone_b),
            ('construction-milestone-audit-trail', self.milestone_b),
            ('site-verification-telemetry', self.vrf_b),
            ('site-verification-audit-trail', self.vrf_b),
        ]
        for url_name, obj in get_targets:
            response = self.client.get(reverse(url_name, kwargs={'pk': str(obj.id)}))
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND,
                             f"{url_name} must be out of scope for another client")
        self.milestone_b.refresh_from_db()
        self.assertNotEqual(self.milestone_b.status, 'VERIFIED')
        self.assertEqual(self.milestone_b.status, 'PLANNED')

    def test_create_on_other_tenants_project_returns_404_and_writes_nothing(self):
        self._authenticate(self.client_a)
        payload = {"project_id": str(self.project_b.id), "work_summary": "Intrusion attempt."}
        response = self.client.post(reverse('site-update-list'), payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(DailySiteUpdate.objects.filter(project=self.project_b).count(), 1)

        response = self.client.post(reverse('construction-milestone-list'),
                                    {"project_id": str(self.project_b.id), "name": "x"},
                                    format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(
            ConstructionMilestone.objects.filter(project=self.project_b, name="x").exists())

    def test_create_on_all_viewsets_rejects_other_tenants_project(self):
        self._authenticate(self.client_a)
        posts = [
            ('missed-site-visit-list',
             {"project_id": str(self.project_b.id), "justification_notes": "intrusion"}),
            ('field-observation-list',
             {"project_id": str(self.project_b.id), "title": "intrusion", "description": "d"}),
            ('site-issue-list',
             {"project_id": str(self.project_b.id), "title": "intrusion", "description": "d"}),
            ('site-verification-list',
             {"project_id": str(self.project_b.id)}),
        ]
        before = {
            MissedSiteVisitRecord: MissedSiteVisitRecord.objects.filter(project=self.project_b).count(),
            FieldObservation: FieldObservation.objects.filter(project=self.project_b).count(),
            SiteIssue: SiteIssue.objects.filter(project=self.project_b).count(),
            SiteVerification: SiteVerification.objects.filter(project=self.project_b).count(),
        }
        for url_name, payload in posts:
            response = self.client.post(reverse(url_name), payload, format='json')
            self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, url_name)
        self.assertEqual(
            MissedSiteVisitRecord.objects.filter(project=self.project_b).count(),
            before[MissedSiteVisitRecord])
        self.assertEqual(
            FieldObservation.objects.filter(project=self.project_b).count(),
            before[FieldObservation])
        self.assertEqual(
            SiteIssue.objects.filter(project=self.project_b).count(), before[SiteIssue])
        self.assertEqual(
            SiteVerification.objects.filter(project=self.project_b).count(),
            before[SiteVerification])

    def test_client_can_create_on_own_project(self):
        self._authenticate(self.client_a)
        response = self.client.post(
            reverse('site-update-list'),
            {"project_id": str(self.project_a.id),
             "work_summary": "Alpha daily log.",
             "update_type": "PROGRESS_REPORT",
             "progress_percentage": 20},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(
            DailySiteUpdate.objects.filter(project=self.project_a).count(), 2)

    def test_stats_overview_only_counts_own_projects(self):
        ConstructionMilestone.objects.create(
            project=self.project_b, name="Beta delayed milestone", status="DELAYED",
            is_delayed=True, target_date=timezone.now().date())
        self._authenticate(self.client_a)
        response = self.client.get(reverse('monitoring-stats-overview'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data['data']
        self.assertEqual(data['milestones']['total'], 1)
        self.assertEqual(data['milestones']['delayed'], 0)
        self.assertEqual(data['live']['active_sites'], 1)
        self.assertEqual(data['progress']['progress_reports'], 1)

    def test_stats_overview_counts_pre_construction_projects_as_sites(self):
        # A client whose only project is not yet under construction still sees
        # it counted through the pre-construction fallback.
        Project.objects.create(
            name="Alpha Planning Scheme", project_type="Commercial", status="PLANNING",
            developer_organization="Alpha Developments")
        DailySiteUpdate.objects.filter(project=self.project_a).delete()
        self._authenticate(self.client_a)
        response = self.client.get(reverse('monitoring-stats-overview'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['live']['active_sites'], 1)

    def test_site_progress_list_is_scoped(self):
        self._authenticate(self.client_a)
        response = self.client.get(reverse('site-progress-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = {row['project_name'] for row in response.data['data']}
        self.assertEqual(names, {"Alpha Tower"})

    def test_site_progress_retrieve_other_tenants_project_returns_404(self):
        self._authenticate(self.client_a)
        response = self.client.get(
            reverse('site-progress-detail', kwargs={'pk': str(self.project_b.id)}))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_site_progress_update_out_of_scope_returns_404(self):
        self._authenticate(self.client_a)
        response = self.client.post(
            reverse('site-progress-update-progress'),
            {"project_id": str(self.project_b.id), "progress_percentage": 50},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(DailySiteUpdate.objects.filter(
            project=self.project_b, update_type='PROGRESS_REPORT').count(), 0)

    def test_site_progress_flag_delay_out_of_scope_returns_404(self):
        self._authenticate(self.client_a)
        response = self.client.post(
            reverse('site-progress-flag-delay'),
            {"project_id": str(self.project_b.id), "reason": "cross-tenant delay"},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(SiteIssue.objects.filter(project=self.project_b).count(), 1)

    def test_site_progress_update_own_project_succeeds(self):
        self._authenticate(self.client_a)
        response = self.client.post(
            reverse('site-progress-update-progress'),
            {"project_id": str(self.project_a.id), "progress_percentage": 35,
             "work_summary": "Alpha frame erection ongoing."},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['verified_progress'], 35)


# ===========================================================================
# API flows (authenticated superuser): create / list / filter / verify
# ===========================================================================

class MonitoringAPIFlowsTestCase(APITestCase):
    def setUp(self):
        self.officer = User.objects.create_superuser(
            username="flow_officer@nexucon.com",
            email="flow_officer@nexucon.com",
            password="Password123!",
            first_name="Femi", last_name="Adebayo",
        )
        self.project = Project.objects.create(
            name="Yaba Tech Cluster Block C", project_type="Commercial", status="ACTIVE",
            site_address="12 Herbert Macaulay Way", lga="Lagos Mainland")
        refresh = RefreshToken.for_user(self.officer)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {refresh.access_token}")
        self.today = timezone.now().date()

    # -- Daily site updates -------------------------------------------------

    def test_create_daily_update_via_api(self):
        response = self.client.post(
            reverse('site-update-list'),
            {
                "project_id": str(self.project.id),
                "update_type": "DRONE_SURVEY",
                "work_summary": "Drone photogrammetry flight over level 5.",
                "progress_percentage": 40,
                "photos": ["https://assets.nexucon.com/flight-1.jpg",
                           "https://assets.nexucon.com/flight-2.jpg"],
                "workforce_count": 30,
                "inspector_name": "Insp. Bala Yusuf",
                "inspector_badge": "BC-1188",
                "gps_coordinates": {"lat": 6.4281, "lng": 3.4219},
            },
            format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(response.data['data']['update_reference'].startswith('UPD-'))
        update = DailySiteUpdate.objects.get(id=response.data['data']['id'])
        self.assertEqual(update.inspector, self.officer)
        self.assertEqual(update.update_type, 'DRONE_SURVEY')
        self.assertEqual(len(update.photos), 2)
        self.assertTrue(AuditEvent.objects.filter(
            action="DAILY_UPDATE_LOGGED", resource_id=str(update.id)).exists())

    def test_create_daily_update_without_project_returns_400(self):
        response = self.client.post(
            reverse('site-update-list'), {"work_summary": "no project"}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data['success'])

    def test_create_daily_update_unknown_project_returns_400(self):
        response = self.client.post(
            reverse('site-update-list'),
            {"project_id": "ghost-project", "work_summary": "x"}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data['success'])

    def test_daily_update_list_filters(self):
        MonitoringService.log_daily_update(
            data={"project_id": self.project.id, "update_type": "DAILY_PHOTO",
                  "work_summary": "Daily photo log.", "inspector_name": "Insp. Musa",
                  "inspector_badge": "BC-777"}, user=self.officer)
        MonitoringService.log_daily_update(
            data={"project_id": self.project.id, "update_type": "DRONE_SURVEY",
                  "work_summary": "Drone sweep.", "status": "Flagged"}, user=self.officer)
        MonitoringService.log_daily_update(
            data={"project_id": self.project.id, "update_type": "PROGRESS_REPORT",
                  "work_summary": "Progress report.", "status": "Approved"}, user=self.officer)

        base = reverse('site-update-list')
        self.assertEqual(
            len(self.client.get(base, {'type': 'drone_survey'}).data), 1)
        self.assertEqual(
            len(self.client.get(base, {'status': 'Flagged'}).data), 1)
        self.assertEqual(
            len(self.client.get(base, {'inspector': 'Musa'}).data), 1)
        self.assertEqual(
            len(self.client.get(base, {'search': 'Yaba Tech Cluster'}).data), 3)
        self.assertEqual(
            len(self.client.get(base, {'date': str(self.today)}).data), 3)
        self.assertEqual(
            len(self.client.get(base, {'project': str(self.project.id)}).data), 3)
        self.assertEqual(
            len(self.client.get(base, {'date': '2020-01-01'}).data), 0)

    def test_daily_update_telemetry_get_and_sync(self):
        update = MonitoringService.log_daily_update(
            data={"project_id": self.project.id, "work_summary": "GPS tagged.",
                  "gps_coordinates": {"lat": 6.4281, "lng": 3.4219}},
            user=self.officer)
        detail_url = reverse('site-update-telemetry', kwargs={'pk': str(update.id)})

        response = self.client.get(detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('google.com/maps', response.data['data']['google_maps_url'])

        response = self.client.post(
            detail_url, {"satellites_tracked": 22, "rtk_fix_status": "FIXED"}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        update.refresh_from_db()
        self.assertEqual(update.gps_coordinates['satellites_tracked'], 22)
        self.assertTrue(AuditEvent.objects.filter(
            action="DAILY_UPDATE_TELEMETRY_SYNCED", resource_id=str(update.id)).exists())

    def test_calculate_location_action(self):
        url = reverse('site-update-calculate-location')
        response = self.client.post(
            url,
            {"latitude": 6.4281, "longitude": 3.4219, "accuracy": 4.2,
             "setback_measured_meters": 6.0, "setback_target_meters": 5.0,
             "project_id": str(self.project.id)},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['setback_status'], 'PASS')
        self.assertEqual(response.data['data']['accuracy_meters'], 4.2)

        response = self.client.post(url, {"latitude": None, "longitude": None}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # -- Missed site visits -------------------------------------------------

    def test_missed_visit_create_and_acknowledge_via_api(self):
        response = self.client.post(
            reverse('missed-site-visit-list'),
            {"project_id": str(self.project.id), "reason_category": "ACCESS_DENIED",
             "justification_notes": "Site gate locked by developer."},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        record_id = response.data['data']['id']
        self.assertTrue(response.data['data']['record_reference'].startswith('MSV-'))

        response = self.client.post(
            reverse('missed-site-visit-acknowledge', kwargs={'pk': record_id}),
            {"status": "JUSTIFIED", "supervisor_acknowledgment": "Reviewed and accepted."},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        record = MissedSiteVisitRecord.objects.get(id=record_id)
        self.assertEqual(record.status, 'JUSTIFIED')
        self.assertEqual(record.supervisor_acknowledged_by, self.officer)
        self.assertIsNotNone(record.acknowledged_at)

    def test_missed_visit_acknowledge_unknown_returns_404(self):
        response = self.client.post(
            reverse('missed-site-visit-acknowledge', kwargs={'pk': str(uuid.uuid4())}),
            {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_missed_visit_list_filters(self):
        MonitoringService.log_missed_site_visit(
            data={"project_id": self.project.id, "reason_category": "ADVERSE_WEATHER",
                  "justification_notes": "Heavy downpour all day."}, user=self.officer)
        MonitoringService.log_missed_site_visit(
            data={"project_id": self.project.id, "reason_category": "ACCESS_DENIED",
                  "justification_notes": "Gate locked."}, user=self.officer)
        base = reverse('missed-site-visit-list')
        self.assertEqual(len(self.client.get(base, {'reason': 'ACCESS_DENIED'}).data), 1)
        self.assertEqual(len(self.client.get(base, {'status': 'SUBMITTED'}).data), 2)
        self.assertEqual(len(self.client.get(base, {'search': 'downpour'}).data), 1)
        self.assertEqual(len(self.client.get(base, {'date': str(self.today)}).data), 2)
        self.assertEqual(len(self.client.get(base, {'scheduled_date': str(self.today)}).data), 2)
        self.assertEqual(
            len(self.client.get(base, {'project': str(self.project.id)}).data), 2)

    # -- Field observations -------------------------------------------------

    def test_observation_create_resolve_and_filters(self):
        response = self.client.post(
            reverse('field-observation-list'),
            {"project_id": str(self.project.id), "category": "NOT_A_CATEGORY",
             "title": "Missing edge protection", "description": "Open sides at level 4.",
             "severity": "HIGH"},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        # Unknown categories fall back to GENERAL rather than being stored raw.
        self.assertEqual(response.data['data']['category'], 'GENERAL')
        obs_id = response.data['data']['id']

        response = self.client.post(
            reverse('field-observation-resolve', kwargs={'pk': obs_id}),
            {"notes": "Edge protection installed and verified."},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['status'], 'RESOLVED')

    def test_observation_list_filters(self):
        MonitoringService.log_observation(
            data={"project_id": self.project.id, "category": "SAFETY",
                  "title": "Scaffold netting gap", "description": "d",
                  "severity": "HIGH", "status": "OPEN"}, user=self.officer)
        MonitoringService.log_observation(
            data={"project_id": self.project.id, "category": "QUALITY",
                  "title": "Honeycomb finish", "description": "d",
                  "severity": "LOW", "status": "RESOLVED"}, user=self.officer)
        base = reverse('field-observation-list')
        self.assertEqual(len(self.client.get(base, {'category': 'SAFETY'}).data), 1)
        self.assertEqual(len(self.client.get(base, {'severity': 'HIGH'}).data), 1)
        self.assertEqual(len(self.client.get(base, {'status': 'RESOLVED'}).data), 1)
        self.assertEqual(len(self.client.get(base, {'search': 'honeycomb'}).data), 1)
        self.assertEqual(
            len(self.client.get(base, {'project': str(self.project.id)}).data), 2)

    # -- Site issues ---------------------------------------------------------

    def test_issue_create_escalate_resolve_via_api(self):
        response = self.client.post(
            reverse('site-issue-list'),
            {"project_id": str(self.project.id), "title": "Unapproved drainage connection",
             "description": "Storm drain tapped without permit.", "severity": "HIGH",
             "due_date": "2026-09-30T00:00:00Z"},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        issue_id = response.data['data']['id']
        self.assertEqual(response.data['data']['due_date'], '2026-09-30')

        response = self.client.post(
            reverse('site-issue-escalate', kwargs={'pk': issue_id}), {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['data']['is_escalated'])
        self.assertEqual(response.data['data']['status'], 'UNDER_REVIEW')

        response = self.client.post(
            reverse('site-issue-resolve', kwargs={'pk': issue_id}),
            {"notes": "Permit obtained, connection regularized.",
             "evidence": ["https://assets/permit-scan.pdf"]},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['status'], 'RESOLVED')
        self.assertEqual(response.data['data']['resolution_evidence'],
                         ["https://assets/permit-scan.pdf"])

    def test_issue_stop_work_enforcement_via_api(self):
        response = self.client.post(
            reverse('site-issue-list'),
            {"project_id": str(self.project.id), "title": "Unsafe excavation",
             "description": "Unshored 5m trench beside public walkway.",
             "enforce_stop_work": True},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['data']['severity'], 'CRITICAL')
        self.project.refresh_from_db()
        self.assertEqual(self.project.status, 'SUSPENDED')
        self.assertTrue(
            StopWorkOrder.objects.filter(project=self.project, status='ACTIVE').exists())

    def test_issue_list_filters(self):
        MonitoringService.report_issue(
            data={"project_id": self.project.id, "title": "Cracked column",
                  "description": "d", "severity": "HIGH", "status": "OPEN"}, user=self.officer)
        MonitoringService.report_issue(
            data={"project_id": self.project.id, "title": "Surface ponding",
                  "description": "d", "severity": "LOW", "status": "RESOLVED"}, user=self.officer)
        base = reverse('site-issue-list')
        self.assertEqual(len(self.client.get(base, {'severity': 'HIGH'}).data), 1)
        self.assertEqual(len(self.client.get(base, {'status': 'OPEN'}).data), 1)
        self.assertEqual(len(self.client.get(base, {'search': 'ponding'}).data), 1)
        self.assertEqual(
            len(self.client.get(base, {'project': str(self.project.id)}).data), 2)

    # -- Construction milestones ---------------------------------------------

    def test_milestone_full_lifecycle_via_api(self):
        response = self.client.post(
            reverse('construction-milestone-list'),
            {"project_id": str(self.project.id), "name": "Level 6 slab pour",
             "phase": "SUPERSTRUCTURE", "target_date": str(self.today + datetime.timedelta(days=14)),
             "progress_percentage": 100,
             "evidence_documents": [
                 {"name": "Cube test 28-day.pdf", "url": "https://assets/cube.pdf"}]},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        ms_id = response.data['data']['id']
        self.assertEqual(response.data['data']['status'], 'IN_PROGRESS')

        response = self.client.post(
            reverse('construction-milestone-attach-evidence', kwargs={'pk': ms_id}),
            {"photos": ["https://assets/slab-pour.jpg"]}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['evidence_photos'], ["https://assets/slab-pour.jpg"])

        response = self.client.get(
            reverse('construction-milestone-gate-status', kwargs={'pk': ms_id}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['data']['all_gates_passed'])

        response = self.client.post(
            reverse('construction-milestone-update-progress', kwargs={'pk': ms_id}),
            {"progress_percentage": 100, "physical_progress_notes": "Slab cured 14 days."},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['status'], 'PENDING_VERIFICATION')

        response = self.client.post(
            reverse('construction-milestone-submit-verification', kwargs={'pk': ms_id}),
            {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.post(
            reverse('construction-milestone-verify', kwargs={'pk': ms_id}),
            {"notes": "Concrete strength benchmarks satisfied."}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['status'], 'VERIFIED')
        self.assertRegex(
            response.data['data']['verification_signoff']['signature_hash'], r'^0x[0-9A-F]{24}$')

        response = self.client.get(
            reverse('construction-milestone-audit-trail', kwargs={'pk': ms_id}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        actions = [e['action'] for e in response.data['data']]
        self.assertIn('CONSTRUCTION_MILESTONE_CREATED', actions)
        self.assertIn('CONSTRUCTION_MILESTONE_VERIFIED', actions)

    def test_milestone_verify_gate_failure_returns_400_with_gate_status(self):
        ms = MonitoringService.create_milestone(
            data={"project_id": self.project.id, "name": "No evidence milestone"},
            user=self.officer)
        response = self.client.post(
            reverse('construction-milestone-verify', kwargs={'pk': str(ms.id)}),
            {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(response.data['success'])
        self.assertIn('gate_status', response.data)
        self.assertFalse(response.data['gate_status']['all_gates_passed'])
        ms.refresh_from_db()
        self.assertEqual(ms.status, 'PLANNED')

    def test_milestone_flag_delay_via_api(self):
        ms = MonitoringService.create_milestone(
            data={"project_id": self.project.id, "name": "Facade milestone",
                  "target_date": str(self.today + datetime.timedelta(days=10))},
            user=self.officer)
        response = self.client.post(
            reverse('construction-milestone-flag-delay', kwargs={'pk': str(ms.id)}),
            {"reason": "Unitized facade panels held at port.",
             "revised_target_date": str(self.today + datetime.timedelta(days=24))},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['status'], 'DELAYED')
        self.assertEqual(response.data['data']['variance_days'], 14)

    def test_milestone_create_invalid_target_date_returns_400(self):
        response = self.client.post(
            reverse('construction-milestone-list'),
            {"project_id": str(self.project.id), "name": "Bad date milestone",
             "target_date": "31/12/2026"},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_milestone_list_filters(self):
        MonitoringService.create_milestone(
            data={"project_id": self.project.id, "name": "Piling completion",
                  "phase": "SUBSTRUCTURE", "status": "VERIFIED"}, user=self.officer)
        MonitoringService.create_milestone(
            data={"project_id": self.project.id, "name": "Frame erection",
                  "phase": "STRUCTURAL_FRAME", "status": "PLANNED",
                  "critical_path": True}, user=self.officer)
        base = reverse('construction-milestone-list')
        self.assertEqual(len(self.client.get(base, {'phase': 'SUBSTRUCTURE'}).data), 1)
        self.assertEqual(len(self.client.get(base, {'phase': 'ALL'}).data), 2)
        self.assertEqual(len(self.client.get(base, {'status': 'VERIFIED'}).data), 1)
        self.assertEqual(len(self.client.get(base, {'status': 'all'}).data), 2)
        self.assertEqual(len(self.client.get(base, {'critical_path': 'true'}).data), 1)
        self.assertEqual(len(self.client.get(base, {'risk': 'ALL'}).data), 2)
        self.assertEqual(len(self.client.get(base, {'risk_level': 'LOW'}).data), 2)
        self.assertEqual(len(self.client.get(base, {'project': str(self.project.id)}).data), 2)
        self.assertEqual(len(self.client.get(base, {'search': 'frame erection'}).data), 1)

    # -- Site verifications ---------------------------------------------------

    def test_site_verification_flows_via_api(self):
        response = self.client.post(
            reverse('site-verification-list'),
            {"project_id": str(self.project.id), "method": "GNSS_RTK_SURVEY",
             "captured_coordinates": {"lat": 6.4281000, "lng": 3.4219000, "elevation": 4.15},
             "approved_coordinates": {"lat": 6.4281000, "lng": 3.4219000, "elevation": 4.15},
             "telemetry_data": {"satellites_tracked": 31, "hdop": 0.6}},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        vrf_id = response.data['data']['id']
        self.assertEqual(response.data['data']['status'], 'VERIFIED')
        self.assertEqual(response.data['data']['telemetry_data']['satellites_tracked'], 31)

        response = self.client.post(
            reverse('site-verification-certify', kwargs={'pk': vrf_id}),
            {"verified_by_name": "Surv. Ada Umeh"}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data['data']['digital_cert_ref'].startswith('CERT-VRF-'))
        self.assertRegex(response.data['data']['signature_hash'], r'^0xLASBCA-VRF-SURV-[0-9A-F]{16}$')

        response = self.client.post(
            reverse('site-verification-list'),
            {"project_id": str(self.project.id), "method": "TERSU_ROVER",
             "captured_coordinates": {"lat": 6.428100, "lng": 3.421900},
             "approved_coordinates": {"lat": 6.428150, "lng": 3.421950}},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        variance_id = response.data['data']['id']
        self.assertEqual(response.data['data']['status'], 'VARIANCE_DETECTED')

        response = self.client.post(
            reverse('site-verification-certify', kwargs={'pk': variance_id}), {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('measured_variance', response.data)
        self.assertIn('error', response.data)

        response = self.client.post(
            reverse('site-verification-flag-encroachment', kwargs={'pk': variance_id}),
            {"reason": "Setback encroachment on north boundary"}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['status'], 'FLAGGED')

        response = self.client.post(
            reverse('site-verification-attach-evidence', kwargs={'pk': vrf_id}),
            {"documents": [{"name": "rinex-log.obs"}],
             "photos": ["https://assets/beacon-1.jpg"]},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['evidence_documents'][0]['name'], 'rinex-log.obs')

        response = self.client.get(
            reverse('site-verification-telemetry', kwargs={'pk': vrf_id}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['satellites_tracked'], 31)

        response = self.client.get(
            reverse('site-verification-audit-trail', kwargs={'pk': vrf_id}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        actions = [e['action'] for e in response.data['data']]
        self.assertIn('SITE_VERIFICATION_RECORDED', actions)
        self.assertIn('SITE_VERIFICATION_CERTIFIED', actions)

    def test_verification_list_filters(self):
        MonitoringService.record_site_verification(
            data={"project_id": self.project.id, "method": "GNSS_RTK_SURVEY",
                  "device_identifier": "Tersus Oscar GNSS RTK #042"}, user=self.officer)
        MonitoringService.record_site_verification(
            data={"project_id": self.project.id, "method": "DRONE_PHOTOGRAMMETRY",
                  "device_identifier": "DJI M300 RTK #007",
                  "variance_meters": 0.4}, user=self.officer)
        base = reverse('site-verification-list')
        self.assertEqual(len(self.client.get(base, {'method': 'GNSS_RTK_SURVEY'}).data), 1)
        self.assertEqual(len(self.client.get(base, {'method': 'ALL'}).data), 2)
        self.assertEqual(len(self.client.get(base, {'status': 'VARIANCE_DETECTED'}).data), 1)
        self.assertEqual(len(self.client.get(base, {'variance_detected': 'true'}).data), 1)
        self.assertEqual(len(self.client.get(base, {'encroachment_detected': 'false'}).data), 2)
        self.assertEqual(len(self.client.get(base, {'search': 'DJI M300'}).data), 1)
        self.assertEqual(
            len(self.client.get(base, {'project': str(self.project.id)}).data), 2)

    # -- Stats overview --------------------------------------------------------

    def test_stats_overview_aggregates_real_rows(self):
        MonitoringService.log_daily_update(
            data={"project_id": self.project.id, "update_type": "DAILY_PHOTO",
                  "work_summary": "Photos.",
                  "photos": ["https://assets/1.jpg", "https://assets/2.jpg"]},
            user=self.officer)
        MonitoringService.log_daily_update(
            data={"project_id": self.project.id, "update_type": "DRONE_SURVEY",
                  "work_summary": "Drone."}, user=self.officer)
        MonitoringService.log_observation(
            data={"project_id": self.project.id, "category": "SAFETY",
                  "title": "open obs", "description": "d"}, user=self.officer)
        MonitoringService.log_observation(
            data={"project_id": self.project.id, "category": "SAFETY",
                  "title": "resolved obs", "description": "d", "status": "RESOLVED"},
            user=self.officer)
        MonitoringService.report_issue(
            data={"project_id": self.project.id, "title": "critical",
                  "description": "d", "severity": "CRITICAL"}, user=self.officer)
        MonitoringService.create_milestone(
            data={"project_id": self.project.id, "name": "verified ms",
                  "status": "VERIFIED"}, user=self.officer)
        MonitoringService.create_milestone(
            data={"project_id": self.project.id, "name": "pending ms"}, user=self.officer)
        MonitoringService.record_site_verification(
            data={"project_id": self.project.id}, user=self.officer)

        response = self.client.get(reverse('monitoring-stats-overview'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data['data']
        for section in ('live', 'progress', 'observations', 'issues', 'milestones', 'verification'):
            self.assertIn(section, data)
        self.assertEqual(data['live']['active_sites'], 1)
        self.assertEqual(data['live']['daily_photos'], 2)
        self.assertEqual(data['live']['drone_surveys'], 1)
        self.assertEqual(data['live']['active_observations'], 1)
        self.assertEqual(data['observations']['resolved'], 1)
        self.assertEqual(data['issues']['critical'], 1)
        self.assertEqual(data['milestones']['total'], 2)
        self.assertEqual(data['milestones']['verified'], 1)
        self.assertEqual(data['milestones']['pending_verification'], 0)
        self.assertEqual(data['verification']['pending'], 0)  # recorded clean = VERIFIED
        self.assertEqual(data['verification']['verified'], 1)
        self.assertEqual(data['verification']['active_devices'], 1)

    def test_stats_list_alias_calls_overview(self):
        response = self.client.get(reverse('monitoring-stats-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('live', response.data['data'])

    # -- Site progress -----------------------------------------------------------

    def test_site_progress_list_and_retrieve(self):
        response = self.client.get(reverse('site-progress-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        project_names = {row['project_name'] for row in response.data['data']}
        self.assertIn('Yaba Tech Cluster Block C', project_names)

        response = self.client.get(
            reverse('site-progress-list'), {'project': str(self.project.id)})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # A specific project returns that project's single detail payload.
        self.assertEqual(response.data['data']['project_name'], 'Yaba Tech Cluster Block C')

        response = self.client.get(
            reverse('site-progress-list'), {'project': 'ALL'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        response = self.client.get(
            reverse('site-progress-detail', kwargs={'pk': str(self.project.id)}))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['project_name'], 'Yaba Tech Cluster Block C')

    def test_site_progress_update_via_api(self):
        response = self.client.post(
            reverse('site-progress-update-progress'),
            {"project_id": str(self.project.id), "progress_percentage": 65,
             "work_summary": "Wing B slab pour complete."},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['data']['verified_progress'], 65)
        log = DailySiteUpdate.objects.get(
            project=self.project, update_type='PROGRESS_REPORT')
        self.assertEqual(log.progress_percentage, 65)

    def test_site_progress_flag_delay_via_api(self):
        ConstructionMilestone.objects.create(
            project=self.project, name="Upcoming roof", status="UPCOMING",
            target_date=self.today + datetime.timedelta(days=15))
        response = self.client.post(
            reverse('site-progress-flag-delay'),
            {"project_id": str(self.project.id), "reason": "Concrete supply disruption."},
            format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        issue = SiteIssue.objects.get(title__startswith="Schedule Delay Notice:")
        self.assertEqual(response.data['data']['reference'], issue.issue_reference)
        self.assertTrue(ConstructionMilestone.objects.get(name="Upcoming roof").is_delayed)

    def test_site_progress_retrieve_unknown_project_returns_400(self):
        response = self.client.get(
            reverse('site-progress-detail', kwargs={'pk': 'ghost-project'}))
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
