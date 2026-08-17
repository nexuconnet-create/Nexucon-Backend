import os
import django

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from apps.projects.models import Project
from apps.applications.models import Application
from django.contrib.auth import get_user_model

User = get_user_model()
user = User.objects.first()

for p in Project.objects.filter(status__in=['DRAFT', 'PLANNING']):
    app, created = Application.objects.get_or_create(
        project=p,
        defaults={
            'applicant': user,
            'application_type': 'General Construction Permit',
            'status': 'SUBMITTED'
        }
    )
    if created:
        print(f"Created application for project {p.name}")
    else:
        print(f"Application already exists for project {p.name}")
