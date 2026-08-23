import os
import sys
import django
import datetime
from django.utils import timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.development')
django.setup()

from apps.projects.models import Project
from apps.monitoring.models import (
    DailySiteUpdate, FieldObservation, SiteIssue, ConstructionMilestone, SiteVerification
)

def run():
    projects = list(Project.objects.all())
    if not projects:
        print("No projects found to link monitoring records.")
        return

    p1 = projects[0] # Eko Atlantic Marina Towers
    p2 = projects[1] if len(projects) > 1 else p1 # Victoria Island Financial Center
    p3 = projects[2] if len(projects) > 2 else p1 # Lekki Free Trade Zone Warehouse Complex
    p4 = projects[3] if len(projects) > 3 else p1 # Ikoyi Imperial Heights

    print(f"Seeding site monitoring data linked to projects: {[p.name for p in [p1, p2, p3, p4]]}")

    # 1. Daily Site Updates & Photos
    if DailySiteUpdate.objects.count() == 0:
        DailySiteUpdate.objects.create(
            project=p1,
            update_type='DAILY_PHOTO',
            reported_by_name='Engr. Kayode Adebayo (Site Resident Engineer)',
            progress_percentage=68,
            work_summary='Level 14 slab reinforcement placement completed. Concrete pouring scheduled for 08:00 tomorrow morning.',
            photos=[
                'https://res.cloudinary.com/fspyt1uw/image/upload/v1787390542/nexucon/daily_updates/kunjsefjt56iys4pj6sv.jpg',
                'https://res.cloudinary.com/fspyt1uw/image/upload/v1787390545/nexucon/daily_updates/lulnlngu73euc9c5byvx.jpg'
            ],
            weather_condition='Clear / Sunny (31°C)',
            workforce_count=42,
            gps_coordinates={'lat': 6.4253, 'lng': 3.4219},
            status='Active',
            priority='High'
        )

        DailySiteUpdate.objects.create(
            project=p2,
            update_type='DAILY_PHOTO',
            reported_by_name='Arch. Folashade Adeleke (Resident Supervisor)',
            progress_percentage=82,
            work_summary='Curtain wall facade glazing on North Elevation grid lines 1-6 installed. Fire-stopping sealant inspection in progress.',
            photos=[
                'https://res.cloudinary.com/fspyt1uw/image/upload/v1787390550/nexucon/daily_updates/ikalmpfdz59w8hbaxstv.jpg',
                'https://res.cloudinary.com/fspyt1uw/image/upload/v1787390555/nexucon/daily_updates/l69bx90wguqmmkkwef2c.jpg'
            ],
            weather_condition='Partly Cloudy (29°C)',
            workforce_count=35,
            gps_coordinates={'lat': 6.4281, 'lng': 3.4244},
            status='Active',
            priority='Medium'
        )

        DailySiteUpdate.objects.create(
            project=p3,
            update_type='DRONE_SURVEY',
            reported_by_name='Capt. Tunde Oladipo (UAV Certified Survey Pilot)',
            progress_percentage=45,
            work_summary='Automated photogrammetry aerial scan of logistics bay foundations and earthwork compaction grading.',
            photos=[
                'https://res.cloudinary.com/fspyt1uw/image/upload/v1787390559/nexucon/daily_updates/kzq8zuorwjbhcfsga7lm.jpg'
            ],
            drone_survey_data={
                'flight_altitude_m': 75,
                'overlap_percentage': 85,
                'ground_resolution_cm': 1.8,
                'point_cloud_url': 'https://s3.amazonaws.com/nexucon-surveys/lekki-logistics.laz'
            },
            weather_condition='Wind 8 knots / Clear (30°C)',
            workforce_count=18,
            gps_coordinates={'lat': 6.4421, 'lng': 3.6120},
            status='Approved',
            priority='High'
        )

        DailySiteUpdate.objects.create(
            project=p4,
            update_type='DAILY_PHOTO',
            reported_by_name='Engr. Chukwuma Obi (MEP Coordinator)',
            progress_percentage=55,
            work_summary='HVAC riser ductwork pressure testing and electrical cable tray trunking installation on Floors 4-7.',
            photos=[
                'https://res.cloudinary.com/fspyt1uw/image/upload/v1787390542/nexucon/daily_updates/kunjsefjt56iys4pj6sv.jpg'
            ],
            weather_condition='Clear / Dry (32°C)',
            workforce_count=28,
            gps_coordinates={'lat': 6.4520, 'lng': 3.4350},
            status='Active',
            priority='Medium'
        )
        print("Seeded 4 Daily Site Updates with site photos.")

    # 2. Field Observations
    if FieldObservation.objects.count() == 0:
        FieldObservation.objects.create(
            project=p1,
            category='QUALITY',
            title='Concrete Slump Test Consistency Verification',
            description='Slump test achieved 120mm at batching discharge chute. Core cube test samples cast and water-cured in on-site tank.',
            severity='LOW',
            status='OPEN',
            observed_by_name='Engr. Babatunde Adeleke (Building Control Inspector)',
            evidence_photos=['https://images.unsplash.com/photo-1541888946425-d0fbb180c5f2?auto=format&fit=crop&w=800&q=80'],
            corrective_action='Proceed with scheduled 28-day compression load crushing.'
        )

        FieldObservation.objects.create(
            project=p2,
            category='SAFETY',
            title='Edge Protection Guardrail Barrier at 8th Floor Void',
            description='Temporary scaffold toe-boards missing on Eastern perimeter cantilever balcony edge.',
            severity='HIGH',
            status='ACTION_REQUIRED',
            observed_by_name='Ibrahim Danladi (HSE Compliance Officer)',
            evidence_photos=['https://images.unsplash.com/photo-1504307651254-35680f356dfd?auto=format&fit=crop&w=800&q=80'],
            corrective_action='Install standard double-guardrail safety barriers with 150mm toe-board before resumption of perimeter facade work.'
        )
        print("Seeded Field Observations.")

    # 3. Site Issues
    if SiteIssue.objects.count() == 0:
        SiteIssue.objects.create(
            project=p3,
            title='Groundwater Ingress at Warehouse Basement Pit B-3',
            description='Seepage detected during dewatering pump downtime. Requires chemical waterproofing membrane application.',
            severity='HIGH',
            status='OPEN',
            assigned_to_name='Engr. Alabi Hassan (Geotechnical Lead)',
            reported_by_name='Engr. Babatunde Adeleke (Field Inspector)',
            due_date=timezone.now().date() + datetime.timedelta(days=4)
        )
        print("Seeded Site Issues.")

    # 4. Construction Milestones
    ConstructionMilestone.objects.all().delete()
    
    today = timezone.now().date()
    
    # Milestones for Project 1 (Eko Atlantic Marina Towers)
    m1_p1 = ConstructionMilestone.objects.create(
        project=p1,
        milestone_code='MS-01-PIL',
        name='Substructure Foundation Raft & Bored Piling Sign-off',
        phase='SUBSTRUCTURE',
        description='Installation of 120 bored cast-in-situ foundation piles to 45m depth and monolithic raft foundation casting.',
        sequence_order=1,
        critical_path=True,
        planned_start_date=today - datetime.timedelta(days=90),
        target_date=today - datetime.timedelta(days=30),
        actual_start_date=today - datetime.timedelta(days=90),
        actual_completion_date=today - datetime.timedelta(days=28),
        duration_days=60,
        variance_days=-2,
        status='VERIFIED',
        progress_percentage=100,
        physical_progress_notes='All 120 piles successfully cast and integrity tested. Core compression strength achieved 38.5 N/mm².',
        risk_level='LOW',
        risk_factors=['Geotechnical water ingress mitigated with chemical grouting.'],
        dependencies=[],
        linked_inspection_ids=[
            {'ref': 'INS-2026-0012', 'type': 'Foundation Inspection', 'status': 'COMPLETED', 'outcome': 'PASSED', 'date': str(today - datetime.timedelta(days=30))}
        ],
        linked_issue_ids=[],
        bim_deviation_mm=3.2,
        bim_tolerance_max_mm=15.0,
        survey_variance_meters=0.015,
        digital_eye_verified=True,
        evidence_documents=[
            {'name': '28-Day Concrete Cube Compressive Strength Certificate.pdf', 'url': 'https://assets.nexucon.gov.ng/reports/concrete_p1_ms1.pdf', 'file_type': 'PDF', 'size': '2.4 MB', 'category': 'Laboratory Test Report', 'verified': True},
            {'name': 'BIM LiDAR Deviation Heatmap & Point Cloud Overlay.pdf', 'url': 'https://assets.nexucon.gov.ng/reports/bim_heatmap_ms1.pdf', 'file_type': 'PDF', 'size': '5.1 MB', 'category': 'BIM / Digital Eye', 'verified': True},
            {'name': 'COREN Registered Structural Engineer Sign-Off.pdf', 'url': 'https://assets.nexucon.gov.ng/reports/coren_seal_ms1.pdf', 'file_type': 'PDF', 'size': '1.2 MB', 'category': 'Regulatory Seal', 'verified': True}
        ],
        evidence_photos=[
            'https://res.cloudinary.com/fspyt1uw/image/upload/v1787390542/nexucon/daily_updates/kunjsefjt56iys4pj6sv.jpg'
        ],
        verification_requirements={
            'require_inspections_passed': True,
            'require_zero_critical_defects': True,
            'require_survey_within_tolerance': True,
            'require_lab_test_evidence': True,
            'require_engineer_signoff': True
        },
        verification_signoff={
            'certificate_reference': 'CERT-MS-2026-00192',
            'signature_hash': '0xLASBCA-VERIFIED-49F12A',
            'verified_by_name': 'Engr. Abimbola Williams (LASBCA Director of Building Control)',
            'verified_by_role': 'Director of Building Control',
            'verified_at': (today - datetime.timedelta(days=28)).isoformat(),
            'notes': 'All piling tests and load distribution benchmarks passed 100%. Superstructure authorization granted.'
        },
        verified_by_name='Engr. Abimbola Williams',
        verified_at=timezone.now() - datetime.timedelta(days=28)
    )

    m2_p1 = ConstructionMilestone.objects.create(
        project=p1,
        milestone_code='MS-02-RC',
        name='Reinforced Concrete Superstructure Frame (Floors 1-15)',
        phase='STRUCTURAL_FRAME',
        description='Casting of reinforced concrete shear walls, columns, post-tensioned floor slabs, and elevator lift shafts.',
        sequence_order=2,
        critical_path=True,
        planned_start_date=today - datetime.timedelta(days=25),
        target_date=today + datetime.timedelta(days=4),
        actual_start_date=today - datetime.timedelta(days=25),
        duration_days=30,
        variance_days=0,
        status='DUE_THIS_WEEK',
        progress_percentage=95,
        physical_progress_notes='Level 14 slab fully cast. Formwork for Level 15 currently standing, rebar installation 90% completed.',
        risk_level='MEDIUM',
        risk_factors=['Approaching statutory inspection gate within 4 days.'],
        dependencies=[{'id': str(m1_p1.id), 'code': 'MS-01-PIL', 'name': m1_p1.name, 'status': 'VERIFIED', 'is_blocking': False}],
        linked_inspection_ids=[
            {'ref': 'INS-2026-0089', 'type': 'Structural Review', 'status': 'SCHEDULED', 'outcome': 'PENDING', 'date': str(today + datetime.timedelta(days=2))}
        ],
        linked_issue_ids=[],
        bim_deviation_mm=6.4,
        bim_tolerance_max_mm=15.0,
        survey_variance_meters=0.022,
        digital_eye_verified=True,
        evidence_documents=[
            {'name': 'Mill Test Certificates - High Yield Rebar Fe500.pdf', 'url': 'https://assets.nexucon.gov.ng/reports/rebar_cert_p1.pdf', 'file_type': 'PDF', 'size': '3.2 MB', 'category': 'Material Certificate', 'verified': True}
        ],
        evidence_photos=[
            'https://res.cloudinary.com/fspyt1uw/image/upload/v1787390545/nexucon/daily_updates/lulnlngu73euc9c5byvx.jpg'
        ],
        verification_requirements={
            'require_inspections_passed': True,
            'require_zero_critical_defects': True,
            'require_survey_within_tolerance': True,
            'require_lab_test_evidence': True,
            'require_engineer_signoff': True
        }
    )

    m3_p1 = ConstructionMilestone.objects.create(
        project=p1,
        milestone_code='MS-03-MEP',
        name='MEP Core Risers, Fire Sprinklers & HVAC Ducting Rough-ins',
        phase='MEP_ROUGHIN',
        description='Installation of vertical pipe risers, fire suppression manifolds, main electrical distribution busducts, and fresh air shafts.',
        sequence_order=3,
        critical_path=False,
        planned_start_date=today + datetime.timedelta(days=10),
        target_date=today + datetime.timedelta(days=45),
        duration_days=35,
        variance_days=0,
        status='PLANNED',
        progress_percentage=15,
        physical_progress_notes='Conduit sleeve embedded during slab pour. Procurement of chilled water AHUs completed.',
        risk_level='LOW',
        risk_factors=['Lead time for centrifugal chiller delivery from Hamburg confirmed.'],
        dependencies=[{'id': str(m2_p1.id), 'code': 'MS-02-RC', 'name': m2_p1.name, 'status': 'DUE_THIS_WEEK', 'is_blocking': True}],
        linked_inspection_ids=[],
        linked_issue_ids=[],
        bim_deviation_mm=0.0,
        bim_tolerance_max_mm=15.0,
        survey_variance_meters=0.0,
        evidence_documents=[]
    )

    # Milestones for Project 2 (Victoria Island Financial Center)
    m1_p2 = ConstructionMilestone.objects.create(
        project=p2,
        milestone_code='MS-02-FAC',
        name='Unitized Curtain Wall Facade & Double Glazed Thermal Envelope',
        phase='FACADE_ENVELOPE',
        description='Fixing of unitized structural glazing brackets, low-E double glazed panels, and silicone perimeter weather seals.',
        sequence_order=1,
        critical_path=True,
        planned_start_date=today - datetime.timedelta(days=40),
        target_date=today - datetime.timedelta(days=5),
        actual_start_date=today - datetime.timedelta(days=40),
        duration_days=35,
        variance_days=10,
        status='DELAYED',
        progress_percentage=70,
        is_delayed=True,
        delay_reason='Custom curved glass shipment delayed at Apapa Port customs terminal by 12 days.',
        physical_progress_notes='Bracket installation completed across Floors 1-12. Glazing panel delivery resumed yesterday.',
        risk_level='HIGH',
        risk_factors=['Apapa port logistics delay impacted installation sequencing.', '10 days behind statutory programme schedule.'],
        dependencies=[],
        linked_inspection_ids=[
            {'ref': 'INS-2026-0062', 'type': 'Safety Audit', 'status': 'COMPLETED', 'outcome': 'PASSED', 'date': str(today - datetime.timedelta(days=10))}
        ],
        linked_issue_ids=[],
        bim_deviation_mm=8.8,
        bim_tolerance_max_mm=15.0,
        survey_variance_meters=0.035,
        digital_eye_verified=True,
        evidence_documents=[
            {'name': 'Wind Tunnel Test & Glazing Structural Calculation Report.pdf', 'url': 'https://assets.nexucon.gov.ng/reports/facade_wind_p2.pdf', 'file_type': 'PDF', 'size': '6.4 MB', 'category': 'Technical Report', 'verified': True}
        ],
        evidence_photos=[
            'https://res.cloudinary.com/fspyt1uw/image/upload/v1787390550/nexucon/daily_updates/ikalmpfdz59w8hbaxstv.jpg'
        ]
    )

    m2_p2 = ConstructionMilestone.objects.create(
        project=p2,
        milestone_code='MS-03-FIN',
        name='Internal Architectural Fit-out & Raised Access Flooring',
        phase='FINISHES',
        description='Installation of acoustic drywall partitions, raised access floor pedestals, ceiling grid, and commercial restrooms.',
        sequence_order=2,
        critical_path=False,
        planned_start_date=today + datetime.timedelta(days=20),
        target_date=today + datetime.timedelta(days=60),
        duration_days=40,
        variance_days=0,
        status='BLOCKED',
        progress_percentage=0,
        physical_progress_notes='Interior fit-out staging blocked until external glazing envelope is 100% watertight.',
        risk_level='MEDIUM',
        risk_factors=['Dependent on delayed facade envelope milestone (MS-02-FAC).'],
        dependencies=[{'id': str(m1_p2.id), 'code': 'MS-02-FAC', 'name': m1_p2.name, 'status': 'DELAYED', 'is_blocking': True}],
        linked_inspection_ids=[],
        linked_issue_ids=[]
    )

    # Milestones for Project 3 (Lekki Free Trade Zone Warehouse Complex)
    m1_p3 = ConstructionMilestone.objects.create(
        project=p3,
        milestone_code='MS-01-LOG',
        name='High-Tolerance Industrial Floor Slab Laser Screed & Curing',
        phase='SUBSTRUCTURE',
        description='Laser-screeded steel fiber reinforced concrete floor slab with FM2 floor flatness tolerance for high-bay forklifts.',
        sequence_order=1,
        critical_path=True,
        planned_start_date=today - datetime.timedelta(days=15),
        target_date=today + datetime.timedelta(days=2),
        actual_start_date=today - datetime.timedelta(days=15),
        duration_days=17,
        variance_days=0,
        status='PENDING_VERIFICATION',
        progress_percentage=100,
        physical_progress_notes='Floor slab pouring completed with laser screed machinery. Surface hardener applied, 14-day wet cure concluded.',
        risk_level='LOW',
        risk_factors=['Awaiting formal regulatory flatness audit and sign-off.'],
        dependencies=[],
        linked_inspection_ids=[
            {'ref': 'INS-2026-0074', 'type': 'Site Verification', 'status': 'COMPLETED', 'outcome': 'PASSED', 'date': str(today - datetime.timedelta(days=1))}
        ],
        linked_issue_ids=[],
        bim_deviation_mm=2.1,
        bim_tolerance_max_mm=15.0,
        survey_variance_meters=0.010,
        digital_eye_verified=True,
        evidence_documents=[
            {'name': 'Floor Flatness FM2 Profilometer Survey Results.pdf', 'url': 'https://assets.nexucon.gov.ng/reports/flatness_p3.pdf', 'file_type': 'PDF', 'size': '1.8 MB', 'category': 'Survey Report', 'verified': True},
            {'name': 'Concrete Core 28-Day Strength Test (40 N/mm²).pdf', 'url': 'https://assets.nexucon.gov.ng/reports/concrete_p3.pdf', 'file_type': 'PDF', 'size': '2.1 MB', 'category': 'Laboratory Test Report', 'verified': True}
        ],
        evidence_photos=[
            'https://res.cloudinary.com/fspyt1uw/image/upload/v1787390559/nexucon/daily_updates/kzq8zuorwjbhcfsga7lm.jpg'
        ],
        verification_requirements={
            'require_inspections_passed': True,
            'require_zero_critical_defects': True,
            'require_survey_within_tolerance': True,
            'require_lab_test_evidence': True,
            'require_engineer_signoff': True
        }
    )

    print("Seeded 6 comprehensive Construction Milestones across projects.")

    # 5. Site Verifications
    if SiteVerification.objects.count() == 0:
        SiteVerification.objects.create(
            project=p1,
            method='GNSS_RTK_SURVEY',
            device_identifier='Tersus Oscar GNSS RTK #042',
            captured_coordinates={'lat': 6.425310, 'lng': 3.421920},
            approved_coordinates={'lat': 6.425308, 'lng': 3.421918},
            variance_meters=0.08,
            variance_detected=False,
            status='VERIFIED',
            verified_by_name='Surv. Olumide Balogun (Licensed Surveyor)',
            verified_at=timezone.now(),
            notes='Setback boundaries conform 100% with Lagos State Urban Planning approval coordinates.'
        )
        print("Seeded Site Verifications.")

if __name__ == '__main__':
    run()
