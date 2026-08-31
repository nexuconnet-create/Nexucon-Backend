import logging
import os
import ifcopenshell
import random

logger = logging.getLogger(__name__)

class ProcessingService:
    """
    Stub Processing Service.
    Holds business logic for starting and tracking AI scan inference/analysis.
    """
    pass

class BIMIFCService:
    """
    Service for parsing real BIM/IFC models and performing clash detection against point clouds.
    """
    @classmethod
    def get_element_points(cls, elem, visited=None, depth=0):
        """Gather coordinates of Cartesian points in an element's geometry safely with depth limit."""
        if depth > 4 or elem is None:
            return []
        if visited is None:
            visited = set()
        
        try:
            elem_id = elem.id()
        except Exception:
            elem_id = None

        if elem_id is not None:
            if elem_id in visited:
                return []
            visited.add(elem_id)

        points = []
        if hasattr(elem, "is_a"):
            if elem.is_a("IfcCartesianPoint"):
                coords = getattr(elem, "Coordinates", None)
                if coords:
                    if len(coords) >= 3:
                        points.append((float(coords[0]), float(coords[1]), float(coords[2])))
                    elif len(coords) == 2:
                        points.append((float(coords[0]), float(coords[1]), 0.0))
                return points

            if elem.is_a("IfcProduct"):
                rep = getattr(elem, "Representation", None)
                if rep:
                    points.extend(cls.get_element_points(rep, visited, depth + 1))
                
                placement = getattr(elem, "ObjectPlacement", None)
                if placement:
                    rel = getattr(placement, "RelativePlacement", None)
                    if rel:
                        loc = getattr(rel, "Location", None)
                        if loc:
                            points.extend(cls.get_element_points(loc, visited, depth + 1))
                return points

            if elem.is_a("IfcProductDefinitionShape") or elem.is_a("IfcShapeRepresentation"):
                items = getattr(elem, "Items", None) or getattr(elem, "Representations", None)
                if items:
                    for item in items:
                        points.extend(cls.get_element_points(item, visited, depth + 1))
                return points

            if elem.is_a("IfcGeometricRepresentationItem") or elem.is_a("IfcExtrudedAreaSolid") or elem.is_a("IfcPolyline") or elem.is_a("IfcFaceBasedSurfaceModel") or elem.is_a("IfcConnectedFaceSet") or elem.is_a("IfcFace") or elem.is_a("IfcPolyLoop"):
                for attr in ["Points", "SweptArea", "Bounds", "Loop", "Polygon", "Outer"]:
                    val = getattr(elem, attr, None)
                    if val:
                        points.extend(cls.get_element_points(val, visited, depth + 1))
                return points

        if isinstance(elem, (list, tuple)):
            for item in elem:
                points.extend(cls.get_element_points(item, visited, depth + 1))
            return points

        return points

    @classmethod
    def get_bounding_box(cls, elem):
        """Compute the 3D bounding box for an IFC element."""
        points = cls.get_element_points(elem)
        if not points:
            return None
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        zs = [p[2] for p in points]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        min_z, max_z = min(zs), max(zs)
        if min_x == max_x:
            min_x -= 0.5
            max_x += 0.5
        if min_y == max_y:
            min_y -= 0.5
            max_y += 0.5
        if min_z == max_z:
            min_z -= 0.5
            max_z += 0.5
        return {
            "min_x": min_x, "max_x": max_x,
            "min_y": min_y, "max_y": max_y,
            "min_z": min_z, "max_z": max_z,
        }

    @classmethod
    def check_bbox_overlap(cls, box1, box2, tolerance=0.01):
        """Check if two 3D bounding boxes overlap within a given tolerance."""
        if not box1 or not box2:
            return False
        return (
            (box1["min_x"] - tolerance) <= box2["max_x"] and (box1["max_x"] + tolerance) >= box2["min_x"] and
            (box1["min_y"] - tolerance) <= box2["max_y"] and (box1["max_y"] + tolerance) >= box2["min_y"] and
            (box1["min_z"] - tolerance) <= box2["max_z"] and (box1["max_z"] + tolerance) >= box2["min_z"]
        )

    @classmethod
    def detect_clashes(cls, ifc_filepath, point_cloud_url=None, ply_url=None):
        """
        Parses the IFC model (translating an RVT through Autodesk APS first)
        and detects spatial clashes: as-built scan points intruding into
        design solids or sitting outside design surfaces, plus hard
        element-on-element interpenetrations inside the design itself.
        """
        return cls.analyze_session(
            ifc_filepath, point_cloud_url=point_cloud_url, ply_url=ply_url
        ).get("clashes", [])

    @classmethod
    def analyze_session(cls, bim_filepath, point_cloud_url=None, ply_url=None):
        """
        Full as-built vs as-design analysis.

        Returns {
            "clashes": [...],            # list, clash-detection results
            "deviations": {...} | None,  # measured cloud-vs-BIM deviations (mm)
            "alignment": {...} | None,   # transformation applied to the cloud
            "bim_file": path,
        }
        """
        import numpy as np
        from apps.processing import bim_geometry as bg

        if not os.path.exists(bim_filepath):
            logger.error("BIM file not found: %s", bim_filepath)
            return {"clashes": [], "deviations": None, "alignment": None, "bim_file": bim_filepath}

        # RVT -> IFC via Autodesk APS (cached on disk after the first run)
        ifc_filepath = bg.ensure_ifc(bim_filepath)

        try:
            logger.info("Tessellating IFC model: %s", ifc_filepath)
            elements = bg.tessellate_ifc(ifc_filepath)
            if not elements:
                logger.warning("No tessellatable structural elements in %s", ifc_filepath)
                return {"clashes": [], "deviations": None, "alignment": None, "bim_file": ifc_filepath}

            # Design-internal hard clashes (deep AABB interpenetration)
            clashes = bg.detect_element_clashes(elements)

            # Prefer the real lidar LAS over the gaussian-splat PLY when both
            # are available — the LAS is the metric as-built survey.
            points = None
            source = None
            for url, ext, name in (
                (point_cloud_url, ".las", "lidar LAS"),
                (ply_url, ".ply", "gaussian splat PLY"),
            ):
                if not url:
                    continue
                p = bg.load_points(bg.resolve_point_cloud(url, ext))
                if p is not None and len(p):
                    points, source = p, name
                    break

            deviations = None
            alignment = None
            if points is not None:
                # Restrict the comparison to the surveyed work zone — the
                # BIM footprint plus a 10 m margin. A site laser scan also
                # captures surroundings the BIM does not model (vegetation,
                # terrain, plant); those points are not deviations.
                bim_min = np.min([el["bbox"][0] for el in elements], axis=0)
                bim_max = np.max([el["bbox"][1] for el in elements], axis=0)
                zone = (
                    (points[:, 0] >= bim_min[0] - 10.0) & (points[:, 0] <= bim_max[0] + 10.0) &
                    (points[:, 1] >= bim_min[1] - 10.0) & (points[:, 1] <= bim_max[1] + 10.0) &
                    (points[:, 2] >= bim_min[2] - 5.0) & (points[:, 2] <= bim_max[2] + 5.0)
                )
                points = points[zone]
                if len(points) == 0:
                    logger.warning("No scan points fall inside the BIM work zone")
                else:
                    aligned, shift = bg._icp_translation_align(points, elements)
                    deviations = bg.compute_deviations(aligned, elements)
                    alignment = {
                        "method": "translation_icp",
                        "translation": [round(float(s), 4) for s in shift],
                        "point_source": source,
                        "points_used": int(len(aligned)),
                    }
                    clashes.extend(bg.detect_scan_clashes(aligned, elements))

            return {
                "clashes": clashes,
                "deviations": deviations,
                "alignment": alignment,
                "bim_file": ifc_filepath,
            }

        except Exception as e:
            logger.error("Failed to analyse IFC model %s: %s", ifc_filepath, e, exc_info=True)
            return {"clashes": [], "deviations": None, "alignment": None, "bim_file": ifc_filepath}
