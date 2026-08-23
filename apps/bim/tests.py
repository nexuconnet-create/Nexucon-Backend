from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from apps.projects.models import Project
from apps.bim.models import (
    BIMModel, BIMModelVersion, BIMClash, BIMAnnotation, 
    BIMProgressValidation, BIMConstructionMilestone
)
from apps.compliance.models import NonConformanceReport
from apps.bim.services import BIMService

User = get_user_model()

class BIMTestCase(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='sarah_jenkins',
            email='reviewer@government.gov.ng',
            password='Password123!',
            first_name='Sarah',
            last_name='Jenkins'
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.project = Project.objects.create(
            name='Downtown Metro Station',
            reference_number='PRJ-2026-METRO',
            lga='Ikeja',
            status='Active'
        )

    def test_upload_model_and_initial_version(self):
        """Test model upload automatically creates v1.0 version."""
        data = {
            "project_id": self.project.id,
            "name": "Downtown Metro Station - Architecture",
            "discipline": "Architecture",
            "format": "IFC4",
            "file_size": "345 MB",
            "element_count": 12450
        }
        model = BIMService.upload_model(data, self.user)
        self.assertIsNotNone(model.id)
        self.assertEqual(model.current_version, 'v1.0')
        self.assertEqual(model.versions.count(), 1)
        self.assertTrue(model.versions.first().is_current)

    def test_create_new_revision(self):
        """Test pushing a new revision updates current version."""
        model = BIMService.upload_model({"project_id": self.project.id, "name": "Hospital Annex"}, self.user)
        v2 = BIMService.create_version(model, {
            "version_label": "v2.0",
            "changes_summary": "Updated HVAC ducting routing.",
            "stats_added": 50,
            "stats_modified": 20,
            "stats_removed": 5
        }, self.user)

        self.assertEqual(v2.version_label, 'v2.0')
        model.refresh_from_db()
        self.assertEqual(model.current_version, 'v2.0')
        self.assertEqual(model.versions.count(), 2)

    def test_stamp_and_certify_model(self):
        """Test applying cryptographic digital certification stamp."""
        model = BIMService.upload_model({"project_id": self.project.id, "name": "Bridge Structural"}, self.user)
        certified = BIMService.stamp_and_certify(model, self.user, "0x3f8ac910022c4f")
        
        self.assertTrue(certified.is_digitally_certified)
        self.assertEqual(certified.status, 'Approved')
        self.assertEqual(certified.hash_signature, '0x3f8ac910022c4f')
        self.assertIsNotNone(certified.certified_at)

    def test_clash_detection_and_conversion_to_site_issue(self):
        """Test running clash detection and converting clash into a site defect issue."""
        m_arch = BIMService.upload_model({"project_id": self.project.id, "name": "Architecture"}, self.user)
        m_mep = BIMService.upload_model({"project_id": self.project.id, "name": "MEP"}, self.user)
        
        clash = BIMService.run_clash_matrix(self.project.id, m_arch.id, m_mep.id, self.user)
        self.assertEqual(clash.clash_type, 'HARD_CLASH')
        self.assertEqual(clash.status, 'OPEN')

        site_issue = BIMService.convert_clash_to_site_issue(clash, self.user)
        self.assertIsNotNone(site_issue.id)
        clash.refresh_from_db()
        self.assertEqual(clash.status, 'CONVERTED_TO_ISSUE')
        self.assertEqual(clash.converted_site_issue.id, site_issue.id)

    def test_bcf_annotation_workflow(self):
        """Test BCF review annotation logging and resolution."""
        model = BIMService.upload_model({"project_id": self.project.id, "name": "Metro Model"}, self.user)
        ann = BIMService.add_annotation(model, {
            "text": "Headroom clearance under 2.4m.",
            "priority": "High"
        }, self.user)
        
        self.assertEqual(ann.status, 'Open')
        resolved = BIMService.resolve_annotation(ann, "Adjusted beam height.", self.user)
        self.assertEqual(resolved.status, 'Resolved')

    def test_bim_stats_overview_endpoint(self):
        """Test the overview stats endpoint."""
        BIMService.upload_model({"project_id": self.project.id, "name": "Metro Model"}, self.user)
        response = self.client.get('/api/v1/bim/stats/overview/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('models', response.data)
        self.assertIn('clashes', response.data)
        self.assertIn('milestones', response.data)

    def test_bim_construction_milestone_lifecycle(self):
        """Test full BIM milestone lifecycle: create, gate check, digital verify, deviation, re-verification."""
        # 1. Upload and certify model
        model = BIMService.upload_model({"project_id": self.project.id, "name": "Structural Superstructure Model"}, self.user)
        BIMService.stamp_and_certify(model, self.user, "0xCERTIFIED998811")

        # 2. Create BIM Construction Milestone
        milestone = BIMService.create_bim_milestone({
            "project_id": self.project.id,
            "bim_model_id": model.id,
            "name": "Level 1-4 Core Shear Wall Alignment",
            "phase": "STRUCTURAL_FRAME",
            "sequence_order": 1,
            "tolerance_max_mm": 15.0,
            "bim_deviation_mm": 5.2,
            "gpr_clearance_status": "VERIFIED",
            "bim_elements": [{"id": "STR-CORE-WALL", "count": 4, "lod": "LOD 400"}],
            "linked_inspections": [{"id": "INS-01", "outcome": "PASSED"}]
        }, self.user)

        self.assertIsNotNone(milestone.id)
        self.assertTrue(milestone.milestone_code.startswith("BIM-MS-"))

        # 3. Evaluate Gates -> Expect all passed
        gates = BIMService.evaluate_milestone_gate_status(milestone)
        self.assertTrue(gates["all_gates_passed"])
        self.assertTrue(gates["can_digitally_sign"])

        # 4. Verify & Digitally Stamp
        verified_ms = BIMService.verify_and_stamp_milestone(milestone, self.user, "Verified by Structural Directorate")
        self.assertEqual(verified_ms.verification_status, 'VERIFIED')
        self.assertIsNotNone(verified_ms.digital_stamp_reference)
        self.assertEqual(verified_ms.verified_by, self.user)

        # 5. Flag Deviation Exceedance
        flagged_ms = BIMService.flag_milestone_deviation(milestone, self.user, {
            "deviation_mm": 28.5,
            "reason": "LiDAR scan showed 28.5mm tilt at Grid 3-C, exceeding 15mm limit."
        })
        self.assertEqual(flagged_ms.verification_status, 'DEVIATION_FLAGGED')
        self.assertEqual(flagged_ms.bim_deviation_mm, 28.5)
        # Check NCR auto-created
        ncr = NonConformanceReport.objects.filter(project=self.project).first()
        self.assertIsNotNone(ncr)
        self.assertIn("28.5", ncr.title)

        # 6. Re-Verification Request
        reopened = BIMService.request_milestone_re_verification(milestone, self.user, "Remediation poured.")
        self.assertEqual(reopened.verification_status, 'RE_VERIFICATION_REQUIRED')
        self.assertIsNone(reopened.digital_stamp_reference)

    def test_milestone_gate_failure_when_model_uncertified(self):
        """Test gate evaluation fails if associated BIM model is not certified."""
        model = BIMService.upload_model({"project_id": self.project.id, "name": "Draft Architecture Model"}, self.user)
        # Leave uncertified / status 'Active'
        milestone = BIMService.create_bim_milestone({
            "project_id": self.project.id,
            "bim_model_id": model.id,
            "name": "Draft Milestone",
            "phase": "SUPERSTRUCTURE"
        }, self.user)

        gates = BIMService.evaluate_milestone_gate_status(milestone)
        self.assertFalse(gates["all_gates_passed"])
        self.assertFalse(gates["can_digitally_sign"])
        self.assertTrue(any("Approved status" in b for b in gates["blockers"]))

        # Attempting verify_and_stamp_milestone should raise ValueError
        with self.assertRaises(ValueError):
            BIMService.verify_and_stamp_milestone(milestone, self.user)
