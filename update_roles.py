import os
import django
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")
django.setup()

from django.contrib.auth import get_user_model
from apps.government.models import Role, Profile
import sys

User = get_user_model()
try:
    agency_head_role = Role.objects.get(name="Agency Head")
except Role.DoesNotExist:
    print("Role 'Agency Head' not found.")
    sys.exit(1)

# Update skprojectx12
try:
    user1 = User.objects.get(email="skprojectx12@gmail.com")
    user1.primary_role = "Agency Head"
    user1.save()
    profile, _ = Profile.objects.get_or_create(user=user1)
    profile.role = agency_head_role
    profile.save()
    print(f"Updated {user1.email} to Agency Head")
except User.DoesNotExist:
    print("User skprojectx12 not found")

# Update director
try:
    director_role = Role.objects.get(name="Director")
    user2 = User.objects.get(email="director@lasbca.gov.ng")
    user2.primary_role = "Director"
    user2.save()
    profile2, _ = Profile.objects.get_or_create(user=user2)
    profile2.role = director_role
    profile2.save()
    print(f"Updated {user2.email} to Director")
except Exception as e:
    pass

# Update inspector
try:
    inspector_role = Role.objects.get(name="Inspector")
    user3 = User.objects.get(email="inspector@lasbca.gov.ng")
    user3.primary_role = "Inspector"
    user3.save()
    profile3, _ = Profile.objects.get_or_create(user=user3)
    profile3.role = inspector_role
    profile3.save()
    print(f"Updated {user3.email} to Inspector")
except Exception as e:
    pass

