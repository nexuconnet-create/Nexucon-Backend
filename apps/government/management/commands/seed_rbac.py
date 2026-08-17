from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from apps.government.models import Agency, Role, Profile

User = get_user_model()

class Command(BaseCommand):
    help = 'Seeds the database with basic RBAC roles and a test user'

    def handle(self, *args, **kwargs):
        self.stdout.write(self.style.SUCCESS('Seeding RBAC data...'))

        # 1. Create an Agency
        agency, created = Agency.objects.get_or_create(
            code='LASBCA',
            defaults={
                'name': 'Lagos State Building Control Agency',
                'description': 'Main building control agency for Lagos State'
            }
        )
        if created:
            self.stdout.write(f'Created Agency: {agency.name}')

        # 2. Create Roles
        director_role, created = Role.objects.get_or_create(
            name='Director',
            defaults={
                'permissions': [
                    'projects.view', 'projects.edit', 'projects.delete',
                    'applications.approve', 'inspections.view', 'inspections.assign',
                    'analytics.view_industry', 'approvals.override'
                ]
            }
        )
        if created:
            self.stdout.write('Created Role: Director')

        inspector_role, created = Role.objects.get_or_create(
            name='Inspector',
            defaults={
                'permissions': [
                    'projects.view', 'inspections.view', 'inspections.execute',
                    'documents.view'
                ]
            }
        )
        if created:
            self.stdout.write('Created Role: Inspector')

        # 3. Create Test Users
        director_email = 'director@lasbca.gov.ng'
        director_user, created = User.objects.get_or_create(
            email="director@lasbca.gov.ng",
            defaults={
                "username": "director@lasbca.gov.ng",
                "first_name": "Director",
                "last_name": "General",
                "is_staff": True,
                "is_superuser": True,
            }
        )
        if created:
            director_user.set_password('password123')
            director_user.save()
            
            # Create profile
            Profile.objects.create(
                user=director_user,
                agency=agency,
                role=director_role,
                approval_limit=50000000.00
            )
            self.stdout.write(f'Created Director User: {director_email} (password123)')

        inspector_email = 'inspector@lasbca.gov.ng'
        inspector_user, created = User.objects.get_or_create(
            email="inspector@lasbca.gov.ng",
            defaults={
                "username": "inspector@lasbca.gov.ng",
                "first_name": "Field",
                "last_name": "Inspector",
                "is_staff": True,
            }
        )
        if created:
            inspector_user.set_password('password123')
            inspector_user.save()
            
            # Create profile
            Profile.objects.create(
                user=inspector_user,
                agency=agency,
                role=inspector_role,
                approval_limit=0.00
            )
            self.stdout.write(f'Created Inspector User: {inspector_email} (password123)')

        self.stdout.write(self.style.SUCCESS('Successfully seeded RBAC data!'))
