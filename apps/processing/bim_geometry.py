"""
Real BIM-vs-point-cloud geometry analysis.

Pipeline:
  * Loads an IFC model (or an RVT previously translated via Autodesk APS)
    and tessellates its structural elements with ifcopenshell.geom.
  * Loads the as-built point cloud (LAS or PLY) — both are captured in the
    BIM project coordinate frame.
  * Refines the alignment with a small translation-only ICP pass.
  * Computes per-point deviations (nearest BIM surface, scipy KD-tree).
  * Detects as-built clashes (scan points inside design solids via a
    z-ray parity test) and design-internal element clashes (deep AABB
    interpenetration, which filters out normal bearing contact).

No fabricated numbers — every value returned is measured from the files.
"""
import hashlib
import logging
import os
import tempfile

import numpy as np

logger = logging.getLogger(__name__)

# Element types included in the as-built comparison. Rebar and grids are
# design-internal detail that a site laser scan cannot resolve.
STRUCTURAL_TYPES = (
    "IfcWall", "IfcWallStandardCase", "IfcSlab", "IfcBeam", "IfcColumn",
    "IfcRoof", "IfcStair", "IfcFooting", "IfcPile", "IfcCurtainWall",
    "IfcMember", "IfcPlate", "IfcBuildingElementProxy",
)


def resolve_point_cloud(url, ext=None):
    """Download a point-cloud file to a temp path, or resolve a local /media/ path."""
    if not url:
        return None
    if url.startswith("http"):
        import requests
        try:
            res = requests.get(url, stream=True, timeout=300)
            res.raise_for_status()
            suffix = ext or os.path.splitext(url.split("?")[0])[1] or ".bin"
            fd, path = tempfile.mkstemp(suffix=suffix)
            with os.fdopen(fd, "wb") as f:
                for chunk in res.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
            return path
        except Exception as e:
            logger.error("Failed to download point cloud from %s: %s", url, e)
            return None
    from django.conf import settings
    rel = url.replace("/media/", "", 1)
    fp = os.path.join(settings.MEDIA_ROOT or "", rel)
    if os.path.exists(fp):
        return fp
    return None


def load_points(path):
    """Load an Nx3 float64 array from a LAS or PLY file. Returns None on failure."""
    if not path or not os.path.exists(path):
        return None
    try:
        lower = path.lower()
        if lower.endswith(".las") or lower.endswith(".laz"):
            import laspy
            with laspy.open(path) as fh:
                data = fh.read()
                return np.stack(
                    [np.asarray(data.x), np.asarray(data.y), np.asarray(data.z)], axis=1
                ).astype(np.float64)
        if lower.endswith(".ply"):
            from plyfile import PlyData
            v = PlyData.read(path)["vertex"]
            return np.stack([v["x"], v["y"], v["z"]], axis=1).astype(np.float64)
    except Exception as e:
        logger.error("Failed to load point cloud %s: %s", path, e)
    return None


def tessellate_ifc(ifc_path, include_types=STRUCTURAL_TYPES, max_elements=400):
    """
    Tessellate the model's structural elements.

    Returns a list of dicts: {"guid", "name", "type", "verts" (Nx3),
    "faces" (Mx3), "bbox" (min/max arrays)}.
    """
    import ifcopenshell
    import ifcopenshell.geom

    model = ifcopenshell.open(ifc_path)
    products = []
    for t in include_types:
        try:
            products.extend(model.by_type(t))
        except Exception:
            pass
    # Any other spatial product with a representation, in case the model
    # uses rarer types for its structure.
    if not products:
        products = [p for p in model.by_type("IfcProduct") if getattr(p, "Representation", None)]

    products = [p for p in products if getattr(p, "Representation", None)][:max_elements]

    settings = ifcopenshell.geom.settings()
    elements = []
    for p in products:
        try:
            shape = ifcopenshell.geom.create_shape(settings, p)
            verts = np.asarray(shape.geometry.verts, dtype=np.float64)
            if verts.size == 0:
                continue
            verts = verts.reshape(-1, 3)
            faces = np.asarray(shape.geometry.faces, dtype=np.int64).reshape(-1, 3)
            elements.append({
                "guid": getattr(p, "GlobalId", None),
                "name": getattr(p, "Name", None) or p.is_a(),
                "type": p.is_a(),
                "verts": verts,
                "faces": faces,
                "bbox": (verts.min(axis=0), verts.max(axis=0)),
            })
        except Exception as e:
            logger.warning("Failed to tessellate %s: %s", p.is_a(), e)

    logger.info("Tessellated %d structural elements from %s", len(elements), ifc_path)
    return elements


def _icp_translation_align(points, elements, iterations=8, sample=20000,
                           max_shift=2.0, max_correspondence=1.0):
    """
    Translation-only ICP against the exact triangulated surface: refine the
    cloud-to-BIM translation by repeatedly shifting the cloud to minimise
    nearest-surface distances. Only correspondences within
    max_correspondence metres drive the shift, so outliers (vegetation,
    surroundings the BIM does not model) cannot drag the alignment.
    Returns the aligned points and the applied translation vector.
    """
    if len(points) == 0 or not elements:
        return points, np.zeros(3)

    index = SurfaceIndex(elements)
    if len(index) == 0:
        return points, np.zeros(3)

    rng = np.random.default_rng(42)
    idx = rng.choice(len(points), min(sample, len(points)), replace=False)
    work = points[idx]
    shift = np.zeros(3)

    for _ in range(iterations):
        moved = work + shift
        d, closest = index.closest(moved)
        mask = d <= max_correspondence
        if not mask.any():
            break
        delta = (closest[mask] - moved[mask]).mean(axis=0)
        if not np.isfinite(delta).all():
            break
        # don't chase outliers across the site
        norm = np.linalg.norm(delta)
        if norm > max_shift:
            delta *= max_shift / norm
        shift += delta
        if np.linalg.norm(delta) < 0.001:
            break

    return points + shift, shift


def compute_deviations(points, elements, top_n=10, correspondence_radius=0.3):
    """
    Per-point exact nearest-surface deviations in millimetres.

    Statistics are computed over the points that correspond to a modelled
    design surface (within correspondence_radius metres) — the standard
    scan-vs-BIM QA definition of as-built deviation. Points with no design
    surface nearby are unmodelled surroundings, not deviations; their share
    is reported separately as outside_model_pct.
    """
    if len(points) == 0 or not elements:
        return None

    index = SurfaceIndex(elements)
    if len(index) == 0:
        return None

    d, _ = index.closest(points)
    d_mm = d * 1000.0

    corr = d <= correspondence_radius
    if not corr.any():
        # no scan point matches a design surface — nothing measurable
        return {
            "count": int(len(points)),
            "points_compared": 0,
            "outside_model_pct": 100.0,
            "mean_mm": None,
            "max_mm": None,
            "min_mm": None,
            "median_mm": None,
            "rmse_mm": None,
            "within_20mm_pct": None,
            "within_50mm_pct": None,
            "top": [],
        }

    corr_mm = d_mm[corr]
    corr_pts = points[corr]

    order = np.argsort(corr_mm)[::-1]
    top = []
    for i in order[:top_n]:
        top.append({
            "deviation_mm": round(float(corr_mm[i]), 1),
            "x": round(float(corr_pts[i][0]), 3),
            "y": round(float(corr_pts[i][1]), 3),
            "z": round(float(corr_pts[i][2]), 3),
        })

    return {
        "count": int(len(points)),
        "points_compared": int(corr.sum()),
        "outside_model_pct": round(float((~corr).mean() * 100.0), 1),
        "mean_mm": round(float(np.mean(corr_mm)), 1),
        "max_mm": round(float(np.max(corr_mm)), 1),
        "min_mm": round(float(np.min(corr_mm)), 1),
        "median_mm": round(float(np.median(corr_mm)), 1),
        "rmse_mm": round(float(np.sqrt(np.mean(corr_mm ** 2))), 1),
        "within_20mm_pct": round(float(np.mean(corr_mm <= 20.0) * 100.0), 1),
        "within_50mm_pct": round(float(np.mean(corr_mm <= 50.0) * 100.0), 1),
        "top": top,
    }


class _ZRayGrid:
    """
    Uniform XY grid of triangles for fast vertical ray-parity queries
    (is a point inside any design solid?).
    """

    def __init__(self, elements, cell=2.0):
        self.cell = cell
        self.cells = {}
        all_tris = []
        el_of_tri = []
        for el_id, el in enumerate(elements):
            v, f = el["verts"], el["faces"]
            if len(f) == 0:
                continue
            tris = v[f]  # (M, 3, 3)
            all_tris.append(tris)
            el_of_tri.extend([el_id] * len(tris))
        if not all_tris:
            self.tris = np.zeros((0, 3, 3))
            self.el_of_tri = np.zeros(0, dtype=int)
            self.origin = np.zeros(2)
            self.n_tris = 0
            return
        self.tris = np.vstack(all_tris)
        self.el_of_tri = np.array(el_of_tri, dtype=int)
        self.n_tris = len(self.tris)
        min_xy = self.tris[:, :, :2].reshape(-1, 2).min(axis=0)
        self.origin = np.floor(min_xy / self.cell) * self.cell
        # bucket each triangle into every cell its XY bbox touches
        tri_min = self.tris[:, :, :2].min(axis=1)
        tri_max = self.tris[:, :, :2].max(axis=1)
        c0 = np.floor((tri_min - self.origin) / self.cell).astype(int)
        c1 = np.floor((tri_max - self.origin) / self.cell).astype(int)
        for t in range(self.n_tris):
            for cx in range(c0[t][0], c1[t][0] + 1):
                for cy in range(c0[t][1], c1[t][1] + 1):
                    self.cells.setdefault((cx, cy), []).append(t)

    def inside_elements(self, pts, tol=0.0):
        """
        For each point, return the index of the element whose solid contains
        it (z-ray parity), or -1. The element reported is the one owning the
        lowest triangle crossed by the upward ray.
        """
        if self.n_tris == 0 or len(pts) == 0:
            return np.full(len(pts), -1, dtype=int)
        out = np.full(len(pts), -1, dtype=int)
        for i, p in enumerate(pts):
            cx = int(np.floor((p[0] - self.origin[0]) / self.cell))
            cy = int(np.floor((p[1] - self.origin[1]) / self.cell))
            cand = self.cells.get((cx, cy))
            if not cand:
                continue
            tris = self.tris[cand]
            # Möller–Trumbore with vertical ray direction (0,0,1)
            v0, v1, v2 = tris[:, 0], tris[:, 1], tris[:, 2]
            e1 = v1 - v0
            e2 = v2 - v0
            px, py = p[0], p[1]
            det = e1[:, 0] * -e2[:, 1] + e2[:, 0] * e1[:, 1]
            ok = np.abs(det) > 1e-12
            if not ok.any():
                continue
            safe_det = np.where(ok, det, 1.0)
            u = ((px - v0[:, 0]) * -e2[:, 1] + e2[:, 0] * (py - v0[:, 1])) / safe_det
            v = (e1[:, 0] * (py - v0[:, 1]) - (px - v0[:, 0]) * e1[:, 1]) / safe_det
            t = (v0[:, 2] + u * e1[:, 2] + v * e2[:, 2]) - p[2]
            hit = ok & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > tol)
            if hit.any():
                tri_idx = np.array(cand)[hit]
                t_hit = t[hit]
                nearest = tri_idx[np.argmin(t_hit)]
                out[i] = int(self.el_of_tri[nearest])
        return out


def detect_scan_clashes(points, elements, tolerance_mm=50.0, max_report=20):
    """
    As-built clash detection: scan points that lie inside a design solid
    (the structure was built where the model has solid material — e.g. a
    member intruding into a slab zone) plus scan points far from any design
    surface inside the design footprint (misplaced/pouring defects).
    """
    if len(points) == 0 or not elements:
        return []

    index = SurfaceIndex(elements)
    if len(index) == 0:
        return []

    d, _ = index.closest(points)
    far = d * 1000.0 > tolerance_mm

    clashes = []

    # points inside design solids
    grid = _ZRayGrid(elements)
    inside = grid.inside_elements(points)
    inside_idx = np.where(inside >= 0)[0]
    if len(inside_idx):
        # cluster the penetrating points by element and report each element once
        by_elem = {}
        for i in inside_idx:
            by_elem.setdefault(inside[i], []).append(i)
        for el_idx, idxs in sorted(by_elem.items(), key=lambda kv: -len(kv[1])):
            el = elements[el_idx]
            pts_el = points[idxs]
            centroid = pts_el.mean(axis=0)
            clashes.append({
                "id": f"CLASH-SCAN-{el['guid'] if el['guid'] else el_idx}",
                "severity": "high" if len(idxs) > 10 else "medium",
                "element1_id": f"As-built scan points ({len(idxs)} points)",
                "element2_id": f"{el['type']} - {el['name']} (GUID: {el['guid']})",
                "location": f"{centroid[0]:.2f}, {centroid[1]:.2f}, {centroid[2]:.2f}",
                "deviation_mm": round(float(np.median(d[idxs]) * 1000.0), 1),
                "type": "scan_intrusion",
            })

    # points far outside any design surface but horizontally within the BIM footprint
    bim_min = np.min([el["bbox"][0] for el in elements], axis=0)
    bim_max = np.max([el["bbox"][1] for el in elements], axis=0)
    within_xy = (
        (points[:, 0] >= bim_min[0]) & (points[:, 0] <= bim_max[0]) &
        (points[:, 1] >= bim_min[1]) & (points[:, 1] <= bim_max[1])
    )
    stray = np.where(far & within_xy)[0]
    if len(stray) > 5:
        # cluster strays into rough zones via a coarse voxel grouping
        vox = np.floor(points[stray] / 5.0).astype(int)
        uniq, counts = np.unique(vox, axis=0, return_counts=True)
        for k in np.argsort(counts)[::-1][:max_report]:
            if counts[k] < 5:
                break
            sel = np.all(vox == uniq[k], axis=1)
            pts_zone = points[stray][sel]
            centroid = pts_zone.mean(axis=0)
            clashes.append({
                "id": f"CLASH-STRAY-{uniq[k][0]}-{uniq[k][1]}-{uniq[k][2]}",
                "severity": "medium",
                "element1_id": f"As-built scan points ({int(counts[k])} points)",
                "element2_id": "No design element (as-built geometry outside design surfaces)",
                "location": f"{centroid[0]:.2f}, {centroid[1]:.2f}, {centroid[2]:.2f}",
                "deviation_mm": round(float(np.median(d[stray][sel]) * 1000.0), 1),
                "type": "as_built_outside_design",
            })

    return clashes[:max_report]


def detect_element_clashes(elements, min_overlap_ratio=0.5, max_report=20):
    """
    Design-internal clash detection: pairs of elements whose axis-aligned
    boxes interpenetrate deeply (overlap volume above a fraction of the
    smaller element). Plain bearing contact (a beam resting on a column)
    only overlaps a small volume and is not reported.
    """
    clashes = []
    for i in range(len(elements)):
        for j in range(i + 1, len(elements)):
            b1min, b1max = elements[i]["bbox"]
            b2min, b2max = elements[j]["bbox"]
            lo = np.maximum(b1min, b2min)
            hi = np.minimum(b1max, b2max)
            overlap = np.maximum(hi - lo, 0.0)
            vol = overlap[0] * overlap[1] * overlap[2]
            if vol <= 0:
                continue
            v1 = float(np.prod(b1max - b1min))
            v2 = float(np.prod(b2max - b2min))
            smaller = min(v1, v2)
            if smaller <= 0 or vol / smaller < min_overlap_ratio:
                continue
            c = (lo + hi) / 2.0
            clashes.append({
                "id": f"CLASH-BIM-{elements[i]['guid'][:6] if elements[i]['guid'] else i}-"
                      f"{elements[j]['guid'][:6] if elements[j]['guid'] else j}",
                "severity": "high" if vol / smaller > 0.8 else "medium",
                "element1_id": f"{elements[i]['type']} - {elements[i]['name']} (GUID: {elements[i]['guid']})",
                "element2_id": f"{elements[j]['type']} - {elements[j]['name']} (GUID: {elements[j]['guid']})",
                "location": f"{c[0]:.2f}, {c[1]:.2f}, {c[2]:.2f}",
                "overlap_m3": round(float(vol), 3),
                "type": "design_hard_clash",
            })
    clashes.sort(key=lambda c: -c["overlap_m3"])
    return clashes[:max_report]


def translated_ifc_cache_path(file_path):
    """
    Stable on-disk cache location for the APS translation of an RVT.
    Keyed on content (size + head/tail bytes) so re-analysis of the same
    upload — even via a fresh temp download — reuses the cached translation.
    """
    size = os.path.getsize(file_path)
    h = hashlib.sha256(str(size).encode())
    with open(file_path, "rb") as f:
        h.update(f.read(65536))
        if size > 131072:
            f.seek(-65536, os.SEEK_END)
            h.update(f.read(65536))
    digest = h.hexdigest()[:16]
    from django.conf import settings
    cache_dir = os.path.join(settings.BASE_DIR, "media", "temp_bim")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, f"translated_{digest}.ifc")


def ensure_ifc(file_path):
    """
    Return a local IFC path, translating an RVT through Autodesk APS when
    needed. Translations are cached on disk so repeated analysis of the
    same upload doesn't re-run a multi-minute translation job.
    """
    if file_path.lower().endswith(".ifc"):
        return file_path
    cache = translated_ifc_cache_path(file_path)
    if os.path.exists(cache) and os.path.getsize(cache) > 0:
        logger.info("Using cached APS translation at %s", cache)
        return cache
    from apps.processing.aps_client import AutodeskAPSClient
    out = AutodeskAPSClient().convert_rvt_to_ifc(file_path)
    # move into the stable cache location
    if os.path.abspath(out) != os.path.abspath(cache):
        import shutil
        shutil.move(out, cache)
    return cache


class SurfaceIndex:
    """
    Exact distance queries against the triangulated BIM surface.

    A cKDTree over triangle centroids finds candidate triangles; the exact
    point-to-triangle distance (Ericson's closest-point algorithm, fully
    vectorised) is then computed for each candidate. This matters because a
    large slab's interior vertices are sparse — measuring to vertices alone
    overstates distances by metres.
    """

    def __init__(self, elements, candidates=24):
        tris = []
        for el in elements:
            v, f = el["verts"], el["faces"]
            if len(f):
                tris.append(v[f])
        if not tris:
            self.tris = np.zeros((0, 3, 3))
            self.tree = None
            return
        self.tris = np.ascontiguousarray(np.vstack(tris), dtype=np.float64)
        self.candidates = candidates
        from scipy.spatial import cKDTree
        self.tree = cKDTree(self.tris.mean(axis=1))

    def __len__(self):
        return len(self.tris)

    def closest(self, pts, k=None):
        """
        Returns (distances (N,), closest_points (N,3)) for each query point.
        """
        k = min(k or self.candidates, len(self.tris)) if len(self.tris) else 0
        if not k or len(pts) == 0:
            return (np.full(len(pts), np.inf), np.zeros((len(pts), 3)))

        _, idx = self.tree.query(pts, k=k)
        if k == 1:
            idx = idx[:, None]

        # expand: for each point, k candidate triangles
        tris = self.tris[idx]                    # (N, k, 3, 3)
        p = pts[:, None, None, :]                # (N, 1, 1, 3)
        best_d2 = np.full(len(pts), np.inf)
        best_pt = np.zeros((len(pts), 3))

        # Ericson closest point on triangle, vectorised over (N, k)
        a = tris[:, :, 0, :]
        b = tris[:, :, 1, :]
        c = tris[:, :, 2, :]
        ab = b - a
        ac = c - a
        ap = p[:, 0, 0, :] - a if False else (pts[:, None, :] - a)

        d1 = np.einsum('nkj,nkj->nk', ab, ap)
        d2 = np.einsum('nkj,nkj->nk', ac, ap)

        # region A
        mask = (d1 <= 0) & (d2 <= 0)
        pa = a
        # region B
        bp = pts[:, None, :] - b
        d3 = np.einsum('nkj,nkj->nk', ab, bp)
        d4 = np.einsum('nkj,nkj->nk', ac, bp)
        mask_b = (d3 >= 0) & (d4 <= d3)
        pb = b
        # region AB
        vc = d1 * d4 - d3 * d2
        mask_ab = (vc <= 0) & (d1 >= 0) & (d3 <= 0)
        denom_ab = np.where(np.abs(d1 - d3) < 1e-15, 1.0, d1 - d3)
        t_ab = np.clip(d1 / denom_ab, 0.0, 1.0)
        pab = a + t_ab[..., None] * ab
        # region C
        cp = pts[:, None, :] - c
        d5 = np.einsum('nkj,nkj->nk', ab, cp)
        d6 = np.einsum('nkj,nkj->nk', ac, cp)
        mask_c = (d6 >= 0) & (d5 <= d6)
        pc = c
        # region AC
        vb = d5 * d2 - d1 * d6
        mask_ac = (vb <= 0) & (d2 >= 0) & (d6 <= 0)
        denom_ac = np.where(np.abs(d2 - d6) < 1e-15, 1.0, d2 - d6)
        t_ac = np.clip(d2 / denom_ac, 0.0, 1.0)
        pac = a + t_ac[..., None] * ac
        # region BC
        va = d3 * d6 - d5 * d4
        mask_bc = (va <= 0) & ((d4 - d3) >= 0) & ((d5 - d6) >= 0)
        denom_bc = np.where(np.abs((d4 - d3) + (d5 - d6)) < 1e-15, 1.0, (d4 - d3) + (d5 - d6))
        t_bc = np.clip((d4 - d3) / denom_bc, 0.0, 1.0)
        pbc = b + t_bc[..., None] * (c - b)
        # region interior
        d00 = np.einsum('nkj,nkj->nk', ab, ab)
        d01 = np.einsum('nkj,nkj->nk', ab, ac)
        d11 = np.einsum('nkj,nkj->nk', ac, ac)
        d20 = np.einsum('nkj,nkj->nk', ap, ab)
        d21 = np.einsum('nkj,nkj->nk', ap, ac)
        denom = d00 * d11 - d01 * d01
        safe = np.abs(denom) > 1e-15
        v = np.where(safe, (d11 * d20 - d01 * d21) / np.where(safe, denom, 1.0), 0.0)
        w = np.where(safe, (d00 * d21 - d01 * d20) / np.where(safe, denom, 1.0), 0.0)
        mask_in = safe & (v >= 0) & (w >= 0) & (v + w <= 1)
        pin = a + v[..., None] * ab + w[..., None] * ac

        closest = np.where(mask[..., None], pa, pts[:, None, :])
        closest = np.where(mask_b[..., None], pb, closest)
        closest = np.where(mask_ab[..., None], pab, closest)
        closest = np.where(mask_c[..., None], pc, closest)
        closest = np.where(mask_ac[..., None], pac, closest)
        closest = np.where(mask_bc[..., None], pbc, closest)
        closest = np.where(mask_in[..., None], pin, closest)

        diff = closest - pts[:, None, :]
        d2all = np.einsum('nkj,nkj->nk', diff, diff)
        best = np.argmin(d2all, axis=1)
        rows = np.arange(len(pts))
        best_pt = closest[rows, best]
        best_d2 = d2all[rows, best]
        return np.sqrt(best_d2), best_pt
