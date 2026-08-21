import os
import django
import sys

# Setup Django environment
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings.base')
django.setup()

from apps.government.models import Role

roles = [
    {
        "name": "Agency Head",
        "permissions": [
            "admin",
            "projects.view", "projects.create", "projects.edit", "projects.delete",
            "applications.view", "applications.create", "applications.approve", "applications.reject",
            "inspections.view", "inspections.create", "inspections.update", "inspections.delete",
            "analytics.view_industry", "all.delete", "permits.create", "permits.read", "permits.update", "permits.delete"
        ]
    },
    {
        "name": "Director",
        "permissions": [
            "projects.view", "projects.create", "projects.edit",
            "applications.view", "applications.create", "applications.approve",
            "inspections.view", "inspections.create", "inspections.update",
            "analytics.view_industry", "permits.create", "permits.read", "permits.update"
        ]
    },
    {
        "name": "Inspector",
        "permissions": [
            "projects.view", "applications.view", "inspections.view", "inspections.create", "inspections.update", "inspections.read"
        ]
    }
]

def seed():
    for role_data in roles:
        role, created = Role.objects.get_or_create(
            name=role_data["name"],
            defaults={"permissions": role_data["permissions"]}
        )
        if created:
            print(f"Created role: {role.name}")
        else:
            print(f"Role {role.name} already exists. Updating permissions...")
            role.permissions = role_data["permissions"]
            role.save()

if __name__ == '__main__':
    seed()
    print("Roles seeded successfully.")
