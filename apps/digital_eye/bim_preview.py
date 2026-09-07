"""
Build the stored 3D preview geometry for an imported BIM model.

Wraps apps.processing.bim_geometry.tessellate_ifc (the same ifcopenshell
tessellation the scan-to-BIM alignment uses) and converts its per-element
NumPy meshes into flat, JSON-serialisable lists bounded to a preview-friendly
size. The result is persisted on BIMModelGeometry by BIMElementImportView.
"""
import logging

from apps.processing.bim_geometry import tessellate_ifc

logger = logging.getLogger(__name__)

# Preview cap: elements denser than this are deterministically stride-sampled
# (every Nth triangle kept). This is a visualisation aid, not the analysis
# mesh — the alignment pipeline still tessellates from the source file.
MAX_TRIANGLES_PER_ELEMENT = 4000


def _decimate_faces(flat_faces):
    """
    Deterministically reduce a flat triangle-index list to at most
    MAX_TRIANGLES_PER_ELEMENT triangles, re-indexed to a compact range so the
    stored payload stays small. Unused vertices are harmless (three.js
    tolerates gaps in the index space) but compacting faces is the size win.
    """
    n_tris = len(flat_faces) // 3
    if n_tris <= MAX_TRIANGLES_PER_ELEMENT:
        return flat_faces
    stride = n_tris // MAX_TRIANGLES_PER_ELEMENT + 1
    kept_tris = [flat_faces[i * 3:i * 3 + 3] for i in range(0, n_tris, stride)]
    # Compact the index space: map the surviving indices to 0..k-1.
    remap = {}
    compact = []
    for tri in kept_tris:
        out = []
        for idx in tri:
            if idx not in remap:
                remap[idx] = len(remap)
            out.append(remap[idx])
        compact.extend(out)
    return compact


def build_preview_geometry(ifc_path):
    """
    Tessellate an IFC file into a JSON-safe preview mesh list.

    Returns a list of dicts: {"guid", "name", "type", "verts" (flat
    [x, y, z, ...] floats), "faces" (flat [i, j, k, ...] ints)}. Raises
    whatever tessellate_ifc raises — the caller decides how to degrade.
    """
    tessellated = tessellate_ifc(ifc_path)
    preview = []
    for el in tessellated:
        verts = el['verts']
        faces = el['faces']
        if verts is None or faces is None or len(faces) == 0:
            continue
        flat_faces = _decimate_faces(faces.reshape(-1).tolist())
        # Quantise coordinates to 1 mm — plenty for a preview, keeps the
        # stored JSON (and the API payload) small.
        flat_verts = [round(float(v), 3) for v in verts.reshape(-1).tolist()]
        preview.append({
            'guid': el['guid'] or '',
            'name': el['name'] or el['type'] or '',
            'type': el['type'] or '',
            'verts': flat_verts,
            'faces': flat_faces,
        })
    logger.info('Built 3D preview geometry: %d elements from %s', len(preview), ifc_path)
    return preview
