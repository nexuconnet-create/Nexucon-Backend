from django.test import TestCase
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient
from apps.projects.models import Project
from apps.bim.models import BIMModel, BIMModelVersion, BIMClash, BIMAnnotation, BIMProgressValidation
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
        res = self.client.get('/api/v1/bim/stats/overview/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('models', res.data)
        self.assertIn('clashes', res.data)
        self.assertIn('annotations', res.data)
        self.assertIn('progress_4d', res.data)
