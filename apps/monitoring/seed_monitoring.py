import os
import django
import datetime
from django.utils import timezone

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'nexucon_backend.settings')
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
                'https://res.cloudinary.com/depeqzb6z/image/upload/v1787389721/nexucon/daily_updates/pnfpw6dntxj1jdzmpeeq.jpg',
                'https://res.cloudinary.com/depeqzb6z/image/upload/v1787389733/nexucon/daily_updates/xiyj7aiv2oyljctn4a4i.jpg'
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
                'https://res.cloudinary.com/depeqzb6z/image/upload/v1787389749/nexucon/daily_updates/wnri4hhtms4nqzv2pe3s.jpg',
                'https://res.cloudinary.com/depeqzb6z/image/upload/v1787389760/nexucon/daily_updates/eyzfp1kanw2jswlhwh6a.jpg'
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
                'https://res.cloudinary.com/depeqzb6z/image/upload/v1787389767/nexucon/daily_updates/y0xd4k45uael2jog0csh.jpg'
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
                'https://res.cloudinary.com/depeqzb6z/image/upload/v1787389721/nexucon/daily_updates/pnfpw6dntxj1jdzmpeeq.jpg'
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
    if ConstructionMilestone.objects.count() == 0:
        ConstructionMilestone.objects.create(
            project=p1,
            name='Substructure Foundation Raft & Piling Sign-off',
            target_date=timezone.now().date() - datetime.timedelta(days=30),
            status='VERIFIED',
            progress_percentage=100,
            actual_completion_date=timezone.now().date() - datetime.timedelta(days=28),
            verified_at=timezone.now() - datetime.timedelta(days=28),
            verified_by_name='Dr. Amina Bello (Chief Structural Engineer)'
        )

        ConstructionMilestone.objects.create(
            project=p1,
            name='Superstructure Concrete Frame Level 15',
            target_date=timezone.now().date() + datetime.timedelta(days=3),
            status='UPCOMING',
            progress_percentage=75
        )

        ConstructionMilestone.objects.create(
            project=p2,
            name='Structural Roof Topping Out & Parapet Casting',
            target_date=timezone.now().date() + datetime.timedelta(days=14),
            status='UPCOMING',
            progress_percentage=60
        )
        print("Seeded Construction Milestones.")

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
