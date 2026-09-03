import os
import shutil
import tempfile
from unittest import mock

import numpy as np
from django.conf import settings
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework import status
from apps.scans.models import ScanSession
from django.contrib.auth import get_user_model

User = get_user_model()

class ProcessingE2ETestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='testuser', email='testuser@test.com', password='password')
        self.client.force_authenticate(user=self.user)
        # Create a mock session
        self.session = ScanSession.objects.create(
            scanner_id="TEST-SCANNER-001",
            status="initialized"
        )

    def test_edge_sync_endpoint(self):
        """Test that edge sync adds defects correctly."""
        url = reverse('edge_sync', kwargs={'session_id': str(self.session.id)})
        payload = {
            "edge_defects": [
                {
                    "type": "crack",
                    "severity": "high",
                    "location_x": 1.5,
                    "location_y": 2.5,
                    "location_z": 3.0,
                    "description": "Deep crack detected by edge model",
                    "confidence_score": 0.95
                }
            ]
        }

        response = self.client.post(url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['synced_count'], 1)
        self.assertEqual(self.session.defects.count(), 1)
        self.assertEqual(self.session.defects.first().severity, 'high')

    def test_bim_alignment_pipeline(self):
        """Test the BIM alignment pipeline."""
        url = f"/api/v1/scans/{self.session.id}/align-bim/"
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn("message", response.data)

    def test_clash_detection_pipeline(self):
        """Test the Clash detection pipeline."""
        url = f"/api/v1/scans/{self.session.id}/clash/"
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("clashes", response.data)


# =============================================================================
# Shared real-geometry fixtures
# =============================================================================

def make_box_element(name="Test Wall", type_name="IfcWall", lo=(0.0, 0.0, 0.0),
                     hi=(4.0, 0.2, 3.0), guid="WALL-GUID-0001"):
    """A real triangulated axis-aligned box mesh (8 verts / 12 triangles)."""
    x0, y0, z0 = lo
    x1, y1, z1 = hi
    verts = np.array([
        [x0, y0, z0], [x1, y0, z0], [x1, y1, z0], [x0, y1, z0],
        [x0, y0, z1], [x1, y0, z1], [x1, y1, z1], [x0, y1, z1],
    ], dtype=np.float64)
    faces = np.array([
        (0, 1, 2), (0, 2, 3),      # bottom (z0)
        (4, 6, 5), (4, 7, 6),      # top (z1)
        (0, 4, 5), (0, 5, 1),      # y0 face
        (2, 6, 7), (2, 7, 3),      # y1 face
        (1, 5, 6), (1, 6, 2),      # x1 face
        (0, 3, 7), (0, 7, 4),      # x0 face
    ], dtype=np.int64)
    return {
        "guid": guid, "name": name, "type": type_name,
        "verts": verts, "faces": faces,
        "bbox": (verts.min(axis=0), verts.max(axis=0)),
    }


def make_ifc_file(length=4.0, thickness=0.2, height=3.0, extra_walls=()):
    """
    Build a real IFC4 file with IfcWall(s) (extruded area solids) using
    ifcopenshell's authoring API. Returns the path of the written file.
    """
    import ifcopenshell
    import ifcopenshell.api as api

    f = ifcopenshell.file(schema="IFC4")
    project = api.run("root.create_entity", f, ifc_class="IfcProject", name="Test Project")
    api.run("unit.assign_unit", f)
    ctx = api.run("context.add_context", f, context_type="Model")
    body = api.run("context.add_context", f, context_type="Model",
                   context_identifier="Body", target_view="MODEL_VIEW", parent=ctx)
    site = api.run("root.create_entity", f, ifc_class="IfcSite", name="Site")
    building = api.run("root.create_entity", f, ifc_class="IfcBuilding", name="Building")
    storey = api.run("root.create_entity", f, ifc_class="IfcBuildingStorey", name="Ground Floor")
    api.run("aggregate.assign_object", f, relating_object=project, products=[site])
    api.run("aggregate.assign_object", f, relating_object=site, products=[building])
    api.run("aggregate.assign_object", f, relating_object=building, products=[storey])

    specs = [(length, thickness, height)] + list(extra_walls)
    for i, (l, t, h) in enumerate(specs):
        wall = api.run("root.create_entity", f, ifc_class="IfcWall", name=f"Test Wall {i + 1}")
        api.run("spatial.assign_container", f, relating_structure=storey, products=[wall])
        rep = api.run("geometry.add_wall_representation", f, context=body,
                      length=l, thickness=t, height=h)
        api.run("geometry.assign_representation", f, product=wall, representation=rep)
        api.run("geometry.edit_object_placement", f, product=wall)

    fd, path = tempfile.mkstemp(suffix=".ifc")
    os.close(fd)
    f.write(path)
    return path


def write_ply(points, path):
    """Write an (N,3) array as a real binary PLY point cloud."""
    from plyfile import PlyData, PlyElement
    arr = np.zeros(len(points), dtype=[('x', 'f4'), ('y', 'f4'), ('z', 'f4')])
    arr['x'] = points[:, 0]
    arr['y'] = points[:, 1]
    arr['z'] = points[:, 2]
    PlyData([PlyElement.describe(arr, 'vertex')]).write(path)
    return path


def write_las(points, path):
    """Write an (N,3) array as a real LAS 1.2 file (mm grid scaling)."""
    import laspy
    las = laspy.create(point_format=0, file_version="1.2")
    las.header.scales = np.array([0.001, 0.001, 0.001])
    las.header.offsets = np.array([0.0, 0.0, 0.0])
    # lowercase x/y/z are the scaled real-world coordinates
    las.x = points[:, 0]
    las.y = points[:, 1]
    las.z = points[:, 2]
    las.write(path)
    return path


class TempDirMixin:
    def setUp(self):
        super().setUp()
        self._tmpdirs = []
        self._media_files = []

    def _tempdir(self):
        d = tempfile.mkdtemp()
        self._tmpdirs.append(d)
        return d

    def _media_root(self):
        """A fresh MEDIA_ROOT, since the active storage backend may leave
        settings.MEDIA_ROOT empty (R2 has no local media dir)."""
        return self._tempdir()

    def _media_file(self, suffix, media_root):
        fd, path = tempfile.mkstemp(suffix=suffix, dir=media_root)
        os.close(fd)
        self._media_files.append(path)
        return path

    def tearDown(self):
        for p in self._media_files:
            if os.path.exists(p):
                os.unlink(p)
        for d in self._tmpdirs:
            shutil.rmtree(d, ignore_errors=True)
        super().tearDown()


# =============================================================================
# BIM geometry: exact surface distances
# =============================================================================

class SurfaceIndexTests(TempDirMixin, TestCase):
    """Exact point-to-triangle distance queries (Ericson closest point)."""

    def _single_triangle(self):
        tri = make_box_element  # noqa: keep flake quiet
        verts = np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=np.float64)
        faces = np.array([[0, 1, 2]], dtype=np.int64)
        return [{
            "guid": "TRI-1", "name": "tri", "type": "IfcWall",
            "verts": verts, "faces": faces,
            "bbox": (verts.min(axis=0), verts.max(axis=0)),
        }]

    def test_distance_above_triangle_interior(self):
        from apps.processing.bim_geometry import SurfaceIndex
        idx = SurfaceIndex(self._single_triangle())
        self.assertEqual(len(idx), 1)
        d, pt = idx.closest(np.array([[0.25, 0.25, 1.0]]))
        self.assertAlmostEqual(float(d[0]), 1.0, places=9)
        np.testing.assert_allclose(pt[0], [0.25, 0.25, 0.0], atol=1e-9)

    def test_distance_outside_vertex_region(self):
        from apps.processing.bim_geometry import SurfaceIndex
        idx = SurfaceIndex(self._single_triangle())
        # closest feature is the vertex (1,0,0): 1m away in x
        d, pt = idx.closest(np.array([[2.0, 0.0, 0.0]]))
        self.assertAlmostEqual(float(d[0]), 1.0, places=9)
        np.testing.assert_allclose(pt[0], [1.0, 0.0, 0.0], atol=1e-9)

    def test_distance_edge_region(self):
        from apps.processing.bim_geometry import SurfaceIndex
        idx = SurfaceIndex(self._single_triangle())
        # projects onto the edge (0,0,0)-(1,0,0) at (0.5,0,0)
        d, pt = idx.closest(np.array([[0.5, -0.5, 0.0]]))
        self.assertAlmostEqual(float(d[0]), 0.5, places=9)
        np.testing.assert_allclose(pt[0], [0.5, 0.0, 0.0], atol=1e-9)

    def test_large_surface_interior_distance_is_exact(self):
        """Distance to a big 10x10m plane is measured to the surface, not to
        sparse vertices — the reason SurfaceIndex exists."""
        from apps.processing.bim_geometry import SurfaceIndex
        wall = make_box_element(name="Slab", lo=(0, 0, 0), hi=(10, 10, 0.3))
        idx = SurfaceIndex([wall])
        # point 0.25m above the middle of the top face, far from every vertex
        d, pt = idx.closest(np.array([[5.0, 5.0, 0.55]]))
        self.assertAlmostEqual(float(d[0]), 0.25, places=9)
        np.testing.assert_allclose(pt[0], [5.0, 5.0, 0.3], atol=1e-9)

    def test_empty_index_returns_inf(self):
        from apps.processing.bim_geometry import SurfaceIndex
        el = make_box_element()
        el["faces"] = np.zeros((0, 3), dtype=np.int64)
        idx = SurfaceIndex([el])
        self.assertEqual(len(idx), 0)
        d, pt = idx.closest(np.array([[1.0, 0.1, 1.0]]))
        self.assertTrue(np.isinf(d[0]))

    def test_multiple_query_points(self):
        from apps.processing.bim_geometry import SurfaceIndex
        wall = make_box_element()
        idx = SurfaceIndex([wall])
        pts = np.array([
            [2.0, 0.1, 1.0],    # inside the wall solid -> nearest face 0.1m
            [2.0, 0.2, 1.0],    # exactly on the +y face
            [-1.0, 0.1, 1.0],   # 1m left of the x0 face
        ])
        d, _ = idx.closest(pts)
        self.assertAlmostEqual(float(d[0]), 0.1, places=9)
        self.assertAlmostEqual(float(d[1]), 0.0, places=9)
        self.assertAlmostEqual(float(d[2]), 1.0, places=9)


# =============================================================================
# BIM geometry: deviation statistics
# =============================================================================

class ComputeDeviationsTests(TempDirMixin, TestCase):
    def _wall(self):
        return make_box_element()

    def test_deviation_statistics_from_known_offsets(self):
        from apps.processing.bim_geometry import compute_deviations
        wall = self._wall()
        # points 10 / 25 / 60 mm off the +y face of the wall
        offsets = [0.010, 0.025, 0.060]
        pts = []
        for off in offsets:
            for x in (0.5, 1.5, 2.5, 3.5):
                for z in (0.5, 1.5, 2.5):
                    pts.append([x, 0.2 + off, z])
        pts = np.array(pts, dtype=np.float64)  # 36 points, 12 per offset
        res = compute_deviations(pts, [wall])
        self.assertIsNotNone(res)
        self.assertEqual(res["count"], 36)
        self.assertEqual(res["points_compared"], 36)
        self.assertEqual(res["outside_model_pct"], 0.0)
        self.assertAlmostEqual(res["mean_mm"], (10 + 25 + 60) / 3.0, places=1)
        self.assertEqual(res["min_mm"], 10.0)
        self.assertEqual(res["max_mm"], 60.0)
        self.assertEqual(res["median_mm"], 25.0)
        self.assertAlmostEqual(res["rmse_mm"], float(np.sqrt(np.mean([100, 625, 3600]))), places=1)
        self.assertAlmostEqual(res["within_20mm_pct"], 100.0 / 3.0, places=1)
        self.assertAlmostEqual(res["within_50mm_pct"], 200.0 / 3.0, places=1)
        # top offenders are the 60mm points, sorted descending
        self.assertEqual(len(res["top"]), 10)
        self.assertEqual(res["top"][0]["deviation_mm"], 60.0)
        self.assertEqual(res["top"][-1]["deviation_mm"], 60.0)
        # a top point is reported with its real coordinates
        top_pt = res["top"][0]
        self.assertIn((round(top_pt["x"], 3), round(top_pt["y"], 3), round(top_pt["z"], 3)),
                      [(round(p[0], 3), round(p[1], 3), round(p[2], 3)) for p in pts])

    def test_unmodelled_points_reported_as_outside(self):
        from apps.processing.bim_geometry import compute_deviations
        wall = self._wall()
        # 2 points 20cm off the face (within the 0.3m correspondence radius),
        # 1 point 5m away (unmodelled surroundings)
        pts = np.array([
            [1.0, 0.4, 1.0],
            [2.0, 0.4, 1.0],
            [1.0, 5.2, 1.0],
        ])
        res = compute_deviations(pts, [wall])
        self.assertEqual(res["points_compared"], 2)
        self.assertAlmostEqual(res["outside_model_pct"], 100.0 / 3.0, places=1)
        self.assertAlmostEqual(res["mean_mm"], 200.0, places=1)

    def test_no_correspondence_at_all(self):
        from apps.processing.bim_geometry import compute_deviations
        wall = self._wall()
        pts = np.array([[100.0, 100.0, 100.0], [120.0, 80.0, 60.0]])
        res = compute_deviations(pts, [wall])
        self.assertEqual(res["points_compared"], 0)
        self.assertEqual(res["outside_model_pct"], 100.0)
        self.assertIsNone(res["mean_mm"])
        self.assertEqual(res["top"], [])

    def test_empty_inputs_return_none(self):
        from apps.processing.bim_geometry import compute_deviations
        wall = self._wall()
        self.assertIsNone(compute_deviations(np.zeros((0, 3)), [wall]))
        self.assertIsNone(compute_deviations(np.array([[1.0, 1.0, 1.0]]), []))
        # faces-only index (no triangles) can't measure anything
        nofaces = make_box_element()
        nofaces["faces"] = np.zeros((0, 3), dtype=np.int64)
        self.assertIsNone(compute_deviations(np.array([[1.0, 1.0, 1.0]]), [nofaces]))


# =============================================================================
# BIM geometry: translation-only ICP alignment
# =============================================================================

class ICPAlignmentTests(TempDirMixin, TestCase):
    def _face_grid(self, n=6):
        xs = np.linspace(0.5, 3.5, n)
        zs = np.linspace(0.5, 2.5, n)
        xx, zz = np.meshgrid(xs, zs)
        grid = np.stack([xx.ravel(), np.full(xx.size, 0.2), zz.ravel()], axis=1)
        return grid

    def test_icp_recovers_pure_offset(self):
        from apps.processing.bim_geometry import _icp_translation_align, SurfaceIndex
        wall = make_box_element()
        grid = self._face_grid()
        shifted = grid + np.array([0.0, 0.06, 0.0])  # cloud 6cm off the +y face
        aligned, shift = _icp_translation_align(shifted, [wall])
        # the recovered translation pulls the cloud back onto the surface
        self.assertAlmostEqual(float(shift[1]), -0.06, delta=0.005)
        self.assertAlmostEqual(float(shift[0]), 0.0, delta=1e-9)
        self.assertAlmostEqual(float(shift[2]), 0.0, delta=1e-9)
        idx = SurfaceIndex([wall])
        d, _ = idx.closest(aligned)
        self.assertLess(float(np.median(d)), 0.005)

    def test_icp_ignores_far_outliers(self):
        """Outlier points (vegetation / surroundings) must not drag the ICP."""
        from apps.processing.bim_geometry import _icp_translation_align
        wall = make_box_element()
        grid = self._face_grid()
        shifted = grid + np.array([0.0, 0.06, 0.0])
        # 8 wild points far from the model, closer to each other than to the BIM
        outliers = np.array([[50.0, 40.0, 30.0], [50.5, 40.2, 30.1]] * 4)
        cloud = np.vstack([shifted, outliers])
        _, shift = _icp_translation_align(cloud, [wall])
        self.assertAlmostEqual(float(shift[1]), -0.06, delta=0.005)
        self.assertLess(abs(float(shift[0])), 0.01)
        self.assertLess(abs(float(shift[2])), 0.01)

    def test_icp_empty_inputs(self):
        from apps.processing.bim_geometry import _icp_translation_align
        pts = np.array([[1.0, 1.0, 1.0]])
        aligned, shift = _icp_translation_align(pts, [])
        self.assertEqual(len(aligned), 1)
        np.testing.assert_array_equal(shift, np.zeros(3))
        aligned, shift = _icp_translation_align(np.zeros((0, 3)), [make_box_element()])
        self.assertEqual(len(aligned), 0)


# =============================================================================
# BIM geometry: z-ray parity / as-built clash detection
# =============================================================================

class ZRayAndScanClashTests(TempDirMixin, TestCase):
    def test_inside_elements_parities(self):
        from apps.processing.bim_geometry import _ZRayGrid
        wall = make_box_element()
        grid = _ZRayGrid([wall])
        pts = np.array([
            [2.0, 0.1, 1.0],    # inside the wall solid
            [2.0, 0.1, 0.05],   # just above the bottom face -> inside
            [2.0, 0.1, 5.0],    # above the wall -> outside
            [2.0, 0.5, 1.0],    # beside the wall -> outside
            [-1.0, 0.1, 1.0],   # left of the wall -> outside
        ])
        out = grid.inside_elements(pts)
        self.assertEqual(out[0], 0)
        self.assertEqual(out[1], 0)
        self.assertEqual(out[2], -1)
        self.assertEqual(out[3], -1)
        self.assertEqual(out[4], -1)

    def test_inside_elements_reports_owning_element(self):
        from apps.processing.bim_geometry import _ZRayGrid
        wall_a = make_box_element(name="A", lo=(0, 0, 0), hi=(2, 2, 3), guid="A")
        wall_b = make_box_element(name="B", lo=(5, 0, 0), hi=(7, 2, 3), guid="B")
        grid = _ZRayGrid([wall_a, wall_b])
        out = grid.inside_elements(np.array([[1.0, 1.0, 1.0], [6.0, 1.0, 1.0]]))
        self.assertEqual(out[0], 0)
        self.assertEqual(out[1], 1)

    def test_scan_intrusion_detected(self):
        from apps.processing.bim_geometry import detect_scan_clashes
        wall = make_box_element()
        # 15 scan points inside the wall solid: structure built where the
        # model has solid material
        inside = np.array([[1.0 + 0.1 * i, 0.1, 1.5] for i in range(15)])
        clashes = detect_scan_clashes(inside, [wall])
        self.assertEqual(len(clashes), 1)
        clash = clashes[0]
        self.assertEqual(clash["type"], "scan_intrusion")
        self.assertEqual(clash["severity"], "high")  # > 10 points
        self.assertIn("IfcWall", clash["element2_id"])
        self.assertIn("WALL-GUID-0001", clash["element2_id"])
        self.assertEqual(clash["deviation_mm"], 100.0)  # 0.1m to nearest face
        self.assertTrue(clash["id"].startswith("CLASH-SCAN-"))

    def test_stray_points_outside_design_detected(self):
        from apps.processing.bim_geometry import detect_scan_clashes
        wall = make_box_element()
        # 10 points 0.6m above the wall top, inside its XY footprint
        stray = np.array([[1.0 + 0.05 * i, 0.1, 3.6] for i in range(10)])
        clashes = detect_scan_clashes(stray, [wall])
        types = [c["type"] for c in clashes]
        self.assertIn("as_built_outside_design", types)
        stray_clash = next(c for c in clashes if c["type"] == "as_built_outside_design")
        self.assertEqual(stray_clash["severity"], "medium")
        self.assertEqual(stray_clash["deviation_mm"], 600.0)
        self.assertTrue(stray_clash["id"].startswith("CLASH-STRAY-"))

    def test_clean_scan_produces_no_clashes(self):
        from apps.processing.bim_geometry import detect_scan_clashes
        wall = make_box_element()
        xs = np.linspace(0.5, 3.5, 6)
        zs = np.linspace(0.5, 2.5, 6)
        xx, zz = np.meshgrid(xs, zs)
        clean = np.stack([xx.ravel(), np.full(xx.size, 0.205), zz.ravel()], axis=1)
        self.assertEqual(detect_scan_clashes(clean, [wall]), [])

    def test_empty_inputs(self):
        from apps.processing.bim_geometry import detect_scan_clashes
        self.assertEqual(detect_scan_clashes(np.zeros((0, 3)), [make_box_element()]), [])
        self.assertEqual(detect_scan_clashes(np.array([[1.0, 1.0, 1.0]]), []), [])


class ElementClashTests(TempDirMixin, TestCase):
    def test_full_containment_is_high_severity_clash(self):
        from apps.processing.bim_geometry import detect_element_clashes
        big = make_box_element(name="Slab", lo=(0, 0, 0), hi=(10, 10, 10), guid="BIG")
        small = make_box_element(name="Column", lo=(1, 1, 1), hi=(2, 2, 2), guid="SMALL")
        clashes = detect_element_clashes([big, small])
        self.assertEqual(len(clashes), 1)
        self.assertEqual(clashes[0]["type"], "design_hard_clash")
        self.assertEqual(clashes[0]["severity"], "high")
        self.assertEqual(clashes[0]["overlap_m3"], 1.0)
        self.assertEqual(clashes[0]["id"], "CLASH-BIM-BIG-SMALL")

    def test_bearing_contact_not_reported(self):
        from apps.processing.bim_geometry import detect_element_clashes
        slab = make_box_element(name="Slab", lo=(0, 0, 0), hi=(10, 10, 1), guid="SLAB")
        # beam resting on the slab: only a small overlap volume
        beam = make_box_element(name="Beam", lo=(2, 2, 0.75), hi=(8, 3, 3), guid="BEAM")
        clashes = detect_element_clashes([slab, beam])
        self.assertEqual(clashes, [])

    def test_disjoint_elements_do_not_clash(self):
        from apps.processing.bim_geometry import detect_element_clashes
        a = make_box_element(name="A", lo=(0, 0, 0), hi=(1, 1, 1), guid="AAA")
        b = make_box_element(name="B", lo=(5, 5, 5), hi=(6, 6, 6), guid="BBB")
        self.assertEqual(detect_element_clashes([a, b]), [])

    def test_partial_overlap_medium_severity(self):
        from apps.processing.bim_geometry import detect_element_clashes
        a = make_box_element(name="A", lo=(0, 0, 0), hi=(2, 2, 2), guid="AAAA")
        # 60% of A's volume covered (B sticks out on all sides below/around)
        b = make_box_element(name="B", lo=(-1, -1, -1), hi=(2, 2, 1.2), guid="BBBB")
        clashes = detect_element_clashes([a, b])
        self.assertEqual(len(clashes), 1)
        self.assertEqual(clashes[0]["severity"], "medium")
        self.assertAlmostEqual(clashes[0]["overlap_m3"], 2 * 2 * 1.2)

    def test_sorted_by_overlap_and_capped(self):
        from apps.processing.bim_geometry import detect_element_clashes
        elems = [make_box_element(name="Big", lo=(0, 0, 0), hi=(10, 10, 10), guid="BIGG")]
        for i in range(25):
            # each subsequent box overlaps the big box slightly less
            hi = 10 - i * 0.1
            elems.append(make_box_element(name=f"S{i}", lo=(0, 0, 0), hi=(hi, 2, 2), guid=f"S{i:04d}"))
        clashes = detect_element_clashes(elems, max_report=5)
        self.assertEqual(len(clashes), 5)
        overlaps = [c["overlap_m3"] for c in clashes]
        self.assertEqual(overlaps, sorted(overlaps, reverse=True))
        # the largest overlap is the biggest box (10*2*2 = 40 m3)
        self.assertAlmostEqual(clashes[0]["overlap_m3"], 40.0)


# =============================================================================
# BIM geometry: real IFC tessellation and point-cloud file IO
# =============================================================================

class TessellationTests(TempDirMixin, TestCase):
    def test_tessellate_real_ifc_wall(self):
        from apps.processing.bim_geometry import tessellate_ifc
        path = make_ifc_file(length=4.0, thickness=0.2, height=3.0)
        self.addCleanup(os.unlink, path)
        elements = tessellate_ifc(path)
        self.assertEqual(len(elements), 1)
        el = elements[0]
        self.assertEqual(el["type"], "IfcWall")
        self.assertEqual(el["name"], "Test Wall 1")
        self.assertTrue(el["guid"])
        # real extruded box: 8 corners, 12 triangles, exact extents
        self.assertEqual(el["verts"].shape, (8, 3))
        self.assertEqual(el["faces"].shape, (12, 3))
        np.testing.assert_allclose(el["bbox"][0], [0.0, 0.0, 0.0], atol=1e-6)
        np.testing.assert_allclose(el["bbox"][1], [4.0, 0.2, 3.0], atol=1e-6)

    def test_tessellation_falls_back_to_any_represented_product(self):
        from apps.processing.bim_geometry import tessellate_ifc
        path = make_ifc_file()
        self.addCleanup(os.unlink, path)
        # no slab in the file -> the IfcProduct fallback still finds the wall
        elements = tessellate_ifc(path, include_types=("IfcSlab",))
        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["type"], "IfcWall")

    def test_multiple_walls_tessellated(self):
        from apps.processing.bim_geometry import tessellate_ifc
        path = make_ifc_file(extra_walls=[(6.0, 0.3, 3.5)])
        self.addCleanup(os.unlink, path)
        elements = tessellate_ifc(path)
        self.assertEqual(len(elements), 2)
        types = {el["type"] for el in elements}
        self.assertEqual(types, {"IfcWall"})

    def test_max_elements_cap(self):
        from apps.processing.bim_geometry import tessellate_ifc
        path = make_ifc_file(extra_walls=[(4.0, 0.2, 3.0)] * 6)
        self.addCleanup(os.unlink, path)
        elements = tessellate_ifc(path, max_elements=3)
        self.assertEqual(len(elements), 3)


class PointCloudFileIOTests(TempDirMixin, TestCase):
    def test_load_points_ply_roundtrip(self):
        from apps.processing.bim_geometry import load_points
        pts = np.array([[1.5, 0.2, 3.0], [2.0, 0.25, 2.5], [-4.0, 0.1, 7.5]])
        path = write_ply(pts, os.path.join(self._tempdir(), "cloud.ply"))
        loaded = load_points(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.shape, (3, 3))
        np.testing.assert_allclose(loaded, pts, atol=1e-5)

    def test_load_points_las_roundtrip(self):
        from apps.processing.bim_geometry import load_points
        pts = np.array([[1.5, 0.2, 3.0], [2.0, 0.25, 2.5], [-4.0, 0.1, 7.5]])
        path = write_las(pts, os.path.join(self._tempdir(), "cloud.las"))
        loaded = load_points(path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.shape, (3, 3))
        np.testing.assert_allclose(loaded, pts, atol=1e-3)  # mm grid

    def test_load_points_missing_and_corrupt(self):
        from apps.processing.bim_geometry import load_points
        self.assertIsNone(load_points(None))
        self.assertIsNone(load_points(os.path.join(self._tempdir(), "nope.las")))
        corrupt = os.path.join(self._tempdir(), "corrupt.ply")
        with open(corrupt, "wb") as f:
            f.write(b"not a ply file at all")
        self.assertIsNone(load_points(corrupt))

    def test_resolve_point_cloud_local_media(self):
        from apps.processing.bim_geometry import resolve_point_cloud
        self.assertIsNone(resolve_point_cloud(None))
        self.assertIsNone(resolve_point_cloud("/media/definitely-not-there-123.ply"))
        with override_settings(MEDIA_ROOT=self._media_root()):
            real = self._media_file(".ply", settings.MEDIA_ROOT)
            media_url = f"/media/{os.path.basename(real)}"
            self.assertEqual(resolve_point_cloud(media_url), real)

    def test_resolve_point_cloud_downloads_http(self):
        from apps.processing import bim_geometry as bg
        payload = b"fake-las-bytes"
        fake = mock.Mock(status_code=200)
        fake.iter_content.return_value = iter([payload])
        with mock.patch("requests.get", return_value=fake) as rget:
            out = bg.resolve_point_cloud("https://storage.example.com/cloud.las?sig=1", ext=".las")
        self.addCleanup(os.unlink, out)
        rget.assert_called_once()
        self.assertTrue(out.endswith(".las"))
        with open(out, "rb") as f:
            self.assertEqual(f.read(), payload)

    def test_resolve_point_cloud_http_failure(self):
        from apps.processing import bim_geometry as bg
        fake = mock.Mock(status_code=500)
        fake.raise_for_status.side_effect = Exception("boom")
        with mock.patch("requests.get", return_value=fake):
            self.assertIsNone(bg.resolve_point_cloud("https://storage.example.com/cloud.las"))


class EnsureIfcTests(TempDirMixin, TestCase):
    def test_ifc_passthrough(self):
        from apps.processing.bim_geometry import ensure_ifc
        path = make_ifc_file()
        self.addCleanup(os.unlink, path)
        self.assertEqual(ensure_ifc(path), path)

    def test_cache_path_is_deterministic_and_content_keyed(self):
        from apps.processing.bim_geometry import translated_ifc_cache_path
        d = self._tempdir()
        a = os.path.join(d, "a.rvt")
        b = os.path.join(d, "b.rvt")
        c = os.path.join(d, "c.rvt")
        with open(a, "wb") as f:
            f.write(b"x" * 200)
        with open(b, "wb") as f:
            f.write(b"x" * 200)
        with open(c, "wb") as f:
            f.write(b"y" * 200)
        self.assertEqual(translated_ifc_cache_path(a), translated_ifc_cache_path(b))
        self.assertNotEqual(translated_ifc_cache_path(a), translated_ifc_cache_path(c))
        self.assertTrue(translated_ifc_cache_path(a).endswith(".ifc"))

    def test_rvt_uses_cached_translation(self):
        from apps.processing.bim_geometry import ensure_ifc, translated_ifc_cache_path
        d = self._tempdir()
        rvt = os.path.join(d, "model.rvt")
        with open(rvt, "wb") as f:
            f.write(b"revit-binary-content")
        cache = translated_ifc_cache_path(rvt)
        self.addCleanup(lambda: os.path.exists(cache) and os.unlink(cache))
        ifc_content = make_ifc_file()
        self.addCleanup(os.unlink, ifc_content)
        shutil.copy(ifc_content, cache)
        # cached translation is reused without contacting APS
        self.assertEqual(ensure_ifc(rvt), cache)

    def test_rvt_translation_cached_after_conversion(self):
        from apps.processing import bim_geometry as bg
        d = self._tempdir()
        rvt = os.path.join(d, "model.rvt")
        with open(rvt, "wb") as f:
            f.write(b"revit-binary-content-v2")
        cache = bg.translated_ifc_cache_path(rvt)
        self.addCleanup(lambda: os.path.exists(cache) and os.unlink(cache))

        translated_src = make_ifc_file()
        with open(translated_src, "rb") as f:
            expected_content = f.read()

        with mock.patch("apps.processing.aps_client.AutodeskAPSClient") as cls:
            cls.return_value.convert_rvt_to_ifc.return_value = translated_src
            out = bg.ensure_ifc(rvt)
        self.assertEqual(out, cache)
        self.assertTrue(os.path.exists(cache))
        with open(cache, "rb") as f:
            self.assertEqual(f.read(), expected_content)
        # the raw APS output has been moved into the cache location
        self.assertFalse(os.path.exists(translated_src))


# =============================================================================
# APS client (all HTTP mocked — no real network)
# =============================================================================

class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", content=b"",
                 set_cookies=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text
        self.content = content
        self.headers = {}
        self.raw = mock.Mock()
        if set_cookies is not None:
            self.raw.headers.getlist.return_value = set_cookies
        else:
            self.raw.headers.getlist.return_value = []

    def json(self):
        if self._json is None:
            return {}
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise Exception(f"HTTP {self.status_code}")

    def iter_content(self, chunk_size=None):
        yield self.content


class APSClientTestCase(TestCase):
    def setUp(self):
        super().setUp()
        from apps.processing.aps_client import AutodeskAPSClient
        env = mock.patch.dict(os.environ, {
            "AUTODESK_CLIENT_ID": "test-client-id",
            "AUTODESK_CLIENT_SECRET": "test-client-secret",
        })
        env.start()
        self.addCleanup(env.stop)
        self.client = AutodeskAPSClient()
        self.tempdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tempdir, True)

    def _rvt(self, size=16):
        path = os.path.join(self.tempdir, "model.rvt")
        with open(path, "wb") as f:
            f.write(bytes(range(size)))
        return path


class APSAuthTests(APSClientTestCase):
    def test_missing_credentials_raise_valueerror(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("AUTODESK_CLIENT_ID", None)
            os.environ.pop("AUTODESK_CLIENT_SECRET", None)
            from apps.processing.aps_client import AutodeskAPSClient
            client = AutodeskAPSClient()
            with self.assertRaises(ValueError):
                client._get_token()

    def test_token_fetched_and_cached(self):
        token_resp = FakeResponse(json_data={"access_token": "tok-123", "expires_in": 3599})
        with mock.patch("apps.processing.aps_client.requests.post", return_value=token_resp) as post:
            self.assertEqual(self.client._get_token(), "tok-123")
            self.assertEqual(self.client._get_token(), "tok-123")
        self.assertEqual(post.call_count, 1)
        # two-legged flow: credentials in the form body
        _, kwargs = post.call_args
        self.assertEqual(kwargs["data"]["client_id"], "test-client-id")
        self.assertEqual(kwargs["data"]["client_secret"], "test-client-secret")
        self.assertEqual(kwargs["data"]["grant_type"], "client_credentials")

    def test_token_failure_raises(self):
        bad = FakeResponse(status_code=401, text="unauthorized")
        with mock.patch("apps.processing.aps_client.requests.post", return_value=bad):
            with self.assertRaisesRegex(Exception, "Authentication Failed"):
                self.client._get_token()

    def test_bucket_conflict_409_is_ok(self):
        ok = FakeResponse(status_code=409)
        token = FakeResponse(json_data={"access_token": "tok", "expires_in": 3599})
        with mock.patch("apps.processing.aps_client.requests.post",
                        side_effect=[token, ok]) as post:
            self.client._ensure_bucket()
        self.assertEqual(post.call_count, 2)
        # bucket POST carries the bearer token
        _, kwargs = post.call_args
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer tok")
        self.assertEqual(kwargs["json"]["bucketKey"], self.client.bucket_key)

    def test_bucket_failure_raises(self):
        token = FakeResponse(json_data={"access_token": "tok", "expires_in": 3599})
        bad = FakeResponse(status_code=500, text="server error")
        with mock.patch("apps.processing.aps_client.requests.post", side_effect=[token, bad]):
            with self.assertRaisesRegex(Exception, "Failed to ensure APS bucket"):
                self.client._ensure_bucket()


class APSUploadTests(APSClientTestCase):
    def _token_resp(self):
        return FakeResponse(json_data={"access_token": "tok", "expires_in": 3599})

    def test_single_part_upload(self):
        path = self._rvt(size=16)
        signed = FakeResponse(json_data={"urls": ["https://s3.example.com/part0"], "uploadKey": "key-1"})
        done = FakeResponse(json_data={"objectId": "urn:adsk.objects:os:nexucon_bim_processing_temp:model.rvt"})
        token = self._token_resp()

        def post(url, *args, **kwargs):
            if url.endswith("/authentication/v2/token"):
                return token
            if url.endswith("signeds3upload"):
                return done
            raise AssertionError(f"unexpected POST {url}")

        with mock.patch("apps.processing.aps_client.requests.post", side_effect=post) as post_m, \
                mock.patch("apps.processing.aps_client.requests.get", return_value=signed) as get, \
                mock.patch("apps.processing.aps_client.requests.put", return_value=FakeResponse(200)) as put:
            object_id = self.client._upload_to_oss(path, "model.rvt")
        self.assertEqual(object_id, "urn:adsk.objects:os:nexucon_bim_processing_temp:model.rvt")
        # the raw file bytes went to the signed S3 URL without auth headers
        put_args, put_kwargs = put.call_args
        self.assertEqual(put_args[0], "https://s3.example.com/part0")
        self.assertEqual(put_kwargs["data"], bytes(range(16)))
        # completion POST carries the upload key
        completion = [c for c in post_m.call_args_list if c.args[0].endswith("signeds3upload")]
        self.assertEqual(completion[-1].kwargs["json"], {"uploadKey": "key-1"})
        # signed URL requested with the real content length
        signed_call = get.call_args
        self.assertEqual(signed_call.kwargs["params"]["Content-Length"], "16")

    def test_multipart_upload_splits_file(self):
        path = self._rvt(size=10)
        signed = FakeResponse(json_data={
            "urls": ["https://s3.example.com/part0", "https://s3.example.com/part1"],
            "uploadKey": "key-mp",
        })
        done = FakeResponse(json_data={"objectId": "urn:mp"})
        with mock.patch("apps.processing.aps_client.requests.post",
                        side_effect=[self._token_resp(), done]), \
                mock.patch("apps.processing.aps_client.requests.get", return_value=signed), \
                mock.patch("apps.processing.aps_client.requests.put", return_value=FakeResponse(200)) as put:
            self.client._upload_to_oss(path, "model.rvt")
        parts = [c.kwargs["data"] for c in put.call_args_list]
        self.assertEqual(parts, [bytes(range(5)), bytes(range(5, 10))])

    def test_signed_url_failure(self):
        path = self._rvt()
        with mock.patch("apps.processing.aps_client.requests.post", return_value=self._token_resp()), \
                mock.patch("apps.processing.aps_client.requests.get", return_value=FakeResponse(500, text="err")):
            with self.assertRaisesRegex(Exception, "Signed Upload URL Generation Failed"):
                self.client._upload_to_oss(path, "model.rvt")

    def test_malformed_signed_response(self):
        path = self._rvt()
        with mock.patch("apps.processing.aps_client.requests.post", return_value=self._token_resp()), \
                mock.patch("apps.processing.aps_client.requests.get", return_value=FakeResponse(json_data={"urls": []})):
            with self.assertRaisesRegex(Exception, "Signed Upload Response Malformed"):
                self.client._upload_to_oss(path, "model.rvt")

    def test_s3_put_failure(self):
        path = self._rvt()
        signed = FakeResponse(json_data={"urls": ["https://s3.example.com/part0"], "uploadKey": "k"})
        with mock.patch("apps.processing.aps_client.requests.post", return_value=self._token_resp()), \
                mock.patch("apps.processing.aps_client.requests.get", return_value=signed), \
                mock.patch("apps.processing.aps_client.requests.put", return_value=FakeResponse(403, text="forbidden")):
            with self.assertRaisesRegex(Exception, "S3 Upload Failed"):
                self.client._upload_to_oss(path, "model.rvt")

    def test_completion_failure(self):
        path = self._rvt()
        signed = FakeResponse(json_data={"urls": ["https://s3.example.com/part0"], "uploadKey": "k"})
        with mock.patch("apps.processing.aps_client.requests.post",
                        side_effect=[self._token_resp(), FakeResponse(500, text="err")]), \
                mock.patch("apps.processing.aps_client.requests.get", return_value=signed), \
                mock.patch("apps.processing.aps_client.requests.put", return_value=FakeResponse(200)):
            with self.assertRaisesRegex(Exception, "Upload Completion Failed"):
                self.client._upload_to_oss(path, "model.rvt")


class APSConvertTests(APSClientTestCase):
    """End-to-end convert_rvt_to_ifc with every HTTP call mocked."""

    def _happy_mocks(self, manifest_statuses=("inprogress", "success")):
        token = FakeResponse(json_data={"access_token": "tok", "expires_in": 3599})
        bucket = FakeResponse(200)
        signed = FakeResponse(json_data={"urls": ["https://s3.example.com/part0"], "uploadKey": "key"})
        complete = FakeResponse(json_data={"objectId": "urn:adsk.objects:os:bkt:model.rvt"})
        job = FakeResponse(201, json_data={"result": "created"})
        manifests = []
        for s in manifest_statuses:
            if s == "success":
                manifests.append(FakeResponse(json_data={
                    "status": "success", "progress": "complete",
                    "derivatives": [{"children": [
                        {"role": "ifc", "status": "success", "urn": "derivative-urn-1"},
                    ]}],
                }))
            else:
                manifests.append(FakeResponse(json_data={"status": s, "progress": "50%"}))
        cookies = FakeResponse(
            json_data={"url": "https://cdn.example.com/translated.ifc"},
            set_cookies=["CloudFront-Policy=abc; Path=/", "CloudFront-Signature=def; Path=/",
                         "CloudFront-Key-Pair-Id=ghi; Path=/"],
        )
        download = FakeResponse(content=b"ISO-10303-21 translated ifc content")

        def post(url, *args, **kwargs):
            if url.endswith("/authentication/v2/token"):
                return token
            if url.endswith("/oss/v2/buckets"):
                return bucket
            if url.endswith("signeds3upload"):
                return complete
            if url.endswith("/designdata/job"):
                return job
            raise AssertionError(f"unexpected POST {url}")

        manifest_iter = iter(manifests)

        def get(url, *args, **kwargs):
            if url.endswith("signeds3upload"):
                return signed
            if url.endswith("/manifest"):
                return next(manifest_iter)
            if url.endswith("signedcookies"):
                return cookies
            if url.startswith("https://cdn.example.com/"):
                return download
            raise AssertionError(f"unexpected GET {url}")

        return post, get

    def test_convert_happy_path(self):
        path = self._rvt(size=32)
        post, get = self._happy_mocks()
        with mock.patch("apps.processing.aps_client.requests.post", side_effect=post) as post_m, \
                mock.patch("apps.processing.aps_client.requests.get", side_effect=get) as get_m, \
                mock.patch("apps.processing.aps_client.requests.put", return_value=FakeResponse(200)), \
                mock.patch("apps.processing.aps_client.time.sleep") as sleep:
            out = self.client.convert_rvt_to_ifc(path)
        self.assertTrue(sleep.called)
        self.assertTrue(out.endswith("model_translated.ifc"))
        self.assertEqual(os.path.dirname(out), self.tempdir)
        with open(out, "rb") as f:
            self.assertEqual(f.read(), b"ISO-10303-21 translated ifc content")
        # the CDN download forwarded the CloudFront cookies
        cdn_call = [c for c in get_m.call_args_list if c.args[0].startswith("https://cdn.example.com/")][0]
        cookie_header = cdn_call.kwargs["headers"]["Cookie"]
        self.assertIn("CloudFront-Policy=abc", cookie_header)
        self.assertIn("CloudFront-Signature=def", cookie_header)
        # one token fetch for the whole conversion
        token_calls = [c for c in post_m.call_args_list if c.args[0].endswith("/authentication/v2/token")]
        self.assertEqual(len(token_calls), 1)
        # the translation job targets the base64 URN of the uploaded object
        import base64
        urn = base64.b64encode(b"urn:adsk.objects:os:bkt:model.rvt").decode().rstrip("=")
        job_call = [c for c in post_m.call_args_list if c.args[0].endswith("/designdata/job")][0]
        self.assertEqual(job_call.kwargs["json"]["input"]["urn"], urn)
        self.assertEqual(job_call.kwargs["json"]["output"]["formats"], [{"type": "ifc"}])

    def test_convert_job_trigger_failure(self):
        path = self._rvt()
        post, get = self._happy_mocks()

        def post_fail(url, *args, **kwargs):
            if url.endswith("/designdata/job"):
                return FakeResponse(500, text="err")
            return post(url, *args, **kwargs)

        with mock.patch("apps.processing.aps_client.requests.post", side_effect=post_fail), \
                mock.patch("apps.processing.aps_client.requests.get", side_effect=get), \
                mock.patch("apps.processing.aps_client.requests.put", return_value=FakeResponse(200)):
            with self.assertRaisesRegex(Exception, "Translation Trigger Failed"):
                self.client.convert_rvt_to_ifc(path)

    def test_convert_manifest_reports_failure(self):
        path = self._rvt()
        post, get = self._happy_mocks(manifest_statuses=("failed",))
        with mock.patch("apps.processing.aps_client.requests.post", side_effect=post), \
                mock.patch("apps.processing.aps_client.requests.get", side_effect=get), \
                mock.patch("apps.processing.aps_client.requests.put", return_value=FakeResponse(200)), \
                mock.patch("apps.processing.aps_client.time.sleep"):
            with self.assertRaisesRegex(Exception, "APS Translation Failed"):
                self.client.convert_rvt_to_ifc(path)

    def test_convert_times_out_when_never_ready(self):
        path = self._rvt()
        post, get = self._happy_mocks(manifest_statuses=("inprogress",) * 50)
        fake_time = mock.Mock()
        # each time.time() call advances 10 minutes: token checks, the
        # deadline computation, and the poll loop all see a moving clock
        fake_time.time.side_effect = lambda: 1000.0 + 600.0 * len(fake_time.time.call_args_list)
        with mock.patch("apps.processing.aps_client.requests.post", side_effect=post), \
                mock.patch("apps.processing.aps_client.requests.get", side_effect=get), \
                mock.patch("apps.processing.aps_client.requests.put", return_value=FakeResponse(200)), \
                mock.patch("apps.processing.aps_client.time.sleep"), \
                mock.patch("apps.processing.aps_client.time.time", fake_time.time):
            with self.assertRaisesRegex(Exception, "timed out"):
                self.client.convert_rvt_to_ifc(path)

    def test_download_derivative_failure(self):
        path = self._rvt()
        post, get = self._happy_mocks()
        bad_cookies = FakeResponse(status_code=403, text="forbidden")

        def get_fail(url, *args, **kwargs):
            if url.endswith("signedcookies"):
                return bad_cookies
            return get(url, *args, **kwargs)

        # stop the pipeline right at the download step by making the manifest
        # succeed but the signedcookies call fail
        with mock.patch("apps.processing.aps_client.requests.post", side_effect=post), \
                mock.patch("apps.processing.aps_client.requests.get", side_effect=get_fail), \
                mock.patch("apps.processing.aps_client.requests.put", return_value=FakeResponse(200)), \
                mock.patch("apps.processing.aps_client.time.sleep"):
            with self.assertRaisesRegex(Exception, "Derivative Download URL Failed"):
                self.client.convert_rvt_to_ifc(path)


# =============================================================================
# BIMIFCService (processing/services.py)
# =============================================================================

class BIMIFCServiceTraversalTests(TempDirMixin, TestCase):
    def _polyline_product(self):
        import ifcopenshell
        f = ifcopenshell.file(schema="IFC4")
        p1 = f.create_entity("IfcCartesianPoint", (0.0, 0.0, 1.0))
        p2 = f.create_entity("IfcCartesianPoint", (4.0, 2.0, 3.0))
        p3 = f.create_entity("IfcCartesianPoint", (1.5, 2.5))  # 2D point
        pl = f.create_entity("IfcPolyline", [p1, p2, p3])
        return f, p1, p2, p3, pl

    def test_get_element_points_from_polyline(self):
        from apps.processing.services import BIMIFCService
        _, p1, p2, p3, pl = self._polyline_product()
        pts = BIMIFCService.get_element_points(pl)
        self.assertIn((0.0, 0.0, 1.0), pts)
        self.assertIn((4.0, 2.0, 3.0), pts)
        # 2D cartesian points get a zero z
        self.assertIn((1.5, 2.5, 0.0), pts)
        self.assertEqual(len(pts), 3)

    def test_get_element_points_none_and_depth(self):
        from apps.processing.services import BIMIFCService
        self.assertEqual(BIMIFCService.get_element_points(None), [])
        # depth-limited traversal still terminates on nested lists
        self.assertEqual(BIMIFCService.get_element_points([[[[["too deep"]]]]]), [])

    def test_bounding_box_from_real_polyline(self):
        from apps.processing.services import BIMIFCService
        _, p1, p2, p3, pl = self._polyline_product()
        box = BIMIFCService.get_bounding_box(pl)
        self.assertEqual(box["min_x"], 0.0)
        self.assertEqual(box["max_x"], 4.0)
        self.assertEqual(box["min_y"], 0.0)
        self.assertEqual(box["max_y"], 2.5)
        self.assertEqual(box["min_z"], 0.0)
        self.assertEqual(box["max_z"], 3.0)

    def test_bounding_box_degenerate_point_gets_padded(self):
        from apps.processing.services import BIMIFCService
        import ifcopenshell
        f = ifcopenshell.file(schema="IFC4")
        p = f.create_entity("IfcCartesianPoint", (2.0, 3.0, 4.0))
        box = BIMIFCService.get_bounding_box(p)
        # a single point gets a 1m cube so downstream overlap checks work
        self.assertEqual(box["min_x"], 1.5)
        self.assertEqual(box["max_x"], 2.5)
        self.assertEqual(box["min_z"], 3.5)
        self.assertIsNone(BIMIFCService.get_bounding_box(None))

    def test_bbox_overlap(self):
        from apps.processing.services import BIMIFCService
        a = {"min_x": 0, "max_x": 2, "min_y": 0, "max_y": 2, "min_z": 0, "max_z": 2}
        b = {"min_x": 1, "max_x": 3, "min_y": 1, "max_y": 3, "min_z": 1, "max_z": 3}
        c = {"min_x": 5, "max_x": 6, "min_y": 5, "max_y": 6, "min_z": 5, "max_z": 6}
        d = {"min_x": 2.05, "max_x": 3, "min_y": 0, "max_y": 1, "min_z": 0, "max_z": 1}
        self.assertTrue(BIMIFCService.check_bbox_overlap(a, b))
        self.assertFalse(BIMIFCService.check_bbox_overlap(a, c))
        self.assertFalse(BIMIFCService.check_bbox_overlap(a, None))
        # within the default 1cm tolerance
        self.assertTrue(BIMIFCService.check_bbox_overlap(a, d, tolerance=0.1))
        self.assertFalse(BIMIFCService.check_bbox_overlap(a, d))


class BIMIFCServiceAnalysisTests(TempDirMixin, TestCase):
    """Full as-built vs as-design analysis over a real IFC + real point cloud."""

    def _noisy_cloud(self, n_low=5, n_high=20):
        """Scan points on the wall's +y face: 20 at +50mm, 5 at +10mm."""
        pts = []
        for i in range(n_high):
            pts.append([0.5 + (i % 4), 0.2 + 0.05, 0.5 + (i // 4) * 0.4])
        for i in range(n_low):
            pts.append([0.5 + (i % 4), 0.2 + 0.01, 0.5 + (i // 4) * 0.4])
        return np.array(pts, dtype=np.float64)

    def test_analyze_session_measures_real_deviations(self):
        from apps.processing import bim_geometry as bg
        from apps.processing.services import BIMIFCService
        ifc = make_ifc_file()
        self.addCleanup(os.unlink, ifc)
        ply = write_ply(self._noisy_cloud(), os.path.join(self._tempdir(), "scan.ply"))

        result = BIMIFCService.analyze_session(ifc, ply_url=ply)
        self.assertEqual(result["bim_file"], ifc)
        self.assertIsNotNone(result["alignment"])
        self.assertEqual(result["alignment"]["method"], "translation_icp")
        self.assertEqual(result["alignment"]["point_source"], "gaussian splat PLY")
        self.assertEqual(result["alignment"]["points_used"], 25)
        # ICP removes the mean offset (+42mm) from the cloud
        self.assertAlmostEqual(result["alignment"]["translation"][0], 0.0, places=3)
        self.assertAlmostEqual(result["alignment"]["translation"][1], -0.042, delta=0.002)
        self.assertAlmostEqual(result["alignment"]["translation"][2], 0.0, places=3)

        dev = result["deviations"]
        self.assertIsNotNone(dev)
        self.assertEqual(dev["points_compared"], 25)
        self.assertEqual(dev["outside_model_pct"], 0.0)
        # residual distances: 8mm x20 and 32mm x5
        self.assertAlmostEqual(dev["mean_mm"], 12.8, delta=0.5)
        self.assertAlmostEqual(dev["min_mm"], 8.0, delta=0.5)
        self.assertAlmostEqual(dev["max_mm"], 32.0, delta=0.5)
        self.assertAlmostEqual(dev["median_mm"], 8.0, delta=0.5)
        self.assertAlmostEqual(dev["rmse_mm"], 16.0, delta=0.5)
        self.assertAlmostEqual(dev["within_20mm_pct"], 80.0, delta=0.5)
        self.assertAlmostEqual(dev["within_50mm_pct"], 100.0, delta=0.5)

        # the 5 low-offset points end up inside the wall solid after ICP ->
        # one as-built intrusion clash is reported, no strays
        self.assertEqual(len(result["clashes"]), 1)
        clash = result["clashes"][0]
        self.assertEqual(clash["type"], "scan_intrusion")
        self.assertAlmostEqual(clash["deviation_mm"], 32.0, delta=1.0)

    def test_analyze_session_prefers_las_over_ply(self):
        from apps.processing.services import BIMIFCService
        ifc = make_ifc_file()
        self.addCleanup(os.unlink, ifc)
        cloud = self._noisy_cloud()
        with override_settings(MEDIA_ROOT=self._media_root()):
            las = write_las(cloud, self._media_file(".las", settings.MEDIA_ROOT))
            ply = write_ply(cloud, self._media_file(".ply", settings.MEDIA_ROOT))
            result = BIMIFCService.analyze_session(
                ifc,
                point_cloud_url=f"/media/{os.path.basename(las)}",
                ply_url=f"/media/{os.path.basename(ply)}",
            )
        self.assertEqual(result["alignment"]["point_source"], "lidar LAS")
        self.assertEqual(result["alignment"]["points_used"], 25)

    def test_detect_clashes_returns_list(self):
        from apps.processing.services import BIMIFCService
        ifc = make_ifc_file()
        self.addCleanup(os.unlink, ifc)
        ply = write_ply(self._noisy_cloud(), os.path.join(self._tempdir(), "scan.ply"))
        clashes = BIMIFCService.detect_clashes(ifc, ply_url=ply)
        self.assertIsInstance(clashes, list)
        self.assertEqual(len(clashes), 1)

    def test_analyze_session_missing_file(self):
        from apps.processing.services import BIMIFCService
        result = BIMIFCService.analyze_session(os.path.join(self._tempdir(), "missing.ifc"))
        self.assertEqual(result["clashes"], [])
        self.assertIsNone(result["deviations"])
        self.assertIsNone(result["alignment"])

    def test_analyze_session_rvt_via_cached_translation(self):
        from apps.processing import bim_geometry as bg
        from apps.processing.services import BIMIFCService
        d = self._tempdir()
        rvt = os.path.join(d, "site_model.rvt")
        with open(rvt, "wb") as f:
            f.write(b"rvt-bytes-for-cache-test")
        cache = bg.translated_ifc_cache_path(rvt)
        self.addCleanup(lambda: os.path.exists(cache) and os.unlink(cache))
        ifc = make_ifc_file()
        self.addCleanup(os.unlink, ifc)
        shutil.copy(ifc, cache)
        ply = write_ply(self._noisy_cloud(), os.path.join(d, "scan.ply"))
        result = BIMIFCService.analyze_session(rvt, ply_url=ply)
        self.assertEqual(result["bim_file"], cache)
        self.assertIsNotNone(result["alignment"])

    def test_analyze_session_cloud_entirely_outside_zone(self):
        from apps.processing.services import BIMIFCService
        ifc = make_ifc_file()
        self.addCleanup(os.unlink, ifc)
        far = np.array([[500.0, 500.0, 500.0], [501.0, 500.0, 500.0]])
        ply = write_ply(far, os.path.join(self._tempdir(), "far.ply"))
        result = BIMIFCService.analyze_session(ifc, ply_url=ply)
        # nothing in the surveyed work zone -> no deviations, no alignment
        self.assertIsNone(result["deviations"])
        self.assertIsNone(result["alignment"])
        self.assertEqual(result["clashes"], [])


# =============================================================================
# Celery task: process_scan_pipeline (called synchronously, externals mocked)
# =============================================================================

class ProcessScanPipelineTests(TestCase):
    def setUp(self):
        from apps.projects.models import Project
        self.user = User.objects.create_user(username='scanop', email='scanop@test.com', password='pw')
        self.project = Project.objects.create(
            name='Pipeline Test Site', reference_number='PRJ-PIPE-1', lga='Ikeja', status='Active')
        self.session = ScanSession.objects.create(
            scanner_id="SCANNER-PIPE-01",
            status="initialized",
            project=self.project,
            rgb_url="https://cdn.example.com/scan/rgb.jpg",
            thermal_url="https://cdn.example.com/scan/thermal.jpg",
        )

    def _run(self):
        from apps.processing.tasks import process_scan_pipeline
        return process_scan_pipeline(self.session.id)

    def test_missing_session_returns_false(self):
        import uuid
        from apps.processing.tasks import process_scan_pipeline
        self.assertFalse(process_scan_pipeline(str(uuid.uuid4())))

    @mock.patch("apps.notifications.tasks.dispatch_webhooks")
    def test_full_pipeline_success(self, dispatch):
        from apps.audit.models import AuditEvent
        from apps.reports.models import QualityReport
        from apps.reports.services import ReportService
        from apps.scans.models import Defect, ProcessingTask
        from apps.scans.services import DataFusionService
        from apps.common.ai_service import AIService
        from apps.common.trimble_service import TrimbleConnectService

        dispatch.return_value.delay = mock.Mock()
        stale_report = QualityReport.objects.create(scan=self.session, status="completed")
        Defect.objects.create(session=self.session, type="crack", severity="low",
                              description="old defect replaced by reprocessing")

        visual_findings = [{
            "type": "crack", "severity": "high",
            "location_x": 1.2, "location_y": 0.4, "location_z": 2.8,
            "description": "Vertical flexural crack at grid B-2",
            "confidence_score": 0.91,
            "image_bbox": {"xmin": 0.1, "ymin": 0.2, "xmax": 0.5, "ymax": 0.6},
        }, {
            "type": "spalling", "severity": "medium",
            "location_x": 3.1, "location_y": 1.1, "location_z": 0.9,
            "description": "Cover spalling on column face",
            "confidence_score": 0.78,
        }]
        delam_findings = [{
            "type": "delamination", "severity": "high",
            "location_x": 2.0, "location_y": 2.0, "location_z": 3.0,
            "description": "Subsurface delamination confirmed by thermal+RGB fusion",
            "confidence_score": 0.83,
        }]

        with mock.patch.object(DataFusionService, "align_lidar_to_bim") as align, \
                mock.patch.object(DataFusionService, "calculate_progress") as progress, \
                mock.patch.object(DataFusionService, "apply_thermal_overlay") as overlay, \
                mock.patch.object(AIService, "detect_visual_defects", return_value=visual_findings), \
                mock.patch.object(AIService, "detect_delamination_multimodal", return_value=delam_findings), \
                mock.patch.object(ReportService, "generate_qaqc_report") as report, \
                mock.patch.object(TrimbleConnectService, "generate_defect_csv", return_value="id,severity\n1,high") as csv, \
                mock.patch.object(TrimbleConnectService, "generate_inspection_summary", return_value='{"defects": 3}') as summary, \
                mock.patch.object(TrimbleConnectService, "generate_ai_overlay_json", return_value='{"overlays": []}') as overlay_json, \
                mock.patch.object(TrimbleConnectService, "upload_files_to_trimble", return_value=True) as upload:
            result = self._run()

        self.assertTrue(result)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "completed")
        align.assert_called_once_with(self.session)
        progress.assert_called_once_with(self.session)
        overlay.assert_called_once_with(self.session)
        report.assert_called_once_with(self.session)
        csv.assert_called_once_with(self.session)
        summary.assert_called_once_with(self.session)
        overlay_json.assert_called_once_with(self.session)
        self.assertTrue(upload.called)

        # the stale report snapshot was dropped for regeneration
        self.assertFalse(QualityReport.objects.filter(id=stale_report.id).exists())

        # all three AI findings became real defect rows, old one replaced
        defects = Defect.objects.filter(session=self.session)
        self.assertEqual(defects.count(), 3)
        by_type = {d.type: d for d in defects}
        self.assertEqual(by_type["crack"].severity, "high")
        self.assertAlmostEqual(by_type["crack"].confidence_score, 0.91)
        self.assertAlmostEqual(by_type["crack"].bbox_xmin, 0.1)
        self.assertAlmostEqual(by_type["crack"].bbox_ymax, 0.6)
        self.assertEqual(by_type["delamination"].description,
                         "Subsurface delamination confirmed by thermal+RGB fusion")
        self.assertIsNone(by_type["spalling"].bbox_xmin)  # no bbox supplied

        task = ProcessingTask.objects.filter(session=self.session).order_by("-created_at").first()
        self.assertEqual(task.status, "completed")
        self.assertEqual(task.result_data, {"status": "success", "defects_detected": 3})

        # audit trail for start + completion
        actions = set(AuditEvent.objects.filter(resource_type__in=["processing_task", "scan_session"])
                      .values_list("action", flat=True))
        self.assertIn("PROCESSING_STARTED", actions)
        self.assertIn("PROCESSING_COMPLETED", actions)

        # completion webhook dispatched with the real session + project ids
        dispatch.delay.assert_any_call(
            "scan_processing_completed",
            {"session_id": str(self.session.id), "status": "completed",
             "message": "Processing pipeline finished."},
            str(self.project.id),
        )

    @mock.patch("apps.notifications.tasks.dispatch_webhooks")
    def test_pipeline_failure_marks_session_failed(self, dispatch):
        from apps.audit.models import AuditEvent
        from apps.scans.models import ProcessingTask
        from apps.scans.services import DataFusionService

        dispatch.return_value.delay = mock.Mock()
        with mock.patch.object(DataFusionService, "align_lidar_to_bim",
                               side_effect=Exception("alignment engine crashed")):
            result = self._run()

        self.assertFalse(result)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "failed")
        task = ProcessingTask.objects.filter(session=self.session).order_by("-created_at").first()
        self.assertEqual(task.status, "failed")
        self.assertEqual(task.result_data, {"error": "alignment engine crashed"})
        actions = set(AuditEvent.objects.values_list("action", flat=True))
        self.assertIn("PROCESSING_FAILED", actions)
        dispatch.delay.assert_any_call(
            "scan_processing_failed",
            {"session_id": str(self.session.id), "status": "failed",
             "error": "alignment engine crashed"},
            str(self.project.id),
        )

    @mock.patch("apps.notifications.tasks.dispatch_webhooks")
    def test_trimble_sync_failure_does_not_fail_pipeline(self, dispatch):
        from apps.scans.services import DataFusionService
        from apps.common.trimble_service import TrimbleConnectService

        dispatch.return_value.delay = mock.Mock()
        with mock.patch.object(DataFusionService, "align_lidar_to_bim"), \
                mock.patch.object(DataFusionService, "calculate_progress"), \
                mock.patch.object(DataFusionService, "apply_thermal_overlay"), \
                mock.patch.object(TrimbleConnectService, "generate_defect_csv",
                                  side_effect=Exception("trimble down")):
            result = self._run()

        self.assertTrue(result)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, "completed")


# =============================================================================
# Processing API views
# =============================================================================

class ProcessingViewsTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='procuser', email='proc@test.com', password='pw')
        self.client.force_authenticate(user=self.user)
        self.session = ScanSession.objects.create(scanner_id="VIEW-SCANNER-1", status="initialized")

    def test_edge_sync_requires_authentication(self):
        anon = APIClient()
        res = anon.post(reverse('edge_sync', kwargs={'session_id': str(self.session.id)}),
                        {"edge_defects": []}, format='json')
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_edge_sync_unknown_session_404(self):
        import uuid
        res = self.client.post(reverse('edge_sync', kwargs={'session_id': str(uuid.uuid4())}),
                               {"edge_defects": []}, format='json')
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_edge_sync_rejects_non_list(self):
        res = self.client.post(reverse('edge_sync', kwargs={'session_id': str(self.session.id)}),
                               {"edge_defects": "nope"}, format='json')
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_edge_sync_skips_non_dict_entries(self):
        res = self.client.post(
            reverse('edge_sync', kwargs={'session_id': str(self.session.id)}),
            {"edge_defects": ["junk", {"type": "crack", "severity": "low"}]}, format='json')
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["synced_count"], 1)
        self.assertEqual(self.session.defects.count(), 1)

    def test_node_status_get_reports_real_host(self):
        from apps.processing.models import ProcessingNode
        res = self.client.get('/api/v1/processing/node-status/')
        self.assertEqual(res.status_code, 200)
        self.assertIn('hostname', res.data)
        self.assertIn('queued_tasks', res.data)
        self.assertTrue(ProcessingNode.objects.filter(is_api_host=True).exists())

    def test_node_status_heartbeat_post(self):
        from apps.processing.models import ProcessingNode
        res = self.client.post('/api/v1/processing/node-status/', {
            "hostname": "gpu-worker-1", "status": "healthy",
            "cpu_utilization": 41.5, "gpu_utilization": 88.2,
            "memory_used_gb": 30.1, "memory_total_gb": 64.0, "gpu_workers": 2,
        }, format='json')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["hostname"], "gpu-worker-1")
        self.assertEqual(res.data["gpu_utilization"], 88.2)
        node = ProcessingNode.objects.get(hostname="gpu-worker-1")
        self.assertEqual(node.gpu_workers, 2)
        self.assertIsNotNone(node.last_heartbeat)

    def test_ai_models_list_seeds_registry(self):
        from apps.processing.models import AIModelVersion
        res = self.client.get('/api/v1/processing/ai-models/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data), 3)
        self.assertTrue(AIModelVersion.objects.filter(task_type='rebar_detection').exists())

    def test_ai_feedback_stats_computed_from_real_reviews(self):
        from apps.scans.models import Defect
        Defect.objects.create(session=self.session, type="crack", severity="high",
                              status="RESOLVED", confidence_score=0.9)
        Defect.objects.create(session=self.session, type="spalling", severity="low",
                              status="RESOLVED", is_false_positive=True, confidence_score=0.4)
        Defect.objects.create(session=self.session, type="corrosion", severity="low",
                              status="REJECTED", confidence_score=0.6)
        res = self.client.get('/api/v1/processing/ai-feedback-stats/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["total_detections"], 3)
        self.assertEqual(res.data["reviewed_count"], 3)
        self.assertEqual(res.data["false_positive_count"], 1)
        self.assertEqual(res.data["confirmed_count"], 1)
        self.assertEqual(res.data["rejected_count"], 1)
        self.assertAlmostEqual(res.data["false_positive_rate"], 33.33, places=2)
        self.assertAlmostEqual(res.data["true_positive_rate"], 33.33, places=2)
        self.assertAlmostEqual(res.data["mean_confidence"], 63.3, places=1)
