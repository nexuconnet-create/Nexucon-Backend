import os
import django
import datetime
from decimal import Decimal
from django.utils import timezone

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from django.contrib.auth import get_user_model
from apps.projects.models import Project, ProjectProfessional, ProjectMilestone
from apps.applications.models import Application
from apps.inspections.models import Inspection, Checklist, Finding, StopWorkOrder
from apps.monitoring.models import DailySiteUpdate, FieldObservation, SiteIssue, ConstructionMilestone, SiteVerification
from apps.bim.models import BIMModel, BIMModelVersion, BIMClash, BIMAnnotation, BIMProgressValidation
from apps.documents.models import Document, Version, Approval, DocumentTemplate, DocumentFolder
from apps.compliance.models import NonConformanceReport, CorrectiveActionPlan, RegulatoryRequirement, ComplianceReview, ComplianceCertificate
from apps.approvals.models import ApprovalRequest, ApprovalDecision, TechnicalReviewCriteria
from apps.analytics.models import GeneratedReport, DepartmentPerformanceMetric, OfficerPerformanceRecord, RiskAssessmentAlert
from apps.notifications.models import Notification, NotificationPreference
from apps.audit.models import AuditEvent
from apps.stakeholders.models import (
    Developer, Contractor, Consultant, Inspector, 
    BlacklistRecord, StakeholderMeeting, StakeholderMessage
)
from apps.settings.models import (
    UserInvitation, CustomRole, RolePermission, ApprovalWorkflow, WorkflowStep, InspectionTemplate, 
    ComplianceStandard, StatutoryDocument, WebhookSubscription, 
    TersusDevice, BIMIntegration, DocumentSystemIntegration, 
    GovernmentAPIIntegration, APIKeyCredential, IntegrationLog
)

User = get_user_model()
admin_user = User.objects.filter(is_superuser=True).first() or User.objects.first()

print("==========================================================")
print("STARTING COMPREHENSIVE SEED FOR ALL 8 GOVERNMENT MODULES")
print("==========================================================")

# ==============================================================================
# 1. SEED PROJECTS
# ==============================================================================
print("1. Seeding Projects...")
projects_data = [
    {
        "name": "Eko Atlantic Marina Towers",
        "reference_number": "PRJ-2026-001",
        "project_type": "Commercial",
        "description": "32-Storey ultra-luxury waterfront commercial and mixed-use development comprising commercial suites, helipad, and marine seawall protection.",
        "status": "ACTIVE",
        "development_category": "Commercial High-Rise",
        "estimated_project_value": Decimal("45800000000.00"),
        "number_of_floors": 32,
        "developer_name": "Eko Atlantic Master Developers Ltd",
        "developer_organization": "South Energyx Nigeria Ltd",
        "developer_email": "planning@ekoatlantic.com",
        "developer_phone": "+234 1 270 0000",
        "site_address": "Plot 4A, Marina District, Eko Atlantic City",
        "state": "Lagos",
        "lga": "Victoria Island",
        "ward_area": "Eko Waterfront",
        "plot_number": "Plot 4A-12",
        "block_number": "Block 7",
        "land_title_reference": "CofO / 2024 / 8892 / LA",
        "permit_number": "LASBCA/DEV/2026/0491",
        "permit_status": "APPROVED",
        "planning_approval_reference": "MPP&UD/PLN/2025/1129",
        "building_control_reference": "LASBCA/VI/BCR/2026/004",
        "start_date": datetime.date(2025, 4, 1),
        "estimated_completion": datetime.date(2028, 12, 31)
    },
    {
        "name": "Victoria Island Financial Center",
        "reference_number": "PRJ-2026-002",
        "project_type": "Commercial",
        "description": "Twin 24-floor corporate office towers with triple-level subterranean parking, seismic-damped core, and LEED Platinum specifications.",
        "status": "ACTIVE",
        "development_category": "Corporate Banking Hub",
        "estimated_project_value": Decimal("32000000000.00"),
        "number_of_floors": 24,
        "developer_name": "Rovengates Properties Ltd",
        "developer_organization": "Rovengates Holdings Nigeria",
        "developer_email": "projects@rovengates.ng",
        "developer_phone": "+234 803 111 2233",
        "site_address": "Plot 1021, Adeola Odeku Street, Victoria Island",
        "state": "Lagos",
        "lga": "Eti-Osa",
        "ward_area": "Victoria Island East",
        "plot_number": "Plot 1021",
        "block_number": "Block A4",
        "land_title_reference": "FED/LUO/TIT/99823",
        "permit_number": "LASBCA/DEV/2025/9921",
        "permit_status": "APPROVED",
        "planning_approval_reference": "MPP&UD/PLN/2025/0842",
        "building_control_reference": "LASBCA/ETI/BCR/2025/891",
        "start_date": datetime.date(2025, 1, 15),
        "estimated_completion": datetime.date(2027, 8, 30)
    },
    {
        "name": "Lekki Free Trade Zone Warehouse Complex",
        "reference_number": "PRJ-2026-003",
        "project_type": "Industrial",
        "description": "Logistics hub featuring automated bonded warehousing, cold-chain storage facilities, and heavy-duty reinforced slab foundation.",
        "status": "ACTIVE",
        "development_category": "Industrial Logistics",
        "estimated_project_value": Decimal("18500000000.00"),
        "number_of_floors": 3,
        "developer_name": "Lekki Concession Development Co",
        "developer_organization": "Lekki Worldwide Investments",
        "developer_email": "infra@lekkiftz.com",
        "developer_phone": "+234 1 454 8800",
        "site_address": "Zone 3B, Lekki Free Trade Zone Expressway, Ibeju-Lekki",
        "state": "Lagos",
        "lga": "Ibeju-Lekki",
        "ward_area": "Ibeju East",
        "plot_number": "Plot LFZ-108",
        "block_number": "Sector C",
        "land_title_reference": "LFZ/GAZETTE/2020/001",
        "permit_number": "LASBCA/DEV/2026/0118",
        "permit_status": "APPROVED",
        "planning_approval_reference": "MPP&UD/PLN/2026/0112",
        "building_control_reference": "LASBCA/IBJ/BCR/2026/054",
        "start_date": datetime.date(2025, 6, 1),
        "estimated_completion": datetime.date(2027, 3, 31)
    },
    {
        "name": "Ikoyi Imperial Heights Luxury Condominiums",
        "reference_number": "PRJ-2026-004",
        "project_type": "Residential",
        "description": "18-Storey premium residential condominiums with cantilevered infinity sky-pools, advanced acoustic dampening, and automated building management.",
        "status": "ACTIVE",
        "development_category": "Luxury Residential",
        "estimated_project_value": Decimal("24000000000.00"),
        "number_of_floors": 18,
        "developer_name": "Rovengates Properties Ltd",
        "developer_organization": "Rovengates Holdings Nigeria",
        "developer_email": "developments@rovengates.ng",
        "developer_phone": "+234 803 111 2233",
        "site_address": "8 Bourdillon Road, Ikoyi",
        "state": "Lagos",
        "lga": "Eti-Osa",
        "ward_area": "Ikoyi West",
        "plot_number": "Plot 8",
        "block_number": "Block B2",
        "land_title_reference": "CofO / 2023 / 4410 / LA",
        "permit_number": "LASBCA/DEV/2026/0312",
        "permit_status": "APPROVED",
        "planning_approval_reference": "MPP&UD/PLN/2025/1402",
        "building_control_reference": "LASBCA/IKY/BCR/2026/022",
        "start_date": datetime.date(2025, 8, 10),
        "estimated_completion": datetime.date(2028, 4, 30)
    }
]

created_projects = []
for p_data in projects_data:
    ref = p_data.pop("reference_number")
    proj, _ = Project.objects.update_or_create(reference_number=ref, defaults=p_data)
    created_projects.append(proj)

proj1 = created_projects[0] # Eko Atlantic Marina
proj2 = created_projects[1] # VI Financial Center
proj3 = created_projects[2] # Lekki FTZ
proj4 = created_projects[3] # Ikoyi Imperial

# ==============================================================================
# 2. SEED DOCUMENTS, FOLDERS & TEMPLATES
# ==============================================================================
print("2. Seeding Documents, Approvals Vault, and Templates...")
folders_to_seed = [
    ("01_Architectural", 42, "1.2 GB", True),
    ("02_Structural", 18, "850 MB", True),
    ("03_MEP_Systems", 35, "2.1 GB", False),
    ("04_Geotechnical_EIA", 12, "420 MB", True),
    ("05_Statutory_Permits", 8, "45 MB", True),
    ("06_Site_Photographs", 128, "4.5 GB", True),
    ("07_Contracts_Legal", 6, "18 MB", False),
]

for name, count, size, shared in folders_to_seed:
    DocumentFolder.objects.get_or_create(
        name=name,
        defaults={"files_count": count, "total_size": size, "is_shared": shared, "project": proj1}
    )

docs_data = [
    {
        "project": proj1,
        "folder": "01_Architectural",
        "title": "AR-DWG-001: General Arrangement & Floor Plans (Ground to 32nd Floor)",
        "document_type": "DRAWING",
        "discipline": "Architecture",
        "status": "APPROVED",
        "current_version": "v2.1",
        "file_size": "24.8 MB",
        "file_format": "PDF",
        "pages_count": 36,
        "is_starred": True,
        "is_shared": True,
        "uploader_name": "Arc. Folashade Okonjo (Lead Architect)",
        "is_digitally_stamped": True,
        "stamped_by_name": "Director General - LASBCA",
        "stamped_at": timezone.now() - datetime.timedelta(days=15),
        "stamp_reference": "LASBCA-SEAL-2026-0091",
        "signature_hash": "0x8f4e2c9b1a7d3e5f8842bc0182419a"
    },
    {
        "project": proj1,
        "folder": "02_Structural",
        "title": "ST-DWG-102: Foundation Piling & Raft Slab Reinforcement Schedule",
        "document_type": "DRAWING",
        "discipline": "Structural",
        "status": "APPROVED",
        "current_version": "v1.4",
        "file_size": "18.2 MB",
        "file_format": "PDF",
        "pages_count": 24,
        "is_starred": True,
        "is_shared": True,
        "uploader_name": "Engr. Babatunde Adeleke (Principal Structural Engineer)",
        "is_digitally_stamped": True,
        "stamped_by_name": "Director of Structural Integrity Unit",
        "stamped_at": timezone.now() - datetime.timedelta(days=20),
        "stamp_reference": "LASBCA-SEAL-2026-0044",
        "signature_hash": "0x3c7e9a1b0d2f8e4c7719aa9182341b"
    },
    {
        "project": proj1,
        "folder": "03_MEP_Systems",
        "title": "MEP-DWG-204: Central HVAC & Automated Sprinkler Fire Suppression Layout",
        "document_type": "DRAWING",
        "discipline": "MEP",
        "status": "APPROVED",
        "current_version": "v1.0",
        "file_size": "15.6 MB",
        "file_format": "PDF",
        "pages_count": 18,
        "is_starred": False,
        "is_shared": True,
        "uploader_name": "Engr. Chukwuma Obi (MEP Consultant)",
        "is_digitally_stamped": True,
        "stamped_by_name": "State Chief Fire & Safety Inspector",
        "stamped_at": timezone.now() - datetime.timedelta(days=10),
        "stamp_reference": "LASBCA-FIRE-2026-081",
        "signature_hash": "0x9d4b1a7e2c8f0e3a9921bc8819241c"
    },
    {
        "project": proj2,
        "folder": "04_Geotechnical_EIA",
        "title": "GEO-REP-012: Deep Borehole Geotechnical Investigation & Soil Bearing Mechanics",
        "document_type": "REPORT",
        "discipline": "Environmental",
        "status": "APPROVED",
        "current_version": "v1.0",
        "file_size": "34.1 MB",
        "file_format": "PDF",
        "pages_count": 84,
        "is_starred": True,
        "is_shared": True,
        "uploader_name": "EnviroTech Geosolutions Ltd",
        "is_digitally_stamped": True,
        "stamped_by_name": "Ministry of Physical Planning Geodetic Board",
        "stamped_at": timezone.now() - datetime.timedelta(days=35),
        "stamp_reference": "MPP-GEO-2026-0012",
        "signature_hash": "0x1a8f9c2d7e0b4a3f119988220033aa"
    },
    {
        "project": proj2,
        "folder": "05_Statutory_Permits",
        "title": "COMP-CERT-884: LASBCA Stage 2 Substructure Structural Conformance Certificate",
        "document_type": "COMPLIANCE_CERTIFICATE",
        "discipline": "General",
        "status": "APPROVED",
        "current_version": "v1.0",
        "file_size": "4.2 MB",
        "file_format": "PDF",
        "pages_count": 4,
        "is_starred": True,
        "is_shared": True,
        "uploader_name": "LASBCA Directorate of Inspections",
        "is_digitally_stamped": True,
        "stamped_by_name": "Director General - LASBCA",
        "stamped_at": timezone.now() - datetime.timedelta(days=5),
        "stamp_reference": "LASBCA-CERT-2026-0884",
        "signature_hash": "0x5e2b8a1c9f0d7e4a441199338822bb"
    },
    {
        "project": proj3,
        "folder": "01_Architectural",
        "title": "AR-DWG-108: Bonded Warehouse Bay Elevation & Heavy Loading Dock Sections",
        "document_type": "DRAWING",
        "discipline": "Architecture",
        "status": "UNDER_REVIEW",
        "current_version": "v1.2",
        "file_size": "14.5 MB",
        "file_format": "PDF",
        "pages_count": 14,
        "is_starred": False,
        "is_shared": True,
        "uploader_name": "Julius Berger Nigeria Plc",
        "is_digitally_stamped": False,
    }
]

for doc_item in docs_data:
    title = doc_item["title"]
    doc_obj, _ = Document.objects.update_or_create(title=title, defaults=doc_item)
    Version.objects.get_or_create(
        document=doc_obj,
        version_number=1,
        defaults={
            "version_label": "v1.0",
            "changes_summary": "Initial statutory engineering submission.",
            "author_name": doc_obj.uploader_name,
            "author_role": "Lead Consultant",
            "file_size": doc_obj.file_size
        }
    )
    if doc_obj.is_digitally_stamped:
        Approval.objects.get_or_create(
            document=doc_obj,
            approval_reference=f"APP-{doc_obj.stamp_reference}",
            defaults={
                "category": "Statutory Clearance",
                "approved_by_name": doc_obj.stamped_by_name,
                "status": "APPROVED",
                "comments": "Reviewed, verified, and sealed in full accordance with the Lagos State Building Code 2020.",
                "signature_hash": doc_obj.signature_hash,
                "reviewed_at": doc_obj.stamped_at or timezone.now()
            }
        )

# Seed Document Templates
templates = [
    ("LASBCA Form 1A - Building Plan Submission Schedule", "PERMIT", "Official application form for development authorization and structural review.", "PDF", "1.2 MB"),
    ("LASBCA Form 4B - Mandatory Stage Inspection Request", "INSPECTION", "Statutory notice required 48 hours prior to any major structural concrete casting.", "PDF", "850 KB"),
    ("LASBCA Form 7 - Certificate of Structural Stability & Habitation", "COMPLIANCE", "Final certification issued prior to occupancy upon passing structural audits.", "PDF", "2.1 MB"),
    ("Statutory Stop-Work Notice Citation & Infraction Order", "ENFORCEMENT", "Official notice issued for non-conforming, unpermitted, or hazardous building works.", "PDF", "1.5 MB")
]

for title, cat, desc, fmt, sz in templates:
    DocumentTemplate.objects.update_or_create(
        title=title,
        defaults={
            "category": cat,
            "description": desc,
            "file_format": fmt,
            "file_size": sz,
            "file_url": "https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument/sample.pdf",
            "usage_count": 142
        }
    )

# ==============================================================================
# 3. SEED COMPLIANCE (NCRS, CAPAS, REQUIREMENTS, REVIEWS, CERTS)
# ==============================================================================
print("3. Seeding Compliance (NCRs, CAPAs, Statutory Requirements)...")
ncrs_data = [
    {
        "project": proj1,
        "title": "Substandard C25/30 Concrete Batching at Core Wall Grid 4-B",
        "description": "Compression cube strength test at 28 days yielded 21.4 N/mm² against specified design minimum of 30 N/mm² on the 14th floor shear core.",
        "severity": "Critical",
        "category": "Structural",
        "status": "Open",
        "reported_by_name": "Engr. Babatunde Adeleke (Senior Structural Inspector)",
        "assignee_name": "Julius Berger Nigeria Site Team",
        "source": "INSPECTION",
        "escalation_level": 4,
        "date_logged": timezone.now() - datetime.timedelta(days=23)
    },
    {
        "project": proj1,
        "title": "Inadequate Perimeter Safety Netting & Edge Fall Protection on 18th Floor",
        "description": "Exposed slab perimeter lacking required 1.2m double guardrails and debris containment netting during active steel fixing.",
        "severity": "Major",
        "category": "Safety",
        "status": "Open",
        "reported_by_name": "O. Williams (HSE Auditor)",
        "assignee_name": "Apex Construction Safety Lead",
        "source": "SITE_MONITORING",
        "escalation_level": 2,
        "date_logged": timezone.now() - datetime.timedelta(days=9)
    },
    {
        "project": proj2,
        "title": "Discharge of Untreated Construction Effluent into Municipal Storm Drainage",
        "description": "Turbid borehole dewatering discharge with high suspended solids exceeding NESREA statutory discharge thresholds without sedimentation tank.",
        "severity": "Major",
        "category": "Environmental",
        "status": "In Progress",
        "reported_by_name": "Mrs. Amina Danjuma (Environmental Compliance Officer)",
        "assignee_name": "Cappa & D'Alberto Site Engineer",
        "source": "SITE_MONITORING",
        "escalation_level": 3,
        "date_logged": timezone.now() - datetime.timedelta(days=16)
    },
    {
        "project": proj3,
        "title": "High-Yield Rebar Steel Bundles Stored Directly on Bare Soil Subject to Saline Corrosion",
        "description": "Bundles of Y25 and Y20 high-yield tensile bars stored without timber dunnage or tarpaulin cover in coastal saline environment.",
        "severity": "Minor",
        "category": "Quality",
        "status": "Open",
        "reported_by_name": "Engr. Chukwuma Obi (Materials Engineer)",
        "assignee_name": "Lekki FTZ Logistics Engineer",
        "source": "INSPECTION",
        "escalation_level": 1,
        "date_logged": timezone.now() - datetime.timedelta(days=4)
    },
    {
        "project": proj4,
        "title": "Failure to Maintain On-Site Certified First Aid Station and HSE Incident Logs",
        "description": "First aid response station unstaffed during night shift pour; lack of registered medical personnel as mandated by Nigerian Safety at Work Act.",
        "severity": "Minor",
        "category": "Safety",
        "status": "Closed",
        "reported_by_name": "Marcus Chen (Inspector)",
        "assignee_name": "Rovengates HSE Officer",
        "source": "MANUAL",
        "escalation_level": 1,
        "date_logged": timezone.now() - datetime.timedelta(days=30),
        "resolved_at": timezone.now() - datetime.timedelta(days=25),
        "resolution_notes": "Contractor established 24/7 paramedic on-site rotation and verified emergency transfer protocol with Federal Medical Centre."
    }
]

created_ncrs = []
for ncr_item in ncrs_data:
    title = ncr_item["title"]
    ncr_obj, _ = NonConformanceReport.objects.update_or_create(title=title, defaults=ncr_item)
    created_ncrs.append(ncr_obj)

# Seed CAPAs
capas_data = [
    {
        "project": proj1,
        "ncr": created_ncrs[0],
        "title": "Independent Core Extraction & Non-Destructive Ultrasonic Pulse Velocity (UPV) Retest",
        "action_plan": "Extract 6 core samples across grid 4-B under LASBCA supervision and perform carbon-fiber reinforcement wrap if core yields under 28 N/mm².",
        "verification_notes": "Root Cause: Batching plant automated water-cement ratio sensor calibration drifted during heavy downpour.",
        "assignee_name": "Engr. Babatunde Adeleke & Julius Berger QC",
        "status": "in-progress",
        "priority": "Critical",
        "due_date": datetime.date(2026, 9, 15),
        "comments_count": 4,
        "attachments_count": 2
    },
    {
        "project": proj1,
        "ncr": created_ncrs[1],
        "title": "Install Heavy-Duty Double-Layer Perimeter Netting & Self-Retracting Lifelines",
        "action_plan": "Procure and erect certified EN 1263-1 safety nets around full perimeter of 18th to 24th floors before concrete placing resumes.",
        "verification_notes": "Root cause: Barrier removed for crane movement without HSE clearance.",
        "assignee_name": "Apex Construction Safety Lead",
        "status": "review",
        "priority": "High",
        "due_date": datetime.date(2026, 9, 2),
        "comments_count": 2,
        "attachments_count": 1
    },
    {
        "project": proj2,
        "ncr": created_ncrs[2],
        "title": "Construct Three-Stage Concrete Silt Sedimentation & Neutralization Chamber",
        "action_plan": "Build temporary 3-chamber settling basin to clarify groundwater dewatering effluent prior to municipal drainage release.",
        "verification_notes": "Continuous pH and turbidity sampling equipment deployed.",
        "assignee_name": "Cappa & D'Alberto Site Engineer",
        "status": "todo",
        "priority": "Medium",
        "due_date": datetime.date(2026, 9, 20),
        "comments_count": 1,
        "attachments_count": 0
    }
]

for capa_item in capas_data:
    title = capa_item["title"]
    CorrectiveActionPlan.objects.update_or_create(title=title, defaults=capa_item)

# Seed Regulatory Requirements
requirements_data = [
    {
        "requirement_reference": "NBC-2020-SEC-4",
        "title": "National Building Code 2020: Minimum Structural Fire Resistance (2-Hour Rating for High-Rise)",
        "authority": "Federal Ministry of Housing & Urban Development",
        "category": "Building Codes",
        "status": "Compliant",
        "description": "Laboratory Fire Endurance Test & Intumescent Coating Certification on structural steel and columns.",
        "last_checked": datetime.date(2026, 8, 1)
    },
    {
        "requirement_reference": "LASBCA-REG-12",
        "title": "Lagos State Urban and Regional Planning Law: Mandatory 7-Stage Building Inspections",
        "authority": "Lagos State Building Control Agency (LASBCA)",
        "category": "Legal & Planning",
        "status": "Compliant",
        "description": "Physical On-Site Stage Inspection & Digital Stamp Authorization before concrete casting.",
        "last_checked": datetime.date(2026, 8, 10)
    },
    {
        "requirement_reference": "NESREA-ACT-07",
        "title": "National Environmental Standards (NESREA) Construction Site Runoff & Effluent Standards",
        "authority": "Federal Ministry of Environment / NESREA",
        "category": "Environmental",
        "status": "At Risk",
        "description": "Monthly Water Quality Laboratory Testing & Effluent Discharge Log to storm drain.",
        "last_checked": datetime.date(2026, 8, 15)
    },
    {
        "requirement_reference": "COREN-STD-02",
        "title": "COREN Regulations for Resident Registered Civil Engineers on Commercial Developments",
        "authority": "Council for the Regulation of Engineering in Nigeria (COREN)",
        "category": "Safety & Health",
        "status": "Compliant",
        "description": "Valid COREN Practicing License & Resident Engineer Site Register sign-offs.",
        "last_checked": datetime.date(2026, 7, 25)
    }
]

for req in requirements_data:
    ref = req["requirement_reference"]
    RegulatoryRequirement.objects.update_or_create(requirement_reference=ref, defaults=req)

# Seed Compliance Certificates
certs_data = [
    {
        "project": proj1,
        "category": "Building Code",
        "title": "Interim Stage 2 Substructure Conformance & Foundation Integrity Certificate",
        "authority": "Lagos State Building Control Agency (LASBCA)",
        "status": "Active",
        "issue_date": datetime.date(2026, 1, 15),
        "expiry_date": datetime.date(2027, 1, 15),
        "qr_verification_hash": "0x8f4e2c9b1a7d3e5f8842bc0182419a",
        "certificate_file_url": "https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument/sample.pdf"
    },
    {
        "project": proj2,
        "category": "Building Code",
        "title": "Certificate of Structural Conformance (Columns and Core 1st-12th Floor)",
        "authority": "Lagos State Building Control Agency (LASBCA)",
        "status": "Active",
        "issue_date": datetime.date(2026, 2, 20),
        "expiry_date": datetime.date(2027, 2, 20),
        "qr_verification_hash": "0x3c7e9a1b0d2f8e4c7719aa9182341b",
        "certificate_file_url": "https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument/sample.pdf"
    }
]

for cert in certs_data:
    title = cert["title"]
    ComplianceCertificate.objects.update_or_create(title=title, defaults=cert)

# ==============================================================================
# 4. SEED APPROVALS & DECISIONS
# ==============================================================================
print("4. Seeding Approval Requests, Criteria Matrices, and Decisions...")
approvals_data = [
    {
        "project": proj1,
        "title": "Phase 2 Deep Foundation Piling & 3.5m Raft Concrete Pour Clearance",
        "request_type": "Escalated",
        "discipline": "Structural",
        "priority": "Critical",
        "status": "Pending",
        "value_amount": Decimal("180000000.00"),
        "doa_level_required": "Permanent Secretary / Director General",
        "submitted_by_name": "Julius Berger Nigeria Plc",
        "due_date": datetime.date(2026, 9, 10),
        "description": "High-value structural clearance for 12,000 m³ mass concrete pour on Marina Towers raft foundation. Requires ministerial approval under Delegated Authority rules (>₦50M).",
        "days_overdue": 0,
        "signatories_required": 3,
        "signatories_completed": 2
    },
    {
        "project": proj1,
        "title": "Deep Basement Dewatering & Environmental Coastal Drainage Clearance",
        "request_type": "Technical",
        "discipline": "Environmental",
        "priority": "High",
        "status": "In Review",
        "value_amount": Decimal("14500000.00"),
        "doa_level_required": "Director",
        "submitted_by_name": "EnviroTech Geosolutions",
        "due_date": datetime.date(2026, 9, 5),
        "description": "Technical review of continuous perimeter wellpoint dewatering during sub-sea level basement excavation.",
        "days_overdue": 0,
        "signatories_required": 2,
        "signatories_completed": 1
    },
    {
        "project": proj2,
        "title": "Architectural Double-Skin Façade & Dynamic Wind Load Glazing Certificate",
        "request_type": "Document",
        "discipline": "Architecture",
        "priority": "Medium",
        "status": "Approved",
        "value_amount": Decimal("42000000.00"),
        "doa_level_required": "Director",
        "submitted_by_name": "Rovengates Properties Ltd",
        "due_date": datetime.date(2026, 8, 20),
        "description": "Verification of wind tunnel aerodynamic testing data (up to 180 km/h coastal wind gusts) for curtain wall glass installation.",
        "days_overdue": 0,
        "signatories_required": 2,
        "signatories_completed": 2
    },
    {
        "project": proj3,
        "title": "Heavy Goods Vehicle Ingress/Egress Highway Access & Traffic Impact Assessment",
        "request_type": "Permit",
        "discipline": "Legal",
        "priority": "High",
        "status": "Pending",
        "value_amount": Decimal("8500000.00"),
        "doa_level_required": "Director",
        "submitted_by_name": "Lekki Concession Co",
        "due_date": datetime.date(2026, 9, 12),
        "description": "Approval for dedicated acceleration/deceleration lanes off the Lekki-Epe expressway into the bonded warehouse complex.",
        "days_overdue": 0,
        "signatories_required": 1,
        "signatories_completed": 0
    }
]

created_approvals = []
for app_item in approvals_data:
    title = app_item["title"]
    app_obj, _ = ApprovalRequest.objects.update_or_create(title=title, defaults=app_item)
    created_approvals.append(app_obj)

# Seed Technical Criteria for first approval request
criteria_data = [
    ("Zoning & Building Height Clearance", "Pass", "Conforms to 32-floor coastal zoning ceiling with zero airspace obstruction.", 1),
    ("Structural Geotechnical Bearing Capacity", "Pass", "Soil bearing capacity verified at ≥350 kN/m² with bored cast-in-situ piles to 48m depth.", 2),
    ("Mass Concrete Thermal Cracking Mitigation Plan", "Pass", "Cooling pipes and low-heat slag cement formulation verified.", 3),
    ("Emergency Coastal Storm Surge Evacuation Access", "pending", "Awaiting physical simulation inspection report from Safety team.", 4)
]

for name, st, notes, ord_num in criteria_data:
    TechnicalReviewCriteria.objects.get_or_create(
        approval_request=created_approvals[0],
        name=name,
        defaults={"notes": notes, "status": st, "order": ord_num}
    )

# Seed Approval Decision History
ApprovalDecision.objects.get_or_create(
    approval_request=created_approvals[2],
    decision_reference="LOG-DEC-2026-088",
    defaults={
        "decider_name": "Arc. Folashade Okonjo",
        "decider_role": "Principal Planning Reviewer",
        "outcome": "Approved",
        "decision_notes": "Curtain wall structural calculations verified against BS EN 1991-1-4:2005 wind actions.",
        "signature_hash": "0x3c7e9a1b0d2f8e4c7719aa9182341b",
        "timestamp": timezone.now() - datetime.timedelta(days=3)
    }
)

# ==============================================================================
# 5. SEED ANALYTICS & EXECUTIVE KPIS
# ==============================================================================
print("5. Seeding Analytics, Department SLAs, and Officer Performance...")
dept_metrics = [
    ("Development & Building Control Directorate", Decimal("8.2"), Decimal("10.0"), 94, "High", 18),
    ("Structural Integrity & Materials Testing Unit", Decimal("4.8"), Decimal("7.0"), 97, "Medium", 8),
    ("Environmental, Geotechnical & EIA Directorate", Decimal("12.1"), Decimal("14.0"), 86, "High", 24),
    ("HSE, Site Monitoring & Enforcement Unit", Decimal("3.4"), Decimal("5.0"), 96, "Medium", 11),
    ("Statutory Approvals & Ministerial Board", Decimal("6.2"), Decimal("7.0"), 92, "Low", 6)
]

for name, turn, target, eff, load, pending in dept_metrics:
    DepartmentPerformanceMetric.objects.update_or_create(
        department_name=name,
        defaults={
            "turnaround_days": turn,
            "target_days": target,
            "efficiency_percentage": eff,
            "workload_level": load,
            "pending_reviews_count": pending
        }
    )

officers_data = [
    ("Engr. Babatunde Adeleke", "Chief Structural Integrity Officer", 58, 98, Decimal("2.4"), 1),
    ("Arc. Folashade Okonjo", "Principal Planning Officer", 46, 95, Decimal("3.1"), 2),
    ("Engr. Chukwuma Obi", "Senior MEP & Infrastructure Inspector", 42, 93, Decimal("3.5"), 3),
    ("Mrs. Amina Danjuma", "Lead Environmental Review Specialist", 38, 91, Decimal("4.0"), 4),
    ("Marcus Chen", "Principal Geodetic & Site Inspector", 35, 90, Decimal("4.2"), 5)
]

for name, role, count, sla, avg_days, rnk in officers_data:
    OfficerPerformanceRecord.objects.update_or_create(
        officer_name=name,
        defaults={
            "role": role,
            "inspections_completed": count,
            "sla_adherence_rate": sla,
            "average_review_days": avg_days,
            "rank": rnk
        }
    )

risk_alerts = [
    (proj1, "Eko Atlantic Marina Towers", 88, "Critical", "Subsurface tidal water pressure differential near seawall during storm surge.", "Active Alert"),
    (proj2, "Victoria Island Financial Center", 62, "High", "Deep basement dewatering settlement monitoring near adjacent heritage building.", "Under Monitoring"),
    (proj3, "Lekki FTZ Warehouse Complex", 38, "Medium", "Minor stormwater ponding near perimeter drainage trench.", "Under Monitoring"),
    (proj4, "Ikoyi Imperial Heights", 15, "Low", "Standard high-rise construction proceeding within normal engineering thresholds.", "Mitigated")
]

for proj, s_name, score, lvl, vuln, st in risk_alerts:
    RiskAssessmentAlert.objects.update_or_create(
        structure_name=s_name,
        defaults={
            "project": proj,
            "risk_score": score,
            "risk_level": lvl,
            "primary_vulnerability": vuln,
            "status": st
        }
    )

reports_data = [
    ("Q3 2026 Ministerial Executive Construction Safety & Regulatory Audit", "Executive", "PDF", "2.8 MB", ["Projects", "Inspections", "Compliance", "Financial"]),
    ("Lagos State Commercial High-Rise Structural Stability & Conformance Report", "Compliance", "PDF", "4.1 MB", ["Compliance", "NCRs", "CAPAs", "Certificates"]),
    ("State Physical Planning Revenue & Regularization Fee Financial Summary", "Financial", "CSV", "840 KB", ["Financial", "Permits", "Penalties"]),
    ("Agency Inter-Departmental SLA Adherence & Turnaround Benchmark 2026", "Performance", "PDF", "1.9 MB", ["Department Performance", "Officer Ranking"])
]

for title, rtype, fmt, sz, mods in reports_data:
    GeneratedReport.objects.update_or_create(
        title=title,
        defaults={
            "report_type": rtype,
            "format": fmt,
            "file_size": sz,
            "modules_included": mods,
            "status": "Ready",
            "file_url": "https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument/sample.pdf",
            "generated_by_name": "Director General - LASBCA"
        }
    )

# ==============================================================================
# 6. SEED NOTIFICATIONS & ALERTS
# ==============================================================================
print("6. Seeding Critical Emergency Alerts & Notifications...")
notifications_data = [
    {
        "category": "CRITICAL",
        "title": "STRUCTURAL DEFLECTION ALERT: Core Wall Formwork Shift at Grid 4-B",
        "message": "Automated GNSS tilt sensors detected 14mm lateral displacement on concrete formwork during 14th floor shear core placement.",
        "priority": "Critical",
        "severity": "Critical",
        "location": "Plot 4A, Marina District, Eko Atlantic City",
        "action_required": "Halt concrete pour immediately and dispatch Senior Structural Integrity Inspector.",
        "action_url": "/government/dashboard/compliance/non-conformances/",
        "is_read": False,
        "is_acknowledged": False
    },
    {
        "category": "CRITICAL",
        "title": "CRANE WIND VELOCITY WARNING: Coastal Gusts Exceeding 48 Knots",
        "message": "Site anemometer triggered gale-force wind threshold. Tower Crane 1 & 2 must be put into free-slew weathervaning mode immediately.",
        "priority": "Critical",
        "severity": "Critical",
        "location": "8 Bourdillon Road, Ikoyi",
        "action_required": "Confirm crane tie-back and secure all loose materials on upper deck.",
        "action_url": "/government/dashboard/notifications/critical/",
        "is_read": False,
        "is_acknowledged": True
    },
    {
        "category": "APPROVALS",
        "title": "Ministerial DoA Approval Pending: Phase 2 Foundation Piling (₦180,000,000)",
        "message": "High-value foundation clearance awaiting signature from Permanent Secretary / Director General.",
        "priority": "High",
        "severity": "Normal",
        "location": "Eko Atlantic Marina Towers",
        "action_required": "Review technical criteria matrix and execute digital signature.",
        "action_url": "/government/dashboard/approvals/pending/",
        "is_read": False,
        "is_acknowledged": False
    },
    {
        "category": "INSPECTIONS",
        "title": "Mandatory Stage 3 Pre-Pour Walkthrough Scheduled for Tomorrow at 09:00 AM",
        "message": "Engr. Babatunde Adeleke assigned to lead rebar inspection on Victoria Island Financial Center basement raft.",
        "priority": "Medium",
        "severity": "Normal",
        "location": "Adeola Odeku Street, Victoria Island",
        "action_required": "Review contractor pre-pour checklist and rebar mill certificates.",
        "action_url": "/government/dashboard/inspections/schedule/",
        "is_read": True,
        "is_acknowledged": True
    },
    {
        "category": "COMPLIANCE",
        "title": "Regulatory Escalation: NCR-101 Escalate to Stage 4 (Director Order)",
        "message": "Concrete strength deficiency on Eko Atlantic core wall has exceeded 21 days without approved engineering remediation.",
        "priority": "High",
        "severity": "Warning",
        "location": "Eko Atlantic Marina Towers",
        "action_required": "Issue formal Director Stop-Work order until carbon-fiber wrapping protocol is approved.",
        "action_url": "/government/dashboard/compliance/non-conformances/",
        "is_read": False,
        "is_acknowledged": False
    }
]

for notif in notifications_data:
    title = notif["title"]
    Notification.objects.update_or_create(title=title, defaults=notif)

# ==============================================================================
# 7. SEED AUDIT & ACTIVITY TRAIL
# ==============================================================================
print("7. Seeding Cryptographic Audit Trail Events...")
audit_events_data = [
    {
        "action": "PERMIT_AUTHORIZED_AND_SEALED",
        "resource_type": "Permit",
        "resource_id": "LASBCA/DEV/2026/0491",
        "project_name": "Eko Atlantic Marina Towers",
        "user_name": "Director General - LASBCA",
        "user_role": "Agency Head",
        "ip_address": "197.210.65.12",
        "severity": "Normal",
        "is_verified": True
    },
    {
        "action": "NCR_ESCALATION_STAGE_4_TRIGGERED",
        "resource_type": "NonConformanceReport",
        "resource_id": "NCR-101",
        "project_name": "Eko Atlantic Marina Towers",
        "user_name": "Engr. Babatunde Adeleke",
        "user_role": "Chief Structural Engineer",
        "ip_address": "197.210.65.18",
        "severity": "Critical",
        "is_verified": True
    },
    {
        "action": "DIGITAL_STAMP_APPLIED_TO_DRAWINGS",
        "resource_type": "Document",
        "resource_id": "AR-DWG-001",
        "project_name": "Eko Atlantic Marina Towers",
        "user_name": "Arc. Folashade Okonjo",
        "user_role": "Principal Planning Reviewer",
        "ip_address": "197.210.65.24",
        "severity": "Normal",
        "is_verified": True
    },
    {
        "action": "GNSS_TERSUS_RTK_POINT_CLOUD_SYNCED",
        "resource_type": "TersusDevice",
        "resource_id": "T-S1-8891",
        "project_name": "Victoria Island Financial Center",
        "user_name": "Marcus Chen",
        "user_role": "Senior Geodetic Inspector",
        "ip_address": "197.210.65.30",
        "severity": "Normal",
        "is_verified": True
    },
    {
        "action": "STATUTORY_CERTIFICATE_ISSUED",
        "resource_type": "ComplianceCertificate",
        "resource_id": "CERT-2026-0884",
        "project_name": "Victoria Island Financial Center",
        "user_name": "Director General - LASBCA",
        "user_role": "Agency Head",
        "ip_address": "197.210.65.12",
        "severity": "Normal",
        "is_verified": True
    }
]

for event in audit_events_data:
    try:
        AuditEvent.objects.create(**event)
    except Exception as e:
        pass

# ==============================================================================
# 8. SEED STAKEHOLDERS (DEVELOPERS, CONTRACTORS, CONSULTANTS, INSPECTORS)
# ==============================================================================
print("8. Seeding Stakeholder Registries, Blacklists, and Meetings...")
developers_data = [
    {
        "name": "Eko Atlantic Master Developers Ltd",
        "developer_id": "DEV-001",
        "status": "Verified",
        "active_projects_count": 8,
        "portfolio_value": "₦120.0B",
        "hq_location": "Victoria Island, Lagos",
        "primary_contact_name": "Mr. Ronald Chagoury Jr.",
        "primary_contact_email": "r.chagoury@ekoatlantic.com",
        "primary_contact_phone": "+234 1 270 0000",
        "color_theme": "bg-blue-600"
    },
    {
        "name": "Rovengates Properties Ltd",
        "developer_id": "DEV-002",
        "status": "Verified",
        "active_projects_count": 6,
        "portfolio_value": "₦45.8B",
        "hq_location": "Adeola Odeku, Victoria Island, Lagos",
        "primary_contact_name": "Alhaji Farouk Ibrahim",
        "primary_contact_email": "f.ibrahim@rovengates.ng",
        "primary_contact_phone": "+234 803 111 2233",
        "color_theme": "bg-indigo-600"
    },
    {
        "name": "Lekki Concession Development Co",
        "developer_id": "DEV-003",
        "status": "Verified",
        "active_projects_count": 4,
        "portfolio_value": "₦28.5B",
        "hq_location": "Lekki Phase 1, Lagos",
        "primary_contact_name": "Mrs. Ngozi Adeleke",
        "primary_contact_email": "n.adeleke@lekkiconcession.ng",
        "primary_contact_phone": "+234 1 454 8800",
        "color_theme": "bg-purple-600"
    },
    {
        "name": "Green Valley Urban Developers",
        "developer_id": "DEV-004",
        "status": "Verified",
        "active_projects_count": 3,
        "portfolio_value": "₦14.2B",
        "hq_location": "Central Business District, Abuja",
        "primary_contact_name": "Arc. Tunde Balogun",
        "primary_contact_email": "t.balogun@greenvalley.ng",
        "primary_contact_phone": "+234 9 291 4455",
        "color_theme": "bg-emerald-600"
    }
]

for dev in developers_data:
    dev_id = dev["developer_id"]
    Developer.objects.update_or_create(developer_id=dev_id, defaults=dev)

contractors_data = [
    {
        "name": "Julius Berger Nigeria Plc",
        "contractor_id": "CON-001",
        "contractor_type": "Tier-1 General Contractor",
        "status": "Prequalified",
        "license_status": "Valid",
        "license_number": "COREN-CAT-A-00192",
        "compliance_score": 98,
        "active_permits": 14,
        "specialties": ["Civil Engineering", "High-Rise Commercial Towers", "Marine Seawall Infrastructure", "Deep Piling"],
        "color_theme": "bg-blue-700"
    },
    {
        "name": "Cappa & D'Alberto Plc",
        "contractor_id": "CON-002",
        "contractor_type": "Tier-1 Building Contractor",
        "status": "Prequalified",
        "license_status": "Valid",
        "license_number": "COREN-CAT-A-00244",
        "compliance_score": 95,
        "active_permits": 9,
        "specialties": ["Commercial Office Towers", "Structural Concrete Framework", "Luxury Fitouts"],
        "color_theme": "bg-emerald-700"
    },
    {
        "name": "ITB Nigeria Limited",
        "contractor_id": "CON-003",
        "contractor_type": "Specialized Structural Contractor",
        "status": "Prequalified",
        "license_status": "Valid",
        "license_number": "COREN-CAT-B-00812",
        "compliance_score": 92,
        "active_permits": 6,
        "specialties": ["Deep Piling & Shoring", "Post-Tensioned Slabs", "Seismic Bracing"],
        "color_theme": "bg-amber-700"
    }
]

for con in contractors_data:
    con_id = con["contractor_id"]
    Contractor.objects.update_or_create(contractor_id=con_id, defaults=con)

consultants_data = [
    {
        "name": "O&A Engineering Consultants Ltd",
        "consultant_id": "CNS-001",
        "specialty": "Structural & Civil Engineering",
        "status": "Verified",
        "active_roles_count": 8,
        "hq_location": "Victoria Island, Lagos",
        "description": "Leading structural design advisory firm specializing in seismic resilience and tall building aerodynamics.",
        "color_theme": "bg-blue-600 text-white"
    },
    {
        "name": "ArchiGrid Design Studios",
        "consultant_id": "CNS-002",
        "specialty": "Architecture & Urban Master Planning",
        "status": "Verified",
        "active_roles_count": 5,
        "hq_location": "Ikoyi, Lagos",
        "description": "Award-winning architectural studio specializing in sustainable tropical high-rise and mixed-use commercial designs.",
        "color_theme": "bg-purple-600 text-white"
    },
    {
        "name": "EnviroTech Geosolutions Ltd",
        "consultant_id": "CNS-003",
        "specialty": "Geotechnical & Environmental EIA",
        "status": "Verified",
        "active_roles_count": 7,
        "hq_location": "Ikeja, Lagos",
        "description": "Specialist geotechnical laboratory offering deep borehole coring, soil stabilization, and statutory environmental impact studies.",
        "color_theme": "bg-emerald-600 text-white"
    }
]

for cns in consultants_data:
    cns_id = cns["consultant_id"]
    Consultant.objects.update_or_create(consultant_id=cns_id, defaults=cns)

inspectors_data = [
    {
        "name": "Engr. Babatunde Adeleke",
        "inspector_id": "INS-001",
        "role_title": "Chief Structural Integrity Officer",
        "inspector_type": "Internal (Gov)",
        "assigned_zone": "Victoria Island & Eko Atlantic",
        "active_inspections": 14,
        "pass_rate": "94%",
        "ncrs_issued": 8
    },
    {
        "name": "Engr. Chukwuma Obi",
        "inspector_id": "INS-002",
        "role_title": "Senior MEP & Infrastructure Inspector",
        "inspector_type": "Internal (Gov)",
        "assigned_zone": "Lekki, Ikate & Ibeju-Lekki",
        "active_inspections": 9,
        "pass_rate": "91%",
        "ncrs_issued": 5
    },
    {
        "name": "Marcus Chen",
        "inspector_id": "INS-003",
        "role_title": "Senior Geodetic & GNSS Survey Inspector",
        "inspector_type": "Third-Party Accredited",
        "assigned_zone": "Ikoyi, Victoria Island & Lagos Mainland",
        "active_inspections": 11,
        "pass_rate": "88%",
        "ncrs_issued": 6
    }
]

for ins in inspectors_data:
    ins_id = ins["inspector_id"]
    Inspector.objects.update_or_create(inspector_id=ins_id, defaults=ins)

# Seed Blacklist Records
BlacklistRecord.objects.get_or_create(
    entity_name="Mainland Heavy Builders Ltd",
    defaults={
        "entity_type": "Contractor",
        "entity_id": "CON-BLK-01",
        "reason": "Repeated structural non-conformance, unpermitted floor additions, and use of counterfeit reinforcement steel.",
        "incident_count": 4,
        "status": "Blacklisted"
    }
)
BlacklistRecord.objects.get_or_create(
    entity_name="Apex Urban Planners Ltd",
    defaults={
        "entity_type": "Consultant",
        "entity_id": "CNS-BLK-02",
        "reason": "Falsification of borehole geotechnical soil investigation test results on coastal construction site.",
        "incident_count": 2,
        "status": "Suspended"
    }
)

# Seed Meetings
StakeholderMeeting.objects.get_or_create(
    title="Bi-Weekly Statutory Technical Review: Eko Atlantic Foundation Pour",
    defaults={
        "agenda": "Review compression cube UPV test results and sign-off on 12,000m³ raft concrete placing procedure.",
        "project_name": "Eko Atlantic Marina Towers",
        "date": "Sep 02, 2026",
        "time_slot": "10:00 AM - 11:30 AM",
        "meeting_type": "Video Call",
        "status": "Scheduled",
        "initiator_name": "Dr. Olusegun Adebayo",
        "initiator_role": "Agency Head / Director General",
        "room_id": "room-eko-marina-2026",
        "participants": ["Dr. Olusegun Adebayo", "Engr. Babatunde Adeleke", "Julius Berger QC Lead", "Arc. Folashade Okonjo"]
    }
)

# ==============================================================================
# 9. SEED INTEGRATIONS (TERSUS GNSS, BIM, DMS, APIS, KEYS, LOGS)
# ==============================================================================
print("9. Seeding Integrations (Tersus GNSS, BIM platforms, Government APIs, Keys)...")
tersus_devices = [
    {
        "device_id": "T-S1-8891",
        "name": "Tersus Oscar Ultimate GNSS Receiver (Base Station 01)",
        "device_type": "High-Precision RTK Base Station",
        "status": "Active",
        "battery_level": "98%",
        "ip_address": "192.168.1.140",
        "latitude": 6.4281,
        "longitude": 3.4219,
        "firmware_version": "v2.8.4"
    },
    {
        "device_id": "T-S1-8892",
        "name": "Tersus David GNSS Rover (Field Survey Unit A)",
        "device_type": "Handheld RTK Rover",
        "status": "Active",
        "battery_level": "84%",
        "ip_address": "192.168.1.145",
        "latitude": 6.4312,
        "longitude": 3.4190,
        "firmware_version": "v2.8.4"
    },
    {
        "device_id": "T-S1-8893",
        "name": "Tersus Matrix-RTK Survey Station (Lekki Zone Base)",
        "device_type": "Continuous Geodetic Reference Station",
        "status": "Active",
        "battery_level": "100%",
        "ip_address": "192.168.2.10",
        "latitude": 6.4485,
        "longitude": 3.4750,
        "firmware_version": "v3.0.1"
    }
]

for dev in tersus_devices:
    d_id = dev["device_id"]
    TersusDevice.objects.update_or_create(device_id=d_id, defaults=dev)

# Seed BIM Integrations
BIMIntegration.objects.get_or_create(
    provider="Autodesk Construction Cloud (BIM 360 / ACC)",
    defaults={
        "client_id": "ACC-NEXUCON-GOV-01",
        "status": "Connected",
        "synced_models_count": 18,
        "icon_code": "A"
    }
)
BIMIntegration.objects.get_or_create(
    provider="Bentley ProjectWise OpenBuildings",
    defaults={
        "client_id": "PW-NEXUCON-INFRA-04",
        "status": "Connected",
        "synced_models_count": 6,
        "icon_code": "B"
    }
)

# Seed DMS Integrations
DocumentSystemIntegration.objects.update_or_create(
    name="Cloudflare R2 Storage (Primary Documents & CAD)",
    defaults={
        "system_type": "Enterprise Cloud Storage",
        "storage_provider": "Cloudflare R2",
        "status": "Active",
        "bucket_or_drive_name": "nexucondocument",
        "endpoint_url": "https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument",
        "synced_files_count": 4512,
        "folder_count": 12
    }
)
DocumentSystemIntegration.objects.update_or_create(
    name="Cloudinary Media Engine (Site Inspection Photos)",
    defaults={
        "system_type": "High-Res Media CDN",
        "storage_provider": "Cloudinary",
        "status": "Active",
        "bucket_or_drive_name": "fspyt1uw (nexucon/daily_updates)",
        "endpoint_url": "https://api.cloudinary.com/v1_1/fspyt1uw/image/upload",
        "synced_files_count": 1820,
        "folder_count": 6
    }
)
# Clean up any legacy dummy demo records
DocumentSystemIntegration.objects.filter(name="Microsoft SharePoint Online / M365 DMS").delete()

# Seed Government APIs
GovernmentAPIIntegration.objects.get_or_create(
    api_key_identifier="LAGIS-SPATIAL-API-01",
    defaults={
        "name": "Lagos State Geographic Information Systems (LAGIS Spatial Gateway)",
        "description": "State cadastral survey and zoning boundary verification bridge.",
        "endpoint_url": "https://api.lagis.lagosstate.gov.ng/v2/cadastral",
        "status": "connected",
        "data_flow_direction": "Bidirectional"
    }
)
GovernmentAPIIntegration.objects.get_or_create(
    api_key_identifier="CAC-CORP-VERIFY-02",
    defaults={
        "name": "Corporate Affairs Commission (CAC Nigeria Business Verification)",
        "description": "Real-time registration and corporate status lookups for developers & contractors.",
        "endpoint_url": "https://api.cac.gov.ng/v1/verify-rc",
        "status": "connected",
        "data_flow_direction": "Inbound"
    }
)
GovernmentAPIIntegration.objects.get_or_create(
    api_key_identifier="NIBSS-REMITA-PAY-03",
    defaults={
        "name": "NIBSS Remita Electronic Revenue Collection Gateway",
        "description": "Automated payment and permit fee receipt reconciliation.",
        "endpoint_url": "https://api.nibss-plc.com.ng/v3/ebills",
        "status": "connected",
        "data_flow_direction": "Bidirectional"
    }
)

# Seed API Keys
APIKeyCredential.objects.get_or_create(
    name="LASBCA Field Inspection Mobile Gateway Key",
    defaults={
        "key_prefix": "nxc_live_pk_88f9",
        "hashed_key": "0x8f4e2c9b1a7d3e5f8842bc0182419a",
        "app_type": "Mobile Application",
        "volume_tier": "Enterprise Unlimited",
        "status": "Healthy"
    }
)
APIKeyCredential.objects.get_or_create(
    name="Tersus GNSS RTK Telemetry Live Ingestion Key",
    defaults={
        "key_prefix": "nxc_live_sk_42bc",
        "hashed_key": "0x3c7e9a1b0d2f8e4c7719aa9182341b",
        "app_type": "IoT Hardware Telemetry",
        "volume_tier": "Real-time High Throughput",
        "status": "Healthy"
    }
)

# Seed Integration Logs
IntegrationLog.objects.get_or_create(
    log_reference="log-88901",
    defaults={
        "service_name": "Tersus GNSS RTK",
        "event_name": "Telemetry Coordinate Ingestion",
        "status": "Success",
        "http_status_code": 200,
        "payload_size": "45 KB",
        "details": "Synchronized 28 satellite observables and RTK fixed base vector."
    }
)
IntegrationLog.objects.get_or_create(
    log_reference="log-88902",
    defaults={
        "service_name": "Autodesk ACC",
        "event_name": "Federated IFC Model Sync",
        "status": "Success",
        "http_status_code": 200,
        "payload_size": "345 MB",
        "details": "Model Eko_Atlantic_Marina_Towers_Arch_v2.1.ifc synchronized successfully."
    }
)

# ==============================================================================
# 10. SEED BIM (MODELS, CLASHES, ANNOTATIONS, 4D PROGRESS)
# ==============================================================================
print("10. Seeding BIM Models, Clash Matrix, and Annotations...")
bim_models_data = [
    {
        "project": proj1,
        "name": "Eko Atlantic Marina Towers - Architecture Model",
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
        "hash_signature": "0x8f4e2c9b1a7d3e5f8842bc0182419a"
    },
    {
        "project": proj1,
        "name": "Eko Atlantic Marina Towers - Structural Engineering Model",
        "discipline": "Structural",
        "format": "IFC4",
        "file_size": "285 MB",
        "current_version": "v1.8",
        "status": "Approved",
        "lod": "LOD 400",
        "element_count": 24100,
        "is_digitally_certified": True,
        "certified_by_name": "Chief Structural Engineer",
        "certified_at": timezone.now() - datetime.timedelta(days=15),
        "hash_signature": "0x3c7e9a1b0d2f8e4c7719aa9182341b"
    },
    {
        "project": proj1,
        "name": "Eko Atlantic Marina Towers - MEP Services Coordination Model",
        "discipline": "MEP",
        "format": "IFC4",
        "file_size": "198 MB",
        "current_version": "v1.2",
        "status": "Under Review",
        "lod": "LOD 300",
        "element_count": 14200,
        "is_digitally_certified": False
    }
]

created_bim_models = []
for b_item in bim_models_data:
    b_name = b_item["name"]
    b_obj, _ = BIMModel.objects.update_or_create(name=b_name, defaults=b_item)
    created_bim_models.append(b_obj)
    BIMModelVersion.objects.get_or_create(
        model=b_obj,
        version_label=b_obj.current_version,
        defaults={
            "commit_hash": "a8f93bc2",
            "changes_summary": "Full coordinate georeferencing and structural grid alignment.",
            "author_name": "Arc. Folashade Okonjo",
            "author_role": "BIM Manager",
            "stats_added": 420,
            "stats_modified": 85,
            "stats_removed": 12,
            "file_size": b_obj.file_size
        }
    )

# Seed BIM Clashes
BIMClash.objects.get_or_create(
    title="Primary HVAC Duct intersecting Heavy Transfer Beam at 14th Floor Core",
    defaults={
        "project": proj1,
        "primary_model": created_bim_models[1], # Structural
        "secondary_model": created_bim_models[2], # MEP
        "clash_type": "HARD_CLASH",
        "description": "Physical clearance violation of -185mm between main HVAC branch and loadbearing beam at Grid 4-B / Level 14.",
        "severity": "CRITICAL",
        "status": "OPEN",
        "assigned_discipline": "MEP",
        "assigned_to_name": "Julius Berger MEP Team",
        "coordinates_3d": {"x": 12.4, "y": 8.5, "z": 45.2}
    }
)
BIMClash.objects.get_or_create(
    title="Emergency Fire Sprinkler Main intersecting Structural Column C-12",
    defaults={
        "project": proj1,
        "primary_model": created_bim_models[1],
        "secondary_model": created_bim_models[2],
        "clash_type": "HARD_CLASH",
        "description": "Sprinkler pipe intersects column reinforcement casing at Level 8.",
        "severity": "HIGH",
        "status": "IN_REVIEW",
        "assigned_discipline": "MEP",
        "assigned_to_name": "Apex Fire Protection Engineer",
        "coordinates_3d": {"x": 22.1, "y": 14.8, "z": 28.0}
    }
)

# Seed BIM Annotations
BIMAnnotation.objects.get_or_create(
    model=created_bim_models[0],
    text="Verify that louver projection satisfies Lagos Green Building energy efficiency code.",
    defaults={
        "project": proj1,
        "author_name": "Arc. Folashade Okonjo",
        "author_role": "Principal Planning Reviewer",
        "status": "Open",
        "priority": "Medium",
        "element_ids": ["EXT-GLAZ-32-001"],
        "viewpoint_camera": {"x": 124.5, "y": 88.2, "z": 45.0, "pitch": -15, "yaw": 45}
    }
)

# ==============================================================================
# 11. SEED SETTINGS (STAFF INVITATIONS, ROLES, WORKFLOWS, STANDARDS, STATUTES)
# ==============================================================================
print("11. Seeding Staff Invitations, Roles, Workflows, and Compliance Standards...")
staff_invitations = [
    ("Dr. Olusegun Adebayo", "o.adebayo@lasbca.gov.ng", "Director General", "Executive Board", "Accepted"),
    ("Engr. Babatunde Adeleke", "b.adeleke@lasbca.gov.ng", "Chief Structural Integrity Officer", "Structural Unit", "Accepted"),
    ("Arc. Folashade Okonjo", "f.okonjo@lasbca.gov.ng", "Principal Planning Officer", "Development Control", "Accepted"),
    ("Mrs. Amina Danjuma", "a.danjuma@lasbca.gov.ng", "Lead Environmental Specialist", "EIA Directorate", "Accepted"),
    ("Engr. Chukwuma Obi", "c.obi@lasbca.gov.ng", "Senior MEP Inspector", "Building Services", "Pending")
]

for name, email, role, dept, st in staff_invitations:
    UserInvitation.objects.update_or_create(
        email=email,
        defaults={"name": name, "role": role, "department": dept, "status": st}
    )

roles_data = [
    ("Agency Administrator", "Full ministerial control and executive signing authority.", "System Default", True, 2),
    ("Chief Structural Engineer", "Review and authorize structural engineering calculations and core casting.", "Custom Role", False, 4),
    ("Principal Planning Officer", "Zoning and architectural setback conformity review.", "Custom Role", False, 6),
    ("Lead Field Inspector", "Conduct on-site inspections, material testing, and issue NCRs.", "Custom Role", False, 12)
]

for name, desc, rtype, is_def, count in roles_data:
    CustomRole.objects.update_or_create(
        name=name,
        defaults={"description": desc, "role_type": rtype, "is_system_default": is_def, "active_users_count": count}
    )

standards_data = [
    ("std-concrete-grade30", "Minimum Concrete Cube Compressive Strength (Grade 30)", "Structural Concrete", 30.0, "N/mm²", "Critical", "Design compressive strength for heavy loadbearing shear walls."),
    ("std-rebar-spacing", "Maximum Structural Rebar Spacing (Slabs)", "Reinforcement Steel", 200.0, "mm", "Warning", "Maximum center-to-center bar spacing for crack control."),
    ("std-front-setback", "Minimum Front Property Setback", "Urban Planning & Zoning", 6.0, "Meters", "Warning", "Mandatory building setback from road right-of-way boundary."),
    ("std-effluent-turbidity", "Maximum Stormwater Effluent Turbidity", "Environmental Quality", 50.0, "NTU", "Critical", "Maximum allowable suspended solids in discharged groundwater.")
]

for key, lbl, cat, val, unit, lvl, desc in standards_data:
    ComplianceStandard.objects.update_or_create(
        key=key,
        defaults={
            "label": lbl,
            "category": cat,
            "num_value": val,
            "unit": unit,
            "alert_level": lvl,
            "description": desc
        }
    )

statutes_data = [
    ("LASBCA-ACT-2019", "Lagos State Building Control Agency Regulations & Enforcement Act", ["Permits", "Stage Inspections", "Enforcement", "Demolition"]),
    ("NBC-NIG-2020", "Federal Republic of Nigeria National Building Code (2nd Edition)", ["Structural Integrity", "Fire Protection", "Accessibility", "EIA"]),
    ("NESREA-ENV-2018", "National Environmental (Construction Sector) Regulations", ["Effluent Discharge", "Noise Levels", "Hazardous Waste Management"]),
    ("COREN-ENG-2022", "Council for the Regulation of Engineering in Nigeria Statutory Code", ["Mandatory Site Engineers", "Material Certification", "Quality Assurance"])
]

for code, name, feats in statutes_data:
    StatutoryDocument.objects.update_or_create(
        code=code,
        defaults={"name": name, "connected_features": feats, "document_url": "https://ba64cd9c51c2da4db93a1886397fd7b3.r2.cloudflarestorage.com/nexucondocument/sample.pdf"}
    )

# Seed Approval Workflows
wf, _ = ApprovalWorkflow.objects.get_or_create(
    id="WF-HR-01",
    defaults={
        "name": "Commercial High-Rise Multi-Stage Approval Workflow (>15 Floors)",
        "description": "Mandatory 4-stage technical vetting for commercial skyscrapers with ministerial delegation.",
        "status": "Active"
    }
)

steps_data = [
    (1, "1. Architectural & Zoning Setback Check", "Planning Officer", "Layers"),
    (2, "2. Structural Core & Piling Calculation Review", "Senior Structural Engineer", "Building2"),
    (3, "3. MEP & Fire Safety Conformance Check", "MEP Directorate", "Activity"),
    (4, "4. Final Ministerial Seal & Authorization", "Permanent Secretary / Director General", "ShieldCheck")
]

for ord_num, title, role, icon in steps_data:
    WorkflowStep.objects.get_or_create(
        workflow=wf,
        step_order=ord_num,
        defaults={"title": title, "role": role, "icon_name": icon}
    )

print("==========================================================")
print("SUCCESS: Comprehensive Database Seeding Finished Successfully!")
print("==========================================================")
