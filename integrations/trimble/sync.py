"""
Trimble BIM sync service (implementation plan §5 Weeks 2–3).

Discovers Trimble Connect projects and their BIM models, extracts IFC
GlobalIds / element identities from model files and upserts
BIMElementMapping rows (the GUID mapping table the correlation engine uses
to attach NDT evidence to design elements).

IFC parsing is deterministic (ifcopenshell). When a model's file cannot be
downloaded through the API the model is recorded and the limitation is
reported — no element data is invented.
"""
import logging
import re
import tempfile

import requests

from apps.digital_eye.models import BIMElementMapping, TrimbleProject

from .client import REQUEST_TIMEOUT, TrimbleClient, TrimbleError

logger = logging.getLogger(__name__)

# GlobalId format: 22 chars from the base64 alphabet used by IFC.
_IFC_GUID_RE = re.compile(r"'([0-9A-Za-z_$]{22})'")


class IFCElementExtractor:
    """
    Extracts structural elements from an IFC STEP file. Uses ifcopenshell
    when available and falls back to a deterministic STEP-line parser
    (entity id, type, GlobalId, Name) for files ifcopenshell cannot open.
    """

    # Entity types relevant for structural correlation.
    STRUCTURAL_TYPES_RE = re.compile(
        r'Ifc(Column|Beam|Slab|Wall|Footing|Pile|Member|Plate|Covering|Stair|'
        r'Ramp|Roof|CurtainWall|BuildingElementProxy|Chimney|Pile\s*Cap)', re.I,
    )

    @classmethod
    def extract(cls, file_obj):
        """
        Yield dicts: {bim_guid, element_id, element_name, element_type,
        level, coordinates, properties}.
        """
        try:
            return cls._extract_with_ifcopenshell(file_obj)
        except ImportError:
            file_obj.seek(0)
            return cls._extract_from_step_text(file_obj)
        except Exception:  # noqa: BLE001 — unparseable file: STEP fallback
            logger.exception('ifcopenshell failed to parse IFC file; falling back to STEP parser')
            file_obj.seek(0)
            return cls._extract_from_step_text(file_obj)

    @classmethod
    def _extract_with_ifcopenshell(cls, file_obj):
        import ifcopenshell

        file_obj.seek(0)
        with tempfile.NamedTemporaryFile(suffix='.ifc', delete=False) as tmp:
            for chunk in file_obj.chunks() if hasattr(file_obj, 'chunks') else [file_obj.read()]:
                tmp.write(chunk)
            tmp_path = tmp.name
        try:
            ifc = ifcopenshell.open(tmp_path)
        finally:
            import os
            os.unlink(tmp_path)

        # Length-unit scale (file units -> SI metres), used to report
        # material layer thicknesses in mm whatever unit the file uses.
        try:
            from ifcopenshell.util.unit import calculate_unit_scale
            unit_scale = calculate_unit_scale(ifc)
        except Exception:
            unit_scale = 1.0

        elements = []
        for product in ifc.by_type('IfcBuildingElement'):
            guid = getattr(product, 'GlobalId', None)
            if not guid:
                continue
            element_type = product.is_a()
            # Only structural / physical building elements.
            if not cls.STRUCTURAL_TYPES_RE.search(element_type):
                continue
            level = ''
            try:
                from ifcopenshell.util.element import get_container
                container = get_container(product)
                if container is not None:
                    level = getattr(container, 'Name', '') or ''
            except Exception:
                pass
            coordinates = None
            try:
                placement = getattr(product, 'ObjectPlacement', None)
                if placement is not None:
                    # Prefer WORLD coordinates (the placement matrix
                    # accumulated up the spatial hierarchy) — local
                    # placements are usually 0,0,0 in Revit IFC exports.
                    try:
                        from ifcopenshell.util.placement import get_local_placement
                        matrix = get_local_placement(placement)
                        location = [float(matrix[0][3]), float(matrix[1][3]),
                                    float(matrix[2][3])]
                        source = 'ifc_world_placement'
                    except Exception:
                        location = (placement.RelativePlacement.Location
                                    .Coordinates)
                        source = 'ifc_object_placement'
                    coordinates = {
                        'x': float(location[0]) if len(location) > 0 else None,
                        'y': float(location[1]) if len(location) > 1 else None,
                        'z': float(location[2]) if len(location) > 2 else None,
                        'source': source,
                    }
            except Exception:
                pass
            # The FULL property set the model actually carries — every
            # property set AND quantity set value, keyed
            # '{PsetOrQtoName}.{PropertyName}'. These are the properties
            # attributed to each element in the platform's model preview;
            # nothing is summarised or dropped here.
            properties = {}
            try:
                from ifcopenshell.util.element import get_psets
                # Default flags return BOTH property sets and quantity sets.
                psets = get_psets(product)
                for pset_name, pset in (psets or {}).items():
                    for key, value in pset.items():
                        if key == 'id':  # ifcopenshell bookkeeping, not a model property
                            continue
                        if value is None or value == '':
                            continue
                        properties[f'{pset_name}.{key}'] = str(value)
            except Exception:
                pass
            # Direct attributes and associations the file carries even when
            # property sets are absent — Autodesk APS RVT->IFC translations
            # export geometry only, but the element Tag, its family/type
            # name and its material association are still real attributed
            # data and must not be dropped. Pset-derived values win on
            # key collisions (setdefault).
            tag = getattr(product, 'Tag', None)
            if tag:
                properties.setdefault('Tag', str(tag))
            try:
                from ifcopenshell.util.element import get_type
                # NOTE: get_type returns the TYPE ENTITY, not a string —
                # keep it in its own variable so the clean is_a() class
                # name in element_type is never clobbered (a str()'d
                # entity prints as a raw STEP line like
                # "#64=IfcSlabType('232RR...', ...)" in every surface
                # that shows the category).
                type_product = get_type(product)
                type_name = getattr(type_product, 'Name', None)
                if type_name:
                    properties.setdefault('ElementType', str(type_name))
            except Exception:
                pass
            try:
                from ifcopenshell.util.element import get_material
                material = get_material(product)
                if material is not None:
                    # A layer set (or a usage pointing at one) is described
                    # by its layers; thicknesses are raw model values.
                    layer_set = getattr(material, 'ForLayerSet', material)
                    layers = getattr(layer_set, 'MaterialLayers', None)
                    if layers:
                        layer_names = ', '.join(
                            f'{getattr(l.Material, "Name", "?")} '
                            f'({l.LayerThickness * unit_scale * 1000:g} mm)'
                            for l in layers if getattr(l, 'Material', None) is not None)
                        if layer_names:
                            properties.setdefault('Material', layer_names)
                    else:
                        material_name = getattr(material, 'Name', None)
                        if material_name:
                            properties.setdefault('Material', str(material_name))
            except Exception:
                pass

            name = getattr(product, 'Name', '') or ''
            mark = properties.get('Pset_ConcreteElementCommon.Mark') \
                or properties.get('Pset_BeamCommon.Mark') \
                or properties.get('Pset_ColumnCommon.Mark') \
                or properties.get('Pset_SlabCommon.Mark') \
                or properties.get('Pset_WallCommon.Mark') \
                or ''
            elements.append({
                'bim_guid': guid,
                'element_id': mark or name or '',
                'element_name': name,
                'element_type': element_type,
                'level': level,
                'coordinates': coordinates,
                'properties': properties,
            })
        return elements

    @classmethod
    def _extract_from_step_text(cls, file_obj):
        """Deterministic STEP fallback: entity lines with GlobalIds."""
        file_obj.seek(0)
        if hasattr(file_obj, 'chunks'):
            content = b''.join(file_obj.chunks())
        else:
            content = file_obj.read()
        try:
            text = content.decode('utf-8', errors='ignore')
        except Exception:
            return []
        elements = []
        for match in re.finditer(
                r"#\d+\s*=\s*(IFC[A-Z0-9_]+)\s*\(([^)]*)\)", text):
            entity_type, args = match.group(1), match.group(2)
            if not cls.STRUCTURAL_TYPES_RE.search(entity_type):
                continue
            strings = re.findall(r"'([^']*)'", args)
            guid_match = _IFC_GUID_RE.search(args)
            if not guid_match:
                continue
            # IFC entities: GlobalId first, then OwnerHistory, then Name.
            guid = guid_match.group(1)
            name = strings[1] if len(strings) > 1 else (strings[0] if strings else '')
            elements.append({
                'bim_guid': guid,
                'element_id': name,
                'element_name': name,
                'element_type': entity_type,
                'level': '',
                'coordinates': None,
                'properties': {},
            })
        return elements


class TrimbleSyncService:
    """Syncs Trimble Connect projects, models and BIM GUID mappings."""

    def __init__(self, client=None):
        self.client = client or TrimbleClient()

    def sync_all_projects(self, connection, user=None):
        """Discover projects, then sync models + GUID mappings for each."""
        projects = self.client.discover_projects(connection)
        results = []
        for trimble_project in projects:
            try:
                results.append(self.sync_project(trimble_project))
            except TrimbleError as exc:
                logger.warning('Trimble sync failed for %s: %s', trimble_project, exc)
                results.append({
                    'trimble_project': str(trimble_project.id),
                    'models': 0, 'elements': 0,
                    'error': str(exc),
                })
        return results

    def sync_project(self, trimble_project):
        """
        Sync a single Trimble project: list models, download each model file
        (when the API exposes a download URL), extract IFC GUIDs and upsert
        BIMElementMapping rows.
        """
        models = self.client.discover_models(trimble_project)
        elements_created = 0
        models_synced = 0
        errors = []

        for model in models:
            model_id = str(model.get('id') or model.get('modelId') or '')
            if not model_id:
                continue
            models_synced += 1
            file_meta = model.get('file') if isinstance(model.get('file'), dict) else {}
            file_url = (model.get('downloadUrl') or model.get('fileUrl')
                        or file_meta.get('url') or file_meta.get('downloadUrl'))
            if not file_url:
                errors.append(
                    f'Model {model_id}: no download URL exposed by the API — '
                    'element extraction requires a manual IFC upload.')
                continue
            try:
                response = requests.get(file_url, timeout=REQUEST_TIMEOUT)
                if response.status_code != 200:
                    errors.append(f'Model {model_id}: download failed HTTP {response.status_code}.')
                    continue
                from django.core.files.base import ContentFile
                content = ContentFile(response.content)
                content.name = f'trimble_model_{model_id}.ifc'
                elements = IFCElementExtractor.extract(content)
                for data in elements:
                    if trimble_project.linked_project is None:
                        continue
                    _, created = BIMElementMapping.objects.update_or_create(
                        project=trimble_project.linked_project,
                        bim_guid=data['bim_guid'],
                        defaults={
                            'trimble_project': trimble_project,
                            'element_id': data.get('element_id') or '',
                            'element_name': data.get('element_name') or '',
                            'element_type': data.get('element_type') or '',
                            'level': data.get('level') or '',
                            'coordinates': data.get('coordinates'),
                            'properties': data.get('properties') or {},
                            'source': 'trimble',
                        },
                    )
                    elements_created += 1
            except Exception as exc:  # noqa: BLE001 — per-model isolation
                logger.exception('Model %s sync failed', model_id)
                errors.append(f'Model {model_id}: {exc}')

        from django.utils import timezone
        trimble_project.last_synced_at = timezone.now()
        trimble_project.save(update_fields=['last_synced_at', 'updated_at'])

        return {
            'trimble_project': str(trimble_project.id),
            'models': models_synced,
            'elements': elements_created,
            'errors': errors,
        }
