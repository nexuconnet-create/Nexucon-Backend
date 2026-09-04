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


# =============================================================================
# API layer: authentication, CRUD, actions, filters
# =============================================================================

import uuid as _uuid
from datetime import date, timedelta

from apps.government.models import District, Profile
from apps.monitoring.models import ConstructionMilestone, SiteIssue
from apps.bim.models import BIMProgressValidation


class BIMAuthRequiredTests(TestCase):
    """Every BIM endpoint rejects unauthenticated requests with 401."""

    def test_anonymous_requests_rejected(self):
        client = APIClient()
        cases = [
            '/api/v1/bim/models/',
            '/api/v1/bim/versions/',
            '/api/v1/bim/clashes/',
            '/api/v1/bim/annotations/',
            '/api/v1/bim/progress-validation/',
            '/api/v1/bim/milestones/',
            '/api/v1/bim/stats/overview/',
        ]
        for url in cases:
            res = client.get(url)
            self.assertEqual(res.status_code, 401, url)
        # write actions too
        res = client.post('/api/v1/bim/models/', {"name": "x"}, format='json')
        self.assertEqual(res.status_code, 401)
        res = client.post('/api/v1/bim/clashes/run-matrix/', {}, format='json')
        self.assertEqual(res.status_code, 401)


class BIMAPITestBase(TestCase):
    """
    A district-scoped reviewer, a project inside their district, and a second
    project in another district that must be invisible to them.
    """

    def setUp(self):
        super().setUp()
        self.district_a = District.objects.create(name='Ikeja District', code='IKJ')
        self.district_b = District.objects.create(name='Epe District', code='EPE')

        self.user = User.objects.create_user(
            username='ada_okafor', email='ada@government.gov.ng', password='Password123!',
            first_name='Ada', last_name='Okafor')
        Profile.objects.create(user=self.user, district=self.district_a, is_state_hq=False)

        self.project = Project.objects.create(
            name='Downtown Metro Station', reference_number='PRJ-API-METRO',
            lga='Ikeja', status='Active', district=self.district_a)
        self.other_project = Project.objects.create(
            name='Epe Reservoir Works', reference_number='PRJ-API-EPE',
            lga='Epe', status='Active', district=self.district_b)

        self.client = APIClient()
        self.client.force_authenticate(user=self.user)

        self.model = BIMModel.objects.create(
            project=self.project, name='Metro Architecture Model',
            discipline='Architecture', format='IFC4', element_count=1200)
        # real registration path: v1.0 version row, like upload_model creates
        BIMModelVersion.objects.create(
            model=self.model, version_label='v1.0', is_current=True,
            commit_hash='aabbcc11', author_name='Ada Okafor')
        self.other_model = BIMModel.objects.create(
            project=self.other_project, name='Epe Structural Model',
            discipline='Structural', format='IFC4', element_count=800)


class BIMModelAPITests(BIMAPITestBase):
    def test_list_returns_only_scoped_project_models(self):
        res = self.client.get('/api/v1/bim/models/')
        self.assertEqual(res.status_code, 200)
        names = [m['name'] for m in res.data]
        self.assertIn('Metro Architecture Model', names)
        self.assertNotIn('Epe Structural Model', names)

    def test_cross_project_detail_404(self):
        res = self.client.get(f'/api/v1/bim/models/{self.other_model.id}/')
        self.assertEqual(res.status_code, 404)

    def test_cross_project_update_and_delete_404(self):
        res = self.client.patch(f'/api/v1/bim/models/{self.other_model.id}/',
                                {"name": "hijacked"}, format='json')
        self.assertEqual(res.status_code, 404)
        res = self.client.delete(f'/api/v1/bim/models/{self.other_model.id}/')
        self.assertEqual(res.status_code, 404)
        self.assertTrue(BIMModel.objects.filter(id=self.other_model.id).exists())

    def test_create_model_registers_v1_version(self):
        res = self.client.post('/api/v1/bim/models/', {
            "project": str(self.project.id),
            "name": "Metro Structural Model",
            "discipline": "Structural",
            "element_count": 5400,
        }, format='json')
        self.assertEqual(res.status_code, 201)
        model = BIMModel.objects.get(id=res.data['id'])
        self.assertEqual(model.project, self.project)
        self.assertEqual(model.current_version, 'v1.0')
        self.assertEqual(model.uploaded_by, self.user)
        self.assertEqual(model.versions.count(), 1)
        self.assertEqual(model.versions.first().version_label, 'v1.0')
        self.assertTrue(model.versions.first().is_current)
        self.assertTrue(model.model_reference.startswith('MDL-'))

    def test_create_into_out_of_scope_project_forbidden(self):
        res = self.client.post('/api/v1/bim/models/', {
            "project": str(self.other_project.id),
            "name": "Sneaky Model",
        }, format='json')
        self.assertEqual(res.status_code, 403)
        self.assertFalse(BIMModel.objects.filter(name="Sneaky Model").exists())

    def test_filters_discipline_status_certified_search(self):
        arch = self.model
        mep = BIMModel.objects.create(project=self.project, name='Metro MEP Model',
                                      discipline='MEP', status='Under Review')
        BIMService.stamp_and_certify(arch, self.user, "0xSIG123")

        res = self.client.get('/api/v1/bim/models/', {'discipline': 'MEP'})
        self.assertEqual([m['id'] for m in res.data], [str(mep.id)])
        res = self.client.get('/api/v1/bim/models/', {'status': 'under review'})
        self.assertEqual([m['id'] for m in res.data], [str(mep.id)])
        res = self.client.get('/api/v1/bim/models/', {'certified': 'true'})
        self.assertEqual([m['id'] for m in res.data], [str(arch.id)])
        res = self.client.get('/api/v1/bim/models/', {'search': 'MEP'})
        self.assertEqual([m['id'] for m in res.data], [str(mep.id)])
        res = self.client.get('/api/v1/bim/models/', {'project': str(self.project.id)})
        self.assertEqual(len(res.data), 2)

    def test_certify_action(self):
        res = self.client.post(f'/api/v1/bim/models/{self.model.id}/certify/',
                               {"hash_signature": "0xABCDEF123456"}, format='json')
        self.assertEqual(res.status_code, 200)
        self.model.refresh_from_db()
        self.assertTrue(self.model.is_digitally_certified)
        self.assertEqual(self.model.hash_signature, "0xABCDEF123456")
        self.assertEqual(self.model.status, 'Approved')
        self.assertEqual(self.model.certified_by_name, 'Ada Okafor')

    def test_certify_generates_signature_when_missing(self):
        res = self.client.post(f'/api/v1/bim/models/{self.model.id}/certify/', {}, format='json')
        self.assertEqual(res.status_code, 200)
        self.model.refresh_from_db()
        self.assertTrue(self.model.hash_signature.startswith("0x"))

    def test_request_changes_action_creates_annotation(self):
        res = self.client.post(f'/api/v1/bim/models/{self.model.id}/request-changes/',
                               {"reason": "Core shear wall location clashes with existing utility corridor."}, format='json')
        self.assertEqual(res.status_code, 200)
        self.model.refresh_from_db()
        self.assertEqual(self.model.status, 'Changes Requested')
        ann = self.model.annotations.first()
        self.assertIsNotNone(ann)
        self.assertEqual(ann.status, 'Open')
        self.assertEqual(ann.priority, 'High')
        self.assertIn("utility corridor", ann.text)

    def test_create_version_action_and_reverification_flag(self):
        BIMService.stamp_and_certify(self.model, self.user, "0xSIG")
        milestone = BIMConstructionMilestone.objects.create(
            project=self.project, bim_model=self.model, name='Core Wall Pour',
            verification_status='VERIFIED')
        res = self.client.post(f'/api/v1/bim/models/{self.model.id}/create-version/', {
            "version_label": "v2.0",
            "changes_summary": "Shear wall relocation per review.",
            "stats_added": 120, "stats_modified": 40, "stats_removed": 12,
        }, format='json')
        self.assertEqual(res.status_code, 201)
        self.assertEqual(res.data['version_label'], 'v2.0')
        self.model.refresh_from_db()
        self.assertEqual(self.model.current_version, 'v2.0')
        self.assertEqual(self.model.versions.filter(is_current=True).count(), 1)
        milestone.refresh_from_db()
        self.assertEqual(milestone.verification_status, 'RE_VERIFICATION_REQUIRED')

    def test_approved_models_action(self):
        BIMService.stamp_and_certify(self.model, self.user, "0xSIG")
        BIMModel.objects.create(project=self.project, name='Draft MEP', discipline='MEP')
        res = self.client.get('/api/v1/bim/models/approved-models/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual([m['id'] for m in res.data], [str(self.model.id)])


class BIMVersionAPITests(BIMAPITestBase):
    def setUp(self):
        super().setUp()
        self.v1 = self.model.versions.first() or BIMModelVersion.objects.create(
            model=self.model, version_label='v1.0', is_current=True)
        self.v2 = BIMService.create_version(self.model, {
            "version_label": "v2.0", "stats_added": 100, "stats_modified": 30, "stats_removed": 10,
        }, self.user)
        self.other_v = BIMModelVersion.objects.create(
            model=self.other_model, version_label='v9.9')

    def test_list_filtered_by_model(self):
        res = self.client.get('/api/v1/bim/versions/', {'model': str(self.model.id)})
        self.assertEqual(res.status_code, 200)
        labels = {v['version_label'] for v in res.data}
        self.assertEqual(labels, {'v1.0', 'v2.0'})

    def test_cross_project_version_hidden(self):
        res = self.client.get('/api/v1/bim/versions/')
        ids = [v['id'] for v in res.data]
        self.assertNotIn(str(self.other_v.id), ids)
        res = self.client.get(f'/api/v1/bim/versions/{self.other_v.id}/')
        self.assertEqual(res.status_code, 404)

    def test_compare_versions(self):
        res = self.client.post('/api/v1/bim/versions/compare/', {
            "version_a": str(self.v1.id), "version_b": str(self.v2.id),
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['elements_added'], 100)
        self.assertEqual(res.data['elements_modified'], 30)
        self.assertEqual(res.data['elements_removed'], 10)

    def test_compare_requires_both_versions(self):
        res = self.client.post('/api/v1/bim/versions/compare/', {
            "version_a": str(self.v1.id),
        }, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn("error", res.data)


class BIMClashAPITests(BIMAPITestBase):
    def setUp(self):
        super().setUp()
        self.mep_model = BIMModel.objects.create(
            project=self.project, name='Metro MEP Model', discipline='MEP')

    def test_run_matrix_creates_clash(self):
        res = self.client.post('/api/v1/bim/clashes/run-matrix/', {
            "project": str(self.project.id),
            "primary_model": str(self.model.id),
            "secondary_model": str(self.mep_model.id),
        }, format='json')
        self.assertEqual(res.status_code, 201)
        clash = BIMClash.objects.get(id=res.data['id'])
        self.assertEqual(clash.project, self.project)
        self.assertEqual(clash.primary_model, self.model)
        self.assertEqual(clash.secondary_model, self.mep_model)
        self.assertEqual(clash.status, 'OPEN')
        # the report is truthful: no invented clearance measurement
        self.assertNotIn("-160mm", clash.description)
        self.assertNotIn("Grid 4-C", clash.description)

    def test_run_matrix_requires_project_and_model(self):
        res = self.client.post('/api/v1/bim/clashes/run-matrix/', {
            "primary_model": str(self.model.id),
        }, format='json')
        self.assertEqual(res.status_code, 400)

    def test_run_matrix_out_of_scope_project_forbidden(self):
        res = self.client.post('/api/v1/bim/clashes/run-matrix/', {
            "project": str(self.other_project.id),
            "primary_model": str(self.other_model.id),
        }, format='json')
        self.assertEqual(res.status_code, 403)

    def _make_clash(self, **kwargs):
        defaults = dict(
            project=self.project, primary_model=self.model, secondary_model=self.mep_model,
            clash_type='HARD_CLASH', title='Duct vs Beam Interference',
            description='MEP duct routes through structural beam zone.',
            severity='HIGH', status='OPEN')
        defaults.update(kwargs)
        return BIMClash.objects.create(**defaults)

    def test_filters_severity_status_model_search(self):
        high = self._make_clash()
        low = self._make_clash(title='Duplicate Column', description='Dup.',
                               severity='LOW', status='RESOLVED')
        res = self.client.get('/api/v1/bim/clashes/', {'severity': 'HIGH'})
        self.assertEqual([c['id'] for c in res.data], [str(high.id)])
        res = self.client.get('/api/v1/bim/clashes/', {'status': 'resolved'})
        self.assertEqual([c['id'] for c in res.data], [str(low.id)])
        res = self.client.get('/api/v1/bim/clashes/', {'model': str(self.mep_model.id)})
        self.assertEqual(len(res.data), 2)
        res = self.client.get('/api/v1/bim/clashes/', {'search': 'Duplicate'})
        self.assertEqual([c['id'] for c in res.data], [str(low.id)])

    def test_resolve_action(self):
        clash = self._make_clash()
        res = self.client.post(f'/api/v1/bim/clashes/{clash.id}/resolve/',
                               {"resolution_notes": "Duct rerouted above beam."}, format='json')
        self.assertEqual(res.status_code, 200)
        clash.refresh_from_db()
        self.assertEqual(clash.status, 'RESOLVED')
        self.assertEqual(clash.resolution_notes, "Duct rerouted above beam.")

    def test_convert_to_issue_action(self):
        clash = self._make_clash()
        res = self.client.post(f'/api/v1/bim/clashes/{clash.id}/convert-to-issue/', {}, format='json')
        self.assertEqual(res.status_code, 200)
        clash.refresh_from_db()
        self.assertEqual(clash.status, 'CONVERTED_TO_ISSUE')
        issue = clash.converted_site_issue
        self.assertIsNotNone(issue)
        self.assertEqual(issue.project, self.project)
        self.assertEqual(issue.severity, 'HIGH')
        self.assertIn(clash.clash_reference, issue.description)

    def test_cross_project_clash_404(self):
        other_clash = BIMClash.objects.create(
            project=self.other_project, primary_model=self.other_model,
            clash_type='HARD_CLASH', title='Epe clash', description='d',
            severity='LOW', status='OPEN')
        res = self.client.get(f'/api/v1/bim/clashes/{other_clash.id}/')
        self.assertEqual(res.status_code, 404)
        res = self.client.post(f'/api/v1/bim/clashes/{other_clash.id}/resolve/', {}, format='json')
        self.assertEqual(res.status_code, 404)


class BIMAnnotationAPITests(BIMAPITestBase):
    def _annotation(self, **kwargs):
        defaults = dict(
            model=self.model, project=self.project, author_name='Ada Okafor',
            author_role='Review Officer', text='Headroom clearance under 2.4m.',
            status='Open', priority='High')
        defaults.update(kwargs)
        return BIMAnnotation.objects.create(**defaults)

    def test_create_annotation(self):
        res = self.client.post('/api/v1/bim/annotations/', {
            "model": str(self.model.id),
            "project": str(self.project.id),
            "text": "Door swing conflicts with egress route.",
            "priority": "Critical",
        }, format='json')
        self.assertEqual(res.status_code, 201)
        ann = BIMAnnotation.objects.get(id=res.data['id'])
        self.assertEqual(ann.project, self.project)
        self.assertEqual(ann.author_name, 'Ada Okafor')
        self.assertEqual(ann.priority, 'Critical')
        self.assertTrue(ann.annotation_reference.startswith('ANN-'))

    def test_create_annotation_for_out_of_scope_model_forbidden(self):
        res = self.client.post('/api/v1/bim/annotations/', {
            "model": str(self.other_model.id),
            "project": str(self.other_project.id),
            "text": "cross-tenant comment",
        }, format='json')
        self.assertEqual(res.status_code, 403)

    def test_filters_status_priority_model_search(self):
        open_high = self._annotation()
        inprog = self._annotation(text='Camera viewpoint adjusted.', status='In Progress',
                                  priority='Low')
        res = self.client.get('/api/v1/bim/annotations/', {'status': 'in progress'})
        self.assertEqual([a['id'] for a in res.data], [str(inprog.id)])
        res = self.client.get('/api/v1/bim/annotations/', {'priority': 'High'})
        self.assertEqual([a['id'] for a in res.data], [str(open_high.id)])
        res = self.client.get('/api/v1/bim/annotations/', {'status': 'all'})
        self.assertEqual(len(res.data), 2)
        res = self.client.get('/api/v1/bim/annotations/', {'search': 'Headroom'})
        self.assertEqual([a['id'] for a in res.data], [str(open_high.id)])

    def test_resolve_action(self):
        ann = self._annotation()
        res = self.client.post(f'/api/v1/bim/annotations/{ann.id}/resolve/',
                               {"notes": "Beam raised by 150mm in v2.0."}, format='json')
        self.assertEqual(res.status_code, 200)
        ann.refresh_from_db()
        self.assertEqual(ann.status, 'Resolved')

    def test_cross_project_annotation_404(self):
        other_ann = BIMAnnotation.objects.create(
            model=self.other_model, project=self.other_project,
            author_name='Someone', text='private', status='Open', priority='Low')
        res = self.client.get(f'/api/v1/bim/annotations/{other_ann.id}/')
        self.assertEqual(res.status_code, 404)


class BIMProgressValidationAPITests(BIMAPITestBase):
    def test_simulate_with_real_construction_milestones(self):
        ConstructionMilestone.objects.create(
            project=self.project, name='Foundation Piling', target_date=date.today(),
            progress_percentage=100, status='COMPLETED')
        ConstructionMilestone.objects.create(
            project=self.project, name='Superstructure Frame', target_date=date.today(),
            progress_percentage=40, status='DELAYED', variance_days=7)

        res = self.client.post('/api/v1/bim/progress-validation/simulate/', {
            "project": str(self.project.id),
        }, format='json')
        self.assertEqual(res.status_code, 201)
        validation = BIMProgressValidation.objects.get(id=res.data['id'])
        self.assertEqual(validation.schedule_status, 'DELAYED')
        self.assertEqual(validation.days_variance, -7)
        phases = {p['phase']: p for p in validation.planned_vs_actual}
        self.assertEqual(phases['Foundation Piling']['status'], 'Completed')
        self.assertEqual(phases['Superstructure Frame']['status'], 'Delayed - 7 Days')
        # avg progress (100+40)/2 = 70% of the model's elements
        self.assertEqual(validation.completed_elements_count,
                         int(0.7 * self.model.element_count))

    def test_simulate_without_milestones_reports_empty_schedule(self):
        res = self.client.post('/api/v1/bim/progress-validation/simulate/', {
            "project": str(self.project.id),
        }, format='json')
        self.assertEqual(res.status_code, 201)
        validation = BIMProgressValidation.objects.get(id=res.data['id'])
        # no milestone data -> no fabricated phases, zero progress measured
        self.assertEqual(validation.planned_vs_actual, [])
        self.assertEqual(validation.completed_elements_count, 0)
        self.assertEqual(validation.earned_value_usd, 'N/A')

    def test_simulate_requires_project(self):
        res = self.client.post('/api/v1/bim/progress-validation/simulate/', {}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_simulate_out_of_scope_project_forbidden(self):
        res = self.client.post('/api/v1/bim/progress-validation/simulate/', {
            "project": str(self.other_project.id),
        }, format='json')
        self.assertEqual(res.status_code, 403)

    def test_list_scoped_to_own_projects(self):
        BIMProgressValidation.objects.create(
            project=self.project, model=self.model,
            schedule_status='ON_TRACK', days_variance=0)
        BIMProgressValidation.objects.create(
            project=self.other_project, model=self.other_model,
            schedule_status='DELAYED', days_variance=-10)
        res = self.client.get('/api/v1/bim/progress-validation/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data), 1)
        self.assertEqual(res.data[0]['schedule_status'], 'ON_TRACK')


class BIMMilestoneAPITests(BIMAPITestBase):
    def _certified_model(self):
        model = BIMService.stamp_and_certify(self.model, self.user, "0xCERT")
        if not model.versions.filter(is_current=True).exists():
            BIMModelVersion.objects.create(
                model=model, version_label='v1.0', is_current=True,
                commit_hash='ab12cd34', author_name='Ada Okafor')
        return model

    def _milestone(self, model=None, **kwargs):
        defaults = dict(
            project=self.project, bim_model=model or self.model,
            name='Level 1 Core Alignment', phase='STRUCTURAL_FRAME',
            sequence_order=1, tolerance_max_mm=15.0, bim_deviation_mm=5.0,
            gpr_clearance_status='VERIFIED')
        defaults.update(kwargs)
        ms = BIMConstructionMilestone.objects.create(**defaults)
        # pin the current version like the service layer does
        current = ms.bim_model.versions.filter(is_current=True).first()
        if current:
            ms.model_version = current
            ms.save()
        return ms

    def test_create_milestone_via_api(self):
        model = self._certified_model()
        res = self.client.post('/api/v1/bim/milestones/', {
            "project": str(self.project.id),
            "bim_model": str(model.id),
            "name": "Level 2 Slab Pour Gate",
            "phase": "SUPERSTRUCTURE",
            "tolerance_max_mm": 12.0,
            "bim_deviation_mm": 3.4,
            "linked_inspections": [{"id": "INS-1", "outcome": "PASSED"}],
        }, format='json')
        self.assertEqual(res.status_code, 201)
        ms = BIMConstructionMilestone.objects.get(id=res.data['id'])
        self.assertEqual(ms.project, self.project)
        self.assertTrue(ms.milestone_code.startswith('BIM-MS-'))
        # certified model -> milestone starts as pending review
        self.assertEqual(ms.verification_status, 'PENDING_REVIEW')
        self.assertIsNotNone(ms.model_version)
        self.assertEqual(ms.model_version.version_label, 'v1.0')
        self.assertAlmostEqual(ms.tolerance_max_mm, 12.0)

    def test_create_milestone_out_of_scope_forbidden(self):
        res = self.client.post('/api/v1/bim/milestones/', {
            "project": str(self.other_project.id),
            "bim_model": str(self.other_model.id),
            "name": "sneaky milestone",
        }, format='json')
        self.assertEqual(res.status_code, 403)

    def test_gate_status_action_reports_blockers(self):
        model = self._certified_model()
        ms = self._milestone(
            model=model,
            bim_deviation_mm=28.0,  # above 15mm tolerance
            linked_clashes=[{"id": "1", "severity": "CRITICAL", "status": "OPEN"}],
            linked_inspections=[{"id": "INS-9", "outcome": "PENDING"}],
            gpr_clearance_status='ANOMALY_DETECTED')
        res = self.client.get(f'/api/v1/bim/milestones/{ms.id}/gate-status/')
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data['all_gates_passed'])
        self.assertFalse(res.data['can_digitally_sign'])
        gates = {g['key']: g for g in res.data['gates']}
        self.assertTrue(gates['model_approved']['passed'])
        self.assertFalse(gates['zero_critical_clashes']['passed'])
        self.assertFalse(gates['tolerance_compliant']['passed'])
        self.assertFalse(gates['inspections_passed']['passed'])
        self.assertFalse(gates['gpr_clear']['passed'])
        blockers = " ".join(res.data['blockers'])
        self.assertIn("tolerance", blockers)
        self.assertIn("clashes", blockers)

    def test_verify_action_success_and_refusal(self):
        model = self._certified_model()
        ms = self._milestone(model=model)
        # not the current version -> version gate fails -> 400
        BIMService.create_version(model, {"version_label": "v2.0"}, self.user)
        res = self.client.post(f'/api/v1/bim/milestones/{ms.id}/verify/', {}, format='json')
        self.assertEqual(res.status_code, 400)
        self.assertIn("error", res.data)
        # the new current version passes
        ms2 = self._milestone(model=model, name='Level 2 Gate', sequence_order=2)
        ms2.model_version = model.versions.filter(is_current=True).first()
        ms2.save()
        res = self.client.post(f'/api/v1/bim/milestones/{ms2.id}/verify/',
                               {"notes": "Structural directorate sign-off"}, format='json')
        self.assertEqual(res.status_code, 200)
        ms2.refresh_from_db()
        self.assertEqual(ms2.verification_status, 'VERIFIED')
        self.assertEqual(ms2.verified_by, self.user)
        self.assertTrue(ms2.digital_stamp_reference.startswith("0x"))

    def test_flag_deviation_action_creates_ncr_above_20mm(self):
        from apps.compliance.models import NonConformanceReport
        ms = self._milestone()
        res = self.client.post(f'/api/v1/bim/milestones/{ms.id}/flag-deviation/', {
            "deviation_mm": 24.0,
            "reason": "Scan showed 24mm column tilt at Grid 2-B.",
        }, format='json')
        self.assertEqual(res.status_code, 200)
        ms.refresh_from_db()
        self.assertEqual(ms.verification_status, 'DEVIATION_FLAGGED')
        self.assertAlmostEqual(ms.bim_deviation_mm, 24.0)
        self.assertEqual(len(ms.evidence_vault), 1)
        self.assertEqual(ms.evidence_vault[0]["deviation_mm"], 24.0)
        ncr = NonConformanceReport.objects.get(project=self.project)
        self.assertEqual(ncr.severity, 'Major')
        self.assertIn("24", ncr.title)

    def test_flag_deviation_below_threshold_no_ncr(self):
        from apps.compliance.models import NonConformanceReport
        ms = self._milestone()
        res = self.client.post(f'/api/v1/bim/milestones/{ms.id}/flag-deviation/', {
            "deviation_mm": 18.0,
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertFalse(NonConformanceReport.objects.filter(project=self.project).exists())

    def test_request_re_verification_action(self):
        model = self._certified_model()
        ms = self._milestone(model=model)
        BIMService.verify_and_stamp_milestone(ms, self.user)
        res = self.client.post(f'/api/v1/bim/milestones/{ms.id}/request-re-verification/',
                               {"reason": "Re-cast after remediation."}, format='json')
        self.assertEqual(res.status_code, 200)
        ms.refresh_from_db()
        self.assertEqual(ms.verification_status, 'RE_VERIFICATION_REQUIRED')
        self.assertIsNone(ms.digital_stamp_reference)

    def test_filters_and_cross_project_404(self):
        ms = self._milestone(name='Alpha Gate', phase='MEP_ROUGHIN',
                             verification_status='VERIFIED')
        other_ms = BIMConstructionMilestone.objects.create(
            project=self.other_project, bim_model=self.other_model, name='Epe Gate')
        res = self.client.get('/api/v1/bim/milestones/', {'phase': 'mep_roughin'})
        self.assertEqual([m['id'] for m in res.data], [str(ms.id)])
        res = self.client.get('/api/v1/bim/milestones/', {'verification_status': 'VERIFIED'})
        self.assertEqual([m['id'] for m in res.data], [str(ms.id)])
        res = self.client.get('/api/v1/bim/milestones/', {'bim_model': str(self.model.id)})
        self.assertEqual(len(res.data), 1)
        res = self.client.get('/api/v1/bim/milestones/', {'search': 'Alpha'})
        self.assertEqual([m['id'] for m in res.data], [str(ms.id)])
        res = self.client.get(f'/api/v1/bim/milestones/{other_ms.id}/')
        self.assertEqual(res.status_code, 404)
        res = self.client.post(f'/api/v1/bim/milestones/{other_ms.id}/verify/', {}, format='json')
        self.assertEqual(res.status_code, 404)


class BIMStatsOverviewAPITests(BIMAPITestBase):
    def test_overview_aggregates_only_scoped_projects(self):
        BIMService.stamp_and_certify(self.model, self.user, "0xSIG")
        BIMClash.objects.create(
            project=self.project, primary_model=self.model,
            clash_type='HARD_CLASH', title='A clash', description='d',
            severity='CRITICAL', status='OPEN')
        # rows in the other district that must be invisible
        BIMClash.objects.create(
            project=self.other_project, primary_model=self.other_model,
            clash_type='HARD_CLASH', title='Epe clash', description='d',
            severity='CRITICAL', status='OPEN')

        res = self.client.get('/api/v1/bim/stats/overview/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['models']['total'], 1)
        self.assertEqual(res.data['models']['certified'], 1)
        self.assertEqual(res.data['clashes']['active'], 1)
        self.assertEqual(res.data['clashes']['critical'], 1)
        # no validation on record -> no fabricated 4D figures
        self.assertIsNone(res.data['progress_4d']['schedule_status'])
        self.assertIsNone(res.data['progress_4d']['completed_elements'])
        self.assertIsNone(res.data['progress_4d']['earned_value'])

    def test_overview_reports_real_validation_values(self):
        BIMProgressValidation.objects.create(
            project=self.project, model=self.model, schedule_status='DELAYED',
            days_variance=-9, completed_elements_count=640, total_elements_count=1200,
            earned_value_usd='₦812.0M')
        res = self.client.get('/api/v1/bim/stats/overview/')
        self.assertEqual(res.data['progress_4d']['schedule_status'], 'DELAYED')
        self.assertEqual(res.data['progress_4d']['days_variance'], -9)
        self.assertEqual(res.data['progress_4d']['completed_elements'], 640)
        self.assertEqual(res.data['progress_4d']['earned_value'], '₦812.0M')


# =============================================================================
# Service layer: 4D simulation, clash matrix, milestone gates
# =============================================================================

class TimelineSimulationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='sim_user', email='sim@gov.ng',
                                             password='pw', first_name='Sim', last_name='User')
        self.project = Project.objects.create(
            name='Simulation Tower', reference_number='PRJ-SIM-1', lga='Ikeja',
            status='Active', estimated_project_value=250_000_000)
        self.model = BIMModel.objects.create(
            project=self.project, name='Sim Model', element_count=1000)

    def test_no_project_in_database_raises(self):
        Project.objects.all().delete()
        with self.assertRaises(ValueError):
            BIMService.run_timeline_simulation(None, self.user)

    def test_site_milestone_branch_computes_real_progress(self):
        ConstructionMilestone.objects.create(
            project=self.project, name='Piling', target_date=date.today(),
            progress_percentage=100, status='COMPLETED', sequence_order=1)
        ConstructionMilestone.objects.create(
            project=self.project, name='Frame', target_date=date.today(),
            progress_percentage=40, status='DELAYED', variance_days=7, sequence_order=2)
        v = BIMService.run_timeline_simulation(self.project.id, self.user)
        self.assertEqual(v.schedule_status, 'DELAYED')
        self.assertEqual(v.days_variance, -7)
        self.assertEqual(v.completed_elements_count, 700)
        self.assertEqual(v.total_elements_count, 1000)
        # earned value = 70% of the real 250M budget
        self.assertEqual(v.earned_value_usd, '₦175.0M')
        phases = {p['phase']: p for p in v.planned_vs_actual}
        self.assertEqual(phases['Piling']['status'], 'Completed')
        self.assertEqual(phases['Frame']['planned'], 52)

    def test_bim_milestone_branch_used_when_no_site_milestones(self):
        model = BIMService.stamp_and_certify(self.model, self.user, "0xS")
        BIMConstructionMilestone.objects.create(
            project=self.project, bim_model=model, name='Core Gate',
            verification_status='VERIFIED', sequence_order=1)
        BIMConstructionMilestone.objects.create(
            project=self.project, bim_model=model, name='Facade Gate',
            verification_status='DEVIATION_FLAGGED', sequence_order=2)
        v = BIMService.run_timeline_simulation(self.project.id, self.user)
        phases = {p['phase']: p for p in v.planned_vs_actual}
        self.assertEqual(phases['Core Gate']['actual'], 100)
        self.assertEqual(phases['Core Gate']['status'], 'Completed')
        self.assertEqual(phases['Facade Gate']['actual'], 45)
        self.assertEqual(phases['Facade Gate']['status'], 'Delayed - 5 Days')
        self.assertEqual(v.schedule_status, 'DELAYED')
        self.assertEqual(v.days_variance, -5)

    def test_project_lookup_by_name_fallback(self):
        v = BIMService.run_timeline_simulation('Simulation Tower', self.user)
        self.assertEqual(v.project, self.project)
        # no milestones at all -> empty schedule, not fabricated demo phases
        self.assertEqual(v.planned_vs_actual, [])
        self.assertEqual(v.completed_elements_count, 0)
        self.assertEqual(v.schedule_status, 'ON_TRACK')
        self.assertEqual(v.earned_value_usd, '₦0.0M')


class ClashMatrixServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='clash_user', email='clash@gov.ng',
                                             password='pw')
        self.project = Project.objects.create(name='Clash Site',
                                              reference_number='PRJ-CLASH-1',
                                              lga='Ikeja', status='Active')
        self.m_arch = BIMModel.objects.create(project=self.project, name='Arch',
                                              discipline='Architecture')
        self.m_mep = BIMModel.objects.create(project=self.project, name='MEP',
                                             discipline='MEP')

    def test_run_clash_matrix_resolves_models(self):
        clash = BIMService.run_clash_matrix(self.project.id, self.m_arch.id,
                                            self.m_mep.id, self.user)
        self.assertEqual(clash.project, self.project)
        self.assertEqual(clash.primary_model, self.m_arch)
        self.assertEqual(clash.secondary_model, self.m_mep)
        self.assertIn("Architecture", clash.title)
        self.assertIn("MEP", clash.title)

    def test_run_clash_matrix_falls_back_to_project_models(self):
        # unknown ids -> falls back to the project's own models
        clash = BIMService.run_clash_matrix(self.project.id, _uuid.uuid4(), None, self.user)
        self.assertEqual(clash.project, self.project)
        self.assertIn(clash.primary_model, (self.m_arch, self.m_mep))
        self.assertIsNotNone(clash.secondary_model)
        self.assertNotEqual(clash.primary_model, clash.secondary_model)

    def test_convert_clash_to_site_issue(self):
        clash = BIMService.run_clash_matrix(self.project.id, self.m_arch.id,
                                            self.m_mep.id, self.user)
        issue = BIMService.convert_clash_to_site_issue(clash, self.user)
        self.assertEqual(issue.project, self.project)
        self.assertEqual(issue.severity, clash.severity)
        clash.refresh_from_db()
        self.assertEqual(clash.converted_site_issue, issue)
        self.assertEqual(clash.status, 'CONVERTED_TO_ISSUE')
        self.assertTrue(SiteIssue.objects.filter(id=issue.id).exists())


class MilestoneGateServiceTests(TestCase):
    """Direct coverage of the remaining gate branches."""

    def setUp(self):
        self.user = User.objects.create_user(username='gate_user', email='gate@gov.ng',
                                             password='pw', first_name='Gate',
                                             last_name='Officer')
        self.project = Project.objects.create(name='Gate Site',
                                              reference_number='PRJ-GATE-1',
                                              lga='Ikeja', status='Active')
        self.model = BIMModel.objects.create(project=self.project, name='Gate Model')

    def _milestone(self, **kwargs):
        defaults = dict(project=self.project, bim_model=self.model,
                        name='Gate', tolerance_max_mm=15.0, bim_deviation_mm=0.0)
        defaults.update(kwargs)
        ms = BIMConstructionMilestone.objects.create(**defaults)
        # pin the current version like the service layer does
        current = self.model.versions.filter(is_current=True).first()
        if current:
            ms.model_version = current
            ms.save()
        return ms

    def test_uncertified_model_blocks(self):
        ms = self._milestone()
        gates = BIMService.evaluate_milestone_gate_status(ms)
        self.assertFalse(gates['all_gates_passed'])
        self.assertTrue(any("digital certification" in b for b in gates['blockers']))

    def test_version_mismatch_blocks(self):
        BIMService.stamp_and_certify(self.model, self.user, "0xV")
        ms = self._milestone()
        # a new version becomes current; the milestone pins the old one
        old_version = self.model.versions.first()
        BIMService.create_version(self.model, {"version_label": "v2.0"}, self.user)
        ms.model_version = old_version
        ms.save()
        gates = BIMService.evaluate_milestone_gate_status(ms)
        self.assertFalse(gates['gates'][1]['passed'])
        self.assertTrue(any("does not match" in b for b in gates['blockers']))

    def test_no_version_blocks(self):
        BIMService.stamp_and_certify(self.model, self.user, "0xV")
        ms = self._milestone()
        ms.model_version = None
        ms.save()
        gates = BIMService.evaluate_milestone_gate_status(ms)
        self.assertFalse(gates['all_gates_passed'])
        self.assertTrue(any("does not match" in b for b in gates['blockers']))

    def test_zero_clashes_gate_details(self):
        BIMService.stamp_and_certify(self.model, self.user, "0xV")
        ms = self._milestone(linked_clashes=[
            {"severity": "CRITICAL", "status": "OPEN"},
            {"severity": "HIGH", "status": "ASSIGNED"},
            {"severity": "CRITICAL", "status": "RESOLVED"},  # resolved: not blocking
            {"severity": "LOW", "status": "OPEN"},           # low: not blocking
        ])
        gates = BIMService.evaluate_milestone_gate_status(ms)
        clash_gate = gates['gates'][2]
        self.assertFalse(clash_gate['passed'])
        self.assertEqual(clash_gate['detail'], "2 open interferences detected")

    def test_inspection_and_gpr_gates(self):
        BIMService.stamp_and_certify(self.model, self.user, "0xV")
        ms = self._milestone(
            linked_inspections=[{"id": "1", "outcome": "PASSED"},
                                {"id": "2", "outcome": "FAILED"}],
            gpr_clearance_status='PENDING')
        gates = BIMService.evaluate_milestone_gate_status(ms)
        self.assertFalse(gates['all_gates_passed'])
        blockers = " ".join(gates['blockers'])
        self.assertIn("PASSED", blockers)
        self.assertIn("GPR", blockers)

    def test_all_gates_pass_and_verify(self):
        model = BIMService.stamp_and_certify(self.model, self.user, "0xV")
        if not model.versions.filter(is_current=True).exists():
            BIMModelVersion.objects.create(
                model=model, version_label='v1.0', is_current=True,
                commit_hash='a1b2c3d4', author_name='Gate Officer')
        ms = self._milestone(gpr_clearance_status='NOT_APPLICABLE',
                             linked_inspections=[{"id": "1", "outcome": "PASSED"}])
        gates = BIMService.evaluate_milestone_gate_status(ms)
        self.assertTrue(gates['all_gates_passed'])
        self.assertTrue(gates['can_digitally_sign'])
        verified = BIMService.verify_and_stamp_milestone(ms, self.user, "ok")
        self.assertEqual(verified.verification_status, 'VERIFIED')
        self.assertEqual(verified.signoff_metadata['signed_by'], 'Gate Officer')
        self.assertIsNotNone(verified.actual_verified_date)

    def test_deviation_ncr_severity_thresholds(self):
        from apps.compliance.models import NonConformanceReport
        BIMService.stamp_and_certify(self.model, self.user, "0xV")
        ms = self._milestone()
        # >35mm -> Critical NCR
        BIMService.flag_milestone_deviation(ms, self.user, {"deviation_mm": 41.0})
        ncr = NonConformanceReport.objects.get(project=self.project)
        self.assertEqual(ncr.severity, 'Critical')
        self.assertEqual(ncr.source, 'BIM_CLASH')
        # <=20mm -> no NCR
        NonConformanceReport.objects.all().delete()
        ms2 = self._milestone(name='Second Gate')
        BIMService.flag_milestone_deviation(ms2, self.user, {"deviation_mm": 15.0})
        self.assertFalse(NonConformanceReport.objects.exists())
        self.assertAlmostEqual(ms2.bim_deviation_mm, 15.0)
        self.assertEqual(len(ms2.evidence_vault), 1)
        self.assertEqual(ms2.evidence_vault[0]['file_type'], 'POINT_CLOUD_SURVEY')

