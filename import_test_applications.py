import os
import django
import json

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
django.setup()

from apps.applications.models import Application
from apps.projects.models import Project
from django.contrib.auth import get_user_model

def run():
    User = get_user_model()
    
    # Use our default test account for the applicant foreign key
    applicant_user, _ = User.objects.get_or_create(
        email="skprojectx12@gmail.com",
        defaults={"username": "skprojectx12@gmail.com"}
    )
    
    json_path = "/app/test_applications.json"
    
    try:
        with open(json_path, 'r') as f:
            data = json.load(f)
    except Exception as e:
        print(f"Error reading test_applications.json: {e}")
        return
        
    applications = data.get("applications", [])
    
    count = 0
    for app_data in applications:
        app_ref = app_data.get("application_reference")
        
        # Check if exists
        if Application.objects.filter(application_reference=app_ref).exists():
            print(f"Application {app_ref} already exists. Skipping.")
            continue
            
        proj_data = app_data.get("project", {})
        proj_ref = proj_data.get("project_reference")
        
        try:
            project = Project.objects.get(reference_number=proj_ref)
        except Project.DoesNotExist:
            print(f"Project {proj_ref} not found for application {app_ref}. Skipping.")
            continue
            
        created_by = app_data.get("created_by", {})
        reviewer = app_data.get("assigned_reviewer") or {}
        sub = app_data.get("submission", {})
        
        status_val = "UNDER_REVIEW" if app_data.get("status") == "Under Review" else "SUBMITTED"
        
        app = Application.objects.create(
            application_reference=app_ref,
            title=app_data.get("title"),
            project=project,
            applicant=applicant_user,
            application_type=app_data.get("application_type"),
            status=status_val,
            priority=app_data.get("priority", "Normal"),
            created_by_name=created_by.get("name"),
            assigned_reviewer_name=reviewer.get("name") if reviewer else None,
            submission_date=sub.get("submitted_date") + "T00:00:00Z" if sub.get("submitted_date") else None,
            review_deadline=sub.get("review_deadline"),
            required_action=sub.get("required_action"),
            review_items=app_data.get("review_items", []),
            attached_documents=app_data.get("documents", [])
        )
        count += 1

    print(f"Successfully imported {count} applications!")

if __name__ == '__main__':
    run()
