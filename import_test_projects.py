import os
import django
import json
import sys

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
django.setup()

from apps.projects.models import Project, ProjectProfessional
from django.contrib.gis.geos import Point

def run():
    # Read the JSON from the test_data.json file
    json_path = "/app/test_data.json"
    
    try:
        with open(json_path, 'r') as f:
            json_str = f.read()
    except Exception as e:
        print(f"Error reading test_data.json: {e}")
        return
        
    if not json_str:
        print("Empty JSON file")
        return

    data = json.loads(json_str)
    projects = data.get("projects", [])
    
    count = 0
    for p_data in projects:
        # Check if project already exists
        if Project.objects.filter(reference_number=p_data.get("project_reference")).exists():
            print(f"Project {p_data.get('project_reference')} already exists. Skipping.")
            continue
            
        location_data = p_data.get("location", {})
        lat = location_data.get("latitude")
        lng = location_data.get("longitude")
        point = None
        if lat is not None and lng is not None:
            point = Point(lng, lat, srid=4326)
            
        dev_data = p_data.get("developer", {})
        dev_details = p_data.get("development_details", {})
        reg = p_data.get("regulatory", {})
        mon = p_data.get("monitoring", {})
        
        status_map = {
            "Under Construction": "ACTIVE",
            "Pending Approval": "PLANNING",
            "Active": "ACTIVE",
            "Flagged": "SUSPENDED",
            "Completed": "COMPLETED",
            "Pending": "PLANNING"
        }
        status_val = status_map.get(p_data.get("status"), "DRAFT")
        
        try:
            val = float(p_data.get("estimated_project_value_ngn", 0))
        except:
            val = 0
            
        project = Project.objects.create(
            name=p_data.get("project_name"),
            reference_number=p_data.get("project_reference"),
            project_type=p_data.get("project_type"),
            description=p_data.get("description"),
            status=status_val,
            development_category=p_data.get("development_category"),
            estimated_project_value=val,
            start_date=p_data.get("estimated_start_date"),
            estimated_completion=p_data.get("expected_completion_date"),
            
            developer_name=dev_data.get("name"),
            developer_reg_number=dev_data.get("registration_number"),
            developer_contact_person=dev_data.get("contact_person"),
            developer_email=dev_data.get("email"),
            developer_phone=dev_data.get("phone"),
            
            site_address=location_data.get("address"),
            state=location_data.get("state"),
            lga=location_data.get("lga"),
            ward_area=location_data.get("area"),
            plot_number=location_data.get("plot_number"),
            location=point,
            
            permit_number=reg.get("planning_permit"),
            permit_status=reg.get("permit_status"),
            building_control_reference=reg.get("building_control_reference"),
            
            primary_use=dev_details.get("primary_use"),
            proposed_use=dev_details.get("proposed_use"),
            gross_floor_area=dev_details.get("gross_floor_area_sqm"),
            building_height=dev_details.get("building_height_m"),
            number_of_floors=dev_details.get("number_of_floors"),
            number_of_units=dev_details.get("number_of_units"),
            construction_method=dev_details.get("construction_method"),
            
            project_priority=p_data.get("priority", "Normal"),
            inspection_frequency=mon.get("inspection_frequency"),
            
            enable_site_monitoring=mon.get("site_monitoring_enabled", False),
            enable_gnss=mon.get("gnss_monitoring_enabled", False),
            enable_bim=mon.get("bim_monitoring_enabled", False),
            inspection_required=mon.get("inspection_required", True),
            compliance_monitoring_required=mon.get("compliance_monitoring_required", True),
            progress_reporting_required=mon.get("progress_reporting_required", False),
            site_verification_required=mon.get("site_verification_required", False),
            
            assigned_officer="skprojectx12@gmail.com",
            assigned_inspector="skprojectx12@gmail.com"
        )
        
        # Add professionals
        profs = p_data.get("professionals", [])
        for prof in profs:
            ProjectProfessional.objects.create(
                project=project,
                name=prof.get("name"),
                role=prof.get("role"),
                license_number=prof.get("registration")
            )
            
        count += 1

    print(f"Successfully imported {count} projects and assigned to skprojectx12@gmail.com!")

if __name__ == '__main__':
    run()
