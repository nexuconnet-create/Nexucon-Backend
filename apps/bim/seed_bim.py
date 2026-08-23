import os
import sys
import django
import datetime
import uuid

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from django.utils import timezone
from apps.projects.models import Project
from apps.bim.models import (
    BIMModel, BIMModelVersion, BIMClash, BIMAnnotation, BIMProgressValidation
)
from apps.monitoring.models import SiteIssue

def seed_bim_database():
    print("==================================================")
    print("  SEEDING BIM MODELS, CLASHES & REVIEW WORKFLOWS  ")
    print("==================================================")

    projects = list(Project.objects.all().order_by('created_at'))
    if not projects:
        print("No projects found in database. Creating default projects...")
        p1 = Project.objects.create(
            name="Eko Atlantic Marina Towers",
            reference_number="PRJ-2026-001",
            lga="Eti-Osa",
            status="Active"
        )
        p2 = Project.objects.create(
            name="Victoria Island Financial Center",
            reference_number="PRJ-2026-002",
            lga="Eti-Osa",
            status="Active"
        )
        p3 = Project.objects.create(
            name="Lekki Free Trade Zone Warehouse Complex",
            reference_number="PRJ-2026-003",
            lga="Ibeju-Lekki",
            status="Active"
        )
        p4 = Project.objects.create(
            name="Ikoyi Imperial Heights",
            reference_number="PRJ-2026-004",
            lga="Ikoyi",
            status="Active"
        )
        projects = [p1, p2, p3, p4]

    print(f"Found {len(projects)} projects. Setting up multi-disciplinary BIM models...")

    models_config = [
        # Project 1: Eko Atlantic
        {
            "project": projects[0],
            "name": f"{projects[0].name} - Architecture Model",
            "discipline": "Architecture",
            "format": "IFC4",
            "file_size": "345 MB",
            "current_version": "v2.1",
            "status": "Approved",
            "lod": "LOD 350",
            "element_count": 18450,
            "is_digitally_certified": True,
            "certified_by_name": "Director General - LASBCA",
            "certified_at": timezone.now() - datetime.timedelta(days=12),
            "hash_signature": "0x8f4e2c9b1a7d3e5f8842bc0182419a",
            "coordinate_system": {"crs": "EPSG:32631", "origin": [6.4281, 3.4219, 12.0]},
            "versions": [
                {
                    "version_label": "v1.0",
                    "commit_hash": "a1b2c3d4",
                    "changes_summary": "Initial architectural massing and floor layouts.",
                    "author_name": "Arc. Folashade Okonjo",
                    "author_role": "BIM Manager",
                    "stats_added": 12500,
                    "stats_modified": 0,
                    "stats_removed": 0,
                    "file_size": "310 MB",
                    "is_current": False
                },
                {
                    "version_label": "v2.1",
                    "commit_hash": "e5f6a7b8",
                    "changes_summary": "Incorporated solar louvers, curtain wall detail, and fire egress routes.",
                    "author_name": "Arc. Folashade Okonjo",
                    "author_role": "Lead Architect",
                    "stats_added": 5950,
                    "stats_modified": 340,
                    "stats_removed": 50,
                    "file_size": "345 MB",
                    "is_current": True
                }
            ]
        },
        {
            "project": projects[0],
            "name": f"{projects[0].name} - Structural Engineering Model",
            "discipline": "Structural",
            "format": "IFC4",
            "file_size": "285 MB",
            "current_version": "v1.8",
            "status": "Approved",
            "lod": "LOD 400",
            "element_count": 24100,
            "is_digitally_certified": True,
            "certified_by_name": "Engr. Babatunde Adeleke",
            "certified_at": timezone.now() - datetime.timedelta(days=15),
            "hash_signature": "0x3c7e9a1b0d2f8e4c7719aa9182341b",
            "coordinate_system": {"crs": "EPSG:32631", "origin": [6.4281, 3.4219, 12.0]},
            "versions": [
                {
                    "version_label": "v1.8",
                    "commit_hash": "c7d8e9f0",
                    "changes_summary": "Full core shear wall rebar reinforcement and foundation pile cap schedules.",
                    "author_name": "Engr. Babatunde Adeleke",
                    "author_role": "Chief Structural Engineer",
                    "stats_added": 24100,
                    "stats_modified": 120,
                    "stats_removed": 15,
                    "file_size": "285 MB",
                    "is_current": True
                }
            ]
        },
        {
            "project": projects[0],
            "name": f"{projects[0].name} - MEP Services Coordination Model",
            "discipline": "MEP",
            "format": "IFC4",
            "file_size": "198 MB",
            "current_version": "v1.2",
            "status": "Under Review",
            "lod": "LOD 300",
            "element_count": 14200,
            "is_digitally_certified": False,
            "coordinate_system": {"crs": "EPSG:32631", "origin": [6.4281, 3.4219, 12.0]},
            "versions": [
                {
                    "version_label": "v1.2",
                    "commit_hash": "b2c3d4e5",
                    "changes_summary": "HVAC riser re-routing through central mechanical core.",
                    "author_name": "Michael Chen",
                    "author_role": "MEP Coordinator",
                    "stats_added": 420,
                    "stats_modified": 85,
                    "stats_removed": 12,
                    "file_size": "198 MB",
                    "is_current": True
                }
            ]
        },

        # Project 2: Victoria Island
        {
            "project": projects[1] if len(projects) > 1 else projects[0],
            "name": f"{(projects[1] if len(projects) > 1 else projects[0]).name} - Architecture & Façade Model",
            "discipline": "Architecture",
            "format": "IFC4",
            "file_size": "412 MB",
            "current_version": "v1.4",
            "status": "Approved",
            "lod": "LOD 350",
            "element_count": 21500,
            "is_digitally_certified": True,
            "certified_by_name": "Arc. Folashade Okonjo",
            "certified_at": timezone.now() - datetime.timedelta(days=8),
            "hash_signature": "0x4a9d7c2e1f0b8e3a5518cc9201948d",
            "coordinate_system": {"crs": "EPSG:32631", "origin": [6.4312, 3.4290, 8.5]},
            "versions": [
                {
                    "version_label": "v1.4",
                    "commit_hash": "d1e2f3a4",
                    "changes_summary": "Double skin curtain wall acoustic and thermal insulation optimization.",
                    "author_name": "David Rossi",
                    "author_role": "Façade Architect",
                    "stats_added": 840,
                    "stats_modified": 120,
                    "stats_removed": 10,
                    "file_size": "412 MB",
                    "is_current": True
                }
            ]
        },
        {
            "project": projects[1] if len(projects) > 1 else projects[0],
            "name": f"{(projects[1] if len(projects) > 1 else projects[0]).name} - Mechanical HVAC & Chillers Model",
            "discipline": "MEP",
            "format": "IFC4",
            "file_size": "265 MB",
            "current_version": "v1.0",
            "status": "Changes Requested",
            "lod": "LOD 300",
            "element_count": 16800,
            "is_digitally_certified": False,
            "coordinate_system": {"crs": "EPSG:32631", "origin": [6.4312, 3.4290, 8.5]},
            "versions": [
                {
                    "version_label": "v1.0",
                    "commit_hash": "f4a5b6c7",
                    "changes_summary": "Initial MEP distribution layout.",
                    "author_name": "Robert Chen",
                    "author_role": "HVAC Consultant",
                    "stats_added": 16800,
                    "stats_modified": 0,
                    "stats_removed": 0,
                    "file_size": "265 MB",
                    "is_current": True
                }
            ]
        },

        # Project 3: Lekki Free Trade Zone
        {
            "project": projects[2] if len(projects) > 2 else projects[0],
            "name": f"{(projects[2] if len(projects) > 2 else projects[0]).name} - Multi-Disciplinary Federated Model",
            "discipline": "Multi-Disciplinary",
            "format": "IFC4",
            "file_size": "580 MB",
            "current_version": "v1.0",
            "status": "Active",
            "lod": "LOD 300",
            "element_count": 38200,
            "is_digitally_certified": False,
            "coordinate_system": {"crs": "EPSG:32631", "origin": [6.4520, 3.8910, 5.0]},
            "versions": [
                {
                    "version_label": "v1.0",
                    "commit_hash": "e7f8a9b0",
                    "changes_summary": "Federated logistics warehouse and automated racking system IFC schema.",
                    "author_name": "Adeola Balogun",
                    "author_role": "BIM Coordinator",
                    "stats_added": 38200,
                    "stats_modified": 0,
                    "stats_removed": 0,
                    "file_size": "580 MB",
                    "is_current": True
                }
            ]
        },

        # Project 4: Ikoyi Imperial Heights
        {
            "project": projects[3] if len(projects) > 3 else projects[0],
            "name": f"{(projects[3] if len(projects) > 3 else projects[0]).name} - Structural & Post-Tensioning Model",
            "discipline": "Structural",
            "format": "IFC4",
            "file_size": "320 MB",
            "current_version": "v2.0",
            "status": "Approved",
            "lod": "LOD 400",
            "element_count": 19800,
            "is_digitally_certified": True,
            "certified_by_name": "Engr. Babatunde Adeleke",
            "certified_at": timezone.now() - datetime.timedelta(days=4),
            "hash_signature": "0x7b2f9e1d0a8c4e6a9921bb0194821a",
            "coordinate_system": {"crs": "EPSG:32631", "origin": [6.4550, 3.4410, 14.0]},
            "versions": [
                {
                    "version_label": "v1.0",
                    "commit_hash": "a9b8c7d6",
                    "changes_summary": "Initial cast concrete frame.",
                    "author_name": "Tariq Mansoor",
                    "author_role": "Structural Drafter",
                    "stats_added": 15000,
                    "stats_modified": 0,
                    "stats_removed": 0,
                    "file_size": "290 MB",
                    "is_current": False
                },
                {
                    "version_label": "v2.0",
                    "commit_hash": "b5c6d7e8",
                    "changes_summary": "Post-tensioned slab tendons and seismic damper integration.",
                    "author_name": "Engr. Babatunde Adeleke",
                    "author_role": "Chief Structural Engineer",
                    "stats_added": 4800,
                    "stats_modified": 210,
                    "stats_removed": 30,
                    "file_size": "320 MB",
                    "is_current": True
                }
            ]
        }
    ]

    saved_models = []
    for cfg in models_config:
        versions_data = cfg.pop("versions", [])
        m_name = cfg["name"]
        model_obj, created = BIMModel.objects.update_or_create(
            name=m_name,
            project=cfg["project"],
            defaults=cfg
        )
        saved_models.append(model_obj)
        action_str = "Created" if created else "Updated"
        print(f"  [{action_str}] Model: {model_obj.name} ({model_obj.discipline} {model_obj.current_version})")

        for v_data in versions_data:
            v_label = v_data["version_label"]
            BIMModelVersion.objects.update_or_create(
                model=model_obj,
                version_label=v_label,
                defaults=v_data
            )

    # --------------------------------------------------------------------------
    # Seed Clashes
    # --------------------------------------------------------------------------
    print("\nSeeding Clash Matrix Detections...")
    clashes_config = [
        {
            "project": saved_models[0].project,
            "primary_model": saved_models[1],  # Structural
            "secondary_model": saved_models[2], # MEP
            "clash_type": "HARD_CLASH",
            "title": "Primary HVAC Duct intersecting Heavy Transfer Beam at 14th Floor Core",
            "description": "Physical clearance violation of -185mm between main HVAC branch and loadbearing beam at Grid 4-B / Level 14.",
            "severity": "CRITICAL",
            "status": "OPEN",
            "assigned_discipline": "MEP",
            "assigned_to_name": "Michael Chen (MEP Coordinator)",
            "coordinates_3d": {"x": 12.4, "y": 8.5, "z": 45.2}
        },
        {
            "project": saved_models[0].project,
            "primary_model": saved_models[1],  # Structural
            "secondary_model": saved_models[2], # MEP
            "clash_type": "HARD_CLASH",
            "title": "Emergency Fire Sprinkler Main intersecting Structural Column C-12",
            "description": "Sprinkler pipe intersects column reinforcement casing at Level 8 without approved sleeve penetration.",
            "severity": "HIGH",
            "status": "IN_REVIEW",
            "assigned_discipline": "MEP",
            "assigned_to_name": "Apex Fire Protection Engineer",
            "coordinates_3d": {"x": 22.1, "y": 14.8, "z": 28.0}
        },
        {
            "project": saved_models[3].project if len(saved_models) > 3 else saved_models[0].project,
            "primary_model": saved_models[3] if len(saved_models) > 3 else saved_models[0],
            "secondary_model": saved_models[4] if len(saved_models) > 4 else None,
            "clash_type": "SOFT_CLASH",
            "title": "Curtain Wall Bracket Anchor Clearance with Post-Tensioning Tendons",
            "description": "Anchor bolt drill path encroaches into 50mm safety buffer of PT live tendon duct at Perimeter Grid F-8.",
            "severity": "CRITICAL",
            "status": "OPEN",
            "assigned_discipline": "Structural",
            "assigned_to_name": "David Rossi (Façade Lead)",
            "coordinates_3d": {"x": 34.8, "y": 19.2, "z": 62.4}
        },
        {
            "project": saved_models[5].project if len(saved_models) > 5 else saved_models[0].project,
            "primary_model": saved_models[5] if len(saved_models) > 5 else saved_models[0],
            "clash_type": "CLEARANCE",
            "title": "Emergency Egress Headroom Clearance under High-Voltage Cable Tray",
            "description": "Finished ceiling clearance is 2.15m (statutory minimum code requirement is 2.40m) at Corridor B-03.",
            "severity": "MEDIUM",
            "status": "RESOLVED",
            "assigned_discipline": "Electrical",
            "assigned_to_name": "Adeola Balogun",
            "coordinates_3d": {"x": 5.4, "y": 11.2, "z": 3.8},
            "resolution_notes": "Tray raised by 300mm following re-route above acoustic baffles."
        }
    ]

    for c_cfg in clashes_config:
        clash_title = c_cfg["title"]
        c_obj, created = BIMClash.objects.update_or_create(
            title=clash_title,
            project=c_cfg["project"],
            defaults=c_cfg
        )
        action_str = "Created" if created else "Updated"
        print(f"  [{action_str}] Clash: {c_obj.title} ({c_obj.severity} • {c_obj.status})")

    # --------------------------------------------------------------------------
    # Seed BCF Annotations
    # --------------------------------------------------------------------------
    print("\nSeeding BCF Review Annotations...")
    annotations_config = [
        {
            "model": saved_models[0],
            "project": saved_models[0].project,
            "author_name": "Arc. Folashade Okonjo",
            "author_role": "Principal Planning Reviewer",
            "text": "Verify that exterior solar louver projection conforms with Lagos State Green Building energy efficiency standards (LSGBC-2024).",
            "status": "Open",
            "priority": "High",
            "element_ids": ["EXT-LVR-04-012", "EXT-LVR-04-013"],
            "viewpoint_camera": {"x": 124.5, "y": 88.2, "z": 45.0, "pitch": -15, "yaw": 45}
        },
        {
            "model": saved_models[1],
            "project": saved_models[1].project,
            "author_name": "Engr. Babatunde Adeleke",
            "author_role": "Chief Structural Integrity Officer",
            "text": "Core shear wall C3 concrete mix specification must be updated to Grade 45 Self-Compacting Concrete (SCC) for congested rebar zones.",
            "status": "In Progress",
            "priority": "Critical",
            "element_ids": ["STR-CORE-WALL-C3"],
            "viewpoint_camera": {"x": 45.0, "y": 32.0, "z": 18.0, "pitch": -30, "yaw": 120}
        },
        {
            "model": saved_models[2],
            "project": saved_models[2].project,
            "author_name": "Michael Chen",
            "author_role": "MEP Coordinator",
            "text": "Provide fire damper sleeves and motorized smoke actuators for all duct penetrations passing through the 2-hour rated fire barrier.",
            "status": "Resolved",
            "priority": "Medium",
            "element_ids": ["MEP-DUCT-FIRE-DAMPER-08"],
            "viewpoint_camera": {"x": 18.5, "y": 24.1, "z": 22.0, "pitch": 0, "yaw": 90}
        }
    ]

    for a_cfg in annotations_config:
        ann_text = a_cfg["text"]
        a_obj, created = BIMAnnotation.objects.update_or_create(
            text=ann_text,
            model=a_cfg["model"],
            defaults=a_cfg
        )
        action_str = "Created" if created else "Updated"
        print(f"  [{action_str}] Annotation: {a_obj.author_name} - {a_obj.text[:45]}...")

    # --------------------------------------------------------------------------
    # Seed 4D Progress Validations
    # --------------------------------------------------------------------------
    print("\nSeeding 4D Progress Validations...")
    progress_config = [
        {
            "project": projects[0],
            "model": saved_models[0],
            "schedule_status": "DELAYED",
            "days_variance": -3,
            "completed_elements_count": 4205,
            "total_elements_count": 9500,
            "earned_value_usd": "$2.4M",
            "planned_vs_actual": [
                {"phase": "Substructure & Deep Piling", "planned": 100, "actual": 100, "status": "Completed"},
                {"phase": "Podium Slab Casting (Levels 1-4)", "planned": 100, "actual": 92, "status": "Delayed"},
                {"phase": "Tower Core Slipform (Levels 5-18)", "planned": 45, "actual": 38, "status": "In Progress"},
                {"phase": "Façade Glazing Installation", "planned": 20, "actual": 12, "status": "Pending"}
            ]
        },
        {
            "project": projects[1] if len(projects) > 1 else projects[0],
            "model": saved_models[3] if len(saved_models) > 3 else saved_models[0],
            "schedule_status": "ON_TRACK",
            "days_variance": 0,
            "completed_elements_count": 6120,
            "total_elements_count": 12000,
            "earned_value_usd": "$3.8M",
            "planned_vs_actual": [
                {"phase": "Foundation & Basement Excavation", "planned": 100, "actual": 100, "status": "Completed"},
                {"phase": "Superstructure Frame", "planned": 65, "actual": 65, "status": "On Track"},
                {"phase": "MEP Rough-Ins", "planned": 40, "actual": 40, "status": "On Track"}
            ]
        }
    ]

    for p_cfg in progress_config:
        p_obj, created = BIMProgressValidation.objects.update_or_create(
            project=p_cfg["project"],
            defaults=p_cfg
        )
        action_str = "Created" if created else "Updated"
        print(f"  [{action_str}] 4D Validation: {p_obj.project.name} ({p_obj.schedule_status} • {p_obj.days_variance} days)")

    print("\n✅ BIM Models and Review Seeding Completed Successfully!")

if __name__ == '__main__':
    seed_bim_database()
