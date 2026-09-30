from django.core.management.base import BaseCommand
from django.utils import timezone
import datetime

from apps.projects.models import Project, ProjectMilestone
from apps.inspections.models import Inspection, StopWorkOrder
from apps.documents.models import Document
from apps.public_portal.models import PublicNotice, ViolationReport
from apps.stakeholders.models import (
    Developer, Contractor, Consultant, Inspector, LicensedProfessional,
    BuildingStageInspection, ProjectTimelineMilestone, StatutoryFinancialTransaction
)


class Command(BaseCommand):
    help = "Seed authoritative data for Public Transparency Portal and Government Stakeholders"

    def handle(self, *args, **options):
        self.stdout.write("Seeding Public Transparency Portal & Stakeholder records...")

        # 1. Seed Developers & Inspectors if missing
        dev_eko, _ = Developer.objects.get_or_create(
            name="South Energyx Nigeria Limited",
            defaults={
                "developer_id": "DEV-001",
                "status": "Active",
                "active_projects_count": 5,
                "portfolio_value": "₦420,000,000,000",
                "hq_location": "Victoria Island, Lagos",
                "primary_contact_name": "David Frame",
                "color_theme": "blue",
                "is_active": True,
            }
        )
        dev_lekki, _ = Developer.objects.get_or_create(
            name="Lekki Port LFTZ Enterprise",
            defaults={
                "developer_id": "DEV-002",
                "status": "Active",
                "active_projects_count": 3,
                "portfolio_value": "₦180,000,000,000",
                "hq_location": "Ibeju-Lekki, Lagos",
                "primary_contact_name": "Engr. Biodun Lawal",
                "color_theme": "emerald",
                "is_active": True,
            }
        )

        inspector_adebayo, _ = Inspector.objects.get_or_create(
            name="Engr. Olufemi Adebayo",
            defaults={
                "inspector_id": "INS-001",
                "role_title": "Senior Building Inspector",
                "inspector_type": "Civil & Structural",
                "assigned_zone": "Lagos Island Zone 1",
                "active_inspections": 6,
                "pass_rate": "92.4%",
                "ncrs_issued": 14,
            }
        )
        inspector_nwosu, _ = Inspector.objects.get_or_create(
            name="Arc. Chioma Nwosu",
            defaults={
                "inspector_id": "INS-002",
                "role_title": "Zonal Review Officer",
                "inspector_type": "Architectural Compliance",
                "assigned_zone": "Eti-Osa / Victoria Island",
                "active_inspections": 4,
                "pass_rate": "96.1%",
                "ncrs_issued": 8,
            }
        )

        # 2. Seed Public Projects
        projects_data = [
            {
                "name": "Eko Atlantic Horizon Towers",
                "reference_number": "NXC-GOV-2026-0041",
                "project_type": "Commercial",
                "status": "ACTIVE",
                "developer_name": "South Energyx Nigeria Limited",
                "developer_organization": "South Energyx Nigeria Limited",
                "site_address": "Plot 14, Marina District, Eko Atlantic City",
                "lga": "Eti-Osa",
                "state": "Lagos State",
                "permit_number": "LASBCA/PRM/2026/0419",
                "permit_status": "VALID_ACTIVE",
                "number_of_floors": 32,
                "site_area": 14500.0,
                "gross_floor_area": 48000.0,
                "latitude": 6.4253,
                "longitude": 3.4219,
                "start_date": datetime.date(2025, 3, 1),
                "estimated_completion": datetime.date(2028, 6, 30),
                "permit_expiry_date": datetime.date(2028, 12, 31),
                "approval_date": datetime.date(2025, 2, 15),
            },
            {
                "name": "Lekki Deep Sea Commercial Interchange",
                "reference_number": "NXC-GOV-2026-0082",
                "project_type": "Infrastructure",
                "status": "ACTIVE",
                "developer_name": "Lekki Port LFTZ Enterprise",
                "developer_organization": "Lekki Port LFTZ Enterprise",
                "site_address": "KM 45 Lekki-Epe Expressway, Free Trade Zone",
                "lga": "Ibeju-Lekki",
                "state": "Lagos State",
                "permit_number": "LASBCA/PRM/2026/0882",
                "permit_status": "VALID_ACTIVE",
                "number_of_floors": 4,
                "site_area": 35000.0,
                "gross_floor_area": 22000.0,
                "latitude": 6.4698,
                "longitude": 3.6015,
                "start_date": datetime.date(2025, 6, 1),
                "estimated_completion": datetime.date(2027, 12, 15),
                "permit_expiry_date": datetime.date(2028, 6, 30),
                "approval_date": datetime.date(2025, 5, 10),
            },
            {
                "name": "Victoria Island Central Commercial Hub",
                "reference_number": "NXC-GOV-2026-0114",
                "project_type": "Commercial",
                "status": "ACTIVE",
                "developer_name": "Urban Prime Properties Plc",
                "developer_organization": "Urban Prime Properties Plc",
                "site_address": "Adeola Odeku Street, Victoria Island",
                "lga": "Eti-Osa",
                "state": "Lagos State",
                "permit_number": "LASBCA/PRM/2026/0114",
                "permit_status": "VALID_ACTIVE",
                "number_of_floors": 18,
                "site_area": 8200.0,
                "gross_floor_area": 29000.0,
                "latitude": 6.4311,
                "longitude": 3.4352,
                "start_date": datetime.date(2025, 1, 10),
                "estimated_completion": datetime.date(2027, 9, 30),
                "permit_expiry_date": datetime.date(2028, 1, 10),
                "approval_date": datetime.date(2024, 12, 20),
            },
            {
                "name": "Ikeja Tech Innovation District Phase 1",
                "reference_number": "NXC-GOV-2026-0331",
                "project_type": "Mixed-Use",
                "status": "ACTIVE",
                "developer_name": "Lagos State Development and Property Corporation (LSDPC)",
                "developer_organization": "LSDPC",
                "site_address": "Mobolaji Bank Anthony Way, Ikeja",
                "lga": "Ikeja",
                "state": "Lagos State",
                "permit_number": "LASBCA/PRM/2026/0331",
                "permit_status": "VALID_ACTIVE",
                "number_of_floors": 12,
                "site_area": 12000.0,
                "gross_floor_area": 31000.0,
                "latitude": 6.6018,
                "longitude": 3.3515,
                "start_date": datetime.date(2025, 8, 1),
                "estimated_completion": datetime.date(2028, 4, 30),
                "permit_expiry_date": datetime.date(2028, 8, 1),
                "approval_date": datetime.date(2025, 7, 15),
            },
            {
                "name": "Badagry Coastal Trade Logistics Hub",
                "reference_number": "NXC-GOV-2026-0991",
                "project_type": "Industrial",
                "status": "SUSPENDED",
                "developer_name": "West Coast Maritime Logistics",
                "developer_organization": "West Coast Maritime Logistics",
                "site_address": "Coastal Expressway, Marina Waterfront, Badagry",
                "lga": "Badagry",
                "state": "Lagos State",
                "permit_number": "LASBCA/PRM/2026/0991",
                "permit_status": "SUSPENDED_REVIEW",
                "number_of_floors": 3,
                "site_area": 25000.0,
                "gross_floor_area": 18000.0,
                "latitude": 6.4167,
                "longitude": 2.8833,
                "start_date": datetime.date(2025, 4, 1),
                "estimated_completion": datetime.date(2027, 8, 15),
                "permit_expiry_date": datetime.date(2027, 12, 31),
                "approval_date": datetime.date(2025, 3, 20),
            },
        ]

        created_projects = []
        for pdata in projects_data:
            proj, _ = Project.objects.update_or_create(
                reference_number=pdata["reference_number"],
                defaults=pdata
            )
            created_projects.append(proj)

            # Milestones
            ProjectMilestone.objects.get_or_create(
                project=proj,
                title="Foundation & Substructure Piling",
                defaults={
                    "target_date": datetime.date(2025, 9, 30),
                    "is_completed": True,
                    "completion_date": datetime.date(2025, 9, 28)
                }
            )
            ProjectMilestone.objects.get_or_create(
                project=proj,
                title="Superstructure Core Framing",
                defaults={
                    "target_date": datetime.date(2026, 11, 30),
                    "is_completed": False
                }
            )

            # Inspection
            Inspection.objects.get_or_create(
                project=proj,
                inspection_type="Foundation Inspection",
                defaults={
                    "inspection_reference": f"INS-{proj.reference_number[-8:]}-01",
                    "status": "COMPLETED",
                    "outcome": "PASSED",
                    "scheduled_date": timezone.now(),
                    "completed_date": timezone.now(),
                    "inspector_name": inspector_adebayo.name,
                }
            )

            # Approved Document
            Document.objects.get_or_create(
                project=proj,
                document_reference=f"DOC-PERMIT-{proj.reference_number[-8:]}",
                defaults={
                    "title": f"Statutory Building Planning Clearance ({proj.permit_number})",
                    "document_type": "Planning Permit",
                    "status": "APPROVED",
                    "is_digitally_stamped": True,
                    "stamp_reference": f"STAMP-LASBCA-2026-{proj.reference_number[-4:]}",
                    "file_url": "https://api.nexucon.net/media/sample_permit.pdf",
                    "file_size": 2.4,
                }
            )

        # 3. Add StopWorkOrder for the suspended project
        badagry_proj = next((p for p in created_projects if "Badagry" in p.name), None)
        if badagry_proj:
            StopWorkOrder.objects.get_or_create(
                project=badagry_proj,
                defaults={
                    "order_number": "SWO-LAS-2026-0044",
                    "reason": "Structural non-conformance: sub-base soil settlement test failed geotechnical criteria under heavy coastal loads. Excavation halted pending reinforcement remediation.",
                    "severity": "CRITICAL",
                    "status": "ACTIVE",
                    "issued_by_name": "LASBCA Zonal Enforcement Directorate (Badagry Division)",
                }
            )

        # 4. Seed Public Notices
        notices_data = [
            {
                "reference_number": "NTC-2026-0081",
                "notice_type": "STOP_WORK",
                "title": "Statutory Stop-Work Notice: Badagry Coastal Trade Logistics Hub",
                "description": "Notice of immediate suspension of construction activities on Sector 4 pending foundation soil bearing capacity re-test and geotechnical review.",
                "issuing_agency": "Lagos State Building Control Agency (LASBCA)",
                "target_lga": "Badagry",
                "target_project_name": "Badagry Coastal Trade Logistics Hub",
                "target_permit_number": "LASBCA/PRM/2026/0991",
                "effective_date": datetime.date(2026, 9, 15),
                "is_active": True,
                "severity": "CRITICAL",
            },
            {
                "reference_number": "NTC-2026-0082",
                "notice_type": "SAFETY_ADVISORY",
                "title": "Severe Rainstorm & Coastal Excavation Safety Advisory",
                "description": "All developers and contractors operating deep basement excavations in coastal and reclamation corridors must install continuous dewatering pumps and shore retaining walls.",
                "issuing_agency": "Lagos State Materials Testing Laboratory (LSMTL)",
                "target_lga": "Statewide",
                "effective_date": datetime.date(2026, 9, 18),
                "is_active": True,
                "severity": "WARNING",
            },
            {
                "reference_number": "NTC-2026-0083",
                "notice_type": "STAGE_CLEARANCE",
                "title": "Stage Clearance Endorsement: Eko Atlantic Horizon Towers",
                "description": "Floor 4 suspended slab and concrete compressive strength results certified (36.8 MPa achieved against 30.0 MPa design threshold). Stage gate cleared for level 5 vertical framing.",
                "issuing_agency": "Lagos State Building Control Agency (LASBCA)",
                "target_lga": "Eti-Osa",
                "target_project_name": "Eko Atlantic Horizon Towers",
                "target_permit_number": "LASBCA/PRM/2026/0419",
                "effective_date": datetime.date(2026, 9, 22),
                "is_active": True,
                "severity": "INFO",
            },
            {
                "reference_number": "NTC-2026-0084",
                "notice_type": "REGULATORY_UPDATE",
                "title": "Mandatory NDT Ultrasonic Pulse Velocity Testing for Slabs > 5 Storeys",
                "description": "Pursuant to LASBCA Regulation 2026/14, all multi-storey construction projects exceeding 5 floors must submit digital PUNDIT ultrasonic pulse velocity (UPV) test evidence before slab formwork removal.",
                "issuing_agency": "Ministry of Physical Planning and Urban Development",
                "target_lga": "Statewide",
                "effective_date": datetime.date(2026, 9, 1),
                "is_active": True,
                "severity": "INFO",
            },
        ]

        for ndata in notices_data:
            PublicNotice.objects.update_or_create(
                reference_number=ndata["reference_number"],
                defaults=ndata
            )

        # 5. Seed Government Stakeholder Records
        # Stage Inspections
        stage_inspections_data = [
            {
                "stage_id": "INS-STG-2026-041",
                "project_name": "Eko Atlantic Horizon Towers",
                "stage": "Foundation Pour & Rebar Cover",
                "assigned_inspector": inspector_adebayo,
                "contractor_on_site": "Julius Berger Nigeria Plc",
                "preferred_date": "20 Sep 2026",
                "time_slot": "Morning (09:00 - 12:00)",
                "status": "Scheduled",
                "has_ncr": False,
            },
            {
                "stage_id": "INS-STG-2026-039",
                "project_name": "Victoria Island Central Commercial Hub",
                "stage": "Level 4 Floor Slab Concrete Pour",
                "assigned_inspector": inspector_nwosu,
                "contractor_on_site": "China Civil Engineering Construction Corp (CCECC)",
                "preferred_date": "18 Sep 2026",
                "time_slot": "Afternoon (13:00 - 16:00)",
                "status": "Passed",
                "has_ncr": False,
            },
            {
                "stage_id": "INS-STG-2026-036",
                "project_name": "Badagry Coastal Trade Logistics Hub",
                "stage": "Soil Bearing & Piling Rig Verification",
                "assigned_inspector": inspector_adebayo,
                "contractor_on_site": "Dredging International Services",
                "preferred_date": "14 Sep 2026",
                "time_slot": "Morning (09:00 - 12:00)",
                "status": "Action Required (NCR)",
                "has_ncr": True,
                "ncr_description": "Sub-base piling density test fell below statutory compaction minimum (92% vs 98% required under coastal foundation code).",
                "ncr_deadline": "05 Oct 2026",
            },
        ]

        for sdata in stage_inspections_data:
            BuildingStageInspection.objects.update_or_create(
                stage_id=sdata["stage_id"],
                defaults=sdata
            )

        # Timeline Milestones
        milestones_data = [
            {
                "milestone_id": "ML-EKO-01",
                "project_name": "Eko Atlantic Horizon Towers",
                "name": "Geotechnical Substructure & Soil Bearing Verification",
                "category": "Foundation Stage",
                "start_date": "01 Jan 2026",
                "due_date": "28 Feb 2026",
                "is_hold_point": True,
                "government_signoff": "Approved & Sealed (LASBCA Zonal Engr)",
                "progress": 100,
                "status": "Completed",
            },
            {
                "milestone_id": "ML-EKO-02",
                "project_name": "Eko Atlantic Horizon Towers",
                "name": "Level 1 to 8 Superstructure Concrete Pour",
                "category": "Superstructure",
                "start_date": "01 Mar 2026",
                "due_date": "30 Oct 2026",
                "is_hold_point": True,
                "government_signoff": "Active Regulatory Review",
                "progress": 68,
                "status": "In Progress",
            },
            {
                "milestone_id": "ML-VI-01",
                "project_name": "Victoria Island Central Commercial Hub",
                "name": "MEP Riser & Fire Safety Penetration Clearance",
                "category": "Services & MEP",
                "start_date": "15 Sep 2026",
                "due_date": "15 Dec 2026",
                "is_hold_point": False,
                "government_signoff": "Pending Submission",
                "progress": 25,
                "status": "In Progress",
            },
        ]

        for mdata in milestones_data:
            ProjectTimelineMilestone.objects.update_or_create(
                milestone_id=mdata["milestone_id"],
                defaults=mdata
            )

        # Financial Invoices
        invoices_data = [
            {
                "invoice_number": "INV-2026-0041",
                "project_name": "Eko Atlantic Horizon Towers",
                "fee_category": "Building Planning Permit Assessment Levy",
                "amount": 18500000.00,
                "amount_formatted": "₦18,500,000.00",
                "issued_date": "01 Sep 2026",
                "due_date": "30 Sep 2026",
                "status": "PAID",
                "paid_date": "14 Sep 2026",
                "receipt_number": "REC-LAS-89304",
                "beneficiary": "Lagos State Central Revenue Board (LASBCA Sub-Account)",
                "payment_gateway": "Remita",
            },
            {
                "invoice_number": "INV-2026-0054",
                "project_name": "Eko Atlantic Horizon Towers",
                "fee_category": "Stage-Gate Mandatory Inspection Tariff (Floors 1-8)",
                "amount": 4200000.00,
                "amount_formatted": "₦4,200,000.00",
                "issued_date": "15 Sep 2026",
                "due_date": "15 Oct 2026",
                "status": "DUE",
                "beneficiary": "Lagos State Materials Testing Laboratory (LSMTL)",
                "payment_gateway": "Remita",
            },
            {
                "invoice_number": "INV-2026-0062",
                "project_name": "Victoria Island Central Commercial Hub",
                "fee_category": "Environmental Impact Mitigation Assessment Fee",
                "amount": 7800000.00,
                "amount_formatted": "₦7,800,000.00",
                "issued_date": "10 Sep 2026",
                "due_date": "10 Oct 2026",
                "status": "DUE",
                "beneficiary": "Ministry of the Environment & Water Resources",
                "payment_gateway": "Remita",
            },
        ]

        for idata in invoices_data:
            StatutoryFinancialTransaction.objects.update_or_create(
                invoice_number=idata["invoice_number"],
                defaults=idata
            )

        self.stdout.write(self.style.SUCCESS("Successfully seeded Public Transparency Portal and Government Stakeholder records."))
