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
        "permissions": ["permits.create", "permits.read", "permits.update", "permits.delete", "inspections.create", "inspections.read", "inspections.update", "inspections.delete", "all.delete"]
    },
    {
        "name": "Director",
        "permissions": ["permits.create", "permits.read", "permits.update", "inspections.create", "inspections.read", "inspections.update"]
    },
    {
        "name": "Inspector",
        "permissions": ["inspections.create", "inspections.read", "inspections.update"]
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
