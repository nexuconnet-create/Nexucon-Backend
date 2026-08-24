import os
import sys
import django
import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from django.utils import timezone
from apps.projects.models import Project
from apps.bim.models import BIMModel
from apps.inspections.models import Inspection
from apps.compliance.models import NonConformanceReport
from apps.approvals.models import ApprovalRequest
from apps.documents.models import (
    Document, Version, Approval, DocumentReview, 
    DocumentTemplate, DocumentFolder
)
from apps.documents.services import DocumentService

def seed_documents_database():
    print("=" * 60)
    print("  SEEDING MULTI-PROJECT STATUTORY DOCUMENTS & VAULT  ")
    print("=" * 60)

    projects = list(Project.objects.all())
    if not projects:
        print("No projects found in database. Creating default projects...")
        p1 = Project.objects.create(
            name="Eko Atlantic Marina Towers",
            reference_number="PRJ-2026-001",
            lga="Victoria Island",
            status="Active"
        )
        projects = [p1]

    print(f"Found {len(projects)} projects. Populating project document directories...")

    # Define standard folders
    standard_folders = [
        "01_Architectural",
        "02_Structural",
        "03_MEP_Systems",
        "04_Permits_Legal",
        "05_Geotechnical",
        "06_Site_Inspections"
    ]

    for proj in projects:
        for f_name in standard_folders:
            DocumentFolder.objects.get_or_create(
                name=f_name,
                project=proj,
                defaults={"files_count": 0, "total_size": "0 MB", "is_shared": True}
            )

    # Base R2 bucket URL
    r2_base = "https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument"

    # Multi-project documents configuration
    documents_config = [
        # Eko Atlantic Marina Towers
        {
            "project": projects[0],
            "folder": "01_Architectural",
            "title": f"{projects[0].name} - Architectural Master Plan & Floor Layouts (Ground to L18)",
            "document_type": "SUBMITTED_DRAWING",
            "discipline": "Architecture",
            "status": "APPROVED",
            "file_url": f"{r2_base}/projects/PRJ-2026-001/architectural_master_plan_l18.pdf",
            "file_size": "24.8 MB",
            "file_format": "PDF",
            "pages_count": 48,
            "is_starred": True,
            "is_digitally_stamped": True,
            "stamped_by_name": "Director General - LASBCA",
            "stamped_at": timezone.now() - datetime.timedelta(days=10),
            "stamp_reference": "APP-DOC-2026-8F21",
            "signature_hash": "0x3f8a9e1d0a8c4e6a9921bb0194821a",
            "versions": [
                {"version_label": "v1.0", "author_name": "Arc. Folashade Okonjo", "author_role": "Lead Architect", "changes_summary": "Initial submission for planning permit.", "file_size": "21.5 MB"},
                {"version_label": "v2.1", "author_name": "Arc. Folashade Okonjo", "author_role": "Lead Architect", "changes_summary": "Incorporated LASBCA fire safety recommendations.", "file_size": "24.8 MB"}
            ]
        },
        {
            "project": projects[0],
            "folder": "02_Structural",
            "title": f"{projects[0].name} - Structural Foundation Pile Cap & Raft Slab Engineering Drawings",
            "document_type": "SUBMITTED_DRAWING",
            "discipline": "Structural",
            "status": "APPROVED",
            "file_url": f"{r2_base}/projects/PRJ-2026-001/structural_piling_raft_calc.pdf",
            "file_size": "18.2 MB",
            "file_format": "PDF",
            "pages_count": 32,
            "is_starred": True,
            "is_digitally_stamped": True,
            "stamped_by_name": "Engr. Babatunde Adeleke",
            "stamped_at": timezone.now() - datetime.timedelta(days=14),
            "stamp_reference": "APP-DOC-2026-44B1",
            "signature_hash": "0x7b2f9e1d0a8c4e6a9921bb0194821a",
            "versions": [
                {"version_label": "v1.0", "author_name": "Engr. Babatunde Adeleke", "author_role": "Chief Structural Engineer", "changes_summary": "Full pile load test calculations & rebar layout.", "file_size": "18.2 MB"}
            ]
        },
        {
            "project": projects[0],
            "folder": "05_Geotechnical",
            "title": f"{projects[0].name} - Marine Geotechnical Soil Investigation & CPT Soil Mechanics Report",
            "document_type": "TECHNICAL_REPORT",
            "discipline": "Environmental",
            "status": "APPROVED",
            "file_url": f"{r2_base}/projects/PRJ-2026-001/geotechnical_cpt_report.pdf",
            "file_size": "14.5 MB",
            "file_format": "PDF",
            "pages_count": 64,
            "is_starred": False,
            "is_digitally_stamped": True,
            "stamped_by_name": "Lagos State Materials Testing Agency",
            "stamped_at": timezone.now() - datetime.timedelta(days=20),
            "stamp_reference": "APP-DOC-2026-90C2",
            "signature_hash": "0x4a9d7c2e1f0b8e3a5518cc9201948d",
            "versions": [
                {"version_label": "v1.0", "author_name": "Dr. K. Adeyemi (Geotechnical Lead)", "author_role": "Soils Consultant", "changes_summary": "Deep borehole drilling logs & settlement analysis.", "file_size": "14.5 MB"}
            ]
        },
        {
            "project": projects[0],
            "folder": "04_Permits_Legal",
            "title": f"{projects[0].name} - Lagos State Environmental Impact Assessment (EIA) Certificate",
            "document_type": "COMPLIANCE_DOCUMENT",
            "discipline": "Environmental",
            "status": "APPROVED",
            "file_url": f"{r2_base}/projects/PRJ-2026-001/eia_compliance_cert.pdf",
            "file_size": "4.8 MB",
            "file_format": "PDF",
            "pages_count": 8,
            "is_starred": True,
            "is_digitally_stamped": True,
            "expiry_date": timezone.now().date() + datetime.timedelta(days=280),
            "stamped_by_name": "Federal Ministry of Environment",
            "stamped_at": timezone.now() - datetime.timedelta(days=30),
            "stamp_reference": "APP-DOC-2026-EIA1",
            "signature_hash": "0x5c8e1a2b9d0f4e3c8811ba9201842a",
            "versions": [
                {"version_label": "v1.0", "author_name": "Director of Environmental Assessment", "author_role": "Regulator", "changes_summary": "Approved statutory EIA certificate.", "file_size": "4.8 MB"}
            ]
        },
        {
            "project": projects[0],
            "folder": "06_Site_Inspections",
            "title": f"{projects[0].name} - Podium Rebar & Concrete Pour Quality Assurance Inspection Report",
            "document_type": "INSPECTION_REPORT",
            "discipline": "Structural",
            "status": "APPROVED",
            "file_url": f"{r2_base}/projects/PRJ-2026-001/podium_rebar_insp_report.pdf",
            "file_size": "8.1 MB",
            "file_format": "PDF",
            "pages_count": 16,
            "is_starred": False,
            "is_digitally_stamped": True,
            "stamped_by_name": "Engr. N. Okoro (LASBCA District Inspector)",
            "stamped_at": timezone.now() - datetime.timedelta(days=5),
            "stamp_reference": "APP-DOC-2026-INSP9",
            "signature_hash": "0x9f1a2b3c4d5e6f7a8b9c0d1e2f3a4b",
            "versions": [
                {"version_label": "v1.0", "author_name": "Engr. N. Okoro", "author_role": "LASBCA Inspector", "changes_summary": "Passed on-site verification with cube compressive test results.", "file_size": "8.1 MB"}
            ]
        },

        # Project 2: Victoria Island Financial Center
        {
            "project": projects[1] if len(projects) > 1 else projects[0],
            "folder": "01_Architectural",
            "title": f"{(projects[1] if len(projects) > 1 else projects[0]).name} - Unitized Double-Skin Façade & Glazing Specifications",
            "document_type": "SUBMITTED_DRAWING",
            "discipline": "Architecture",
            "status": "APPROVED",
            "file_url": f"{r2_base}/projects/PRJ-2026-002/facade_glazing_spec_v1.pdf",
            "file_size": "31.2 MB",
            "file_format": "PDF",
            "pages_count": 42,
            "is_starred": True,
            "is_digitally_stamped": True,
            "stamped_by_name": "Arc. Folashade Okonjo",
            "stamped_at": timezone.now() - datetime.timedelta(days=8),
            "stamp_reference": "APP-DOC-2026-VI01",
            "signature_hash": "0x2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f",
            "versions": [
                {"version_label": "v1.0", "author_name": "David Rossi (Façade Lead)", "author_role": "Façade Consultant", "changes_summary": "Solar heat gain and acoustic decibel calculations.", "file_size": "31.2 MB"}
            ]
        },
        {
            "project": projects[1] if len(projects) > 1 else projects[0],
            "folder": "03_MEP_Systems",
            "title": f"{(projects[1] if len(projects) > 1 else projects[0]).name} - Central Chiller Plant & Primary HVAC Risers Engineering Report",
            "document_type": "TECHNICAL_REPORT",
            "discipline": "MEP",
            "status": "PENDING_REVIEW",
            "file_url": f"{r2_base}/projects/PRJ-2026-002/mep_chillers_hvac_calc.pdf",
            "file_size": "16.8 MB",
            "file_format": "PDF",
            "pages_count": 38,
            "is_starred": False,
            "is_digitally_stamped": False,
            "versions": [
                {"version_label": "v1.0", "author_name": "Robert Chen (HVAC Consultant)", "author_role": "MEP Specialist", "changes_summary": "Thermal load calculations and riser routing schedules.", "file_size": "16.8 MB"}
            ]
        },

        # Project 3: Lekki Free Trade Zone Warehouse Complex
        {
            "project": projects[2] if len(projects) > 2 else projects[0],
            "folder": "02_Structural",
            "title": f"{(projects[2] if len(projects) > 2 else projects[0]).name} - High-Bay Automated Racking & Structural Steel Portal Frame Plans",
            "document_type": "SUBMITTED_DRAWING",
            "discipline": "Structural",
            "status": "APPROVED",
            "file_url": f"{r2_base}/projects/PRJ-2026-003/steel_portal_frame_plans.pdf",
            "file_size": "28.5 MB",
            "file_format": "PDF",
            "pages_count": 28,
            "is_starred": True,
            "is_digitally_stamped": True,
            "stamped_by_name": "Engr. Babatunde Adeleke",
            "stamped_at": timezone.now() - datetime.timedelta(days=12),
            "stamp_reference": "APP-DOC-2026-LK01",
            "signature_hash": "0x8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a",
            "versions": [
                {"version_label": "v1.0", "author_name": "Adeola Balogun", "author_role": "Structural Drafter", "changes_summary": "Laser screed concrete slab and portal frame joints.", "file_size": "28.5 MB"}
            ]
        },
        {
            "project": projects[2] if len(projects) > 2 else projects[0],
            "folder": "04_Permits_Legal",
            "title": f"{(projects[2] if len(projects) > 2 else projects[0]).name} - Lagos State Fire Service Fire Safety Certificate",
            "document_type": "COMPLIANCE_DOCUMENT",
            "discipline": "Safety",
            "status": "EXPIRING_SOON",
            "expiry_date": timezone.now().date() + datetime.timedelta(days=18),
            "file_url": f"{r2_base}/projects/PRJ-2026-003/fire_safety_cert.pdf",
            "file_size": "3.5 MB",
            "file_format": "PDF",
            "pages_count": 6,
            "is_starred": False,
            "is_digitally_stamped": True,
            "stamped_by_name": "Lagos State Fire & Rescue Service",
            "stamped_at": timezone.now() - datetime.timedelta(days=340),
            "stamp_reference": "APP-DOC-2026-FIRE9",
            "signature_hash": "0x1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c",
            "versions": [
                {"version_label": "v1.0", "author_name": "Chief Fire Officer", "author_role": "Fire Marshal", "changes_summary": "Annual statutory industrial fire certificate.", "file_size": "3.5 MB"}
            ]
        },

        # Project 4: Ikoyi Imperial Heights
        {
            "project": projects[3] if len(projects) > 3 else projects[0],
            "folder": "02_Structural",
            "title": f"{(projects[3] if len(projects) > 3 else projects[0]).name} - Post-Tensioned Slabs & Seismic Damper Structural Calculations",
            "document_type": "TECHNICAL_REPORT",
            "discipline": "Structural",
            "status": "APPROVED",
            "file_url": f"{r2_base}/projects/PRJ-2026-004/pt_seismic_damper_calc.pdf",
            "file_size": "19.4 MB",
            "file_format": "PDF",
            "pages_count": 52,
            "is_starred": True,
            "is_digitally_stamped": True,
            "stamped_by_name": "Engr. Babatunde Adeleke",
            "stamped_at": timezone.now() - datetime.timedelta(days=6),
            "stamp_reference": "APP-DOC-2026-IK01",
            "signature_hash": "0x3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e",
            "versions": [
                {"version_label": "v1.0", "author_name": "Tariq Mansoor", "author_role": "PT Engineer", "changes_summary": "Tendon elongation verification and anchor heads detail.", "file_size": "19.4 MB"}
            ]
        },
        {
            "project": projects[3] if len(projects) > 3 else projects[0],
            "folder": "06_Site_Inspections",
            "title": f"{(projects[3] if len(projects) > 3 else projects[0]).name} - 12th Floor Post-Tensioning Tendon Stressing Inspection Certificate",
            "document_type": "INSPECTION_REPORT",
            "discipline": "Structural",
            "status": "APPROVED",
            "file_url": f"{r2_base}/projects/PRJ-2026-004/tendon_stressing_insp.pdf",
            "file_size": "6.2 MB",
            "file_format": "PDF",
            "pages_count": 12,
            "is_starred": False,
            "is_digitally_stamped": True,
            "stamped_by_name": "LASBCA Special Structural Unit",
            "stamped_at": timezone.now() - datetime.timedelta(days=2),
            "stamp_reference": "APP-DOC-2026-IK99",
            "signature_hash": "0x7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c",
            "versions": [
                {"version_label": "v1.0", "author_name": "Lead Structural Inspector", "author_role": "LASBCA Inspector", "changes_summary": "Hydraulic jack calibration and tendon gauge logs.", "file_size": "6.2 MB"}
            ]
        }
    ]

    # Seed any additional projects with default baseline drawings & compliance records
    for p_idx, proj in enumerate(projects[4:], start=5):
        documents_config.append({
            "project": proj,
            "folder": "01_Architectural",
            "title": f"{proj.name} - Architectural Master Concept & Floor Plans",
            "document_type": "SUBMITTED_DRAWING",
            "discipline": "Architecture",
            "status": "APPROVED",
            "file_url": f"{r2_base}/projects/{proj.reference_number or f'PRJ-{p_idx}'}/architectural_master.pdf",
            "file_size": "22.1 MB",
            "file_format": "PDF",
            "pages_count": 36,
            "is_starred": False,
            "is_digitally_stamped": True,
            "stamped_by_name": "Director General - LASBCA",
            "stamped_at": timezone.now() - datetime.timedelta(days=15),
            "stamp_reference": f"APP-DOC-2026-{p_idx:02d}01",
            "signature_hash": f"0x{p_idx:02d}e9f0a1b2c3d4e5f6a7b8c9d0e1f2a",
            "versions": [
                {"version_label": "v1.0", "author_name": "Arc. Folashade Okonjo", "author_role": "Lead Architect", "changes_summary": "Initial statutory concept submission.", "file_size": "22.1 MB"}
            ]
        })
        documents_config.append({
            "project": proj,
            "folder": "04_Permits_Legal",
            "title": f"{proj.name} - Lagos State Planning Development Permit",
            "document_type": "COMPLIANCE_DOCUMENT",
            "discipline": "Planning",
            "status": "APPROVED",
            "file_url": f"{r2_base}/projects/{proj.reference_number or f'PRJ-{p_idx}'}/development_permit.pdf",
            "file_size": "5.4 MB",
            "file_format": "PDF",
            "pages_count": 10,
            "is_starred": True,
            "is_digitally_stamped": True,
            "expiry_date": timezone.now().date() + datetime.timedelta(days=365),
            "stamped_by_name": "Lagos State Physical Planning Board",
            "stamped_at": timezone.now() - datetime.timedelta(days=25),
            "stamp_reference": f"APP-DOC-2026-{p_idx:02d}99",
            "signature_hash": f"0x{p_idx:02d}a1b2c3d4e5f6a7b8c9d0e1f2a3b4c",
            "versions": [
                {"version_label": "v1.0", "author_name": "Chief Physical Planner", "author_role": "Planning Board", "changes_summary": "Approved statutory development permit.", "file_size": "5.4 MB"}
            ]
        })

    for cfg in documents_config:
        versions_data = cfg.pop("versions", [])
        d_title = cfg["title"]
        d_proj = cfg["project"]
        
        doc_obj, created = Document.objects.update_or_create(
            title=d_title,
            project=d_proj,
            defaults=cfg
        )
        action_str = "Created" if created else "Updated"
        print(f"  [{action_str}] Document: {doc_obj.title[:55]}... ({doc_obj.discipline} • {doc_obj.document_type})")

        for v_idx, v_cfg in enumerate(versions_data, start=1):
            v_label = v_cfg["version_label"]
            v_obj, v_created = Version.objects.update_or_create(
                document=doc_obj,
                version_label=v_label,
                defaults={
                    "version_number": v_idx,
                    "changes_summary": v_cfg.get("changes_summary", "Revision update."),
                    "author_name": v_cfg.get("author_name", "Review Team"),
                    "author_role": v_cfg.get("author_role", "Reviewer"),
                    "file_url": doc_obj.file_url,
                    "file_size": v_cfg.get("file_size", doc_obj.file_size),
                    "status": "Current" if v_idx == len(versions_data) else "Superseded",
                    "signature_hash": doc_obj.signature_hash if doc_obj.is_digitally_stamped else None
                }
            )

        # If digitally stamped, ensure approval record exists in vault
        if doc_obj.is_digitally_stamped:
            current_ver = doc_obj.versions.filter(status='Current').first()
            Approval.objects.update_or_create(
                approval_reference=doc_obj.stamp_reference,
                defaults={
                    "document": doc_obj,
                    "version": current_ver,
                    "category": doc_obj.folder.replace('_', ' ').title(),
                    "approved_by_name": doc_obj.stamped_by_name or 'Government Planning Authority',
                    "status": "APPROVED",
                    "comments": f"Officially validated and digitally stamped for {doc_obj.project.name}.",
                    "signature_hash": doc_obj.signature_hash,
                    "reviewed_at": doc_obj.stamped_at or timezone.now()
                }
            )

    # Recalculate folder file counts & sizes
    for f in DocumentFolder.objects.all():
        docs_in_folder = Document.objects.filter(folder=f.name, project=f.project)
        f.files_count = docs_in_folder.count()
        f.save()

    # Seed Standard Templates
    print("\nSeeding Regulatory Document Templates...")
    templates_config = [
        {"title": "LASBCA Stage 1 Foundation Verification Inspection Checklist", "category": "INSPECTION", "description": "Mandatory geotechnical and rebar verification checklist for foundation sign-off.", "file_size": "420 KB"},
        {"title": "Lagos State Official Stop-Work Order & Remediation Notice", "category": "ENFORCEMENT", "description": "Statutory notice template for critical non-conformance and safety halt orders.", "file_size": "380 KB"},
        {"title": "Standard Commercial Development Planning Permit Application Form", "category": "PERMIT", "description": "Statutory application template for high-rise and commercial schemes.", "file_size": "650 KB"},
        {"title": "Environmental & Geotechnical Soil Suitability Certification Template", "category": "COMPLIANCE", "description": "Agency environmental compliance and ground stability clearance template.", "file_size": "510 KB"},
    ]

    for t_cfg in templates_config:
        t_obj, t_created = DocumentTemplate.objects.update_or_create(
            title=t_cfg["title"],
            defaults={
                "category": t_cfg["category"],
                "description": t_cfg["description"],
                "file_format": "PDF",
                "file_url": f"{r2_base}/templates/{t_cfg['category'].lower()}_template.pdf",
                "file_size": t_cfg["file_size"],
                "usage_count": 14
            }
        )
        t_action = "Created" if t_created else "Updated"
        print(f"  [{t_action}] Template: {t_obj.title[:50]}... ({t_obj.category})")

    print("\n✅ Document Repository & Digital Vault Database Seeding Completed Successfully!")

if __name__ == '__main__':
    seed_documents_database()
