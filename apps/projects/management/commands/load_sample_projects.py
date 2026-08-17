import json
import os
from django.core.management.base import BaseCommand
from apps.projects.models import Project
from django.contrib.gis.geos import Point

class Command(BaseCommand):
    help = 'Load sample projects from JSON file into the database'

    def handle(self, *args, **kwargs):
        json_file_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))), 'seed_projects.json')
        
        if not os.path.exists(json_file_path):
            self.stdout.write(self.style.ERROR(f"Could not find JSON file at {json_file_path}"))
            return

        with open(json_file_path, 'r') as file:
            data = json.load(file)

        projects_created = 0
        
        for project_data in data.get('projects', []):
            ref = project_data.get('project_reference')
            if Project.objects.filter(reference_number=ref).exists():
                self.stdout.write(self.style.WARNING(f"Project with reference {ref} already exists. Skipping."))
                continue

            loc = project_data.get('location', {})
            dev = project_data.get('developer', {})
            details = project_data.get('development_details', {})
            reg = project_data.get('regulatory', {})

            lat = loc.get('latitude')
            lng = loc.get('longitude')
            point = Point(lng, lat) if lat and lng else None

            project = Project(
                name=project_data.get('project_name', ''),
                reference_number=ref,
                project_type=project_data.get('project_type', ''),
                description=project_data.get('description', ''),
                status=project_data.get('status', 'DRAFT'),
                development_category=project_data.get('development_category', ''),
                estimated_project_value=project_data.get('estimated_project_value_ngn', None),
                number_of_floors=details.get('number_of_floors', None),
                start_date=project_data.get('estimated_start_date', None),
                estimated_completion=project_data.get('expected_completion_date', None),
                
                developer_name=dev.get('name', ''),
                developer_reg_number=dev.get('registration_number', ''),
                developer_contact_person=dev.get('contact_person', ''),
                developer_email=dev.get('email', ''),
                developer_phone=dev.get('phone', ''),
                
                site_address=loc.get('address', ''),
                state=loc.get('state', ''),
                lga=loc.get('lga', ''),
                ward_area=loc.get('area', ''),
                plot_number=loc.get('plot_number', ''),
                location=point,
                site_area=loc.get('site_area_sqm', None),
                
                primary_use=details.get('primary_use', ''),
                proposed_use=details.get('proposed_use', ''),
                gross_floor_area=details.get('gross_floor_area_sqm', None),
                building_height=details.get('building_height_m', None),
                number_of_units=details.get('number_of_units', None),
                construction_method=details.get('construction_method', ''),
                
                permit_number=reg.get('planning_permit', ''),
                permit_status=reg.get('permit_status', ''),
                planning_approval_reference=reg.get('planning_permit', ''),
                building_control_reference=reg.get('building_control_reference', ''),
                environmental_approval_reference=reg.get('environmental_approval', ''),
                approval_date=reg.get('planning_approval_date', None),
                permit_expiry_date=reg.get('permit_expiry_date', None),
            )
            project.save()
            projects_created += 1
            self.stdout.write(self.style.SUCCESS(f"Created project: {project.name}"))

        self.stdout.write(self.style.SUCCESS(f"Successfully loaded {projects_created} projects!"))
